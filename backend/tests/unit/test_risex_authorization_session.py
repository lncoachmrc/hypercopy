from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.risex_types import ProviderDataMalformed
from app.security.risex_deployment_runtime import RISExReadOnlyRPCTransport


AUTH = '0x' + ('aa' * 20)
ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
BLOCK = '0x1234'


def _word(value: int) -> str:
    return '0x' + value.to_bytes(32, 'big').hex()


class FakeRPC:
    public_read_only = True

    def __init__(self, *, status: int = 1, perps: bool = True) -> None:
        self.status = status
        self.perps = perps
        self.calls: list[tuple[str, list[object]]] = []

    async def call(self, method: str, params: list[object]) -> object:
        self.calls.append((method, params))
        assert method == 'eth_call'
        call = params[0]
        assert isinstance(call, dict)
        assert call['to'] == AUTH
        assert params[1] == BLOCK
        data = str(call['data'])
        if data.startswith('0xdd962cb2'):
            return _word(self.status)
        if data.startswith('0xed82f4b8'):
            return _word(1 if self.perps else 0)
        raise AssertionError(data)


@pytest.mark.asyncio
async def test_collects_authorized_perps_permission_without_claiming_perps_only() -> None:
    from app.security.risex_authorization_session import collect_authorization_session_evidence

    rpc = FakeRPC()
    evidence = await collect_authorization_session_evidence(
        rpc,
        authorization_address=AUTH,
        account=ACCOUNT,
        signer=SIGNER,
        block_tag=BLOCK,
    )

    assert evidence.status_code == 1
    assert evidence.session_active is True
    assert evidence.perps_permission is True
    assert evidence.perps_permission_id == 2
    assert evidence.perps_only_scope is None
    assert [method for method, _params in rpc.calls] == ['eth_call', 'eth_call']

    status_data = str(rpc.calls[0][1][0]['data'])  # type: ignore[index]
    permission_data = str(rpc.calls[1][1][0]['data'])  # type: ignore[index]
    assert status_data.startswith('0xdd962cb2')
    assert permission_data.startswith('0xed82f4b8')
    assert permission_data.endswith((2).to_bytes(32, 'big').hex())


@pytest.mark.asyncio
async def test_unknown_status_code_is_not_treated_as_active() -> None:
    from app.security.risex_authorization_session import collect_authorization_session_evidence

    evidence = await collect_authorization_session_evidence(
        FakeRPC(status=9),
        authorization_address=AUTH,
        account=ACCOUNT,
        signer=SIGNER,
        block_tag=BLOCK,
    )
    assert evidence.status_code == 9
    assert evidence.session_active is None


@pytest.mark.asyncio
async def test_malformed_boolean_permission_fails_closed() -> None:
    from app.security.risex_authorization_session import collect_authorization_session_evidence

    class MalformedRPC(FakeRPC):
        async def call(self, method: str, params: list[object]) -> object:
            call = params[0]
            assert isinstance(call, dict)
            if str(call['data']).startswith('0xed82f4b8'):
                return _word(2)
            return await super().call(method, params)

    with pytest.raises(ProviderDataMalformed, match='boolean'):
        await collect_authorization_session_evidence(
            MalformedRPC(),
            authorization_address=AUTH,
            account=ACCOUNT,
            signer=SIGNER,
            block_tag=BLOCK,
        )


@pytest.mark.asyncio
async def test_runtime_rpc_allows_eth_call_as_read_only_evidence() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(__import__('json').loads(request.content.decode('utf-8')))
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': _word(1)})

    async with RISExReadOnlyRPCTransport(
        rpc_url='https://rpc.example.test',
        transport=httpx.MockTransport(handler),
    ) as rpc:
        result = await rpc.call('eth_call', [{'to': AUTH, 'data': '0x'}, BLOCK])

    assert result == _word(1)
    assert requests[0]['method'] == 'eth_call'
