from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.adapters.risex import RISExAdapter
from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
from app.adapters.risex_types import ProviderWriteDisabled
from app.models.entities import JobState
from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import (
    RISExPreparedPlaceOrderRequest,
    prepare_place_order_request,
)
from app.security.risex_pre_order_gate import authorize_pre_order_probe
from tests.unit.risex_replay_test_support import make_test_replay_architecture_attestation
from app.security.risex_signed_testnet_policy import SignedTestnetPolicy
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence
from app.services import queue as queue_service
from app.services import strategy_intents
from app.services.risex_execution_window import (
    RISEX_WINDOW_TTL_SECONDS,
    RISExExecutionState,
    RISExOperationalWindowController,
)
from app.workers import execution_worker, resilient_execution_worker


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
OTHER_SIGNER = '0x' + ('33' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
CHAIN_ID = 11155931
SESSION_EXPIRATION = 4_000_000_000
CONTEXT_FINGERPRINT = 'risex-4b-continuous-context'
CONTINUOUS_GATE3_MODE = 'continuous_window'


class _CommitDB:
    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class _DateClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _evidence(**overrides: object) -> RISExSignerCapabilityEvidence:
    values: dict[str, object] = {
        'network': 'testnet',
        'account': ACCOUNT,
        'signer': SIGNER,
        'chain_id': CHAIN_ID,
        'auth_contract': AUTH,
        'router': ROUTER,
        'session_active': True,
        'session_account': ACCOUNT,
        'session_expiration': SESSION_EXPIRATION,
        'onchain_perps_only_scope': False,
        'perps_order_succeeded': None,
        'fund_movement_rejected': True,
        'withdrawal_rejected': True,
        'post_revoke_order_rejected': None,
        'operatorhub_bypass_disabled': True,
        'perps_permission': True,
        'fund_movement_path_absent': True,
    }
    values.update(overrides)
    return RISExSignerCapabilityEvidence(**values)  # type: ignore[arg-type]


def _gate() -> object:
    policy = SignedTestnetPolicy(
        network='testnet',
        explicit_approval=True,
        deployment_verdict='PASS',
        deployment_identity_verified=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
    )
    return authorize_pre_order_probe(
        policy=policy,
        evidence=_evidence(),
        now=1_900_000_000,
        replay_protection_architecture_attestation=(
            make_test_replay_architecture_attestation(
                request=_request(),
                chain_id=CHAIN_ID,
                authorization_address=AUTH,
                router_address=ROUTER,
            )
        ),
    )


def _request(*, signer: str = SIGNER) -> RISExPreparedPlaceOrderRequest:
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=7,
        ttl_units=0,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=ACCOUNT,
        signer_address=signer,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=SESSION_EXPIRATION - 1,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


def _job(*, attempt_count: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid.uuid4(),
        execution_epoch_id=uuid.uuid4(),
        execution_provider='risex',
        execution_network='testnet',
        state=JobState.PROCESSING,
        attempt_count=attempt_count,
        last_error=None,
        owner='execution-worker-test',
        locked_until=datetime.now(UTC),
        next_attempt_at=None,
        enqueued_at=datetime.now(UTC),
    )


async def _allow_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapters import risex as risex_module

    async def matcher(_db: object, _job: object) -> bool:
        return True

    monkeypatch.setattr(risex_module, 'job_matches_active_destination', matcher, raising=False)


def _open_window(*, clock: _DateClock) -> RISExOperationalWindowController:
    window = RISExOperationalWindowController(
        worker_id='execution-worker-test',
        boot_id=uuid.uuid4(),
        clock=clock,
    )
    request_id = uuid.uuid4()
    assert window.begin_arm(
        request_id=request_id,
        control_generation=1,
        context_fingerprint=CONTEXT_FINGERPRINT,
    )
    window.open_window(
        request_id=request_id,
        control_generation=1,
        context_fingerprint=CONTEXT_FINGERPRINT,
    )
    assert window.state == RISExExecutionState.ENABLED
    return window


def _continuous_authorization(window: RISExOperationalWindowController) -> SimpleNamespace:
    return SimpleNamespace(
        window=window,
        account_address=ACCOUNT,
        signer_address=SIGNER,
        context_fingerprint=CONTEXT_FINGERPRINT,
    )


def _signed_transport(
    *,
    freshness_probe: Any,
) -> tuple[RISExSignedTestnetHTTPTransport, httpx.AsyncClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={'success': True})
        ),
        trust_env=False,
    )
    transport = RISExSignedTestnetHTTPTransport(
        gate=_gate(),  # type: ignore[arg-type]
        client=client,
        freshness_probe=freshness_probe,
    )
    return transport, client, calls


