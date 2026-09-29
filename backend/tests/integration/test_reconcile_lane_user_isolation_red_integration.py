"""RED integration contracts: one follower must not stop the reconcile cycle.

Production evidence (28-29/09): ``reconcile_active_users`` raised on the first
failing follower, so every later follower (always the same ones, ordered by
``created_at``) was never reconciled. A RECONCILE-lane exhaustion must defer
the rest of the cycle instead of failing it, and the next cycle must start
from the followers that waited the longest.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.adapters.ratelimit import RateLimitExhausted
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.engine.sizing import AssetSpec
from app.models.entities import (
    CopyState,
    CredentialStatus,
    ReconciliationRun,
    RISExSigningCredential,
    RISExTradingAccount,
    TradingAccount,
    User,
    UserState,
)
from app.services import reconcile
from app.services.execution_destination import set_user_destination


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


def _wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _snapshot(equity: Decimal = Decimal("100")):
    return SimpleNamespace(
        perp_state={"assetPositions": []},
        account_value=equity,
        free_margin=equity,
        collateral_balance=equity,
        unrealized_pnl=Decimal("0"),
        abstraction="default",
    )


class RecordingHL:
    network = "testnet"

    def __init__(self, master_address: str) -> None:
        self.master_address = master_address
        self.fail_master = False
        self.calls: list[tuple[str, str | None]] = []

    async def mids(self, *, priority=None):
        self.calls.append(("mids", None))
        return {"BTC": "100", "ETH": "100"}

    async def account_snapshot(self, address: str, *, priority=None):
        self.calls.append(("account_snapshot", address))
        if address == self.master_address and self.fail_master:
            raise RuntimeError("master snapshot unavailable")
        return _snapshot(Decimal("1000") if address == self.master_address else Decimal("100"))

    async def user_fills_by_time(self, address: str, _start_ms: int, *args, **kwargs):
        self.calls.append(("user_fills_by_time", address))
        return []

    async def query_order_by_cloid(self, address: str, _cloid: str):
        self.calls.append(("query_order_by_cloid", address))
        return {"status": "unknownOid"}

    async def asset_spec(self, asset: str):
        return AssetSpec(asset, sz_decimals=3, max_leverage=50)


class RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def _record(self, level: str, message: str, *args, **kwargs) -> None:
        self.records.append((level, message, dict(kwargs.get("extra") or {})))

    def warning(self, message: str, *args, **kwargs) -> None:
        self._record("warning", message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs) -> None:
        self._record("info", message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs) -> None:
        self._record("error", message, *args, **kwargs)

    def exception(self, message: str, *args, **kwargs) -> None:
        self._record("exception", message, *args, **kwargs)

    def debug(self, message: str, *args, **kwargs) -> None:
        self._record("debug", message, *args, **kwargs)

    def events(self, code: str) -> list[dict]:
        return [extra for _level, _message, extra in self.records if extra.get("event_code") == code]


async def _seed_user(*, provider: str, created_at: datetime) -> SimpleNamespace:
    user_id = uuid.uuid4()
    hl_address = _wallet().lower()
    risex_address = _wallet().lower()
    now = datetime.now(UTC)
    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet=_wallet().lower(),
                state=UserState.ACTIVE,
                copy_state=CopyState.ACTIVE,
                created_at=created_at,
            )
        )
        await db.flush()
        db.add(
            TradingAccount(
                user_id=user_id,
                account_address=hl_address,
                agent_address=_wallet().lower(),
                agent_name=f"reconcile-lane-{uuid.uuid4().hex}",
            )
        )
        await db.flush()
        account_address = hl_address
        if provider == "risex":
            risex_account = RISExTradingAccount(user_id=user_id, account_address=risex_address)
            db.add(risex_account)
            await db.flush()
            db.add(
                RISExSigningCredential(
                    risex_trading_account_id=risex_account.id,
                    signer_address=_wallet().lower(),
                    ciphertext_b64=f"cipher-{uuid.uuid4().hex}",
                    nonce_b64=f"nonce-{uuid.uuid4().hex}",
                    wrapped_dek_b64=f"wrapped-{uuid.uuid4().hex}",
                    wrap_nonce_b64=f"wrap-{uuid.uuid4().hex}",
                    key_provider="test",
                    key_reference=f"reconcile-lane-{uuid.uuid4().hex}",
                    key_version=1,
                    generation=1,
                    expires_at=now + timedelta(hours=2),
                    status=CredentialStatus.ACTIVE,
                )
            )
            account_address = risex_address
        await set_user_destination(
            db,
            user_id,
            provider=provider,
            network="testnet",
            account_address=account_address,
            credential_version=1,
        )
        await db.commit()
    return SimpleNamespace(user_id=user_id, hl_address=hl_address)


async def _runs(user_id: uuid.UUID) -> int:
    async with SessionLocal() as db:
        return int(
            (
                await db.execute(
                    select(func.count()).select_from(ReconciliationRun).where(ReconciliationRun.user_id == user_id)
                )
            ).scalar_one()
        )


async def _prepare(mp: pytest.MonkeyPatch) -> SimpleNamespace:
    base = datetime.now(UTC) - timedelta(days=3650)
    first = await _seed_user(provider="hyperliquid", created_at=base)
    second = await _seed_user(provider="hyperliquid", created_at=base + timedelta(seconds=1))
    third = await _seed_user(provider="hyperliquid", created_at=base + timedelta(seconds=2))
    risex = await _seed_user(provider="risex", created_at=base + timedelta(seconds=3))
    master_address = _wallet().lower()
    recorder = RecordingHL(master_address)
    recording_log = RecordingLog()
    own = {first.user_id, second.user_id, third.user_id, risex.user_id}

    async def causal_order(*, required=False):
        return 9001

    async def entitled(*_args, **_kwargs):
        return {"entitled": True, "status": "active", "plan": "pro", "commercial_plan": "pro", "limits": {}}

    async def ai_policy(*_args, **_kwargs):
        return SimpleNamespace(factor=Decimal("1"), effective_mode="OFF", effective=False)

    async def protected(_db, **kwargs):
        return kwargs["desired_target"]

    failures: dict[uuid.UUID, BaseException] = {}
    served: list[uuid.UUID] = []
    original = reconcile.reconcile_user

    async def reconcile_user(db, hl, user, **kwargs):
        served.append(user.id)
        failure = failures.get(user.id)
        if failure is not None:
            raise failure
        return await original(db, hl, user, **kwargs)

    mp.setattr(reconcile.settings, "HYPERLIQUID_MASTER_ADDRESS", master_address)
    mp.setattr(reconcile, "master_snapshot_started_order", causal_order)
    mp.setattr(reconcile, "entitlement", entitled)
    mp.setattr(reconcile, "read_ai_execution_policy", ai_policy)
    mp.setattr(reconcile, "protected_reconcile_target", protected)
    mp.setattr(reconcile, "is_master_source_user", lambda user: user.id not in own)
    mp.setattr(reconcile, "reconcile_user", reconcile_user)
    mp.setattr(reconcile, "log", recording_log)
    return SimpleNamespace(
        first=first,
        second=second,
        third=third,
        risex=risex,
        recorder=recorder,
        log=recording_log,
        failures=failures,
        served=served,
    )


async def _cycle(ctx: SimpleNamespace) -> BaseException | None:
    try:
        async with SessionLocal() as db:
            await reconcile.reconcile_active_users(db, ctx.recorder, master_hl=ctx.recorder)
    except BaseException as exc:  # recorded, asserted below
        return exc
    return None


def _own(ctx: SimpleNamespace, served: list[uuid.UUID]) -> list[uuid.UUID]:
    own = [ctx.first.user_id, ctx.second.user_id, ctx.third.user_id, ctx.risex.user_id]
    return [user_id for user_id in served if user_id in own]


@pytest.mark.asyncio
async def test_generic_follower_error_does_not_stop_the_cycle():
    mp = pytest.MonkeyPatch()
    try:
        ctx = await _prepare(mp)
        ctx.failures[ctx.second.user_id] = RuntimeError("follower snapshot malformed")

        outcome = await _cycle(ctx)
        order = _own(ctx, ctx.served)
        first_runs = await _runs(ctx.first.user_id)
        third_runs = await _runs(ctx.third.user_id)
        failed = ctx.log.events("RECONCILE_USER_FAILED")
        completed = ctx.log.events("RECONCILE_CYCLE_COMPLETED")

        assert order[:2] == [ctx.first.user_id, ctx.second.user_id]
        assert first_runs >= 1
        assert ctx.risex.user_id not in order, "PR-0: RISEx destinations stay out of the HL loop"

        assert outcome is None, (
            f"RED C3: a generic error on one follower must not abort the cycle, got {outcome!r}"
        )
        assert order == [ctx.first.user_id, ctx.second.user_id, ctx.third.user_id]
        assert third_runs >= 1, "RED C3: the follower after the failing one must be reconciled"
        assert [event.get("user_id") for event in failed] == [str(ctx.second.user_id)], (
            f"RED C3: RECONCILE_USER_FAILED must name the failing follower, got {ctx.log.records}"
        )
        assert failed[0].get("error_type") == "RuntimeError"
        assert completed and completed[-1].get("failed") == 1, (
            f"RED C3: RECONCILE_CYCLE_COMPLETED must report the failure, got {completed}"
        )
        assert completed[-1].get("reconciled") == 2
    finally:
        mp.undo()
        await engine.dispose()
        await position_ledger_lock_engine.dispose()


@pytest.mark.asyncio
async def test_limiter_exhaustion_defers_rest_of_cycle_and_rotates_next_cycle():
    mp = pytest.MonkeyPatch()
    try:
        ctx = await _prepare(mp)
        ctx.failures[ctx.second.user_id] = RateLimitExhausted(
            "No headroom for weight 22 on the RECONCILE lane after 15s"
        )

        first_outcome = await _cycle(ctx)
        first_order = _own(ctx, ctx.served)
        deferred = ctx.log.events("RECONCILE_USER_DEFERRED_LIMITER")
        completed = ctx.log.events("RECONCILE_CYCLE_COMPLETED")

        ctx.failures.clear()
        ctx.served.clear()
        second_outcome = await _cycle(ctx)
        second_order = _own(ctx, ctx.served)

        ctx.recorder.fail_master = True
        ctx.served.clear()
        master_outcome = await _cycle(ctx)
        master_served = _own(ctx, ctx.served)

        assert first_order[:2] == [ctx.first.user_id, ctx.second.user_id]
        assert ctx.risex.user_id not in first_order + second_order, (
            "PR-0: RISEx destinations stay out of the HL loop"
        )
        assert isinstance(master_outcome, RuntimeError), (
            f"a master snapshot failure must still abort the cycle, got {master_outcome!r}"
        )
        assert master_served == []

        assert first_outcome is None, (
            f"RED C3: limiter exhaustion must defer the rest of the cycle, not raise; got {first_outcome!r}"
        )
        assert first_order == [ctx.first.user_id, ctx.second.user_id], (
            "RED C3: after RateLimitExhausted the remaining followers must wait for the next cycle"
        )
        assert [event.get("user_id") for event in deferred] == [str(ctx.second.user_id)], (
            f"RED C3: RECONCILE_USER_DEFERRED_LIMITER must name the follower, got {ctx.log.records}"
        )
        assert completed and int(completed[-1].get("deferred", 0)) >= 1
        assert second_outcome is None
        assert ctx.third.user_id in second_order and ctx.first.user_id in second_order
        assert second_order.index(ctx.third.user_id) < second_order.index(ctx.first.user_id), (
            "RED C3: the next cycle must serve the follower that waited (third) before "
            f"the one reconciled last cycle (first); order was {second_order}"
        )
    finally:
        mp.undo()
        await engine.dispose()
        await position_ledger_lock_engine.dispose()
