"""Real PostgreSQL LATERAL contract for adaptive SHADOW learning.

Uses temporary tables and rollback; no RISEx API or trading connections.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import engine
from app.services.ai_profit_exit_learning import read_shadow_learning_feedback


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture
async def isolated():
    async with engine.connect() as connection:
        tx = await connection.begin()
        try:
            await connection.execute(text(
                "CREATE TEMP TABLE ai_profit_exit_decisions "
                "(LIKE public.ai_profit_exit_decisions INCLUDING ALL) ON COMMIT DROP"
            ))
            await connection.execute(text(
                "CREATE TEMP TABLE ai_profit_exit_observations "
                "(LIKE public.ai_profit_exit_observations INCLUDING ALL) ON COMMIT DROP"
            ))
            factory = async_sessionmaker(
                connection, join_transaction_mode="create_savepoint",
                expire_on_commit=False,
            )
            yield factory
        finally:
            await tx.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_learning_query_pairs_only_future_matching_epoch_cycle_and_position(isolated):
    now = datetime.now(UTC)
    time = now - timedelta(minutes=30)
    uid = uuid.uuid4()
    epoch = uuid.uuid4()
    cycle = "hyperliquid-master:BTC:cycle-123"
    async with isolated() as db:
        await db.execute(text("""
            INSERT INTO ai_profit_exit_decisions
            (id,user_id,execution_epoch_id,execution_provider,execution_network,
             asset,side,source_cycle_id,source_cycle_open_event_id,source_master_position,
             follower_position_size,state_version,action,net_pnl,pnl_complete,
             position_verified_at,decision_inputs,decision_reason,model_provider,
             model_name,decided_at,expires_at)
            VALUES (:id,:uid,:epoch,'risex','testnet','BTC','SHORT',:cycle,
              :opening,-1,-1,42,'CLOSE_PROFIT',10,false,:time,
              CAST(:inputs AS JSON),'fixture','test','test',:time,:expiry)
        """), {
            "id": uuid.uuid4(), "uid": uid, "epoch": epoch,
            "cycle": cycle, "opening": uuid.uuid4(),
            "time": time, "expiry": now + timedelta(hours=1),
            "inputs": json.dumps({
                "copy_state":"SHADOW","evaluation_basis":"risex_shadow_estimate",
                "entry_price":"100",
            }),
        })
        for observed_net, observed_epoch, observed_version in [
            ("-90", uuid.uuid4(), 42),  # wrong destination, must not pair
            ("-80", epoch, 43),         # wrong master state, must not pair
            ("9", epoch, 42),           # valid next sample
        ]:
            await db.execute(text("""
                INSERT INTO ai_profit_exit_observations
                  (id,user_id,execution_epoch_id,execution_provider,execution_network,
                   asset,source_cycle_id,state_version,evaluation_slot,
                   follower_position_size,evaluation_basis,pnl_complete,funding_model,
                   net_pnl,complete,eligible,position_matches,reason,observed_at)
                VALUES
                  (:id,:uid,:epoch,'risex','testnet','BTC',:cycle,:version,40,
                   -1,'risex_shadow_estimate',false,'not_modeled',
                   :pnl,true,true,true,'',:observed_at)
            """), {
                "id": uuid.uuid4(), "uid": uid, "epoch": observed_epoch,
                "cycle": cycle, "version": observed_version,
                "pnl": Decimal(observed_net),
                "observed_at": time + timedelta(minutes=5),
            })
        await db.commit()
        result = await read_shadow_learning_feedback(
            db, provider="risex", network="testnet", asset="BTC",
            current_source_cycle_id="another-cycle", now=now,
        )
        assert result["status"] == "collecting"
        assert result["independent_source_cycles"] == 1
        not_current = await read_shadow_learning_feedback(
            db, provider="risex", network="testnet", asset="BTC",
            current_source_cycle_id=cycle, now=now,
        )
        assert not_current["independent_source_cycles"] == 0
        assert result["policy_promotion"] == "disabled"
