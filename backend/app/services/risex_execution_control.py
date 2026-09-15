from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import RISExExecutionControl


RISExControlAction = Literal['ARM', 'DISARM']
RISExControlState = Literal['REQUESTED', 'CONSUMED']


class RISExControlConflict(RuntimeError):
    """The requested control transition conflicts with pending control state."""


def _control_lock_name(worker_id: str, boot_id: uuid.UUID) -> str:
    return f'hypercopy:risex-execution-control:{worker_id}:{boot_id}'


async def _acquire_control_fence_lock(
    db: AsyncSession,
    *,
    worker_id: str,
    boot_id: uuid.UUID,
) -> None:
    """Serialize command creation and ARM finalization for one process incarnation."""

    await db.execute(
        text('SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))'),
        {'lock_key': _control_lock_name(worker_id, boot_id)},
    )


async def create_control_request(
    db: AsyncSession,
    *,
    action: RISExControlAction,
    target_worker_id: str,
    target_boot_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    reason: str,
    readiness_assertions: dict[str, bool] | None = None,
    supersedes_request_id: uuid.UUID | None = None,
) -> RISExExecutionControl:
    """Create a one-shot operator request under the target's transaction fence."""

    if action not in {'ARM', 'DISARM'}:
        raise ValueError('RISEx control action must be ARM or DISARM')
    if not target_worker_id:
        raise ValueError('RISEx control target worker is required')

    await _acquire_control_fence_lock(
        db,
        worker_id=target_worker_id,
        boot_id=target_boot_id,
    )

    target_filter = (
        RISExExecutionControl.target_worker_id == target_worker_id,
        RISExExecutionControl.target_boot_id == target_boot_id,
    )
    latest = (
        await db.execute(
            select(RISExExecutionControl)
            .where(*target_filter)
            .order_by(RISExExecutionControl.control_generation.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if action == 'ARM':
        pending = (
            await db.execute(
                select(RISExExecutionControl.request_id)
                .where(
                    *target_filter,
                    RISExExecutionControl.state == 'REQUESTED',
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if pending is not None:
            raise RISExControlConflict('A RISEx control request is already pending for this worker boot')

    generation = 1 if latest is None else int(latest.control_generation) + 1
    if action == 'DISARM' and supersedes_request_id is None:
        latest_arm = (
            await db.execute(
                select(RISExExecutionControl.request_id)
                .where(*target_filter, RISExExecutionControl.action == 'ARM')
                .order_by(RISExExecutionControl.control_generation.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        supersedes_request_id = latest_arm

    request = RISExExecutionControl(
        request_id=uuid.uuid4(),
        control_generation=generation,
        action=action,
        target_worker_id=target_worker_id,
        target_boot_id=target_boot_id,
        state='REQUESTED',
        requested_at=datetime.now(UTC),
        requested_by=requested_by,
        reason=reason,
        supersedes_request_id=supersedes_request_id,
        readiness_assertions=dict(readiness_assertions or {}),
    )
    db.add(request)
    await db.flush()
    return request


async def claim_control_request(
    db: AsyncSession,
    *,
    worker_id: str,
    boot_id: uuid.UUID,
) -> RISExExecutionControl | None:
    """Atomically claim one REQUESTED command for this exact worker incarnation."""

    request = (
        await db.execute(
            select(RISExExecutionControl)
            .where(
                RISExExecutionControl.target_worker_id == worker_id,
                RISExExecutionControl.target_boot_id == boot_id,
                RISExExecutionControl.state == 'REQUESTED',
            )
            .order_by(RISExExecutionControl.control_generation.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if request is None:
        return None

    request.state = 'CONSUMED'
    request.consumed_at = datetime.now(UTC)
    request.consumed_by_worker_id = worker_id
    request.consumed_by_boot_id = boot_id
    await db.commit()
    return request


async def arm_finalization_fence(
    db: AsyncSession,
    *,
    arm_request_id: uuid.UUID,
    arm_control_generation: int,
    worker_id: str,
    boot_id: uuid.UUID,
) -> bool:
    """Hold the shared transaction fence and reject any superseded ARM attempt."""

    await _acquire_control_fence_lock(db, worker_id=worker_id, boot_id=boot_id)

    arm = (
        await db.execute(
            select(RISExExecutionControl)
            .where(RISExExecutionControl.request_id == arm_request_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if arm is None:
        return False

    latest_generation = (
        await db.execute(
            select(func.max(RISExExecutionControl.control_generation)).where(
                RISExExecutionControl.target_worker_id == worker_id,
                RISExExecutionControl.target_boot_id == boot_id,
            )
        )
    ).scalar_one_or_none()

    return bool(
        arm.action == 'ARM'
        and arm.state == 'CONSUMED'
        and arm.request_id == arm_request_id
        and int(arm.control_generation) == int(arm_control_generation)
        and int(latest_generation or 0) == int(arm_control_generation)
        and arm.target_worker_id == worker_id
        and arm.target_boot_id == boot_id
        and arm.consumed_by_worker_id == worker_id
        and arm.consumed_by_boot_id == boot_id
    )


async def latest_control_request(
    db: AsyncSession,
    *,
    worker_id: str | None = None,
    boot_id: uuid.UUID | None = None,
) -> RISExExecutionControl | None:
    query = select(RISExExecutionControl)
    if worker_id is not None:
        query = query.where(RISExExecutionControl.target_worker_id == worker_id)
    if boot_id is not None:
        query = query.where(RISExExecutionControl.target_boot_id == boot_id)
    return (
        await db.execute(
            query.order_by(
                RISExExecutionControl.requested_at.desc(),
                RISExExecutionControl.control_generation.desc(),
            ).limit(1)
        )
    ).scalar_one_or_none()
