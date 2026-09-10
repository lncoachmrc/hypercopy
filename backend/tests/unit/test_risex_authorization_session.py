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

    def __init__(
        self,
        *,
        status: int = 1,
        permissions: dict[int, bool] | None = None,
    ) -> None:
        self.status = status
        self.permissions = permissions or {1: False, 2: True, 3: False, 4: False}
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
            permission_id = int(data[-64:], 16)
            return _word(1 if self.permissions.get(permission_id, False) else 0)
        raise AssertionError(data)


@pytest.mark.asyncio
async def test_collects_full_permission_matrix_without_claiming_final_perps_only() -> None:
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
    assert evidence.all_permission_id == 1
    assert evidence.all_permission is False
    assert evidence.perps_permission_id == 2
    assert evidence.perps_permission is True
    assert evidence.spot_permission_id == 3
    assert evidence.spot_permission is False
    assert evidence.move_fund_permission_id == 4
    assert evidence.move_fund_permission is False
    assert evidence.perps_only_scope is None
    assert [method for method, _params in rpc.calls] == ['eth_call'] * 5

    status_data = str(rpc.calls[0][1][0]['data'])  # type: ignore[index]
    assert status_data.startswith('0xdd962cb2')
    for index, permission_id in enumerate((1, 2, 3, 4), start=1):
        permission_data = str(rpc.calls[index][1][0]['data'])  # type: ignore[index]
        assert permission_data.startswith('0xed82f4b8')
        assert permission_data.endswith(permission_id.to_bytes(32, 'big').hex())
        assert rpc.calls[index][1][1] == BLOCK


@pytest.mark.asyncio
@pytest.mark.parametrize('dangerous_permission_id', [1, 3, 4])
async def test_broader_permissions_are_definitively_not_perps_only(
    dangerous_permission_id: int,
) -> None:
    from app.security.risex_authorization_session import collect_authorization_session_evidence

    permissions = {1: False, 2: True, 3: False, 4: False}
    permissions[dangerous_permission_id] = True
    evidence = await collect_authorization_session_evidence(
        FakeRPC(permissions=permissions),
        authorization_address=AUTH,
        account=ACCOUNT,
        signer=SIGNER,
        block_tag=BLOCK,
    )

    assert evidence.perps_permission is True
    assert evidence.perps_only_scope is False


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
            data = str(call['data'])
            if data.startswith('0xed82f4b8') and int(data[-64:], 16) == 4:
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
