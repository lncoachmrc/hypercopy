from __future__ import annotations

import httpx
import pytest

from app.adapters.risex_types import ProviderReadUnavailable, ProviderWriteDisabled


@pytest.mark.asyncio
async def test_risex_http_transport_gets_json_with_query_params() -> None:
    from app.adapters.risex_http import RISExReadOnlyHTTPTransport

    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={'data': {'active': True}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = RISExReadOnlyHTTPTransport(
            base_url='https://api.testnet.rise.trade',
            client=client,
        )
        payload = await transport.get_json(
            '/v1/auth/session-key-status',
            params={'account': '0xabc', 'signer': '0xdef'},
        )

    assert payload == {'data': {'active': True}}
    assert len(seen) == 1
    assert seen[0].method == 'GET'
    assert str(seen[0].url).startswith(
        'https://api.testnet.rise.trade/v1/auth/session-key-status?'
    )


@pytest.mark.asyncio
async def test_risex_http_transport_blocks_post_before_network_io() -> None:
    from app.adapters.risex_http import RISExReadOnlyHTTPTransport

    network_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = RISExReadOnlyHTTPTransport(
            base_url='https://api.testnet.rise.trade',
            client=client,
        )
        with pytest.raises(ProviderWriteDisabled, match='read-only'):
            await transport.post_json('/v1/orders/place', json={'anything': 'blocked'})

    assert network_calls == 0


@pytest.mark.asyncio
async def test_risex_http_transport_rejects_non_object_json() -> None:
    from app.adapters.risex_http import RISExReadOnlyHTTPTransport

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=['unexpected'])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = RISExReadOnlyHTTPTransport(
            base_url='https://api.testnet.rise.trade',
            client=client,
        )
        with pytest.raises(ProviderReadUnavailable, match='JSON object'):
            await transport.get_json('/v1/system/config')
