from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionErrorClass(str, Enum):
    """Semantic class for an explicit Hyperliquid order rejection.

    Transport ambiguity, HTTP failures and nonce ownership are deliberately
    handled outside this classifier. A signed action with an indeterminate
    transport result must remain UNKNOWN and be reconciled before replacement.
    """

    TERMINAL = "TERMINAL"
    LIQUIDITY = "LIQUIDITY"
    TRANSIENT = "TRANSIENT"
    UNCLASSIFIED = "UNCLASSIFIED"


class ActionRetryPolicy(str, Enum):
    NONE = "NONE"
    RECONCILE = "RECONCILE"


@dataclass(frozen=True, slots=True)
class ActionErrorDecision:
    error_class: ActionErrorClass
    retry_policy: ActionRetryPolicy


_LIQUIDITY_TOKENS = (
    "order could not immediately match against any resting orders",
    "order could not immediately match",
    "no liquidity available for market order",
    "ioccancel",
    "ioc cancel",
    "ioccancelrejected",
    "marketordernoliquidity",
    "market order no liquidity",
    "marketordernoliquidityrejected",
)

_TRANSIENT_TOKENS = (
    "open interest is capped",
    "open interest cap",
    "openinterestcap",
    "positionincreaseatopeninterestcap",
    "positionflipatopeninterestcap",
    "tooaggressiveatopeninterestcap",
    "openinterestincrease",
    "oracle issue",
    "oraclerejected",
)

_TERMINAL_TOKENS = (
    "price must be divisible by tick size",
    "order must have minimum value",
    "insufficient margin to place order",
    "reduce only order would increase position",
    "invalid tp/sl price",
    "order has invalid size",
    "order has zero size",
    "perp max position",
    "insufficient spot balance",
    "tickrejected",
    "mintradentlrejected",
    "mintradespotntlrejected",
    "perpmarginrejected",
    "reduceonlyrejected",
    "badtriggerpxrejected",
    "insufficientspotbalancerejected",
    "perpmaxpositionrejected",
)


def classify_action_error(reason: str | None) -> ActionErrorDecision:
    """Classify an explicit exchange rejection without guessing side effects.

    `LIQUIDITY` and `TRANSIENT` are safe to revisit only through a fresh
    reconciliation cycle. We intentionally never ask the same durable job to
    blind-resubmit its old CLOID. Deterministic business errors and unknown
    rejection strings remain terminal for the current job.
    """

    normalized = str(reason or "").strip().lower()
    compact = normalized.replace("_", "").replace("-", "")

    if any(token in normalized or token in compact for token in _LIQUIDITY_TOKENS):
        return ActionErrorDecision(ActionErrorClass.LIQUIDITY, ActionRetryPolicy.RECONCILE)
    if any(token in normalized or token in compact for token in _TRANSIENT_TOKENS):
        return ActionErrorDecision(ActionErrorClass.TRANSIENT, ActionRetryPolicy.RECONCILE)
    if any(token in normalized or token in compact for token in _TERMINAL_TOKENS):
        return ActionErrorDecision(ActionErrorClass.TERMINAL, ActionRetryPolicy.NONE)
    return ActionErrorDecision(ActionErrorClass.UNCLASSIFIED, ActionRetryPolicy.NONE)
