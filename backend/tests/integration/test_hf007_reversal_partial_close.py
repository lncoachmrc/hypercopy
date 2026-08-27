"""PostgreSQL regression for abandoning a reversal open after a partial close."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.adapters.hyperliquid import parse_order_response
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.engine.sizing import AssetSpec
from app.models.entities import (
    AuditLog,
    CopyJob,
    CopyState,
    CredentialStatus,
    EquitySnapshot,
    Execution,
    JobState,
    PositionLedger,
    RiskProfile,
    SigningCredential,
    TradingAccount,
    User,
    UserState,
)
from app.services import execution
from app.services.execution import process_job
from app.services.reconcile import reconcile_user

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


class ReversalPartialCloseHL:
    network = "testnet"

    def __init__(self):
        self.spec = AssetSpec("BTC", sz_decimals=3, max_leverage=20)
        self.position = Decimal("1")
        self.place_ioc_calls: list[dict] = []

    async def mids(self):
        return {"BTC": "100"}

    async def asset_spec(self, asset: str) -> AssetSpec:
        assert asset == "BTC"
        return self.spec

    async def place_ioc(self, **kwargs):
        self.place_ioc_calls.append(kwargs)
        filled = Decimal("0.400") if len(self.place_ioc_calls) == 1 else Decimal(str(kwargs["size"]))
        self.position += filled if kwargs["is_buy"] else -filled
        response = {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {
                            "filled": {
                                "totalSz": str(filled),
                                "avgPx": "100",
                                "oid": 7000 + len(self.place_ioc_calls),
                            }
                        }
                    ]
                },
            },
        }
        return parse_order_response(response)

    async def account_snapshot(self, account: str):
        return SimpleNamespace(
            perp_state={
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": str(self.position),
                            "marginUsed": "10",
                            "leverage": {"type": "cross", "value": "1"},
                        }
                    }
                ]
            },
            account_value=Decimal("100"),
            free_margin=Decimal("90"),
            collateral_balance=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            abstraction="default",
        )

    async def user_fills_by_time(self, account: str, start_ms: int):
        return []


async def _seed_case():
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    correlation_id = uuid.uuid4().hex
    wallet = "0x" + uuid.uuid4().hex + "00000000"
    agent = "0x" + uuid.uuid4().hex + "00000000"
    now = datetime.now(UTC)

    async with SessionLocal() as db:
        user = User(
            id=user_id,
            auth_wallet=wallet,
            state=UserState.ACTIVE,
            copy_state=CopyState.ACTIVE,
        )
        db.add(user)
        await db.flush()
        await db.execute(
            text(
                "UPDATE users SET execution_network = 'testnet', "
                "network_started_at = :started_at WHERE id = :user_id"
            ),
            {"started_at": now - timedelta(minutes=1), "user_id": user_id},
        )
        account = TradingAccount(
            user_id=user_id,
            account_address=wallet,
            agent_address=agent,
            agent_name="hf007-reversal-partial",
        )
        db.add(account)
        await db.flush()
        db.add(
            SigningCredential(
                trading_account_id=account.id,
                ciphertext_b64="test-only-ciphertext",
                nonce_b64="test-only-nonce",
                wrapped_dek_b64="test-only-wrapped-dek",
                wrap_nonce_b64="test-only-wrap-nonce",
                key_provider="env",
                key_reference="hf007-test",
                key_version=1,
                agent_fingerprint="hf007-test-fingerprint",
                status=CredentialStatus.ACTIVE,
            )
        )
        db.add(
            RiskProfile(
                user_id=user_id,
                multiplier=Decimal("1"),
                max_notional_per_trade=Decimal("1000"),
                max_total_exposure=Decimal("5000"),
                max_asset_exposure=Decimal("2500"),
                max_leverage=Decimal("3"),
                max_positions=5,
                min_notional=Decimal("10"),
                max_slippage_bps=50,
                allow_assets=["BTC"],
                block_assets=[],
            )
        )
        db.add(
            EquitySnapshot(
                user_id=user_id,
                taken_at=now,
                account_value=Decimal("100"),
                free_margin=Decimal("90"),
                unmanaged_margin=Decimal("0"),
                collateral_balance=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                account_mode="default",
            )
        )
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=Decimal("1"),
                target_size=Decimal("-1"),
                mark_price=Decimal("100"),
                managed=True,
            )
        )
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="hf007-reversal-partial",
                attempt_count=1,
                correlation_id=correlation_id,
                context={
                    "master_position": "-1",
                    "master_equity": "100",
                    "master_mark_price": "100",
                    "follower_network": "testnet",
                },
            )
        )
        await db.commit()

    return user_id, job_id, correlation_id


@pytest.mark.asyncio
async def test_process_job_abandons_reversal_open_after_partial_close(monkeypatch):
    user_id, job_id, correlation_id = await _seed_case()
    hl = ReversalPartialCloseHL()

    async def _entitled(_db, _user):
        return {"entitled": True}

    monkeypatch.setattr(execution, "entitlement", _entitled)
    monkeypatch.setattr(
        execution,
        "crypto",
        SimpleNamespace(decrypt=lambda *_args, **_kwargs: "test-only-key"),
    )

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        result = await process_job(db, hl, job)

    assert result == JobState.SKIPPED.value
    assert len(hl.place_ioc_calls) == 1
    assert hl.place_ioc_calls[0]["reduce_only"] is True
    assert hl.place_ioc_calls[0]["size"] == Decimal("1.000")
    assert hl.position == Decimal("0.600")

    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()
        executions = (
            await db.execute(select(Execution).where(Execution.copy_job_id == job_id))
        ).scalars().all()
        abandonment = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.subject_id == user_id,
                    AuditLog.action == "REVERSAL_OPEN_LEG_ABANDONED",
                )
            )
        ).scalar_one()

        assert job.state == JobState.SKIPPED
        assert "open leg abandoned" in (job.last_error or "")
        assert ledger.size == Decimal("0.600")
        assert len(executions) == 1
        assert executions[0].attempt_kind == "c"
        assert executions[0].reduce_only is True
        assert executions[0].requested_size == Decimal("1.000")
        assert executions[0].filled_size == Decimal("0.400")
        assert abandonment.correlation_id == correlation_id
        assert abandonment.after["asset"] == "BTC"
        assert abandonment.after["requested_close_size"] == "1.000"
        assert abandonment.after["filled_close_size"] == "0.400"
        assert Decimal(abandonment.after["ledger_residual"]) == Decimal("0.600")
        assert Decimal(abandonment.after["lot_size"]) == Decimal("0.001")
        assert abandonment.after["sz_decimals"] == 3
        assert abandonment.after["correlation_id"] == correlation_id

    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        reconciliation = await reconcile_user(
            db,
            hl,
            user,
            master_positions={"BTC": Decimal("-1")},
            master_equity=Decimal("100"),
            mids={"BTC": "100"},
            create_jobs=True,
        )
        assert reconciliation["jobs_created"] == 1
        fresh_job = (
            await db.execute(
                select(CopyJob).where(
                    CopyJob.user_id == user_id,
                    CopyJob.asset == "BTC",
                    CopyJob.origin == "RECONCILE",
                    CopyJob.state == JobState.QUEUED,
                )
            )
        ).scalar_one()
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()

        assert fresh_job.id != job_id
        assert ledger.size == Decimal("0.600")
        assert ledger.target_size == Decimal("-1")
        assert len(hl.place_ioc_calls) == 1
