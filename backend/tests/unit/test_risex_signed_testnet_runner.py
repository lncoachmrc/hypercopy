from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.security.risex_deployment_preflight import canonical_deployment_fingerprint
from app.security.risex_deployment_runtime import collect_runtime_deployment_evidence


CHAIN_ID = 11155931
BLOCK = '0x1234'
BLOCK_TIMESTAMP = 1_800_000_000
EXPIRATION = BLOCK_TIMESTAMP + 3_600
AUTH = '0x' + ('aa' * 20)
AUTH_IMPL = '0x' + ('ab' * 20)
ROUTER = '0x' + ('bb' * 20)
ROUTER_IMPL = '0x' + ('bc' * 20)
ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
SESSION_KEYS_SELECTOR = '0x96ade1f9'


def _word(value: int) -> str:
    return '0x' + value.to_bytes(32, 'big').hex()


def _tuple_words(*values: int) -> str:
    return '0x' + ''.join(value.to_bytes(32, 'big').hex() for value in values)


def _implementation_slot(address: str) -> str:
    return '0x' + ('00' * 12) + address[2:]


class FakeAPI:
    public_read_only = True

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del params
        if path == '/v1/auth/eip712-domain':
            return {
                'name': 'RISEx',
                'version': '1',
                'chainId': CHAIN_ID,
                'verifyingContract': AUTH,
            }
        if path == '/v1/system/config':
            return {'addresses': {'auth': AUTH, 'router': ROUTER}}
        raise AssertionError(path)


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
        if method == 'eth_chainId':
            return hex(CHAIN_ID)
        if method == 'eth_blockNumber':
            return BLOCK
        if method == 'eth_getCode':
            address = str(params[0]).lower()
            code_by_address = {
                AUTH.lower(): '0x6001',
                AUTH_IMPL.lower(): '0x6002',
                ROUTER.lower(): '0x6003',
                ROUTER_IMPL.lower(): '0x6004',
            }
            return code_by_address[address]
        if method == 'eth_getStorageAt':
            address = str(params[0]).lower()
            if address == AUTH.lower():
                return _implementation_slot(AUTH_IMPL)
            if address == ROUTER.lower():
                return _implementation_slot(ROUTER_IMPL)
            raise AssertionError(address)
        if method == 'eth_getBlockByNumber':
            assert params == [BLOCK, False]
            return {'number': BLOCK, 'timestamp': hex(self.block_timestamp)}
        if method == 'eth_call':
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
        raise AssertionError(method)


async def _expected_fingerprint() -> str:
    evidence = await collect_runtime_deployment_evidence(
        FakeAPI(),
        FakeRPC(),
        network='testnet',
    )
    fingerprint = canonical_deployment_fingerprint(evidence)
    assert fingerprint is not None
    return fingerprint


def _patch_credential_loader(monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    monkeypatch.setattr(
        runner,
        'load_testnet_signer_credential',
        lambda _env: SimpleNamespace(account_address=ACCOUNT, signer_address=SIGNER),
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    permissions: dict[int, bool],
    expiration: int = EXPIRATION,
) -> Any:
    from app.security import risex_signed_testnet_runner as runner

    _patch_credential_loader(monkeypatch, runner)
    rpc = FakeRPC(status=1, permissions=permissions, expiration=expiration)
    report = await runner.run_signed_testnet_readiness(
        env={
            'RISEX_TESTNET_ACCOUNT_ADDRESS': ACCOUNT,
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': 'injected-test-secret',
        },
        api=FakeAPI(),
        rpc=rpc,
        network='testnet',
        explicit_approval=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
        expected_fingerprint=await _expected_fingerprint(),
    )
    return report, rpc


@pytest.mark.asyncio
async def test_runner_collects_same_block_expiration_evidence_and_blocks_post_until_scope_is_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, rpc = await _run(
        monkeypatch,
        permissions={1: False, 2: True, 3: False, 4: False},
    )

    assert report.deployment_verdict == 'PASS'
    assert report.deployment_identity_verified is True
    assert report.block_tag == BLOCK
    assert report.observed_block_timestamp == BLOCK_TIMESTAMP
    assert report.session_expiration == EXPIRATION
    assert report.session_permission_bitmap == 0x1234
    assert report.stored_session_status_code == 1
    assert report.session_not_expired is True
    assert report.session_active is True
    assert report.perps_permission_id == 2
    assert report.perps_permission is True
    assert report.perps_only_scope is None
    assert report.verdict == 'UNKNOWN'
    assert report.post_allowed is False
    assert report.full_security_gate_passed is False
    assert report.writes_enabled is False
    assert 'injected-test-secret' not in repr(report)

    eth_call_blocks = [
        str(params[1])
        for method, params in rpc.calls
        if method == 'eth_call'
    ]
    assert eth_call_blocks == [BLOCK] * 6
    assert [params for method, params in rpc.calls if method == 'eth_getBlockByNumber'] == [
        [BLOCK, False]
    ]


@pytest.mark.asyncio
async def test_runner_fails_when_authorized_signer_is_expired_at_observed_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _rpc = await _run(
        monkeypatch,
        permissions={1: False, 2: True, 3: False, 4: False},
        expiration=BLOCK_TIMESTAMP,
    )

    assert report.session_not_expired is False
    assert report.session_active is False
    assert report.verdict == 'FAIL'
    assert report.post_allowed is False
    assert report.full_security_gate_passed is False
    assert report.writes_enabled is False


@pytest.mark.asyncio
async def test_runner_fails_when_signer_still_has_default_all_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _rpc = await _run(
        monkeypatch,
        permissions={1: True, 2: True, 3: False, 4: False},
    )

    assert report.perps_only_scope is False
    assert report.verdict == 'FAIL'
    assert report.post_allowed is False
    assert report.full_security_gate_passed is False
    assert report.writes_enabled is False
