"""Regression coverage for reconciliation batch risk reservations (#133)."""

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


class BatchReconcileHL:
    network = "testnet"

    def __init__(
        self,
        *,
        positions: dict[str, Decimal] | None = None,
        account_value: Decimal = Decimal("100"),
        free_margin: Decimal = Decimal("1000"),
    ):
        self.positions = dict(positions or {})
        self.account_value = account_value
        self.free_margin = free_margin

    async def account_snapshot(self, account: str):
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
                        "leverage": {"type": "cross", "value": "1"},
                    }
                }
            )
        return SimpleNamespace(
            perp_state={"assetPositions": rows},
            account_value=self.account_value,
            free_margin=self.free_margin,
            collateral_balance=self.account_value,
            unrealized_pnl=Decimal("0"),
            abstraction="default",
        )

    async def user_fills_by_time(self, account: str, start_ms: int):
        return []

    async def asset_spec(self, asset: str):
        return AssetSpec(asset, sz_decimals=3, max_leverage=50)


async def _seed_user(
    *,
    positions: dict[str, Decimal] | None = None,
    allow_assets: list[str],
    max_positions: int = 10,
    max_total_exposure: Decimal = Decimal("1000"),
    max_asset_exposure: Decimal = Decimal("1000"),
    max_notional_per_trade: Decimal = Decimal("1000"),
    max_leverage: Decimal = Decimal("10"),
) -> uuid.UUID:
    user_id = uuid.uuid4()
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
                agent_name="reconcile-batch-reservation",
            )
        )
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
                allow_assets=allow_assets,
                block_assets=[],
            )
        )
        for asset, size in sorted((positions or {}).items()):
            db.add(
                PositionLedger(
                    user_id=user_id,
                    asset=asset,
                    size=size,
                    target_size=size,
                    mark_price=Decimal("100"),
                    managed=True,
                )
            )
        await db.commit()

    return user_id


async def _run_reconcile(
    user_id: uuid.UUID,
    hl: BatchReconcileHL,
    *,
    master_positions: dict[str, Decimal],
    create_jobs: bool = True,
):
    mids = {asset: "100" for asset in set(master_positions) | set(hl.positions)}
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        return await reconcile_user(
            db,
            hl,
            user,
            master_positions=master_positions,
            master_equity=Decimal("100"),
            mids=mids,
            create_jobs=create_jobs,
        )


async def _queued_jobs(user_id: uuid.UUID) -> list[CopyJob]:
    async with SessionLocal() as db:
        return (
            await db.execute(
                select(CopyJob)
                .where(
                    CopyJob.user_id == user_id,
                    CopyJob.origin == "RECONCILE",
                )
                .order_by(CopyJob.asset)
            )
        ).scalars().all()


def _submitted(job: CopyJob) -> Decimal | None:
    raw = (job.context or {}).get("reconcile_risk_submitted_size")
    return None if raw in (None, "") else Decimal(str(raw))


@pytest.mark.asyncio
async def test_batch_reserves_open_position_capacity():
    user_id = await _seed_user(
        allow_assets=["BTC", "ETH"],
        max_positions=1,
    )
    result = await _run_reconcile(
        user_id,
        BatchReconcileHL(),
        master_positions={"BTC": Decimal("0.2"), "ETH": Decimal("0.2")},
    )

    jobs = await _queued_jobs(user_id)
    assert result["jobs_created"] == 1
    assert len(jobs) == 1
    assert _submitted(jobs[0]) == Decimal("0.200")


@pytest.mark.asyncio
async def test_batch_trims_later_orders_to_remaining_total_exposure():
    user_id = await _seed_user(
        allow_assets=["BTC", "ETH"],
        max_total_exposure=Decimal("100"),
    )
    result = await _run_reconcile(
        user_id,
        BatchReconcileHL(),
        master_positions={"BTC": Decimal("0.6"), "ETH": Decimal("0.6")},
    )

    jobs = await _queued_jobs(user_id)
    assert result["jobs_created"] == 2
    assert {job.asset: _submitted(job) for job in jobs} == {
        "BTC": Decimal("0.600"),
        "ETH": Decimal("0.400"),
    }
    assert Decimal(str(result.get("reserved_total_exposure"))) == Decimal("100")


@pytest.mark.asyncio
async def test_existing_exposure_plus_batch_reservations_never_exceeds_cap():
    positions = {"BTC": Decimal("0.5")}
    user_id = await _seed_user(
        positions=positions,
        allow_assets=["BTC", "ETH", "SOL"],
        max_total_exposure=Decimal("100"),
    )
    result = await _run_reconcile(
        user_id,
        BatchReconcileHL(positions=positions),
        master_positions={
            "BTC": Decimal("0.5"),
            "ETH": Decimal("0.6"),
            "SOL": Decimal("0.6"),
        },
    )

    jobs = await _queued_jobs(user_id)
    assert result["jobs_created"] == 1
    assert [(job.asset, _submitted(job)) for job in jobs] == [
        ("ETH", Decimal("0.500")),
    ]
    assert Decimal(str(result.get("reserved_total_exposure"))) == Decimal("100")


@pytest.mark.asyncio
async def test_reduction_does_not_release_capacity_for_later_open_in_same_batch():
    positions = {"BTC": Decimal("0.8")}
    user_id = await _seed_user(
        positions=positions,
        allow_assets=["BTC", "ETH"],
        max_total_exposure=Decimal("100"),
    )
    result = await _run_reconcile(
        user_id,
        BatchReconcileHL(positions=positions),
        master_positions={"BTC": Decimal("0.2"), "ETH": Decimal("0.3")},
    )

    jobs = await _queued_jobs(user_id)
    assert result["jobs_created"] == 2
    assert {job.asset: _submitted(job) for job in jobs} == {
        "BTC": Decimal("0.600"),
        "ETH": Decimal("0.200"),
    }
    assert Decimal(str(result.get("reserved_total_exposure"))) == Decimal("100")


@pytest.mark.asyncio
async def test_reductions_remain_allowed_when_current_book_is_over_cap():
    positions = {"BTC": Decimal("1.2")}
    user_id = await _seed_user(
        positions=positions,
        allow_assets=["BTC"],
        max_total_exposure=Decimal("100"),
    )
    result = await _run_reconcile(
        user_id,
        BatchReconcileHL(positions=positions),
        master_positions={"BTC": Decimal("0.5")},
    )

    jobs = await _queued_jobs(user_id)
    assert result["jobs_created"] == 1
    assert len(jobs) == 1
    assert _submitted(jobs[0]) == Decimal("0.700")
    assert Decimal(str(result.get("reserved_total_exposure"))) == Decimal("120")


@pytest.mark.asyncio
async def test_preview_uses_same_batch_reservations_without_creating_jobs():
    user_id = await _seed_user(
        allow_assets=["BTC", "ETH"],
        max_total_exposure=Decimal("100"),
    )
    result = await _run_reconcile(
        user_id,
        BatchReconcileHL(),
        master_positions={"BTC": Decimal("0.6"), "ETH": Decimal("0.6")},
        create_jobs=False,
    )

    jobs = await _queued_jobs(user_id)
    planned = result.get("planned_jobs") or []
    assert result["jobs_created"] == 0
    assert result.get("jobs_planned") == 2
    assert jobs == []
    assert {
        row["asset"]: Decimal(str(row["submitted_size"]))
        for row in planned
    } == {
        "BTC": Decimal("0.600"),
        "ETH": Decimal("0.400"),
    }
    assert Decimal(str(result.get("reserved_total_exposure"))) == Decimal("100")
