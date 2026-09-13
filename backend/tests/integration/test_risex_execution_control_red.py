from __future__ import annotations

import asyncio
import importlib
import os
import uuid

import pytest
from sqlalchemy import select, text

from app.db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


async def _cleanup_controls() -> None:
    async with SessionLocal() as db:
        await db.execute(text('DELETE FROM risex_execution_control'))
        await db.commit()


@pytest.mark.asyncio
async def test_control_request_is_claimed_atomically_by_only_one_worker_process() -> None:
    control_module = importlib.import_module('app.services.risex_execution_control')
    entities = importlib.import_module('app.models.entities')
    model = getattr(entities, 'RISExExecutionControl')

    worker_id = 'execution-worker-atomic-claim'
    boot_id = uuid.uuid4()
    request_id = uuid.uuid4()

    try:
        async with SessionLocal() as db:
            db.add(
                model(
                    request_id=request_id,
                    control_generation=1001,
                    action='ARM',
                    target_worker_id=worker_id,
                    target_boot_id=boot_id,
                    state='REQUESTED',
                    reason='RED atomic claim test',
                )
            )
            await db.commit()

        async def claim_once():
            async with SessionLocal() as db:
                return await control_module.claim_control_request(
                    db,
                    worker_id=worker_id,
                    boot_id=boot_id,
                )

        first, second = await asyncio.gather(claim_once(), claim_once())
        claimed = [row for row in (first, second) if row is not None]
        assert len(claimed) == 1
        assert claimed[0].request_id == request_id

        async with SessionLocal() as db:
            persisted = (
                await db.execute(select(model).where(model.request_id == request_id))
            ).scalar_one()
            assert persisted.state == 'CONSUMED'
            assert persisted.consumed_by_worker_id == worker_id
            assert persisted.consumed_by_boot_id == boot_id
            assert persisted.consumed_at is not None
    finally:
        try:
            await _cleanup_controls()
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_later_disarm_generation_blocks_arm_finalization() -> None:
    control_module = importlib.import_module('app.services.risex_execution_control')
    entities = importlib.import_module('app.models.entities')
    model = getattr(entities, 'RISExExecutionControl')

    worker_id = 'execution-worker-finalization-fence'
    boot_id = uuid.uuid4()
    arm_id = uuid.uuid4()
    disarm_id = uuid.uuid4()

    try:
        async with SessionLocal() as db:
            db.add_all(
                [
                    model(
                        request_id=arm_id,
                        control_generation=2001,
                        action='ARM',
                        target_worker_id=worker_id,
                        target_boot_id=boot_id,
                        state='CONSUMED',
                        reason='RED arm generation',
                        consumed_by_worker_id=worker_id,
                        consumed_by_boot_id=boot_id,
                    ),
                    model(
                        request_id=disarm_id,
                        control_generation=2002,
                        action='DISARM',
                        target_worker_id=worker_id,
                        target_boot_id=boot_id,
                        state='REQUESTED',
                        reason='RED later disarm generation',
                        supersedes_request_id=arm_id,
                    ),
                ]
            )
            await db.commit()

        async with SessionLocal() as db:
            allowed = await control_module.arm_finalization_fence(
                db,
                arm_request_id=arm_id,
                arm_control_generation=2001,
                worker_id=worker_id,
                boot_id=boot_id,
            )
            assert allowed is False
    finally:
        try:
            await _cleanup_controls()
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_arm_finalization_fence_allows_only_the_latest_consumed_arm() -> None:
    control_module = importlib.import_module('app.services.risex_execution_control')
    entities = importlib.import_module('app.models.entities')
    model = getattr(entities, 'RISExExecutionControl')

    worker_id = 'execution-worker-finalization-positive'
    boot_id = uuid.uuid4()
    arm_id = uuid.uuid4()

    try:
        async with SessionLocal() as db:
            db.add(
                model(
                    request_id=arm_id,
                    control_generation=3001,
                    action='ARM',
                    target_worker_id=worker_id,
                    target_boot_id=boot_id,
                    state='CONSUMED',
                    reason='RED latest arm generation',
                    consumed_by_worker_id=worker_id,
                    consumed_by_boot_id=boot_id,
                )
            )
            await db.commit()

        async with SessionLocal() as db:
            allowed = await control_module.arm_finalization_fence(
                db,
                arm_request_id=arm_id,
                arm_control_generation=3001,
                worker_id=worker_id,
                boot_id=boot_id,
            )
            assert allowed is True
    finally:
        try:
            await _cleanup_controls()
        finally:
            await engine.dispose()
