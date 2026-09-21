"""RED contracts for RISEx 4B-ter A: nonce persistence + serialized settlement."""

from __future__ import annotations

import inspect
from pathlib import Path

from app.db import schema as db_schema
from app.models.entities import Execution
from app.services import risex_copy_execution


def test_execution_model_persists_risex_nonce_identity() -> None:
    table = Execution.__table__
    assert "nonce_anchor" in table.c, (
        "RED: Execution must persist the exact RISEx permit nonce anchor before POST"
    )
    assert "nonce_bitmap_index" in table.c, (
        "RED: Execution must persist the exact RISEx permit bitmap index before POST"
    )
    assert table.c.nonce_anchor.nullable is True
    assert table.c.nonce_bitmap_index.nullable is True


def test_schema_and_release_preflight_register_0015_nonce_migration() -> None:
    assert db_schema.EXPECTED_REVISION == "0015_risex_execution_nonce", (
        "RED: schema head must advance to additive 0015 RISEx nonce persistence"
    )

    repo_root = Path(__file__).resolve().parents[3]
    migration = repo_root / "backend" / "alembic" / "versions" / "0015_risex_execution_nonce.py"
    assert migration.exists(), "RED: additive 0015 RISEx execution nonce migration is missing"

    preflight = (repo_root / "scripts" / "targeted_release_preflight.py").read_text()
    assert "'0015_risex_execution_nonce.py'" in preflight, (
        "RED: targeted release preflight must register migration 0015"
    )


def test_prepost_persistence_requires_exact_nonce_identity() -> None:
    persist = risex_copy_execution.persist_risex_pre_post_execution
    params = inspect.signature(persist).parameters

    assert "nonce_anchor" in params, (
        "RED: pre-POST persistence must require the signed permit nonce anchor"
    )
    assert "nonce_bitmap_index" in params, (
        "RED: pre-POST persistence must require the signed permit bitmap index"
    )


def test_serialized_terminal_settlement_boundary_exists() -> None:
    settle = getattr(
        risex_copy_execution,
        "settle_risex_execution_under_accounting_lock",
        None,
    )
    assert callable(settle), (
        "RED: terminal RISEx outcomes need one settlement entry point under the "
        "shared accounting advisory lock"
    )

    source = inspect.getsource(settle)
    assert "_acquire_exposure_budget_lock" in source, (
        "RED: settlement must use the same PostgreSQL serialization domain as "
        "pre-POST reservation creation"
    )


def test_process_risex_job_cannot_release_reservation_by_direct_terminal_state_mutation() -> None:
    source = inspect.getsource(risex_copy_execution.process_risex_job)

    assert "settle_risex_execution_under_accounting_lock(" in source, (
        "RED: process_risex_job must route definitive outcomes through serialized settlement"
    )
    assert "existing.state = outcome.execution_state" not in source, (
        "RED: a terminal state assignment outside the shared accounting lock releases "
        "the reservation without serialized provider-truth replacement"
    )
