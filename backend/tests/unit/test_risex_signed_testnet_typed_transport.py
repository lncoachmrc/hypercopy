from __future__ import annotations

import asyncio
import json
from time import time
from typing import Any

import httpx
import pytest

from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import (
    RISExPreparedPlaceOrderRequest,
    prepare_place_order_request,
)
from app.security.risex_pre_order_gate import authorize_pre_order_probe
from app.security.risex_signed_testnet_policy import SignedTestnetPolicy
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence

ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)


def _gate():
    now = int(time())
    policy = SignedTestnetPolicy(
        network='testnet',
        explicit_approval=True,
        deployment_verdict='PASS',
        deployment_identity_verified=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
    )
    evidence = RISExSignerCapabilityEvidence(
        network='testnet',
        account=ACCOUNT,
        signer=SIGNER,
        chain_id=11155931,
        auth_contract=AUTH,
        router=ROUTER,
        session_active=True,
        session_account=ACCOUNT,
        session_expiration=now + 3600,
        onchain_perps_only_scope=True,
        perps_order_succeeded=None,
        fund_movement_rejected=True,
        withdrawal_rejected=True,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=True,
    )
    return authorize_pre_order_probe(
        policy=policy,
        evidence=evidence,
        now=now,
        replay_protection_verified=True,
    )


def _request(*, account: str = ACCOUNT, signer: str = SIGNER):
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=0,
        client_order_id=7,
        ttl_units=0,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=account,
        signer_address=signer,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=1_800_000_300,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


def test_transport_posts_only_typed_place_order_request() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport

    calls: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={'success': True})
        )
    )
    transport = RISExSignedTestnetHTTPTransport(gate=_gate(), client=client)
    prepared = _request()

    result = asyncio.run(transport.post_place_order(prepared))
    asyncio.run(client.aclose())

    assert result == {'success': True}
    assert len(calls) == 1
    assert str(calls[0].url) == 'https://api.testnet.rise.trade/v1/orders/place'
    assert json.loads(calls[0].content) == prepared.json_for_testnet_transport()


def test_transport_has_no_public_generic_post_json_bypass() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport

    transport = RISExSignedTestnetHTTPTransport(gate=_gate())
    try:
        assert not hasattr(transport, 'post_json')
    finally:
        asyncio.run(transport.aclose())


def test_transport_rejects_untyped_request_before_network() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    calls: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={'success': True})
        )
    )
    transport = RISExSignedTestnetHTTPTransport(gate=_gate(), client=client)
    untyped: Any = {'permit': {'account': ACCOUNT, 'signer': SIGNER}}

    with pytest.raises(SignedTestnetBlocked, match='typed place-order request'):
        asyncio.run(transport.post_place_order(untyped))
    asyncio.run(client.aclose())

    assert calls == []


def test_transport_revalidates_hand_built_typed_request_before_network() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    signed = _request()
    changed_order = RISExPlaceOrder(
        market_id=signed.order.market_id,
        size_steps=signed.order.size_steps + 1,
        price_ticks=signed.order.price_ticks,
        side=signed.order.side,
        post_only=signed.order.post_only,
        reduce_only=signed.order.reduce_only,
        stp_mode=signed.order.stp_mode,
        order_type=signed.order.order_type,
        time_in_force=signed.order.time_in_force,
        client_order_id=signed.order.client_order_id,
        ttl_units=signed.order.ttl_units,
    )
    forged = RISExPreparedPlaceOrderRequest(order=changed_order, permit=signed.permit)

    calls: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={'success': True})
        )
    )
    transport = RISExSignedTestnetHTTPTransport(gate=_gate(), client=client)

    with pytest.raises(SignedTestnetBlocked, match='action hash'):
        asyncio.run(transport.post_place_order(forged))
    asyncio.run(client.aclose())

    assert calls == []
