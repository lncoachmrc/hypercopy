from __future__ import annotations

import importlib
import inspect as pyinspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import inspect as sa_inspect

from app.api import admin as admin_module
from app.db import schema as schema_module
from app.models import entities as entities_module
from app.models.entities import WorkerHeartbeat
from app.workers import execution_worker as worker_module


def _build_worker(monkeypatch: pytest.MonkeyPatch) -> worker_module.Worker:
    monkeypatch.setattr(worker_module, 'replica_identity', lambda: 'execution-worker-test')
    monkeypatch.setattr(worker_module, 'redis_client', lambda: MagicMock())
    monkeypatch.setattr(worker_module, 'WeightedRateLimiter', lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(worker_module, 'HyperliquidAdapter', lambda *_args, **_kwargs: MagicMock())
    return worker_module.Worker()


def test_0013_schema_revision_and_migration_are_additive_only() -> None:
    assert schema_module.EXPECTED_REVISION == '0013_risex_execution_control'

    backend_root = Path(__file__).resolve().parents[2]
    migration = backend_root / 'alembic' / 'versions' / '0013_risex_execution_control.py'
    assert migration.exists(), '0013_risex_execution_control.py must exist'

    source = migration.read_text()
    upgrade_source = source.split('def downgrade()', 1)[0]
    assert 'op.create_table(' in upgrade_source
    assert 'risex_execution_control' in upgrade_source
    assert 'op.create_index(' in upgrade_source
    for forbidden in ('op.add_column(', 'op.alter_column(', 'op.drop_column(', 'op.execute('):
        assert forbidden not in upgrade_source
    assert 'UPDATE ' not in upgrade_source.upper()


def test_dedicated_control_model_exposes_one_shot_columns() -> None:
    model = getattr(entities_module, 'RISExExecutionControl', None)
    assert model is not None, 'dedicated RISExExecutionControl model is required'

    columns = set(sa_inspect(model).columns.keys())
    assert {
        'request_id',
        'control_generation',
        'action',
        'target_worker_id',
        'target_boot_id',
        'state',
        'requested_at',
        'requested_by',
        'reason',
        'consumed_at',
        'consumed_by_worker_id',
        'consumed_by_boot_id',
        'supersedes_request_id',
    } <= columns


def test_worker_generates_a_fresh_process_local_boot_id(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _build_worker(monkeypatch)
    second = _build_worker(monkeypatch)

    assert isinstance(first.boot_id, uuid.UUID)
    assert isinstance(second.boot_id, uuid.UUID)
    assert first.boot_id != second.boot_id
    assert first.risex_window.state.value == 'DISABLED'
    assert second.risex_window.state.value == 'DISABLED'


class _HeartbeatDB:
    def __init__(self) -> None:
        self.heartbeat: WorkerHeartbeat | None = None
        self.committed = False

    async def get(self, _model, _key):
        return self.heartbeat

    def add(self, heartbeat: WorkerHeartbeat) -> None:
        self.heartbeat = heartbeat

    async def commit(self) -> None:
        self.committed = True


class _HeartbeatSession:
    def __init__(self, db: _HeartbeatDB) -> None:
        self.db = db

    async def __aenter__(self) -> _HeartbeatDB:
        return self.db

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        return None


@pytest.mark.asyncio
async def test_heartbeat_publishes_boot_id_and_observational_window_state(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _build_worker(monkeypatch)
    db = _HeartbeatDB()
    monkeypatch.setattr(worker_module, 'SessionLocal', lambda: _HeartbeatSession(db))

    await worker.heartbeat()

    assert db.committed is True
    assert db.heartbeat is not None
    meta = db.heartbeat.meta or {}
    assert meta.get('boot_id') == str(worker.boot_id)
    assert meta.get('risex', {}).get('reported_state') == 'DISABLED'
    assert meta.get('risex', {}).get('authorization_status') != 'AUTHORIZED'


class _OneIdleReadRedis:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def xreadgroup(self, *_args, **_kwargs):
        self.events.append('redis-read')
        worker_module.stop.set()
        return []


@pytest.mark.asyncio
async def test_consume_loop_polls_control_channel_before_each_idle_block(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _build_worker(monkeypatch)
    events: list[str] = []
    worker.redis = _OneIdleReadRedis(events)
    worker.heartbeat = AsyncMock()
    worker._poll_risex_control_once = AsyncMock(side_effect=lambda: events.append('control-poll'))
    monkeypatch.setattr(worker_module, 'ensure_group', AsyncMock())

    worker_module.stop.clear()
    try:
        await worker.consume()
    finally:
        worker_module.stop.clear()

    assert events[:2] == ['control-poll', 'redis-read']
    worker._poll_risex_control_once.assert_awaited_once()


def test_window_state_machine_exposes_adr_0004_states_and_bounds() -> None:
    module = importlib.import_module('app.services.risex_execution_window')

    assert [state.value for state in module.RISExExecutionState] == [
        'DISABLED',
        'ARMING',
        'ENABLED',
        'PAUSED',
        'LOCKED',
    ]
    assert module.RISEX_WINDOW_TTL_SECONDS == 24 * 60 * 60
    assert module.RISEX_HEARTBEAT_STALE_SECONDS == 180

    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    boot_id = uuid.uuid4()
    controller = module.RISExOperationalWindowController(
        worker_id='execution-worker-test',
        boot_id=boot_id,
        clock=lambda: now,
    )
    request_id = uuid.uuid4()
    controller.begin_arm(
        request_id=request_id,
        control_generation=10,
        context_fingerprint='context-v1',
    )
    assert controller.state.value == 'ARMING'

    controller.open_window(
        request_id=request_id,
        control_generation=10,
        context_fingerprint='context-v1',
    )
    assert controller.state.value == 'ENABLED'
    assert controller.expires_at == now + timedelta(hours=24)

    controller.pause('rpc unreachable')
    assert controller.state.value == 'PAUSED'
    controller.resume_after_positive_probe()
    assert controller.state.value == 'ENABLED'
    controller.lock('security-negative response')
    assert controller.state.value == 'LOCKED'
    assert controller.expires_at is None


def test_disarm_cancels_an_inflight_arm_attempt_fail_closed() -> None:
    module = importlib.import_module('app.services.risex_execution_window')

    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    controller = module.RISExOperationalWindowController(
        worker_id='execution-worker-test',
        boot_id=uuid.uuid4(),
        clock=lambda: now,
    )
    request_id = uuid.uuid4()
    controller.begin_arm(
        request_id=request_id,
        control_generation=41,
        context_fingerprint='context-v1',
    )

    controller.disarm(control_generation=42, reason='operator disarm')

    assert controller.state.value == 'DISABLED'
    assert controller.arm_attempt is None
    assert controller.can_finalize_arm(
        request_id=request_id,
        control_generation=41,
        context_fingerprint='context-v1',
    ) is False


def test_control_creation_and_finalization_share_a_database_serialization_fence() -> None:
    module = importlib.import_module('app.services.risex_execution_control')

    acquire = getattr(module, '_acquire_control_fence_lock', None)
    create_request = getattr(module, 'create_control_request', None)
    finalize = getattr(module, 'arm_finalization_fence', None)
    assert callable(acquire)
    assert callable(create_request)
    assert callable(finalize)

    acquire_source = pyinspect.getsource(acquire)
    create_source = pyinspect.getsource(create_request)
    finalize_source = pyinspect.getsource(finalize)
    assert 'pg_advisory_xact_lock' in acquire_source
    assert '_acquire_control_fence_lock' in create_source
    assert '_acquire_control_fence_lock' in finalize_source


def test_stale_heartbeat_never_reports_current_authorization() -> None:
    module = importlib.import_module('app.services.risex_execution_window')
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    heartbeat = WorkerHeartbeat(
        worker_id='execution-worker-test',
        service='execution-worker',
        seen_at=now - timedelta(seconds=181),
        meta={
            'boot_id': str(uuid.uuid4()),
            'risex': {
                'reported_state': 'ENABLED',
                'opened_at': (now - timedelta(hours=1)).isoformat(),
                'expires_at': (now + timedelta(hours=23)).isoformat(),
            },
        },
    )

    status = module.build_risex_execution_status(
        heartbeats=[heartbeat],
        control_request=None,
        now=now,
    )

    assert status['reported_state'] == 'ENABLED'
    assert status['observability'] == 'STALE'
    assert status['authorization_status'] == 'UNKNOWN'
    assert status['remaining_seconds'] is None


def _find_admin_route(path: str, method: str):
    for route in admin_module.router.routes:
        if getattr(route, 'path', None) == path and method in getattr(route, 'methods', set()):
            return route
    return None


def test_risex_control_endpoint_reuses_superadmin_and_csrf_dependencies() -> None:
    route = _find_admin_route('/admin/risex-execution-control', 'POST')
    assert route is not None
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert admin_module.superadmin in dependency_calls
    assert admin_module.require_csrf in dependency_calls


def test_risex_status_endpoint_is_read_only_admin_visible() -> None:
    route = _find_admin_route('/admin/risex-execution-status', 'GET')
    assert route is not None
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert admin_module.admin in dependency_calls
    assert admin_module.require_csrf not in dependency_calls
