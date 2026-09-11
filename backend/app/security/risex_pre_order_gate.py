from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.security.risex_signed_testnet_policy import (
    SignedTestnetBlocked,
    SignedTestnetPolicy,
    assert_signed_testnet_probe_allowed,
)
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence


_ATTESTATION_SEAL = object()


def _is_address(value: str | None) -> bool:
    if value is None or len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class RISExPreOrderProbeGate:
    """Attested capability token required before the first signed Perps order probe."""

    account_address: str
    signer_address: str
    order_probe_allowed: Literal[True] = True
    _attestation_seal: object = field(repr=False, compare=False, default=None)


def assert_pre_order_probe_gate_attested(gate: RISExPreOrderProbeGate) -> None:
    """Reject hand-built or stale-looking gate objects before any provider mutation."""

    if not isinstance(gate, RISExPreOrderProbeGate):
        raise SignedTestnetBlocked('RISEx order probe requires an attested pre-order gate')
    if gate._attestation_seal is not _ATTESTATION_SEAL or gate.order_probe_allowed is not True:
        raise SignedTestnetBlocked('RISEx order probe requires an attested pre-order gate')


def authorize_pre_order_probe(
    *,
    policy: SignedTestnetPolicy,
    evidence: RISExSignerCapabilityEvidence,
    now: int,
    replay_protection_verified: bool | None,
) -> RISExPreOrderProbeGate:
    """Fail closed until every security proof required before the first order is explicit.

    The positive Perps order and post-revocation negative order are deliberately
    excluded here because they can only be observed after this pre-order gate.
    """

    assert_signed_testnet_probe_allowed(policy)

    if evidence.network != 'testnet' or evidence.network != policy.network:
        raise SignedTestnetBlocked('RISEx pre-order evidence must be bound to testnet')
    if not _is_address(evidence.account) or not _is_address(evidence.signer):
        raise SignedTestnetBlocked('RISEx pre-order account and signer addresses must be verified')
    if evidence.chain_id is None or evidence.chain_id <= 0:
        raise SignedTestnetBlocked('RISEx pre-order deployment chain identity is unavailable')
    if not _is_address(evidence.auth_contract) or not _is_address(evidence.router):
        raise SignedTestnetBlocked('RISEx pre-order deployment addresses are unavailable')

    if evidence.session_active is not True:
        raise SignedTestnetBlocked('RISEx session signer must be active before the order probe')
    if evidence.session_account is None or evidence.session_account.lower() != evidence.account.lower():
        raise SignedTestnetBlocked('RISEx session signer account binding is not verified')
    if evidence.session_expiration is None or evidence.session_expiration <= now:
        raise SignedTestnetBlocked('RISEx session signer is expired or expiration is unavailable')

    if evidence.onchain_perps_only_scope is not True:
        raise SignedTestnetBlocked('RISEx Perps-only authorization scope is not proven')
    if evidence.fund_movement_rejected is not True:
        raise SignedTestnetBlocked('RISEx fund-movement rejection has not been proven')
    if evidence.withdrawal_rejected is not True:
        raise SignedTestnetBlocked('RISEx withdrawal rejection has not been proven')
    if evidence.operatorhub_bypass_disabled is not True:
        raise SignedTestnetBlocked('RISEx JWT/OperatorHub bypass must remain disabled')
    if replay_protection_verified is not True:
        raise SignedTestnetBlocked('RISEx replay protection has not been verified')

    gate = RISExPreOrderProbeGate(
        account_address=evidence.account,
        signer_address=evidence.signer,
        _attestation_seal=_ATTESTATION_SEAL,
    )
    assert_pre_order_probe_gate_attested(gate)
    return gate
