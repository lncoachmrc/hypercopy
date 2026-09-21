"""RED contracts for RISEx b2 worker wiring.

No production implementation belongs in this commit. These contracts make the
first signed CopyJob path explicit: exact b1 plan, request-specific authorization,
durable first-POST claim, and production fail-closed gates.
"""

from __future__ import annotations

import importlib
import inspect
import os
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.security.risex_order_codec import (
    RISExPlaceOrder,
    build_place_order_action_hash,
    encode_order_data_88,
)
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import prepare_place_order_request
from app.services import risex_copy_execution, risex_order_preparation


ACCOUNT = "0x" + ("11" * 20)
SIGNER = "0x" + ("22" * 20)


def _require(module: object, name: str):
    value = getattr(module, name, None)
    assert value is not None, f"RED: expected {module.__name__}.{name}"
    return value


def _market():
    return risex_order_preparation.RISExMarketMetadata(
        market_id=1,
        step_size=Decimal("0.000001"),
        step_price=Decimal("0.1"),
        min_order_size=Decimal("0.0001"),
        max_leverage=Decimal("50"),
        mark_price=Decimal("86192.08"),
    )


def _plan(*, reduce_only: bool = True):
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=861_920,
        side=1,
        post_only=False,
        reduce_only=reduce_only,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=77,
        ttl_units=0,
    )
    return risex_order_preparation.RISExIOCPlan(
        order=order,
        requested_size=Decimal("0.000100"),
        limit_price=Decimal("86192.0"),
        market=_market(),
    )


def _request(*, reduce_only: bool = True):
    plan = _plan(reduce_only=reduce_only)
    permit = RISExPreparedPlaceOrderPermit(
        account_address=ACCOUNT,
        signer_address=SIGNER,
        action_hash=build_place_order_action_hash(plan.order),
        nonce_anchor=7,
        nonce_bitmap_index=13,
        deadline=2_000_000_000,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=plan.order, permit=permit)


def test_request_from_plan_helper_never_rebuilds_size_price_or_client_id() -> None:
    helper = _require(risex_order_preparation, "prepare_risex_ioc_request_from_plan")
    source = inspect.getsource(helper)

    for forbidden in (
        "resolve_market_metadata(",
        "load_top_of_book(",
        "size_to_steps(",
        "marketable_price_to_ticks(",
        "generate_client_order_id(",
    ):
        assert forbidden not in source, (
            f"RED: request-from-plan must sign the exact authorized plan; found {forbidden}"
        )

    assert "plan.order" in source


