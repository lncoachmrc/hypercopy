from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import CredentialStatus, RiskHalt, RiskState, SigningCredential, TradingAccount


async def monitor_credential_expiry(db: AsyncSession, redis: Redis | None = None) -> dict[str, int]:
    """Update credential lifecycle states and emit user alerts.

    30d: informational alert, 7d: persistent EXPIRING state, 0d: EXPIRED and a
    distinct risk halt. This never mutates the user's own pause/kill state.
    """
    now = datetime.now(UTC)
    rows = (await db.execute(
        select(SigningCredential, TradingAccount.user_id)
        .join(TradingAccount, TradingAccount.id == SigningCredential.trading_account_id)
        .where(SigningCredential.expires_at.is_not(None))
    )).all()
    stats = {'expiring_30d': 0, 'expiring_7d': 0, 'expired': 0}
    alerts: list[tuple[str, dict]] = []
    for cred, user_id in rows:
        remaining = cred.expires_at - now
        if remaining <= timedelta(0):
            stats['expired'] += 1
            if cred.status != CredentialStatus.EXPIRED:
                cred.status = CredentialStatus.EXPIRED
            risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user_id))).scalar_one_or_none()
            if not risk_state:
                risk_state = RiskState(user_id=user_id)
                db.add(risk_state)
            risk_state.state = RiskHalt.CREDENTIAL_EXPIRED
            risk_state.reason = 'Hyperliquid API wallet expired; renew the named agent wallet'
            alerts.append((str(user_id), {'type': 'credential_expired', 'expires_at': cred.expires_at.isoformat()}))
        elif remaining <= timedelta(days=7):
            stats['expiring_7d'] += 1
            if cred.status == CredentialStatus.ACTIVE:
                cred.status = CredentialStatus.EXPIRING
            alerts.append((str(user_id), {'type': 'credential_expiring', 'days': 7, 'expires_at': cred.expires_at.isoformat()}))
        elif remaining <= timedelta(days=30):
            stats['expiring_30d'] += 1
            alerts.append((str(user_id), {'type': 'credential_expiring', 'days': 30, 'expires_at': cred.expires_at.isoformat()}))
        elif cred.status == CredentialStatus.EXPIRING:
            cred.status = CredentialStatus.ACTIVE
    await db.commit()
    if redis:
        for user_id, payload in alerts:
            try:
                await redis.publish(f'{settings.REALTIME_CHANNEL_PREFIX}:user:{user_id}', json.dumps(payload))
            except Exception:
                # Alert delivery is transient; state is already durable in PG.
                pass
    return stats