@pytest.mark.asyncio
async def test_prepare_destination_allows_bound_risex_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bound(_db, _job) -> bool:
        return True

    monkeypatch.setattr(queue_service, 'bind_job_to_active_destination', _bound)
    job = SimpleNamespace(
        execution_provider='risex',
        state=JobState.QUEUED,
        last_error=None,
        owner=None,
        locked_until=None,
        next_attempt_at=None,
        enqueued_at=None,
    )

    allowed = await queue_service.prepare_job_destination_for_execution(_CommitDB(), job)

    assert allowed is True
    assert job.state == JobState.QUEUED
    assert job.last_error is None


@pytest.mark.asyncio
async def test_prepare_destination_still_rejects_unknown_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bound(_db, _job) -> bool:
        return True

    monkeypatch.setattr(queue_service, 'bind_job_to_active_destination', _bound)
    job = SimpleNamespace(
        execution_provider='unknown-provider',
        state=JobState.QUEUED,
        last_error=None,
        owner='worker-before-guard',
        locked_until=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        enqueued_at=datetime.now(UTC),
    )

    allowed = await queue_service.prepare_job_destination_for_execution(_CommitDB(), job)

    assert allowed is False
    assert job.state == JobState.SKIPPED
    assert job.owner is None
    assert job.locked_until is None
    assert job.next_attempt_at is None
    assert job.enqueued_at is None


def test_all_four_delivery_callers_keep_the_shared_destination_guard() -> None:
    callers = {
        'publish_job': queue_service.publish_job,
        'repair_stream': queue_service.repair_stream,
        'worker_handle_job_id': execution_worker.Worker.handle_job_id,
        'postgres_fallback': resilient_execution_worker.ResilientExecutionWorker._next_database_job_id,
    }

    for name, caller in callers.items():
        source = inspect.getsource(caller)
        assert 'prepare_job_destination_for_execution(' in source, name
        assert "execution_provider != 'hyperliquid'" not in source, name


def test_worker_routes_risex_after_claim_without_changing_hyperliquid_branch_shape() -> None:
    source = inspect.getsource(execution_worker.Worker.handle_job_id)

    admin_shape = "elif job.origin=='ADMIN_LEVERAGE_SYNC':"
    hyperliquid_shape = 'result=await process_job(db,self.follower_hl(network),job)'
    risex_shape = "if job.execution_provider=='risex':"
    risex_call = 'result=await self._run_risex_copy_job(db,job)'

    assert admin_shape in source
    assert hyperliquid_shape in source
    assert risex_shape in source
    assert risex_call in source

    claim_index = source.index('job=await claim_job')
    risex_index = source.index(risex_shape)
    hyperliquid_index = source.index(hyperliquid_shape)
    assert claim_index < risex_index < hyperliquid_index


