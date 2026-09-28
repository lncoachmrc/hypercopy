"""RED integration contracts for STOP2-B PR-0 Hyperliquid-loop provider guards."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text as sa_text

from app.adapters.ratelimit import Priority
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.engine.sizing import AssetSpec
from app.models.entities import (
    CopyJob,
    CopyState,
    CredentialStatus,
    EquitySnapshot,
    Execution,
    ExecutionEpoch,
    ExecutionState,
    JobState,
    PositionLedger,
    ReconciliationRun,
    RISExSigningCredential,
    RISExTradingAccount,
    TradingAccount,
    User,
    UserState,
)
from app.services import reconcile
from app.services.execution_destination import set_user_destination
from app.services.execution_resolution import (
    UNKNOWN_EXECUTION_SLA_SECONDS,
    resolve_ambiguous_executions,
)
from app.workers import execution_worker
from app.workers.execution_worker import Worker


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


def _wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _snapshot(
    *,
    position: Decimal = Decimal("0"),
    equity: Decimal = Decimal("100"),
):
    rows = []
    if position != 0:
        rows.append(
            {
                "position": {
                    "coin": "BTC",
                    "szi": str(position),
                    "marginUsed": "5",
                    "liquidationPx": "50",
                    "leverage": {"type": "cross", "value": "1"},
                }
            }
        )
    return SimpleNamespace(
        perp_state={"assetPositions": rows},
        account_value=equity,
        free_margin=equity,
        collateral_balance=equity,
        unrealized_pnl=Decimal("0"),
        abstraction="default",
    )


class RecordingHL:
    network = "testnet"

    def __init__(self, *, snapshots: dict[str, object] | None = None):
        self.snapshots = dict(snapshots or {})
        self.calls: list[tuple[str, str | None, object | None]] = []

    async def mids(self, *, priority=None):
        self.calls.append(("mids", None, priority))
        return {"BTC": "100", "ETH": "100"}

    async def account_snapshot(self, address: str, *, priority=None):
        self.calls.append(("account_snapshot", address, priority))
        return self.snapshots.get(address, _snapshot())

    async def user_fills_by_time(self, address: str, _start_ms: int, *args, priority=None, **kwargs):
        self.calls.append(("user_fills_by_time", address, priority))
        return []

    async def query_order_by_cloid(self, address: str, _cloid: str):
        self.calls.append(("query_order_by_cloid", address, None))
        return {"status": "unknownOid"}

    async def asset_spec(self, asset: str):
        self.calls.append(("asset_spec", asset, None))
        return AssetSpec(asset, sz_decimals=3, max_leverage=50)


async def _seed_user(
    *,
    provider: str,
    state: UserState,
    copy_state: CopyState = CopyState.ACTIVE,
    seed_ledger: bool = False,
    seed_equity: bool = False,
) -> SimpleNamespace:
    user_id = uuid.uuid4()
    auth_wallet = _wallet().lower()
    hl_address = _wallet().lower()
    risex_address = _wallet().lower()
    signer = _wallet().lower()
    now = datetime.now(UTC)

    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet=auth_wallet,
                state=state,
                copy_state=copy_state,
                created_at=now,
            )
        )
        await db.flush()

        db.add(
            TradingAccount(
                user_id=user_id,
                account_address=hl_address,
                agent_address=_wallet().lower(),
                agent_name=f"stop2b-pr0-{uuid.uuid4().hex}",
            )
        )
        await db.flush()

        if provider == "risex":
            risex_account = RISExTradingAccount(
                user_id=user_id,
                account_address=risex_address,
            )
            db.add(risex_account)
            await db.flush()
            db.add(
                RISExSigningCredential(
                    risex_trading_account_id=risex_account.id,
                    signer_address=signer,
                    ciphertext_b64=f"cipher-{uuid.uuid4().hex}",
                    nonce_b64=f"nonce-{uuid.uuid4().hex}",
                    wrapped_dek_b64=f"wrapped-{uuid.uuid4().hex}",
                    wrap_nonce_b64=f"wrap-{uuid.uuid4().hex}",
                    key_provider="test",
                    key_reference=f"stop2b-pr0-{uuid.uuid4().hex}",
                    key_version=1,
                    generation=1,
                    expires_at=now + timedelta(hours=2),
                    status=CredentialStatus.ACTIVE,
                )
            )
            account_address = risex_address
        elif provider == "hyperliquid":
            account_address = hl_address
        else:
            raise ValueError(provider)

        destination = await set_user_destination(
            db,
            user_id,
            provider=provider,
            network="testnet",
            account_address=account_address,
            credential_version=1,
        )

        if seed_ledger:
            db.add(
                PositionLedger(
                    user_id=user_id,
                    asset="BTC",
                    size=Decimal("0.123"),
                    target_size=Decimal("0.123"),
                    mark_price=Decimal("100"),
                    managed=True,
                    exchange_verified_at=now - timedelta(minutes=10),
                )
            )
        if seed_equity:
            db.add(
                EquitySnapshot(
                    user_id=user_id,
                    taken_at=now - timedelta(minutes=10),
                    account_value=Decimal("123"),
                    free_margin=Decimal("120"),
                    unmanaged_margin=Decimal("0"),
                    collateral_balance=Decimal("123"),
                    unrealized_pnl=Decimal("0"),
                    account_mode="seed",
                )
            )

        await db.commit()

    return SimpleNamespace(
        user_id=user_id,
        auth_wallet=auth_wallet,
        hl_address=hl_address,
        risex_address=risex_address,
        signer=signer,
        epoch_id=destination.epoch_id,
    )


async def _ledger_state(user_id: uuid.UUID):
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return (
            row.size,
            row.target_size,
            row.mark_price,
            row.managed,
            row.exchange_verified_at,
        )


async def _equity_state(user_id: uuid.UUID):
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(EquitySnapshot)
                .where(EquitySnapshot.user_id == user_id)
                .order_by(EquitySnapshot.taken_at)
            )
        ).scalars().all()
        return [
            (
                row.id,
                row.account_value,
                row.free_margin,
                row.unmanaged_margin,
                row.collateral_balance,
                row.unrealized_pnl,
                row.account_mode,
                row.taken_at,
            )
            for row in rows
        ]


async def _count_rows(model, user_id: uuid.UUID) -> int:
    async with SessionLocal() as db:
        return len(
            (
                await db.execute(select(model).where(model.user_id == user_id))
            ).scalars().all()
        )


def _has_call(calls, method: str, address: str | None = None) -> bool:
    return any(
        call_method == method and (address is None or call_address == address)
        for call_method, call_address, _priority in calls
    )


@pytest.mark.asyncio
async def test_reconcile_active_users_excludes_risex_destination_with_legacy_hl_account():
    mp = pytest.MonkeyPatch()
    try:
        master_address = _wallet().lower()
        hl_user = await _seed_user(
            provider="hyperliquid",
            state=UserState.ACTIVE,
        )
        rx_user = await _seed_user(
            provider="risex",
            state=UserState.SUSPENDED,
            seed_ledger=True,
            seed_equity=True,
        )

        recorder = RecordingHL(
            snapshots={
                master_address: _snapshot(equity=Decimal("1000")),
                hl_user.hl_address: _snapshot(equity=Decimal("500")),
                rx_user.hl_address: _snapshot(
                    position=Decimal("0.700"),
                    equity=Decimal("777"),
                ),
            }
        )

        async def causal_order(*, required=False):
            return 9001

        async def entitled(*_args, **_kwargs):
            return {
                "entitled": True,
                "status": "active",
                "plan": "pro",
                "commercial_plan": "pro",
                "limits": {},
            }

        async def ai_policy(*_args, **_kwargs):
            return SimpleNamespace(
                factor=Decimal("1"),
                effective_mode="OFF",
                effective=False,
            )

        async def protected(_db, **kwargs):
            return kwargs["desired_target"]

        own_users = {hl_user.user_id, rx_user.user_id}
        mp.setattr(reconcile.settings, "HYPERLIQUID_MASTER_ADDRESS", master_address)
        mp.setattr(reconcile, "master_snapshot_started_order", causal_order)
        mp.setattr(reconcile, "entitlement", entitled)
        mp.setattr(reconcile, "read_ai_execution_policy", ai_policy)
        mp.setattr(reconcile, "protected_reconcile_target", protected)
        mp.setattr(
            reconcile,
            "is_master_source_user",
            lambda user: user.id not in own_users,
        )

        async with SessionLocal() as db:
            await reconcile.reconcile_active_users(
                db,
                recorder,
                limit=1,
                master_hl=recorder,
            )

        baseline_calls = list(recorder.calls)
        baseline_runs = await _count_rows(ReconciliationRun, hl_user.user_id)

        async with SessionLocal() as db:
            persisted_hl = await db.get(User, hl_user.user_id)
            persisted_rx = await db.get(User, rx_user.user_id)
            persisted_hl.state = UserState.SUSPENDED
            persisted_rx.state = UserState.ACTIVE
            await db.commit()

        rx_ledger_before = await _ledger_state(rx_user.user_id)
        rx_equity_before = await _equity_state(rx_user.user_id)
        recorder.calls.clear()

        async with SessionLocal() as db:
            await reconcile.reconcile_active_users(
                db,
                recorder,
                limit=1,
                master_hl=recorder,
            )

        rx_calls = list(recorder.calls)
        rx_runs = await _count_rows(ReconciliationRun, rx_user.user_id)
        rx_jobs = await _count_rows(CopyJob, rx_user.user_id)
        rx_ledger_after = await _ledger_state(rx_user.user_id)
        rx_equity_after = await _equity_state(rx_user.user_id)

        assert _has_call(baseline_calls, "account_snapshot", hl_user.hl_address)
        assert baseline_runs >= 1
        assert not _has_call(rx_calls, "account_snapshot", rx_user.hl_address), (
            "RED T1: reconcile_active_users used Hyperliquid follower truth for "
            "an active RISEx destination that still has a legacy TradingAccount"
        )
        assert rx_runs == 0
        assert rx_jobs == 0
        assert rx_ledger_after == rx_ledger_before
        assert rx_equity_after == rx_equity_before
    finally:
        mp.undo()
        await engine.dispose()
        await position_ledger_lock_engine.dispose()


@pytest.mark.asyncio
async def test_run_reconcile_if_leader_ignores_networks_owned_only_by_risex_users():
    mp = pytest.MonkeyPatch()
    try:
        master_address = _wallet().lower()
        rx_user = await _seed_user(
            provider="risex",
            state=UserState.ACTIVE,
        )
        hl_user = await _seed_user(
            provider="hyperliquid",
            state=UserState.SUSPENDED,
        )

        events: list[tuple[str, str | None, object | None]] = []
        follower = RecordingHL()
        master = RecordingHL(
            snapshots={master_address: _snapshot(equity=Decimal("1000"))}
        )
        worker = object.__new__(Worker)
        worker.master_hl = master

        def follower_hl(network):
            events.append(("follower_adapter", str(network), None))
            return follower

        async def resolver(_db, _hl):
            events.append(("resolver", None, None))
            return {"checked": 0, "resolved": 0, "quarantined": 0, "aged": 0}

        async def reconciler(_db, _follower, *, master_hl=None, **_kwargs):
            events.append(("reconcile", None, None))
            await master_hl.account_snapshot(
                master_address,
                priority=Priority.MASTER_STATE,
            )
            await master_hl.mids(priority=Priority.RECONCILE)
            return 0

        scoped_ids = (rx_user.user_id, hl_user.user_id)

        def scoped_text(sql):
            raw = str(sql)
            if "SELECT DISTINCT u.execution_network" in raw:
                raw = raw.replace(
                    "WHERE u.state = 'ACTIVE'",
                    (
                        "WHERE u.id IN "
                        f"('{scoped_ids[0]}'::uuid, '{scoped_ids[1]}'::uuid) "
                        "AND u.state = 'ACTIVE'"
                    ),
                )
            return sa_text(raw)

        worker.follower_hl = follower_hl
        mp.setattr(execution_worker, "text", scoped_text)
        mp.setattr(execution_worker, "resolve_ambiguous_executions", resolver)
        mp.setattr(execution_worker, "reconcile_active_users", reconciler)

        await worker.run_reconcile_if_leader()
        risex_only_events = list(events)
        risex_only_master_calls = list(master.calls)

        async with SessionLocal() as db:
            persisted_hl = await db.get(User, hl_user.user_id)
            persisted_hl.state = UserState.ACTIVE
            await db.commit()

        events.clear()
        master.calls.clear()
        follower.calls.clear()
        await worker.run_reconcile_if_leader()
        hl_baseline_events = list(events)
        hl_baseline_master_calls = list(master.calls)

        assert _has_call(
            hl_baseline_master_calls,
            "account_snapshot",
            master_address,
        )
        assert _has_call(hl_baseline_master_calls, "mids")
        assert any(event[0] == "follower_adapter" for event in hl_baseline_events)
        assert any(event[0] == "resolver" for event in hl_baseline_events)
        assert any(event[0] == "reconcile" for event in hl_baseline_events)
        assert risex_only_events == [], (
            "RED T2: run_reconcile_if_leader derived a Hyperliquid cycle network "
            "from an active RISEx user solely because a legacy TradingAccount exists"
        )
        assert risex_only_master_calls == []
    finally:
        mp.undo()
        await engine.dispose()
        await position_ledger_lock_engine.dispose()


@pytest.mark.asyncio
async def test_refresh_follower_observability_excludes_risex_destination_with_legacy_hl_account():
    mp = pytest.MonkeyPatch()
    try:
        hl_user = await _seed_user(
            provider="hyperliquid",
            state=UserState.ACTIVE,
            seed_ledger=True,
            seed_equity=True,
        )
        rx_user = await _seed_user(
            provider="risex",
            state=UserState.ACTIVE,
            seed_ledger=True,
            seed_equity=True,
        )

        recorder = RecordingHL(
            snapshots={
                hl_user.hl_address: _snapshot(
                    position=Decimal("0.500"),
                    equity=Decimal("500"),
                ),
                rx_user.hl_address: _snapshot(
                    position=Decimal("0.900"),
                    equity=Decimal("900"),
                ),
            }
        )
        worker = object.__new__(Worker)
        worker.followers = {"testnet": recorder}

        real_user_network_state = execution_worker.user_network_state
        own_users = {hl_user.user_id, rx_user.user_id}

        async def scoped_network_state(db, user_id):
            if user_id in own_users:
                return await real_user_network_state(db, user_id)
            return SimpleNamespace(
                network="mainnet",
                provider="hyperliquid",
                epoch_id=uuid.uuid4(),
                started_at=datetime.now(UTC),
            )

        mp.setattr(execution_worker, "user_network_state", scoped_network_state)

        hl_ledger_before = await _ledger_state(hl_user.user_id)
        hl_equity_before = await _equity_state(hl_user.user_id)
        rx_ledger_before = await _ledger_state(rx_user.user_id)
        rx_equity_before = await _equity_state(rx_user.user_id)

        async with SessionLocal() as db:
            refreshed = await worker._refresh_follower_observability(db, "testnet")

        calls = list(recorder.calls)
        hl_ledger_after = await _ledger_state(hl_user.user_id)
        hl_equity_after = await _equity_state(hl_user.user_id)
        rx_ledger_after = await _ledger_state(rx_user.user_id)
        rx_equity_after = await _equity_state(rx_user.user_id)

        assert refreshed >= 1
        assert _has_call(calls, "account_snapshot", hl_user.hl_address)
        assert hl_ledger_after is not None
        assert hl_ledger_after[0] == Decimal("0.500")
        assert hl_ledger_after != hl_ledger_before
        assert len(hl_equity_after) == len(hl_equity_before) + 1
        assert not _has_call(calls, "account_snapshot", rx_user.hl_address), (
            "RED T3: follower observability refreshed an active RISEx destination "
            "from its legacy Hyperliquid TradingAccount"
        )
        assert rx_ledger_after == rx_ledger_before
        assert rx_equity_after == rx_equity_before
    finally:
        mp.undo()
        await engine.dispose()
        await position_ledger_lock_engine.dispose()


async def _seed_ambiguous_execution(
    fixture: SimpleNamespace,
    *,
    provider: str | None,
) -> uuid.UUID:
    now = datetime.now(UTC)
    created_at = now - timedelta(seconds=UNKNOWN_EXECUTION_SLA_SECONDS + 30)
    async with SessionLocal() as db:
        epoch = await db.get(ExecutionEpoch, fixture.epoch_id)
        epoch.started_at = now - timedelta(
            seconds=UNKNOWN_EXECUTION_SLA_SECONDS + 600
        )

        job = CopyJob(
            user_id=fixture.user_id,
            execution_epoch_id=fixture.epoch_id if provider is not None else None,
            execution_provider=provider,
            execution_network="testnet" if provider is not None else None,
            asset="BTC",
            origin="EVENT",
            state=JobState.SKIPPED,
            correlation_id=uuid.uuid4().hex,
            context={"follower_network": "testnet"},
            created_at=created_at,
        )
        db.add(job)
        await db.flush()

        execution = Execution(
            copy_job_id=job.id,
            user_id=fixture.user_id,
            execution_epoch_id=fixture.epoch_id if provider is not None else None,
            execution_provider=provider,
            execution_network="testnet" if provider is not None else None,
            attempt_kind="o",
            cloid="0x" + uuid.uuid4().hex,
            state=ExecutionState.SUBMITTING,
            asset="BTC",
            is_buy=True,
            requested_size=Decimal("0.400"),
            reduce_only=False,
            limit_px=Decimal("100"),
            created_at=created_at,
        )
        db.add(execution)
        await db.commit()
        return execution.id


@pytest.mark.asyncio
async def test_hyperliquid_ambiguous_resolver_never_touches_risex_execution():
    try:
        hl_user = await _seed_user(
            provider="hyperliquid",
            state=UserState.ACTIVE,
        )
        rx_user = await _seed_user(
            provider="risex",
            state=UserState.ACTIVE,
        )
        hl_execution_id = await _seed_ambiguous_execution(
            hl_user,
            provider=None,
        )
        rx_execution_id = await _seed_ambiguous_execution(
            rx_user,
            provider="risex",
        )

        recorder = RecordingHL(
            snapshots={
                hl_user.hl_address: _snapshot(
                    position=Decimal("0.400"),
                    equity=Decimal("400"),
                ),
                rx_user.hl_address: _snapshot(
                    position=Decimal("0.900"),
                    equity=Decimal("900"),
                ),
            }
        )

        async with SessionLocal() as db:
            hl_result = await resolve_ambiguous_executions(
                db,
                recorder,
                user_id=hl_user.user_id,
            )

        hl_calls = list(recorder.calls)
        recorder.calls.clear()

        async with SessionLocal() as db:
            rx_result = await resolve_ambiguous_executions(
                db,
                recorder,
                user_id=rx_user.user_id,
            )

        rx_calls = list(recorder.calls)

        async with SessionLocal() as db:
            hl_execution = await db.get(Execution, hl_execution_id)
            rx_execution = await db.get(Execution, rx_execution_id)
            rx_job = await db.get(CopyJob, rx_execution.copy_job_id)

        assert hl_result["checked"] >= 1
        assert _has_call(
            hl_calls,
            "query_order_by_cloid",
            hl_user.hl_address,
        )
        assert hl_execution is not None
        assert rx_result["checked"] == 0, (
            "RED T4: Hyperliquid ambiguous resolver selected an Execution whose "
            "persisted execution_provider is 'risex'"
        )
        assert not any(
            address == rx_user.hl_address
            for _method, address, _priority in rx_calls
        )
        assert rx_execution is not None
        assert rx_execution.state == ExecutionState.SUBMITTING
        assert rx_execution.resolved_at is None
        assert rx_job is not None and rx_job.state == JobState.SKIPPED
    finally:
        await engine.dispose()
        await position_ledger_lock_engine.dispose()
