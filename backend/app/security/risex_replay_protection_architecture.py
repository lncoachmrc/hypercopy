from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from app.adapters.risex_types import ProviderDataMalformed, ProviderReadUnavailable
from app.security.risex_authorization_session import collect_authorization_session_evidence
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    PublicAPITransport,
    PublicRPCTransport,
    collect_runtime_deployment_evidence,
)
from app.security.risex_nonce_state import collect_order_nonce_selection
from app.security.risex_order_codec import (
    build_place_order_action_hash,
    build_verify_witness_typed_data,
)
from app.security.risex_place_order_request import RISExPreparedPlaceOrderRequest
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


IS_NONCE_USED_SELECTOR = '0xdcd621a2'
GET_NONCE_STATE_SELECTOR = '0x8c1009b5'
VERIFY_WITNESS_TYPEHASH_SELECTOR = '0x8110edc1'
VERIFY_WITNESS_TYPE_STRING = (
    'VerifyWitness(address account,address target,bytes32 hash,uint48 nonceAnchor,'
    'uint8 nonceBitmap,uint32 deadline)'
)
VERIFY_WITNESS_TYPEHASH = (
    '0x055e6bcbf2ba5ff1c2ba5dc95b6648a5de6aaab3185251a34e3b88c11e116821'
)
_REPLAY_ARCHITECTURE_SEAL = object()


