"""Causal and bounded e-learning evidence; SHADOW-only research."""
from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from app.services import ai_profit_exit_decision as decision
from app.services.ai_profit_exit_learning import (
    LEARNING_VERSION,
    ShadowLearningPair,
    _learning_query,
    learning_feedback_from_pairs,
    read_shadow_learning_feedback,
)


NOW = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)


def pair(i, action="CLOSE_PROFIT", *, later="9", current="10", lag=120, cycle=None):
    at = NOW - timedelta(days=40-i)
    return ShadowLearningPair(
        cycle_id=cycle or f"mainnet:BTC:cycle-{i}",
        decision_at=at,
        next_observation_at=at+timedelta(seconds=lag),
        action=action,
        decision_net_pnl=D(current),
        next_net_pnl=D(later),
        entry_notional=D("1000"),
    )


def build(pairs):
    return learning_feedback_from_pairs(
        pairs, now=NOW, execution_provider="risex",
        execution_network="testnet", asset="BTC",
    )


def test_learning_collects_without_inventing_improvement_from_tiny_sample():
    x=build([pair(i) for i in range(5)])
    assert x["status"] == "collecting"
    assert x["independent_source_cycles"] == 5
    assert "later_period_observations" not in x
    assert x["policy_promotion"] == "disabled"


def test_learning_publishes_descriptive_later_period_when_many_independent_cycles():
    samples=[pair(i, action="CLOSE_PROFIT" if i % 2 else "HOLD",
                  later="9" if i % 2 else "11") for i in range(30)]
    x=build(samples)
    assert x["status"] == "exploratory"
    assert x["version"] == LEARNING_VERSION
    assert x["earlier_cycles"] == 21
    assert x["later_cycles"] == 9
    assert x["independent_source_cycles"] == 30
    assert x["later_period_observations"]["CLOSE_PROFIT"]["median_signed_advantage_bps"] == "10.00"
    assert x["later_period_observations"]["HOLD"]["median_signed_advantage_bps"] == "10.00"
    assert x["policy_promotion"] == "disabled"
    assert "realized" not in x.get("data_quality", "").split("not_")[0]


def test_multiple_observations_from_same_source_cycle_count_only_once():
    p=[pair(i, cycle="same-source-cycle") for i in range(30)]
    x=build(p)
    assert x["independent_source_cycles"] == 1
    assert x["status"] == "collecting"


@pytest.mark.parametrize("bad", [
    dict(lag=0),
    dict(lag=7201),
    dict(current="-1"),
    dict(current="NaN"),
    dict(later="Infinity"),
])
def test_learning_fails_closed_on_stale_future_or_invalid_pnl(bad):
    x=build([pair(0, **bad)])
    assert x["independent_source_cycles"] == 0


def test_learning_never_uses_a_future_not_yet_observed():
    row=ShadowLearningPair(
        cycle_id="future", decision_at=NOW-timedelta(minutes=1),
        next_observation_at=NOW+timedelta(seconds=1), action="CLOSE_PROFIT",
        decision_net_pnl=D("10"), next_net_pnl=D("9"),
        entry_notional=D("1000"),
    )
    assert build([row])["independent_source_cycles"] == 0


def test_learning_does_not_confuse_hold_with_close_reward():
    rows=[pair(0, "CLOSE_PROFIT", later="12"),
          pair(1, "HOLD", later="12")]
    x=build(rows)
    assert x["status"] == "collecting"
    # Capturing opposing advantages is tested once the sample gate opens.
    bigger=[pair(i, "CLOSE_PROFIT" if i%2 else "HOLD", later="12") for i in range(30)]
    x=build(bigger)
    assert x["later_period_observations"]["CLOSE_PROFIT"]["median_signed_advantage_bps"] == "-20.00"
    assert x["later_period_observations"]["HOLD"]["median_signed_advantage_bps"] == "20.00"


def test_lateral_query_filters_scope_and_future_verified_shadow_observation():
    statement=str(_learning_query(provider="risex", network="testnet", asset="BTC", now=NOW))
    for name in ["LATERAL", "ai_profit_exit_observations", "ai_profit_exit_decisions",
                 "source_cycle_id", "state_version", "execution_epoch_id",
                 "follower_position_size", "position_matches", "pnl_complete",
                 "net_pnl", "decided_at", "observed_at"]:
        assert name in statement


@pytest.mark.asyncio
async def test_learning_read_only_filters_current_source_cycle_and_live_copy_inputs():
    at=NOW-timedelta(hours=2)
    make=lambda cycle,mode: (
        cycle, at, "CLOSE_PROFIT", D("10"), D("-0.5"),
        {"copy_state":mode,"evaluation_basis":"risex_shadow_estimate","entry_price":"100"},
        D("9"), at+timedelta(minutes=10),
    )
    class Result:
        def all(self):return [
            make("other-cycle","SHADOW"),
            make("current-cycle","SHADOW"),
            make("live-copy","ACTIVE"),
        ]
    class DB:
        def __init__(self):self.queries=0
        async def execute(self,stmt):
            self.queries+=1
            return Result()
    db=DB()
    result=await read_shadow_learning_feedback(
        db, provider="risex", network="testnet", asset="BTC",
        current_source_cycle_id="current-cycle", now=NOW,
    )
    assert result["independent_source_cycles"] == 1
    assert result["status"] == "collecting"
    assert db.queries == 1


@pytest.mark.asyncio
async def test_learning_rejects_unrecognized_provider_before_query():
    class DB:
        async def execute(self,stmt):raise AssertionError("Should not reach database")
    with pytest.raises(ValueError):
        await read_shadow_learning_feedback(
            DB(), provider="unknown", network="testnet", asset="BTC",
            current_source_cycle_id="anything", now=NOW,
        )


@pytest.mark.asyncio
async def test_shadow_prompt_uses_learning_only_as_weak_context(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    seen=[]
    async def fake_llm(system,prompt):
        seen.append((system,prompt))
        return {"action":"HOLD","reason":"evidence weak"}, {"provider":"test","model":"test"}
    monkeypatch.setattr(decision,"call_llm_with_failover",fake_llm)
    await decision._decide_with_ai({"net_pnl":"1"})
    assert "SHADOW RESEARCH FEEDBACK" not in seen[0][0]
    await decision._decide_with_ai({"net_pnl":"1","learning_feedback":{
        "status":"exploratory","version":LEARNING_VERSION,
    }})
    assert "SHADOW RESEARCH FEEDBACK" in seen[1][0]
    assert "not with an actual alternative execution" not in seen[1][0]  # only in feedback text, not prompt
    assert "override" in seen[1][0]


def test_learning_never_routes_order_and_is_shadow_short_only():
    s=inspect.getsource(decision.evaluate_profit_exit_portfolio)
    assert "read_shadow_learning_feedback(" in s
    assert "_shadow_retention_allowed" in s
    assert "destination.provider == \"hyperliquid\"" in s
    assert 'and user.copy_state == CopyState.ACTIVE' in s
