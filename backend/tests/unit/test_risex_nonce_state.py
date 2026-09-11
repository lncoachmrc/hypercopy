from __future__ import annotations

from typing import Any

import pytest

from app.adapters.risex_types import ProviderDataMalformed, ProviderReadUnavailable


ACCOUNT = '0x' + ('11' * 20)


class FakeAPI:
    public_read_only = True

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        return self.payload


@pytest.mark.asyncio
async def test_nonce_state_uses_provider_anchor_and_bitmap_index() -> None:
    from app.security.risex_nonce_state import collect_order_nonce_selection

    api = FakeAPI({'data': {'nonce_anchor': '42', 'current_bitmap_index': 17}})
    evidence = await collect_order_nonce_selection(api, account=ACCOUNT)

    assert evidence.observed_nonce_anchor == 42
    assert evidence.observed_bitmap_index == 17
    assert evidence.selected_nonce_anchor == 42
    assert evidence.selected_bitmap_index == 17
    assert evidence.rolled_anchor is False
    assert api.calls == [(f'/v1/nonce-state/{ACCOUNT}', None)]


@pytest.mark.asyncio
async def test_nonce_state_rolls_full_anchor_exactly_as_provider_documents() -> None:
    from app.security.risex_nonce_state import collect_order_nonce_selection

    api = FakeAPI({'nonce_anchor': 42, 'current_bitmap_index': 208})
    evidence = await collect_order_nonce_selection(api, account=ACCOUNT)

    assert evidence.observed_nonce_anchor == 42
    assert evidence.observed_bitmap_index == 208
    assert evidence.selected_nonce_anchor == 43
    assert evidence.selected_bitmap_index == 0
    assert evidence.rolled_anchor is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('payload', 'match'),
    [
        ({'nonce_anchor': -1, 'current_bitmap_index': 0}, 'nonce_anchor'),
        ({'nonce_anchor': 1 << 48, 'current_bitmap_index': 0}, 'nonce_anchor'),
        ({'nonce_anchor': 42, 'current_bitmap_index': -1}, 'current_bitmap_index'),
        ({'nonce_anchor': 42, 'current_bitmap_index': 209}, 'current_bitmap_index'),
        ({'nonce_anchor': True, 'current_bitmap_index': 0}, 'nonce_anchor'),
        ({'nonce_anchor': 42, 'current_bitmap_index': False}, 'current_bitmap_index'),
        ({'nonce_anchor': 'not-a-number', 'current_bitmap_index': 0}, 'nonce_anchor'),
        ({'nonce_anchor': 42}, 'current_bitmap_index'),
    ],
)
async def test_nonce_state_rejects_malformed_or_out_of_range_provider_data(
    payload: dict[str, Any],
    match: str,
) -> None:
    from app.security.risex_nonce_state import collect_order_nonce_selection

    with pytest.raises(ProviderDataMalformed, match=match):
        await collect_order_nonce_selection(FakeAPI(payload), account=ACCOUNT)


@pytest.mark.asyncio
async def test_nonce_state_rejects_uint48_rollover_overflow() -> None:
    from app.security.risex_nonce_state import collect_order_nonce_selection

    api = FakeAPI({'nonce_anchor': (1 << 48) - 1, 'current_bitmap_index': 208})
    with pytest.raises(ProviderDataMalformed, match='overflow'):
        await collect_order_nonce_selection(api, account=ACCOUNT)


@pytest.mark.asyncio
async def test_nonce_state_requires_explicit_public_read_only_transport() -> None:
    from app.security.risex_nonce_state import collect_order_nonce_selection

    api = FakeAPI({'nonce_anchor': 42, 'current_bitmap_index': 0})
    api.public_read_only = False

    with pytest.raises(ProviderReadUnavailable, match='public read-only'):
        await collect_order_nonce_selection(api, account=ACCOUNT)

    assert api.calls == []
