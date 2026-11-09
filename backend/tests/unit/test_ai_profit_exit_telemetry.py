"""Prospective Profit Exit telemetry: record every SHADOW valuation, including losses."""
from __future__ import annotations

import inspect
import uuid
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.models.entities import AIProfitExitObservation
from app.services import ai_profit_exit_decision as decision
from app.services.ai_profit_exit_telemetry import (
    observation_key,
    record_shadow_observation,
    shadow_observation_values,
)


def _observation(net_pnl: str | None, *, complete: bool = True):
    economics = SimpleNamespace(net_pnl=D(net_pnl)) if net_pnl is not None else None
    return SimpleNamespace(
        current_position=D("-0.5"),
        entry_price=D("100"),
        mark_price=D("96"),
        executable_exit_price=D("96.1"),
        economics=economics,
        basis="copy_shadow_estimate",
        complete=complete,
        eligible=bool(net_pnl is not None and D(net_pnl) > 0),
        pnl_complete=False,
        funding_model="not_modeled",
        reason="" if complete else "Market data incomplete",
    )


def _fields(net_pnl: str | None, *, complete: bool = True):
    return shadow_observation_values(
        user_id=uuid.UUID(int=1),
        execution_epoch_id=uuid.UUID(int=2),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:cycle",
        state_version=123,
        evaluation_slot=987,
        observation=_observation(net_pnl, complete=complete),
        position_matches=True,
    )


@pytest.mark.parametrize("value", ["-2.40", "0", "1.20"])
def test_shadow_negative_flat_and_positive_pnl_are_all_recordable(value):
    fields = _fields(value)
    assert fields["net_pnl"] == D(value)
    assert fields["pnl_complete"] is False
    assert fields["evaluation_basis"] == "copy_shadow_estimate"
    assert fields["eligible"] is (D(value) > 0)
    assert fields["follower_position_size"] == D("-0.5")
    assert fields["id"] is not None


def test_incomplete_market_data_remains_visible_without_invented_pnl():
    fields = _fields(None, complete=False)
    assert fields["complete"] is False
    assert fields["net_pnl"] is None
    assert fields["eligible"] is False
    assert fields["reason"] == "Market data incomplete"


def test_observation_uuid_is_idempotent_per_slot_and_destination():
    fields = _fields("-2")
    assert fields["id"] == _fields("-2")["id"]
    changed = observation_key(
        user_id=uuid.UUID(int=1),
        execution_epoch_id=uuid.UUID(int=3),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:cycle",
        state_version=123,
        evaluation_slot=987,
        follower_position_size=D("-0.5"),
    )
    assert fields["id"] != changed


def test_observation_does_not_duplicate_existing_decision_identity():
    cols = AIProfitExitObservation.__table__.c
    for col in (
        "user_id", "execution_epoch_id", "source_cycle_id",
        "state_version", "evaluation_slot", "follower_position_size",
        "evaluation_basis", "pnl_complete", "funding_model",
        "mark_price", "executable_exit_price", "net_pnl",
        "complete", "eligible", "position_matches", "reason", "observed_at",
    ):
        assert col in cols
    assert cols.net_pnl.nullable is True
    assert cols.observed_at.nullable is False


@pytest.mark.asyncio
async def test_telemetry_insert_is_idempotent_no_llm_or_order_side_effects():
    class Result:
        rowcount = 1

    class Session:
        stmt = None
        commits = 0

        async def execute(self, stmt):
            self.stmt = stmt
            return Result()

        async def commit(self):
            self.commits += 1

    db = Session()
    await record_shadow_observation(
        db, **{
            "user_id": uuid.UUID(int=1),
            "execution_epoch_id": uuid.UUID(int=2),
            "execution_provider": "hyperliquid",
            "execution_network": "mainnet",
            "asset": "BTC",
            "source_cycle_id": "mainnet:BTC:cycle",
            "state_version": 123,
            "evaluation_slot": 987,
            "observation": _observation("-1.23"),
            "position_matches": True,
        }
    )
    stmt = str(db.stmt).upper()
    assert "ON CONFLICT" in stmt
    assert "DO NOTHING" in stmt
    assert "AI_PROFIT_EXIT_OBSERVATIONS" in stmt
    assert db.commits == 1


def test_telemetry_is_before_positive_only_gate_but_shadows_never_queue():
    source = inspect.getsource(decision.evaluate_profit_exit_portfolio)
    record = source.index("await record_shadow_observation(")
    positive_gate = source.index("or observation.economics.net_pnl <= 0")
    assert record < positive_gate
    assert "if mode is ProfitExitFeatureMode.SHADOW and user.copy_state == CopyState.SHADOW:" in source
    assert "and user.copy_state == CopyState.ACTIVE" in source
    assert "mode is ProfitExitFeatureMode.ON" in source
