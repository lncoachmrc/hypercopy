from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.adapters.risex_types import ProviderDataMalformed, ProviderReadUnavailable


GET_SESSION_KEY_STATUS_SELECTOR = '0xdd962cb2'
HAS_PERMISSION_SELECTOR = '0xed82f4b8'
AUTHORIZED_STATUS_ID = 1
ALL_PERMISSION_ID = 1
PERPS_PERMISSION_ID = 2
SPOT_PERMISSION_ID = 3
MOVE_FUND_PERMISSION_ID = 4


class PublicRPCTransport(Protocol):
    @property
    def public_read_only(self) -> bool: ...

    async def call(self, method: str, params: list[object]) -> object: ...


@dataclass(frozen=True, slots=True)
class RISExAuthorizationSessionEvidence:
    authorization_address: str
    account: str
    signer: str
    block_tag: str
    status_code: int
    session_active: bool | None
    all_permission_id: int
    all_permission: bool
    perps_permission_id: int
    perps_permission: bool
    spot_permission_id: int
    spot_permission: bool
    move_fund_permission_id: int
    move_fund_permission: bool
    perps_only_scope: bool | None


def _address_word(value: str, *, field: str) -> str:
    if len(value) != 42 or not value.startswith('0x'):
        raise ProviderDataMalformed(f'{field} is not a valid EVM address')
    try:
        number = int(value[2:], 16)
    except ValueError as exc:
        raise ProviderDataMalformed(f'{field} is not a valid EVM address') from exc
    if number == 0:
        raise ProviderDataMalformed(f'{field} is the zero address')
    return number.to_bytes(32, 'big').hex()


def _uint_word(value: int) -> str:
    return value.to_bytes(32, 'big').hex()


def _decode_word(value: object, *, field: str) -> int:
    if not isinstance(value, str) or not value.startswith('0x') or len(value) != 66:
        raise ProviderDataMalformed(f'{field} is not a 32-byte ABI word')
    try:
        return int(value[2:], 16)
    except ValueError as exc:
        raise ProviderDataMalformed(f'{field} is not valid hex ABI data') from exc


def _decode_uint8(value: object, *, field: str) -> int:
    decoded = _decode_word(value, field=field)
    if decoded > 0xFF:
        raise ProviderDataMalformed(f'{field} is outside uint8 range')
    return decoded


def _decode_bool(value: object, *, field: str) -> bool:
    decoded = _decode_word(value, field=field)
    if decoded not in (0, 1):
        raise ProviderDataMalformed(f'{field} is not a canonical ABI boolean')
    return decoded == 1


async def _collect_permission(
    rpc: PublicRPCTransport,
    *,
    authorization_address: str,
    account_word: str,
    signer_word: str,
    permission_id: int,
    permission_name: str,
    block_tag: str,
) -> bool:
    permission_data = (
        HAS_PERMISSION_SELECTOR
        + account_word
        + signer_word
        + _uint_word(permission_id)
    )
    permission_raw = await rpc.call(
        'eth_call',
        [{'to': authorization_address, 'data': permission_data}, block_tag],
    )
    return _decode_bool(
        permission_raw,
        field=f'RISEx {permission_name} permission boolean',
    )


async def collect_authorization_session_evidence(
    rpc: PublicRPCTransport,
    *,
    authorization_address: str,
    account: str,
    signer: str,
    block_tag: str,
) -> RISExAuthorizationSessionEvidence:
    """Read signer status and the relevant permission matrix at one fixed block."""

    if getattr(rpc, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable('RISEx signer evidence requires a read-only RPC transport')
    if not isinstance(block_tag, str) or not block_tag.startswith('0x'):
        raise ProviderDataMalformed('RISEx signer evidence block tag must be canonical hex')

    auth_word = _address_word(authorization_address, field='RISEx Authorization address')
    account_word = _address_word(account, field='RISEx account')
    signer_word = _address_word(signer, field='RISEx signer')
    del auth_word  # validation only; calldata targets the address separately

    status_data = GET_SESSION_KEY_STATUS_SELECTOR + account_word + signer_word
    status_raw = await rpc.call(
        'eth_call',
        [{'to': authorization_address, 'data': status_data}, block_tag],
    )
    status_code = _decode_uint8(status_raw, field='RISEx session status')
    if status_code == AUTHORIZED_STATUS_ID:
        session_active: bool | None = True
    elif status_code == 0:
        session_active = False
    else:
        session_active = None

    all_permission = await _collect_permission(
        rpc,
        authorization_address=authorization_address,
        account_word=account_word,
        signer_word=signer_word,
        permission_id=ALL_PERMISSION_ID,
        permission_name='All',
        block_tag=block_tag,
    )
    perps_permission = await _collect_permission(
        rpc,
        authorization_address=authorization_address,
        account_word=account_word,
        signer_word=signer_word,
        permission_id=PERPS_PERMISSION_ID,
        permission_name='Perps',
        block_tag=block_tag,
    )
    spot_permission = await _collect_permission(
        rpc,
        authorization_address=authorization_address,
        account_word=account_word,
        signer_word=signer_word,
        permission_id=SPOT_PERMISSION_ID,
        permission_name='Spot',
        block_tag=block_tag,
    )
    move_fund_permission = await _collect_permission(
        rpc,
        authorization_address=authorization_address,
        account_word=account_word,
        signer_word=signer_word,
        permission_id=MOVE_FUND_PERMISSION_ID,
        permission_name='MoveFund',
        block_tag=block_tag,
    )

    broader_permission_present = (
        all_permission or spot_permission or move_fund_permission
    )
    perps_only_scope: bool | None = False if broader_permission_present else None

    return RISExAuthorizationSessionEvidence(
        authorization_address=authorization_address,
        account=account,
        signer=signer,
        block_tag=block_tag,
        status_code=status_code,
        session_active=session_active,
        all_permission_id=ALL_PERMISSION_ID,
        all_permission=all_permission,
        perps_permission_id=PERPS_PERMISSION_ID,
        perps_permission=perps_permission,
        spot_permission_id=SPOT_PERMISSION_ID,
        spot_permission=spot_permission,
        move_fund_permission_id=MOVE_FUND_PERMISSION_ID,
        move_fund_permission=move_fund_permission,
        perps_only_scope=perps_only_scope,
    )
