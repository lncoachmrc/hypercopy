from __future__ import annotations

from dataclasses import replace

import pytest
from eth_abi import encode
from eth_utils import keccak

from app.security.risex_order_codec import (
    RISExOrderCodecError,
    RISExPlaceOrder,
    build_place_order_action_hash,
    build_verify_witness_typed_data,
    compute_protocol_header_flags,
    encode_order_data_88,
)


ACCOUNT = '0x1111111111111111111111111111111111111111'
ROUTER = '0x2222222222222222222222222222222222222222'
AUTH = '0x3333333333333333333333333333333333333333'
CHAIN_ID = 11155931
ACTION = 'RISE_PERPS_PLACE_ORDER_V1'


def _order() -> RISExPlaceOrder:
    return RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=500_000,
        side=1,
        post_only=True,
        reduce_only=False,
        stp_mode=2,
        order_type=1,
        time_in_force=3,
        client_order_id=42,
        ttl_units=7,
    )


def test_encode_order_data_matches_risex_88_bit_layout() -> None:
    order = _order()
    order_flags = 1 | 0x02 | (2 << 3) | (1 << 5) | (3 << 6)
    expected = (
        (1 << 70)
        | (100 << 38)
        | (500_000 << 14)
        | (order_flags << 6)
        | (1 << 1)
    )

    encoded = encode_order_data_88(order)

    assert encoded == expected
    assert 0 <= encoded < (1 << 88)


def test_place_order_hash_matches_builder_free_v3_abi_fixture() -> None:
    order = _order()
    order_data = encode_order_data_88(order)
    protocol_flags = 0x01 | 0x04 | 0x10  # permit + client id + ttl; no builder
    action_type_hash = keccak(text=ACTION)
    expected = '0x' + keccak(
        encode(
            ['bytes32', 'uint8', 'uint88', 'uint16', 'uint64', 'uint16'],
            [action_type_hash, protocol_flags, order_data, 0, 42, 7],
        )
    ).hex()

    assert compute_protocol_header_flags(order) == protocol_flags
    assert build_place_order_action_hash(order) == expected


def test_verify_witness_fixture_uses_live_domain_and_router_target_without_signing() -> None:
    action_hash = build_place_order_action_hash(_order())

    fixture = build_verify_witness_typed_data(
        domain_name='RISEx',
        domain_version='1',
        chain_id=CHAIN_ID,
        verifying_contract=AUTH,
        account=ACCOUNT,
        router=ROUTER,
        action_hash=action_hash,
        nonce_anchor=9,
        nonce_bitmap=3,
        deadline=1_800_000_000,
    )

    assert fixture['primaryType'] == 'VerifyWitness'
    assert fixture['domain'] == {
        'name': 'RISEx',
        'version': '1',
        'chainId': CHAIN_ID,
        'verifyingContract': AUTH,
    }
    assert fixture['message'] == {
        'account': ACCOUNT,
        'target': ROUTER,
        'hash': action_hash,
        'nonceAnchor': 9,
        'nonceBitmap': 3,
        'deadline': 1_800_000_000,
    }
    assert fixture['types']['VerifyWitness'] == [
        {'name': 'account', 'type': 'address'},
        {'name': 'target', 'type': 'address'},
        {'name': 'hash', 'type': 'bytes32'},
        {'name': 'nonceAnchor', 'type': 'uint48'},
        {'name': 'nonceBitmap', 'type': 'uint8'},
        {'name': 'deadline', 'type': 'uint32'},
    ]


def test_codec_rejects_out_of_range_or_unsupported_values_instead_of_masking() -> None:
    base = _order()
    invalid_orders = [
        replace(base, market_id=1 << 16),
        replace(base, size_steps=0),
        replace(base, size_steps=1 << 32),
        replace(base, price_ticks=1 << 24),
        replace(base, side=2),
        replace(base, stp_mode=4),
        replace(base, order_type=2),
        replace(base, time_in_force=4),
        replace(base, client_order_id=1 << 64),
        replace(base, ttl_units=1 << 16),
    ]

    for invalid in invalid_orders:
        with pytest.raises(RISExOrderCodecError):
            encode_order_data_88(invalid)


def test_verify_witness_fixture_rejects_invalid_domain_or_nonce_fields() -> None:
    action_hash = build_place_order_action_hash(_order())

    with pytest.raises(RISExOrderCodecError):
        build_verify_witness_typed_data(
            domain_name='RISEx',
            domain_version='1',
            chain_id=0,
            verifying_contract=AUTH,
            account=ACCOUNT,
            router=ROUTER,
            action_hash=action_hash,
            nonce_anchor=9,
            nonce_bitmap=3,
            deadline=1_800_000_000,
        )

    with pytest.raises(RISExOrderCodecError):
        build_verify_witness_typed_data(
            domain_name='RISEx',
            domain_version='1',
            chain_id=CHAIN_ID,
            verifying_contract=AUTH,
            account=ACCOUNT,
            router=ROUTER,
            action_hash='0x1234',
            nonce_anchor=1 << 48,
            nonce_bitmap=3,
            deadline=1_800_000_000,
        )