def test_worker_exposes_dedicated_continuous_risex_path_with_point_of_use_window_fence() -> None:
    runner = getattr(execution_worker.Worker, '_run_risex_copy_job', None)
    assert callable(runner), 'Step 4B requires a dedicated Worker._run_risex_copy_job path'

    source = inspect.getsource(runner)
    assert '_process_job_locked' not in source
    assert 'process_job(' not in source

    expire_index = source.index('self.risex_window.expire_if_needed()')
    enabled_index = source.index('RISExExecutionState.ENABLED', expire_index)
    continuous_mode_index = source.index("gate3_mode='continuous_window'", enabled_index)
    authorization_index = source.index('continuous_authorization=', continuous_mode_index)
    per_operation_index = source.index('await process_risex_job(', authorization_index)
    assert expire_index < enabled_index < continuous_mode_index < authorization_index < per_operation_index

    assert 'readiness_attestation=' not in source
    assert '_poll_risex_control_once' not in source
    assert 'WorkerHeartbeat' not in source
    assert 'build_risex_execution_status' not in source


@pytest.mark.asyncio
@pytest.mark.parametrize('window_state', ['PAUSED', 'DISABLED'])
async def test_window_unavailable_deferral_preserves_failure_budget(window_state: str) -> None:
    defer = getattr(execution_worker, '_defer_risex_window_unavailable', None)
    assert callable(defer), 'Step 4B requires a dedicated post-claim RISEx window deferral'

    db = _CommitDB()
    job = _job(attempt_count=4)

    result = await defer(db, job, window_state=window_state)

    assert result == JobState.RETRYING.value
    assert job.state == JobState.RETRYING
    assert job.attempt_count == 3, 'claim_job increment must be compensated by deferral'
    assert job.owner is None
    assert job.locked_until is None
    assert job.enqueued_at is None
    assert job.next_attempt_at is not None
    assert window_state in (job.last_error or '')
    assert db.commits == 1


def test_latest_intent_authorization_becomes_provider_aware() -> None:
    signature = inspect.signature(strategy_intents.current_strategy_intent_for_cloid)
    assert 'execution_provider' in signature.parameters

    source = inspect.getsource(strategy_intents.current_strategy_intent_for_cloid)
    assert "job.execution_provider != 'hyperliquid'" not in source
    assert 'job.execution_provider != execution_provider' in source


def test_dedicated_risex_writer_reuses_latest_intent_fence_without_short_lived_fallback() -> None:
    spec = importlib.util.find_spec('app.services.risex_copy_execution')
    assert spec is not None, 'Step 4B requires a dedicated RISEx CopyJob execution service'
    module = importlib.import_module('app.services.risex_copy_execution')
    process_risex_job = getattr(module, 'process_risex_job', None)
    assert callable(process_risex_job)

    writer_source = inspect.getsource(process_risex_job)
    assert 'current_strategy_intent_for_cloid(' in writer_source
    assert "execution_provider='risex'" in writer_source
    assert 'readiness_attestation' not in writer_source


@pytest.mark.asyncio
async def test_continuous_permit_signer_mismatch_locks_window_and_never_defers_or_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    clock = _DateClock(datetime(2026, 9, 15, 20, 0, tzinfo=UTC))
    window = _open_window(clock=clock)

    async def fresh_probe() -> RISExSignerCapabilityEvidence:
        return _evidence()

    transport, client, http_calls = _signed_transport(freshness_probe=fresh_probe)
    adapter = RISExAdapter(
        network='testnet',
        transport=transport,
        gate3_mode=CONTINUOUS_GATE3_MODE,
        continuous_authorization=_continuous_authorization(window),
    )
    try:
        with pytest.raises(ProviderWriteDisabled, match='cause 3|identity|signer'):
            await adapter.place_ioc(
                db=object(),
                job=_job(),
                request=_request(signer=OTHER_SIGNER),
            )
    finally:
        await client.aclose()

    assert window.state == RISExExecutionState.LOCKED
    assert http_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('invalidation', ['disarm', 'expiry'])
