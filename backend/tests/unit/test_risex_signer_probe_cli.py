from __future__ import annotations

import argparse
import asyncio
from time import time
from types import SimpleNamespace

import pytest


ADR_REFERENCE = 'ADR-0002'
ADR_0003_REFERENCE = 'ADR-0003'
AUTHORIZATION_CRITERION = 'perps_permission_and_fund_movement_path_absent'


def test_cli_exposes_public_inputs_only() -> None:
    from app.security.risex_signer_probe_cli import build_parser

    parser = build_parser()
    options = {option for action in parser._actions for option in action.option_strings}
    assert {'--network', '--account', '--signer', '--base-url', '--fund-movement-path-absent'} <= options
    forbidden_tokens = ('key', 'secret', 'seed', 'signature', 'password', 'token')
    assert not any(token in option.lower() for option in options for token in forbidden_tokens)


def test_cli_fund_movement_path_assertion_defaults_false() -> None:
    from app.security.risex_signer_probe_cli import build_parser

    args = build_parser().parse_args([
        '--account', '0x1111111111111111111111111111111111111111',
        '--signer', '0x2222222222222222222222222222222222222222',
    ])
    assert getattr(args, 'fund_movement_path_absent', None) is False


def test_cli_fund_movement_path_flag_is_propagated_to_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.security import risex_signer_probe_cli as cli
    from app.security.risex_signer_probe import RISExSignerCapabilityEvidence, RISExSignerCapabilityReport

    class DummyTransport:
        def __init__(self, *, base_url: str) -> None:
            self.base_url = base_url

        async def __aenter__(self) -> DummyTransport:
            return self

        async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
            return None

    evidence = RISExSignerCapabilityEvidence(
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
        perps_order_succeeded=None,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=True,
        perps_permission=True,
    )
    observed: dict[str, bool] = {}

    async def fake_collect(_transport: object, **_kwargs: object) -> RISExSignerCapabilityEvidence:
        return evidence

    def fake_evaluate(current: RISExSignerCapabilityEvidence, *, now: int) -> RISExSignerCapabilityReport:
        del now
        observed['fund_movement_path_absent'] = current.fund_movement_path_absent
        return RISExSignerCapabilityReport(verdict='UNKNOWN', security_gate_passed=False, checks=())

    monkeypatch.setattr(cli, 'RISExReadOnlyHTTPTransport', DummyTransport)
    monkeypatch.setattr(cli, 'collect_public_signer_evidence', fake_collect)
    monkeypatch.setattr(cli, 'evaluate_signer_capabilities', fake_evaluate)

    args = cli.build_parser().parse_args([
        '--account', evidence.account,
        '--signer', evidence.signer,
        '--fund-movement-path-absent',
    ])
    payload, _exit_code = asyncio.run(cli._run_probe(args))
    assert observed['fund_movement_path_absent'] is True
    assert payload['evidence']['fund_movement_path_absent'] is True


def _payload(*, fund_movement_path_absent: bool, probe_value: bool | None):
    from app.security.risex_signer_probe_cli import report_payload

    now = int(time())
    evidence = SimpleNamespace(
        network='testnet',
        account='0x1111111111111111111111111111111111111111',
        signer='0x2222222222222222222222222222222222222222',
        chain_id=11155931,
        auth_contract='0x3333333333333333333333333333333333333333',
        router='0x4444444444444444444444444444444444444444',
        session_active=True,
        session_account='0x1111111111111111111111111111111111111111',
        session_expiration=now + 3600,
        onchain_perps_only_scope=False,
        perps_permission=True,
        fund_movement_path_absent=fund_movement_path_absent,
        perps_order_succeeded=None,
        fund_movement_rejected=probe_value,
        withdrawal_rejected=probe_value,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=True,
    )
    report = SimpleNamespace(
        verdict='PASS' if fund_movement_path_absent else 'UNKNOWN',
        security_gate_passed=fund_movement_path_absent,
        adr_reference=ADR_REFERENCE,
        authorization_criterion=AUTHORIZATION_CRITERION,
        checks=(),
    )
    return report_payload(evidence, report)  # type: ignore[arg-type]


def test_cli_payload_marks_negative_probes_na_under_adr0003() -> None:
    payload = _payload(fund_movement_path_absent=True, probe_value=None)
    for field in ('fund_movement_rejected', 'withdrawal_rejected'):
        probe = payload['evidence'][field]
        assert probe['value'] is None
        assert probe['status'] == 'N/A'
        assert probe['adr_reference'] == ADR_0003_REFERENCE
        assert ADR_0003_REFERENCE in probe['reason']


def test_cli_payload_keeps_unknown_distinct_from_na() -> None:
    payload = _payload(fund_movement_path_absent=False, probe_value=None)
    for field in ('fund_movement_rejected', 'withdrawal_rejected'):
        probe = payload['evidence'][field]
        assert probe['value'] is None
        assert probe['status'] == 'UNKNOWN'
        assert probe['adr_reference'] == ADR_0003_REFERENCE
        assert probe['status'] != 'N/A'


def test_cli_address_type_accepts_public_address_and_rejects_private_key_shape() -> None:
    from app.security.risex_signer_probe_cli import public_address

    address = '0x1111111111111111111111111111111111111111'
    assert public_address(address) == address
    with pytest.raises(argparse.ArgumentTypeError):
        public_address('0x' + '1' * 64)
    with pytest.raises(argparse.ArgumentTypeError):
        public_address('not-an-address')


def test_cli_payload_is_json_safe_and_exposes_adr0002_criterion_without_enabling_writes() -> None:
    from app.adapters.risex import RISExAdapter

    payload = _payload(fund_movement_path_absent=True, probe_value=None)
    assert payload.get('adr_reference') == ADR_REFERENCE
    assert payload.get('authorization_criterion') == AUTHORIZATION_CRITERION
    assert payload['evidence'].get('perps_permission') is True
    assert payload['evidence'].get('fund_movement_path_absent') is True
    assert payload['evidence']['onchain_perps_only_scope'] is False
    rendered = str(payload).lower()
    assert 'private_key' not in rendered
    assert 'seed' not in rendered
    assert RISExAdapter.writes_enabled is False


def test_cli_exit_codes_keep_unknown_distinct_from_pass_and_fail() -> None:
    from app.security.risex_signer_probe_cli import verdict_exit_code

    assert verdict_exit_code('PASS') == 0
    assert verdict_exit_code('FAIL') == 1
    assert verdict_exit_code('UNKNOWN') == 2
