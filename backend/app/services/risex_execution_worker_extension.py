from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import select

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.adapters.risex_types import ProviderDataMalformed, ProviderReadUnavailable
from app.models.entities import RISExExecutionControl, WorkerHeartbeat
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)
from app.security.risex_signed_testnet_policy import (
    SignedTestnetBlocked,
    reject_main_wallet_key_inputs,
)
from app.services.risex_order_preparation import ADR_0006_MAINNET_GATE_ACCEPTED
from app.services.risex_execution_control import (
    arm_finalization_fence,
    claim_control_request,
)
from app.services.risex_execution_window import (
    RISEX_HEARTBEAT_STALE_SECONDS,
    RISExExecutionState,
    RISExOperationalWindowController,
    fresh_execution_worker_heartbeats,
)


_RISEX_TESTNET_API_URL = 'https://api.testnet.rise.trade'
_RISEX_TESTNET_RPC_URL = 'https://testnet.riselabs.xyz'
_RISEX_READINESS_TIMEOUT_SECONDS = 60.0
_REQUIRED_ARM_ASSERTIONS = (
    'operatorhub_bypass_disabled',
)
_T = TypeVar('_T')


def _worker_module(worker: Any) -> Any:
    return sys.modules[worker.__class__.__module__]


async def _serialized_window_transition(
    worker: Any,
    transition: Callable[[], _T],
) -> _T:
    """Serialize authorization-ending local transitions with the §10B POST fence."""
    lock = getattr(worker, 'risex_submission_lock', None)
    if lock is None:
        return transition()
    async with lock:
        return transition()


def _current_context_fingerprint(
    worker: Any,
    readiness_assertions: dict[str, bool],
) -> str:
    material = {
        'worker_id': worker.id,
        'boot_id': str(worker.boot_id),
        'build': os.environ.get('RAILWAY_GIT_COMMIT_SHA') or os.environ.get('GIT_COMMIT_SHA') or '',
        'provider': 'risex',
        'environment': os.environ.get('RAILWAY_ENVIRONMENT_NAME') or os.environ.get('APP_ENV') or '',
        'network': 'testnet',
        'api_url': _RISEX_TESTNET_API_URL,
        'rpc_url': _RISEX_TESTNET_RPC_URL,
        'deployment_fingerprint': PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
        'signed_writes_env': os.environ.get('RISEX_SIGNED_WRITES_ENABLED') or '',
        'adr_0006_mainnet_gate_accepted': ADR_0006_MAINNET_GATE_ACCEPTED is True,
        'operatorhub_bypass_disabled': readiness_assertions.get(
            'operatorhub_bypass_disabled'
        ) is True,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


async def _singleton_matches_worker(worker: Any, db: Any) -> bool:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=RISEX_HEARTBEAT_STALE_SECONDS)
    rows = (
        await db.execute(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.service == 'execution-worker',
                WorkerHeartbeat.seen_at >= cutoff,
            )
        )
    ).scalars().all()
    live = fresh_execution_worker_heartbeats(rows, now=now)
    if len(live) != 1:
        return False
    heartbeat = live[0]
    meta = heartbeat.meta or {}
    return bool(
        heartbeat.worker_id == worker.id
        and str(meta.get('boot_id') or '') == str(worker.boot_id)
    )


def _assertions_from_request(request: RISExExecutionControl) -> dict[str, bool]:
    raw = request.readiness_assertions or {}
    return {key: raw.get(key) is True for key in _REQUIRED_ARM_ASSERTIONS}


@dataclass(frozen=True, slots=True)
class RISExGlobalContinuousReadinessAttestation:
    issued_at: float
    deployment_fingerprint: str
    operatorhub_bypass_disabled: bool


