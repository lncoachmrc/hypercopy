from __future__ import annotations

import pytest

from app.adapters.risex_types import ProviderReadUnavailable


class FakeTransport:
    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.get_calls: list[tuple[str, dict | None]] = []
        self.post_calls: list[tuple[str, dict | None]] = []

    async def get_json(self, path: str, *, params: dict | None = None) -> dict:
        self.get_calls.append((path, params))
        response = self.responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    async def post_json(self, path: str, *, json: dict | None = None) -> dict:
        self.post_calls.append((path, json))
        raise AssertionError('signer evidence collection must never POST')


@pytest.mark.asyncio
async def test_collect_public_signer_evidence_uses_get_only_and_stays_unknown() -> None:
    from app.security.risex_signer_probe import collect_public_signer_evidence, evaluate_signer_capabilities

    account = '0x1111111111111111111111111111111111111111'
    signer = '0x2222222222222222222222222222222222222222'
    transport = FakeTransport({
        '/v1/auth/eip712-domain': {
            'data': {
                'name': 'RISEx Auth',
                'version': '1',
                'chain_id': 11155931,
                'verifying_contract': '0x3333333333333333333333333333333333333333',
            }
        },
        '/v1/system/config': {
            'data': {'addresses': {'router': '0x4444444444444444444444444444444444444444'}}
        },
        '/v1/auth/session-key-status': {'data': {'active': True}},
        '/v1/auth/signers': {
            'data': {
                'signers': [
                    {
                        'signer': signer,
                        'account': account,
                        'expiration': 2_000_000_000,
                        'status': 1,
                        'permissions': ['PERPS'],
                    }
                ]
            }
        },
    })

    evidence = await collect_public_signer_evidence(
        transport,
        network='testnet',
        account=account,
        signer=signer,
    )

    assert evidence.chain_id == 11155931
    assert evidence.auth_contract == '0x3333333333333333333333333333333333333333'
    assert evidence.router == '0x4444444444444444444444444444444444444444'
    assert evidence.session_active is True
    assert evidence.session_account == account
    assert evidence.session_expiration == 2_000_000_000
    assert evidence.permission_evidence_source == 'api'
    assert evidence.permissions == frozenset({'PERPS'})
    assert evidence.perps_order_succeeded is None
    assert evidence.fund_movement_rejected is None
    assert evidence.withdrawal_rejected is None
    assert evidence.post_revoke_order_rejected is None
    assert evidence.operatorhub_bypass_disabled is None
    assert transport.post_calls == []
    assert [path for path, _ in transport.get_calls] == [
        '/v1/auth/eip712-domain',
        '/v1/system/config',
        '/v1/auth/session-key-status',
        '/v1/auth/signers',
    ]

    report = evaluate_signer_capabilities(evidence, now=1_900_000_000)
    assert report.verdict == 'UNKNOWN'
    assert report.security_gate_passed is False


@pytest.mark.asyncio
async def test_numeric_session_status_is_not_guessed_as_active() -> None:
    from app.security.risex_signer_probe import collect_public_signer_evidence

    account = '0x1111111111111111111111111111111111111111'
    signer = '0x2222222222222222222222222222222222222222'
    transport = FakeTransport({
        '/v1/auth/eip712-domain': {'data': {'chain_id': 11155931, 'verifying_contract': None}},
        '/v1/system/config': {'data': {}},
        '/v1/auth/session-key-status': {'data': {'status': 1}},
        '/v1/auth/signers': {'data': {'signers': []}},
    })

    evidence = await collect_public_signer_evidence(
        transport,
        network='testnet',
        account=account,
        signer=signer,
    )

    assert evidence.session_active is None
    assert evidence.session_account is None
    assert evidence.session_expiration is None
    assert evidence.permission_evidence_source == 'none'
    assert evidence.permissions is None


@pytest.mark.asyncio
async def test_public_signer_evidence_read_failure_is_explicit() -> None:
    from app.security.risex_signer_probe import collect_public_signer_evidence

    account = '0x1111111111111111111111111111111111111111'
    signer = '0x2222222222222222222222222222222222222222'
    transport = FakeTransport({
        '/v1/auth/eip712-domain': RuntimeError('provider timeout'),
    })

    with pytest.raises(ProviderReadUnavailable, match='eip712-domain'):
        await collect_public_signer_evidence(
            transport,
            network='testnet',
            account=account,
            signer=signer,
        )

    assert transport.post_calls == []
