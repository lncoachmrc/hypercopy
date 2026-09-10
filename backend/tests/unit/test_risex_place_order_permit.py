from __future__ import annotations

from base64 import b64decode

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from app.security.risex_nonce_state import RISExOrderNonceSelection
from app.security.risex_order_codec import (
    RISExPlaceOrder,
    build_place_order_action_hash,
    build_verify_witness_typed_data,
)
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_testnet_signer import load_testnet_signer_credential


CHAIN_ID = 11155931
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)
BLOCK_TIMESTAMP = 1_800_000_000
SESSION_EXPIRATION = BLOCK_TIMESTAMP + 3_600
DEADLINE = BLOCK_TIMESTAMP + 300


def _credential():
    signer_key = bytes([7]) * 32
    return load_testnet_signer_credential(
        {
            'RISEX_TESTNET_ACCOUNT_ADDRESS': '0x' + ('11' * 20),
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': '0x' + signer_key.hex(),
        }
    )


def _order() -> RISExPlaceOrder:
    return RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=0,
        client_order_id=7,
        ttl_units=0,
    )


def _nonce(*, rolled: bool = True) -> RISExOrderNonceSelection:
    if rolled:
        return RISExOrderNonceSelection(
            observed_nonce_anchor=42,
            observed_bitmap_index=208,
            selected_nonce_anchor=43,
            selected_bitmap_index=0,
            rolled_anchor=True,
        )
    return RISExOrderNonceSelection(
        observed_nonce_anchor=42,
        observed_bitmap_index=17,
        selected_nonce_anchor=42,
        selected_bitmap_index=17,
        rolled_anchor=False,
    )


def _prepare(**overrides: object):
    from app.security.risex_place_order_permit import prepare_place_order_permit

    values: dict[str, object] = {
        'order': _order(),
        'credential': _credential(),
        'nonce_selection': _nonce(),
        'domain_name': 'RISEx',
        'domain_version': '1',
        'chain_id': CHAIN_ID,
        'verifying_contract': AUTH,
        'router': ROUTER,
        'observed_block_timestamp': BLOCK_TIMESTAMP,
        'session_expiration': SESSION_EXPIRATION,
        'deadline': DEADLINE,
    }
    values.update(overrides)
    return prepare_place_order_permit(**values)  # type: ignore[arg-type]


def test_place_order_permit_binds_order_nonce_deadline_and_dedicated_signer() -> None:
    credential = _credential()
    order = _order()
    prepared = _prepare(order=order, credential=credential)
    permit = prepared.permit_for_testnet_transport()

    assert prepared.action_hash == build_place_order_action_hash(order)
    assert prepared.post_allowed is False
    assert permit['account'] == credential.account_address
    assert permit['signer'] == credential.signer_address
    assert permit['nonce_anchor'] == 43
    assert permit['nonce_bitmap_index'] == 0
    assert permit['deadline'] == DEADLINE

    signature = b64decode(str(permit['signature']), validate=True)
    assert len(signature) == 65
    assert str(permit['signature']) not in repr(prepared)
    assert 'signature=' not in repr(prepared)

    typed_data = build_verify_witness_typed_data(
        domain_name='RISEx',
        domain_version='1',
        chain_id=CHAIN_ID,
        verifying_contract=AUTH,
        account=credential.account_address,
        router=ROUTER,
        action_hash=prepared.action_hash,
        nonce_anchor=43,
        nonce_bitmap=0,
        deadline=DEADLINE,
    )
    recovered = Account.recover_message(
        encode_typed_data(full_message=typed_data),
        signature=signature,
    )
    assert recovered == credential.signer_address


def test_place_order_permit_rejects_deadline_not_after_observed_block() -> None:
    with pytest.raises(SignedTestnetBlocked, match='deadline.*observed block'):
        _prepare(deadline=BLOCK_TIMESTAMP)


def test_place_order_permit_rejects_deadline_after_session_expiration() -> None:
    with pytest.raises(SignedTestnetBlocked, match='deadline.*session expiration'):
        _prepare(deadline=SESSION_EXPIRATION + 1)


@pytest.mark.parametrize(
    'nonce_selection',
    [
        RISExOrderNonceSelection(42, 17, 43, 17, False),
        RISExOrderNonceSelection(42, 208, 42, 208, False),
        RISExOrderNonceSelection(42, 208, 44, 0, True),
        RISExOrderNonceSelection(42, 17, 43, 0, True),
    ],
)
def test_place_order_permit_rejects_tampered_nonce_selection(
    nonce_selection: RISExOrderNonceSelection,
) -> None:
    with pytest.raises(SignedTestnetBlocked, match='nonce selection'):
        _prepare(nonce_selection=nonce_selection)