def _is_address(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        return False
    try:
        return int(value[2:], 16) != 0
    except ValueError:
        return False


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


def _abi_address(value: str) -> bytes:
    if not _is_address(value):
        raise SignedTestnetBlocked('RISEx replay architecture account address is invalid')
    return bytes.fromhex(value[2:]).rjust(32, bytes([0]))


def _abi_uint(value: int, *, bits: int, field_name: str) -> bytes:
    if type(value) is not int or value < 0 or value >= (1 << bits):
        raise SignedTestnetBlocked(
            f'RISEx replay architecture {field_name} is outside uint{bits}'
        )
    return value.to_bytes(32, byteorder='big', signed=False)


def _call_data(selector: str, *words: bytes) -> str:
    return '0x' + (bytes.fromhex(selector[2:]) + b''.join(words)).hex()


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


def _decode_bytes32(value: object, *, field_name: str) -> str:
    raw = _hex_bytes(value, field_name=field_name)
    if len(raw) != 32:
        raise ProviderDataMalformed(f'RISEx {field_name} must return bytes32')
    return '0x' + raw.hex()


def _attested_values(
    *,
    network: str,
    deployment_fingerprint: str,
    block_tag: str,
    chain_id: int,
    account_address: str,
    signer_address: str,
    authorization_address: str,
    authorization_implementation: str,
    router_address: str,
    nonce_anchor: int,
    nonce_bitmap_index: int,
    nonce_currently_unused: bool,
    deadline: int,
    action_hash: str,
    verify_witness_typehash: str,
    behavioral_replay_rejection_proven: bool,
) -> tuple[object, ...]:
    return (
        network,
        deployment_fingerprint,
        block_tag,
        chain_id,
        account_address.lower(),
        signer_address.lower(),
        authorization_address.lower(),
        authorization_implementation.lower(),
        router_address.lower(),
        nonce_anchor,
        nonce_bitmap_index,
        nonce_currently_unused,
        deadline,
        action_hash.lower(),
        verify_witness_typehash.lower(),
        behavioral_replay_rejection_proven,
    )


@dataclass(frozen=True, slots=True, init=False)
class RISExReplayProtectionArchitectureAttestation:
    """Sealed architectural evidence for one prepared RISEx VerifyWitness request.

    The attestation proves that the reviewed deployment fingerprint still matches,
    the runtime exposes the expected VerifyWitness typehash and bitmap-nonce read
    surface, the request's selected nonce is currently unused, and the permit
    signature binds the same action hash, nonce and deadline observed here.

    This is architectural and point-in-time evidence only. It does not prove
    that the provider rejects a second identical submission after the first
    submission is accepted. That behavioral replay-rejection proof is deliberately
    separate and remains false until an explicitly authorized post-order replay test
    is performed.
    """

    network: Literal['testnet']
    deployment_fingerprint: str
    block_tag: str
    chain_id: int
    account_address: str
    signer_address: str
    authorization_address: str
    authorization_implementation: str
    router_address: str
    nonce_anchor: int
    nonce_bitmap_index: int
    nonce_currently_unused: Literal[True]
    deadline: int
    action_hash: str
    verify_witness_typehash: str
    behavioral_replay_rejection_proven: Literal[False]
    _attestation_seal: object = field(repr=False, compare=False)
    _attested_values: tuple[object, ...] = field(repr=False, compare=False)

    def __copy__(self) -> None:
        raise TypeError('RISEx replay architecture attestation cannot be copied')

    def __deepcopy__(self, _memo: object) -> None:
        raise TypeError('RISEx replay architecture attestation cannot be copied')

    def __reduce_ex__(self, _protocol: int) -> None:
        raise TypeError('RISEx replay architecture attestation cannot be serialized')


def assert_replay_protection_architecture_attested(
    attestation: object,
    *,
    request: RISExPreparedPlaceOrderRequest | None = None,
    account_address: str,
    signer_address: str,
    chain_id: int,
    authorization_address: str,
    router_address: str,
) -> None:
    """Fail closed unless sealed architecture evidence matches identity and request."""

    if not isinstance(attestation, RISExReplayProtectionArchitectureAttestation):
        raise SignedTestnetBlocked(
            'RISEx replay protection requires sealed architectural evidence'
        )
    if getattr(attestation, '_attestation_seal', None) is not _REPLAY_ARCHITECTURE_SEAL:
        raise SignedTestnetBlocked('RISEx replay architecture attestation seal is invalid')

    current_values = _attested_values(
        network=attestation.network,
        deployment_fingerprint=attestation.deployment_fingerprint,
        block_tag=attestation.block_tag,
        chain_id=attestation.chain_id,
        account_address=attestation.account_address,
        signer_address=attestation.signer_address,
        authorization_address=attestation.authorization_address,
        authorization_implementation=attestation.authorization_implementation,
        router_address=attestation.router_address,
        nonce_anchor=attestation.nonce_anchor,
        nonce_bitmap_index=attestation.nonce_bitmap_index,
        nonce_currently_unused=attestation.nonce_currently_unused,
        deadline=attestation.deadline,
        action_hash=attestation.action_hash,
        verify_witness_typehash=attestation.verify_witness_typehash,
        behavioral_replay_rejection_proven=attestation.behavioral_replay_rejection_proven,
    )
    if getattr(attestation, '_attested_values', None) != current_values:
        raise SignedTestnetBlocked('RISEx replay architecture attestation was tampered with')

    if (
        attestation.network != 'testnet'
        or attestation.nonce_currently_unused is not True
        or attestation.behavioral_replay_rejection_proven is not False
        or attestation.verify_witness_typehash.lower() != VERIFY_WITNESS_TYPEHASH.lower()
    ):
        raise SignedTestnetBlocked('RISEx replay architecture attestation is invalid')

    identity = (
        (attestation.account_address, account_address, 'account'),
        (attestation.signer_address, signer_address, 'signer'),
        (attestation.authorization_address, authorization_address, 'authorization'),
        (attestation.router_address, router_address, 'router'),
    )
    for observed, expected, label in identity:
        if (
            not _is_address(expected)
            or not _is_address(observed)
            or observed.lower() != expected.lower()
        ):
            raise SignedTestnetBlocked(
                f'RISEx replay architecture {label} identity does not match'
            )

    if (
        type(chain_id) is not int
        or chain_id <= 0
        or attestation.chain_id != chain_id
    ):
        raise SignedTestnetBlocked('RISEx replay architecture chain identity does not match')

    if request is None:
        return
    if not isinstance(request, RISExPreparedPlaceOrderRequest):
        raise SignedTestnetBlocked(
            'RISEx replay architecture requires a typed place-order request'
        )

    expected_action_hash = build_place_order_action_hash(request.order)
    permit = request.permit
    request_matches = (
        permit.account_address.lower() == attestation.account_address.lower()
        and permit.signer_address.lower() == attestation.signer_address.lower()
        and permit.action_hash.lower() == attestation.action_hash.lower()
        and expected_action_hash.lower() == attestation.action_hash.lower()
        and permit.nonce_anchor == attestation.nonce_anchor
        and permit.nonce_bitmap_index == attestation.nonce_bitmap_index
        and permit.deadline == attestation.deadline
    )
    if not request_matches:
        raise SignedTestnetBlocked(
            'RISEx replay architecture attestation does not match the prepared request'
        )


async def collect_replay_protection_architecture_attestation(
    *,
    api: PublicAPITransport,
    rpc: PublicRPCTransport,
    request: RISExPreparedPlaceOrderRequest,
    expected_fingerprint: str = PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
) -> RISExReplayProtectionArchitectureAttestation:
    """Collect sealed architectural anti-replay evidence with read-only I/O only.

    The collector proves the pinned deployment identity is unchanged, the live
    Authorization implementation exposes isNonceUsed(address,uint48,uint8),
    getNonceState(address) and the expected VERIFY_WITNESS_TYPEHASH, the
    selected nonce is currently unused in both API/on-chain state, and the signed
    permit is coherent with that nonce, deadline and action hash.

    It does not prove that the provider rejects a second identical submission
    after accepting the first. That is behavioral replay-rejection evidence and is
    intentionally outside this collector and outside the first-order authorization.
    """

    if getattr(api, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx replay architecture collector requires a public read-only API'
        )
    if getattr(rpc, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx replay architecture collector requires a read-only RPC transport'
        )
    if not isinstance(request, RISExPreparedPlaceOrderRequest):
        raise SignedTestnetBlocked(
            'RISEx replay architecture collector requires a typed place-order request'
        )

    deployment = await collect_runtime_deployment_evidence(api, rpc, network='testnet')
    preflight = evaluate_pinned_deployment_preflight(
        deployment,
        expected_fingerprint=expected_fingerprint,
    )
    if (
        preflight.verdict != 'PASS'
        or preflight.deployment_identity_verified is not True
        or preflight.observed_fingerprint is None
        or preflight.observed_fingerprint.lower() != expected_fingerprint.lower()
    ):
        raise SignedTestnetBlocked(
            'RISEx replay architecture blocked because deployment identity drifted'
        )

    if (
        deployment.block_number is None
        or deployment.api_chain_id is None
        or deployment.domain_name is None
        or deployment.domain_version is None
        or deployment.domain_verifying_contract is None
        or deployment.system_router is None
        or deployment.auth.implementation is None
    ):
        raise SignedTestnetBlocked(
            'RISEx replay architecture deployment evidence is incomplete'
        )
    block_tag = hex(deployment.block_number)

    implementation_code_raw = await rpc.call(
        'eth_getCode',
        [deployment.auth.implementation, block_tag],
    )
    implementation_code = _hex_bytes(
        implementation_code_raw,
        field_name='Authorization implementation runtime code',
    )
    implementation_hex = implementation_code.hex()
    required_runtime_values = {
        'isNonceUsed selector': IS_NONCE_USED_SELECTOR[2:],
        'getNonceState selector': GET_NONCE_STATE_SELECTOR[2:],
        'VERIFY_WITNESS_TYPEHASH selector': VERIFY_WITNESS_TYPEHASH_SELECTOR[2:],
        'VerifyWitness typehash': VERIFY_WITNESS_TYPEHASH[2:],
    }
    for label, expected_hex in required_runtime_values.items():
        if expected_hex.lower() not in implementation_hex:
            raise SignedTestnetBlocked(
                f'RISEx replay architecture runtime is missing {label}'
            )

    typehash_call = await rpc.call(
        'eth_call',
        [
            {
                'to': deployment.domain_verifying_contract,
                'data': VERIFY_WITNESS_TYPEHASH_SELECTOR,
            },
            block_tag,
        ],
    )
    runtime_typehash = _decode_bytes32(
        typehash_call,
        field_name='VERIFY_WITNESS_TYPEHASH',
    )
    if runtime_typehash.lower() != VERIFY_WITNESS_TYPEHASH.lower():
        raise SignedTestnetBlocked(
            'RISEx replay architecture VerifyWitness typehash does not match'
        )

    permit = request.permit
    nonce_selection = await collect_order_nonce_selection(
        api,
        account=permit.account_address,
    )
    if (
        nonce_selection.selected_nonce_anchor != permit.nonce_anchor
        or nonce_selection.selected_bitmap_index != permit.nonce_bitmap_index
    ):
        raise SignedTestnetBlocked(
            'RISEx replay architecture permit nonce is not the current selected nonce'
        )

    chain_nonce_state_raw = await rpc.call(
        'eth_call',
        [
            {
                'to': deployment.domain_verifying_contract,
                'data': _call_data(
                    GET_NONCE_STATE_SELECTOR,
                    _abi_address(permit.account_address),
                ),
            },
            block_tag,
        ],
    )
    chain_anchor, chain_bitmap = _decode_two_uints(
        chain_nonce_state_raw,
        field_name='getNonceState',
    )
    if (
        chain_anchor != nonce_selection.observed_nonce_anchor
        or chain_bitmap != nonce_selection.observed_bitmap
    ):
        raise SignedTestnetBlocked(
            'RISEx replay architecture API and on-chain nonce state disagree'
        )

    nonce_used_raw = await rpc.call(
        'eth_call',
        [
            {
                'to': deployment.domain_verifying_contract,
                'data': _call_data(
                    IS_NONCE_USED_SELECTOR,
                    _abi_address(permit.account_address),
                    _abi_uint(permit.nonce_anchor, bits=48, field_name='nonce anchor'),
                    _abi_uint(
                        permit.nonce_bitmap_index,
                        bits=8,
                        field_name='nonce bitmap index',
                    ),
                ),
            },
            block_tag,
        ],
    )
    if _decode_bool(nonce_used_raw, field_name='isNonceUsed'):
        raise SignedTestnetBlocked(
            'RISEx replay architecture selected nonce is already used'
        )

    authorization = await collect_authorization_session_evidence(
        rpc,
        authorization_address=deployment.domain_verifying_contract,
        account=permit.account_address,
        signer=permit.signer_address,
        block_tag=block_tag,
    )
    if not (
        authorization.block_timestamp < permit.deadline <= authorization.session_expiration
    ):
        raise SignedTestnetBlocked(
            'RISEx replay architecture permit deadline is outside the live session window'
        )

    expected_action_hash = build_place_order_action_hash(request.order)
    if permit.action_hash.lower() != expected_action_hash.lower():
        raise SignedTestnetBlocked(
            'RISEx replay architecture action hash does not match the signed permit'
        )

    expected_type_fields = [
        {'name': 'account', 'type': 'address'},
        {'name': 'target', 'type': 'address'},
        {'name': 'hash', 'type': 'bytes32'},
        {'name': 'nonceAnchor', 'type': 'uint48'},
        {'name': 'nonceBitmap', 'type': 'uint8'},
        {'name': 'deadline', 'type': 'uint32'},
    ]
    typed_data = build_verify_witness_typed_data(
        domain_name=deployment.domain_name,
        domain_version=deployment.domain_version,
        chain_id=deployment.api_chain_id,
        verifying_contract=deployment.domain_verifying_contract,
        account=permit.account_address,
        router=deployment.system_router,
        action_hash=permit.action_hash,
        nonce_anchor=permit.nonce_anchor,
        nonce_bitmap=permit.nonce_bitmap_index,
        deadline=permit.deadline,
    )
    witness_fields = typed_data.get('types', {}).get('VerifyWitness')
    if witness_fields != expected_type_fields:
        raise SignedTestnetBlocked(
            'RISEx replay architecture VerifyWitness signing schema changed'
        )
    schema_type_string = 'VerifyWitness(' + ','.join(
        f"{field['type']} {field['name']}" for field in witness_fields
    ) + ')'
    schema_typehash = '0x' + keccak(text=schema_type_string).hex()
    if (
        schema_type_string != VERIFY_WITNESS_TYPE_STRING
        or schema_typehash.lower() != runtime_typehash.lower()
    ):
        raise SignedTestnetBlocked(
            'RISEx replay architecture VerifyWitness signing typehash does not match runtime'
        )

    recovered = Account.recover_message(
        encode_typed_data(full_message=typed_data),
        signature=permit._signature,
    )
    if recovered.lower() != permit.signer_address.lower():
        raise SignedTestnetBlocked(
            'RISEx replay architecture permit signature does not match the signer'
        )

    values = _attested_values(
        network='testnet',
        deployment_fingerprint=preflight.observed_fingerprint,
        block_tag=block_tag,
        chain_id=deployment.api_chain_id,
        account_address=permit.account_address,
        signer_address=permit.signer_address,
        authorization_address=deployment.domain_verifying_contract,
        authorization_implementation=deployment.auth.implementation,
        router_address=deployment.system_router,
        nonce_anchor=permit.nonce_anchor,
        nonce_bitmap_index=permit.nonce_bitmap_index,
        nonce_currently_unused=True,
        deadline=permit.deadline,
        action_hash=permit.action_hash,
        verify_witness_typehash=runtime_typehash,
        behavioral_replay_rejection_proven=False,
    )
    attestation = object.__new__(RISExReplayProtectionArchitectureAttestation)
    object.__setattr__(attestation, 'network', 'testnet')
    object.__setattr__(
        attestation,
        'deployment_fingerprint',
        preflight.observed_fingerprint,
    )
    object.__setattr__(attestation, 'block_tag', block_tag)
    object.__setattr__(attestation, 'chain_id', deployment.api_chain_id)
    object.__setattr__(attestation, 'account_address', permit.account_address)
    object.__setattr__(attestation, 'signer_address', permit.signer_address)
    object.__setattr__(
        attestation,
        'authorization_address',
        deployment.domain_verifying_contract,
    )
    object.__setattr__(
        attestation,
        'authorization_implementation',
        deployment.auth.implementation,
    )
    object.__setattr__(attestation, 'router_address', deployment.system_router)
    object.__setattr__(attestation, 'nonce_anchor', permit.nonce_anchor)
    object.__setattr__(attestation, 'nonce_bitmap_index', permit.nonce_bitmap_index)
    object.__setattr__(attestation, 'nonce_currently_unused', True)
    object.__setattr__(attestation, 'deadline', permit.deadline)
    object.__setattr__(attestation, 'action_hash', permit.action_hash)
    object.__setattr__(attestation, 'verify_witness_typehash', runtime_typehash)
    object.__setattr__(attestation, 'behavioral_replay_rejection_proven', False)
    object.__setattr__(attestation, '_attestation_seal', _REPLAY_ARCHITECTURE_SEAL)
    object.__setattr__(attestation, '_attested_values', values)

    assert_replay_protection_architecture_attested(
        attestation,
        request=request,
        account_address=permit.account_address,
        signer_address=permit.signer_address,
        chain_id=deployment.api_chain_id,
        authorization_address=deployment.domain_verifying_contract,
        router_address=deployment.system_router,
    )
    return attestation
