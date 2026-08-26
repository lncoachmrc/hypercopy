"""PostgreSQL regression for HF-004 terminal rejection retry policy."""

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    CopyState,
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

    def __init__(self):
        self.position = Decimal("0.400")

    async def account_snapshot(self, account: str):
        positions = []
        if self.position != 0:
            positions.append(
                {
                    "position": {
                        "coin": "BTC",
                        "szi": str(self.position),
                        "marginUsed": "10",
                        "leverage": {"type": "cross", "value": "1"},
                    }
                }
            )
        return SimpleNamespace(
            perp_state={"assetPositions": positions},
            account_value=Decimal("100"),
            free_margin=Decimal("90"),
            collateral_balance=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            abstraction="default",
        )

    async def user_fills_by_time(self, account: str, start_ms: int):
        return []


async def _seed_terminal_rejection():
    user_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"
    agent = "0x" + uuid.uuid4().hex + "00000000"

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
                "network_started_at = now() - interval '1 minute' WHERE id = :user_id"
            ),
            {"user_id": user_id},
        )
        db.add(
            TradingAccount(
                user_id=user_id,
                account_address=wallet,
                agent_address=agent,
                agent_name="hf004-test",
            )
        )
        db.add(
            RiskProfile(
                user_id=user_id,
                multiplier=Decimal("1"),
                min_notional=Decimal("10"),
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
                        "reason": "Price must be divisible by tick size.",
                        "asset": "BTC",
                        "network": "testnet",
                        "leg": "primary",
                    },
                },
                last_error="Price must be divisible by tick size.",
            )
        )
        await db.commit()

    return user_id


@pytest.mark.asyncio
async def test_terminal_rejection_suppresses_only_unchanged_reconciliation_intent():
    user_id = await _seed_terminal_rejection()
    hl = ReconcileHL()

    # Same target and same authoritative real position as the rejected intent:
    # a deterministic NONE policy must suppress a fresh job/CLOID.
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
        queued = (
            await db.execute(
                select(CopyJob).where(
                    CopyJob.user_id == user_id,
                    CopyJob.asset == "BTC",
                    CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
                )
            )
        ).scalars().all()
        assert queued == []

    # If exchange truth changes, the old terminal rejection no longer describes
    # the same intent. Reconciliation is allowed to create a fresh job/new CLOID.
    hl.position = Decimal("0.500")
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
