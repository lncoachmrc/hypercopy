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
    session_expiration: int = 0
    deployment_chain_id: int = 0
    deployment_auth_contract: str = ''
    deployment_router: str = ''
    order_probe_allowed: Literal[True] = True
    _attestation_seal: object = field(repr=False, compare=False, default=None)


def assert_pre_order_probe_gate_attested(
    gate: RISExPreOrderProbeGate,
    *,
    now: int | None = None,
    evidence: RISExSignerCapabilityEvidence | None = None,
) -> None:
    """Reject forged or stale gates before any provider mutation.

    Without ``now``/``evidence`` this verifies only the sealed gate structure, which
    is sufficient at construction time.  Immediately before a provider POST callers
    must supply both values so session freshness and pinned deployment identity are
    revalidated against current evidence.
    """

    if not isinstance(gate, RISExPreOrderProbeGate):
        raise SignedTestnetBlocked('RISEx order probe requires an attested pre-order gate')
    if gate._attestation_seal is not _ATTESTATION_SEAL or gate.order_probe_allowed is not True:
        raise SignedTestnetBlocked('RISEx order probe requires an attested pre-order gate')

    if now is None and evidence is None:
        return
    if now is None or evidence is None:
        raise SignedTestnetBlocked('RISEx pre-order gate freshness evidence is incomplete')

    if gate.session_expiration <= now:
        raise SignedTestnetBlocked('RISEx pre-order gate session is expired')
    if evidence.network != 'testnet':
        raise SignedTestnetBlocked('RISEx pre-order freshness evidence must be bound to testnet')
    if evidence.session_active is not True:
        raise SignedTestnetBlocked('RISEx session signer is revoked or no longer active')
    if evidence.session_expiration is None or evidence.session_expiration <= now:
        raise SignedTestnetBlocked('RISEx session signer is expired or expiration is unavailable')
    if evidence.session_expiration != gate.session_expiration:
        raise SignedTestnetBlocked('RISEx session signer lifecycle changed after gate attestation')
    if (
        evidence.account.lower() != gate.account_address.lower()
        or evidence.signer.lower() != gate.signer_address.lower()
        or evidence.session_account is None
        or evidence.session_account.lower() != gate.account_address.lower()
    ):
        raise SignedTestnetBlocked('RISEx session signer identity changed after gate attestation')

    deployment_matches = (
        evidence.chain_id == gate.deployment_chain_id
        and evidence.auth_contract is not None
        and evidence.router is not None
        and evidence.auth_contract.lower() == gate.deployment_auth_contract.lower()
        and evidence.router.lower() == gate.deployment_router.lower()
    )
    if not deployment_matches:
        raise SignedTestnetBlocked('RISEx deployment identity changed after gate attestation')


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
        session_expiration=evidence.session_expiration,
        deployment_chain_id=evidence.chain_id,
        deployment_auth_contract=evidence.auth_contract,
        deployment_router=evidence.router,
        _attestation_seal=_ATTESTATION_SEAL,
    )
    assert_pre_order_probe_gate_attested(gate)
    return gate
