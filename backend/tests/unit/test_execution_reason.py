from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.models.entities import ExecutionState
from app.services.execution_reason import (
    execution_reason_code,
    master_transition_reason,
    order_provenance_from_status,
)


def _event(start: str, after: str, raw: dict | None = None) -> Any:
    return SimpleNamespace(
        start_position=start,
        position_after=after,
        raw=raw or {},
    )


def _execution(state: ExecutionState = ExecutionState.FILLED) -> Any:
    return SimpleNamespace(
        state=state,
        reject_reason=None,
    )


def _job(origin: str) -> Any:
    return SimpleNamespace(origin=origin)


def test_master_transition_reasons_cover_open_reduce_close_increase_and_reversal() -> None:
    assert master_transition_reason(_event("0", "1")) == "MASTER_OPEN"
    assert master_transition_reason(_event("1", "2")) == "MASTER_INCREASE"
    assert master_transition_reason(_event("2", "1")) == "MASTER_REDUCE"
    assert master_transition_reason(_event("1", "0")) == "MASTER_CLOSE"
    assert master_transition_reason(_event("1", "-1")) == "MASTER_REVERSAL"


def test_master_order_provenance_overrides_generic_close_reason() -> None:
    event = _event(
        "1",
        "0",
        {
            "_hypercopy_order_provenance": {
                "reason_code": "STOP_LOSS",
                "order_type": "Stop Market",
            }
        },
    )
    assert master_transition_reason(event) == "MASTER_STOP_LOSS"

    event.raw["_hypercopy_order_provenance"]["reason_code"] = "TAKE_PROFIT"
    assert master_transition_reason(event) == "MASTER_TAKE_PROFIT"


def test_execution_reason_codes_cover_ai_manual_reconcile_and_failures() -> None:
    assert execution_reason_code(
        _execution(),
        _job("AI_PROFIT_EXIT"),
        None,
    ) == "AI_PROFIT_EXIT"
    assert execution_reason_code(
        _execution(),
        _job("CLOSE_ALL"),
        None,
    ) == "USER_CLOSE_ALL"
    assert execution_reason_code(
        _execution(),
        _job("RECONCILE"),
        None,
    ) == "RECONCILE"
    assert execution_reason_code(
        _execution(ExecutionState.REJECTED),
        _job("EVENT"),
        _event("0", "1"),
    ) == "EXECUTION_REJECTED"
    assert execution_reason_code(
        _execution(ExecutionState.CANCELED),
        _job("EVENT"),
        _event("0", "1"),
    ) == "EXECUTION_CANCELED"


def test_order_status_parser_recognizes_tp_and_sl_without_guessing() -> None:
    tp = {
        "status": "order",
        "order": {
            "status": "filled",
            "statusTimestamp": 123,
            "order": {
                "orderType": "Take Profit Market",
                "isTrigger": True,
                "isPositionTpsl": True,
                "triggerPx": "100000",
            },
        },
    }
    parsed = order_provenance_from_status(tp)
    assert parsed is not None
    assert parsed["reason_code"] == "TAKE_PROFIT"
    assert parsed["order_type"] == "Take Profit Market"

    sl = {
        "status": "order",
        "order": {
            "status": "filled",
            "statusTimestamp": 124,
            "order": {
                "orderType": "Stop Limit",
                "isTrigger": True,
                "isPositionTpsl": True,
                "triggerPx": "90000",
            },
        },
    }
    parsed = order_provenance_from_status(sl)
    assert parsed is not None
    assert parsed["reason_code"] == "STOP_LOSS"


def test_order_status_parser_does_not_mislabel_normal_orders() -> None:
    response = {
        "status": "order",
        "order": {
            "status": "filled",
            "order": {
                "orderType": "Market",
                "isTrigger": False,
                "isPositionTpsl": False,
                "triggerPx": "0",
            },
        },
    }
    parsed = order_provenance_from_status(response)
    assert parsed is not None
    assert parsed["reason_code"] is None

    assert order_provenance_from_status({"status": "unknownOid"}) is None
