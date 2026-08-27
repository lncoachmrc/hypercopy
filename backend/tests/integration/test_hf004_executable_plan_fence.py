"""PostgreSQL regression for HF-004 executable-plan rejection fencing."""

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.engine.sizing import AssetSpec
from app.models.entities import (
    CopyJob,
    CopyState,
    Execution,
    ExecutionState,
    JobState,
    PositionLedger,
    RiskProfile,
    TradingAccount,
    User,
    UserState,
)
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


class ReconcileHL:
    network = "testnet"

    async def account_snapshot(self, account: str):
        return SimpleNamespace(
            perp_state={
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "0.400",
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

    async def asset_spec(self, asset: str):
        return AssetSpec(asset, sz_decimals=3, max_leverage=50)


async def _seed_trimmed_terminal_rejection() -> uuid.UUID:
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"
    agent = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        db.add(
            User(
                id=user_id,
                auth_wallet=wallet,
                state=UserState.ACTIVE,
                copy_state=CopyState.ACTIVE,
            )
        )
        await db.flush()
        await db.execute(
            text(
                "UPDATE users SET execution_network = 'testnet', "
                "network_started_at = now() - interval '1 minute' WHERE id = :user_id"
            ),
            {"user_id": user_id},
        )
        db.add(
            TradingAccount(
                user_id=user_id,
                account_address=wallet,
                agent_address=agent,
                agent_name="hf004-executable-plan",
            )
        )
        db.add(
            RiskProfile(
                user_id=user_id,
                multiplier=Decimal("1"),
                min_notional=Decimal("10"),
                max_notional_per_trade=Decimal("5"),
                allow_assets=["BTC"],
            )
        )
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=Decimal("0.400"),
                target_size=Decimal("1.000"),
                mark_price=Decimal("100"),
                managed=True,
            )
        )
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                asset="BTC",
                origin="RECONCILE",
                state=JobState.SKIPPED,
                correlation_id=uuid.uuid4().hex,
                context={
                    "master_position": "1",
                    "master_equity": "100",
                    "master_mark_price": "100",
                    "follower_network": "testnet",
                    "last_action_error": {
                        "class": "TERMINAL",
                        "retry_policy": "NONE",
                        "reason": "Order must have minimum value of $10.",
                        "asset": "BTC",
                        "network": "testnet",
                        "leg": "primary",
                        "rejected_target": "1.000",
                        "rejected_real": "0.400",
                    },
                },
                last_error="Order must have minimum value of $10.",
            )
        )
        await db.flush()
        db.add(
            Execution(
                copy_job_id=job_id,
                user_id=user_id,
                cloid="0x" + uuid.uuid4().hex,
                state=ExecutionState.REJECTED,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.050"),
                reduce_only=False,
                limit_px=Decimal("100"),
                reject_reason="Order must have minimum value of $10.",
            )
        )
        await db.commit()

    return user_id


@pytest.mark.asyncio
async def test_terminal_rejection_releases_when_risk_changes_submitted_size():
    user_id = await _seed_trimmed_terminal_rejection()
    hl = ReconcileHL()

    # The same $5 risk-trimmed size is still the current executable plan, so the
    # deterministic rejection remains fenced despite unchanged target and real.
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        result = await reconcile_user(
            db,
            hl,
            user,
            master_positions={"BTC": Decimal("1")},
            master_equity=Decimal("100"),
            mids={"BTC": "100"},
            create_jobs=True,
        )
        assert result["jobs_created"] == 0

    # Freeing risk headroom changes the actual submitted size from 0.050 BTC to
    # 0.600 BTC while desired target and exchange position remain unchanged.
    async with SessionLocal() as db:
        risk = (
            await db.execute(select(RiskProfile).where(RiskProfile.user_id == user_id))
        ).scalar_one()
        risk.max_notional_per_trade = Decimal("100")
        await db.commit()

    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        result = await reconcile_user(
            db,
            hl,
            user,
            master_positions={"BTC": Decimal("1")},
            master_equity=Decimal("100"),
            mids={"BTC": "100"},
            create_jobs=True,
        )
        assert result["jobs_created"] == 1
        queued = (
            await db.execute(
                select(CopyJob).where(
                    CopyJob.user_id == user_id,
                    CopyJob.asset == "BTC",
                    CopyJob.origin == "RECONCILE",
                    CopyJob.state == JobState.QUEUED,
                )
            )
        ).scalars().all()
        assert len(queued) == 1