async def run_global_risex_continuous_readiness(
    *,
    api: RISExReadOnlyHTTPTransport,
    rpc: RISExReadOnlyRPCTransport,
    operatorhub_bypass_disabled: bool,
) -> RISExGlobalContinuousReadinessAttestation:
    if not PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT:
        raise SignedTestnetBlocked('RISEx pinned deployment fingerprint is unavailable')
    if os.environ.get('RISEX_SIGNED_WRITES_ENABLED') != 'true':
        raise SignedTestnetBlocked('RISEX_SIGNED_WRITES_ENABLED must be explicitly true')
    if ADR_0006_MAINNET_GATE_ACCEPTED is not True:
        raise SignedTestnetBlocked('ADR-0006 mainnet gate is not accepted')
    if operatorhub_bypass_disabled is not True:
        raise SignedTestnetBlocked('operatorhub_bypass_disabled must be explicitly true')

    deployment = await collect_runtime_deployment_evidence(
        api,
        rpc,
        network='testnet',
    )
    report = evaluate_pinned_deployment_preflight(
        deployment,
        expected_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    )
    if report.verdict != 'PASS' or report.deployment_identity_verified is not True:
        raise SignedTestnetBlocked('RISEx global continuous deployment readiness failed')

    return RISExGlobalContinuousReadinessAttestation(
        issued_at=time.time(),
        deployment_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
        operatorhub_bypass_disabled=True,
    )


async def _run_readiness(
    readiness_assertions: dict[str, bool],
) -> RISExGlobalContinuousReadinessAttestation:
    async with RISExReadOnlyHTTPTransport(base_url=_RISEX_TESTNET_API_URL) as api:
        async with RISExReadOnlyRPCTransport(rpc_url=_RISEX_TESTNET_RPC_URL) as rpc:
            return await run_global_risex_continuous_readiness(
                api=api,
                rpc=rpc,
                operatorhub_bypass_disabled=readiness_assertions.get(
                    'operatorhub_bypass_disabled'
                ) is True,
            )


async def _run_risex_arm_attempt(
    worker: Any,
    request: RISExExecutionControl,
    readiness_assertions: dict[str, bool],
    context_fingerprint: str,
) -> None:
    mod = _worker_module(worker)
    request_id = request.request_id
    generation = int(request.control_generation)
    try:
        result = await asyncio.wait_for(
            _run_readiness(readiness_assertions),
            timeout=_RISEX_READINESS_TIMEOUT_SECONDS,
        )
        if not isinstance(result, RISExGlobalContinuousReadinessAttestation):
            worker.risex_window.cancel_arm(
                'RISEx global continuous readiness did not produce a valid attestation',
                locked=True,
            )
            await worker.heartbeat()
            return

        current_fingerprint = _current_context_fingerprint(worker, readiness_assertions)
        if current_fingerprint != context_fingerprint:
            worker.risex_window.cancel_arm('RISEx security-relevant runtime context changed', locked=True)
            await worker.heartbeat()
            return

        async with mod.SessionLocal() as db:
            fence_ok = await arm_finalization_fence(
                db,
                arm_request_id=request_id,
                arm_control_generation=generation,
                worker_id=worker.id,
                boot_id=worker.boot_id,
            )
            singleton_ok = await _singleton_matches_worker(worker, db) if fence_ok else False
            local_ok = worker.risex_window.can_finalize_arm(
                request_id=request_id,
                control_generation=generation,
                context_fingerprint=context_fingerprint,
            )
            if not (fence_ok and singleton_ok and local_ok):
                worker.risex_window.cancel_arm(
                    'RISEx ARM finalization fence rejected the readiness result',
                    locked=not singleton_ok,
                )
                await db.rollback()
            else:
                worker.risex_window.open_window(
                    request_id=request_id,
                    control_generation=generation,
                    context_fingerprint=context_fingerprint,
                )
                try:
                    await db.commit()
                except Exception:
                    await _serialized_window_transition(
                        worker,
                        lambda: worker.risex_window.lock('RISEx finalization transaction failed'),
                    )
                    raise
        await worker.heartbeat()
    except asyncio.CancelledError:
        attempt = worker.risex_window.arm_attempt
        if attempt is not None and attempt.request_id == request_id:
            worker.risex_window.cancel_arm('RISEx ARM attempt canceled before finalization')
            try:
                await worker.heartbeat()
            except Exception:
                pass
        raise
    except (asyncio.TimeoutError, ProviderReadUnavailable) as exc:
        if worker.risex_window.can_finalize_arm(
            request_id=request_id,
            control_generation=generation,
            context_fingerprint=context_fingerprint,
        ):
            worker.risex_window.cancel_arm(
                f'RISEx readiness unavailable: {type(exc).__name__}',
                locked=False,
            )
        try:
            await worker.heartbeat()
        except Exception:
            pass
    except (ProviderDataMalformed, SignedTestnetBlocked) as exc:
        if worker.risex_window.can_finalize_arm(
            request_id=request_id,
            control_generation=generation,
            context_fingerprint=context_fingerprint,
        ):
            worker.risex_window.cancel_arm(
                f'RISEx readiness security failure: {type(exc).__name__}: {exc}',
                locked=True,
            )
        try:
            await worker.heartbeat()
        except Exception:
            pass
    except Exception as exc:
        if worker.risex_window.can_finalize_arm(
            request_id=request_id,
            control_generation=generation,
            context_fingerprint=context_fingerprint,
        ):
            worker.risex_window.cancel_arm(
                f'RISEx readiness/finalization failed ambiguously: {type(exc).__name__}: {exc}',
                locked=True,
            )
        mod.log.warning('RISEx ARM attempt failed closed', exc_info=True)
        try:
            await worker.heartbeat()
        except Exception:
            pass
    finally:
        current = getattr(worker, '_risex_arm_task', None)
        if current is asyncio.current_task():
            worker._risex_arm_task = None


