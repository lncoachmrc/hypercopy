"""PostgreSQL evidence for HF-007 partial-fill lifecycle convergence."""

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.adapters.hyperliquid import OrderOutcome, parse_order_response
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.engine.sizing import AssetSpec, FollowerState, MasterExposure, plan
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
from app.services.execution import _apply_fill_to_ledger, _execute_leg
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


class PartialFillHL:
    network = "testnet"

    def __init__(self):
        self.spec = AssetSpec("BTC", sz_decimals=3, max_leverage=20)
        self.position = Decimal("0")
        self.place_ioc_calls: list[dict] = []
        self._fill_sizes = [Decimal("0.400"), Decimal("0.600")]

    async def asset_spec(self, asset: str) -> AssetSpec:
        assert asset == "BTC"
        return self.spec

    async def place_ioc(self, **kwargs) -> OrderOutcome:
        self.place_ioc_calls.append(kwargs)
        filled = self._fill_sizes.pop(0)
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
                                "oid": 1000 + len(self.place_ioc_calls),
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
    first_job_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"
    agent = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        user = User(
            id=user_id,
            auth_wallet=wallet,
            state=UserState.SUSPENDED,
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
                agent_name="hf007-test",
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
                size=Decimal("0"),
                target_size=Decimal("1"),
                mark_price=Decimal("100"),
                managed=True,
            )
        )
        db.add(
            CopyJob(
                id=first_job_id,
                user_id=user_id,
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="hf007",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={
                    "master_position": "1",
                    "master_equity": "100",
                    "master_mark_price": "100",
                    "follower_network": "testnet",
                },
            )
        )
        await db.commit()

    return user_id, first_job_id


def _target_plan(current: Decimal):
    return plan(
        MasterExposure("BTC", Decimal("1"), Decimal("100"), Decimal("100")),
        FollowerState("hf007", Decimal("100"), Decimal("0"), current, Decimal("1")),
        AssetSpec("BTC", sz_decimals=3, max_leverage=20),
        min_notional=Decimal("10"),
        follower_mark_price=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_partial_ioc_fill_updates_actual_size_then_reconciles_exact_residual():
    user_id, first_job_id = await _seed_case()
    hl = PartialFillHL()

    # Requested 1.000 BTC, exchange acknowledges only 0.400 BTC as filled.
    async with SessionLocal() as db:
        job = await db.get(CopyJob, first_job_id)
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()
        primary = _target_plan(Decimal("0"))
        assert primary.order_size == Decimal("1.000")

        first = await _execute_leg(
            db,
            hl,
            job,
            user_id,
            "0x" + "1" * 40,
            "test-only-key-never-used-by-fake",
            primary,
            Decimal("100"),
            50,
            "o",
            ledger.last_execution_id,
        )
        assert first.state == "FILLED"
        assert first.filled_size == Decimal("0.400")
        await _apply_fill_to_ledger(db, ledger, job, "o", primary, first)

        await db.refresh(ledger)
        assert ledger.size == Decimal("0.400")
        execution = (
            await db.execute(select(Execution).where(Execution.copy_job_id == first_job_id))
        ).scalar_one()
        assert execution.state == ExecutionState.FILLED
        assert execution.requested_size == Decimal("1.000")
        assert execution.filled_size == Decimal("0.400")

        # Re-entering the same durable job must not submit its original CLOID again.
        duplicate = await _execute_leg(
            db,
            hl,
            job,
            user_id,
            "0x" + "1" * 40,
            "test-only-key-never-used-by-fake",
            primary,
            Decimal("100"),
            50,
            "o",
            ledger.last_execution_id,
        )
        assert duplicate.filled_size == Decimal("0.400")
        assert len(hl.place_ioc_calls) == 1

    # The authoritative exchange snapshot is still 0.400 BTC; reconciliation
    # must create one and only one residual job toward the 1.000 BTC target.
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
        residual_job = (
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
        assert ledger.size == Decimal("0.400")
        assert ledger.target_size == Decimal("1")

        residual = _target_plan(ledger.size)
        assert residual.order_size == Decimal("0.600")
        second = await _execute_leg(
            db,
            hl,
            residual_job,
            user_id,
            "0x" + "1" * 40,
            "test-only-key-never-used-by-fake",
            residual,
            Decimal("100"),
            50,
            "o",
            ledger.last_execution_id,
        )
        assert second.state == "FILLED"
        assert second.filled_size == Decimal("0.600")
        await _apply_fill_to_ledger(db, ledger, residual_job, "o", residual, second)
        await db.refresh(ledger)
        assert ledger.size == Decimal("1.000")
        residual_job.state = JobState.DONE
        await db.commit()

    # With the real exchange position now at target, a further reconciliation
    # must create no additional order intent: the lifecycle has converged.
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
        assert len(hl.place_ioc_calls) == 2
        executions = (
            await db.execute(
                select(Execution).where(Execution.user_id == user_id).order_by(Execution.created_at)
            )
        ).scalars().all()
        assert [row.requested_size for row in executions] == [Decimal("1.000"), Decimal("0.600")]
        assert [row.filled_size for row in executions] == [Decimal("0.400"), Decimal("0.600")]
