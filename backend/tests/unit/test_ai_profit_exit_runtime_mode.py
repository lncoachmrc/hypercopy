from app.services.ai_profit_exit import ProfitExitFeatureMode
from app.services.ai_profit_exit_mode import (
    AI_PROFIT_EXIT_MODE_FLAG_SLUG,
    _mode_from_value,
)


def test_profit_exit_mode_flag_has_dedicated_namespace() -> None:
    assert AI_PROFIT_EXIT_MODE_FLAG_SLUG == "ai:profit_exit_mode"


def test_profit_exit_mode_parser_accepts_all_supported_modes() -> None:
    assert _mode_from_value({"mode": "OFF"}) is ProfitExitFeatureMode.OFF
    assert _mode_from_value({"mode": "shadow"}) is ProfitExitFeatureMode.SHADOW
    assert _mode_from_value({"mode": "ON"}) is ProfitExitFeatureMode.ON


def test_profit_exit_mode_parser_fails_closed_on_malformed_state() -> None:
    assert _mode_from_value(None) is ProfitExitFeatureMode.OFF
    assert _mode_from_value({}) is ProfitExitFeatureMode.OFF
    assert _mode_from_value({"mode": "INVALID"}) is ProfitExitFeatureMode.OFF
