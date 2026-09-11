from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass, field
from typing import Literal

from app.security.risex_nonce_state import RISExOrderNonceSelection
from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_testnet_signer import RISExTestnetSignerCredential
from app.security.risex_verify_witness_signer import sign_verify_witness


_UINT48_LIMIT = 1 << 48
_MAX_SELECTED_BITMAP_INDEX = 207
_FULL_BITMAP_SENTINEL = 208


@dataclass(frozen=True, slots=True)
class RISExPreparedPlaceOrderPermit:
    """Offline place-order permit material that never authorizes a provider POST."""

    account_address: str
    signer_address: str
    action_hash: str
    nonce_anchor: int
    nonce_bitmap_index: int
    deadline: int
    _signature: bytes = field(repr=False, compare=False)
    post_allowed: Literal[False] = False

    def permit_for_testnet_transport(self) -> dict[str, object]:
        """Serialize only the permit object for an explicitly controlled testnet transport."""

        return {
            'account': self.account_address,
            'signer': self.signer_address,
            'nonce_anchor': self.nonce_anchor,
            'nonce_bitmap_index': self.nonce_bitmap_index,
            'deadline': self.deadline,
            'signature': b64encode(self._signature).decode('ascii'),
        }


def _require_plain_int(name: str, value: int) -> int:
    if type(value) is not int:
        raise SignedTestnetBlocked(f'RISEx {name} must be an integer')
    return value


def _assert_nonce_selection_consistent(selection: RISExOrderNonceSelection) -> None:
    observed_anchor = _require_plain_int(
        'nonce selection observed anchor', selection.observed_nonce_anchor
    )
    observed_index = _require_plain_int(
        'nonce selection observed bitmap index', selection.observed_bitmap_index
    )
    selected_anchor = _require_plain_int(
        'nonce selection selected anchor', selection.selected_nonce_anchor
    )
    selected_index = _require_plain_int(
        'nonce selection selected bitmap index', selection.selected_bitmap_index
    )

    if not (0 <= observed_anchor < _UINT48_LIMIT):
        raise SignedTestnetBlocked('RISEx nonce selection observed anchor is outside uint48')
    if not (0 <= selected_anchor < _UINT48_LIMIT):
        raise SignedTestnetBlocked('RISEx nonce selection selected anchor is outside uint48')
    if not (0 <= observed_index <= _FULL_BITMAP_SENTINEL):
        raise SignedTestnetBlocked('RISEx nonce selection observed bitmap index is invalid')
    if not (0 <= selected_index <= _MAX_SELECTED_BITMAP_INDEX):
        raise SignedTestnetBlocked('RISEx nonce selection selected bitmap index is invalid')

    if selection.rolled_anchor:
        is_consistent = (
            observed_index == _FULL_BITMAP_SENTINEL
            and observed_anchor < _UINT48_LIMIT - 1
            and selected_anchor == observed_anchor + 1
            and selected_index == 0
        )
    else:
        is_consistent = (
            observed_index <= _MAX_SELECTED_BITMAP_INDEX
            and selected_anchor == observed_anchor
            and selected_index == observed_index
        )

    if not is_consistent:
        raise SignedTestnetBlocked('RISEx nonce selection evidence is inconsistent')


def prepare_place_order_permit(
    *,
    order: RISExPlaceOrder,
    credential: RISExTestnetSignerCredential,
    nonce_selection: RISExOrderNonceSelection,
    domain_name: str,
    domain_version: str,
    chain_id: int,
    verifying_contract: str,
    router: str,
    observed_block_timestamp: int,
    session_expiration: int,
    deadline: int,
) -> RISExPreparedPlaceOrderPermit:
    """Prepare and sign a place-order permit entirely offline and fail closed."""

    observed_block_timestamp = _require_plain_int(
        'observed block timestamp', observed_block_timestamp
    )
    session_expiration = _require_plain_int('session expiration', session_expiration)
    deadline = _require_plain_int('permit deadline', deadline)

    if session_expiration <= observed_block_timestamp:
        raise SignedTestnetBlocked('RISEx signer session is expired at the observed block')
    if deadline <= observed_block_timestamp:
        raise SignedTestnetBlocked('RISEx permit deadline must be after observed block time')
    if deadline > session_expiration:
        raise SignedTestnetBlocked('RISEx permit deadline must not exceed session expiration')

    _assert_nonce_selection_consistent(nonce_selection)
    action_hash = build_place_order_action_hash(order)
    signed = sign_verify_witness(
        credential,
        domain_name=domain_name,
        domain_version=domain_version,
        chain_id=chain_id,
        verifying_contract=verifying_contract,
        router=router,
        action_hash=action_hash,
        nonce_anchor=nonce_selection.selected_nonce_anchor,
        nonce_bitmap=nonce_selection.selected_bitmap_index,
        deadline=deadline,
    )

    return RISExPreparedPlaceOrderPermit(
        account_address=credential.account_address,
        signer_address=signed.signer_address,
        action_hash=action_hash,
        nonce_anchor=nonce_selection.selected_nonce_anchor,
        nonce_bitmap_index=nonce_selection.selected_bitmap_index,
        deadline=deadline,
        _signature=signed.signature_bytes_for_transport(),
    )
