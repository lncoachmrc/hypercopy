from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionErrorClass(str, Enum):
    """Semantic class for a definitive action rejection/cancellation.

    Transport ambiguity, HTTP failures and nonce ownership are deliberately
    handled outside this classifier. A signed action with an indeterminate
    transport result must remain UNKNOWN and be reconciled before replacement.
    The strategy freshness fence can also produce a definitive pre-submit
    cancellation; that case is safe to revisit only through fresh reconciliation.
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
    # Internal definitive pre-submit cancellations. No exchange action was sent;
    # only a newly computed authoritative intent may replace the skipped job.
    "strategy intent canceled pre-submit",
    "mainnet single-writer fence blocked signed action",
    "open interest is capped",
    "open interest cap",
    "openinterestcap",
    "positionincreaseatopeninterestcap",
    "positionflipatopeninterestcap",
    "tooaggressiveatopeninterestcap",
    "openinterestincrease",
    "oracle issue",
    "oraclerejected",
    # Only an explicit exchange rejection reaches the remaining throttle cases.
    # HTTP 429 and transport-level throttles stay outside this classifier and
    # remain Execution.UNKNOWN.
    "rate limited",
    "user rate limit",
    "address rate limit",
    "rate limit exceeded",
    "user rate limit exceeded",
    "address rate limit exceeded",
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


def _compact(value: str) -> str:
    return value.replace("_", "").replace("-", "").replace(" ", "")


def _matches(normalized: str, compact: str, tokens: tuple[str, ...]) -> bool:
    return any(token in normalized or _compact(token) in compact for token in tokens)


def classify_action_error(reason: str | None) -> ActionErrorDecision:
    """Classify a definitive rejection/cancellation without guessing side effects.

    `LIQUIDITY` and `TRANSIENT` are safe to revisit only through a fresh
    reconciliation cycle. We intentionally never ask the same durable job to
    blind-resubmit its old CLOID. Deterministic business errors and unknown
    rejection strings remain terminal for the current job.
    """

    normalized = str(reason or "").strip().lower()
    compact = _compact(normalized)

    if _matches(normalized, compact, _LIQUIDITY_TOKENS):
        return ActionErrorDecision(ActionErrorClass.LIQUIDITY, ActionRetryPolicy.RECONCILE)
    if _matches(normalized, compact, _TRANSIENT_TOKENS):
        return ActionErrorDecision(ActionErrorClass.TRANSIENT, ActionRetryPolicy.RECONCILE)
    if _matches(normalized, compact, _TERMINAL_TOKENS):
        return ActionErrorDecision(ActionErrorClass.TERMINAL, ActionRetryPolicy.NONE)
    return ActionErrorDecision(ActionErrorClass.UNCLASSIFIED, ActionRetryPolicy.NONE)
