from __future__ import annotations

import inspect

from app.models.entities import CopyState
from app.services import execution
from app.services.ai_profit_exit_decision import evaluate_profit_exit_portfolio


def test_shadow_execution_uses_virtual_position_before_sizing() -> None:
    source = inspect.getsource(execution._process_job_locked)

    virtual_current = source.index(
        "current = Decimal(str(shadow_position.size))"
    )
    sizing = source.index("sizing = plan(")

    assert virtual_current < sizing
    assert "current_shadow_positions(" in source
    assert "apply_shadow_plan(" in source
    assert "persist_shadow_plan_result(" in source


def test_shadow_branch_returns_before_any_provider_write_path() -> None:
    source = inspect.getsource(execution._process_job_locked)

    shadow_branch = source.index(
        "if _shadow_suppresses_exchange(user.copy_state, job.origin):"
    )
    shadow_return = source.index(
        "return await _finish(db, job, JobState.DONE, 'Shadow mode')",
        shadow_branch,
    )
    live_gate = source.index("if not await live_trading_allowed", shadow_return)
    decrypt = source.index("private_key = crypto.decrypt", shadow_return)

    assert shadow_branch < shadow_return < live_gate < decrypt


def test_profit_exit_shadow_rows_are_evaluated_but_never_operational() -> None:
    source = inspect.getsource(evaluate_profit_exit_portfolio)

    assert "User.copy_state == CopyState.SHADOW" in source
    assert "ShadowPositionLedger.size != 0" in source

    operational = source.index(
        "user.copy_state == CopyState.ACTIVE"
    )
    mode_gate = source.index(
        "mode is ProfitExitFeatureMode.ON",
        operational,
    )
    job_id = source.index(
        "job_id = profit_exit_job_id(decision_id) if operational else None"
    )

    assert operational < mode_gate < job_id


def test_close_all_shadow_exception_is_preserved() -> None:
    assert execution._shadow_suppresses_exchange(
        CopyState.SHADOW,
        "CLOSE_ALL",
    ) is False
