from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ExecutionProvider
from app.core.config import Network
from app.models.entities import CopyJob

_ALLOWED_PROVIDERS = {'hyperliquid', 'risex'}
_ALLOWED_NETWORKS = {'testnet', 'mainnet'}


@dataclass(frozen=True, slots=True)
class UserDestinationState:
    provider: ExecutionProvider
    network: Network
    epoch_id: uuid.UUID
    started_at: datetime


def _provider(value: object) -> ExecutionProvider:
    raw = str(value or '').lower()
    if raw not in _ALLOWED_PROVIDERS:
        raise RuntimeError(f'Unsupported user execution provider: {raw}')
    return raw  # type: ignore[return-value]


def _network(value: object) -> Network:
    raw = str(value or '').lower()
    if raw not in _ALLOWED_NETWORKS:
        raise RuntimeError(f'Unsupported user execution network: {raw}')
    return raw  # type: ignore[return-value]


async def user_destination_state(db: AsyncSession, user_id: uuid.UUID) -> UserDestinationState:
    row = (
        await db.execute(
            text(
                """
                SELECT
                    u.execution_provider AS user_provider,
                    u.execution_network AS user_network,
                    u.active_execution_epoch_id,
                    e.provider AS epoch_provider,
                    e.network AS epoch_network,
                    e.started_at,
                    e.ended_at
                FROM users u
                LEFT JOIN execution_epochs e
                  ON e.id = u.active_execution_epoch_id
                WHERE u.id = :user_id
                """
            ),
            {'user_id': user_id},
        )
    ).mappings().one_or_none()
    if not row:
        raise RuntimeError('User destination state is unavailable')
    if row['active_execution_epoch_id'] is None or row['epoch_provider'] is None:
        raise RuntimeError('User has no active execution destination epoch')
    if row['ended_at'] is not None:
        raise RuntimeError('User active execution destination epoch is closed')

    provider = _provider(row['epoch_provider'])
    network = _network(row['epoch_network'])
    if _provider(row['user_provider']) != provider:
        raise RuntimeError('User execution provider diverges from active destination epoch')
    if _network(row['user_network']) != network:
        raise RuntimeError('User execution network diverges from active destination epoch')

    return UserDestinationState(
        provider=provider,
        network=network,
        epoch_id=row['active_execution_epoch_id'],
        started_at=row['started_at'],
    )


async def job_matches_active_destination(db: AsyncSession, job: CopyJob) -> bool:
    """Fail closed unless a job is bound to the user's exact active destination epoch."""
    if (
        job.execution_epoch_id is None
        or job.execution_provider is None
        or job.execution_network is None
    ):
        return False
    try:
        destination = await user_destination_state(db, job.user_id)
        provider = _provider(job.execution_provider)
        network = _network(job.execution_network)
    except RuntimeError:
        return False
    return (
        job.execution_epoch_id == destination.epoch_id
        and provider == destination.provider
        and network == destination.network
    )


