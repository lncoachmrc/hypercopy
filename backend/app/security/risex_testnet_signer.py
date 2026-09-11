from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from eth_account import Account
from eth_account.signers.local import LocalAccount

from app.security.risex_signed_testnet_policy import (
    SignedTestnetBlocked,
    reject_main_wallet_key_inputs,
)


_ACCOUNT_ADDRESS_ENV = 'RISEX_TESTNET_ACCOUNT_ADDRESS'
_SIGNER_PRIVATE_KEY_ENV = 'RISEX_TESTNET_SIGNER_PRIVATE_KEY'


def _is_hex_address(value: str | None) -> bool:
    if value is None or len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class RISExTestnetSignerCredential:
    """Dedicated testnet signer material with secret representation suppressed."""

    account_address: str
    signer_address: str
    _local_account: LocalAccount = field(repr=False, compare=False)

    def local_account_for_signing(self) -> LocalAccount:
        """Internal signing capability; callers must never log or serialize the result."""

        return self._local_account


def load_testnet_signer_credential(env: Mapping[str, str]) -> RISExTestnetSignerCredential:
    """Load only the disposable account address and its dedicated RISEx session signer."""

    reject_main_wallet_key_inputs(env)

    account_address = env.get(_ACCOUNT_ADDRESS_ENV)
    if not _is_hex_address(account_address):
        raise SignedTestnetBlocked('A valid RISEx testnet account address is required')

    signer_private_key = env.get(_SIGNER_PRIVATE_KEY_ENV)
    if not signer_private_key:
        raise SignedTestnetBlocked('A dedicated signer private key is required for testnet verification')

    try:
        local_account = Account.from_key(signer_private_key)
    except Exception as exc:
        raise SignedTestnetBlocked('The dedicated RISEx testnet signer private key is invalid') from exc

    return RISExTestnetSignerCredential(
        account_address=account_address,
        signer_address=local_account.address,
        _local_account=local_account,
    )
