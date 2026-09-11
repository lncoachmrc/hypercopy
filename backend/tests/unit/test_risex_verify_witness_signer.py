from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data


CHAIN_ID = 11155931
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)
ACTION_HASH = '0x' + ('55' * 32)
NONCE_ANCHOR = 1_800_000_000
NONCE_BITMAP = 1
DEADLINE = 1_800_000_300


def _credential():
    from app.security.risex_testnet_signer import load_testnet_signer_credential

    signer_key = bytes([9]) * 32
    return load_testnet_signer_credential(
        {
            'RISEX_TESTNET_ACCOUNT_ADDRESS': '0x' + ('11' * 20),
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': '0x' + signer_key.hex(),
        }
    )


def _signing_kwargs() -> dict[str, object]:
    return {
        'domain_name': 'RISEx',
        'domain_version': '1',
        'chain_id': CHAIN_ID,
        'verifying_contract': AUTH,
        'router': ROUTER,
        'action_hash': ACTION_HASH,
        'nonce_anchor': NONCE_ANCHOR,
        'nonce_bitmap': NONCE_BITMAP,
        'deadline': DEADLINE,
    }


def test_verify_witness_signer_recovers_dedicated_signer() -> None:
    from app.security.risex_order_codec import build_verify_witness_typed_data
    from app.security.risex_verify_witness_signer import sign_verify_witness

    credential = _credential()
    signed = sign_verify_witness(credential, **_signing_kwargs())  # type: ignore[arg-type]

    typed_data = build_verify_witness_typed_data(
        account=credential.account_address,
        **_signing_kwargs(),  # type: ignore[arg-type]
    )
    signable_message = encode_typed_data(full_message=typed_data)
    signature = signed.signature_bytes_for_transport()

    assert len(signature) == 65
    assert signed.signer_address == credential.signer_address
    assert Account.recover_message(
        signable_message,
        signature=signature,
    ) == credential.signer_address


def test_verify_witness_signature_repr_does_not_expose_reusable_signature() -> None:
    from app.security.risex_verify_witness_signer import sign_verify_witness

    credential = _credential()
    signed = sign_verify_witness(credential, **_signing_kwargs())  # type: ignore[arg-type]
    signature = signed.signature_bytes_for_transport()
    rendered = repr(signed)

    assert signature.hex() not in rendered
    assert 'signature=' not in rendered
