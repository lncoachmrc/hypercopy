from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Priority, WEIGHT_CHEAP_INFO


@pytest.mark.asyncio
async def test_query_order_by_oid_uses_cheap_diagnostic_lane() -> None:
    adapter = object.__new__(HyperliquidAdapter)
    method = object()
    adapter.info = SimpleNamespace(query_order_by_oid=method)
    calls = []

    async def fake_read(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return {
            "status": "order",
            "order": {
                "status": "filled",
                "order": {
                    "oid": 123,
                    "orderType": "Stop Market",
                },
            },
        }

    adapter._read = fake_read

    result = await adapter.query_order_by_oid(
        "0x" + "11" * 20,
        123,
    )

    assert result["status"] == "order"
    assert calls == [
        (
            method,
            ("0x" + "11" * 20, 123),
            {
                "weight": WEIGHT_CHEAP_INFO,
                "priority": Priority.DIAGNOSTIC,
                "timeout": 10,
            },
        )
    ]


@pytest.mark.asyncio
async def test_query_order_by_oid_rejects_malformed_response() -> None:
    adapter = object.__new__(HyperliquidAdapter)
    adapter.info = SimpleNamespace(query_order_by_oid=object())

    async def fake_read(*_args, **_kwargs):
        return []

    adapter._read = fake_read

    with pytest.raises(ValueError, match="Malformed Hyperliquid order status response"):
        await adapter.query_order_by_oid(
            "0x" + "22" * 20,
            456,
        )
