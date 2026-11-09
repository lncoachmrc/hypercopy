"""Profit retention v1: causal sampled high-water context, SHADOW only."""
from __future__ import annotations

import inspect
import uuid
from decimal import Decimal as D

import pytest

from app.services import ai_profit_exit_decision as decision
from app.services.ai_profit_exit import ProfitExitAction
from app.services.ai_profit_exit_retention import (
    RETENTION_VARIANT,
    build_retention_context,
    read_retention_context,
)


def _context(current, peak=None, last=None, count=0, *, complete=True):
    return build_retention_context(
        current_net_pnl=D(current),
        prior_peak_net_pnl=D(peak) if peak is not None else None,
        prior_latest_net_pnl=D(last) if last is not None else None,
        prior_samples=count,
        pnl_complete=complete,
    )


def test_first_sample_never_invents_intraperiod_mfe_or_trend():
    data = _context("5.25")
    assert data["variant"] == RETENTION_VARIANT
    assert data["status"] == "observed"
    assert data["comparison_available"] is False
    assert data["sample_count"] == 1
    assert data["observed_peak_net_pnl_usdc"] == "5.25"
    assert data["observed_giveback_usdc"] == "0.00"
    assert data["previous_net_pnl_usdc"] is None
    assert data["peak_is_intraperiod_mfe"] is False


def test_sampled_giveback_and_recent_change_are_separate():
    data = _context("6", "10", "7", 2)
    assert data["sample_count"] == 3
    assert data["observed_peak_net_pnl_usdc"] == "10"
    assert data["observed_giveback_usdc"] == "4"
    assert data["observed_giveback_pct"] == "40.00"
    assert data["change_since_previous_usdc"] == "-1"


def test_new_sampled_peak_has_zero_giveback():
    data = _context("12", "10", "10", 3)
    assert data["observed_peak_net_pnl_usdc"] == "12"
    assert data["observed_giveback_pct"] == "0.00"
    assert data["change_since_previous_usdc"] == "2"


def test_shadow_estimate_cannot_be_reported_as_complete_funding():
    data = _context("4.62", complete=False)
    assert data["funding_complete"] is False


@pytest.mark.parametrize("bad", ["0", "-0.01", "NaN", "Infinity", "-Infinity"])
def test_nonprofitable_or_nonfinite_net_pnl_cannot_enter_research(bad):
    with pytest.raises(ValueError):
        _context(bad)


def test_corrupt_history_is_rejected_not_used_as_ai_evidence():
    with pytest.raises(ValueError):
        _context("4", "3", "5", 2)
    with pytest.raises(ValueError):
        _context("4", count=1)
    with pytest.raises(ValueError):
        _context("4", "10", "8", 0)


@pytest.mark.asyncio
async def test_history_query_uses_exact_cycle_and_destination_scope():
    class Result:
        def one_or_none(self):
            return D("7"), D("10"), 3

    class Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

    session = Session()
    result = await read_retention_context(
        session,
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:current",
        state_version=52,
        follower_position_size=D("1"),
        pnl_complete=False,
        current_net_pnl=D("6"),
    )
    assert result["observed_giveback_pct"] == "40.00"
    assert result["sample_count"] == 4

    sql = str(session.statement)
    for column in (
        "user_id", "execution_epoch_id", "execution_provider",
        "execution_network", "asset", "source_cycle_id",
        "state_version", "follower_position_size", "pnl_complete",
    ):
        assert f"ai_profit_exit_decisions.{column}" in sql
    assert "max(ai_profit_exit_decisions.net_pnl) OVER" in sql
    assert "ai_profit_exit_decisions.net_pnl >" in sql
    assert "ORDER BY ai_profit_exit_decisions.decided_at DESC" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_on_prompt_remains_unchanged_without_research_context(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    seen = []

    async def fake_provider(system, prompt):
        seen.append((system, prompt))
        return {"action": "HOLD", "reason": "continue"}, {
            "provider": "test", "model": "test", "fallback_index": 0, "failures": [],
        }

    monkeypatch.setattr(decision, "call_llm_with_failover", fake_provider)
    action, _, _ = await decision._decide_with_ai({"net_pnl": "6"})
    assert action is ProfitExitAction.HOLD
    assert len(seen) == 1
    assert "SHADOW-ONLY PROFIT RETENTION RESEARCH" not in seen[0][0]
    assert "fixed retracement rules" in seen[0][0]


@pytest.mark.asyncio
async def test_shadow_prompt_uses_observed_profit_erosion_without_hard_threshold(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    seen = []

    async def fake_provider(system, prompt):
        seen.append((system, prompt))
        return {"action": "CLOSE_PROFIT", "reason": "observed giveback"}, {
            "provider": "test", "model": "test", "fallback_index": 0, "failures": [],
        }

    monkeypatch.setattr(decision, "call_llm_with_failover", fake_provider)
    payload = {"net_pnl": "6", "profit_retention": _context("6", "10", "7", 2)}
    action, _, _ = await decision._decide_with_ai(payload)
    assert action is ProfitExitAction.CLOSE_PROFIT
    assert "SHADOW-ONLY PROFIT RETENTION RESEARCH" in seen[0][0]
    assert "do not introduce a fixed exit threshold" in seen[0][0]
    assert '"observed_giveback_pct":"40.00"' in seen[0][1]


def test_retention_context_is_only_loaded_for_shadow_mode_after_dedupe():
    source = inspect.getsource(decision.evaluate_profit_exit_portfolio)
    duplicate_index = source.index("if await db.get(AIProfitExitDecision, decision_id) is not None:")
    pilot_index = source.index("if mode is ProfitExitFeatureMode.SHADOW:")
    call_index = source.index("action, reason, runtime = await _decide_with_ai(inputs)")
    assert duplicate_index < pilot_index < call_index
    pilot_scope = source[pilot_index:call_index]
    assert "read_retention_context(" in pilot_scope
    assert "user_id=user.id" in pilot_scope
    assert "state_version=cycle.state_version" in pilot_scope
    assert "follower_position_size=observation.current_position" in pilot_scope
    assert "pnl_complete=observation.pnl_complete" in pilot_scope
    # The operational branch must remain gated by ON and ACTIVE.
    assert "mode is ProfitExitFeatureMode.ON" in source
    assert "and user.copy_state == CopyState.ACTIVE" in source
