from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ExecutionProvider
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.core.config import Network, settings
from app.db.redis import redis_client


class DestinationLifecycleState(str, Enum):
    VERIFIED_FLAT = 'VERIFIED_FLAT'
    NEVER_ACTIVATED = 'NEVER_ACTIVATED'
    UNREADABLE = 'UNREADABLE'


@dataclass(frozen=True, slots=True)
class DestinationSwitchBlocker:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DestinationSwitchAssessment:
    state: DestinationLifecycleState
    source_epoch_id: uuid.UUID | None
    provider: ExecutionProvider
    network: Network
    blockers: tuple[DestinationSwitchBlocker, ...]
    reason: str | None = None


class DestinationSwitchBlocked(RuntimeError):
    def __init__(self, assessment: DestinationSwitchAssessment):
        self.assessment = assessment
        codes = ','.join(blocker.code for blocker in assessment.blockers)
        detail = codes or assessment.reason or assessment.state.value
        super().__init__(f'Destination switch blocked: {detail}')


async def destination_switch_blockers(
    db: AsyncSession,
    user_id: uuid.UUID,
    source_epoch_id: uuid.UUID | None,
) -> tuple[DestinationSwitchBlocker, ...]:
    row = (
        await db.execute(
            text('SELECT copy_state FROM users WHERE id = :user_id'),
            {'user_id': user_id},
        )
    ).mappings().one_or_none()
    if row is None:
        return (DestinationSwitchBlocker('user_unavailable', 'User state is unavailable.'),)

    blockers: list[DestinationSwitchBlocker] = []
    if str(row['copy_state']) != 'PAUSED':
        blockers.append(
            DestinationSwitchBlocker('pause_required', 'Strategy must be PAUSED before switching destination.')
        )

    has_position = (
        await db.execute(
            text(
                'SELECT 1 FROM position_ledger '
                'WHERE user_id = :user_id AND managed IS TRUE AND size <> 0 LIMIT 1'
            ),
            {'user_id': user_id},
        )
    ).scalar_one_or_none()
    if has_position is not None:
        blockers.append(
            DestinationSwitchBlocker('positions_not_flat', 'Managed TRAXION positions must be flat.')
        )

    epoch_filter = 'execution_epoch_id = :source_epoch_id'
    params: dict[str, object] = {'user_id': user_id, 'source_epoch_id': source_epoch_id}
    if source_epoch_id is None:
        epoch_filter = 'execution_epoch_id IS NULL'
        params.pop('source_epoch_id')

    has_pending_job = (
        await db.execute(
            text(
                'SELECT 1 FROM copy_jobs '
                'WHERE user_id = :user_id '
                f'AND {epoch_filter} '
                "AND state IN ('QUEUED','PROCESSING','RETRYING') LIMIT 1"
            ),
            params,
        )
    ).scalar_one_or_none()
    if has_pending_job is not None:
        blockers.append(
            DestinationSwitchBlocker('pending_jobs', 'Pending destination jobs must resolve before switching.')
        )

    has_unresolved_execution = (
        await db.execute(
            text(
                'SELECT 1 FROM executions '
                'WHERE user_id = :user_id '
                f'AND {epoch_filter} '
                "AND state IN ('SUBMITTING','UNKNOWN') LIMIT 1"
            ),
            params,
        )
    ).scalar_one_or_none()
    if has_unresolved_execution is not None:
        blockers.append(
            DestinationSwitchBlocker(
                'unresolved_executions',
                'SUBMITTING or UNKNOWN executions must resolve before switching.',
            )
        )

    return tuple(blockers)


async def _never_activated(db: AsyncSession, user_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            text(
                """
                SELECT
                    EXISTS(SELECT 1 FROM trading_accounts WHERE user_id = :user_id) AS has_account,
                    EXISTS(
                        SELECT 1
                        FROM signing_credentials sc
                        JOIN trading_accounts ta ON ta.id = sc.trading_account_id
                        WHERE ta.user_id = :user_id
                    ) AS has_credential,
                    EXISTS(
                        SELECT 1 FROM execution_epochs
                        WHERE user_id = :user_id AND account_address IS NOT NULL
                    ) AS has_bound_epoch
                """
            ),
            {'user_id': user_id},
        )
    ).mappings().one()
    return not bool(row['has_account'] or row['has_credential'] or row['has_bound_epoch'])


def _reader(network: Network) -> HyperliquidAdapter:
    limiter = WeightedRateLimiter(
        redis_client(),
        Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN),
    )
    return HyperliquidAdapter(limiter, network=network)


def _unreadable(
    *,
    source_epoch_id: uuid.UUID | None,
    provider: ExecutionProvider,
    network: Network,
    blockers: tuple[DestinationSwitchBlocker, ...] = (),
    reason: str,
) -> DestinationSwitchAssessment:
    return DestinationSwitchAssessment(
        state=DestinationLifecycleState.UNREADABLE,
        source_epoch_id=source_epoch_id,
        provider=provider,
        network=network,
        blockers=blockers,
        reason=reason,
    )


