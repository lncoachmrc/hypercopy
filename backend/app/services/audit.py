from __future__ import annotations

import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import redact
from app.models.entities import AuditLog


async def audit(
    db: AsyncSession, *, action: str, actor_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None, reason: str | None = None,
    ip_hash: str | None = None, correlation_id: str | None = None,
    before: dict[str, Any] | None = None, after: dict[str, Any] | None = None,
) -> AuditLog:
    # Audit payloads are stored in SQLAlchemy JSON columns. Redaction protects
    # secrets, while jsonable_encoder guarantees values such as datetime,
    # Decimal, UUID and enums are converted to JSON-safe primitives before the
    # transaction is committed. Without this, an otherwise successful external
    # action can end in a 500 while persisting its audit record.
    safe_before = jsonable_encoder(redact(before or {}))
    safe_after = jsonable_encoder(redact(after or {}))
    row = AuditLog(
        action=action, actor_id=actor_id, subject_id=subject_id, reason=reason,
        ip_hash=ip_hash, correlation_id=correlation_id,
        before=safe_before, after=safe_after,
    )
    db.add(row)
    return row
