from __future__ import annotations

from pathlib import Path
import subprocess
import sys
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
        permission_bitmap: int = 0xFFFFFFFF,
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
    status: int = 1,
    permission_bitmap: int = 0xFFFFFFFF,
    fund_movement_path_absent: bool | None = None,
) -> Any:
    from app.security import risex_signed_testnet_runner as runner

    _patch_credential_loader(monkeypatch, runner)
    rpc = FakeRPC(
        status=status,
        permissions=permissions,
        expiration=expiration,
        permission_bitmap=permission_bitmap,
    )
    kwargs: dict[str, Any] = {
        'env': {
            'RISEX_TESTNET_ACCOUNT_ADDRESS': ACCOUNT,
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': 'injected-test-secret',
        },
        'api': FakeAPI(),
        'rpc': rpc,
        'network': 'testnet',
        'explicit_approval': True,
        'disposable_account_asserted': True,
        'dedicated_signer_asserted': True,
        'operatorhub_bypass_disabled': True,
        'expected_fingerprint': await _expected_fingerprint(),
    }
    if fund_movement_path_absent is not None:
        kwargs['fund_movement_path_absent'] = fund_movement_path_absent
    result = await runner.run_signed_testnet_readiness(**kwargs)
    return result.report, rpc


def _assert_writes_stay_disabled(report: Any) -> None:
    assert report.post_allowed is False
    assert report.full_security_gate_passed is False
    assert report.writes_enabled is False


@pytest.mark.asyncio
async def test_runner_passes_with_perps_permission_and_explicit_adr0002_fund_path_assumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _rpc = await _run(
        monkeypatch,
        permissions={1: True, 2: True, 3: True, 4: True},
        fund_movement_path_absent=True,
    )

    assert report.session_active is True
    assert report.perps_permission is True
    assert report.move_fund_permission is True
    assert report.perps_only_scope is False
    assert report.fund_movement_path_absent is True
    assert report.adr_reference == 'ADR-0002'
    assert report.verdict == 'PASS'
    _assert_writes_stay_disabled(report)


@pytest.mark.asyncio
async def test_runner_defaults_to_unknown_when_fund_movement_path_absence_is_not_asserted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _rpc = await _run(
        monkeypatch,
        permissions={1: True, 2: True, 3: True, 4: True},
    )

    assert report.session_active is True
    assert report.perps_permission is True
    assert report.move_fund_permission is True
    assert report.fund_movement_path_absent is False
    assert report.adr_reference == 'ADR-0002'
    assert report.verdict == 'UNKNOWN'
    _assert_writes_stay_disabled(report)


@pytest.mark.asyncio
async def test_runner_fails_without_perps_permission_regardless_of_fund_path_assumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _rpc = await _run(
        monkeypatch,
        permissions={1: True, 2: False, 3: True, 4: True},
        fund_movement_path_absent=True,
    )

    assert report.perps_permission is False
    assert report.fund_movement_path_absent is True
    assert report.adr_reference == 'ADR-0002'
    assert report.verdict == 'FAIL'
    _assert_writes_stay_disabled(report)


@pytest.mark.asyncio
async def test_runner_fails_when_session_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _rpc = await _run(
        monkeypatch,
        permissions={1: True, 2: True, 3: True, 4: True},
        status=0,
        fund_movement_path_absent=True,
    )

    assert report.session_active is False
    assert report.fund_movement_path_absent is True
    assert report.adr_reference == 'ADR-0002'
    assert report.verdict == 'FAIL'
    _assert_writes_stay_disabled(report)


@pytest.mark.asyncio
async def test_report_exposes_all_authorization_permissions_and_adr_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, rpc = await _run(
        monkeypatch,
        permissions={1: True, 2: True, 3: True, 4: True},
        permission_bitmap=0xFFFFFFFF,
    )

    assert report.all_permission_id == 1
    assert report.all_permission is True
    assert report.perps_permission_id == 2
    assert report.perps_permission is True
    assert report.spot_permission_id == 3
    assert report.spot_permission is True
    assert report.move_fund_permission_id == 4
    assert report.move_fund_permission is True
    assert report.session_permission_bitmap == 0xFFFFFFFF
    assert report.session_permission_bitmap_hex == '0xFFFFFFFF'
    assert report.fund_movement_path_absent is False
    assert report.adr_reference == 'ADR-0002'
    assert report.perps_only_scope is False
    _assert_writes_stay_disabled(report)

    eth_call_blocks = [
        str(params[1])
        for method, params in rpc.calls
        if method == 'eth_call'
    ]
    assert eth_call_blocks == [BLOCK] * 6


@pytest.mark.asyncio
async def test_runner_still_fails_when_authorized_signer_is_expired_at_observed_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _rpc = await _run(
        monkeypatch,
        permissions={1: False, 2: True, 3: False, 4: False},
        expiration=BLOCK_TIMESTAMP,
        fund_movement_path_absent=True,
    )

    assert report.session_not_expired is False
    assert report.session_active is False
    assert report.verdict == 'FAIL'
    _assert_writes_stay_disabled(report)


def test_readiness_script_help_exposes_adr0002_fund_movement_opt_in_flag() -> None:
    script = Path(__file__).resolve().parents[3] / 'scripts' / 'risex_signed_testnet_readiness.py'
    result = subprocess.run(
        [sys.executable, str(script), '--help'],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert '--fund-movement-path-absent' in result.stdout
    assert 'ADR-0002' in result.stdout
