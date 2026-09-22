from __future__ import annotations

import inspect

from app.api import ai
from app.services import execution
from app.services.ai_profit_exit_decision import evaluate_profit_exit_portfolio


def test_profit_exit_mode_api_is_superadmin_scoped_and_audited() -> None:
    get_source = inspect.getsource(ai.profit_exit_mode)
    post_source = inspect.getsource(ai.update_profit_exit_mode)

    assert "Depends(superadmin)" in get_source
    assert "Depends(superadmin)" in post_source
    assert "set_profit_exit_mode(" in post_source
    assert "AI_PROFIT_EXIT_MODE_CHANGED" in post_source


def test_profit_exit_mode_is_shared_by_decision_and_execution_paths() -> None:
    decision_source = inspect.getsource(evaluate_profit_exit_portfolio)
    execution_source = inspect.getsource(execution._process_ai_profit_exit_locked)

    assert "mode = await read_profit_exit_mode(db)" in decision_source
    assert execution_source.count("await read_profit_exit_mode(db)") >= 2
