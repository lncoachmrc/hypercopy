from __future__ import annotations

import asyncio
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

    async def wait_for_existing_backoff(self, _address: str):
        self._events.append("pre_wait")
        return 0.0

    async def record_action_attempt(self, _address: str):
        # The real tracker writes attempt accounting first, then atomically
        # reserves the sustained-mode cadence slot as its final operation.
        self._events.append("record_attempt")
        self._events.append("cadence_slot")

    async def wait_if_backed_off(self, _address: str):
        self._events.append("pre_final_wait")
        return 0.0

    async def mark_throttled(self, _address: str):
        self._events.append("mark_throttled")


@pytest.mark.asyncio
async def test_final_cadence_slot_is_reserved_at_worker_thread_start(monkeypatch):
    events: list[str] = []
    limiter = _OrderedLimiter(events)
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())
    adapter = HyperliquidAdapter(limiter, network="testnet")
    adapter.address_limits = _OrderedAddressTracker(events)

    @asynccontextmanager
    async def ordered_signer_lock(_signer_address: str):
        events.append("signer_lock")
        yield

    original_to_thread = asyncio.to_thread
    allow_worker_start = asyncio.Event()

    async def delayed_to_thread(func, *args):
        events.append("thread_queued")
        await allow_worker_start.wait()
        events.append("thread_start")
        return await original_to_thread(func, *args)

    def submit():
        events.append("submit")
        return {"status": "ok"}

    monkeypatch.setattr("app.adapters.hyperliquid.asyncio.to_thread", delayed_to_thread)
    monkeypatch.setattr("app.adapters.hyperliquid.signer_action_lock", ordered_signer_lock)

    task = asyncio.create_task(
        adapter._signed_call(
            "0x" + "22" * 20,
            "0x" + "11" * 20,
            submit,
        )
    )

    for _ in range(100):
        if "thread_queued" in events:
            break
        await asyncio.sleep(0)

    assert "thread_queued" in events
    # Executor queue delay must not consume either accounting work or a cadence slot.
    assert "pre_final_wait" not in events
    assert "cadence_slot" not in events
    assert "record_attempt" not in events
    assert "submit" not in events

    allow_worker_start.set()
    result = await asyncio.wait_for(task, timeout=2)

    assert result == {"status": "ok"}
    assert events == [
        "pre_wait",
        "signer_lock",
        "ip_budget",
        "thread_queued",
        "thread_start",
        "pre_final_wait",
        "record_attempt",
        "cadence_slot",
        "submit",
    ]