async def assess_destination_switch(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    source_epoch_id: uuid.UUID | None,
    provider: ExecutionProvider,
    network: Network,
) -> DestinationSwitchAssessment:
    # Local lifecycle proof comes first. In particular, NEVER_ACTIVATED must be
    # established entirely from durable DB facts before any provider object/read.
    blockers = await destination_switch_blockers(db, user_id, source_epoch_id)
    if blockers:
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            blockers=blockers,
            reason='DB-local safe-switch prerequisites are not satisfied.',
        )

    if await _never_activated(db, user_id):
        return DestinationSwitchAssessment(
            state=DestinationLifecycleState.NEVER_ACTIVATED,
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            blockers=(),
            reason='No trading account, signing credential, or historically bound execution epoch exists.',
        )

    if source_epoch_id is None:
        return _unreadable(
            source_epoch_id=None,
            provider=provider,
            network=network,
            reason='Activated destination has no authoritative source epoch.',
        )

    source = (
        await db.execute(
            text(
                """
                SELECT id, provider, network, account_address, credential_version
                FROM execution_epochs
                WHERE id = :epoch_id AND user_id = :user_id
                """
            ),
            {'epoch_id': source_epoch_id, 'user_id': user_id},
        )
    ).mappings().one_or_none()
    if source is None:
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason='Source execution epoch is unavailable.',
        )
    if str(source['provider']).lower() != provider or str(source['network']).lower() != network:
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason='Source execution epoch identity does not match persisted destination.',
        )

    account_address = str(source['account_address'] or '').lower()
    if not account_address:
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason='Activated destination source account identity is missing.',
        )

    account = (
        await db.execute(
            text(
                """
                SELECT ta.account_address, sc.key_version
                FROM trading_accounts ta
                LEFT JOIN signing_credentials sc ON sc.trading_account_id = ta.id
                WHERE ta.user_id = :user_id
                """
            ),
            {'user_id': user_id},
        )
    ).mappings().one_or_none()
    if account is None or str(account['account_address'] or '').lower() != account_address:
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason='Stored trading-account identity is missing or disagrees with the source epoch.',
        )
    source_credential_version = source['credential_version']
    if source_credential_version is None or account['key_version'] is None:
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason='Source credential identity is incomplete.',
        )
    if int(account['key_version']) != int(source_credential_version):
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason='Source credential identity disagrees with the source epoch.',
        )

    if provider != 'hyperliquid':
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason='Activated provider does not yet expose complete safe-switch read verification.',
        )

    try:
        adapter = _reader(network)
        state = await adapter.user_state(account_address, priority=Priority.DIAGNOSTIC)
        orders = await adapter.frontend_open_orders(account_address, priority=Priority.DIAGNOSTIC)

        if not isinstance(state, dict):
            raise ValueError('Hyperliquid account state is not an object')
        positions = state.get('assetPositions')
        if not isinstance(positions, list):
            raise ValueError('Hyperliquid account state is missing assetPositions')

        for row in positions:
            if not isinstance(row, dict):
                raise ValueError('Hyperliquid position row is malformed')
            position = row.get('position')
            if not isinstance(position, dict) or 'szi' not in position:
                raise ValueError('Hyperliquid position payload is incomplete')
            try:
                size = Decimal(str(position['szi']))
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ValueError('Hyperliquid position size is malformed') from exc
            if size != 0:
                blocker = DestinationSwitchBlocker(
                    'provider_positions_not_flat',
                    'Hyperliquid still reports an open position.',
                )
                return _unreadable(
                    source_epoch_id=source_epoch_id,
                    provider=provider,
                    network=network,
                    blockers=(blocker,),
                    reason='Provider-side positions are not flat.',
                )

        if orders:
            blocker = DestinationSwitchBlocker(
                'provider_open_orders',
                'Hyperliquid still reports open or conditional orders.',
            )
            return _unreadable(
                source_epoch_id=source_epoch_id,
                provider=provider,
                network=network,
                blockers=(blocker,),
                reason='Provider-side open/conditional orders are present.',
            )
    except Exception as exc:
        return _unreadable(
            source_epoch_id=source_epoch_id,
            provider=provider,
            network=network,
            reason=f'Provider-side destination verification failed: {type(exc).__name__}.',
        )

    return DestinationSwitchAssessment(
        state=DestinationLifecycleState.VERIFIED_FLAT,
        source_epoch_id=source_epoch_id,
        provider=provider,
        network=network,
        blockers=(),
        reason='Provider-side positions and open/conditional orders are verified empty.',
    )
