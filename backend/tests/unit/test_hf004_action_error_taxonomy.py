import pytest

from app.adapters.action_errors import (
    ActionErrorClass,
    ActionRetryPolicy,
    classify_action_error,
)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Price must be divisible by tick size.", ActionErrorClass.TERMINAL),
        ("Order must have minimum value of $10.", ActionErrorClass.TERMINAL),
        ("Insufficient margin to place order.", ActionErrorClass.TERMINAL),
        ("Reduce only order would increase position.", ActionErrorClass.TERMINAL),
        ("Invalid TP/SL price.", ActionErrorClass.TERMINAL),
        ("perpMaxPositionRejected", ActionErrorClass.TERMINAL),
        ("Order could not immediately match against any resting orders.", ActionErrorClass.LIQUIDITY),
        ("No liquidity available for market order.", ActionErrorClass.LIQUIDITY),
        ("iocCancelRejected", ActionErrorClass.LIQUIDITY),
        ("Too many cumulative requests sent (500 > 100)", ActionErrorClass.TRANSIENT),
        ("positionIncreaseAtOpenInterestCapRejected", ActionErrorClass.TRANSIENT),
        ("oracleRejected", ActionErrorClass.TRANSIENT),
    ],
)
def test_documented_action_errors_have_explicit_semantic_class(reason, expected):
    assert classify_action_error(reason).error_class is expected


def test_only_semantically_recoverable_rejections_delegate_retry_to_reconciliation():
    assert classify_action_error("Order could not immediately match against any resting orders.").retry_policy is ActionRetryPolicy.RECONCILE
    assert classify_action_error("Too many cumulative requests sent").retry_policy is ActionRetryPolicy.RECONCILE
    assert classify_action_error("Insufficient margin to place order.").retry_policy is ActionRetryPolicy.NONE


def test_unknown_rejection_is_conservative_and_never_directly_retried():
    decision = classify_action_error("new undocumented exchange rejection")
    assert decision.error_class is ActionErrorClass.UNCLASSIFIED
    assert decision.retry_policy is ActionRetryPolicy.NONE


def test_transport_ambiguity_is_not_disguised_as_retryable_action_error():
    # Transport exceptions are handled by execution.py as UNKNOWN. If a similar
    # string reaches this explicit-rejection classifier, it remains conservative.
    decision = classify_action_error("connection reset after submit")
    assert decision.error_class is ActionErrorClass.UNCLASSIFIED
    assert decision.retry_policy is ActionRetryPolicy.NONE
