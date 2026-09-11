from __future__ import annotations

from typing import Any

import httpx

from app.security.risex_pre_order_gate import (
    RISExPreOrderProbeGate,
    assert_pre_order_probe_gate_attested,
)
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


_TESTNET_BASE_URL = 'https://api.testnet.rise.trade'
_ALLOWED_POST_PATHS = frozenset({'/v1/orders/place'})
_FORBIDDEN_AUTH_HEADERS = frozenset(
    {
        'authorization',
        'proxy-authorization',
        'cookie',
        'x-api-key',
        'x-auth-token',
    }
)


def _reject_ambient_auth(client: httpx.AsyncClient) -> None:
    header_names = {name.lower() for name in client.headers.keys()}
    if header_names & _FORBIDDEN_AUTH_HEADERS or any(True for _cookie in client.cookies.jar):
        raise SignedTestnetBlocked(
            'RISEx signed testnet transport rejects JWT/OperatorHub or ambient authentication'
        )


class RISExSignedTestnetHTTPTransport:
    """Narrow transport for an attested permit-signed RISEx testnet order probe."""

    def __init__(
        self,
        *,
        gate: RISExPreOrderProbeGate,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        assert_pre_order_probe_gate_attested(gate)
        if client is not None:
            _reject_ambient_auth(client)
        self._gate = gate
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def post_json(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
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
