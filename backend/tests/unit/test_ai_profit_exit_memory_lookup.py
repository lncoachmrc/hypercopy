from __future__ import annotations

import uuid

import pytest
from sqlalchemy import and_
from sqlalchemy.dialects import postgresql

from app.models.entities import AIProfitExitDecision
from app.services.ai_profit_exit import (
    operational_profit_exit_memory_stmt,
    read_operational_profit_exit_memory,
)


def _where_sql(stmt) -> str:
    clause = and_(*stmt._where_criteria)
    return str(
        clause.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"render_postcompile": True},
        )
    )


def _order_sql(stmt) -> str:
    return " ".join(str(clause) for clause in stmt._order_by_clauses)


def test_operational_memory_query_is_bound_to_exact_destination_scope():
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    cycle_id = f"mainnet:BTC:{uuid.uuid4()}"

    stmt = operational_profit_exit_memory_stmt(
        user_id=user_id,
        execution_epoch_id=epoch_id,
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id=cycle_id,
    )

    where_sql = _where_sql(stmt)

    assert "ai_profit_exit_decisions.user_id" in where_sql
    assert "ai_profit_exit_decisions.execution_epoch_id" in where_sql
    assert "ai_profit_exit_decisions.execution_provider" in where_sql
    assert "ai_profit_exit_decisions.execution_network" in where_sql
    assert "ai_profit_exit_decisions.asset" in where_sql
    assert "ai_profit_exit_decisions.source_cycle_id" in where_sql


def test_lookup_accepts_only_close_profit_operational_memory():
    stmt = operational_profit_exit_memory_stmt(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:cycle",
    )

    where_sql = _where_sql(stmt)
    compiled = stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    )

    assert "ai_profit_exit_decisions.action" in where_sql
    assert "ai_profit_exit_decisions.intent_state" in where_sql

    values = set()

    for value in compiled.params.values():
        if isinstance(value, (list, tuple, set, frozenset)):
            values.update(str(item) for item in value)
        else:
            values.add(str(value))

    assert "CLOSE_PROFIT" in values
    assert {"PENDING", "PARTIAL", "COMPLETED", "AMBIGUOUS"} <= values

    assert "FAILED" not in values
    assert "HOLD" not in values
    assert "ABSTAIN" not in values


def test_decision_expiry_does_not_erase_operational_exit_memory():
    stmt = operational_profit_exit_memory_stmt(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:cycle",
    )

    # expires_at governs whether a decision may still be submitted.
    # Once an operational intent exists, expiry must not erase the durable
    # anti-reopen memory.
    assert "expires_at" not in _where_sql(stmt)


def test_verified_cycle_lookup_requires_exact_source_cycle():
    stmt = operational_profit_exit_memory_stmt(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:verified-cycle",
    )

    assert "source_cycle_id" in _where_sql(stmt)


def test_unverified_cycle_lookup_remains_conservative_within_destination():
    stmt = operational_profit_exit_memory_stmt(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id=None,
    )

    where_sql = _where_sql(stmt)

    # Until a distinct new source cycle can be proven, an existing operational
    # exit memory in this exact destination/asset scope must remain visible.
    assert "source_cycle_id" not in where_sql

    assert "user_id" in where_sql
    assert "execution_epoch_id" in where_sql
    assert "execution_provider" in where_sql
    assert "execution_network" in where_sql
    assert "asset" in where_sql


def test_lookup_prefers_most_recent_operational_memory():
    stmt = operational_profit_exit_memory_stmt(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id=None,
    )

    order_sql = _order_sql(stmt)

    assert "decided_at DESC" in order_sql
    assert "created_at DESC" in order_sql


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeDB:
    def __init__(self, value):
        self.value = value
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _FakeResult(self.value)


@pytest.mark.asyncio
async def test_reader_returns_persisted_operational_memory():
    expected = object()
    db = _FakeDB(expected)

    result = await read_operational_profit_exit_memory(
        db,
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:cycle",
    )

    assert result is expected
    assert db.statement is not None


def test_lookup_selects_ai_profit_exit_decision_entity():
    stmt = operational_profit_exit_memory_stmt(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider="hyperliquid",
        execution_network="mainnet",
        asset="BTC",
        source_cycle_id="mainnet:BTC:cycle",
    )

    assert AIProfitExitDecision.__table__ in stmt.get_final_froms()
