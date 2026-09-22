from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import ai_profit_exit, reconcile
from app.services.ai_profit_exit import (
    ProfitExitIntentState,
    SourceCycle,
    protected_reconcile_target,
    source_cycle_events_stmt,
)
from app.services.networking import UserNetworkState


def _cycle(name: str) -> SourceCycle:
    event_id = uuid.uuid5(uuid.NAMESPACE_DNS, name)
    return SourceCycle(
        open_event_id=event_id,
        source_cycle_id=f"mainnet:BTC:{event_id}",
        state_version=100,
        master_position=Decimal("1"),
        side="LONG",
    )


def test_user_network_state_carries_exact_active_destination_identity():
    fields = UserNetworkState.__dataclass_fields__

    assert {"network", "started_at", "provider", "epoch_id"} <= set(fields)


def test_source_cycle_query_stays_before_snapshot_causal_boundary():
    stmt = source_cycle_events_stmt(
        asset="BTC",
        snapshot_started_order=500,
    )

    sql = str(stmt)

    assert "master_events.asset" in sql
    assert "master_events.causal_order" in sql

    compiled = stmt.compile()
    assert 500 in compiled.params.values()


@pytest.mark.asyncio
async def test_same_cycle_completed_memory_keeps_flat_follower_flat(monkeypatch):
    cycle = _cycle("same-cycle")
    memory = SimpleNamespace(
        intent_state=ProfitExitIntentState.COMPLETED.value,
        source_cycle_id=cycle.source_cycle_id,
    )

    async def fake_cycle(*args, **kwargs):
        return cycle

    async def fake_memory(*args, **kwargs):
        return memory

    monkeypatch.setattr(
        ai_profit_exit,
        "read_current_source_cycle",
        fake_cycle,
    )
    monkeypatch.setattr(
        ai_profit_exit,
        "read_operational_profit_exit_memory",
        fake_memory,
    )

    target = await protected_reconcile_target(
        object(),
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        master_network="mainnet",
        snapshot_started_order=200,
        current_master_position=Decimal("1"),
        current_position=Decimal("0"),
        desired_target=Decimal("0.75"),
    )

    assert target == Decimal("0")


@pytest.mark.asyncio
async def test_partial_memory_freezes_real_residual(monkeypatch):
    cycle = _cycle("partial")
    memory = SimpleNamespace(
        intent_state=ProfitExitIntentState.PARTIAL.value,
        source_cycle_id=cycle.source_cycle_id,
    )

    async def fake_cycle(*args, **kwargs):
        return cycle

    async def fake_memory(*args, **kwargs):
        return memory

    monkeypatch.setattr(
        ai_profit_exit,
        "read_current_source_cycle",
        fake_cycle,
    )
    monkeypatch.setattr(
        ai_profit_exit,
        "read_operational_profit_exit_memory",
        fake_memory,
    )

    target = await protected_reconcile_target(
        object(),
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        master_network="mainnet",
        snapshot_started_order=200,
        current_master_position=Decimal("1"),
        current_position=Decimal("0.12"),
        desired_target=Decimal("0.75"),
    )

    assert target == Decimal("0.12")


@pytest.mark.asyncio
async def test_new_verified_cycle_reenables_normal_target(monkeypatch):
    new_cycle = _cycle("new-cycle")

    async def fake_cycle(*args, **kwargs):
        return new_cycle

    async def fake_memory(*args, **kwargs):
        # No operational exit belongs to the new verified cycle.
        return None

    monkeypatch.setattr(
        ai_profit_exit,
        "read_current_source_cycle",
        fake_cycle,
    )
    monkeypatch.setattr(
        ai_profit_exit,
        "read_operational_profit_exit_memory",
        fake_memory,
    )

    target = await protected_reconcile_target(
        object(),
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        master_network="mainnet",
        snapshot_started_order=300,
        current_master_position=Decimal("1"),
        current_position=Decimal("0"),
        desired_target=Decimal("0.75"),
    )

    assert target == Decimal("0.75")


@pytest.mark.asyncio
async def test_unverified_cycle_keeps_existing_operational_memory_conservative(
    monkeypatch,
):
    memory = SimpleNamespace(
        intent_state=ProfitExitIntentState.COMPLETED.value,
        source_cycle_id="mainnet:BTC:old-cycle",
    )

    async def fake_cycle(*args, **kwargs):
        return None

    async def fake_memory(*args, **kwargs):
        assert kwargs["source_cycle_id"] is None
        return memory

    monkeypatch.setattr(
        ai_profit_exit,
        "read_current_source_cycle",
        fake_cycle,
    )
    monkeypatch.setattr(
        ai_profit_exit,
        "read_operational_profit_exit_memory",
        fake_memory,
    )

    target = await protected_reconcile_target(
        object(),
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        master_network="mainnet",
        snapshot_started_order=None,
        current_master_position=Decimal("1"),
        current_position=Decimal("0"),
        desired_target=Decimal("0.75"),
    )

    assert target == Decimal("0")


@pytest.mark.asyncio
async def test_risex_v1_does_not_apply_profit_exit_memory():
    class NoDatabaseAccess:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("RISEx v1 must not read AI profit-exit memory")

    target = await protected_reconcile_target(
        NoDatabaseAccess(),
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="risex",
        execution_network="mainnet",
        asset="BTC",
        master_network="mainnet",
        snapshot_started_order=200,
        current_master_position=Decimal("1"),
        current_position=Decimal("0"),
        desired_target=Decimal("0.75"),
    )

    assert target == Decimal("0.75")


@pytest.mark.asyncio
async def test_master_flat_target_bypasses_ai_memory_entirely():
    class NoDatabaseAccess:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("master flatten must not depend on AI lookup")

    target = await protected_reconcile_target(
        NoDatabaseAccess(),
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        master_network="mainnet",
        snapshot_started_order=200,
        current_master_position=Decimal("0"),
        current_position=Decimal("0.25"),
        desired_target=Decimal("0"),
    )

    assert target == Decimal("0")


def test_reconcile_applies_profit_exit_guard_before_persisting_target():
    source = inspect.getsource(reconcile._reconcile_user_locked)

    guard = source.index("protected_reconcile_target(")
    persisted_target = source.index("ledger.target_size = desired_target")

    assert guard < persisted_target


def test_reconcile_passes_active_destination_identity_to_guard():
    source = inspect.getsource(reconcile._reconcile_user_locked)

    assert "execution_epoch_id=network_state.epoch_id" in source
    assert "execution_provider=network_state.provider" in source
    assert "execution_network=network" in source


def test_user_network_state_constructor_remains_single_destination_source():
    source = inspect.getsource(
        __import__(
            "app.services.networking",
            fromlist=["user_network_state"],
        ).user_network_state
    )

    assert "user_destination_state" in source
    assert "provider=destination.provider" in source
    assert "epoch_id=destination.epoch_id" in source
