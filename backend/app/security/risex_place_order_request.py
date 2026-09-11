from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


@dataclass(frozen=True, slots=True)
class RISExPreparedPlaceOrderRequest:
    """Offline builder-free request whose wire order is bound to its signed permit."""

    order: RISExPlaceOrder
    permit: RISExPreparedPlaceOrderPermit
    post_allowed: Literal[False] = False

    def json_for_testnet_transport(self) -> dict[str, object]:
        return {
            'market_id': self.order.market_id,
            'size_steps': self.order.size_steps,
            'price_ticks': self.order.price_ticks,
            'side': self.order.side,
            'post_only': self.order.post_only,
            'reduce_only': self.order.reduce_only,
            'stp_mode': self.order.stp_mode,
            'order_type': self.order.order_type,
            'time_in_force': self.order.time_in_force,
            'builder_id': 0,
            'client_order_id': str(self.order.client_order_id),
            'ttl_units': self.order.ttl_units,
            'permit': self.permit.permit_for_testnet_transport(),
        }


def prepare_place_order_request(
    *,
    order: RISExPlaceOrder,
    permit: RISExPreparedPlaceOrderPermit,
) -> RISExPreparedPlaceOrderRequest:
    """Bind the exact serialized order fields to the action hash already signed."""

    if not isinstance(order, RISExPlaceOrder):
        raise SignedTestnetBlocked('RISEx place-order request requires a typed order')
    if not isinstance(permit, RISExPreparedPlaceOrderPermit):
        raise SignedTestnetBlocked('RISEx place-order request requires a prepared permit')

    expected_action_hash = build_place_order_action_hash(order)
    if permit.action_hash.lower() != expected_action_hash.lower():
        raise SignedTestnetBlocked(
            'RISEx place-order action hash does not match the signed permit'
        )

    return RISExPreparedPlaceOrderRequest(order=order, permit=permit)
