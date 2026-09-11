from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_utils import keccak


ACTION_PLACE_ORDER = 'RISE_PERPS_PLACE_ORDER_V1'
V3_FLAG_PERMIT = 0x01
V3_FLAG_CLIENT_ID = 0x04
V3_FLAG_TTL = 0x10
_ORDER_HEADER_VERSION = 1


class RISExOrderCodecError(ValueError):
    """Raised when order or EIP-712 fixture input is not provably encodable."""


@dataclass(frozen=True, slots=True)
class RISExPlaceOrder:
    market_id: int
    size_steps: int
    price_ticks: int
    side: int
    post_only: bool
    reduce_only: bool
    stp_mode: int
    order_type: int
    time_in_force: int
    client_order_id: int = 0
    ttl_units: int = 0


def _require_uint(name: str, value: int, bits: int, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value >= (1 << bits):
        raise RISExOrderCodecError(
            f'{name} must be an integer in [{minimum}, {(1 << bits) - 1}]'
        )
    return value


def _require_enum(name: str, value: int, allowed: set[int]) -> int:
    if type(value) is not int or value not in allowed:
        allowed_text = ', '.join(str(item) for item in sorted(allowed))
        raise RISExOrderCodecError(f'{name} must be one of: {allowed_text}')
    return value


def _require_bool(name: str, value: bool) -> bool:
    if type(value) is not bool:
        raise RISExOrderCodecError(f'{name} must be a boolean')
    return value


def _validate_order(order: RISExPlaceOrder) -> None:
    _require_uint('market_id', order.market_id, 16)
    _require_uint('size_steps', order.size_steps, 32, minimum=1)
    _require_uint('price_ticks', order.price_ticks, 24)
    _require_enum('side', order.side, {0, 1})
    _require_bool('post_only', order.post_only)
    _require_bool('reduce_only', order.reduce_only)
    _require_enum('stp_mode', order.stp_mode, {0, 1, 2, 3})
    _require_enum('order_type', order.order_type, {0, 1})
    _require_enum('time_in_force', order.time_in_force, {0, 1, 2, 3})
    _require_uint('client_order_id', order.client_order_id, 64)
    _require_uint('ttl_units', order.ttl_units, 16)


def encode_order_data_88(order: RISExPlaceOrder) -> int:
    """Encode only the current 88-bit RISEx order core; no signing or I/O."""

    _validate_order(order)

    order_flags = order.side
    if order.post_only:
        order_flags |= 0x02
    if order.reduce_only:
        order_flags |= 0x04
    order_flags |= order.stp_mode << 3
    order_flags |= order.order_type << 5
    order_flags |= order.time_in_force << 6

    encoded = 0
    encoded |= order.market_id << 70
    encoded |= order.size_steps << 38
    encoded |= order.price_ticks << 14
    encoded |= order_flags << 6
    encoded |= _ORDER_HEADER_VERSION << 1

    if encoded >= (1 << 88):
        raise RISExOrderCodecError('encoded order data exceeds uint88')
    return encoded


def compute_protocol_header_flags(order: RISExPlaceOrder) -> int:
    """Return the builder-free V3 header flags accepted by the v1 harness."""

    _validate_order(order)
    flags = V3_FLAG_PERMIT
    if order.client_order_id != 0:
        flags |= V3_FLAG_CLIENT_ID
    if order.ttl_units != 0:
        flags |= V3_FLAG_TTL
    return flags


def _abi_word(value: int) -> bytes:
    return value.to_bytes(32, byteorder='big', signed=False)


def build_place_order_action_hash(order: RISExPlaceOrder) -> str:
    """Build the builder-free RISE_PERPS_PLACE_ORDER_V1 action hash.

    The v1 TRAXION harness deliberately fixes builder_id to zero and omits a
    builder-fee word. This function is pure and cannot sign or submit an order.
    """

    order_data = encode_order_data_88(order)
    header_flags = compute_protocol_header_flags(order)
    action_type_hash = keccak(text=ACTION_PLACE_ORDER)

    encoded = b''.join(
        (
            action_type_hash,
            _abi_word(header_flags),
            _abi_word(order_data),
            _abi_word(0),  # builder_id is disabled in TRAXION RISEx v1
            _abi_word(order.client_order_id),
            _abi_word(order.ttl_units),
        )
    )
    return '0x' + keccak(encoded).hex()


def _require_address(name: str, value: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        raise RISExOrderCodecError(f'{name} must be a 20-byte 0x-prefixed address')
    try:
        numeric = int(value[2:], 16)
    except ValueError as exc:
        raise RISExOrderCodecError(
            f'{name} must be a 20-byte 0x-prefixed address'
        ) from exc
    if numeric == 0:
        raise RISExOrderCodecError(f'{name} must not be the zero address')
    return value


def _require_bytes32(name: str, value: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith('0x'):
        raise RISExOrderCodecError(f'{name} must be a 32-byte 0x-prefixed hex value')
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise RISExOrderCodecError(
            f'{name} must be a 32-byte 0x-prefixed hex value'
        ) from exc
    return value


def build_verify_witness_typed_data(
    *,
    domain_name: str,
    domain_version: str,
    chain_id: int,
    verifying_contract: str,
    account: str,
    router: str,
    action_hash: str,
    nonce_anchor: int,
    nonce_bitmap: int,
    deadline: int,
) -> dict[str, Any]:
    """Build an unsigned VerifyWitness EIP-712 fixture from live runtime identity."""

    if not isinstance(domain_name, str) or not domain_name.strip():
        raise RISExOrderCodecError('domain_name must be non-empty')
    if not isinstance(domain_version, str) or not domain_version.strip():
        raise RISExOrderCodecError('domain_version must be non-empty')

    _require_uint('chain_id', chain_id, 256, minimum=1)
    verifying_contract = _require_address('verifying_contract', verifying_contract)
    account = _require_address('account', account)
    router = _require_address('router', router)
    action_hash = _require_bytes32('action_hash', action_hash)
    _require_uint('nonce_anchor', nonce_anchor, 48)
    _require_uint('nonce_bitmap', nonce_bitmap, 8)
    _require_uint('deadline', deadline, 32, minimum=1)

    return {
        'types': {
            'EIP712Domain': [
                {'name': 'name', 'type': 'string'},
                {'name': 'version', 'type': 'string'},
                {'name': 'chainId', 'type': 'uint256'},
                {'name': 'verifyingContract', 'type': 'address'},
            ],
            'VerifyWitness': [
                {'name': 'account', 'type': 'address'},
                {'name': 'target', 'type': 'address'},
                {'name': 'hash', 'type': 'bytes32'},
                {'name': 'nonceAnchor', 'type': 'uint48'},
                {'name': 'nonceBitmap', 'type': 'uint8'},
                {'name': 'deadline', 'type': 'uint32'},
            ],
        },
        'primaryType': 'VerifyWitness',
        'domain': {
            'name': domain_name,
            'version': domain_version,
            'chainId': chain_id,
            'verifyingContract': verifying_contract,
        },
        'message': {
            'account': account,
            'target': router,
            'hash': action_hash,
            'nonceAnchor': nonce_anchor,
            'nonceBitmap': nonce_bitmap,
            'deadline': deadline,
        },
    }
