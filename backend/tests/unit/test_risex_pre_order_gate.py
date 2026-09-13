from __future__ import annotations

from dataclasses import fields
from time import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.security.risex_signed_testnet_policy import SignedTestnetBlocked, SignedTestnetPolicy
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)
ADR_REFERENCE = 'ADR-0002'
AUTHORIZATION_CRITERION = 'perps_permission_and_fund_movement_path_absent'


def _policy(**overrides: object) -> SignedTestnetPolicy:
    values: dict[str, object] = {
        'network': 'testnet',
        'explicit_approval': True,
        'deployment_verdict': 'PASS',
        'deployment_identity_verified': True,
        'disposable_account_asserted': True,
        'dedicated_signer_asserted': True,
        'operatorhub_bypass_disabled': True,
    }
    values.update(overrides)
    return SignedTestnetPolicy(**values)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> RISExSignerCapabilityEvidence:
    now = int(time())
    values: dict[str, object] = {
        'network': 'testnet',
        'account': ACCOUNT,
        'signer': SIGNER,
        'chain_id': 11155931,
        'auth_contract': AUTH,
        'router': ROUTER,
        'session_active': True,
        'session_account': ACCOUNT,
        'session_expiration': now + 3600,
        'onchain_perps_only_scope': True,
        'perps_order_succeeded': None,
        'fund_movement_rejected': True,
        'withdrawal_rejected': True,
        'post_revoke_order_rejected': None,
        'operatorhub_bypass_disabled': True,
    }
    values.update(overrides)
    return RISExSignerCapabilityEvidence(**values)  # type: ignore[arg-type]


def _adr0002_evidence(**overrides: object) -> SimpleNamespace:
    base = _evidence(onchain_perps_only_scope=False)
    values = {field.name: getattr(base, field.name) for field in fields(base)}
    values.update(
        {
            'perps_permission': True,
            'fund_movement_path_absent': True,
        }
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _gate(**evidence_overrides: object):
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    return authorize_pre_order_probe(
        policy=_policy(),
        evidence=_evidence(**evidence_overrides),
        now=int(time()),
        replay_protection_verified=True,
    )


def _authorize_adr0002(**evidence_overrides: object):
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    return authorize_pre_order_probe(
        policy=_policy(),
        evidence=_adr0002_evidence(**evidence_overrides),  # type: ignore[arg-type]
        now=int(time()),
        replay_protection_verified=True,
    )


def test_pre_order_gate_allows_probe_before_positive_order_and_post_revoke_tests() -> None:
    gate = _gate(perps_order_succeeded=None, post_revoke_order_rejected=None)

    assert gate.account_address == ACCOUNT
    assert gate.signer_address == SIGNER
    assert gate.order_probe_allowed is True
    assert gate.session_expiration > int(time())
    assert gate.deployment_chain_id == 11155931
    assert gate.deployment_auth_contract == AUTH
    assert gate.deployment_router == ROUTER


def test_pre_order_gate_attests_broad_signer_under_adr0002_criterion() -> None:
    gate = _authorize_adr0002()

    assert gate.order_probe_allowed is True
    assert getattr(gate, 'adr_reference', None) == ADR_REFERENCE
    assert getattr(gate, 'authorization_criterion', None) == AUTHORIZATION_CRITERION


def test_pre_order_gate_rejects_without_fund_movement_path_assertion() -> None:
    with pytest.raises(SignedTestnetBlocked, match='fund-movement path'):
        _authorize_adr0002(fund_movement_path_absent=False)


def test_pre_order_gate_rejects_without_perps_permission_even_with_fund_path_assertion() -> None:
    with pytest.raises(SignedTestnetBlocked, match='Perps permission'):
        _authorize_adr0002(perps_permission=False, fund_movement_path_absent=True)


@pytest.mark.parametrize(
    ('field', 'match'),
    [
        ('fund_movement_rejected', 'fund-movement rejection'),
        ('withdrawal_rejected', 'withdrawal rejection'),
    ],
)
def test_pre_order_gate_still_requires_negative_provider_evidence(
    field: str,
    match: str,
) -> None:
    with pytest.raises(SignedTestnetBlocked, match=match):
        _authorize_adr0002(**{field: None})


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('fund_movement_rejected', None),
        ('fund_movement_rejected', False),
        ('withdrawal_rejected', None),
        ('withdrawal_rejected', False),
        ('session_active', None),
        ('session_active', False),
    ],
)
def test_pre_order_gate_fails_closed_without_required_provider_evidence(
    field: str,
    value: object,
) -> None:
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    with pytest.raises(SignedTestnetBlocked):
        authorize_pre_order_probe(
            policy=_policy(),
            evidence=_evidence(**{field: value}),
            now=int(time()),
            replay_protection_verified=True,
        )


def test_pre_order_gate_requires_replay_protection_proof() -> None:
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    for value in (None, False):
        with pytest.raises(SignedTestnetBlocked, match='replay'):
            authorize_pre_order_probe(
                policy=_policy(),
                evidence=_evidence(),
                now=int(time()),
                replay_protection_verified=value,  # type: ignore[arg-type]
            )


def test_pre_order_gate_binds_session_to_expected_account() -> None:
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    with pytest.raises(SignedTestnetBlocked, match='account'):
        authorize_pre_order_probe(
            policy=_policy(),
            evidence=_evidence(session_account='0x' + ('55' * 20)),
            now=int(time()),
            replay_protection_verified=True,
        )


def test_signed_transport_rejects_policy_only_constructor_bypass() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport

    constructor: Any = RISExSignedTestnetHTTPTransport
    with pytest.raises(TypeError, match='policy'):
        constructor(policy=_policy())
