from __future__ import annotations

from dataclasses import fields
from time import time
from types import SimpleNamespace

from app.adapters.risex import RISExAdapter


ADR_REFERENCE = 'ADR-0002'
ADR_0003_REFERENCE = 'ADR-0003'
AUTHORIZATION_CRITERION = 'perps_permission_and_fund_movement_path_absent'


def _evidence(**overrides):
    from app.security.risex_signer_probe import RISExSignerCapabilityEvidence

    values = {
        'network': 'testnet',
        'account': '0x1111111111111111111111111111111111111111',
        'signer': '0x2222222222222222222222222222222222222222',
        'chain_id': 11155931,
        'auth_contract': '0x3333333333333333333333333333333333333333',
        'router': '0x4444444444444444444444444444444444444444',
        'session_active': True,
        'session_account': '0x1111111111111111111111111111111111111111',
        'session_expiration': int(time()) + 3600,
        'onchain_perps_only_scope': True,
        'perps_order_succeeded': True,
        'fund_movement_rejected': True,
        'withdrawal_rejected': True,
        'post_revoke_order_rejected': True,
        'operatorhub_bypass_disabled': True,
    }
    values.update(overrides)
    return RISExSignerCapabilityEvidence(**values)


def _evidence_without_explicit_fund_path_assertion():
    from app.security.risex_signer_probe import RISExSignerCapabilityEvidence

    return RISExSignerCapabilityEvidence(
        network='testnet',
        account='0x1111111111111111111111111111111111111111',
        signer='0x2222222222222222222222222222222222222222',
        chain_id=11155931,
        auth_contract='0x3333333333333333333333333333333333333333',
        router='0x4444444444444444444444444444444444444444',
        session_active=True,
        session_account='0x1111111111111111111111111111111111111111',
        session_expiration=int(time()) + 3600,
        onchain_perps_only_scope=False,
        perps_order_succeeded=True,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=True,
        operatorhub_bypass_disabled=True,
        perps_permission=True,
    )


def _adr0002_evidence(**overrides) -> SimpleNamespace:
    base = _evidence(onchain_perps_only_scope=False)
    values = {field.name: getattr(base, field.name) for field in fields(base)}
    values.update({'perps_permission': True, 'fund_movement_path_absent': True})
    values.update(overrides)
    return SimpleNamespace(**values)


def _criterion_check(report):
    return next((check for check in report.checks if check.name == 'adr0002_authorization_criterion'), None)


def _negative_check(report, name: str):
    return next((check for check in report.checks if check.name == name), None)


def test_capability_evidence_defaults_fund_path_assertion_fail_closed() -> None:
    evidence = _evidence()
    assert hasattr(evidence, 'perps_permission')
    assert getattr(evidence, 'fund_movement_path_absent', None) is False


def test_capability_probe_passes_broad_signer_under_adr0002_criterion() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(_adr0002_evidence(), now=int(time()))  # type: ignore[arg-type]
    assert report.verdict == 'PASS'
    assert report.security_gate_passed is True
    assert getattr(report, 'adr_reference', None) == ADR_REFERENCE
    assert getattr(report, 'authorization_criterion', None) == AUTHORIZATION_CRITERION
    criterion = _criterion_check(report)
    assert criterion is not None
    assert criterion.verdict == 'PASS'


def test_capability_probe_treats_none_negative_probes_as_na_when_fund_path_is_absent() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(
        _adr0002_evidence(
            fund_movement_path_absent=True,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
        ),  # type: ignore[arg-type]
        now=int(time()),
    )
    assert report.verdict == 'PASS'
    assert report.security_gate_passed is True
    for name in ('fund_movement_negative_test', 'withdrawal_negative_test'):
        check = _negative_check(report, name)
        assert check is not None
        assert check.verdict == 'N/A'
        assert ADR_0003_REFERENCE in check.detail


def test_capability_probe_is_unknown_without_fund_movement_path_assertion() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(
        _adr0002_evidence(
            fund_movement_path_absent=False,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
        ),  # type: ignore[arg-type]
        now=int(time()),
    )
    assert report.verdict == 'UNKNOWN'
    assert report.security_gate_passed is False
    criterion = _criterion_check(report)
    assert criterion is not None
    assert criterion.verdict == 'UNKNOWN'
    for name in ('fund_movement_negative_test', 'withdrawal_negative_test'):
        check = _negative_check(report, name)
        assert check is not None
        assert check.verdict == 'UNKNOWN'
        assert ADR_0003_REFERENCE in check.detail


def test_capability_probe_is_unknown_when_fund_path_assertion_is_uncertain() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(
        _adr0002_evidence(
            fund_movement_path_absent=None,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
        ),  # type: ignore[arg-type]
        now=int(time()),
    )
    assert report.verdict == 'UNKNOWN'
    assert report.security_gate_passed is False
    criterion = _criterion_check(report)
    assert criterion is not None
    assert criterion.verdict == 'UNKNOWN'
    for name in ('fund_movement_negative_test', 'withdrawal_negative_test'):
        check = _negative_check(report, name)
        assert check is not None
        assert check.verdict == 'UNKNOWN'
        assert ADR_0003_REFERENCE in check.detail


def test_capability_probe_is_unknown_when_fund_path_assertion_is_omitted() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(_evidence_without_explicit_fund_path_assertion(), now=int(time()))
    assert report.verdict == 'UNKNOWN'
    assert report.security_gate_passed is False
    criterion = _criterion_check(report)
    assert criterion is not None
    assert criterion.verdict == 'UNKNOWN'
    for name in ('fund_movement_negative_test', 'withdrawal_negative_test'):
        check = _negative_check(report, name)
        assert check is not None
        assert check.verdict == 'UNKNOWN'
        assert ADR_0003_REFERENCE in check.detail


def test_capability_probe_fails_without_perps_permission_even_with_fund_path_assertion() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(
        _adr0002_evidence(
            perps_permission=False,
            fund_movement_path_absent=True,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
        ),  # type: ignore[arg-type]
        now=int(time()),
    )
    assert report.verdict == 'FAIL'
    assert report.security_gate_passed is False
    criterion = _criterion_check(report)
    assert criterion is not None
    assert criterion.verdict == 'FAIL'


def test_onchain_perps_only_scope_remains_diagnostic_only() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(_adr0002_evidence(onchain_perps_only_scope=False), now=int(time()))  # type: ignore[arg-type]
    assert report.verdict == 'PASS'
    assert report.security_gate_passed is True


def test_capability_probe_fails_if_operatorhub_bypass_is_reachable() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(_adr0002_evidence(operatorhub_bypass_disabled=False), now=int(time()))  # type: ignore[arg-type]
    assert report.verdict == 'FAIL'
    assert report.security_gate_passed is False


def test_capability_probe_fails_inactive_expired_or_wrong_account() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    now = int(time())
    for overrides in (
        {'session_active': False},
        {'session_expiration': now - 1},
        {'session_account': '0x5555555555555555555555555555555555555555'},
    ):
        report = evaluate_signer_capabilities(_adr0002_evidence(**overrides), now=now)  # type: ignore[arg-type]
        assert report.verdict == 'FAIL'
        assert report.security_gate_passed is False


def test_capability_probe_never_enables_risex_writes() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(_adr0002_evidence(), now=int(time()))  # type: ignore[arg-type]
    assert report.verdict == 'PASS'
    assert RISExAdapter.writes_enabled is False
