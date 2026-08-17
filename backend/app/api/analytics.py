from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import User
from app.services.metrics import PNL_RANGE_CONFIG, pnl_history_for_user

router = APIRouter(tags=['analytics'])


@router.get('/pnl-history')
async def pnl_history(
    range: str = Query('1d', pattern='^(1d|7d|30d|90d|all)$'),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    if range not in PNL_RANGE_CONFIG:
        raise HTTPException(422, 'Unsupported PnL range')
    return await pnl_history_for_user(db, user.id, range)