async def _poll_risex_control_once(worker: Any) -> None:
    mod = _worker_module(worker)
    async with mod.SessionLocal() as db:
        request = await claim_control_request(
            db,
            worker_id=worker.id,
            boot_id=worker.boot_id,
        )
    if request is None:
        return

    worker.risex_window.last_control_request_id = request.request_id
    if request.action == 'DISARM':
        await _serialized_window_transition(
            worker,
            lambda: worker.risex_window.disarm(
                control_generation=int(request.control_generation),
                reason=f'operator DISARM: {request.reason}',
            ),
        )
        task = getattr(worker, '_risex_arm_task', None)
        if task is not None and not task.done():
            task.cancel()
        await worker.heartbeat()
        return

    if request.action != 'ARM':
        await _serialized_window_transition(
            worker,
            lambda: worker.risex_window.lock('unknown RISEx control action'),
        )
        await worker.heartbeat()
        return

    readiness_assertions = _assertions_from_request(request)
    context_fingerprint = _current_context_fingerprint(worker, readiness_assertions)
    armed = await _serialized_window_transition(
        worker,
        lambda: worker.risex_window.begin_arm(
            request_id=request.request_id,
            control_generation=int(request.control_generation),
            context_fingerprint=context_fingerprint,
        ),
    )
    if not armed:
        return
    await worker.heartbeat()

    if not all(readiness_assertions.values()):
        worker.risex_window.cancel_arm('RISEx ARM is missing required global readiness assertions', locked=True)
        await worker.heartbeat()
        return

    try:
        async with mod.SessionLocal() as db:
            singleton_ok = await _singleton_matches_worker(worker, db)
    except Exception:
        singleton_ok = False
    if not singleton_ok:
        worker.risex_window.cancel_arm('RISEx singleton execution-worker invariant is not established', locked=True)
        await worker.heartbeat()
        return

    previous = getattr(worker, '_risex_arm_task', None)
    if previous is not None and not previous.done():
        previous.cancel()
    worker._risex_arm_task = asyncio.create_task(
        _run_risex_arm_attempt(worker, request, readiness_assertions, context_fingerprint)
    )


async def _maintain_risex_window_once(worker: Any) -> None:
    changed = bool(await _serialized_window_transition(worker, worker.risex_window.expire_if_needed))
    if worker.risex_window.state in {
        RISExExecutionState.ARMING,
        RISExExecutionState.ENABLED,
        RISExExecutionState.PAUSED,
    }:
        mod = _worker_module(worker)
        try:
            async with mod.SessionLocal() as db:
                singleton_ok = await _singleton_matches_worker(worker, db)
        except Exception:
            singleton_ok = False
        if not singleton_ok:
            await _serialized_window_transition(
                worker,
                lambda: worker.risex_window.lock('RISEx singleton invariant cannot be established'),
            )
            task = getattr(worker, '_risex_arm_task', None)
            if task is not None and not task.done():
                task.cancel()
            changed = True
    if changed:
        try:
            await worker.heartbeat()
        except Exception:
            pass


