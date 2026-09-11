from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import time
from typing import Any

import httpx

from app.security.risex_place_order_request import (
    RISExPreparedPlaceOrderRequest,
    prepare_place_order_request,
)
from app.security.risex_pre_order_gate import (
    RISExPreOrderProbeGate,
    assert_pre_order_probe_gate_attested,
)
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


_TESTNET_BASE_URL = 'https://api.testnet.rise.trade'
_ALLOWED_POST_PATHS = frozenset({'/v1/orders/place'})
_SAFE_CLIENT_HEADERS = {'Accept': 'application/json'}
_FORBIDDEN_AUTH_HEADERS = frozenset(
    {
        'authorization',
        'proxy-authorization',
        'cookie',
        'x-api-key',
        'x-auth-token',
    }
)
RISExPreOrderFreshnessProbe = Callable[[], Awaitable[RISExSignerCapabilityEvidence]]


def _reject_ambient_auth(client: httpx.AsyncClient) -> None:
    header_names = {name.lower() for name in client.headers.keys()}
    has_request_hooks = bool(client.event_hooks.get('request'))
    if (
        client.auth is not None
        or client.trust_env
        or has_request_hooks
        or header_names & _FORBIDDEN_AUTH_HEADERS
        or any(True for _cookie in client.cookies.jar)
    ):
        raise SignedTestnetBlocked(
            'RISEx signed testnet transport rejects JWT/OperatorHub or ambient authentication'
        )


def _assert_permit_identity_bound(
    gate: RISExPreOrderProbeGate,
    payload: dict[str, Any],
) -> None:
    permit = payload.get('permit')
    if not isinstance(permit, dict):
        raise SignedTestnetBlocked(
            'RISEx signed permit identity must match the attested pre-order gate'
        )

    account = permit.get('account')
    signer = permit.get('signer')
    if not isinstance(account, str) or not isinstance(signer, str):
        raise SignedTestnetBlocked(
            'RISEx signed permit identity must match the attested pre-order gate'
        )

    if (
        account.lower() != gate.account_address.lower()
        or signer.lower() != gate.signer_address.lower()
    ):
        raise SignedTestnetBlocked(
            'RISEx signed permit identity must match the attested pre-order gate'
        )


class RISExSignedTestnetHTTPTransport:
    """Narrow transport for one attested, typed RISEx testnet Perps order probe."""

    def __init__(
        self,
        *,
        gate: RISExPreOrderProbeGate,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        freshness_probe: RISExPreOrderFreshnessProbe | None = None,
    ) -> None:
        assert_pre_order_probe_gate_attested(gate)
        if client is not None:
            _reject_ambient_auth(client)
        self._gate = gate
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
            auth=None,
            headers=_SAFE_CLIENT_HEADERS,
        )
        self._owns_client = client is None
        self._freshness_probe = freshness_probe

    async def post_place_order(
        self,
        request: RISExPreparedPlaceOrderRequest,
    ) -> dict[str, Any]:
        if not isinstance(request, RISExPreparedPlaceOrderRequest):
            raise SignedTestnetBlocked(
                'RISEx signed transport requires a typed place-order request'
            )

        validated = prepare_place_order_request(
            order=request.order,
            permit=request.permit,
        )
        payload = validated.json_for_testnet_transport()
        _assert_permit_identity_bound(self._gate, payload)

        if self._freshness_probe is None:
            raise SignedTestnetBlocked(
                'RISEx signed transport requires fresh session and deployment evidence before POST'
            )
        current_evidence = await self._freshness_probe()
        assert_pre_order_probe_gate_attested(
            self._gate,
            now=int(time()),
            evidence=current_evidence,
        )
        _reject_ambient_auth(self._client)

        return await self._post_json('/v1/orders/place', json=payload)

    async def _post_json(
        self,
        path: str,
        *,
        json: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_path = '/' + path.lstrip('/')
        if normalized_path not in _ALLOWED_POST_PATHS:
            raise SignedTestnetBlocked('RISEx POST is not an approved signed-testnet endpoint')

        try:
            response = await self._client.post(
                f'{_TESTNET_BASE_URL}{normalized_path}',
                json=json,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SignedTestnetBlocked(
                f'RISEx signed testnet POST failed at {normalized_path}: {type(exc).__name__}'
            ) from exc

        if not isinstance(payload, dict):
            raise SignedTestnetBlocked(
                f'RISEx signed testnet POST at {normalized_path} did not return a JSON object'
            )
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> 'RISExSignedTestnetHTTPTransport':
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.aclose()
