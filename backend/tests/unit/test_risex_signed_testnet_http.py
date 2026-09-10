from __future__ import annotations

import asyncio

import httpx
import pytest

from app.security.risex_signed_testnet_policy import SignedTestnetPolicy


def _policy(**overrides: object) -> SignedTestnetPolicy:
    values: dict[str, object] = {
        'network': 'testnet',
        'explicit_approval': True,
        'deployment_verdict': 'PASS',
        'deployment_identity_verified': True,
        'disposable_account_asserted': True,
        'dedicated_signer_asserted': True,
        'operatorhub_bypass_disabled': True,
    }
    values.update(overrides)
    return SignedTestnetPolicy(**values)  # type: ignore[arg-type]


def test_signed_transport_allows_only_permit_order_post() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={'success': True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = RISExSignedTestnetHTTPTransport(policy=_policy(), client=client)
    result = asyncio.run(transport.post_json('/v1/orders/place', json={'permit': {'signature': 'redacted'}}))
    asyncio.run(client.aclose())

    assert result == {'success': True}
    assert [str(request.url) for request in calls] == ['https://api.testnet.rise.trade/v1/orders/place']
    assert 'authorization' not in calls[0].headers


def test_signed_transport_rejects_unapproved_post_before_network() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    calls: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200)))
    transport = RISExSignedTestnetHTTPTransport(policy=_policy(), client=client)

    with pytest.raises(SignedTestnetBlocked, match='approved signed-testnet endpoint'):
        asyncio.run(transport.post_json('/v1/account/deposit', json={'amount': '1'}))
    asyncio.run(client.aclose())

    assert calls == []


@pytest.mark.parametrize('header', ['Authorization', 'Proxy-Authorization', 'Cookie', 'X-API-Key', 'X-Auth-Token'])
def test_signed_transport_rejects_ambient_auth_headers(header: str) -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    client = httpx.AsyncClient(headers={header: 'forbidden'})
    with pytest.raises(SignedTestnetBlocked, match='JWT/OperatorHub|ambient authentication'):
        RISExSignedTestnetHTTPTransport(policy=_policy(), client=client)
    asyncio.run(client.aclose())


def test_signed_transport_rejects_non_testnet_policy() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    with pytest.raises(SignedTestnetBlocked, match='testnet'):
        RISExSignedTestnetHTTPTransport(policy=_policy(network='mainnet'))
