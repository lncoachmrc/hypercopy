"""STOP 3 case (A) RED contracts: RISEx provider truth and two-phase settlement."""

from __future__ import annotations

import importlib
import inspect
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.adapters import risex_signed_testnet_http
from app.adapters.hyperliquid import OrderOutcome, parse_order_response
from app.db.position_ledger_lock import position_ledger_lock_engine
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionEpoch,
    ExecutionState,
    JobState,
    PositionLedger,
    User,
    UserState,
)
from app.services import execution as hyperliquid_execution
from app.services import risex_copy_execution, risex_worker_submission
from app.workers import execution_worker


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    yield
    await engine.dispose()
    await position_ledger_lock_engine.dispose()


def _provider_truth_module():
    try:
        module = importlib.import_module("app.services.risex_provider_truth")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED: RISEx provider-truth service is missing: {exc}")
    return module


def _terminal_evidence(
    *,
    state: ExecutionState = ExecutionState.FILLED,
    order_id: str = "risex-order-1",
    filled: str = "0.5",
    reason: str | None = None,
) -> dict:
    return {
        "risex_case_a": {
            "terminal_outcome": {
                "definitive": True,
                "execution_state": state.value,
                "provider_order_id": order_id,
                "filled_quantity": filled,
                "reason": reason,
                "status_code": 200,
                "provider_response": {
                    "success": True,
                    "order_id": order_id,
                    "filled_quantity": filled,
                },
            }
        }
    }


async def _seed_case(
    *,
    suffix: str,
    terminal_evidence: bool = True,
    execution_state: ExecutionState = ExecutionState.SUBMITTING,
    ledger_size: Decimal = Decimal("0.1"),
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        db.add(User(id=user_id, auth_wallet=wallet, state=UserState.SUSPENDED))
        await db.flush()
        db.add(
            ExecutionEpoch(
                id=epoch_id,
                user_id=user_id,
                provider="risex",
                network="testnet",
                account_address=wallet,
                credential_version=1,
                started_at=datetime.now(UTC),
            )
        )
        await db.flush()
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner=f"stop3-{suffix}",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"execution_provider": "risex", "follower_network": "testnet"},
            )
        )
        await db.flush()
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=ledger_size,
                target_size=Decimal("0.5"),
                mark_price=Decimal("90"),
                managed=True,
            )
        )
        db.add(
            Execution(
                id=execution_id,
                copy_job_id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                attempt_kind="o",
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=1 + (uuid.uuid4().int % ((1 << 63) - 1)),
                nonce_anchor=7,
                nonce_bitmap_index=13,
                state=execution_state,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.5"),
                reduce_only=False,
                limit_px=Decimal("100"),
                reserved_exposure_usdc=Decimal("50"),
                response=(
                    _terminal_evidence()
                    if terminal_evidence
                    else {"risex_4b_bis": {"submission_status": "POST_IN_FLIGHT"}}
                ),
            )
        )
        await db.commit()

    return user_id, job_id, execution_id


