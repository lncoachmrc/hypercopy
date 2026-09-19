from __future__ import annotations

from dataclasses import fields
from time import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import prepare_place_order_request
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked, SignedTestnetPolicy
from tests.unit.risex_replay_test_support import make_test_replay_architecture_attestation
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)
ADR_REFERENCE = 'ADR-0002'
ADR_0003_REFERENCE = 'ADR-0003'
AUTHORIZATION_CRITERION = 'perps_permission_and_fund_movement_path_absent'



def _request(*, account: str = ACCOUNT, signer: str = SIGNER):
    now = int(time())
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=7,
        ttl_units=0,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=account,
        signer_address=signer,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=now + 300,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


def _replay_attestation(*, account: str = ACCOUNT, signer: str = SIGNER):
    return make_test_replay_architecture_attestation(
        request=_request(account=account, signer=signer),
        chain_id=11155931,
        authorization_address=AUTH,
        router_address=ROUTER,
    )


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
        'onchain_perps_only_scope': False,
        'perps_order_succeeded': None,
        'fund_movement_rejected': True,
        'withdrawal_rejected': True,
        'post_revoke_order_rejected': None,
        'operatorhub_bypass_disabled': True,
        'perps_permission': True,
        'fund_movement_path_absent': True,
    }
    values.update(overrides)
    return RISExSignerCapabilityEvidence(**values)  # type: ignore[arg-type]


def _evidence_without_explicit_fund_path_assertion() -> RISExSignerCapabilityEvidence:
    now = int(time())
    return RISExSignerCapabilityEvidence(
        network='testnet',
        account=ACCOUNT,
        signer=SIGNER,
        chain_id=11155931,
        auth_contract=AUTH,
        router=ROUTER,
        session_active=True,
        session_account=ACCOUNT,
        session_expiration=now + 3600,
        onchain_perps_only_scope=False,
        perps_order_succeeded=None,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=True,
        perps_permission=True,
    )


def _adr0002_evidence(**overrides: object) -> SimpleNamespace:
    base = _evidence(onchain_perps_only_scope=False)
    values = {field.name: getattr(base, field.name) for field in fields(base)}
    values.update({'perps_permission': True, 'fund_movement_path_absent': True})
    values.update(overrides)
    return SimpleNamespace(**values)


def _gate(**evidence_overrides: object):
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    return authorize_pre_order_probe(
        policy=_policy(),
        evidence=_evidence(**evidence_overrides),
        now=int(time()),
        replay_protection_architecture_attestation=_replay_attestation(),
    )


def _authorize_adr0002(**evidence_overrides: object):
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    return authorize_pre_order_probe(
        policy=_policy(),
        evidence=_adr0002_evidence(**evidence_overrides),  # type: ignore[arg-type]
        now=int(time()),
        replay_protection_architecture_attestation=_replay_attestation(),
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


def test_pre_order_gate_allows_none_negative_probes_when_fund_path_is_absent() -> None:
    gate = _authorize_adr0002(
        fund_movement_path_absent=True,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
    )
    assert gate.order_probe_allowed is True


def test_pre_order_gate_rejects_none_negative_probes_when_fund_path_is_not_absent() -> None:
    with pytest.raises(SignedTestnetBlocked, match='fund-movement path'):
        _authorize_adr0002(
            fund_movement_path_absent=False,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
        )


def test_pre_order_gate_rejects_when_fund_path_assertion_is_uncertain() -> None:
    with pytest.raises(SignedTestnetBlocked, match='fund-movement path'):
        _authorize_adr0002(
            fund_movement_path_absent=None,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
        )


def test_pre_order_gate_rejects_when_fund_path_assertion_is_omitted() -> None:
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    with pytest.raises(SignedTestnetBlocked, match='fund-movement path'):
        authorize_pre_order_probe(
            policy=_policy(),
            evidence=_evidence_without_explicit_fund_path_assertion(),
            now=int(time()),
            replay_protection_architecture_attestation=_replay_attestation(),
        )


def test_pre_order_gate_rejects_without_perps_permission_even_with_fund_path_assertion() -> None:
    with pytest.raises(SignedTestnetBlocked, match='Perps permission'):
        _authorize_adr0002(
            perps_permission=False,
            fund_movement_path_absent=True,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
        )


def test_pre_order_gate_records_negative_probes_as_na_under_adr0003() -> None:
    gate = _authorize_adr0002(
        fund_movement_path_absent=True,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
    )
    assert getattr(gate, 'negative_probe_adr_reference', None) == ADR_0003_REFERENCE
    assert getattr(gate, 'fund_movement_rejected_probe_status', None) == 'N/A'
    assert getattr(gate, 'withdrawal_rejected_probe_status', None) == 'N/A'
    reason = getattr(gate, 'negative_probe_reason', '')
    assert ADR_0003_REFERENCE in reason
    assert 'no session-key fund-movement path' in reason.lower()


@pytest.mark.parametrize(('field', 'value'), [('session_active', None), ('session_active', False)])
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
            replay_protection_architecture_attestation=_replay_attestation(),
        )


def test_pre_order_gate_requires_sealed_replay_architecture_evidence() -> None:
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    with pytest.raises(SignedTestnetBlocked, match='replay|architectural|sealed'):
        authorize_pre_order_probe(
            policy=_policy(),
            evidence=_evidence(),
            now=int(time()),
            replay_protection_architecture_attestation=object(),  # type: ignore[arg-type]
        )


def test_pre_order_gate_binds_session_to_expected_account() -> None:
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    with pytest.raises(SignedTestnetBlocked, match='account'):
        authorize_pre_order_probe(
            policy=_policy(),
            evidence=_evidence(session_account='0x' + ('55' * 20)),
            now=int(time()),
            replay_protection_architecture_attestation=_replay_attestation(),
        )


def test_signed_transport_rejects_policy_only_constructor_bypass() -> None:
    from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport

    constructor: Any = RISExSignedTestnetHTTPTransport
    with pytest.raises(TypeError, match='policy'):
        constructor(policy=_policy())
