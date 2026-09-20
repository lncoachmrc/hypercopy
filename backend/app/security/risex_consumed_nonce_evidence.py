from __future__ import annotations

from dataclasses import dataclass

from app.adapters.risex_types import ProviderDataMalformed, ProviderReadUnavailable
from app.security.risex_deployment_runtime import PublicRPCTransport
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


IS_NONCE_USED_SELECTOR = '0xdcd621a2'
GET_NONCE_STATE_SELECTOR = '0x8c1009b5'


@dataclass(frozen=True, slots=True)
class RISExConsumedNonceEvidence:
    """Read-only, same-block evidence for one RISEx permit nonce after submission."""

    block_number: int
    block_tag: str
    nonce_anchor: int
    nonce_bitmap_index: int
    is_nonce_used: bool
    state_anchor: int
    state_bitmap: int
    bitmap_consistent: bool


def _is_address(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        return int(value[2:], 16) != 0
    except ValueError:
        return False


def _abi_address(value: str) -> bytes:
    if not _is_address(value):
        raise SignedTestnetBlocked('RISEx consumed-nonce account address is invalid')
    return bytes.fromhex(value[2:]).rjust(32, bytes([0]))


def _abi_uint(value: int, *, bits: int, field_name: str) -> bytes:
    if type(value) is not int or value < 0 or value >= (1 << bits):
        raise SignedTestnetBlocked(
            f'RISEx consumed-nonce {field_name} is outside uint{bits}'
        )
    return value.to_bytes(32, byteorder='big', signed=False)


def _call_data(selector: str, *words: bytes) -> str:
    return '0x' + (bytes.fromhex(selector[2:]) + b''.join(words)).hex()


def _hex_bytes(value: object, *, field_name: str) -> bytes:
    if not isinstance(value, str) or not value.startswith('0x'):
        raise ProviderDataMalformed(f'RISEx {field_name} is not canonical hex')
    raw = value[2:]
    if len(raw) % 2:
        raise ProviderDataMalformed(f'RISEx {field_name} has odd-length hex')
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise ProviderDataMalformed(f'RISEx {field_name} is not valid hex') from exc


def _decode_two_uints(value: object, *, field_name: str) -> tuple[int, int]:
    raw = _hex_bytes(value, field_name=field_name)
    if len(raw) != 64:
        raise ProviderDataMalformed(
            f'RISEx {field_name} must return exactly two ABI words'
        )
    return int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')


def _decode_bool(value: object, *, field_name: str) -> bool:
    raw = _hex_bytes(value, field_name=field_name)
    if len(raw) != 32:
        raise ProviderDataMalformed(f'RISEx {field_name} must return one ABI word')
    parsed = int.from_bytes(raw, 'big')
    if parsed not in (0, 1):
        raise ProviderDataMalformed(f'RISEx {field_name} returned a non-boolean value')
    return parsed == 1


def _parse_block_number(value: object) -> int:
    if not isinstance(value, str) or not value.startswith('0x'):
        raise ProviderDataMalformed('RISEx eth_blockNumber is not canonical hex')
    try:
        block_number = int(value, 16)
    except ValueError as exc:
        raise ProviderDataMalformed('RISEx eth_blockNumber is not valid hex') from exc
    if block_number < 0:
        raise ProviderDataMalformed('RISEx eth_blockNumber is negative')
    return block_number


async def collect_consumed_nonce_evidence(
    rpc: PublicRPCTransport,
    *,
    authorization_address: str,
    account: str,
    nonce_anchor: int,
    nonce_bitmap_index: int,
) -> RISExConsumedNonceEvidence:
    """Collect both nonce reads at one explicit block tag without any write path."""

    if getattr(rpc, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx consumed-nonce collector requires a read-only RPC transport'
        )
    if not _is_address(authorization_address):
        raise SignedTestnetBlocked(
            'RISEx consumed-nonce Authorization address is invalid'
        )

    account_word = _abi_address(account)
    anchor_word = _abi_uint(nonce_anchor, bits=48, field_name='nonce anchor')
    bitmap_index_word = _abi_uint(
        nonce_bitmap_index,
        bits=8,
        field_name='nonce bitmap index',
    )

    block_number = _parse_block_number(await rpc.call('eth_blockNumber', []))
    block_tag = hex(block_number)

    nonce_used_raw = await rpc.call(
        'eth_call',
        [
            {
                'to': authorization_address,
                'data': _call_data(
                    IS_NONCE_USED_SELECTOR,
                    account_word,
                    anchor_word,
                    bitmap_index_word,
                ),
            },
            block_tag,
        ],
    )
    nonce_state_raw = await rpc.call(
        'eth_call',
        [
            {
                'to': authorization_address,
                'data': _call_data(GET_NONCE_STATE_SELECTOR, account_word),
            },
            block_tag,
        ],
    )

    is_nonce_used = _decode_bool(nonce_used_raw, field_name='isNonceUsed')
    state_anchor, state_bitmap = _decode_two_uints(
        nonce_state_raw,
        field_name='getNonceState',
    )
    relevant_bit_set = bool(state_bitmap & (1 << nonce_bitmap_index))
    bitmap_consistent = (
        state_anchor == nonce_anchor
        and relevant_bit_set == is_nonce_used
    )

    return RISExConsumedNonceEvidence(
        block_number=block_number,
        block_tag=block_tag,
        nonce_anchor=nonce_anchor,
        nonce_bitmap_index=nonce_bitmap_index,
        is_nonce_used=is_nonce_used,
        state_anchor=state_anchor,
        state_bitmap=state_bitmap,
        bitmap_consistent=bitmap_consistent,
    )
