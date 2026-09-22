from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import SystemFlag
from app.services.ai_profit_exit import ProfitExitFeatureMode, profit_exit_feature_mode

AI_PROFIT_EXIT_MODE_FLAG_SLUG = "ai:profit_exit_mode"


def _mode_from_value(value: object) -> ProfitExitFeatureMode:
    if not isinstance(value, dict):
        return ProfitExitFeatureMode.OFF
    raw = str(value.get("mode") or "").upper()
    try:
        return ProfitExitFeatureMode(raw)
    except ValueError:
        return ProfitExitFeatureMode.OFF


async def read_profit_exit_mode(db: AsyncSession) -> ProfitExitFeatureMode:
    """Read the shared runtime mode, failing closed on malformed persisted state.

    Before the SUPERADMIN control has ever been used there is no database row;
    preserve the existing environment-configured fallback for backward
    compatibility. Once a row exists, its value is authoritative for every
    service that shares PostgreSQL.
    """
    row = await db.get(SystemFlag, AI_PROFIT_EXIT_MODE_FLAG_SLUG)
    if row is None:
        return profit_exit_feature_mode()
    return _mode_from_value(row.value)


async def read_profit_exit_mode_state(db: AsyncSession) -> dict:
    row = await db.get(SystemFlag, AI_PROFIT_EXIT_MODE_FLAG_SLUG)
    if row is None:
        mode = profit_exit_feature_mode()
        return {
            "mode": mode.value,
            "source": "environment_default",
            "evaluates": mode is not ProfitExitFeatureMode.OFF,
            "operational": mode is ProfitExitFeatureMode.ON,
            "reason": None,
            "updated_at": None,
        }

    mode = _mode_from_value(row.value)
    return {
        "mode": mode.value,
        "source": "database",
        "evaluates": mode is not ProfitExitFeatureMode.OFF,
        "operational": mode is ProfitExitFeatureMode.ON,
        "reason": row.reason,
        "updated_at": (row.value or {}).get("updated_at") if isinstance(row.value, dict) else None,
    }


async def set_profit_exit_mode(
    db: AsyncSession,
    *,
    mode: ProfitExitFeatureMode,
    reason: str,
    actor_id: uuid.UUID,
) -> dict:
    now = datetime.now(UTC)
    row = await db.get(SystemFlag, AI_PROFIT_EXIT_MODE_FLAG_SLUG)
    value = {
        "mode": mode.value,
        "updated_at": now.isoformat(),
    }
    if row is None:
        row = SystemFlag(
            slug=AI_PROFIT_EXIT_MODE_FLAG_SLUG,
            enabled=mode is ProfitExitFeatureMode.ON,
            value=value,
            reason=reason,
            updated_by=actor_id,
        )
        db.add(row)
    else:
        row.enabled = mode is ProfitExitFeatureMode.ON
        row.value = value
        row.reason = reason
        row.updated_by = actor_id

    await db.flush()
    return await read_profit_exit_mode_state(db)
