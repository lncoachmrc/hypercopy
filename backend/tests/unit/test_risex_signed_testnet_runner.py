from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.security.risex_deployment_preflight import canonical_deployment_fingerprint
from app.security.risex_deployment_runtime import collect_runtime_deployment_evidence


CHAIN_ID = 11155931
BLOCK = '0x1234'
AUTH = '0x' + ('aa' * 20)
AUTH_IMPL = '0x' + ('ab' * 20)
ROUTER = '0x' + ('bb' * 20)
ROUTER_IMPL = '0x' + ('bc' * 20)
ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)


def _word(value: int) -> str:
    return '0x' + value.to_bytes(32, 'big').hex()


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

    def __init__(self, *, status: int = 1, perps: bool = True) -> None:
        self.status = status
        self.perps = perps
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
        if method == 'eth_call':
            call = params[0]
            assert isinstance(call, dict)
            assert call['to'] == AUTH
            data = str(call['data'])
            if data.startswith('0xdd962cb2'):
                return _word(self.status)
            if data.startswith('0xed82f4b8'):
                return _word(1 if self.perps else 0)
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


@pytest.mark.asyncio
async def test_runner_collects_same_block_signer_evidence_and_blocks_post_until_scope_is_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security import risex_signed_testnet_runner as runner

    monkeypatch.setattr(
        runner,
        'load_testnet_signer_credential',
        lambda _env: SimpleNamespace(account_address=ACCOUNT, signer_address=SIGNER),
    )

    rpc = FakeRPC(status=1, perps=True)
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

    assert report.deployment_verdict == 'PASS'
    assert report.deployment_identity_verified is True
    assert report.block_tag == BLOCK
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
    assert eth_call_blocks == [BLOCK, BLOCK]
