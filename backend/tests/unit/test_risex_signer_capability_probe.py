from __future__ import annotations

from time import time

from app.adapters.risex import RISExAdapter


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
        'permission_evidence_source': 'onchain',
        'permissions': frozenset({'PERPS'}),
        'perps_order_succeeded': True,
        'fund_movement_rejected': True,
        'withdrawal_rejected': True,
        'post_revoke_order_rejected': True,
        'operatorhub_bypass_disabled': True,
    }
    values.update(overrides)
    return RISExSignerCapabilityEvidence(**values)


def test_capability_probe_pass_requires_all_security_evidence() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(_evidence(), now=int(time()))

    assert report.verdict == 'PASS'
    assert report.security_gate_passed is True
    assert all(check.verdict == 'PASS' for check in report.checks)


def test_capability_probe_is_unknown_without_onchain_permission_proof() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(
        _evidence(permission_evidence_source='none', permissions=None),
        now=int(time()),
    )

    assert report.verdict == 'UNKNOWN'
    assert report.security_gate_passed is False
    permission_check = next(check for check in report.checks if check.name == 'least_privilege_permissions')
    assert permission_check.verdict == 'UNKNOWN'


def test_capability_probe_fails_if_fund_movement_permission_exists() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(
        _evidence(permissions=frozenset({'PERPS', 'MOVE_FUNDS'})),
        now=int(time()),
    )

    assert report.verdict == 'FAIL'
    assert report.security_gate_passed is False


def test_capability_probe_fails_if_operatorhub_bypass_is_reachable() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(
        _evidence(operatorhub_bypass_disabled=False),
        now=int(time()),
    )

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
        report = evaluate_signer_capabilities(_evidence(**overrides), now=now)
        assert report.verdict == 'FAIL'
        assert report.security_gate_passed is False


def test_capability_probe_never_enables_risex_writes() -> None:
    from app.security.risex_signer_probe import evaluate_signer_capabilities

    report = evaluate_signer_capabilities(_evidence(), now=int(time()))

    assert report.verdict == 'PASS'
    assert RISExAdapter.writes_enabled is False
