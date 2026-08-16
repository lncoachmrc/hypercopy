from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import redact
from app.models.entities import AuditLog


async def audit(
    db: AsyncSession, *, action: str, actor_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None, reason: str | None = None,
    ip_hash: str | None = None, correlation_id: str | None = None,
    before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
) -> AuditLog:
    row = AuditLog(
        action=action, actor_id=actor_id, subject_id=subject_id, reason=reason,
        ip_hash=ip_hash, correlation_id=correlation_id,
        before=redact(before or {}), after=redact(after or {}),
    )
    db.add(row)
    return row
