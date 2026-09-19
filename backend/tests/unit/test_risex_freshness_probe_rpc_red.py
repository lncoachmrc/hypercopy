from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.security import risex_pre_order_gate as gate_module
from app.security.risex_authorization_session import (
    GET_SESSION_KEY_STATUS_SELECTOR,
    HAS_PERMISSION_SELECTOR,
    SESSION_KEYS_SELECTOR,
)


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)
CHAIN_ID = 11155931
BLOCK_1 = 0x1234
BLOCK_2 = 0x1235
TIMESTAMP_1 = 1_900_000_000
TIMESTAMP_2 = TIMESTAMP_1 + 12
EXPIRATION = TIMESTAMP_2 + 3600


def _word(value: int) -> str:
    return '0x' + value.to_bytes(32, 'big').hex()


def _tuple_words(*values: int) -> str:
    return '0x' + ''.join(value.to_bytes(32, 'big').hex() for value in values)


class FakeAuthorizationRPC:
    public_read_only = True

    def __init__(
        self,
        *,
        status: int = 1,
        expiration: int = EXPIRATION,
    ) -> None:
        self.status = status
        self.expiration = expiration
        self.calls: list[tuple[str, list[object]]] = []

    async def call(self, method: str, params: list[object]) -> object:
        self.calls.append((method, params))

        if method == 'eth_getBlockByNumber':
            block_tag = str(params[0])
            block_number = int(block_tag, 16)
            timestamp = TIMESTAMP_1 if block_number == BLOCK_1 else TIMESTAMP_2
            return {'number': block_tag, 'timestamp': hex(timestamp)}

        assert method == 'eth_call'
        call = params[0]
        assert isinstance(call, dict)
        assert call['to'] == AUTH
        data = str(call['data'])

        if data.startswith(SESSION_KEYS_SELECTOR):
            return _tuple_words(self.expiration, 0xFFFFFFFF, self.status)
        if data.startswith(GET_SESSION_KEY_STATUS_SELECTOR):
            return _word(self.status)
        if data.startswith(HAS_PERMISSION_SELECTOR):
            permission_id = int(data[-64:], 16)
            return _word(1 if permission_id == 2 else 0)

        raise AssertionError(data)


def _runtime_collector(blocks: list[int], calls: list[int]):
    block_iter = iter(blocks)

    async def collect(*_args: object, **_kwargs: object) -> SimpleNamespace:
        block = next(block_iter)
        calls.append(block)
        return SimpleNamespace(
            block_number=block,
            api_chain_id=CHAIN_ID,
            rpc_chain_id=CHAIN_ID,
            domain_name='RISEx',
            domain_version='1',
            domain_verifying_contract=AUTH,
            system_auth_contract=AUTH,
            system_router=ROUTER,
        )

    return collect


@pytest.mark.asyncio
async def test_freshness_probe_reads_new_block_and_active_session_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import risex_order_preparation as module

    runtime_calls: list[int] = []
    monkeypatch.setattr(
        module,
        'collect_runtime_deployment_evidence',
        _runtime_collector([BLOCK_1, BLOCK_2], runtime_calls),
    )

    async def forbidden_public_probe(*_args: object, **_kwargs: object) -> object:
        pytest.fail('freshness probe must not use collect_public_signer_evidence')

    if hasattr(module, 'collect_public_signer_evidence'):
        monkeypatch.setattr(module, 'collect_public_signer_evidence', forbidden_public_probe)

    rpc = FakeAuthorizationRPC(status=1)
    probe = module.make_freshness_probe(
        api=object(),
        rpc=rpc,
        account_address=ACCOUNT,
        signer_address=SIGNER,
        operatorhub_bypass_disabled=True,
        fund_movement_path_absent=True,
    )

    first = await probe()
    second = await probe()

    assert runtime_calls == [BLOCK_1, BLOCK_2]
    assert first.session_active is True
    assert first.session_account == ACCOUNT
    assert first.session_expiration == EXPIRATION
    assert first.chain_id == CHAIN_ID
    assert first.auth_contract == AUTH
    assert first.router == ROUTER
    assert second.session_active is True
    assert second.session_account == ACCOUNT

    observed_blocks = [
        params[0]
        for method, params in rpc.calls
        if method == 'eth_getBlockByNumber'
    ]
    assert observed_blocks == [hex(BLOCK_1), hex(BLOCK_2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('status', 'expiration'),
    [
        (0, EXPIRATION),
        (1, TIMESTAMP_1),
    ],
)
async def test_freshness_probe_returns_inactive_for_revoked_or_expired_session(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expiration: int,
) -> None:
    from app.services import risex_order_preparation as module

    monkeypatch.setattr(
        module,
        'collect_runtime_deployment_evidence',
        _runtime_collector([BLOCK_1], []),
    )
    rpc = FakeAuthorizationRPC(status=status, expiration=expiration)

    probe = module.make_freshness_probe(
        api=object(),
        rpc=rpc,
        account_address=ACCOUNT,
        signer_address=SIGNER,
        operatorhub_bypass_disabled=True,
        fund_movement_path_absent=True,
    )

    evidence = await probe()

    assert evidence.session_active is False
    assert evidence.session_account == ACCOUNT


@pytest.mark.asyncio
async def test_pre_order_gate_accepts_corrected_rpc_freshness_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import risex_order_preparation as module

    monkeypatch.setattr(
        module,
        'collect_runtime_deployment_evidence',
        _runtime_collector([BLOCK_1], []),
    )
    rpc = FakeAuthorizationRPC(status=1)
    probe = module.make_freshness_probe(
        api=object(),
        rpc=rpc,
        account_address=ACCOUNT,
        signer_address=SIGNER,
        operatorhub_bypass_disabled=True,
        fund_movement_path_absent=True,
    )
    evidence = await probe()

    monkeypatch.setattr(
        gate_module,
        'assert_replay_protection_architecture_attested',
        lambda *_args, **_kwargs: None,
    )
    gate = gate_module.RISExPreOrderProbeGate(
        account_address=ACCOUNT,
        signer_address=SIGNER,
        session_expiration=EXPIRATION,
        deployment_chain_id=CHAIN_ID,
        deployment_auth_contract=AUTH,
        deployment_router=ROUTER,
        _replay_protection_architecture_attestation=object(),
        _attestation_seal=gate_module._ATTESTATION_SEAL,
    )

    gate_module.assert_pre_order_probe_gate_attested(
        gate,
        now=TIMESTAMP_1,
        evidence=evidence,
    )
