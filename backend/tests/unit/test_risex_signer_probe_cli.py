from __future__ import annotations

import argparse
from time import time

import pytest


def test_cli_exposes_public_inputs_only() -> None:
    from app.security.risex_signer_probe_cli import build_parser

    parser = build_parser()
    options = {
        option
        for action in parser._actions
        for option in action.option_strings
    }

    assert {'--network', '--account', '--signer', '--base-url'} <= options
    forbidden_tokens = ('key', 'secret', 'seed', 'signature', 'password', 'token')
    assert not any(
        token in option.lower()
        for option in options
        for token in forbidden_tokens
    )


def test_cli_address_type_accepts_public_address_and_rejects_private_key_shape() -> None:
    from app.security.risex_signer_probe_cli import public_address

    address = '0x1111111111111111111111111111111111111111'
    assert public_address(address) == address

    with pytest.raises(argparse.ArgumentTypeError):
        public_address('0x' + '1' * 64)

    with pytest.raises(argparse.ArgumentTypeError):
        public_address('not-an-address')


def test_cli_payload_is_json_safe_and_does_not_enable_writes() -> None:
    from app.adapters.risex import RISExAdapter
    from app.security.risex_signer_probe import (
        RISExSignerCapabilityEvidence,
        evaluate_signer_capabilities,
    )
    from app.security.risex_signer_probe_cli import report_payload

    now = int(time())
    evidence = RISExSignerCapabilityEvidence(
        network='testnet',
        account='0x1111111111111111111111111111111111111111',
        signer='0x2222222222222222222222222222222222222222',
        chain_id=11155931,
        auth_contract='0x3333333333333333333333333333333333333333',
        router='0x4444444444444444444444444444444444444444',
        session_active=True,
        session_account='0x1111111111111111111111111111111111111111',
        session_expiration=now + 3600,
        onchain_perps_only_scope=None,
        perps_order_succeeded=None,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=None,
    )
    report = evaluate_signer_capabilities(evidence, now=now)

    payload = report_payload(evidence, report)

    assert payload['verdict'] == 'UNKNOWN'
    assert payload['security_gate_passed'] is False
    assert payload['evidence']['onchain_perps_only_scope'] is None
    assert 'permissions' not in payload['evidence']
    rendered = str(payload).lower()
    assert 'private_key' not in rendered
    assert 'seed' not in rendered
    assert RISExAdapter.writes_enabled is False


def test_cli_exit_codes_keep_unknown_distinct_from_pass_and_fail() -> None:
    from app.security.risex_signer_probe_cli import verdict_exit_code

    assert verdict_exit_code('PASS') == 0
    assert verdict_exit_code('FAIL') == 1
    assert verdict_exit_code('UNKNOWN') == 2
