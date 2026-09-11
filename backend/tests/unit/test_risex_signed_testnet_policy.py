from __future__ import annotations

import pytest


def _policy(**overrides: object):
    from app.security.risex_signed_testnet_policy import SignedTestnetPolicy

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
    return SignedTestnetPolicy(**values)


def test_signed_testnet_policy_accepts_only_the_controlled_testnet_probe() -> None:
    from app.security.risex_signed_testnet_policy import assert_signed_testnet_probe_allowed

    assert_signed_testnet_probe_allowed(_policy())


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'network': 'mainnet'}, 'testnet'),
        ({'explicit_approval': False}, 'approval'),
        ({'deployment_verdict': 'UNKNOWN'}, 'deployment'),
        ({'deployment_verdict': 'FAIL'}, 'deployment'),
        ({'deployment_identity_verified': False}, 'deployment'),
        ({'disposable_account_asserted': False}, 'disposable'),
        ({'dedicated_signer_asserted': False}, 'dedicated signer'),
        ({'operatorhub_bypass_disabled': False}, 'OperatorHub'),
    ],
)
def test_signed_testnet_policy_fails_closed(overrides: dict[str, object], message: str) -> None:
    from app.security.risex_signed_testnet_policy import (
        SignedTestnetBlocked,
        assert_signed_testnet_probe_allowed,
    )

    with pytest.raises(SignedTestnetBlocked, match=message):
        assert_signed_testnet_probe_allowed(_policy(**overrides))


def test_signed_testnet_policy_rejects_any_main_wallet_private_key_environment_name() -> None:
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked, reject_main_wallet_key_inputs

    env = {
        'RISEX_TESTNET_ACCOUNT_ADDRESS': '0x' + ('11' * 20),
        'RISEX_TESTNET_SIGNER_PRIVATE_KEY': '0x' + ('22' * 32),
        'RISEX_TESTNET_ACCOUNT_PRIVATE_KEY': '0x' + ('33' * 32),
    }

    with pytest.raises(SignedTestnetBlocked, match='main-wallet private key'):
        reject_main_wallet_key_inputs(env)
