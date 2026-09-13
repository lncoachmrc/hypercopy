from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter


def test_destination_switch_service_exposes_three_state_contract() -> None:
    import importlib

    module = importlib.import_module('app.services.destination_switch')

    assert module.DestinationLifecycleState.VERIFIED_FLAT.value == 'VERIFIED_FLAT'
    assert module.DestinationLifecycleState.NEVER_ACTIVATED.value == 'NEVER_ACTIVATED'
    assert module.DestinationLifecycleState.UNREADABLE.value == 'UNREADABLE'
    assert hasattr(module, 'DestinationSwitchAssessment')
    assert hasattr(module, 'destination_switch_blockers')
    assert hasattr(module, 'assess_destination_switch')


def test_hyperliquid_adapter_exposes_frontend_open_orders_read() -> None:
    assert hasattr(HyperliquidAdapter, 'frontend_open_orders')


@pytest.mark.asyncio
async def test_frontend_open_orders_preserves_trigger_and_conditional_fields(monkeypatch) -> None:
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    adapter = HyperliquidAdapter(None, network='testnet')
    trigger = {
        'coin': 'BTC',
        'oid': 123,
        'isTrigger': True,
        'triggerPx': '61000',
        'isPositionTpsl': True,
        'orderType': 'Take Profit Market',
    }
    adapter._read = AsyncMock(return_value=[trigger])

    result = await adapter.frontend_open_orders('0x' + ('11' * 20))

    assert result == [trigger]
    assert result[0]['isTrigger'] is True
    assert result[0]['triggerPx'] == '61000'
    assert result[0]['isPositionTpsl'] is True
    assert result[0]['orderType'] == 'Take Profit Market'


@pytest.mark.asyncio
async def test_frontend_open_orders_rejects_malformed_non_list_response(monkeypatch) -> None:
    monkeypatch.setattr('app.adapters.hyperliquid.Info', MagicMock())
    adapter = HyperliquidAdapter(None, network='testnet')
    adapter._read = AsyncMock(return_value={'unexpected': 'shape'})

    with pytest.raises(ValueError, match='frontend open orders'):
        await adapter.frontend_open_orders('0x' + ('11' * 20))