async def bootstrap_user_destination_epoch(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> UserDestinationState:
    """Materialize the epoch for a legacy/incomplete user without changing its destination.

    This compatibility path is deliberately narrow: it is valid only when the user has
    no active epoch at all. Existing epoch corruption, closure, or provider/network
    divergence continues to fail closed through ``user_destination_state``.
    """
    row = (
        await db.execute(
            text(
                """
                SELECT
                    u.active_execution_epoch_id,
                    u.execution_provider,
                    u.execution_network,
                    u.network_started_at,
                    ta.account_address,
                    sc.key_version AS credential_version
                FROM users u
                LEFT JOIN trading_accounts ta
                  ON ta.user_id = u.id
                LEFT JOIN signing_credentials sc
                  ON sc.trading_account_id = ta.id
                WHERE u.id = :user_id
                FOR UPDATE OF u
                """
            ),
            {'user_id': user_id},
        )
    ).mappings().one_or_none()
    if not row:
        raise RuntimeError('User destination state is unavailable')

    # A concurrent caller may have created the epoch while this transaction waited
    # on the user-row lock. In that case, validate and return the canonical state.
    if row['active_execution_epoch_id'] is not None:
        return await user_destination_state(db, user_id)

    provider = _provider(row['execution_provider'])
    network = _network(row['execution_network'])
    started_at = row['network_started_at'] or datetime.now(UTC)
    epoch_id = uuid.uuid4()

    await db.execute(
        text(
            """
            INSERT INTO execution_epochs (
                id, user_id, provider, network, account_address,
                credential_version, started_at, ended_at
            ) VALUES (
                :epoch_id, :user_id, :provider, :network, :account_address,
                :credential_version, :started_at, NULL
            )
            """
        ),
        {
            'epoch_id': epoch_id,
            'user_id': user_id,
            'provider': provider,
            'network': network,
            'account_address': row['account_address'],
            'credential_version': row['credential_version'],
            'started_at': started_at,
        },
    )
    await db.execute(
        text(
            """
            UPDATE users
            SET active_execution_epoch_id = :epoch_id
            WHERE id = :user_id
              AND active_execution_epoch_id IS NULL
            """
        ),
        {'epoch_id': epoch_id, 'user_id': user_id},
    )

    return UserDestinationState(
        provider=provider,
        network=network,
        epoch_id=epoch_id,
        started_at=started_at,
    )


async def set_user_destination(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    provider: ExecutionProvider,
    network: Network,
    account_address: str | None = None,
    credential_version: int | None = None,
) -> UserDestinationState:
    provider = _provider(provider)
    network = _network(network)
    now = datetime.now(UTC)

    row = (
        await db.execute(
            text(
                """
                SELECT
                    u.active_execution_epoch_id,
                    e.provider,
                    e.network,
                    e.account_address,
                    e.credential_version,
                    e.started_at,
                    e.ended_at
                FROM users u
                LEFT JOIN execution_epochs e
                  ON e.id = u.active_execution_epoch_id
                WHERE u.id = :user_id
                FOR UPDATE OF u
                """
            ),
            {'user_id': user_id},
        )
    ).mappings().one_or_none()
    if not row:
        raise RuntimeError('User destination state is unavailable')

    current_epoch_id = row['active_execution_epoch_id']
    current_provider = str(row['provider'] or '').lower()
    current_network = str(row['network'] or '').lower()
    current_open = current_epoch_id is not None and row['ended_at'] is None

    preserved_account = account_address
    preserved_credential_version = credential_version
    if current_open and current_provider == provider:
        if preserved_account is None:
            preserved_account = row['account_address']
        if preserved_credential_version is None:
            preserved_credential_version = row['credential_version']

    unchanged = (
        current_open
        and current_provider == provider
        and current_network == network
        and (account_address is None or account_address == row['account_address'])
        and (credential_version is None or credential_version == row['credential_version'])
    )
    if unchanged:
        return UserDestinationState(
            provider=provider,
            network=network,
            epoch_id=current_epoch_id,
            started_at=row['started_at'],
        )

    if current_open:
        await db.execute(
            text('UPDATE execution_epochs SET ended_at = :ended_at WHERE id = :epoch_id AND ended_at IS NULL'),
            {'ended_at': now, 'epoch_id': current_epoch_id},
        )

    epoch_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO execution_epochs (
                id, user_id, provider, network, account_address,
                credential_version, started_at, ended_at
            ) VALUES (
                :epoch_id, :user_id, :provider, :network, :account_address,
                :credential_version, :started_at, NULL
            )
            """
        ),
        {
            'epoch_id': epoch_id,
            'user_id': user_id,
            'provider': provider,
            'network': network,
            'account_address': preserved_account,
            'credential_version': preserved_credential_version,
            'started_at': now,
        },
    )
    await db.execute(
        text(
            """
            UPDATE users
            SET execution_provider = :provider,
                execution_network = :network,
                network_started_at = :started_at,
                active_execution_epoch_id = :epoch_id,
                updated_at = :started_at
            WHERE id = :user_id
            """
        ),
        {
            'provider': provider,
            'network': network,
            'started_at': now,
            'epoch_id': epoch_id,
            'user_id': user_id,
        },
    )
    return UserDestinationState(
        provider=provider,
        network=network,
        epoch_id=epoch_id,
        started_at=now,
    )
