from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from app.models.entities import CopyState
from app.services import copy as copy_service
from app.services import execution, reconcile
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


def test_shadow_jobs_are_bound_to_exact_session_and_cannot_turn_live() -> None:
    started = datetime(2026, 9, 23, 17, 0, tzinfo=UTC)
    encoded = started.isoformat()

    assert execution._shadow_session_job_allowed(
        CopyState.SHADOW,
        started,
        encoded,
        "EVENT",
    )
    assert not execution._shadow_session_job_allowed(
        CopyState.SHADOW,
        started,
        None,
        "EVENT",
    )
    assert not execution._shadow_session_job_allowed(
        CopyState.SHADOW,
        started,
        (started - timedelta(minutes=1)).isoformat(),
        "RECONCILE",
    )
    assert not execution._shadow_session_job_allowed(
        CopyState.ACTIVE,
        None,
        encoded,
        "EVENT",
    )
    assert execution._shadow_session_job_allowed(
        CopyState.ACTIVE,
        None,
        None,
        "EVENT",
    )
    assert execution._shadow_session_job_allowed(
        CopyState.SHADOW,
        started,
        None,
        "CLOSE_ALL",
    )


def test_event_and_reconcile_jobs_persist_shadow_session_identity() -> None:
    event_source = inspect.getsource(copy_service.persist_master_fill_and_jobs)
    reconcile_source = inspect.getsource(reconcile._reconcile_user_locked)

    assert "u.shadow_started_at" in event_source
    assert "context['shadow_started_at'] = shadow_started_at.isoformat()" in event_source
    assert "context['shadow_started_at'] = user.shadow_started_at.isoformat()" in reconcile_source


def test_shadow_ai_decision_revalidates_under_position_lock_after_llm() -> None:
    source = inspect.getsource(evaluate_profit_exit_portfolio)

    ai_index = source.index("action, reason, runtime = await _decide_with_ai(inputs)")
    lock_index = source.index("async with position_ledger_lock(user.id):", ai_index)
    user_refresh = source.index("await db.refresh(user)", lock_index)
    shadow_refresh = source.index("await db.refresh(shadow_position)", lock_index)
    commit_index = source.index("await db.commit()", shadow_refresh)

    assert ai_index < lock_index < user_refresh < shadow_refresh < commit_index
    assert "_shadow_economic_state(shadow_position)" in source[lock_index:commit_index]
