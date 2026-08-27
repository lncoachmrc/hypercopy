from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter


class _OrderedLimiter:
    def __init__(self, events: list[str]):
        self._redis = MagicMock()
        self._events = events

    async def acquire(self, *_args, **_kwargs):
        self._events.append("ip_budget")


class _OrderedAddressTracker:
    def __init__(self, events: list[str]):
        self._events = events

    async def record_action_attempt(self, _address: str):
        self._events.append("record_attempt")

    async def wait_if_backed_off(self, _address: str):
        self._events.append("cadence_slot")
        return 0.0

    async def mark_throttled(self, _address: str):
        self._events.append("mark_throttled")


@pytest.mark.asyncio
async def test_final_cadence_slot_is_reserved_after_blocking_gates_and_before_submit(monkeypatch):
    events: list[str] = []
    limiter = _OrderedLimiter(events)
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())
    adapter = HyperliquidAdapter(limiter, network="testnet")
    adapter.address_limits = _OrderedAddressTracker(events)

    @asynccontextmanager
    async def ordered_signer_lock(_signer_address: str):
        events.append("signer_lock")
        yield

    async def ordered_call(func, *args):
        events.append("submit")
        return func(*args)

    monkeypatch.setattr("app.adapters.hyperliquid.signer_action_lock", ordered_signer_lock)
    monkeypatch.setattr(adapter, "_call", ordered_call)

    result = await adapter._signed_call(
        "0x" + "22" * 20,
        "0x" + "11" * 20,
        lambda: {"status": "ok"},
    )

    assert result == {"status": "ok"}
    assert events == [
        "signer_lock",
        "ip_budget",
        "record_attempt",
        "cadence_slot",
        "submit",
    ]
