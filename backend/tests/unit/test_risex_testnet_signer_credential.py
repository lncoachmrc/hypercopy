from __future__ import annotations

from eth_account import Account
import pytest


def _valid_env() -> dict[str, str]:
    signer_key = bytes([7]) * 32
    return {
        'RISEX_TESTNET_ACCOUNT_ADDRESS': '0x' + ('11' * 20),
        'RISEX_TESTNET_SIGNER_PRIVATE_KEY': '0x' + signer_key.hex(),
    }


def test_loader_derives_public_signer_without_exposing_secret_in_repr() -> None:
    from app.security.risex_testnet_signer import load_testnet_signer_credential

    env = _valid_env()
    credential = load_testnet_signer_credential(env)
    expected = Account.from_key(env['RISEX_TESTNET_SIGNER_PRIVATE_KEY']).address

    assert credential.account_address == env['RISEX_TESTNET_ACCOUNT_ADDRESS']
    assert credential.signer_address == expected
    rendered = repr(credential)
    assert env['RISEX_TESTNET_SIGNER_PRIVATE_KEY'] not in rendered
    assert env['RISEX_TESTNET_SIGNER_PRIVATE_KEY'][2:] not in rendered


def test_loader_rejects_missing_dedicated_signer_secret() -> None:
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
    from app.security.risex_testnet_signer import load_testnet_signer_credential

    env = {'RISEX_TESTNET_ACCOUNT_ADDRESS': '0x' + ('11' * 20)}
    with pytest.raises(SignedTestnetBlocked, match='dedicated signer'):
        load_testnet_signer_credential(env)


def test_loader_rejects_invalid_public_account_address() -> None:
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
    from app.security.risex_testnet_signer import load_testnet_signer_credential

    env = _valid_env()
    env['RISEX_TESTNET_ACCOUNT_ADDRESS'] = 'not-an-address'
    with pytest.raises(SignedTestnetBlocked, match='account address'):
        load_testnet_signer_credential(env)


def test_loader_rejects_invalid_signer_key_without_echoing_it() -> None:
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
    from app.security.risex_testnet_signer import load_testnet_signer_credential

    env = _valid_env()
    env['RISEX_TESTNET_SIGNER_PRIVATE_KEY'] = 'definitely-not-a-private-key'
    with pytest.raises(SignedTestnetBlocked) as exc_info:
        load_testnet_signer_credential(env)
    assert env['RISEX_TESTNET_SIGNER_PRIVATE_KEY'] not in str(exc_info.value)


def test_loader_rejects_main_wallet_private_key_inputs_before_loading_signer() -> None:
    from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
    from app.security.risex_testnet_signer import load_testnet_signer_credential

    env = _valid_env()
    env['RISEX_TESTNET_ACCOUNT_PRIVATE_KEY'] = 'must-never-be-read'
    with pytest.raises(SignedTestnetBlocked, match='main-wallet private key'):
        load_testnet_signer_credential(env)
