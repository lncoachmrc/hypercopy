from __future__ import annotations

import inspect
import uuid
from decimal import Decimal

from app.services.ai_profit_exit import (
    ProfitExitAction,
    profit_exit_decision_id,
    profit_exit_job_id,
)
from app.services.ai_profit_exit_decision import _validated_profit_exit_action, evaluate_profit_exit_portfolio
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
        evaluation_basis="live_exact",
    )
    first=profit_exit_decision_id(**kwargs)
    second=profit_exit_decision_id(**kwargs)
    assert first == second
    assert profit_exit_job_id(first) == profit_exit_job_id(second)

    changed=profit_exit_decision_id(**{**kwargs,"evaluation_slot":124})
    assert changed != first

    shadow=profit_exit_decision_id(
        **{**kwargs,"evaluation_basis":"copy_shadow_estimate"}
    )
    assert shadow != first


def test_profit_exit_llm_contract_is_closed() -> None:
    action,reason=_validated_profit_exit_action({"action":"CLOSE_PROFIT","reason":"context changed"})
    assert action is ProfitExitAction.CLOSE_PROFIT
    assert reason == "context changed"

    action,_=_validated_profit_exit_action({"action":"OPEN_MORE","reason":"bad"})
    assert action is ProfitExitAction.ABSTAIN

    action,_=_validated_profit_exit_action("not-json")
    assert action is ProfitExitAction.ABSTAIN


def test_existing_execution_recovery_precedes_new_submission_gates() -> None:
    source=inspect.getsource(execution._process_ai_profit_exit_locked)
    existing_index=source.index("existing = (await db.execute(select(Execution)")
    mode_index=source.index("await read_profit_exit_mode(db) is not ProfitExitFeatureMode.ON")
    expiry_index=source.index("decision.expires_at <= now")
    credential_index=source.index("cred = (await db.execute(select(SigningCredential)")

    assert existing_index < mode_index < credential_index
    assert existing_index < expiry_index

    recovery=source[existing_index:source.index("open_event = await db.get")]
    assert "_execute_leg(" in recovery
    assert "before_submit=" not in recovery
    assert "crypto.decrypt" not in recovery
    assert "OrderIntent.CLOSE" in recovery


def test_ai_worker_runs_profit_exit_as_separate_singleton_workflow() -> None:
    source=inspect.getsource(ai_intelligence_worker.AIIntelligenceWorker)
    assert "hypercopy:ai-profit-exit" in source
    assert "evaluate_profit_exit_portfolio" in source


def test_profit_exit_evaluator_includes_copy_shadow_as_record_only_input() -> None:
    source = inspect.getsource(evaluate_profit_exit_portfolio)
    assert "User.copy_state == CopyState.ACTIVE" in source
    assert "User.copy_state == CopyState.SHADOW" in source
    assert "PositionLedger.target_size != 0" in source
    assert "collect_shadow_profit_exit_economics(" in source
    assert "and user.copy_state == CopyState.ACTIVE" in source


def test_profit_exit_execution_rechecks_active_copy_state_before_new_submission() -> None:
    source = inspect.getsource(execution._process_ai_profit_exit_locked)
    existing_index = source.index("existing = (await db.execute(select(Execution)")
    active_index = source.index("user.copy_state != CopyState.ACTIVE")
    mode_index = source.index("await read_profit_exit_mode(db) is not ProfitExitFeatureMode.ON")

    assert existing_index < active_index < mode_index


def test_profit_exit_shadow_decisions_never_create_operational_jobs() -> None:
    source = inspect.getsource(evaluate_profit_exit_portfolio)
    operational_index = source.index("operational = (")
    job_index = source.index("job_id = profit_exit_job_id")
    operational_block = source[operational_index:job_index]

    assert "mode is ProfitExitFeatureMode.ON" in operational_block
    assert "user.copy_state == CopyState.ACTIVE" in operational_block
    assert "action is ProfitExitAction.CLOSE_PROFIT" in operational_block
    assert "pnl_complete=observation.pnl_complete" in source
    assert '"shadow_decisions": shadow_decisions' in source


def test_profit_exit_decision_timestamp_is_captured_after_ai_response() -> None:
    source = inspect.getsource(evaluate_profit_exit_portfolio)
    ai_index = source.index("action, reason, runtime = await _decide_with_ai(inputs)")
    timestamp_index = source.index("decision_now = datetime.now(UTC)")

    assert ai_index < timestamp_index
    assert "position_verified_at=decision_now" in source
    assert "decided_at=decision_now" in source
    assert "expires_at=decision_now + timedelta(" in source


def test_profit_exit_evaluator_reads_shared_runtime_mode() -> None:
    source = inspect.getsource(evaluate_profit_exit_portfolio)
    assert "mode = await read_profit_exit_mode(db)" in source


def test_ai_worker_can_leave_idle_state_after_dashboard_mode_change() -> None:
    source = inspect.getsource(ai_intelligence_worker.AIIntelligenceWorker.run)
    assert "await self._profit_exit_mode()" in source
    assert "AI Profit Exit runtime mode enabled; leaving idle state" in source
