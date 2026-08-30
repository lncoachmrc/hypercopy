from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Network, settings
from app.models.entities import CopyState, CredentialStatus


class LeverageSyncAuthorizationError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _blocked(status_code: int, message: str) -> LeverageSyncAuthorizationError:
    return LeverageSyncAuthorizationError(status_code, message)


async def fresh_position_config_sync_authorization(
    db: AsyncSession,
    user_id: uuid.UUID,
    expected_network: Network,
    *,
    expected_account_id: uuid.UUID,
    expected_account_address: str,
    expected_credential_id: uuid.UUID,
    asset: str,
    master_leverage: int,
    exchange_max_leverage: int,
    desired_is_cross: bool,
) -> tuple[int, bool, str]:
    """Take one final DB snapshot immediately before a signed leverage submit.

    The execution-worker calls this only after refreshing strategy/exchange state
    and any degraded address cadence slot. Keeping every mutable follower/control
    input in this single query preserves the no-await authorization boundary that
    existed in the direct API signing path while allowing signing custody to live
    exclusively in the execution-worker.
    """
    row = (await db.execute(
        text(
            """
            SELECT
                u.execution_network,
                u.copy_state,
                r.max_leverage AS risk_max_leverage,
                r.allow_assets,
                r.block_assets,
                ta.id AS account_id,
                ta.account_address,
                sc.id AS credential_id,
                sc.status AS credential_status,
                sc.expires_at AS credential_expires_at,
                COALESCE((SELECT enabled FROM system_flags WHERE slug = 'live_trading'), FALSE) AS live_trading,
                COALESCE((SELECT enabled FROM system_flags WHERE slug = 'global_pause'), FALSE) AS global_pause,
                COALESCE((SELECT enabled FROM system_flags WHERE slug = 'emergency_stop'), FALSE) AS emergency_stop
            FROM users u
            LEFT JOIN risk_profiles r ON r.user_id = u.id
            LEFT JOIN trading_accounts ta ON ta.user_id = u.id
            LEFT JOIN signing_credentials sc ON sc.trading_account_id = ta.id
            WHERE u.id = :user_id
            """
        ),
        {'user_id': user_id},
    )).mappings().one_or_none()
    if not row:
        raise _blocked(404, 'User not found during leverage synchronization')

    current_network = str(row['execution_network'] or settings.follower_network).lower()
    if current_network != expected_network:
        raise _blocked(409, 'Follower network changed during leverage synchronization')
    if str(row['copy_state'] or '') != CopyState.PAUSED.value:
        raise _blocked(409, 'Pause the follower before direct leverage synchronization')

    if row['risk_max_leverage'] is None:
        raise _blocked(409, 'Follower risk profile is missing')
    risk_max_leverage = Decimal(str(row['risk_max_leverage']))
    allow_assets = list(row['allow_assets'] or [])
    block_assets = list(row['block_assets'] or [])
    allowed_asset = (not allow_assets or asset in allow_assets) and asset not in block_assets
    if not allowed_asset:
        raise _blocked(409, f'{asset} is not permitted by the follower Risk Engine')
    desired_leverage = max(
        1,
        min(
            int(master_leverage),
            int(risk_max_leverage),
            int(exchange_max_leverage),
        ),
    )

    account_id = row['account_id']
    account_address = str(row['account_address'] or '')
    if (
        account_id != expected_account_id
        or account_address.lower() != expected_account_address.lower()
    ):
        raise _blocked(409, 'Follower trading account changed during leverage synchronization')

    credential_status = str(row['credential_status'] or '')
    credential_expires_at = row['credential_expires_at']
    if (
        row['credential_id'] != expected_credential_id
        or credential_status not in {CredentialStatus.ACTIVE.value, CredentialStatus.EXPIRING.value}
        or (credential_expires_at is not None and credential_expires_at <= datetime.now(UTC))
    ):
        raise _blocked(409, 'Trading credential changed or became unavailable')

    if expected_network == 'mainnet' and (
        not settings.ENABLE_LIVE_TRADING or not bool(row['live_trading'])
    ):
        raise _blocked(409, 'Mainnet live-trading gate is closed')
    active_halts = sorted(
        slug
        for slug in ('global_pause', 'emergency_stop')
        if bool(row[slug])
    )
    if active_halts:
        raise _blocked(409, f"Leverage synchronization blocked by system halt: {', '.join(active_halts)}")

    return desired_leverage, desired_is_cross, str(risk_max_leverage)
