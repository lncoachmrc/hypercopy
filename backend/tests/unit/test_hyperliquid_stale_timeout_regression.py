import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Priority
from app.workers.execution_worker import Worker


@pytest.mark.asyncio
async def test_safe_read_timeout_covers_hung_sdk_call(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    monkeypatch.setattr('app.adapters.hyperliquid.settings.HL_SAFE_READ_RETRIES', 1)
    adapter = HyperliquidAdapter(None, network='testnet')

    def slow_read():
        time.sleep(0.25)
        return {'late': True}

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await adapter._read(
            slow_read,
            weight=1,
            priority=Priority.RECONCILE,
            timeout=0.02,
        )
    assert time.monotonic() - started < 0.15


@pytest.mark.asyncio
async def test_reconcile_reads_enter_local_cooldown_after_429(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    monkeypatch.setattr('app.adapters.hyperliquid.settings.HL_SAFE_READ_RETRIES', 1)
    adapter = HyperliquidAdapter(None, network='testnet')
    adapter._read_cooldown_seconds = 60.0

    def rate_limited():
        raise RuntimeError('429 Too Many Requests')

    with pytest.raises(RuntimeError, match='429'):
        await adapter._read(
            rate_limited,
            weight=1,
            priority=Priority.RECONCILE,
            timeout=0.1,
        )

    called = False

    def should_not_call():
        nonlocal called
        called = True
        return {'unexpected': True}

    with pytest.raises(RuntimeError, match='cooldown'):
        await adapter._read(
            should_not_call,
            weight=1,
            priority=Priority.RECONCILE,
            timeout=0.1,
        )
    assert called is False


@pytest.mark.asyncio
async def test_order_reads_bypass_observability_cooldown(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    monkeypatch.setattr('app.adapters.hyperliquid.settings.HL_SAFE_READ_RETRIES', 1)
    adapter = HyperliquidAdapter(None, network='testnet')
    adapter._read_cooldown_seconds = 60.0

    def rate_limited():
        raise RuntimeError('429 Too Many Requests')

    with pytest.raises(RuntimeError, match='429'):
        await adapter._read(
            rate_limited,
            weight=1,
            priority=Priority.RECONCILE,
            timeout=0.1,
        )

    result = await adapter._read(
        lambda: {'ok': True},
        weight=1,
        priority=Priority.ORDER,
        timeout=0.1,
    )
    assert result == {'ok': True}


@pytest.mark.asyncio
async def test_mids_can_use_reconcile_lane(monkeypatch):
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    adapter = HyperliquidAdapter(None, network='testnet')
    adapter._read = AsyncMock(return_value={'BTC': '60000'})

    result = await adapter.mids(priority=Priority.RECONCILE)

    assert result == {'BTC': '60000'}
    assert adapter._read.await_args.kwargs['priority'] == Priority.RECONCILE


@pytest.mark.asyncio
async def test_rate_limited_reconcile_failure_skips_observability_fallback():
    worker = object.__new__(Worker)
    worker._refresh_follower_observability = AsyncMock(return_value=1)

    refreshed = await worker._fallback_observability_after_reconcile_failure(
        object(),
        'mainnet',
        RuntimeError('429 Too Many Requests'),
    )

    assert refreshed == 0
    worker._refresh_follower_observability.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_deadline_returns_control_and_allows_next_cycle():
    worker = object.__new__(Worker)
    blocker = asyncio.Event()
    calls = 0

    async def reconcile():
        nonlocal calls
        calls += 1
        if calls == 1:
            await blocker.wait()

    worker.run_reconcile_if_leader = reconcile

    started = time.monotonic()
    first_completed = await worker._run_reconcile_with_deadline(timeout=0.02)
    second_completed = await worker._run_reconcile_with_deadline(timeout=0.02)

    assert first_completed is False
    assert second_completed is True
    assert calls == 2
    assert time.monotonic() - started < 0.15