async def _cleanup(user_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await db.execute(delete(PositionLedger).where(PositionLedger.user_id == user_id))
        await db.execute(delete(Execution).where(Execution.user_id == user_id))
        await db.execute(delete(CopyJob).where(CopyJob.user_id == user_id))
        await db.execute(delete(ExecutionEpoch).where(ExecutionEpoch.user_id == user_id))
        await db.commit()


def test_worker_wires_real_persist_provider_truth_into_process_risex_job_integration() -> None:
    source = inspect.getsource(execution_worker.Worker._run_risex_copy_job)
    assert source.count(
        "persist_provider_truth=persist_risex_provider_truth"
    ) >= 3, (
        "RED: recovery plus both real process_risex_job callsites must receive "
        "the production persist_risex_provider_truth callback"
    )
    assert "_REAL_PROCESS_RISEX_JOB" in source, (
        "RED: compatibility with historical test doubles must not remove the "
        "callback from the real writer path"
    )


def test_hyperliquid_position_lock_network_shape_remains_unchanged_integration() -> None:
    from app.services import execution

    source = inspect.getsource(execution.process_job)
    locked_source = inspect.getsource(execution._process_job_locked)

    assert "async with position_ledger_lock(job.user_id)" in source
    assert "return await _process_job_locked" in source
    assert "await hl.mids()" in locked_source
    assert "await _execute_leg(" in locked_source


def test_provider_truth_reads_before_position_ledger_lock_integration() -> None:
    module = _provider_truth_module()
    persist = getattr(module, "persist_risex_provider_truth", None)
    assert callable(persist), "RED: persist_risex_provider_truth is missing"
    source = inspect.getsource(persist)
    read_index = source.find("read_risex_provider_truth_snapshot")
    lock_index = source.find("position_ledger_lock")
    assert read_index >= 0, "RED: provider truth must perform verified read-only RISEx reads"
    assert lock_index >= 0, "RED: provider truth settlement must use position_ledger_lock"
    assert read_index < lock_index, (
        "RED: RISEx network reads must finish before the PostgreSQL position_ledger_lock"
    )


@pytest.mark.asyncio
async def test_signed_transport_exposes_http_4xx_status_and_body_integration() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "success": False,
                "error": {"code": "UNVERIFIED", "message": "provider-specific"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = object.__new__(
            risex_signed_testnet_http.RISExSignedTestnetHTTPTransport
        )
        transport._client = client
        result = await transport._post_json("/v1/orders/place", json={"x": 1})

    assert getattr(result, "status_code", None) == 409, (
        "RED: received HTTP status must remain visible to the classifier"
    )
    assert result["success"] is False
    assert result["error"]["code"] == "UNVERIFIED"


def test_risex_4xx_rejection_whitelist_starts_empty_integration() -> None:
    outcome = risex_copy_execution.classify_risex_runtime_response(
        status_code=409,
        payload={
            "success": False,
            "error": {"code": "UNVERIFIED", "message": "provider-specific"},
        },
        requested_size=Decimal("0.5"),
    )
    assert outcome.definitive is False, (
        "RED: every RISEx 4xx remains ambiguous until provider rejection "
        "semantics are independently verified"
    )
    assert outcome.execution_state in {
        ExecutionState.SUBMITTING,
        ExecutionState.UNKNOWN,
    }
    assert outcome.reservation_active is True


@pytest.mark.asyncio
async def test_crash_recovery_terminal_evidence_never_posts_second_order_integration() -> None:
    user_id, job_id, execution_id = await _seed_case(suffix="crash-recovery")

    class NoPostAdapter:
        calls = 0

        async def place_ioc(self, **_kwargs):
            self.calls += 1
            raise AssertionError("terminal-evidence recovery must never submit again")

    calls = 0

    async def persist_provider_truth(db, execution):
        nonlocal calls
        calls += 1
        assert execution.id == execution_id
        execution.state = ExecutionState.FILLED
        execution.exchange_oid = "risex-order-1"
        execution.filled_size = Decimal("0.5")
        execution.resolved_at = datetime.now(UTC)
        await db.commit()
        return {
            "provider_truth_persisted": True,
            "provider_truth_settled": True,
        }

    adapter = NoPostAdapter()
    try:
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            result = await risex_copy_execution.process_risex_job(
                db,
                adapter,
                job,
                submission=None,
                persist_provider_truth=persist_provider_truth,
            )

        assert result == JobState.DONE.value
        assert calls == 1
        assert adapter.calls == 0
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            durable_job = await db.get(CopyJob, job_id)
            assert durable is not None and durable_job is not None
            assert durable.state == ExecutionState.FILLED
            assert durable_job.state == JobState.DONE
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_ambiguous_execution_keeps_nonce_reservation_and_blocks_second_post_integration() -> None:
    user_id, job_id, execution_id = await _seed_case(
        suffix="ambiguous",
        terminal_evidence=False,
    )

    class NoPostAdapter:
        calls = 0

        async def place_ioc(self, **_kwargs):
            self.calls += 1
            raise AssertionError("ambiguous recovery must never submit again")

    async def provider_truth_must_not_run(_db, _execution):
        raise AssertionError("case B must wait for 4C, not case-A settlement")

    adapter = NoPostAdapter()
    try:
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            result = await risex_copy_execution.process_risex_job(
                db,
                adapter,
                job,
                submission=None,
                persist_provider_truth=provider_truth_must_not_run,
            )

        assert result == JobState.RETRYING.value
        assert adapter.calls == 0
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
            assert durable.nonce_anchor == 7
            assert durable.nonce_bitmap_index == 13
            assert durable.reserved_exposure_usdc == Decimal("50")
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_provider_truth_settlement_updates_absolute_ledger_and_terminal_execution_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provider_truth_module()
    persist = getattr(module, "persist_risex_provider_truth", None)
    assert callable(persist), "RED: persist_risex_provider_truth is missing"
    user_id, _job_id, execution_id = await _seed_case(suffix="filled")

    verified_at = datetime.now(UTC)

    async def read_snapshot(*_args, **_kwargs):
        return {
            "market_id": 1,
            "position_size": Decimal("0.5"),
            "mark_price": Decimal("101"),
            "verified_at": verified_at,
        }

    monkeypatch.setattr(
        module,
        "read_risex_provider_truth_snapshot",
        read_snapshot,
        raising=False,
    )

    try:
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            result = await persist(db, execution)

        assert result["provider_truth_settled"] is True
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            ledger = (
                await db.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            assert durable is not None
            assert durable.state == ExecutionState.FILLED
            assert durable.exchange_oid == "risex-order-1"
            assert durable.filled_size == Decimal("0.5")
            assert durable.resolved_at is not None
            assert ledger.size == Decimal("0.5")
            assert ledger.target_size == Decimal("0.5")
            assert ledger.mark_price == Decimal("101")
            assert ledger.last_execution_id == execution_id
            assert ledger.exchange_verified_at is not None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_provider_truth_baseline_change_defers_without_writing_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provider_truth_module()
    persist = getattr(module, "persist_risex_provider_truth", None)
    assert callable(persist), "RED: persist_risex_provider_truth is missing"
    user_id, _job_id, execution_id = await _seed_case(suffix="baseline-race")

    async def read_snapshot(*_args, **_kwargs):
        async with SessionLocal() as concurrent:
            ledger = (
                await concurrent.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            ledger.size = Decimal("0.2")
            await concurrent.commit()
        return {
            "market_id": 1,
            "position_size": Decimal("0.5"),
            "mark_price": Decimal("101"),
            "verified_at": datetime.now(UTC),
        }

    monkeypatch.setattr(
        module,
        "read_risex_provider_truth_snapshot",
        read_snapshot,
        raising=False,
    )

    try:
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            result = await persist(db, execution)

        assert result["provider_truth_settled"] is False
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            ledger = (
                await db.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            assert durable is not None
            assert durable.state == ExecutionState.SUBMITTING
            assert ledger.size == Decimal("0.2")
            assert ledger.last_execution_id is None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_other_unresolved_execution_same_user_asset_blocks_provider_truth_write_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provider_truth_module()
    persist = getattr(module, "persist_risex_provider_truth", None)
    assert callable(persist), "RED: persist_risex_provider_truth is missing"
    user_id, _job_id, execution_id = await _seed_case(suffix="unresolved-peer")

    async with SessionLocal() as db:
        current = await db.get(Execution, execution_id)
        assert current is not None
        peer_job_id = uuid.uuid4()
        db.add(
            CopyJob(
                id=peer_job_id,
                user_id=user_id,
                execution_epoch_id=current.execution_epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset="BTC",
                origin="EVENT",
                state=JobState.RETRYING,
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"execution_provider": "risex", "follower_network": "testnet"},
            )
        )
        await db.flush()
        db.add(
            Execution(
                copy_job_id=peer_job_id,
                user_id=user_id,
                execution_epoch_id=current.execution_epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                attempt_kind="o",
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=1 + (uuid.uuid4().int % ((1 << 63) - 1)),
                nonce_anchor=8,
                nonce_bitmap_index=14,
                state=ExecutionState.UNKNOWN,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.1"),
                reduce_only=False,
                limit_px=Decimal("100"),
                reserved_exposure_usdc=Decimal("10"),
                response={"risex_4b_bis": {"submission_status": "POST_IN_FLIGHT"}},
            )
        )
        await db.commit()

    async def read_snapshot(*_args, **_kwargs):
        return {
            "market_id": 1,
            "position_size": Decimal("0.5"),
            "mark_price": Decimal("101"),
            "verified_at": datetime.now(UTC),
        }

    monkeypatch.setattr(
        module,
        "read_risex_provider_truth_snapshot",
        read_snapshot,
        raising=False,
    )

    try:
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            result = await persist(db, execution)

        assert result["provider_truth_settled"] is False
        assert result["reason"] == "other_unresolved_execution_same_asset"
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            ledger = (
                await db.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            assert durable is not None
            assert durable.state == ExecutionState.SUBMITTING
            assert ledger.size == Decimal("0.1")
            assert ledger.last_execution_id is None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_provider_truth_settlement_enters_position_ledger_lock_after_network_read_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provider_truth_module()
    persist = getattr(module, "persist_risex_provider_truth", None)
    assert callable(persist), "RED: persist_risex_provider_truth is missing"
    user_id, _job_id, execution_id = await _seed_case(suffix="lock-order")

    state = {"network_read": False, "lock_entered": False}

    async def read_snapshot(*_args, **_kwargs):
        assert state["lock_entered"] is False
        state["network_read"] = True
        return {
            "market_id": 1,
            "position_size": Decimal("0.5"),
            "mark_price": Decimal("101"),
            "verified_at": datetime.now(UTC),
        }

    @asynccontextmanager
    async def recording_lock(_user_id):
        assert state["network_read"] is True
        state["lock_entered"] = True
        yield

    monkeypatch.setattr(
        module,
        "read_risex_provider_truth_snapshot",
        read_snapshot,
        raising=False,
    )
    monkeypatch.setattr(module, "position_ledger_lock", recording_lock, raising=False)

    try:
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            await persist(db, execution)
        assert state == {"network_read": True, "lock_entered": True}
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_provider_truth_retry_reloads_job_after_optimistic_rollback_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _provider_truth_module()
    persist = getattr(module, "persist_risex_provider_truth", None)
    assert callable(persist)
    user_id, job_id, execution_id = await _seed_case(suffix="rollback-reload")

    async def read_snapshot(*_args, **_kwargs):
        async with SessionLocal() as concurrent:
            ledger = (
                await concurrent.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            ledger.size = Decimal("0.2")
            await concurrent.commit()
        return {
            "market_id": 1,
            "position_size": Decimal("0.5"),
            "mark_price": Decimal("101"),
            "verified_at": datetime.now(UTC),
        }

    monkeypatch.setattr(module, "read_risex_provider_truth_snapshot", read_snapshot)

    class NoPostAdapter:
        calls = 0

        async def place_ioc(self, **_kwargs):
            self.calls += 1
            raise AssertionError("rollback recovery must never submit again")

    adapter = NoPostAdapter()
    try:
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            result = await risex_copy_execution.process_risex_job(
                db,
                adapter,
                job,
                submission=None,
                persist_provider_truth=persist,
            )

        assert result == JobState.RETRYING.value
        assert adapter.calls == 0
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            durable_job = await db.get(CopyJob, job_id)
            assert durable is not None and durable_job is not None
            assert durable.state == ExecutionState.SUBMITTING
            assert durable_job.state == JobState.RETRYING
            assert "ledger_baseline_changed" in str(durable_job.last_error)
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_worker_terminal_evidence_recovery_precedes_disabled_write_window_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def recovered(_db, _job, *, persist_provider_truth):
        nonlocal calls
        calls += 1
        assert persist_provider_truth is execution_worker.persist_risex_provider_truth
        return JobState.DONE.value

    monkeypatch.setattr(execution_worker, "recover_risex_case_a_job", recovered)

    class WindowMustNotRun:
        def expire_if_needed(self):
            raise AssertionError(
                "terminal-evidence recovery must precede the signed-write window gate"
            )

    worker = SimpleNamespace(risex_window=WindowMustNotRun())
    fake_db = SimpleNamespace(execute=lambda *_args, **_kwargs: None)
    result = await execution_worker.Worker._run_risex_copy_job(
        worker,
        fake_db,
        SimpleNamespace(),
    )

    assert result == JobState.DONE.value
    assert calls == 1


@pytest.mark.asyncio
async def test_case_b_unresolved_execution_blocks_worker_repreparation_before_provider_io_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, job_id, execution_id = await _seed_case(
        suffix="case-b-reprepare",
        terminal_evidence=False,
    )

    monkeypatch.setattr(
        risex_worker_submission,
        "assert_risex_worker_write_allowed",
        lambda **_kwargs: None,
    )

    try:
        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            prepared = await risex_worker_submission.prepare_risex_worker_submission(
                db,
                job,
                readiness_assertions={"operatorhub_bypass_disabled": True},
            )

        assert prepared is None
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            assert durable is not None
            assert durable.state == ExecutionState.SUBMITTING
            assert durable.nonce_anchor == 7
            assert durable.nonce_bitmap_index == 13
            assert durable.reserved_exposure_usdc == Decimal("50")
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_state",
    [ExecutionState.REJECTED, ExecutionState.CANCELED],
)
async def test_terminal_no_fill_outcomes_refresh_absolute_ledger_and_finish_skipped_integration(
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: ExecutionState,
) -> None:
    module = _provider_truth_module()
    persist = getattr(module, "persist_risex_provider_truth", None)
    assert callable(persist), "RED: persist_risex_provider_truth is missing"
    user_id, job_id, execution_id = await _seed_case(
        suffix=f"terminal-{terminal_state.value.lower()}"
    )

    verified_at = datetime.now(UTC)

    async def read_snapshot(*_args, **_kwargs):
        return {
            "market_id": 1,
            "position_size": Decimal("0.2"),
            "mark_price": Decimal("101"),
            "verified_at": verified_at,
        }

    monkeypatch.setattr(
        module,
        "read_risex_provider_truth_snapshot",
        read_snapshot,
        raising=False,
    )

    class NoPostAdapter:
        calls = 0

        async def place_ioc(self, **_kwargs):
            self.calls += 1
            raise AssertionError("terminal-evidence recovery must never submit again")

    adapter = NoPostAdapter()
    try:
        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            execution.response = _terminal_evidence(
                state=terminal_state,
                order_id="risex-terminal-no-fill",
                filled="0",
                reason="provider-confirmed terminal no-fill",
            )
            await db.commit()

        async with SessionLocal() as db:
            job = await db.get(CopyJob, job_id)
            assert job is not None
            result = await risex_copy_execution.process_risex_job(
                db,
                adapter,
                job,
                submission=None,
                persist_provider_truth=persist,
            )

        assert result == JobState.SKIPPED.value
        assert adapter.calls == 0
        async with SessionLocal() as db:
            durable = await db.get(Execution, execution_id)
            durable_job = await db.get(CopyJob, job_id)
            ledger = (
                await db.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            assert durable is not None and durable_job is not None
            assert durable.state == terminal_state
            assert durable_job.state == JobState.SKIPPED
            assert ledger.size == Decimal("0.2")
            assert ledger.mark_price == Decimal("101")
            assert ledger.last_execution_id == execution_id
            assert ledger.exchange_verified_at == verified_at
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_state", "filled_size"),
    [
        (ExecutionState.REJECTED, Decimal("0")),
        (ExecutionState.CANCELED, Decimal("0")),
        (ExecutionState.FILLED, Decimal("0.2")),
    ],
)
async def test_risex_copyjob_terminal_state_matches_hyperliquid_integration(
    terminal_state: ExecutionState,
    filled_size: Decimal,
) -> None:
    user_id, risex_job_id, execution_id = await _seed_case(
        suffix=f"parity-{terminal_state.value.lower()}"
    )
    hyperliquid_job_id = uuid.uuid4()

    try:
        async with SessionLocal() as db:
            db.add(
                CopyJob(
                    id=hyperliquid_job_id,
                    user_id=user_id,
                    execution_provider="hyperliquid",
                    execution_network="testnet",
                    asset="BTC",
                    origin="EVENT",
                    state=JobState.PROCESSING,
                    owner=f"hl-parity-{terminal_state.value.lower()}",
                    attempt_count=1,
                    correlation_id=uuid.uuid4().hex,
                    context={
                        "execution_provider": "hyperliquid",
                        "follower_network": "testnet",
                    },
                )
            )
            await db.commit()

        async with SessionLocal() as db:
            hyperliquid_job = await db.get(CopyJob, hyperliquid_job_id)
            assert hyperliquid_job is not None

            if terminal_state == ExecutionState.FILLED:
                hl_outcome = parse_order_response(
                    {
                        "response": {
                            "data": {
                                "statuses": [
                                    {
                                        "filled": {
                                            "oid": 7,
                                            "totalSz": str(filled_size),
                                            "avgPx": "100",
                                        }
                                    }
                                ]
                            }
                        }
                    }
                )
                assert hl_outcome.state == "FILLED"
                assert hl_outcome.filled_size < Decimal("0.5")
                hl_result = await hyperliquid_execution._finish(
                    db,
                    hyperliquid_job,
                    JobState.DONE,
                    None,
                )
            else:
                hl_outcome = OrderOutcome(
                    terminal_state.value,
                    reason=f"hyperliquid-{terminal_state.value.lower()}",
                )
                hl_result = await hyperliquid_execution._finish_action_rejection(
                    db,
                    hyperliquid_job,
                    user_id=user_id,
                    network="testnet",
                    outcome=hl_outcome,
                    leg="parity",
                    rejected_target=Decimal("0.5"),
                    rejected_real=Decimal("0.1"),
                )

            hyperliquid_job_state = JobState(hl_result)

        async with SessionLocal() as db:
            execution = await db.get(Execution, execution_id)
            assert execution is not None
            execution.state = terminal_state
            execution.filled_size = filled_size
            execution.reject_reason = (
                None
                if terminal_state == ExecutionState.FILLED
                else f"risex-{terminal_state.value.lower()}"
            )
            await db.commit()

        async with SessionLocal() as db:
            risex_job = await db.get(CopyJob, risex_job_id)
            execution = await db.get(Execution, execution_id)
            assert risex_job is not None and execution is not None
            risex_result = await risex_copy_execution._finish_case_a_job(
                db,
                risex_job,
                execution,
            )
            assert JobState(risex_result) == hyperliquid_job_state
            assert risex_job.state == hyperliquid_job_state
    finally:
        await _cleanup(user_id)
