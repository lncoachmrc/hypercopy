from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.models.entities import CopyJob, Execution, ExecutionState, MasterEvent


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)


def master_transition_reason(event: MasterEvent | None) -> str:
    if event is None:
        return "STRATEGY_EVENT"

    start = _decimal(event.start_position)
    after = _decimal(event.position_after)

    reducing_same_side = (
        start != 0
        and after != 0
        and (start > 0) == (after > 0)
        and abs(after) < abs(start)
    )
    closes_position = start != 0 and after == 0

    raw = event.raw or {}
    provenance = raw.get("_hypercopy_order_provenance")
    if isinstance(provenance, dict):
        code = str(provenance.get("reason_code") or "").upper()
        is_position_tpsl = provenance.get("is_position_tpsl") is True
        if (
            code in {"STOP_LOSS", "TAKE_PROFIT"}
            and is_position_tpsl
            and (reducing_same_side or closes_position)
        ):
            return f"MASTER_{code}"

    if start == 0 and after != 0:
        return "MASTER_OPEN"
    if closes_position:
        return "MASTER_CLOSE"
    if start != 0 and after != 0 and (start > 0) != (after > 0):
        return "MASTER_REVERSAL"
    if start != 0 and after != 0 and (start > 0) == (after > 0):
        if abs(after) > abs(start):
            return "MASTER_INCREASE"
        if reducing_same_side:
            return "MASTER_REDUCE"
    return "STRATEGY_EVENT"


def execution_reason_code(
    execution: Execution,
    job: CopyJob,
    master_event: MasterEvent | None,
) -> str:
    if execution.state == ExecutionState.REJECTED:
        return "EXECUTION_REJECTED"
    if execution.state == ExecutionState.CANCELED:
        return "EXECUTION_CANCELED"
    if execution.state in {ExecutionState.UNKNOWN, ExecutionState.QUARANTINED}:
        return "EXECUTION_UNRESOLVED"

    origin = str(job.origin or "").upper()
    if origin == "AI_PROFIT_EXIT":
        return "AI_PROFIT_EXIT"
    if origin == "CLOSE_ALL":
        return "USER_CLOSE_ALL"
    if origin == "RECONCILE":
        return "RECONCILE"
    if origin == "ADMIN_RECONCILE":
        return "ADMIN_RECONCILE"
    if origin == "ADMIN_LEVERAGE_SYNC":
        return "ADMIN_LEVERAGE_SYNC"
    if origin == "EVENT":
        return master_transition_reason(master_event)
    return "STRATEGY_EXECUTION"


def execution_reason_detail(
    execution: Execution,
    *,
    ai_decision_reason: str | None = None,
) -> str | None:
    if execution.reject_reason:
        return execution.reject_reason
    if ai_decision_reason:
        return ai_decision_reason
    return None


def order_provenance_from_status(response: object) -> dict[str, Any] | None:
    if not isinstance(response, dict) or str(response.get("status") or "") != "order":
        return None

    wrapper = response.get("order")
    if not isinstance(wrapper, dict):
        return None
    order = wrapper.get("order")
    if not isinstance(order, dict):
        return None

    order_type = str(order.get("orderType") or "").strip()
    lowered = order_type.lower()

    reason_code: str | None = None
    if lowered.startswith("take profit"):
        reason_code = "TAKE_PROFIT"
    elif lowered.startswith("stop "):
        reason_code = "STOP_LOSS"

    return {
        "reason_code": reason_code,
        "order_type": order_type or None,
        "is_trigger": bool(order.get("isTrigger")),
        "is_position_tpsl": bool(order.get("isPositionTpsl")),
        "trigger_px": order.get("triggerPx"),
        "status": wrapper.get("status"),
        "status_timestamp": wrapper.get("statusTimestamp"),
    }
