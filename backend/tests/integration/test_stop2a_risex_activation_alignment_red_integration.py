"""RED integration contracts for STOP2-A RISEx activation alignment.

Only tests live in this PR.  No production symbol added here may be satisfied by
copying the Hyperliquid loop: the GREEN must normalize provider reads and feed
one shared reconciliation core.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from app.adapters import risex as risex_adapter
from app.adapters import risex_signed_testnet_http
from app.api import activation
from app.api.router import http_router
from app.core.crypto import crypto
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.engine.sizing import AssetSpec
from app.models.entities import (
    AuditLog,
    CopyJob,
    CopyState,
    CredentialStatus,
    Execution,
    ExecutionState,
    JobState,
    PositionLedger,
    RISExSigningCredential,
    RISExTradingAccount,
    RiskProfile,
    TradingAccount,
    User,
    UserState,
)
from app.services import reconcile, risex_copy_execution
from app.services.execution_destination import set_user_destination, user_destination_state
from app.services.queue import publish_job

pytestmark = pytest.mark.skipif(
    __import__("os").getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)

MASTER = "0x" + ("11" * 20)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_around_test():
    await engine.dispose()
    await position_ledger_lock_engine.dispose()
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


def _require_symbol(module, name: str):
    value = getattr(module, name, None)
    assert callable(value), f"RED: missing symbol {module.__name__}.{name}"
    return value


def _wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


async def _seed_user(
    *,
    provider: str,
    copy_state: CopyState,
    positions: dict[str, Decimal] | None = None,
    managed: bool = True,
    max_positions: int = 10,
    max_total_exposure: Decimal = Decimal("1000"),
    max_asset_exposure: Decimal = Decimal("1000"),
    max_notional_per_trade: Decimal = Decimal("1000"),
    max_leverage: Decimal = Decimal("10"),
    credential_status: CredentialStatus = CredentialStatus.ACTIVE,
    expires_at: datetime | None = None,
) -> SimpleNamespace:
    user_id = uuid.uuid4()
    wallet = _wallet().lower()
    signer = _wallet().lower()
    expires_at = expires_at or (datetime.now(UTC) + timedelta(hours=2))
    async with SessionLocal() as db:
        user = User(
            id=user_id,
            auth_wallet=wallet,
            state=UserState.ACTIVE,
            copy_state=copy_state,
        )
        db.add(user)
        await db.flush()
        db.add(
            RiskProfile(
                user_id=user_id,
                multiplier=Decimal("1"),
                min_notional=Decimal("10"),
                max_notional_per_trade=max_notional_per_trade,
                max_total_exposure=max_total_exposure,
                max_asset_exposure=max_asset_exposure,
                max_leverage=max_leverage,
                max_positions=max_positions,
                max_slippage_bps=50,
                allow_assets=["BTC", "ETH", "SOL"],
                block_assets=[],
            )
        )
        await db.flush()

        generation = 1
        if provider == "hyperliquid":
            db.add(
                TradingAccount(
                    user_id=user_id,
                    account_address=wallet,
                    agent_address="0x" + ("ab" * 20),
                    agent_name="stop2a-red",
                )
            )
            await db.flush()
            destination = await set_user_destination(
                db,
                user_id,
                provider="hyperliquid",
                network="testnet",
                account_address=wallet,
                credential_version=1,
            )
        elif provider == "risex":
            account = RISExTradingAccount(user_id=user_id, account_address=wallet)
            db.add(account)
            await db.flush()
            db.add(
                RISExSigningCredential(
                    risex_trading_account_id=account.id,
                    signer_address=signer,
                    ciphertext_b64="red-ciphertext",
                    nonce_b64="red-nonce",
                    wrapped_dek_b64="red-wrapped",
                    wrap_nonce_b64="red-wrapnonce",
                    key_provider="test",
                    key_reference="stop2a-red",
                    key_version=1,
                    generation=generation,
                    expires_at=expires_at,
                    status=credential_status,
                )
            )
            await db.flush()
            destination = await set_user_destination(
                db,
                user_id,
                provider="risex",
                network="testnet",
                account_address=wallet,
                credential_version=generation,
            )
        else:
            raise AssertionError(provider)

        for asset, size in sorted((positions or {}).items()):
            db.add(
                PositionLedger(
                    user_id=user_id,
                    asset=asset,
                    size=size,
                    target_size=size,
                    mark_price=Decimal("100"),
                    managed=managed,
                )
            )
        await db.commit()
        return SimpleNamespace(
            user_id=user_id,
            wallet=wallet,
            epoch_id=destination.epoch_id,
            generation=generation,
            signer=signer,
        )


class _FakeHL:
    network = "testnet"

    def __init__(
        self,
        *,
        positions: dict[str, Decimal] | None = None,
        equity: Decimal = Decimal("100"),
        free_margin: Decimal = Decimal("1000"),
        marks: dict[str, Decimal] | None = None,
    ):
        self.positions = dict(positions or {})
        self.equity = equity
        self.free_margin = free_margin
        self.marks = dict(marks or {})

    async def account_snapshot(self, _account: str, *, priority=None):
        rows = []
        for asset, size in sorted(self.positions.items()):
            if size == 0:
                continue
            rows.append(
                {
                    "position": {
                        "coin": asset,
                        "szi": str(size),
                        "marginUsed": "0",
                        "liquidationPx": "50",
                        "leverage": {"type": "cross", "value": "1"},
                    }
                }
            )
        return SimpleNamespace(
            perp_state={"assetPositions": rows},
            account_value=self.equity,
            free_margin=self.free_margin,
            collateral_balance=self.equity,
            unrealized_pnl=Decimal("0"),
            abstraction="default",
        )

    async def user_fills_by_time(self, _account: str, _start_ms: int):
        return []

    async def mids(self, *, priority=None):
        return {asset: str(mark) for asset, mark in self.marks.items()}

    async def asset_spec(self, asset: str):
        return AssetSpec(asset, sz_decimals=3, max_leverage=50)


def _observation(
    fixture: SimpleNamespace,
    *,
    positions: dict[str, Decimal],
    marks: dict[str, Decimal],
    equity: Decimal = Decimal("100"),
    free_margin: Decimal = Decimal("1000"),
    unmanaged_margin: Decimal | None = Decimal("0"),
):
    assets = set(positions) | set(marks)
    return SimpleNamespace(
        provider="risex",
        network="testnet",
        epoch_id=fixture.epoch_id,
        account_address=fixture.wallet,
        account_equity=equity,
        free_margin=free_margin,
        collateral_balance=equity,
        unrealized_pnl=Decimal("0"),
        account_mode="risex",
        positions=dict(positions),
        marks={asset: Decimal(str(marks.get(asset, 100))) for asset in assets},
        liquidation_prices={asset: Decimal("50") for asset in positions},
        unmanaged_margin=unmanaged_margin,
        follower_configs={},
        asset_specs={asset: AssetSpec(asset, sz_decimals=3, max_leverage=50) for asset in assets},
        fills_synced=0,
    )


def _entitled_payload() -> dict:
    return {
        "entitled": True,
        "status": "active",
        "plan": "pro",
        "commercial_plan": "pro",
        "limits": {},
    }


def _install_common_reconcile_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def entitled(*_args, **_kwargs):
        return _entitled_payload()

    async def ai_policy(*_args, **_kwargs):
        return SimpleNamespace(factor=Decimal("1"), effective_mode="OFF", effective=False)

    async def protected(_db, **kwargs):
        return kwargs["desired_target"]

    monkeypatch.setattr(reconcile, "entitlement", entitled)
    monkeypatch.setattr(reconcile, "read_ai_execution_policy", ai_policy)
    monkeypatch.setattr(reconcile, "protected_reconcile_target", protected)


def _install_signed_write_tripwires(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden_async(*_args, **_kwargs):
        raise AssertionError("RED: /copy/resume performed worker-only signed I/O")

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("RED: /copy/resume decrypted a credential or built signed transport")

    monkeypatch.setattr(risex_adapter.RISExAdapter, "place_ioc", forbidden_async)
    monkeypatch.setattr(risex_copy_execution, "claim_risex_first_post", forbidden_async)
    monkeypatch.setattr(crypto, "decrypt", forbidden_sync)
    monkeypatch.setattr(
        risex_signed_testnet_http,
        "RISExSignedTestnetHTTPTransport",
        forbidden_sync,
    )
    monkeypatch.setattr(activation, "RISExSignedTestnetHTTPTransport", forbidden_sync, raising=False)


def _install_activation_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fixture: SimpleNamespace,
    master_positions: dict[str, Decimal],
    follower_positions: dict[str, Decimal],
    provider_reads: list[str] | None = None,
    rotate_after_read: bool = False,
    session_active: bool = True,
    session_not_expired: bool = True,
    perps_permission: bool = True,
    deployment_identity_verified: bool = True,
    verified_account: str | None = None,
    verified_signer: str | None = None,
) -> None:
    provider_reads = provider_reads if provider_reads is not None else []
    marks = {asset: Decimal("100") for asset in set(master_positions) | set(follower_positions)}

    monkeypatch.setattr(activation.settings, "HYPERLIQUID_MASTER_ADDRESS", MASTER)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")

    async def live_allowed(*_args, **_kwargs):
        return True

    async def causal_order(*, required=False):
        assert required is True
        return 101

    async def entitled(*_args, **_kwargs):
        return _entitled_payload()

    async def no_repair(*_args, **_kwargs):
        return 0

    def env_gate(*_args, **_kwargs):
        provider_reads.append("gate")

    async def risex_reader(*_args, **_kwargs):
        provider_reads.append("portfolio")
        if rotate_after_read:
            async with SessionLocal() as db:
                row = (
                    await db.execute(
                        select(RISExSigningCredential)
                        .join(
                            RISExTradingAccount,
                            RISExTradingAccount.id
                            == RISExSigningCredential.risex_trading_account_id,
                        )
                        .where(RISExTradingAccount.user_id == fixture.user_id)
                    )
                ).scalar_one()
                row.generation += 1
                await db.commit()
        return _observation(
            fixture,
            positions=follower_positions,
            marks=marks,
        )

    async def verify_binding(*_args, **kwargs):
        provider_reads.append("session")
        account = kwargs.get("account_address") or kwargs.get("account") or fixture.wallet
        signer = kwargs.get("signer_address") or kwargs.get("signer") or fixture.signer
        assert str(account).lower() == fixture.wallet.lower()
        assert str(signer).lower() == fixture.signer.lower()
        return SimpleNamespace(
            account=verified_account or fixture.wallet,
            signer=verified_signer or fixture.signer,
            session_active=session_active,
            session_not_expired=session_not_expired,
            perps_permission=perps_permission,
            move_fund_permission=True,
            deployment_identity_verified=deployment_identity_verified,
        )

    class MasterAdapter:
        def __init__(self, _limiter, network=None):
            self.network = network

        async def account_snapshot(self, address, *, priority=None):
            assert address == MASTER, "RED: RISEx activation must never read a follower through Hyperliquid"
            rows = [
                {
                    "position": {
                        "coin": asset,
                        "szi": str(size),
                        "leverage": {"type": "cross", "value": "1"},
                    }
                }
                for asset, size in sorted(master_positions.items())
                if size != 0
            ]
            return SimpleNamespace(
                perp_state={"assetPositions": rows},
                account_value=Decimal("100"),
            )

        async def mids(self):
            return {asset: str(mark) for asset, mark in marks.items()}

    monkeypatch.setattr(activation, "live_trading_allowed", live_allowed)
    monkeypatch.setattr(activation, "master_snapshot_started_order", causal_order)
    monkeypatch.setattr(activation, "entitlement", entitled)
    monkeypatch.setattr(activation, "repair_stream", no_repair)
    monkeypatch.setattr(activation, "HyperliquidAdapter", MasterAdapter)
    monkeypatch.setattr(activation, "_limiter", lambda: None)
    monkeypatch.setattr(activation, "assert_risex_environment_allowed", env_gate, raising=False)
    monkeypatch.setattr(activation, "read_risex_reconcile_observation", risex_reader, raising=False)
    monkeypatch.setattr(activation, "_verify_risex_signer_binding", verify_binding, raising=False)
    monkeypatch.setattr(activation, "verify_risex_activation_session", verify_binding, raising=False)


async def _run_hl(
    monkeypatch: pytest.MonkeyPatch,
    fixture: SimpleNamespace,
    *,
    positions: dict[str, Decimal],
    master_positions: dict[str, Decimal],
    max_total_exposure: Decimal | None = None,
):
    _install_common_reconcile_stubs(monkeypatch)
    marks = {asset: Decimal("100") for asset in set(positions) | set(master_positions)}
    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        return await reconcile.reconcile_user(
            db,
            _FakeHL(positions=positions, marks=marks),
            user,
            master_positions=master_positions,
            master_equity=Decimal("100"),
            mids={asset: str(px) for asset, px in marks.items()},
            master_mids={asset: str(px) for asset, px in marks.items()},
        )


async def _run_risex_common(
    monkeypatch: pytest.MonkeyPatch,
    fixture: SimpleNamespace,
    *,
    positions: dict[str, Decimal],
    master_positions: dict[str, Decimal],
    unmanaged_margin: Decimal | None = Decimal("0"),
):
    _install_common_reconcile_stubs(monkeypatch)
    marks = {asset: Decimal("100") for asset in set(positions) | set(master_positions)}
    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        planner = _require_symbol(reconcile, "reconcile_observed_follower")
        return await planner(
            db,
            user,
            observation=_observation(
                fixture,
                positions=positions,
                marks=marks,
                unmanaged_margin=unmanaged_margin,
            ),
            master_positions=master_positions,
            master_equity=Decimal("100"),
            master_mids={asset: str(px) for asset, px in marks.items()},
            master_configs=None,
            create_jobs=True,
        )


async def _semantic_state(user_id: uuid.UUID) -> dict[str, dict]:
    async with SessionLocal() as db:
        ledgers = (
            await db.execute(
                select(PositionLedger).where(PositionLedger.user_id == user_id)
            )
        ).scalars().all()
        jobs = (
            await db.execute(
                select(CopyJob)
                .where(CopyJob.user_id == user_id, CopyJob.origin == "RECONCILE")
                .order_by(CopyJob.asset)
            )
        ).scalars().all()
    ledger_by_asset = {row.asset: row for row in ledgers}
    out: dict[str, dict] = {}
    for job in jobs:
        ledger = ledger_by_asset[job.asset]
        real = Decimal(str(job.context.get("real_position", ledger.size)))
        target = Decimal(str(ledger.target_size))
        reversal = real != 0 and target != 0 and real * target < 0
        if target == 0 and real != 0:
            intent = "CLOSE"
        elif reversal:
            intent = "REVERSE"
        elif real == 0 and target != 0:
            intent = "OPEN"
        elif abs(target) < abs(real):
            intent = "REDUCE"
        else:
            intent = "OPEN"
        out[job.asset] = {
            "target": target,
            "intent": intent,
            "delta": target - real,
            "reduce_only": intent in {"CLOSE", "REDUCE"},
            "reversal": reversal,
            "submitted_size": job.context.get("reconcile_risk_submitted_size"),
            "reserved_additional_exposure": job.context.get("reconcile_reserved_additional_exposure"),
            "reserved_total_exposure": job.context.get("reconcile_reserved_total_exposure"),
            "reserved_open_positions": job.context.get("reconcile_reserved_open_positions"),
        }
    return out


@pytest.mark.asyncio
async def test_risex_resume_nonflat_master_creates_alignment_reconcile_jobs_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_user(provider="risex", copy_state=CopyState.SHADOW)
    _install_common_reconcile_stubs(monkeypatch)
    _install_signed_write_tripwires(monkeypatch)
    provider_reads: list[str] = []
    _install_activation_stubs(
        monkeypatch,
        fixture=fixture,
        master_positions={"BTC": Decimal("0.25")},
        follower_positions={},
        provider_reads=provider_reads,
    )

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        response = await activation.resume_copy_immediate(user=user, db=db)

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        jobs = (
            await db.execute(select(CopyJob).where(CopyJob.user_id == fixture.user_id))
        ).scalars().all()
        ta = (
            await db.execute(select(TradingAccount).where(TradingAccount.user_id == fixture.user_id))
        ).scalar_one_or_none()

    assert ta is None
    assert user is not None and user.copy_state == CopyState.ACTIVE
    assert response["copy_state"] == CopyState.ACTIVE.value
    assert len(jobs) == 1 and jobs[0].origin == "RECONCILE"
    assert jobs[0].execution_epoch_id == fixture.epoch_id
    assert jobs[0].execution_provider == "risex"
    assert jobs[0].execution_network == "testnet"
    assert jobs[0].context["master_position"] == "0.25"
    assert "place_ioc" not in repr(provider_reads)


@pytest.mark.asyncio
async def test_initial_alignment_risex_matches_hyperliquid_target_orders_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = {"BTC": Decimal("0.10"), "ETH": Decimal("0.20")}
    master = {"BTC": Decimal("0.25"), "ETH": Decimal("0.05")}
    hl = await _seed_user(provider="hyperliquid", copy_state=CopyState.ACTIVE, positions=positions)
    rx = await _seed_user(provider="risex", copy_state=CopyState.ACTIVE, positions=positions)

    await _run_hl(monkeypatch, hl, positions=positions, master_positions=master)
    await _run_risex_common(monkeypatch, rx, positions=positions, master_positions=master)

    assert await _semantic_state(rx.user_id) == await _semantic_state(hl.user_id)


@pytest.mark.asyncio
async def test_risex_initial_alignment_closes_position_absent_from_master_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_user(
        provider="risex",
        copy_state=CopyState.ACTIVE,
        positions={"BTC": Decimal("0.50")},
        managed=True,
    )
    await _run_risex_common(
        monkeypatch,
        fixture,
        positions={"BTC": Decimal("0.50")},
        master_positions={"BTC": Decimal("0")},
    )
    state = await _semantic_state(fixture.user_id)
    assert state["BTC"]["target"] == 0
    assert state["BTC"]["intent"] == "CLOSE"
    assert state["BTC"]["reduce_only"] is True


@pytest.mark.asyncio
async def test_risex_initial_alignment_reversal_matches_hyperliquid_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = {"BTC": Decimal("-0.20")}
    master = {"BTC": Decimal("0.30")}
    hl = await _seed_user(provider="hyperliquid", copy_state=CopyState.ACTIVE, positions=positions)
    rx = await _seed_user(provider="risex", copy_state=CopyState.ACTIVE, positions=positions)
    await _run_hl(monkeypatch, hl, positions=positions, master_positions=master)
    await _run_risex_common(monkeypatch, rx, positions=positions, master_positions=master)
    assert await _semantic_state(rx.user_id) == await _semantic_state(hl.user_id)
    assert (await _semantic_state(rx.user_id))["BTC"]["reversal"] is True


@pytest.mark.asyncio
async def test_risex_initial_alignment_uses_same_batch_capacity_reservations_as_hyperliquid_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = {"BTC": Decimal("0.60"), "ETH": Decimal("0.60")}
    hl = await _seed_user(
        provider="hyperliquid",
        copy_state=CopyState.ACTIVE,
        max_total_exposure=Decimal("100"),
        max_positions=2,
    )
    rx = await _seed_user(
        provider="risex",
        copy_state=CopyState.ACTIVE,
        max_total_exposure=Decimal("100"),
        max_positions=2,
    )
    await _run_hl(monkeypatch, hl, positions={}, master_positions=master)
    await _run_risex_common(monkeypatch, rx, positions={}, master_positions=master)
    assert await _semantic_state(rx.user_id) == await _semantic_state(hl.user_id)


@pytest.mark.asyncio
async def test_risex_initial_alignment_allows_same_over_cap_reductions_as_hyperliquid_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = {"BTC": Decimal("1.20")}
    master = {"BTC": Decimal("0.50")}
    hl = await _seed_user(
        provider="hyperliquid",
        copy_state=CopyState.ACTIVE,
        positions=positions,
        max_total_exposure=Decimal("100"),
    )
    rx = await _seed_user(
        provider="risex",
        copy_state=CopyState.ACTIVE,
        positions=positions,
        max_total_exposure=Decimal("100"),
    )
    await _run_hl(monkeypatch, hl, positions=positions, master_positions=master)
    await _run_risex_common(monkeypatch, rx, positions=positions, master_positions=master)
    assert await _semantic_state(rx.user_id) == await _semantic_state(hl.user_id)


@pytest.mark.asyncio
async def test_risex_alignment_reconcile_job_is_bound_to_exact_active_epoch_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_user(provider="risex", copy_state=CopyState.ACTIVE)
    await _run_risex_common(
        monkeypatch,
        fixture,
        positions={},
        master_positions={"BTC": Decimal("0.25")},
    )

    async with SessionLocal() as db:
        job = (
            await db.execute(select(CopyJob).where(CopyJob.user_id == fixture.user_id))
        ).scalar_one()
        assert (job.execution_epoch_id, job.execution_provider, job.execution_network) == (
            fixture.epoch_id,
            "risex",
            "testnet",
        )
        await set_user_destination(
            db,
            fixture.user_id,
            provider="risex",
            network="testnet",
            account_address=fixture.wallet,
            credential_version=fixture.generation + 1,
        )
        await db.commit()

    class RedisMustNotPublish:
        async def xadd(self, *_args, **_kwargs):
            raise AssertionError("RED: stale RISEx epoch must fail before Redis publish")

    async with SessionLocal() as db:
        job = (
            await db.execute(select(CopyJob).where(CopyJob.user_id == fixture.user_id))
        ).scalar_one()
        await publish_job(RedisMustNotPublish(), db, job)
        assert job.state == JobState.SKIPPED
        assert job.enqueued_at is None


@pytest.mark.asyncio
async def test_risex_resume_revalidates_credential_and_session_before_alignment_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_user(provider="risex", copy_state=CopyState.SHADOW)
    _install_common_reconcile_stubs(monkeypatch)
    events: list[str] = []
    _install_activation_stubs(
        monkeypatch,
        fixture=fixture,
        master_positions={"BTC": Decimal("0.25")},
        follower_positions={},
        provider_reads=events,
    )

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        await activation.resume_copy_immediate(user=user, db=db)

    assert "gate" in events
    assert "session" in events
    assert "portfolio" in events
    assert events.index("gate") < events.index("session")
    assert events.index("gate") < events.index("portfolio")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "detail_tokens", "verifier_must_run"),
    [
        ("credential-revoked", ("credential", "revoked", "status"), False),
        ("credential-expired-at", ("credential", "expired", "expiry"), False),
        ("session-inactive", ("session", "inactive", "authorization"), True),
        ("session-expired", ("session", "expired", "authorization"), True),
        ("perps-denied", ("perps", "permission", "authorization"), True),
        ("deployment-unverified", ("deployment", "identity", "authorization"), True),
        ("account-mismatch", ("account", "binding", "mismatch", "authorization"), True),
        ("signer-mismatch", ("signer", "binding", "mismatch", "authorization"), True),
    ],
)
async def test_risex_resume_rejects_invalid_credential_or_session_before_alignment_integration(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    detail_tokens: tuple[str, ...],
    verifier_must_run: bool,
) -> None:
    status = CredentialStatus.REVOKED if scenario == "credential-revoked" else CredentialStatus.ACTIVE
    expires_at = (
        datetime.now(UTC) - timedelta(minutes=1)
        if scenario == "credential-expired-at"
        else datetime.now(UTC) + timedelta(hours=2)
    )
    fixture = await _seed_user(
        provider="risex",
        copy_state=CopyState.SHADOW,
        credential_status=status,
        expires_at=expires_at,
    )
    _install_common_reconcile_stubs(monkeypatch)
    _install_signed_write_tripwires(monkeypatch)
    reads: list[str] = []
    _install_activation_stubs(
        monkeypatch,
        fixture=fixture,
        master_positions={"BTC": Decimal("0.25")},
        follower_positions={},
        provider_reads=reads,
        session_active=scenario != "session-inactive",
        session_not_expired=scenario != "session-expired",
        perps_permission=scenario != "perps-denied",
        deployment_identity_verified=scenario != "deployment-unverified",
        verified_account=("0x" + ("22" * 20)) if scenario == "account-mismatch" else None,
        verified_signer=("0x" + ("33" * 20)) if scenario == "signer-mismatch" else None,
    )

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        with pytest.raises(HTTPException) as exc:
            await activation.resume_copy_immediate(user=user, db=db)

    assert exc.value.status_code in {409, 422, 503}
    detail = str(exc.value.detail).lower()
    assert any(token in detail for token in detail_tokens), (
        "RED: rejection must identify the invalid RISEx credential/session evidence, "
        f"not an unrelated prerequisite: {detail!r}"
    )
    if verifier_must_run:
        assert "session" in reads, (
            "RED: live RISEx authorization evidence must be read before rejecting "
            f"{scenario!r}"
        )

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        jobs = (
            await db.execute(select(CopyJob).where(CopyJob.user_id == fixture.user_id))
        ).scalars().all()
    assert user is not None and user.copy_state != CopyState.ACTIVE
    assert jobs == []


@pytest.mark.asyncio
async def test_risex_resume_rejects_credential_rotation_between_read_and_reconcile_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_user(provider="risex", copy_state=CopyState.SHADOW)
    _install_common_reconcile_stubs(monkeypatch)
    _install_activation_stubs(
        monkeypatch,
        fixture=fixture,
        master_positions={"BTC": Decimal("0.25")},
        follower_positions={},
        rotate_after_read=True,
    )

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        with pytest.raises(HTTPException) as exc:
            await activation.resume_copy_immediate(user=user, db=db)
    assert exc.value.status_code == 409
    assert any(
        token in str(exc.value.detail).lower()
        for token in ("rotation", "generation", "binding", "credential")
    ), "RED: the 409 must identify the RISEx credential/binding rotation, not an unrelated Hyperliquid prerequisite"

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        jobs = (
            await db.execute(select(CopyJob).where(CopyJob.user_id == fixture.user_id))
        ).scalars().all()
    assert user is not None and user.copy_state != CopyState.ACTIVE
    assert jobs == []


@pytest.mark.asyncio
async def test_risex_activation_planning_failure_rolls_back_to_paused_like_hyperliquid_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_user(provider="risex", copy_state=CopyState.SHADOW)
    _install_common_reconcile_stubs(monkeypatch)
    _install_activation_stubs(
        monkeypatch,
        fixture=fixture,
        master_positions={"BTC": Decimal("0.25")},
        follower_positions={},
    )
    _require_symbol(reconcile, "reconcile_observed_follower")

    async def failing_planner(db, user, **_kwargs):
        destination = await user_destination_state(db, user.id)
        db.add(
            CopyJob(
                user_id=user.id,
                execution_epoch_id=destination.epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset="BTC",
                origin="RECONCILE",
                state=JobState.QUEUED,
                correlation_id=uuid.uuid4().hex,
                context={"master_position": "0.25"},
            )
        )
        await db.flush()
        raise RuntimeError("forced STOP2-A planning failure")

    monkeypatch.setattr(activation, "reconcile_observed_follower", failing_planner, raising=False)
    monkeypatch.setattr(reconcile, "reconcile_observed_follower", failing_planner)

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        with pytest.raises(HTTPException) as exc:
            await activation.resume_copy_immediate(user=user, db=db)
    assert exc.value.status_code == 503

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        job = (
            await db.execute(select(CopyJob).where(CopyJob.user_id == fixture.user_id))
        ).scalar_one()
        actions = set(
            (
                await db.execute(
                    select(AuditLog.action).where(AuditLog.subject_id == fixture.user_id)
                )
            ).scalars().all()
        )
    assert user is not None and user.copy_state == CopyState.PAUSED
    assert job.state == JobState.SKIPPED
    assert "COPY_ACTIVATION_STARTED" in actions
    assert "COPY_ACTIVATION_ROLLED_BACK" in actions


@pytest.mark.asyncio
async def test_risex_unmanaged_position_without_verified_margin_used_fails_closed_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _seed_user(
        provider="risex",
        copy_state=CopyState.ACTIVE,
        positions={"BTC": Decimal("0.25")},
        managed=False,
    )
    _install_common_reconcile_stubs(monkeypatch)
    _require_symbol(reconcile, "reconcile_observed_follower")
    with pytest.raises(Exception, match="margin|indeterminate|marginUsed"):
        await _run_risex_common(
            monkeypatch,
            fixture,
            positions={"BTC": Decimal("0.25")},
            master_positions={"ETH": Decimal("0.25")},
            unmanaged_margin=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocker", "expected_detail"),
    [
        ("pending", "pending"),
        ("submitting", "unresolved"),
        ("unknown", "unresolved"),
    ],
)
async def test_risex_resume_rejects_unresolved_or_pending_epoch_work_before_provider_reads_integration(
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
    expected_detail: str,
) -> None:
    fixture = await _seed_user(provider="risex", copy_state=CopyState.SHADOW)
    _install_common_reconcile_stubs(monkeypatch)
    reads: list[str] = []
    _install_activation_stubs(
        monkeypatch,
        fixture=fixture,
        master_positions={"BTC": Decimal("0.25")},
        follower_positions={},
        provider_reads=reads,
    )

    async with SessionLocal() as db:
        job = CopyJob(
            user_id=fixture.user_id,
            execution_epoch_id=fixture.epoch_id,
            execution_provider="risex",
            execution_network="testnet",
            asset="BTC",
            origin="RECONCILE",
            state=JobState.QUEUED if blocker == "pending" else JobState.DONE,
            correlation_id=uuid.uuid4().hex,
            context={"master_position": "0.25"},
        )
        db.add(job)
        await db.flush()
        if blocker in {"submitting", "unknown"}:
            db.add(
                Execution(
                    copy_job_id=job.id,
                    user_id=fixture.user_id,
                    execution_epoch_id=fixture.epoch_id,
                    execution_provider="risex",
                    execution_network="testnet",
                    attempt_kind="o",
                    cloid="0x" + uuid.uuid4().hex,
                    state=(
                        ExecutionState.SUBMITTING
                        if blocker == "submitting"
                        else ExecutionState.UNKNOWN
                    ),
                    asset="BTC",
                    is_buy=True,
                    requested_size=Decimal("0.1"),
                    reduce_only=False,
                    limit_px=Decimal("100"),
                )
            )
        await db.commit()

    async with SessionLocal() as db:
        user = await db.get(User, fixture.user_id)
        assert user is not None
        with pytest.raises(HTTPException) as exc:
            await activation.resume_copy_immediate(user=user, db=db)
    assert exc.value.status_code == 409
    assert expected_detail in str(exc.value.detail).lower()
    assert reads == [], "RED: pending/ambiguous epoch work must block before any RISEx provider read"

