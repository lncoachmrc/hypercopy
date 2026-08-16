from __future__ import annotations

from fastapi import APIRouter, HTTPException
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import settings
from app.db.redis import redis_client
from app.db.session import SessionLocal
from app.db.schema import EXPECTED_REVISION

router = APIRouter(tags=['health'])


@router.get('/health/live')
async def live():
    return {'status':'live','version':settings.APP_VERSION}


@router.get('/health/ready')
async def ready():
    details={}
    try:
        async with SessionLocal() as db:
            await db.execute(text('SELECT 1'))
            revision=(await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))).scalar_one_or_none()
            details['postgres']='ok'; details['alembic_revision']=revision
            if revision != EXPECTED_REVISION:
                raise RuntimeError(f'schema {revision} != {EXPECTED_REVISION}')
    except Exception as exc:
        raise HTTPException(503,detail={'status':'not_ready','dependency':'postgres'}) from exc
    try:
        await redis_client().ping(); details['redis']='ok'
    except Exception as exc:
        raise HTTPException(503,detail={'status':'not_ready','dependency':'redis'}) from exc
    return {'status':'ready','version':settings.APP_VERSION,'dependencies':details}
