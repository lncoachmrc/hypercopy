from __future__ import annotations

import importlib
import os
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Numeric

from app.adapters.hyperliquid import deterministic_cloid
from app.db.schema import EXPECTED_REVISION
from app.models.entities import Execution, ExecutionState
from app.services import risex_copy_execution, risex_order_preparation


def _require(module: object, name: str):
    value = getattr(module, name, None)
    assert value is not None, f"RED: expected {module.__name__}.{name}"
    return value


def test_execution_has_dedicated_nullable_uint64_client_order_id_column_and_partial_unique_lookup_index():
    table = Execution.__table__
    assert "client_order_id" in table.c, "RED: Execution.client_order_id column is missing"

    column = table.c.client_order_id
    assert column.nullable is True
    assert isinstance(column.type, Numeric)
    assert column.type.precision == 20
    assert column.type.scale == 0

    assert "reserved_exposure_usdc" in table.c, (
        "RED: the durable reservation amount must be stored on Execution"
    )
    reserved = table.c.reserved_exposure_usdc
    assert reserved.nullable is True
    assert isinstance(reserved.type, Numeric)
    assert reserved.type.precision == 30
    assert reserved.type.scale == 12

    matching = [
        index
        for index in table.indexes
        if [item.name for item in index.columns]
        == ["execution_provider", "execution_network", "client_order_id"]
    ]
    assert matching, "RED: provider/network/client_order_id lookup index is missing"
    unique = [index for index in matching if index.unique]
    assert unique, (
        "RED: provider/network/client_order_id must be unique so 4C lookup is unambiguous"
    )
    where = unique[0].dialect_options["postgresql"].get("where")
    assert where is not None
    assert "client_order_id IS NOT NULL" in str(where)


def test_0014_is_registered_and_is_additive_only():
    backend_root = Path(__file__).resolve().parents[2]
    repo_root = backend_root.parent
    migration = backend_root / "alembic" / "versions" / "0014_risex_client_order_id.py"

    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    actual_head = ScriptDirectory.from_config(alembic_config).get_current_head()
    assert EXPECTED_REVISION == actual_head
    assert migration.exists(), "RED: migration 0014_risex_client_order_id.py is missing"

    source = migration.read_text()
    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]

    # 0014 is intentionally narrow: nullable columns + indexes only.
    assert upgrade.count("op.add_column(") == 2
    assert "op.create_index(" in upgrade
    assert "client_order_id" in upgrade
    assert "reserved_exposure_usdc" in upgrade
    assert "nullable=True" in upgrade
    assert "unique=True" in upgrade
    assert "postgresql_where" in upgrade

    for forbidden in (
        "op.alter_column(",
        "op.create_table(",
        "op.drop_column(",
        "op.drop_table(",
        "op.execute(",
        "op.create_unique_constraint(",
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


def test_same_job_id_produces_same_client_order_id_after_module_reload():
    derive = _require(risex_copy_execution, "client_order_id_for_job")
    job_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    before_restart = derive(job_id, "o")

    reloaded = importlib.reload(risex_copy_execution)
    derive_after_restart = _require(reloaded, "client_order_id_for_job")
    after_restart = derive_after_restart(job_id, "o")

    assert after_restart == before_restart


def test_risex_reservation_is_the_nonterminal_execution_and_survives_ambiguity():
    reserved = _require(risex_copy_execution, "reserved_exposure_usdc")

    base = dict(
        execution_provider="risex",
        execution_network="mainnet",
        requested_size=100,
        limit_px=25,
        reserved_exposure_usdc=2500,
        reduce_only=False,
    )
    submitting = SimpleNamespace(**base, state=ExecutionState.SUBMITTING)
    unknown = SimpleNamespace(**base, state=ExecutionState.UNKNOWN)
    filled = SimpleNamespace(**base, state=ExecutionState.FILLED)
    rejected = SimpleNamespace(**base, state=ExecutionState.REJECTED)
    testnet = SimpleNamespace(
        **{**base, "execution_network": "testnet"}, state=ExecutionState.SUBMITTING
    )
    reducing = SimpleNamespace(
        **{**base, "reduce_only": True}, state=ExecutionState.SUBMITTING
    )

    assert reserved(submitting) == 2500
    assert reserved(unknown) == 2500
    assert reserved(filled) == 0
    assert reserved(rejected) == 0
    assert reserved(testnet) == 0
    assert reserved(reducing) == 0


def test_provider_confirmed_full_fill_is_terminal_and_releases_reservation():
    classify = _require(risex_copy_execution, "classify_risex_submission_response")

    outcome = classify(
        status_code=200,
        payload={"order_id": "risex-order-1", "filled_quantity": "0.5"},
        requested_size=Decimal("0.5"),
    )

    assert outcome.definitive is True
    assert outcome.execution_state == ExecutionState.FILLED
    assert outcome.reservation_active is False
    assert outcome.provider_order_id == "risex-order-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True},
        {"order_id": "risex-order-1"},
        {"filled_quantity": "0.5"},
        {"unexpected": "shape"},
        {},
    ],
)
def test_http_200_with_unexpected_or_incomplete_payload_stays_submitting_and_keeps_reservation(
    payload,
):
    classify = _require(risex_copy_execution, "classify_risex_submission_response")

    outcome = classify(
        status_code=200,
        payload=payload,
        requested_size=Decimal("0.5"),
    )

    assert outcome.definitive is False
    assert outcome.execution_state == ExecutionState.SUBMITTING
    assert outcome.reservation_active is True


