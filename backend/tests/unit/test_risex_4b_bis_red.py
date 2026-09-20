from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import Numeric

from app.adapters.hyperliquid import deterministic_cloid
from app.db.schema import EXPECTED_REVISION
from app.models.entities import Execution, ExecutionState
from app.services import risex_copy_execution, risex_order_preparation


def _require(module: object, name: str):
    value = getattr(module, name, None)
    assert value is not None, f"RED: expected {module.__name__}.{name}"
    return value


def test_execution_has_dedicated_nullable_uint64_client_order_id_column_and_unique_lookup_index():
    table = Execution.__table__
    assert "client_order_id" in table.c, "RED: Execution.client_order_id column is missing"

    column = table.c.client_order_id
    assert column.nullable is True
    assert isinstance(column.type, Numeric)
    assert column.type.precision == 20
    assert column.type.scale == 0

    matching = [
        index
        for index in table.indexes
        if [item.name for item in index.columns]
        == ["execution_provider", "execution_network", "client_order_id"]
    ]
    assert matching, "RED: provider/network/client_order_id lookup index is missing"
    assert any(index.unique for index in matching), (
        "RED: provider/network/client_order_id must be unique so 4C lookup is unambiguous"
    )


def test_0014_is_registered_and_is_additive_only():
    backend_root = Path(__file__).resolve().parents[2]
    repo_root = backend_root.parent
    migration = backend_root / "alembic" / "versions" / "0014_risex_client_order_id.py"

    assert EXPECTED_REVISION == "0014_risex_client_order_id"
    assert migration.exists(), "RED: migration 0014_risex_client_order_id.py is missing"

    source = migration.read_text()
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    assert "op.add_column(" in upgrade
    assert "op.create_index(" in upgrade
    for forbidden in (
        "op.alter_column(",
        "op.create_table(",
        "op.drop_column(",
        "op.drop_table(",
        "op.execute(",
    ):
        assert forbidden not in upgrade, f"RED: 0014 must remain additive-only; found {forbidden}"

    preflight = (repo_root / "scripts" / "targeted_release_preflight.py").read_text()
    assert "'0014_risex_client_order_id.py'" in preflight


def test_risex_client_order_id_reuses_hyperliquid_cloid_digest_deterministically():
    derive = _require(risex_copy_execution, "client_order_id_for_job")
    job_id = uuid.UUID("12345678-1234-5678-1234-567812345678")

    cloid = deterministic_cloid(str(job_id), "o")
    expected = 1 + (int(cloid[2:], 16) % ((1 << 64) - 1))

    first = derive(job_id, "o")
    second = derive(job_id, "o")

    assert first == expected
    assert second == expected
    assert 0 < first < (1 << 64)


def test_risex_reservation_is_the_nonterminal_execution_and_survives_ambiguity():
    reserved = _require(risex_copy_execution, "reserved_exposure_usdc")

    base = dict(
        execution_provider="risex",
        execution_network="mainnet",
        requested_size=100,
        limit_px=25,
        reduce_only=False,
    )
    submitting = SimpleNamespace(**base, state=ExecutionState.SUBMITTING)
    unknown = SimpleNamespace(**base, state=ExecutionState.UNKNOWN)
    filled = SimpleNamespace(**base, state=ExecutionState.FILLED)
    testnet = SimpleNamespace(**{**base, "execution_network": "testnet"}, state=ExecutionState.SUBMITTING)
    reducing = SimpleNamespace(**{**base, "reduce_only": True}, state=ExecutionState.SUBMITTING)

    assert reserved(submitting) == 2500
    assert reserved(unknown) == 2500
    assert reserved(filled) == 0
    assert reserved(testnet) == 0
    assert reserved(reducing) == 0


def test_submission_state_contract_acknowledged_vs_ambiguous_vs_pre_submit_block():
    transition = _require(risex_copy_execution, "submission_transition")

    acknowledged = transition("ACKNOWLEDGED")
    assert acknowledged.execution_state == ExecutionState.SUBMITTING
    assert acknowledged.reservation_active is True
    assert acknowledged.job_terminal is False

    ambiguous = transition("AMBIGUOUS")
    assert ambiguous.execution_state == ExecutionState.UNKNOWN
    assert ambiguous.reservation_active is True
    assert ambiguous.job_terminal is False

    blocked = transition("PRE_SUBMIT_BLOCKED")
    assert blocked.execution_state == ExecutionState.CANCELED
    assert blocked.reservation_active is False
    assert blocked.job_terminal is True


def test_network_policy_defaults_testnet_and_blocks_mainnet_before_signing():
    gate = _require(risex_order_preparation, "assert_risex_execution_network_allowed")

    assert gate() == "testnet"
    assert gate("testnet") == "testnet"
    with pytest.raises(RuntimeError, match="ADR-0006|mainnet"):
        gate("mainnet")


def test_order_preparation_exposes_application_intent_and_unsigned_plan_boundary():
    intent_type = _require(risex_order_preparation, "RISExOrderIntent")
    plan_builder = _require(risex_order_preparation, "prepare_risex_ioc_plan")

    intent = intent_type(
        symbol="BTC",
        is_buy=True,
        requested_size=1,
        reduce_only=False,
        slippage_bps=25,
        client_order_id=7,
    )

    assert intent.symbol == "BTC"
    assert intent.client_order_id == 7
    assert callable(plan_builder)
