from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


def _module() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / 'scripts'
        / 'risex_manual_order_probe.py'
    )
    if not path.exists():
        pytest.fail(
            'RED: manual RISEx one-shot CLI entry point is not implemented yet',
            pytrace=False,
        )

    spec = importlib.util.spec_from_file_location('risex_manual_order_probe', path)
    if spec is None or spec.loader is None:
        pytest.fail('RED: unable to load manual RISEx probe module', pytrace=False)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manual_cli_defaults_to_no_order_authorization() -> None:
    module = _module()

    args = module.build_parser().parse_args([])

    assert args.approve_testnet_order is False


def test_manual_cli_intent_is_fixed_to_btc_buy_minimum() -> None:
    module = _module()

    intent = module.manual_probe_intent()

    assert intent.symbol == 'BTC'
    assert intent.side == 'BUY'
    assert intent.use_min_order_size is True


def test_manual_cli_refuses_without_explicit_operator_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    execute_once = AsyncMock(side_effect=AssertionError('must not execute'))
    monkeypatch.setattr(module, '_execute_once', execute_once)

    exit_code = module.main([])

    assert exit_code != 0
    assert execute_once.await_count == 0


def test_manual_cli_never_retries_failed_one_shot_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    execute_once = AsyncMock(side_effect=SignedTestnetBlocked('sentinel block'))
    monkeypatch.setattr(module, '_execute_once', execute_once)

    exit_code = module.main(['--approve-testnet-order'])

    assert exit_code != 0
    assert execute_once.await_count == 1


def test_manual_cli_sanitizes_private_key_and_signature_material() -> None:
    module = _module()
    private_key = '0x' + ('ab' * 32)
    signature = 'sensitive-signature-bytes'

    payload = module.sanitize_diagnostic_payload(
        {
            'market_id': 17,
            'client_order_id': 42,
            'permit': {
                'account': '0x' + ('11' * 20),
                'signer': '0x' + ('22' * 20),
                'signature': signature,
            },
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': private_key,
            '_signature': signature,
        }
    )
    rendered = json.dumps(payload, sort_keys=True).lower()

    assert private_key.lower() not in rendered
    assert signature.lower() not in rendered
    assert 'private_key' not in rendered
    assert 'signature' not in rendered
    assert payload['market_id'] == 17
    assert payload['client_order_id'] == 42


@pytest.mark.asyncio
async def test_build_pre_order_gate_rejects_without_disposable_account_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    credential = SimpleNamespace(
        account_address='0x' + ('11' * 20),
        signer_address='0x' + ('22' * 20),
    )
    deployment = SimpleNamespace(
        block_number=123,
        api_chain_id=11155931,
        domain_verifying_contract='0x' + ('33' * 20),
        system_router='0x' + ('44' * 20),
    )
    deployment_report = SimpleNamespace(
        verdict='PASS',
        deployment_identity_verified=True,
    )
    authorization = SimpleNamespace(
        session_active=True,
        account=credential.account_address,
        session_expiration=1_900_000_100,
        perps_only_scope=None,
        perps_permission=True,
        block_timestamp=1_900_000_000,
    )

    monkeypatch.setattr(module, 'load_testnet_signer_credential', lambda _env: credential)

    async def collect_deployment(*_args: object, **_kwargs: object) -> object:
        return deployment

    async def collect_authorization(*_args: object, **_kwargs: object) -> object:
        return authorization

    monkeypatch.setattr(module, 'collect_runtime_deployment_evidence', collect_deployment)
    monkeypatch.setattr(
        module,
        'evaluate_pinned_deployment_preflight',
        lambda *_args, **_kwargs: deployment_report,
    )
    monkeypatch.setattr(
        module,
        'collect_authorization_session_evidence',
        collect_authorization,
    )

    with pytest.raises(SignedTestnetBlocked, match='disposable'):
        await module._build_pre_order_gate(
            env={},
            api=object(),
            rpc=object(),
            disposable_account_asserted=False,
            dedicated_signer_asserted=True,
            operatorhub_bypass_disabled=True,
            fund_movement_path_absent=True,
        )
