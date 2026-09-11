from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.risex_types import (
    ProviderDataMalformed,
    ProviderReadUnavailable,
    RISExPublicReadTransport,
)


_UINT48_LIMIT = 1 << 48
_FULL_BITMAP_SENTINEL = 208


@dataclass(frozen=True, slots=True)
class RISExOrderNonceSelection:
    """Provider-observed nonce state and the exact permit nonce selected from it."""

    observed_nonce_anchor: int
    observed_bitmap_index: int
    selected_nonce_anchor: int
    selected_bitmap_index: int
    rolled_anchor: bool


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get('data')
    if data is None:
        return payload
    if not isinstance(data, dict):
        raise ProviderDataMalformed('RISEx nonce-state data envelope is not an object')
    return data


def _parse_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ProviderDataMalformed(f'{field} is not a valid integer')
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProviderDataMalformed(f'{field} is not a valid integer') from exc
    return result


async def collect_order_nonce_selection(
    api: RISExPublicReadTransport,
    *,
    account: str,
) -> RISExOrderNonceSelection:
    """Read and select the current RISEx bitmap nonce without generating one locally."""

    if getattr(api, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx nonce-state collector requires a public read-only API transport'
        )

    payload = _unwrap(await api.get_json(f'/v1/nonce-state/{account}'))
    nonce_anchor = _parse_int(payload.get('nonce_anchor'), field='nonce_anchor')
    bitmap_index = _parse_int(
        payload.get('current_bitmap_index'),
        field='current_bitmap_index',
    )

    if nonce_anchor < 0 or nonce_anchor >= _UINT48_LIMIT:
        raise ProviderDataMalformed('nonce_anchor is outside uint48 range')
    if bitmap_index < 0 or bitmap_index > _FULL_BITMAP_SENTINEL:
        raise ProviderDataMalformed('current_bitmap_index must be in [0, 208]')

    if bitmap_index == _FULL_BITMAP_SENTINEL:
        if nonce_anchor == _UINT48_LIMIT - 1:
            raise ProviderDataMalformed('RISEx nonce_anchor rollover would overflow uint48')
        return RISExOrderNonceSelection(
            observed_nonce_anchor=nonce_anchor,
            observed_bitmap_index=bitmap_index,
            selected_nonce_anchor=nonce_anchor + 1,
            selected_bitmap_index=0,
            rolled_anchor=True,
        )

    return RISExOrderNonceSelection(
        observed_nonce_anchor=nonce_anchor,
        observed_bitmap_index=bitmap_index,
        selected_nonce_anchor=nonce_anchor,
        selected_bitmap_index=bitmap_index,
        rolled_anchor=False,
    )