def install_risex_window(worker_cls: type[Any]) -> None:
    """Install ADR-0004 lifecycle hooks without changing Hyperliquid job execution."""

    original_init = worker_cls.__init__

    def __init__(self: Any) -> None:
        original_init(self)
        self.boot_id = uuid.uuid4()
        self.risex_window = RISExOperationalWindowController(
            worker_id=self.id,
            boot_id=self.boot_id,
        )
        self.risex_submission_lock = asyncio.Lock()
        self._risex_arm_task = None

    async def heartbeat(self: Any) -> None:
        mod = _worker_module(self)
        async with mod.SessionLocal() as db:
            hb = await db.get(WorkerHeartbeat, self.id)
            if not hb:
                hb = WorkerHeartbeat(worker_id=self.id, service='execution-worker')
                db.add(hb)
            hb.seen_at = datetime.now(UTC)
            hb.current_job_id = self.current_job
            hb.meta = {
                **(hb.meta or {}),
                'boot_id': str(self.boot_id),
                'risex': self.risex_window.telemetry(),
            }
            await db.commit()

    async def poll(self: Any) -> None:
        await _poll_risex_control_once(self)

    async def maintain_window(self: Any) -> None:
        await _maintain_risex_window_once(self)

    async def consume(self: Any) -> None:
        mod = _worker_module(self)
        await mod.ensure_group(self.redis)
        while not mod.stop.is_set():
            try:
                await self._poll_risex_control_once()
                messages = await self.redis.xreadgroup(
                    mod.settings.STREAM_GROUP,
                    self.id,
                    {mod.settings.STREAM_NAME: '>'},
                    count=1,
                    block=2000,
                )
                if not messages:
                    await self.heartbeat()
                    continue
                for _, entries in messages:
                    for message_id, data in entries:
                        ack = await self.handle_job_id(data.get('job_id', ''))
                        if ack:
                            await self.redis.xack(
                                mod.settings.STREAM_NAME,
                                mod.settings.STREAM_GROUP,
                                message_id,
                            )
            except Exception:
                mod.log.exception('Worker consume loop failed')
                await asyncio.sleep(2)

    async def maintenance(self: Any) -> None:
        mod = _worker_module(self)
        while not mod.stop.is_set():
            try:
                await self._poll_risex_control_once()
                await self._maintain_risex_window_once()
                async with mod.SessionLocal() as db:
                    await mod._quarantine_stale_admin_leverage_jobs(db)
                    await mod.release_stale_jobs(db)
                    await mod.repair_stream(self.redis, db)
                    await mod.monitor_credential_expiry(db, self.redis)

                await self._run_reconcile_with_deadline()

                async with mod.SessionLocal() as db:
                    await mod.repair_stream(self.redis, db)
                await self.heartbeat()
            except Exception:
                mod.log.warning('Worker maintenance failed', exc_info=True)
            await asyncio.sleep(mod.settings.RECONCILE_INTERVAL_SECONDS)

    async def run(self: Any) -> None:
        mod = _worker_module(self)
        async with mod.SessionLocal() as db:
            await mod.assert_schema(db)
        await self.heartbeat()
        mod.log.info(
            'Execution worker networks',
            extra={'master_network': mod.settings.master_network, 'follower_network_mode': 'per-user'},
        )
        consume_task = asyncio.create_task(self.consume())
        maintenance_task = asyncio.create_task(self.maintenance())
        await mod.stop.wait()
        maintenance_task.cancel()
        await asyncio.gather(maintenance_task, return_exceptions=True)
        arm_task = getattr(self, '_risex_arm_task', None)
        if arm_task is not None and not arm_task.done():
            arm_task.cancel()
            await asyncio.gather(arm_task, return_exceptions=True)
        try:
            await asyncio.wait_for(consume_task, timeout=55)
        except asyncio.TimeoutError:
            consume_task.cancel()
            await asyncio.gather(consume_task, return_exceptions=True)

    worker_cls.__init__ = __init__
    worker_cls.heartbeat = heartbeat
    worker_cls._poll_risex_control_once = poll
    worker_cls._maintain_risex_window_once = maintain_window
    worker_cls.consume = consume
    worker_cls.maintenance = maintenance
    worker_cls.run = run
