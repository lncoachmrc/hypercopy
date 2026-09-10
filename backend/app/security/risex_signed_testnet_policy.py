from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.core.config import Network


DeploymentVerdict = Literal['PASS', 'FAIL', 'UNKNOWN']


class SignedTestnetBlocked(RuntimeError):
    """A signed RISEx testnet verification prerequisite is not satisfied."""


@dataclass(frozen=True, slots=True)
class SignedTestnetPolicy:
    network: Network
    explicit_approval: bool
    deployment_verdict: DeploymentVerdict
    deployment_identity_verified: bool
    disposable_account_asserted: bool
    dedicated_signer_asserted: bool
    operatorhub_bypass_disabled: bool


_FORBIDDEN_MAIN_WALLET_KEY_NAMES = frozenset(
    {
        'RISEX_TESTNET_ACCOUNT_PRIVATE_KEY',
        'RISEX_ACCOUNT_PRIVATE_KEY',
        'TRAXION_MAIN_WALLET_PRIVATE_KEY',
        'MAIN_WALLET_PRIVATE_KEY',
    }
)


def reject_main_wallet_key_inputs(env: Mapping[str, object]) -> None:
    """Reject forbidden secret *names* without inspecting or logging their values."""

    present = _FORBIDDEN_MAIN_WALLET_KEY_NAMES.intersection(env.keys())
    if present:
        raise SignedTestnetBlocked(
            'RISEx signed verification rejects every main-wallet private key input'
        )


def assert_signed_testnet_probe_allowed(policy: SignedTestnetPolicy) -> None:
    """Fail closed unless every prerequisite for the isolated testnet probe is explicit."""

    if policy.network != 'testnet':
        raise SignedTestnetBlocked('RISEx signed verification is testnet-only')
    if policy.explicit_approval is not True:
        raise SignedTestnetBlocked('Explicit signed-testnet approval is required')
    if policy.deployment_verdict != 'PASS' or policy.deployment_identity_verified is not True:
        raise SignedTestnetBlocked('Pinned RISEx deployment identity must PASS before signing')
    if policy.disposable_account_asserted is not True:
        raise SignedTestnetBlocked('A disposable RISEx testnet account must be explicitly asserted')
    if policy.dedicated_signer_asserted is not True:
        raise SignedTestnetBlocked('A dedicated signer must be explicitly asserted')
    if policy.operatorhub_bypass_disabled is not True:
        raise SignedTestnetBlocked('JWT/OperatorHub bypass must be disabled for signed verification')