@pytest.mark.asyncio
async def test_request_from_plan_signs_exact_order_and_preserves_reduce_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _require(risex_order_preparation, "prepare_risex_ioc_request_from_plan")
    plan = _plan(reduce_only=True)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        risex_order_preparation,
        "load_testnet_signer_credential",
        lambda _env: SimpleNamespace(
            account_address=ACCOUNT,
            signer_address=SIGNER,
        ),
    )
    monkeypatch.setattr(
        risex_order_preparation,
        "collect_runtime_deployment_evidence",
        AsyncMock(
            return_value=SimpleNamespace(
                block_number=100,
                api_chain_id=11155931,
                domain_name="RISE",
                domain_version="1",
                domain_verifying_contract="0x" + ("33" * 20),
                system_router="0x" + ("44" * 20),
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        risex_order_preparation,
        "collect_authorization_session_evidence",
        AsyncMock(
            return_value=SimpleNamespace(
                block_timestamp=1_900_000_000,
                session_expiration=2_000_000_100,
            )
        ),
    )
    monkeypatch.setattr(
        risex_order_preparation,
        "collect_order_nonce_selection",
        AsyncMock(return_value=SimpleNamespace(anchor=7, bitmap_index=13)),
    )

    def fake_permit(*, order, **_kwargs):
        captured["order"] = order
        return RISExPreparedPlaceOrderPermit(
            account_address=ACCOUNT,
            signer_address=SIGNER,
            action_hash=build_place_order_action_hash(order),
            nonce_anchor=7,
            nonce_bitmap_index=13,
            deadline=2_000_000_000,
            _signature=bytes([8]) * 65,
        )

    monkeypatch.setattr(
        risex_order_preparation,
        "prepare_place_order_permit",
        fake_permit,
    )

    request = await helper(
        env={},
        api=object(),
        rpc=object(),
        plan=plan,
        deadline_seconds=30,
    )

    assert captured["order"] is plan.order
    assert request.order is plan.order
    assert request.order.reduce_only is True
    assert request.permit.action_hash == build_place_order_action_hash(plan.order)


def test_exact_size_invariant_is_required_before_signature() -> None:
    checker = _require(risex_order_preparation, "assert_risex_plan_matches_intent")
    intent = risex_order_preparation.RISExOrderIntent(
        symbol="BTC",
        is_buy=False,
        requested_size=Decimal("0.000100"),
        reduce_only=True,
        slippage_bps=25,
        client_order_id=77,
    )
    plan = _plan(reduce_only=True)

    checker(intent=intent, plan=plan)

    bad = risex_order_preparation.RISExIOCPlan(
        order=replace(plan.order, size_steps=101),
        requested_size=Decimal("0.000101"),
        limit_price=plan.limit_price,
        market=plan.market,
    )
    with pytest.raises(Exception, match="size|intent|authorized"):
        checker(intent=intent, plan=bad)


def test_reduce_only_is_bound_into_signed_action_hash_bit() -> None:
    plan = _plan(reduce_only=True)
    request = _request(reduce_only=True)

    # order_flags occupy bits [6..13] inside order_data; reduce_only is flag 0x04.
    order_data = encode_order_data_88(plan.order)
    order_flags = (order_data >> 6) & 0xFF
    assert order_flags & 0x04 == 0x04
    assert request.order.reduce_only is True
    assert request.permit.action_hash == build_place_order_action_hash(plan.order)


def test_process_local_submission_binds_execution_plan_request_and_client_id() -> None:
    submission_type = _require(risex_copy_execution, "RISExPreparedCopySubmission")
    fields = set(getattr(submission_type, "__dataclass_fields__", {}))
    assert {
        "execution_id",
        "cloid",
        "client_order_id",
        "intent",
        "plan",
        "request",
    }.issubset(fields)


def test_first_post_claim_is_row_locked_and_consumes_pre_post_committed() -> None:
    claim = _require(risex_copy_execution, "claim_risex_first_post")
    source = inspect.getsource(claim)

    assert ".with_for_update()" in source
    assert "PRE_POST_COMMITTED" in source
    assert "POST_IN_FLIGHT" in source
    assert "await db.commit()" in source

    for identity in (
        "execution_id",
        "cloid",
        "client_order_id",
        "nonce_anchor",
        "nonce_bitmap_index",
        "reduce_only",
        "requested_size",
        "limit_px",
    ):
        assert identity in source, f"RED: first-POST claim must bind {identity}"


def test_process_risex_job_claims_first_post_before_adapter_write() -> None:
    source = inspect.getsource(risex_copy_execution.process_risex_job)

    claim_index = source.index("claim_risex_first_post(")
    post_index = source.index("await adapter.place_ioc(")
    assert claim_index < post_index


def test_worker_preparation_module_checks_durable_execution_before_risk_or_signing() -> None:
    try:
        module = importlib.import_module("app.services.risex_worker_submission")
    except ModuleNotFoundError:
        pytest.fail("RED: app.services.risex_worker_submission is required")

    prepare = _require(module, "prepare_risex_worker_submission")
    source = inspect.getsource(prepare)

    durable_index = source.index("Execution")
    risk_index = source.index("plan_risex_order_intent(")
    plan_index = source.index("prepare_risex_ioc_plan(")
    sign_index = source.index("prepare_risex_ioc_request_from_plan(")

    assert durable_index < risk_index < plan_index < sign_index
    assert "ExecutionState.SUBMITTING" in source
    assert "ExecutionState.UNKNOWN" in source


def test_request_specific_gate_and_freshness_are_built_after_signed_request() -> None:
    try:
        module = importlib.import_module("app.services.risex_worker_submission")
    except ModuleNotFoundError:
        pytest.fail("RED: app.services.risex_worker_submission is required")

    prepare = _require(module, "prepare_risex_worker_submission")
    source = inspect.getsource(prepare)

    sign_index = source.index("prepare_risex_ioc_request_from_plan(")
    replay_index = source.index("collect_replay_protection_architecture_attestation(", sign_index)
    gate_index = source.index("build_risex_pre_order_gate_for_request(", replay_index)
    freshness_index = source.index("make_freshness_probe(", gate_index)
    transport_index = source.index("RISExSignedTestnetHTTPTransport(", freshness_index)

    assert sign_index < replay_index < gate_index < freshness_index < transport_index


def test_risex_worker_write_gate_matrix_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _require(risex_order_preparation, "assert_risex_worker_write_allowed")
    accepted = getattr(risex_order_preparation, "ADR_0006_MAINNET_GATE_ACCEPTED", None)
    assert accepted is False, "RED: ADR-0006 mainnet gate must default False"

    def env(*, live: str | None, signed: str = "true") -> dict[str, str]:
        values = {"RISEX_SIGNED_WRITES_ENABLED": signed}
        if live is not None:
            values["ENABLE_LIVE_TRADING"] = live
        return values

    # Real-capital worker: ADR-0002 requires ADR-0006 acceptance on ANY RISEx network.
    with pytest.raises(Exception, match="ADR-0002|ADR-0006|real-capital|live"):
        gate(network="testnet", env=env(live="true"))
    with pytest.raises(Exception, match="ADR-0006|mainnet|real-capital|live"):
        gate(network="mainnet", env=env(live="true"))

    # Dedicated test stack: signed testnet is reachable, mainnet remains impossible.
    assert gate(network="testnet", env=env(live="false")) == "testnet"
    with pytest.raises(Exception, match="mainnet|ADR-0006"):
        gate(network="mainnet", env=env(live="false"))

    # Missing or malformed ENABLE_LIVE_TRADING is treated as real-capital fail-closed.
    for malformed in (None, "", "0", "1", "TRUE", "False", "garbage"):
        with pytest.raises(Exception, match="ADR-0002|ADR-0006|real-capital|live"):
            gate(network="testnet", env=env(live=malformed))

    # The signed-write opt-in remains mandatory even on the isolated test stack.
    with pytest.raises(Exception, match="signed|RISEX_SIGNED_WRITES_ENABLED"):
        gate(network="testnet", env=env(live="false", signed="false"))


def test_risex_mainnet_remains_blocked_on_test_stack_even_if_adr_gate_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _require(risex_order_preparation, "assert_risex_worker_write_allowed")
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        True,
    )
    with pytest.raises(Exception, match="mainnet|test stack|ADR-0006"):
        gate(
            network="mainnet",
            env={
                "ENABLE_LIVE_TRADING": "false",
                "RISEX_SIGNED_WRITES_ENABLED": "true",
            },
        )


def test_real_capital_worker_requires_adr_gate_even_for_testnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _require(risex_order_preparation, "assert_risex_worker_write_allowed")
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        True,
    )
    assert gate(
        network="testnet",
        env={
            "ENABLE_LIVE_TRADING": "true",
            "RISEX_SIGNED_WRITES_ENABLED": "true",
        },
    ) == "testnet"
