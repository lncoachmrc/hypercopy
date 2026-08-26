import pytest

from app.adapters.action_errors import (
    ActionErrorClass,
    ActionRetryPolicy,
    classify_action_error,
)
from app.adapters.hyperliquid import parse_order_response
from app.services.reconcile import _is_liquidity_reject, _persisted_ledger_decimal


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
        ("Order could not immediately match.", ActionErrorClass.LIQUIDITY),
        ("No liquidity available for market order.", ActionErrorClass.LIQUIDITY),
        ("IOC cancel", ActionErrorClass.LIQUIDITY),
        ("iocCancelRejected", ActionErrorClass.LIQUIDITY),
        ("positionIncreaseAtOpenInterestCapRejected", ActionErrorClass.TRANSIENT),
        ("oracleRejected", ActionErrorClass.TRANSIENT),
    ],
)
def test_documented_action_errors_have_explicit_semantic_class(reason, expected):
    assert classify_action_error(reason).error_class is expected


@pytest.mark.parametrize(
    ("error", "expected", "retry_policy"),
    [
        (
            "Order could not immediately match against any resting orders.",
            ActionErrorClass.LIQUIDITY,
            ActionRetryPolicy.RECONCILE,
        ),
        (
            "No liquidity available for market order.",
            ActionErrorClass.LIQUIDITY,
            ActionRetryPolicy.RECONCILE,
        ),
        (
            "Position increase at open interest cap.",
            ActionErrorClass.TRANSIENT,
            ActionRetryPolicy.RECONCILE,
        ),
        (
            "Insufficient margin to place order.",
            ActionErrorClass.TERMINAL,
            ActionRetryPolicy.NONE,
        ),
        (
            "Reduce only order would increase position.",
            ActionErrorClass.TERMINAL,
            ActionRetryPolicy.NONE,
        ),
    ],
)
def test_production_parser_output_flows_into_taxonomy(error, expected, retry_policy):
    fixture = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": error}]}},
    }
    outcome = parse_order_response(fixture)
    assert outcome.state == "REJECTED"
    assert outcome.reason == error
    decision = classify_action_error(outcome.reason)
    assert decision.error_class is expected
    assert decision.retry_policy is retry_policy


def test_only_semantically_recoverable_rejections_delegate_retry_to_reconciliation():
    assert classify_action_error("Order could not immediately match against any resting orders.").retry_policy is ActionRetryPolicy.RECONCILE
    assert classify_action_error("Oracle issue.").retry_policy is ActionRetryPolicy.RECONCILE
    assert classify_action_error("Insufficient margin to place order.").retry_policy is ActionRetryPolicy.NONE


def test_liquidity_backoff_predicate_reuses_the_same_taxonomy():
    for reason in (
        "IOC cancel",
        "iocCancelRejected",
        "Order could not immediately match.",
        "No liquidity available for market order.",
    ):
        assert _is_liquidity_reject(reason)
    assert not _is_liquidity_reject("Insufficient margin to place order.")


def test_persisted_ledger_precision_matches_numeric_30_12():
    assert _persisted_ledger_decimal(__import__("decimal").Decimal("1.00000000000049")) == __import__("decimal").Decimal("1.000000000000")
    assert _persisted_ledger_decimal(__import__("decimal").Decimal("1.00000000000050")) == __import__("decimal").Decimal("1.000000000001")


def test_unknown_rejection_is_conservative_and_never_directly_retried():
    decision = classify_action_error("new undocumented exchange rejection")
    assert decision.error_class is ActionErrorClass.UNCLASSIFIED
    assert decision.retry_policy is ActionRetryPolicy.NONE


def test_transport_and_nonce_failures_stay_out_of_explicit_order_taxonomy():
    # Signed-action HTTP/transport ambiguity remains Execution.UNKNOWN and nonce
    # coordination has its own HF-003 policy. Neither is safe to reinterpret as
    # an explicit no-effect order rejection here.
    for reason in (
        "connection reset after submit",
        "429 Too Many Requests",
        "invalid nonce",
    ):
        decision = classify_action_error(reason)
        assert decision.error_class is ActionErrorClass.UNCLASSIFIED
        assert decision.retry_policy is ActionRetryPolicy.NONE
