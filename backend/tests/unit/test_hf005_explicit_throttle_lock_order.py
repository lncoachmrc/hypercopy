from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter


class _Tracker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.mark_count = 0

    async def wait_for_existing_backoff(self, _address: str) -> float:
        self.events.append("pre_wait")
        return 0.0

    async def record_action_attempt(self, _address: str) -> None:
        self.events.append("record_attempt")

    async def wait_if_backed_off(self, _address: str) -> float:
        self.events.append("cadence")
        return 0.0

    async def mark_throttled(self, _address: str) -> None:
        self.mark_count += 1
        self.events.append("mark_throttled")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": "UserRateLimitExceeded"}]},
            },
        },
        {"status": "err", "response": "AddressRateLimitExceeded"},
    ],
)
async def test_explicit_throttle_slot_is_installed_before_signer_unlock(monkeypatch, response):
    events: list[str] = []
    tracker = _Tracker(events)

    @asynccontextmanager
    async def fake_signer_lock(_signer_address: str):
        events.append("signer_enter")
        try:
            yield
        finally:
            events.append("signer_exit")

    adapter = HyperliquidAdapter(None, network="testnet")
    adapter.address_limits = tracker
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())
    monkeypatch.setattr("app.adapters.hyperliquid.signer_action_lock", fake_signer_lock)
    monkeypatch.setattr(adapter, "_call", AsyncMock(return_value=response))

    result = await adapter._signed_call(
        "0x" + "11" * 20,
        "0x" + "22" * 20,
        lambda: response,
    )

    assert result == response
    assert tracker.mark_count == 1
    assert events.index("mark_throttled") < events.index("signer_exit")


@pytest.mark.asyncio
async def test_slow_explicit_throttle_diagnostic_does_not_double_mark(monkeypatch):
    events: list[str] = []
    tracker = _Tracker(events)
    adapter = HyperliquidAdapter(None, network="testnet")
    adapter.address_limits = tracker
    diagnostic = AsyncMock()
    monkeypatch.setattr(adapter, "_record_exchange_address_throttle", diagnostic)

    await adapter._observe_explicit_address_throttle(
        "0x" + "33" * 20,
        "UserRateLimitExceeded",
    )

    assert tracker.mark_count == 0
    diagnostic.assert_awaited_once()
