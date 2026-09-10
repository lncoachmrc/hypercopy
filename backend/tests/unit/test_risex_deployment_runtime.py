from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.risex_types import ProviderReadUnavailable, ProviderWriteDisabled
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)


AUTH = '0x' + '11' * 20
ROUTER = '0x' + '22' * 20
AUTH_IMPL = '0x' + '33' * 20
ROUTER_IMPL = '0x' + '44' * 20
BLOCK = '0x1234'


def _slot(address: str) -> str:
    return '0x' + ('00' * 12) + address[2:]


class FakePublicAPI:
    def __init__(self, *, public_read_only: bool = True) -> None:
        self.public_read_only = public_read_only
        self.calls: list[str] = []

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del params
        self.calls.append(path)
        if path == '/v1/auth/eip712-domain':
            return {
                'data': {
                    'name': 'RISEx',
                    'version': '1',
                    'chain_id': 11155931,
                    'verifying_contract': AUTH,
                }
            }
        if path == '/v1/system/config':
            return {'data': {'addresses': {'auth': AUTH, 'router': ROUTER}}}
        raise AssertionError(path)


class FakeRPC:
    def __init__(self, *, public_read_only: bool = True) -> None:
        self.public_read_only = public_read_only
        self.calls: list[tuple[str, list[object]]] = []

    async def call(self, method: str, params: list[object]) -> object:
        self.calls.append((method, params))
        if method == 'eth_chainId':
            return hex(11155931)
        if method == 'eth_blockNumber':
            return BLOCK
        if method == 'eth_getStorageAt':
            address = str(params[0]).lower()
            if address == AUTH.lower():
                return _slot(AUTH_IMPL)
            if address == ROUTER.lower():
                return _slot(ROUTER_IMPL)
        if method == 'eth_getCode':
            address = str(params[0]).lower()
            code = {
                AUTH.lower(): '0x60016000',
                ROUTER.lower(): '0x60026000',
                AUTH_IMPL.lower(): '0x60036000',
                ROUTER_IMPL.lower(): '0x60046000',
            }.get(address)
            if code is not None:
                return code
        raise AssertionError((method, params))


@pytest.mark.asyncio
async def test_collector_pins_api_and_rpc_evidence_to_one_observed_block() -> None:
    api = FakePublicAPI()
    rpc = FakeRPC()

    evidence = await collect_runtime_deployment_evidence(
        api,
        rpc,
        network='testnet',
    )

    assert evidence.api_chain_id == 11155931
    assert evidence.rpc_chain_id == 11155931
    assert evidence.block_number == int(BLOCK, 16)
    assert evidence.domain_name == 'RISEx'
    assert evidence.domain_version == '1'
    assert evidence.domain_verifying_contract == AUTH
    assert evidence.system_auth_contract == AUTH
    assert evidence.system_router == ROUTER
    assert evidence.auth.address == AUTH
    assert evidence.auth.implementation == AUTH_IMPL
    assert evidence.auth.runtime_code_bytes == 4
    assert evidence.auth.implementation_code_bytes == 4
    assert evidence.auth.abi_verified is None
    assert evidence.router.address == ROUTER
    assert evidence.router.implementation == ROUTER_IMPL
    assert evidence.router.runtime_code_bytes == 4
    assert evidence.router.implementation_code_bytes == 4
    assert evidence.router.abi_verified is None

    assert api.calls == ['/v1/auth/eip712-domain', '/v1/system/config']
    assert {method for method, _params in rpc.calls} <= {
        'eth_chainId',
        'eth_blockNumber',
        'eth_getCode',
        'eth_getStorageAt',
    }
    for method, params in rpc.calls:
        if method in {'eth_getCode', 'eth_getStorageAt'}:
            assert params[-1] == BLOCK


@pytest.mark.asyncio
async def test_collector_rejects_non_public_api_transport_before_io() -> None:
    api = FakePublicAPI(public_read_only=False)
    rpc = FakeRPC()

    with pytest.raises(ProviderReadUnavailable, match='public read-only'):
        await collect_runtime_deployment_evidence(api, rpc, network='testnet')

    assert api.calls == []
    assert rpc.calls == []


@pytest.mark.asyncio
async def test_collector_rejects_non_read_only_rpc_before_io() -> None:
    api = FakePublicAPI()
    rpc = FakeRPC(public_read_only=False)

    with pytest.raises(ProviderReadUnavailable, match='read-only RPC'):
        await collect_runtime_deployment_evidence(api, rpc, network='testnet')

    assert api.calls == []
    assert rpc.calls == []


@pytest.mark.asyncio
async def test_rpc_transport_blocks_transaction_methods_before_network_io() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': '0x1'})

    transport = httpx.MockTransport(handler)
    async with RISExReadOnlyRPCTransport(
        rpc_url='https://rpc.example.test',
        transport=transport,
    ) as rpc:
        with pytest.raises(ProviderWriteDisabled, match='read-only'):
            await rpc.call('eth_sendRawTransaction', ['0xdeadbeef'])

    assert calls == 0


@pytest.mark.asyncio
async def test_rpc_transport_allows_only_explicit_deployment_read_methods() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(__import__('json').loads(request.content.decode('utf-8')))
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': '0x1'})

    async with RISExReadOnlyRPCTransport(
        rpc_url='https://rpc.example.test',
        transport=httpx.MockTransport(handler),
    ) as rpc:
        result = await rpc.call('eth_chainId', [])

    assert result == '0x1'
    assert requests == [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []}
    ]


def test_reviewed_testnet_pin_matches_retained_runtime_evidence() -> None:
    assert PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT == (
        '764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f'
    )