def test_explicit_provider_rejection_is_terminal_and_releases_reservation():
    classify = _require(risex_copy_execution, "classify_risex_submission_response")

    outcome = classify(
        status_code=400,
        payload={
            "success": False,
            "error": {"code": "ORDER_REJECTED", "message": "order rejected"},
        },
        requested_size=Decimal("0.5"),
    )

    assert outcome.definitive is True
    assert outcome.execution_state == ExecutionState.REJECTED
    assert outcome.reservation_active is False


def test_transport_ambiguity_moves_to_unknown_and_keeps_reservation():
    transition = _require(risex_copy_execution, "submission_transition")

    ambiguous = transition("AMBIGUOUS")
    assert ambiguous.execution_state == ExecutionState.UNKNOWN
    assert ambiguous.reservation_active is True
    assert ambiguous.job_terminal is False


def test_network_policy_defaults_testnet_and_blocks_mainnet_before_signing():
    gate = _require(risex_order_preparation, "assert_risex_execution_network_allowed")

    assert gate() == "testnet"
    assert gate("testnet") == "testnet"
    with pytest.raises(RuntimeError, match="ADR-0006|mainnet"):
        gate("mainnet")


@pytest.mark.asyncio
async def test_mainnet_is_blocked_before_signing_or_provider_io_even_when_signed_writes_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = {"signer": 0, "api": 0}

    class NoProviderIO:
        async def get_json(self, *_args, **_kwargs):
            calls["api"] += 1
            raise AssertionError("mainnet gate must run before provider I/O")

    def signer_loader(*_args, **_kwargs):
        calls["signer"] += 1
        raise AssertionError("mainnet gate must run before signer loading")

    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        risex_order_preparation,
        "load_testnet_signer_credential",
        signer_loader,
    )

    with pytest.raises(RuntimeError, match="ADR-0006|mainnet"):
        await risex_order_preparation.prepare_risex_ioc_request(
            env=os.environ,
            api=NoProviderIO(),
            rpc=object(),
            network="mainnet",
            symbol="BTC",
            side="BUY",
            use_min_order_size=True,
            slippage_bps=25,
            deadline_seconds=30,
            client_order_id_factory=lambda: 42,
        )

    assert calls == {"signer": 0, "api": 0}


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
