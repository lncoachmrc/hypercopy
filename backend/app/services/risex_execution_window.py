from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.entities import RISExExecutionControl, WorkerHeartbeat


RISEX_WINDOW_TTL_SECONDS = 24 * 60 * 60
RISEX_HEARTBEAT_STALE_SECONDS = 180
WindowClock = Callable[[], datetime]


class RISExExecutionState(str, enum.Enum):
    DISABLED = 'DISABLED'
    ARMING = 'ARMING'
    ENABLED = 'ENABLED'
    PAUSED = 'PAUSED'
    LOCKED = 'LOCKED'


@dataclass(slots=True)
class RISExArmAttempt:
    request_id: uuid.UUID
    control_generation: int
    started_at: datetime
    context_fingerprint: str
    invalidation_epoch_snapshot: int


class RISExOperationalWindowController:
    """Process-local ADR-0004 state. Persisted telemetry is never read back here."""

    def __init__(
        self,
        *,
        worker_id: str,
        boot_id: uuid.UUID,
        clock: WindowClock | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.boot_id = boot_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self.state = RISExExecutionState.DISABLED
        self.arm_attempt: RISExArmAttempt | None = None
        self.opened_at: datetime | None = None
        self.expires_at: datetime | None = None
        self.context_fingerprint: str | None = None
        self.authorization_invalidation_epoch = 0
        self.last_control_generation = 0
        self.last_control_request_id: uuid.UUID | None = None
        now = self._now()
        self.last_transition_at = now
        self.last_transition_reason = 'process boot: RISEx unauthorized'

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _transition(self, state: RISExExecutionState, reason: str) -> None:
        self.state = state
        self.last_transition_at = self._now()
        self.last_transition_reason = reason

    def begin_arm(
        self,
        *,
        request_id: uuid.UUID,
        control_generation: int,
        context_fingerprint: str,
    ) -> bool:
        if control_generation <= self.last_control_generation:
            return False
        self.last_control_generation = int(control_generation)
        self.last_control_request_id = request_id
        self.opened_at = None
        self.expires_at = None
        self.context_fingerprint = None
        self.arm_attempt = RISExArmAttempt(
            request_id=request_id,
            control_generation=int(control_generation),
            started_at=self._now(),
            context_fingerprint=context_fingerprint,
            invalidation_epoch_snapshot=self.authorization_invalidation_epoch,
        )
        self._transition(RISExExecutionState.ARMING, 'explicit ARM request consumed')
        return True

    def can_finalize_arm(
        self,
        *,
        request_id: uuid.UUID,
        control_generation: int,
        context_fingerprint: str,
    ) -> bool:
        attempt = self.arm_attempt
        return bool(
            self.state == RISExExecutionState.ARMING
            and attempt is not None
            and attempt.request_id == request_id
            and attempt.control_generation == int(control_generation)
            and attempt.control_generation == self.last_control_generation
            and attempt.context_fingerprint == context_fingerprint
            and attempt.invalidation_epoch_snapshot == self.authorization_invalidation_epoch
        )

    def open_window(
        self,
        *,
        request_id: uuid.UUID,
        control_generation: int,
        context_fingerprint: str,
    ) -> None:
        if not self.can_finalize_arm(
            request_id=request_id,
            control_generation=control_generation,
            context_fingerprint=context_fingerprint,
        ):
            raise RuntimeError('RISEx ARM finalization is no longer valid')
        now = self._now()
        self.opened_at = now
        self.expires_at = now + timedelta(seconds=RISEX_WINDOW_TTL_SECONDS)
        self.context_fingerprint = context_fingerprint
        self.arm_attempt = None
        self._transition(RISExExecutionState.ENABLED, 'readiness PASS finalized into operational window')

    def disarm(self, *, control_generation: int, reason: str) -> bool:
        if control_generation < self.last_control_generation:
            return False
        self.last_control_generation = int(control_generation)
        self.authorization_invalidation_epoch += 1
        self.arm_attempt = None
        self.opened_at = None
        self.expires_at = None
        self.context_fingerprint = None
        if self.state != RISExExecutionState.LOCKED:
            self._transition(RISExExecutionState.DISABLED, reason)
        else:
            self.last_transition_at = self._now()
            self.last_transition_reason = reason
        return True

    def cancel_arm(self, reason: str, *, locked: bool = False) -> None:
        self.authorization_invalidation_epoch += 1
        self.arm_attempt = None
        self.opened_at = None
        self.expires_at = None
        self.context_fingerprint = None
        self._transition(
            RISExExecutionState.LOCKED if locked else RISExExecutionState.DISABLED,
            reason,
        )

    def pause(self, reason: str) -> None:
        self.expire_if_needed()
        if self.state != RISExExecutionState.ENABLED:
            return
        self._transition(RISExExecutionState.PAUSED, reason)

    def resume_after_positive_probe(self) -> None:
        self.expire_if_needed()
        if self.state != RISExExecutionState.PAUSED:
            return
        if self.expires_at is None or self._now() >= self.expires_at:
            self.disable('operational window expired')
            return
        self._transition(RISExExecutionState.ENABLED, 'trustworthy positive probe after availability pause')

    def lock(self, reason: str) -> None:
        self.authorization_invalidation_epoch += 1
        self.arm_attempt = None
        self.opened_at = None
        self.expires_at = None
        self.context_fingerprint = None
        self._transition(RISExExecutionState.LOCKED, reason)

    def disable(self, reason: str) -> None:
        self.authorization_invalidation_epoch += 1
        self.arm_attempt = None
        self.opened_at = None
        self.expires_at = None
        self.context_fingerprint = None
        self._transition(RISExExecutionState.DISABLED, reason)

    def expire_if_needed(self) -> bool:
        if self.state not in {RISExExecutionState.ENABLED, RISExExecutionState.PAUSED}:
            return False
        if self.expires_at is None or self._now() < self.expires_at:
            return False
        self.disable('operational window reached its 24-hour maximum')
        return True

    def telemetry(self) -> dict[str, Any]:
        self.expire_if_needed()
        now = self._now()
        remaining: int | None = None
        if self.expires_at is not None:
            remaining = max(0, int((self.expires_at - now).total_seconds()))
        if self.state == RISExExecutionState.ENABLED and self.expires_at is not None and self.expires_at > now:
            authorization_status = 'AUTHORIZED'
        elif self.state == RISExExecutionState.ARMING:
            authorization_status = 'PENDING'
        else:
            authorization_status = 'NOT_AUTHORIZED'
        attempt = self.arm_attempt
        return {
            'reported_state': self.state.value,
            'authorization_status': authorization_status,
            'arm_request_id': str(attempt.request_id) if attempt else None,
            'arm_control_generation': attempt.control_generation if attempt else None,
            'arm_started_at': attempt.started_at.isoformat() if attempt else None,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'remaining_seconds': remaining,
            'last_transition_at': self.last_transition_at.isoformat(),
            'last_transition_reason': self.last_transition_reason,
            'last_control_request_id': str(self.last_control_request_id) if self.last_control_request_id else None,
            'last_control_generation': self.last_control_generation,
        }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def fresh_execution_worker_heartbeats(
    heartbeats: Iterable[WorkerHeartbeat],
    *,
    now: datetime,
) -> list[WorkerHeartbeat]:
    now = _aware(now)
    return [
        heartbeat
        for heartbeat in heartbeats
        if heartbeat.service == 'execution-worker'
        and heartbeat.seen_at is not None
        and (now - _aware(heartbeat.seen_at)).total_seconds() <= RISEX_HEARTBEAT_STALE_SECONDS
    ]


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware(parsed)


def _control_payload(control_request: RISExExecutionControl | None) -> dict[str, Any] | None:
    if control_request is None:
        return None
    return {
        'request_id': str(control_request.request_id),
        'control_generation': int(control_request.control_generation),
        'action': control_request.action,
        'state': control_request.state,
        'target_worker_id': control_request.target_worker_id,
        'target_boot_id': str(control_request.target_boot_id),
        'requested_at': control_request.requested_at.isoformat() if control_request.requested_at else None,
        'consumed_at': control_request.consumed_at.isoformat() if control_request.consumed_at else None,
    }


def build_risex_execution_status(
    *,
    heartbeats: Iterable[WorkerHeartbeat],
    control_request: RISExExecutionControl | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an observational read model; never reconstruct authorization from it."""

    current = _aware(now or datetime.now(UTC))
    relevant = [heartbeat for heartbeat in heartbeats if heartbeat.service == 'execution-worker']
    relevant.sort(key=lambda heartbeat: _aware(heartbeat.seen_at), reverse=True)
    fresh = fresh_execution_worker_heartbeats(relevant, now=current)

    if not relevant:
        return {
            'worker_id': None,
            'boot_id': None,
            'heartbeat_seen_at': None,
            'heartbeat_age_seconds': None,
            'reported_state': 'UNKNOWN',
            'observability': 'MISSING',
            'authorization_status': 'UNKNOWN',
            'remaining_seconds': None,
            'live_worker_count': 0,
            'control_request': _control_payload(control_request),
        }

    heartbeat = relevant[0]
    meta = heartbeat.meta or {}
    risex = meta.get('risex') if isinstance(meta.get('risex'), dict) else {}
    reported_state = str(risex.get('reported_state') or 'UNKNOWN')
    age = max(0.0, (current - _aware(heartbeat.seen_at)).total_seconds())
    base: dict[str, Any] = {
        'worker_id': heartbeat.worker_id,
        'boot_id': meta.get('boot_id'),
        'heartbeat_seen_at': _aware(heartbeat.seen_at).isoformat(),
        'heartbeat_age_seconds': age,
        'reported_state': reported_state,
        'arm_request_id': risex.get('arm_request_id'),
        'arm_control_generation': risex.get('arm_control_generation'),
        'arm_started_at': risex.get('arm_started_at'),
        'opened_at': risex.get('opened_at'),
        'expires_at': risex.get('expires_at'),
        'last_transition_at': risex.get('last_transition_at'),
        'last_transition_reason': risex.get('last_transition_reason'),
        'live_worker_count': len(fresh),
        'control_request': _control_payload(control_request),
    }

    if age > RISEX_HEARTBEAT_STALE_SECONDS:
        base.update(
            observability='STALE',
            authorization_status='UNKNOWN',
            remaining_seconds=None,
        )
        return base

    if len(fresh) != 1:
        base.update(
            observability='AMBIGUOUS',
            authorization_status='UNKNOWN',
            remaining_seconds=None,
        )
        return base

    expires_at = _parse_time(risex.get('expires_at'))
    remaining: int | None = None
    if expires_at is not None:
        remaining = max(0, int((expires_at - current).total_seconds()))

    locally_blocked = bool(
        control_request is not None
        and control_request.target_worker_id == heartbeat.worker_id
        and str(control_request.target_boot_id) == str(meta.get('boot_id'))
        and control_request.action == 'DISARM'
        and control_request.state == 'REQUESTED'
    )
    authorized = bool(
        reported_state == RISExExecutionState.ENABLED.value
        and expires_at is not None
        and expires_at > current
        and not locally_blocked
        and risex.get('authorization_status') == 'AUTHORIZED'
    )
    if reported_state == RISExExecutionState.ARMING.value:
        auth_status = 'PENDING'
    else:
        auth_status = 'AUTHORIZED' if authorized else 'NOT_AUTHORIZED'
    base.update(
        observability='FRESH',
        authorization_status=auth_status,
        remaining_seconds=remaining,
    )
    return base