async def test_post_freshness_fence_blocks_post_and_defers_when_authorization_ends_during_probe(
    monkeypatch: pytest.MonkeyPatch,
    invalidation: str,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    clock = _DateClock(datetime(2026, 9, 15, 20, 0, tzinfo=UTC))
    window = _open_window(clock=clock)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def delayed_positive_probe() -> RISExSignerCapabilityEvidence:
        probe_started.set()
        await release_probe.wait()
        return _evidence()

    transport, client, http_calls = _signed_transport(freshness_probe=delayed_positive_probe)
    adapter = RISExAdapter(
        network='testnet',
        transport=transport,
        gate3_mode=CONTINUOUS_GATE3_MODE,
        continuous_authorization=_continuous_authorization(window),
    )
    task = asyncio.create_task(
        adapter.place_ioc(db=object(), job=_job(), request=_request())
    )
    try:
        await asyncio.wait_for(probe_started.wait(), timeout=1)
        if invalidation == 'disarm':
            assert window.disarm(control_generation=2, reason='explicit DISARM during freshness')
        else:
            clock.value = clock.value + timedelta(seconds=RISEX_WINDOW_TTL_SECONDS + 1)
            assert window.expire_if_needed() is True
        assert window.state == RISExExecutionState.DISABLED
        release_probe.set()
        with pytest.raises(ProviderWriteDisabled, match='cause 3|window|authorization|disabled|expired'):
            await task
    finally:
        release_probe.set()
        if not task.done():
            task.cancel()
        await client.aclose()

    assert http_calls == []

    defer = getattr(execution_worker, '_defer_risex_window_unavailable', None)
    assert callable(defer)
    db = _CommitDB()
    claimed_job = _job(attempt_count=4)
    result = await defer(db, claimed_job, window_state=window.state.value)
    assert result == JobState.RETRYING.value
    assert claimed_job.attempt_count == 3
    assert db.commits == 1


@pytest.mark.asyncio
async def test_post_freshness_context_invalidation_is_locked_and_never_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('RISEX_SIGNED_WRITES_ENABLED', 'true')
    await _allow_epoch(monkeypatch)
    clock = _DateClock(datetime(2026, 9, 15, 20, 0, tzinfo=UTC))
    window = _open_window(clock=clock)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def delayed_positive_probe() -> RISExSignerCapabilityEvidence:
        probe_started.set()
        await release_probe.wait()
        return _evidence()

    transport, client, http_calls = _signed_transport(freshness_probe=delayed_positive_probe)
    adapter = RISExAdapter(
        network='testnet',
        transport=transport,
        gate3_mode=CONTINUOUS_GATE3_MODE,
        continuous_authorization=_continuous_authorization(window),
    )
    task = asyncio.create_task(
        adapter.place_ioc(db=object(), job=_job(), request=_request())
    )
    try:
        await asyncio.wait_for(probe_started.wait(), timeout=1)
        window.lock('security-relevant context mismatch during freshness')
        assert window.state == RISExExecutionState.LOCKED
        release_probe.set()
        with pytest.raises(ProviderWriteDisabled, match='cause 3|window|authorization|context|locked'):
            await task
    finally:
        release_probe.set()
        if not task.done():
            task.cancel()
        await client.aclose()

    assert window.state == RISExExecutionState.LOCKED
    assert http_calls == []


def test_step_4b_explicitly_leaves_fresh_risex_operational_truth_for_step_4c() -> None:
    """Scope guard: 4B may submit RISEx orders, but reconciliation remains Hyperliquid-only."""

    reconcile_source = inspect.getsource(execution_worker.Worker.run_reconcile_if_leader)
    observability_source = inspect.getsource(execution_worker.Worker._refresh_follower_observability)

    assert 'follower_hl=self.follower_hl(network)' in reconcile_source
    assert 'resolve_ambiguous_executions(db,follower_hl)' in reconcile_source
    assert 'RISExAdapter' not in reconcile_source

    assert 'follower_hl=self.follower_hl(network)' in observability_source
    assert 'snapshot=await follower_hl.account_snapshot(account.account_address)' in observability_source
    assert 'RISExAdapter' not in observability_source
