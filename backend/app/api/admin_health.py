from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.core.config import settings
from app.db.session import get_db
from app.models.entities import Role, SystemFlag, User
from app.services.ai_intelligence import read_ai_intelligence

router = APIRouter(prefix='/admin', tags=['admin'])
admin = require_role(Role.ADMIN, Role.SUPERADMIN)


def _current_checkpoint_slug() -> str | None:
    address = (settings.HYPERLIQUID_MASTER_ADDRESS or '').strip().lower()
    if not address:
        return None
    return f'master_checkpoint:{settings.master_network}:{address}'


@router.get('/health')
async def health(user: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    checkpoint_slug = _current_checkpoint_slug()
    checkpoint_row = await db.get(SystemFlag, checkpoint_slug) if checkpoint_slug else None
    checkpoint_value = (checkpoint_row.value or {}) if checkpoint_row else {}
    ai = await read_ai_intelligence(db)

    return {
        'master_checkpoint': {
            'configured': bool(checkpoint_slug),
            'present': checkpoint_row is not None,
            'enabled': bool(checkpoint_row and checkpoint_row.enabled),
            'time_ms': int(checkpoint_value.get('time_ms', 0) or 0),
            'updated_at': checkpoint_row.updated_at if checkpoint_row else None,
            'network': settings.master_network,
        },
        'ai_intelligence': {
            'status': str(ai.get('status') or 'pending'),
            'mode': ai.get('mode'),
            'provider': ai.get('provider'),
            'model': ai.get('model'),
            'updated_at': ai.get('updated_at'),
        },
    }
