from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import normalize_address
from app.models.entities import User

MASTER_SOURCE_MODE = 'MASTER_SOURCE_READ_ONLY'
MASTER_SOURCE_NETWORK = 'mainnet'
MASTER_SOURCE_FOLLOWER_BLOCK_REASON = (
    'Configured master source is MAINNET read-only and cannot use follower controls'
)


def _normalized_address(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return normalize_address(value)
    except Exception:
        return None


def configured_master_source_address() -> str | None:
    return _normalized_address(settings.HYPERLIQUID_MASTER_ADDRESS)


def is_master_source_wallet(wallet: str | None) -> bool:
    master = configured_master_source_address()
    candidate = _normalized_address(wallet)
    return bool(master and candidate and candidate == master)


def is_master_source_user(user: User) -> bool:
    return is_master_source_wallet(getattr(user, 'auth_wallet', None))


def follower_controls_enabled(user: User) -> bool:
    return not is_master_source_user(user)


async def quarantine_master_source_jobs(db: AsyncSession) -> int:
    """Terminally quarantine legacy follower jobs before a worker consumes them.

    Normal producers are separately blocked from creating/publishing new master
    follower work. This startup fence handles rows persisted before the isolation
    rollout, including signed admin jobs. PROCESSING is marked DEAD rather than
    SKIPPED because a previous worker may have crossed an external-action boundary.
    """

    master = configured_master_source_address()
    if not master:
        return 0
    rows = (
        await db.execute(
            text(
                """
                UPDATE copy_jobs AS cj
                SET state = CASE WHEN cj.state = 'PROCESSING' THEN 'DEAD' ELSE 'SKIPPED' END,
                    last_error = CASE
                        WHEN cj.state = 'PROCESSING'
                            THEN 'Master Source legacy follower job quarantined after worker loss; verify exchange state before recovery'
                        ELSE 'Master Source is MAINNET read-only; follower job quarantined'
                    END,
                    owner = NULL,
                    locked_until = NULL,
                    next_attempt_at = NULL,
                    enqueued_at = NULL,
                    updated_at = now()
                FROM users AS u
                WHERE cj.user_id = u.id
                  AND lower(trim(u.auth_wallet)) = lower(trim(:master_address))
                  AND cj.state IN ('QUEUED', 'RETRYING', 'PROCESSING')
                RETURNING cj.id
                """
            ),
            {'master_address': master},
        )
    ).all()
    if rows:
        await db.commit()
    return len(rows)
