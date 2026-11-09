"""PostgreSQL integration contracts for Profit Exit SHADOW measurement.

Uses pg_temp clones, savepoints and rollback. No Railway or Hyperliquid calls.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import engine
from app.services.ai_profit_exit_retention import read_retention_context
from app.services.ai_profit_exit_telemetry import record_shadow_observation


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires ephemeral PostgreSQL and Alembic schema in CI",
)


@pytest_asyncio.fixture
async def isolated_tables():
    async with engine.connect() as conn:
        outer = await conn.begin()
        try:
            await conn.execute(text(
                "CREATE TEMP TABLE ai_profit_exit_observations "
                "(LIKE public.ai_profit_exit_observations INCLUDING ALL) ON COMMIT DROP"
            ))
            await conn.execute(text(
                "CREATE TEMP TABLE ai_profit_exit_decisions "
                "(LIKE public.ai_profit_exit_decisions INCLUDING ALL) ON COMMIT DROP"
            ))
            factory = async_sessionmaker(
                conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            yield factory
        finally:
            await outer.rollback()
    await engine.dispose()


def _shadow_observation(net_pnl):
    amount = Decimal(net_pnl) if net_pnl is not None else None
    return SimpleNamespace(
        current_position=Decimal("-1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("96"),
        executable_exit_price=Decimal("96.2"),
        economics=SimpleNamespace(net_pnl=amount) if amount is not None else None,
        complete=amount is not None,
        eligible=amount is not None and amount > 0,
        basis="copy_shadow_estimate",
        pnl_complete=False,
        funding_model="not_modeled",
        reason="" if amount is not None else "not available",
    )


def _keys(slot):
    return dict(
        user_id=uuid.UUID(int=1),
        execution_epoch_id=uuid.UUID(int=2),
        execution_provider="hyperliquid",
        execution_network="testnet",
        asset="BTC",
        source_cycle_id="testnet:BTC:verified-cycle",
        state_version=200,
        evaluation_slot=slot,
        position_matches=True,
    )


@pytest.mark.asyncio
async def test_shadow_observations_persist_losses_and_nulls_idempotently(isolated_tables):
    async with isolated_tables() as db:
        await record_shadow_observation(
            db, **_keys(10), observation=_shadow_observation("-2.5")
        )
        # Same semantic slot with a different mark must retain the FIRST
        # sample, not overwrite its historical evidence.
        await record_shadow_observation(
            db, **_keys(10), observation=_shadow_observation("3.5")
        )
        await record_shadow_observation(
            db, **_keys(11), observation=_shadow_observation("0")
        )
        await record_shadow_observation(
            db, **_keys(12), observation=_shadow_observation(None)
        )
        rows = (await db.execute(text(
            "SELECT evaluation_slot, net_pnl, eligible, complete "
            "FROM ai_profit_exit_observations ORDER BY evaluation_slot"
        ))).all()
        assert len(rows) == 3
        assert rows[0].evaluation_slot == 10
        assert rows[0].net_pnl == Decimal("-2.5")
        assert rows[0].eligible is False
        assert rows[1].net_pnl == 0
        assert rows[1].eligible is False
        assert rows[2].net_pnl is None
        assert rows[2].complete is False


@pytest.mark.asyncio
async def test_prior_profit_peak_does_not_mix_other_cycle_or_state(isolated_tables):
    now = datetime.now(UTC)
    user = uuid.UUID(int=1)
    epoch = uuid.UUID(int=2)
    async with isolated_tables() as db:
        records = [
            ("testnet:BTC:verified-cycle", 200, "10", now-timedelta(minutes=2)),
            ("testnet:BTC:verified-cycle", 200, "7", now-timedelta(minutes=1)),
            ("testnet:BTC:other-cycle", 200, "500", now-timedelta(seconds=30)),
            ("testnet:BTC:verified-cycle", 201, "600", now-timedelta(seconds=15)),
        ]
        for cycle, version, pnl, stamped in records:
            await db.execute(text(
                "INSERT INTO ai_profit_exit_decisions ("
                "id, user_id, execution_epoch_id, execution_provider, "
                "execution_network, asset, side, source_cycle_id, "
                "source_cycle_open_event_id, source_master_position, "
                "follower_position_size, state_version, action, net_pnl, "
                "pnl_complete, position_verified_at, decision_inputs, "
                "decision_reason, model_provider, model_name, decided_at, expires_at"
                ") VALUES ("
                ":id,:user_id,:epoch,'hyperliquid','testnet','BTC','SHORT',"
                ":cycle,:open_event,-1,-1,:version,'HOLD',:pnl,false,"
                ":stamped,CAST('{}' AS JSON),'fixture','test','test',"
                ":stamped,:expires"
                ")"
            ), {
                "id": uuid.uuid4(), "user_id": user, "epoch": epoch,
                "cycle": cycle, "open_event": uuid.uuid4(),
                "version": version, "pnl": Decimal(pnl),
                "stamped": stamped, "expires": now+timedelta(hours=1),
            })
        await db.commit()
        result = await read_retention_context(
            db,
            user_id=user,
            execution_epoch_id=epoch,
            execution_provider="hyperliquid",
            execution_network="testnet",
            asset="BTC",
            source_cycle_id="testnet:BTC:verified-cycle",
            state_version=200,
            follower_position_size=Decimal("-1"),
            pnl_complete=False,
            current_net_pnl=Decimal("6"),
        )
        assert result["sample_count"] == 3
        assert result["observed_peak_net_pnl_usdc"] == "10"
        assert result["observed_giveback_usdc"] == "4"
        assert result["change_since_previous_usdc"] == "-1"
        assert result["peak_is_intraperiod_mfe"] is False
