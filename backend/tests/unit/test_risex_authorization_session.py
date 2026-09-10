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
BLOCK_TIMESTAMP = 1_800_000_000
EXPIRATION = BLOCK_TIMESTAMP + 3_600
SESSION_KEYS_SELECTOR = '0x96ade1f9'


def _word(value: int) -> str:
    return '0x' + value.to_bytes(32, 'big').hex()


def _tuple_words(*values: int) -> str:
    return '0x' + ''.join(value.to_bytes(32, 'big').hex() for value in values)


class FakeRPC:
    public_read_only = True

    def __init__(
        self,
        *,
        status: int = 1,
        permissions: dict[int, bool] | None = None,
        expiration: int = EXPIRATION,
        permission_bitmap: int = 0x1234,
        stored_status: int = 1,
        block_timestamp: int = BLOCK_TIMESTAMP,
    ) -> None:
        self.status = status
        self.permissions = permissions or {1: False, 2: True, 3: False, 4: False}
        self.expiration = expiration
        self.permission_bitmap = permission_bitmap
        self.stored_status = stored_status
        self.block_timestamp = block_timestamp
        self.calls: list[tuple[str, list[object]]] = []

    async def call(self, method: str, params: list[object]) -> object:
        self.calls.append((method, params))
        if method == 'eth_getBlockByNumber':
            assert params == [BLOCK, False]
            return {'number': BLOCK, 'timestamp': hex(self.block_timestamp)}

        assert method == 'eth_call'
        call = params[0]
        assert isinstance(call, dict)
        assert call['to'] == AUTH
        assert params[1] == BLOCK
        data = str(call['data'])
        if data.startswith(SESSION_KEYS_SELECTOR):
            return _tuple_words(self.expiration, self.permission_bitmap, self.stored_status)
        if data.startswith('0xdd962cb2'):
            return _word(self.status)
        if data.startswith('0xed82f4b8'):
            permission_id = int(data[-64:], 16)
            return _word(1 if self.permissions.get(permission_id, False) else 0)
        raise AssertionError(data)


@pytest.mark.asyncio
async def test_collects_expiration_and_permission_matrix_at_one_block() -> None:
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
    assert evidence.stored_status_code == 1
    assert evidence.block_timestamp == BLOCK_TIMESTAMP
    assert evidence.session_expiration == EXPIRATION
    assert evidence.session_permission_bitmap == 0x1234
    assert evidence.session_not_expired is True
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

    eth_calls = [params for method, params in rpc.calls if method == 'eth_call']
    assert len(eth_calls) == 6
    assert all(params[1] == BLOCK for params in eth_calls)
    assert [params for method, params in rpc.calls if method == 'eth_getBlockByNumber'] == [
        [BLOCK, False]
    ]

    session_keys_data = [
        str(params[0]['data'])
        for method, params in rpc.calls
        if method == 'eth_call'
        and isinstance(params[0], dict)
        and str(params[0]['data']).startswith(SESSION_KEYS_SELECTOR)
    ]
    assert len(session_keys_data) == 1
    assert session_keys_data[0] == SESSION_KEYS_SELECTOR + ACCOUNT[2:].rjust(64, '0') + SIGNER[2:].rjust(64, '0')


@pytest.mark.asyncio
async def test_authorized_status_is_not_active_after_expiration() -> None:
    from app.security.risex_authorization_session import collect_authorization_session_evidence

    evidence = await collect_authorization_session_evidence(
        FakeRPC(status=1, expiration=BLOCK_TIMESTAMP),
        authorization_address=AUTH,
        account=ACCOUNT,
        signer=SIGNER,
        block_tag=BLOCK,
    )

    assert evidence.status_code == 1
    assert evidence.session_not_expired is False
    assert evidence.session_active is False


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
    assert evidence.session_not_expired is True
    assert evidence.session_active is None


@pytest.mark.asyncio
async def test_malformed_session_keys_tuple_fails_closed() -> None:
    from app.security.risex_authorization_session import collect_authorization_session_evidence

    class MalformedRPC(FakeRPC):
        async def call(self, method: str, params: list[object]) -> object:
            if method == 'eth_call':
                call = params[0]
                assert isinstance(call, dict)
                data = str(call['data'])
                if data.startswith(SESSION_KEYS_SELECTOR):
                    return _word(EXPIRATION)
            return await super().call(method, params)

    with pytest.raises(ProviderDataMalformed, match='session key'):
        await collect_authorization_session_evidence(
            MalformedRPC(),
            authorization_address=AUTH,
            account=ACCOUNT,
            signer=SIGNER,
            block_tag=BLOCK,
        )


@pytest.mark.asyncio
async def test_malformed_boolean_permission_fails_closed() -> None:
    from app.security.risex_authorization_session import collect_authorization_session_evidence

    class MalformedRPC(FakeRPC):
        async def call(self, method: str, params: list[object]) -> object:
            if method == 'eth_call':
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
async def test_runtime_rpc_allows_explicit_block_read_for_expiration_evidence() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__('json').loads(request.content.decode('utf-8'))
        requests.append(payload)
        result: object = {'number': BLOCK, 'timestamp': hex(BLOCK_TIMESTAMP)}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload['id'], 'result': result})

    async with RISExReadOnlyRPCTransport(
        rpc_url='https://rpc.example.test',
        transport=httpx.MockTransport(handler),
    ) as rpc:
        result = await rpc.call('eth_getBlockByNumber', [BLOCK, False])

    assert result == {'number': BLOCK, 'timestamp': hex(BLOCK_TIMESTAMP)}
    assert requests[0]['method'] == 'eth_getBlockByNumber'
