from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.entities import RISExExecutionControl, User, WorkerHeartbeat
from app.schemas.admin import RISExExecutionControlAction
from app.services.audit import audit
from app.services.risex_execution_control import RISExControlConflict, create_control_request
from app.services.risex_execution_window import (
    RISExExecutionState,
    build_risex_execution_status,
    fresh_execution_worker_heartbeats,
)


_ARM_CONFIRMATION = 'ARM RISEX'
_DISARM_CONFIRMATION = 'DISARM RISEX'


def _boot_id(heartbeat: WorkerHeartbeat) -> str | None:
    meta = heartbeat.meta or {}
    value = meta.get('boot_id')
    return str(value) if value else None


def install_risex_admin(
    router: APIRouter,
    *,
    admin: Any,
    superadmin: Any,
    require_csrf: Any,
) -> None:
    """Register ADR-0004 endpoints on the existing admin router/auth boundary."""

    @router.post('/risex-execution-control', dependencies=[Depends(require_csrf)])
    async def risex_execution_control(
        body: RISExExecutionControlAction,
        actor: User = Depends(superadmin),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        expected_confirmation = _ARM_CONFIRMATION if body.action == 'ARM' else _DISARM_CONFIRMATION
        if body.confirmation != expected_confirmation:
            raise HTTPException(422, f'Confirmation must be {expected_confirmation}')

        heartbeats = (
            await db.execute(
                select(WorkerHeartbeat)
                .where(WorkerHeartbeat.service == 'execution-worker')
                .order_by(WorkerHeartbeat.seen_at.desc())
            )
        ).scalars().all()
        now = datetime.now(UTC)
        live = fresh_execution_worker_heartbeats(heartbeats, now=now)
        target_live = [
            heartbeat
            for heartbeat in live
            if heartbeat.worker_id == body.target_worker_id
            and _boot_id(heartbeat) == str(body.target_boot_id)
        ]

        if body.action == 'ARM':
            if len(live) != 1 or len(target_live) != 1:
                raise HTTPException(
                    409,
                    'RISEx ARM requires exactly one live execution-worker matching the requested worker_id and boot_id',
                )
            risex_meta = (target_live[0].meta or {}).get('risex') or {}
            reported_state = str(risex_meta.get('reported_state') or 'UNKNOWN')
            if reported_state not in {
                RISExExecutionState.DISABLED.value,
                RISExExecutionState.LOCKED.value,
            }:
                raise HTTPException(409, f'RISEx ARM is not allowed while worker reports {reported_state}')
            readiness_assertions = {
                'disposable_account_asserted': body.disposable_account_asserted,
                'dedicated_signer_asserted': body.dedicated_signer_asserted,
                'operatorhub_bypass_disabled': body.operatorhub_bypass_disabled,
                'fund_movement_path_absent': body.fund_movement_path_absent,
            }
            if not all(readiness_assertions.values()):
                raise HTTPException(422, 'RISEx ARM requires all explicit ADR-0002 readiness assertions')
        else:
            readiness_assertions = {}
            live_same_worker = [heartbeat for heartbeat in live if heartbeat.worker_id == body.target_worker_id]
            if live_same_worker and not target_live:
                raise HTTPException(409, 'RISEx DISARM target boot_id no longer matches the live worker incarnation')

        try:
            request = await create_control_request(
                db,
                action=body.action,
                target_worker_id=body.target_worker_id,
                target_boot_id=body.target_boot_id,
                requested_by=actor.id,
                reason=body.reason,
                readiness_assertions=readiness_assertions,
            )
        except RISExControlConflict as exc:
            await db.rollback()
            raise HTTPException(409, str(exc)) from exc

        await audit(
            db,
            action=f'ADMIN_RISEX_EXECUTION_{body.action}_REQUESTED',
            actor_id=actor.id,
            reason=body.reason,
            correlation_id=str(request.request_id),
            after={
                'request_id': str(request.request_id),
                'control_generation': request.control_generation,
                'action': request.action,
                'target_worker_id': request.target_worker_id,
                'target_boot_id': str(request.target_boot_id),
                'state': request.state,
                'supersedes_request_id': str(request.supersedes_request_id) if request.supersedes_request_id else None,
            },
        )
        await db.commit()
        return {
            'accepted': True,
            'request_id': str(request.request_id),
            'control_generation': request.control_generation,
            'action': request.action,
            'target_worker_id': request.target_worker_id,
            'target_boot_id': str(request.target_boot_id),
            'state': request.state,
        }

    @router.get('/risex-execution-status')
    async def risex_execution_status(
        actor: User = Depends(admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict[str, Any]:
        del actor
        heartbeats = (
            await db.execute(
                select(WorkerHeartbeat)
                .where(WorkerHeartbeat.service == 'execution-worker')
                .order_by(WorkerHeartbeat.seen_at.desc())
            )
        ).scalars().all()
        control_request = (
            await db.execute(
                select(RISExExecutionControl)
                .order_by(
                    RISExExecutionControl.requested_at.desc(),
                    RISExExecutionControl.control_generation.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return build_risex_execution_status(
            heartbeats=heartbeats,
            control_request=control_request,
            now=datetime.now(UTC),
        )
