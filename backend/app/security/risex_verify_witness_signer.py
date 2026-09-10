from __future__ import annotations

from dataclasses import dataclass, field

from eth_account import Account
from eth_account.messages import encode_typed_data

from app.security.risex_order_codec import build_verify_witness_typed_data
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_testnet_signer import RISExTestnetSignerCredential


@dataclass(frozen=True, slots=True)
class RISExVerifyWitnessSignature:
    """Offline VerifyWitness signature with reusable signature bytes hidden from repr."""

    signer_address: str
    message_hash: str
    _signature: bytes = field(repr=False, compare=False)

    def signature_bytes_for_transport(self) -> bytes:
        """Return signature bytes only to an explicitly controlled testnet transport."""

        return self._signature


def sign_verify_witness(
    credential: RISExTestnetSignerCredential,
    *,
    domain_name: str,
    domain_version: str,
    chain_id: int,
    verifying_contract: str,
    router: str,
    action_hash: str,
    nonce_anchor: int,
    nonce_bitmap: int,
    deadline: int,
) -> RISExVerifyWitnessSignature:
    """Build, sign, and locally recover a RISEx VerifyWitness message offline."""

    typed_data = build_verify_witness_typed_data(
        domain_name=domain_name,
        domain_version=domain_version,
        chain_id=chain_id,
        verifying_contract=verifying_contract,
        account=credential.account_address,
        router=router,
        action_hash=action_hash,
        nonce_anchor=nonce_anchor,
        nonce_bitmap=nonce_bitmap,
        deadline=deadline,
    )
    signable_message = encode_typed_data(full_message=typed_data)
    local_account = credential.local_account_for_signing()
    signed = local_account.sign_message(signable_message)
    signature = bytes(signed.signature)

    recovered = Account.recover_message(signable_message, signature=signature)
    if recovered.lower() != credential.signer_address.lower():
        raise SignedTestnetBlocked(
            'RISEx VerifyWitness signature recovery did not match the dedicated signer'
        )

    return RISExVerifyWitnessSignature(
        signer_address=credential.signer_address,
        message_hash='0x' + bytes(signed.message_hash).hex(),
        _signature=signature,
    )
