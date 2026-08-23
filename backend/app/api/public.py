from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.public_performance import (
    PUBLIC_PERFORMANCE_RANGE_CONFIG,
    public_master_performance,
)

router = APIRouter(tags=['public'])


@router.get('/public/master-performance')
async def master_performance(
    response: Response,
    range: str = Query('all', pattern='^(90d|180d|1y|all)$'),
    db: AsyncSession = Depends(get_db),
):
    if range not in PUBLIC_PERFORMANCE_RANGE_CONFIG:
        raise HTTPException(422, 'Unsupported performance range')
    try:
        payload = await public_master_performance(db, range)
    except RuntimeError as exc:
        raise HTTPException(503, 'Performance data temporarily unavailable') from exc

    response.headers['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=120'
    return payload
