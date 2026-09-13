from __future__ import annotations

import importlib

from app.adapters.hyperliquid import HyperliquidAdapter


def test_destination_switch_service_exposes_three_state_contract() -> None:
    module = importlib.import_module('app.services.destination_switch')

    assert module.DestinationLifecycleState.VERIFIED_FLAT.value == 'VERIFIED_FLAT'
    assert module.DestinationLifecycleState.NEVER_ACTIVATED.value == 'NEVER_ACTIVATED'
    assert module.DestinationLifecycleState.UNREADABLE.value == 'UNREADABLE'
    assert hasattr(module, 'DestinationSwitchAssessment')
    assert hasattr(module, 'destination_switch_blockers')
    assert hasattr(module, 'assess_destination_switch')


def test_hyperliquid_adapter_exposes_frontend_open_orders_read() -> None:
    assert hasattr(HyperliquidAdapter, 'frontend_open_orders')
