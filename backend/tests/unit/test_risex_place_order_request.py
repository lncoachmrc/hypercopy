from __future__ import annotations

from base64 import b64encode

import pytest

from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
SIGNATURE = bytes([9]) * 65
DEADLINE = 1_800_000_300


def _order(**overrides: object) -> RISExPlaceOrder:
    values: dict[str, object] = {
        'market_id': 1,
        'size_steps': 100,
        'price_ticks': 50_000,
        'side': 0,
        'post_only': False,
        'reduce_only': False,
        'stp_mode': 0,
        'order_type': 1,
        'time_in_force': 0,
        'client_order_id': 7,
        'ttl_units': 0,
    }
    values.update(overrides)
    return RISExPlaceOrder(**values)  # type: ignore[arg-type]


def _permit(order: RISExPlaceOrder, **overrides: object) -> RISExPreparedPlaceOrderPermit:
    values: dict[str, object] = {
        'account_address': ACCOUNT,
        'signer_address': SIGNER,
        'action_hash': build_place_order_action_hash(order),
        'nonce_anchor': 43,
        'nonce_bitmap_index': 0,
        'deadline': DEADLINE,
        '_signature': SIGNATURE,
    }
    values.update(overrides)
    return RISExPreparedPlaceOrderPermit(**values)  # type: ignore[arg-type]


def test_place_order_request_serializes_exact_builder_free_permit_flow() -> None:
    from app.security.risex_place_order_request import prepare_place_order_request

    order = _order()
    request = prepare_place_order_request(order=order, permit=_permit(order))

    assert request.post_allowed is False
    assert request.json_for_testnet_transport() == {
        'market_id': 1,
        'size_steps': 100,
        'price_ticks': 50_000,
        'side': 0,
        'post_only': False,
        'reduce_only': False,
        'stp_mode': 0,
        'order_type': 1,
        'time_in_force': 0,
        'builder_id': 0,
        'client_order_id': '7',
        'ttl_units': 0,
        'permit': {
            'account': ACCOUNT,
            'signer': SIGNER,
            'nonce_anchor': 43,
            'nonce_bitmap_index': 0,
            'deadline': DEADLINE,
            'signature': b64encode(SIGNATURE).decode('ascii'),
        },
    }
    assert 'builder_fee_bps' not in request.json_for_testnet_transport()
    assert b64encode(SIGNATURE).decode('ascii') not in repr(request)


def test_place_order_request_rejects_order_not_bound_to_signed_action_hash() -> None:
    from app.security.risex_place_order_request import prepare_place_order_request

    signed_order = _order()
    changed_order = _order(size_steps=101)

    with pytest.raises(SignedTestnetBlocked, match='action hash'):
        prepare_place_order_request(order=changed_order, permit=_permit(signed_order))


def test_place_order_request_rejects_nonzero_builder_path_by_construction() -> None:
    from app.security.risex_place_order_request import prepare_place_order_request

    order = _order()
    request = prepare_place_order_request(order=order, permit=_permit(order))
    payload = request.json_for_testnet_transport()

    assert payload['builder_id'] == 0
    assert 'builder_fee_bps' not in payload
