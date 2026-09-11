from __future__ import annotations

import asyncio
from time import time

import httpx
import pytest

from app.security.risex_pre_order_gate import RISExPreOrderProbeGate, authorize_pre_order_probe
from app.security.risex_signed_testnet_policy import SignedTestnetPolicy
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)


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


def _gate() -> RISExPreOrderProbeGate:
    now = int(time())
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
        policy=_policy(),
        evidence=evidence,
        now=now,
        replay_protection_verified=True,
    )


def test_signed_transport_allows_only_permit_order_post() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={'success': True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = RISExSignedTestnetHTTPTransport(gate=_gate(), client=client)
    result = asyncio.run(
        transport.post_json('/v1/orders/place', json={'permit': {'signature': 'redacted'}})
    )
    asyncio.run(client.aclose())

    assert result == {'success': True}
    assert [str(request.url) for request in calls] == ['https://api.testnet.rise.trade/v1/orders/place']
    assert 'authorization' not in calls[0].headers


def test_signed_transport_rejects_unapproved_post_before_network() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    calls: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200)
        )
    )
    transport = RISExSignedTestnetHTTPTransport(gate=_gate(), client=client)

    with pytest.raises(SignedTestnetBlocked, match='approved signed-testnet endpoint'):
        asyncio.run(transport.post_json('/v1/account/deposit', json={'amount': '1'}))
    asyncio.run(client.aclose())

    assert calls == []


@pytest.mark.parametrize(
    'header',
    ['Authorization', 'Proxy-Authorization', 'Cookie', 'X-API-Key', 'X-Auth-Token'],
)
def test_signed_transport_rejects_ambient_auth_headers(header: str) -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    client = httpx.AsyncClient(headers={header: 'forbidden'})
    with pytest.raises(SignedTestnetBlocked, match='JWT/OperatorHub|ambient authentication'):
        RISExSignedTestnetHTTPTransport(gate=_gate(), client=client)
    asyncio.run(client.aclose())


def test_signed_transport_rejects_hand_built_unattested_gate() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked

    forged_gate = RISExPreOrderProbeGate(
        account_address=ACCOUNT,
        signer_address=SIGNER,
    )
    with pytest.raises(SignedTestnetBlocked, match='attested pre-order gate'):
        RISExSignedTestnetHTTPTransport(gate=forged_gate)
