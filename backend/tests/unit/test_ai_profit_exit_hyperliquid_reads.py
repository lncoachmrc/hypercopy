from types import SimpleNamespace

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import (
    Priority,
    WEIGHT_STANDARD_INFO,
    WEIGHT_USER_FILLS_MAX,
)


@pytest.mark.asyncio
async def test_user_funding_history_uses_conservative_history_read_lane():
    adapter = object.__new__(HyperliquidAdapter)

    funding_method = object()
    adapter.info = SimpleNamespace(
        user_funding_history=funding_method,
    )

    calls = []

    async def fake_read(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return [
            {
                "time": 123,
                "delta": {
                    "coin": "BTC",
                    "szi": "1",
                    "usdc": "-0.25",
                },
            }
        ]

    adapter._read = fake_read

    result = await adapter.user_funding_history(
        "0x" + "11" * 20,
        100,
        200,
    )

    assert len(result) == 1
    assert calls == [
        (
            funding_method,
            ("0x" + "11" * 20, 100, 200),
            {
                "weight": WEIGHT_USER_FILLS_MAX,
                "priority": Priority.RECONCILE,
                "timeout": 30,
            },
        )
    ]


@pytest.mark.asyncio
async def test_user_funding_history_allows_open_end_time():
    adapter = object.__new__(HyperliquidAdapter)

    funding_method = object()
    adapter.info = SimpleNamespace(
        user_funding_history=funding_method,
    )

    captured = {}

    async def fake_read(method, *args, **kwargs):
        captured["method"] = method
        captured["args"] = args
        captured["kwargs"] = kwargs
        return []

    adapter._read = fake_read

    result = await adapter.user_funding_history(
        "0x" + "22" * 20,
        100,
    )

    assert result == []
    assert captured["method"] is funding_method
    assert captured["args"] == ("0x" + "22" * 20, 100, None)
    assert captured["kwargs"]["weight"] == WEIGHT_USER_FILLS_MAX


@pytest.mark.asyncio
async def test_user_funding_history_rejects_malformed_response():
    adapter = object.__new__(HyperliquidAdapter)

    adapter.info = SimpleNamespace(
        user_funding_history=object(),
    )

    async def fake_read(*_args, **_kwargs):
        return {"unexpected": "object"}

    adapter._read = fake_read

    with pytest.raises(
        ValueError,
        match="Malformed Hyperliquid user funding response",
    ):
        await adapter.user_funding_history(
            "0x" + "33" * 20,
            100,
            200,
        )


@pytest.mark.asyncio
async def test_user_fees_uses_standard_info_lane():
    adapter = object.__new__(HyperliquidAdapter)

    fees_method = object()
    adapter.info = SimpleNamespace(
        user_fees=fees_method,
    )

    calls = []

    async def fake_read(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return {
            "userCrossRate": "0.000315",
            "userAddRate": "0.000105",
        }

    adapter._read = fake_read

    result = await adapter.user_fees(
        "0x" + "44" * 20,
    )

    assert result["userCrossRate"] == "0.000315"
    assert calls == [
        (
            fees_method,
            ("0x" + "44" * 20,),
            {
                "weight": WEIGHT_STANDARD_INFO,
                "priority": Priority.RECONCILE,
                "timeout": 15,
            },
        )
    ]


@pytest.mark.asyncio
async def test_user_fees_rejects_missing_cross_rate():
    adapter = object.__new__(HyperliquidAdapter)

    adapter.info = SimpleNamespace(
        user_fees=object(),
    )

    async def fake_read(*_args, **_kwargs):
        return {
            "userAddRate": "0.000105",
        }

    adapter._read = fake_read

    with pytest.raises(
        ValueError,
        match="userCrossRate",
    ):
        await adapter.user_fees(
            "0x" + "55" * 20,
        )


@pytest.mark.asyncio
async def test_user_fees_rejects_non_object_response():
    adapter = object.__new__(HyperliquidAdapter)

    adapter.info = SimpleNamespace(
        user_fees=object(),
    )

    async def fake_read(*_args, **_kwargs):
        return []

    adapter._read = fake_read

    with pytest.raises(
        ValueError,
        match="Malformed Hyperliquid user fees response",
    ):
        await adapter.user_fees(
            "0x" + "66" * 20,
        )
