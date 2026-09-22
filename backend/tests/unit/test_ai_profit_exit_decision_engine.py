from __future__ import annotations

import inspect
import uuid
from decimal import Decimal

from app.services.ai_profit_exit import (
    ProfitExitAction,
    profit_exit_decision_id,
    profit_exit_job_id,
)
from app.services.ai_profit_exit_decision import _validated_profit_exit_action
from app.services import execution
from app.workers import ai_intelligence_worker


def test_profit_exit_semantic_identity_is_deterministic_per_evaluation_slot() -> None:
    user_id=uuid.uuid4()
    epoch_id=uuid.uuid4()
    kwargs=dict(
        user_id=user_id,
        execution_epoch_id=epoch_id,
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:cycle",
        state_version=42,
        follower_position=Decimal("0.1"),
        evaluation_slot=123,
    )
    first=profit_exit_decision_id(**kwargs)
    second=profit_exit_decision_id(**kwargs)
    assert first == second
    assert profit_exit_job_id(first) == profit_exit_job_id(second)

    changed=profit_exit_decision_id(**{**kwargs,"evaluation_slot":124})
    assert changed != first


def test_profit_exit_llm_contract_is_closed() -> None:
    action,reason=_validated_profit_exit_action({"action":"CLOSE_PROFIT","reason":"context changed"})
    assert action is ProfitExitAction.CLOSE_PROFIT
    assert reason == "context changed"

    action,_=_validated_profit_exit_action({"action":"OPEN_MORE","reason":"bad"})
    assert action is ProfitExitAction.ABSTAIN

    action,_=_validated_profit_exit_action("not-json")
    assert action is ProfitExitAction.ABSTAIN


def test_ambiguous_profit_exit_retries_only_durable_cloid_resolution() -> None:
    source=inspect.getsource(execution._process_ai_profit_exit_locked)
    ambiguous=source[source.index("if decision.intent_state == ProfitExitIntentState.AMBIGUOUS.value"):]
    ambiguous=ambiguous[:ambiguous.index("open_event = await db.get")]

    assert "select(Execution)" in ambiguous
    assert "_execute_leg(" in ambiguous
    assert "before_submit=" not in ambiguous
    assert "crypto.decrypt" not in ambiguous


def test_ai_worker_runs_profit_exit_as_separate_singleton_workflow() -> None:
    source=inspect.getsource(ai_intelligence_worker.AIIntelligenceWorker)
    assert "hypercopy:ai-profit-exit" in source
    assert "evaluate_profit_exit_portfolio" in source
