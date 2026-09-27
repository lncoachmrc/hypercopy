from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, position_configs
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.api.deps import current_user, require_csrf
from app.core.config import settings
from app.db.position_ledger_lock import position_ledger_lock
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import (
    CopyJob,
    CopyState,
    CredentialStatus,
    Execution,
    ExecutionEpoch,
    ExecutionState,
    JobState,
    RISExSigningCredential,
    RISExTradingAccount,
    RiskHalt,
    RiskState,
    TradingAccount,
    User,
)
from app.services.audit import audit
from app.services.entitlement import entitlement
from app.services.execution import live_trading_allowed
from app.services.master_source_identity import (
    MASTER_SOURCE_FOLLOWER_BLOCK_REASON,
    is_master_source_user,
)
from app.services.networking import user_network_state
from app.services.queue import repair_stream
from app.services.reconcile import (
    ReconcileObservationIndeterminate,
    master_snapshot_started_order,
    observed_master_mids,
    reconcile_observed_follower,
    reconcile_user,
)
from app.services.risex_order_preparation import assert_risex_environment_allowed
from app.services.risex_reconcile_observation import read_risex_reconcile_observation
from app.services.risex_signer_binding import _verify_risex_signer_binding


router = APIRouter(tags=['activation'])


@dataclass(frozen=True, slots=True)
class _RISExActivationBinding:
    account_id: uuid.UUID
    credential_id: uuid.UUID
    account_address: str
    signer_address: str
    generation: int
    status: CredentialStatus
    expires_at: datetime | None


def _limiter() -> WeightedRateLimiter:
    return WeightedRateLimiter(
        redis_client(),
        Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN),
    )


def _positions(state: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in state.get('assetPositions', []):
        p = row.get('position', row)
        asset = str(p.get('coin') or '')
        if not asset:
            continue
        out[asset] = Decimal(str(p.get('szi', '0') or '0'))
    return out


def _activation_entitlement_error(ent: dict) -> str | None:
    if ent.get('entitled'):
        return None

    if ent.get('portfolio_limit_exceeded'):
        equity = ent.get('portfolio_equity')
        limit = ent.get('portfolio_limit_usd')
        if equity is not None and limit is not None:
            return (
                f'Current plan does not cover this portfolio size '
                f'(equity ${float(equity):.2f} > plan limit ${float(limit):.2f}). '
                'Choose a plan that covers the account before activating the strategy.'
            )
        return (
            'Current plan does not cover this portfolio size. '
            'Choose a higher plan before activating the strategy.'
        )

    status = str(ent.get('status') or 'none').lower()
    if status == 'none':
        return 'Activate a plan before activating the strategy.'
    if status == 'complimentary':
        return (
            'The complimentary plan is no longer entitled. Restore its 100% '
            'personal discount or activate another plan.'
        )
    return (
        f'Subscription is not entitled ({status}). '
        'Activate or renew a plan before activating the strategy.'
    )


async def _risex_activation_binding(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> _RISExActivationBinding | None:
    stmt = (
        select(
            RISExTradingAccount.id.label('account_id'),
            RISExSigningCredential.id.label('credential_id'),
            RISExTradingAccount.account_address.label('account_address'),
            RISExSigningCredential.signer_address.label('signer_address'),
            RISExSigningCredential.generation.label('generation'),
            RISExSigningCredential.status.label('status'),
            RISExSigningCredential.expires_at.label('expires_at'),
        )
        .join(
            RISExSigningCredential,
            RISExSigningCredential.risex_trading_account_id
            == RISExTradingAccount.id,
        )
        .where(RISExTradingAccount.user_id == user_id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).mappings().one_or_none()
    if row is None:
        return None
    return _RISExActivationBinding(
        account_id=row['account_id'],
        credential_id=row['credential_id'],
        account_address=str(row['account_address']),
        signer_address=str(row['signer_address']),
        generation=int(row['generation']),
        status=row['status'],
        expires_at=row['expires_at'],
    )


def _require_usable_risex_binding(
    binding: _RISExActivationBinding | None,
) -> _RISExActivationBinding:
    if binding is None:
        raise HTTPException(409, 'Connect a RISEx trading account first')
    if binding.status not in {CredentialStatus.ACTIVE, CredentialStatus.EXPIRING}:
        raise HTTPException(
            409,
            f'RISEx credential status {binding.status.value} is not usable',
        )
    if binding.expires_at is None or binding.expires_at <= datetime.now(UTC):
        raise HTTPException(409, 'RISEx credential is expired or has no verifiable expiry')
    return binding


def _same_risex_binding(
    before: _RISExActivationBinding,
    after: _RISExActivationBinding,
) -> bool:
    return (
        before.account_id == after.account_id
        and before.credential_id == after.credential_id
        and before.account_address.lower() == after.account_address.lower()
        and before.signer_address.lower() == after.signer_address.lower()
        and before.generation == after.generation
        and before.status == after.status
        and before.expires_at == after.expires_at
    )


async def _require_risex_epoch_binding(
    db: AsyncSession,
    *,
    epoch_id: uuid.UUID,
    network: str,
    binding: _RISExActivationBinding,
) -> ExecutionEpoch:
    epoch = await db.get(ExecutionEpoch, epoch_id)
    if epoch is None or epoch.ended_at is not None:
        raise HTTPException(409, 'RISEx active execution epoch is unavailable')
    if epoch.provider != 'risex' or epoch.network != network:
        raise HTTPException(409, 'RISEx active execution epoch binding mismatch')
    if (
        epoch.account_address is None
        or epoch.account_address.lower() != binding.account_address.lower()
    ):
        raise HTTPException(409, 'RISEx epoch account binding mismatch')
    if epoch.credential_version != binding.generation:
        raise HTTPException(409, 'RISEx epoch credential generation mismatch')
    return epoch


def _require_verified_risex_session(
    evidence,
    binding: _RISExActivationBinding,
) -> None:
    if getattr(evidence, 'deployment_identity_verified', True) is not True:
        raise HTTPException(409, 'RISEx deployment identity is not verified')
    if str(getattr(evidence, 'account', '')).lower() != binding.account_address.lower():
        raise HTTPException(409, 'RISEx authorization account binding mismatch')
    if str(getattr(evidence, 'signer', '')).lower() != binding.signer_address.lower():
        raise HTTPException(409, 'RISEx authorization signer binding mismatch')
    if getattr(evidence, 'session_active', None) is not True:
        raise HTTPException(409, 'RISEx authorization session is inactive')
    if getattr(evidence, 'session_not_expired', None) is not True:
        raise HTTPException(409, 'RISEx authorization session is expired')
    if getattr(evidence, 'perps_permission', None) is not True:
        raise HTTPException(409, 'RISEx authorization lacks Perps permission')


async def _rollback_activation(
    db: AsyncSession,
    *,
    user: User,
    activation_started: datetime,
    network: str,
    exc: Exception,
) -> None:
    rows = (
        await db.execute(
            select(CopyJob).where(
                CopyJob.user_id == user.id,
                CopyJob.created_at >= activation_started,
                CopyJob.state.in_(
                    [JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]
                ),
            )
        )
    ).scalars().all()
    for job in rows:
        job.state = JobState.SKIPPED
        job.last_error = 'Activation rolled back before execution'
        job.owner = None
        job.locked_until = None
    user.copy_state = CopyState.PAUSED
    await audit(
        db,
        action='COPY_ACTIVATION_ROLLED_BACK',
        actor_id=user.id,
        subject_id=user.id,
        reason=f'{type(exc).__name__}: {exc}',
        after={'follower_network': network},
    )
    await db.commit()


async def _resume_risex_alignment(
    db: AsyncSession,
    user: User,
    *,
    observation,
    master_positions: dict[str, Decimal],
    master_equity: Decimal,
    master_mids: dict[str, str],
    master_configs,
    create_jobs: bool = True,
) -> dict:
    return await reconcile_observed_follower(
        db,
        user,
        observation=observation,
        master_positions=master_positions,
        master_equity=master_equity,
        master_mids=master_mids,
        master_configs=master_configs,
        create_jobs=create_jobs,
    )


@router.post('/copy/resume', dependencies=[Depends(require_csrf)])
async def resume_copy_immediate(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Activate the selected follower destination after fresh, fail-closed alignment."""
    if is_master_source_user(user):
        raise HTTPException(409, MASTER_SOURCE_FOLLOWER_BLOCK_REASON)

    network_state = await user_network_state(db, user.id)
    network = network_state.network
    provider = network_state.provider

    if not await live_trading_allowed(db, network):
        raise HTTPException(409, 'Mainnet live-trading gate is closed')

    rs = (
        await db.execute(select(RiskState).where(RiskState.user_id == user.id))
    ).scalar_one_or_none()
    if rs and rs.state != RiskHalt.NORMAL:
        raise HTTPException(409, f'Cannot resume while {rs.state.value} is active')

    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise HTTPException(409, 'Strategy source account is not configured')

    if provider == 'risex':
        pending = (
            await db.execute(
                select(CopyJob.id)
                .where(
                    CopyJob.user_id == user.id,
                    CopyJob.execution_epoch_id == network_state.epoch_id,
                    CopyJob.state.in_(
                        [JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if pending is not None:
            raise HTTPException(
                409,
                'Pending RISEx jobs exist on the active epoch; activation is blocked',
            )

        unresolved = (
            await db.execute(
                select(Execution.id)
                .where(
                    Execution.user_id == user.id,
                    Execution.execution_epoch_id == network_state.epoch_id,
                    Execution.execution_provider == 'risex',
                    Execution.state.in_(
                        [ExecutionState.SUBMITTING, ExecutionState.UNKNOWN]
                    ),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if unresolved is not None:
            raise HTTPException(
                409,
                'Unresolved RISEx execution exists on the active epoch',
            )

        binding = _require_usable_risex_binding(
            await _risex_activation_binding(db, user.id)
        )
        await _require_risex_epoch_binding(
            db,
            epoch_id=network_state.epoch_id,
            network=network,
            binding=binding,
        )

        try:
            assert_risex_environment_allowed(
                network=network,
                env=os.environ,
            )
        except Exception as exc:
            raise HTTPException(409, f'RISEx environment gate rejected activation: {exc}') from exc

        try:
            evidence = await _verify_risex_signer_binding(
                account_address=binding.account_address,
                signer_address=binding.signer_address,
            )
        except Exception as exc:
            raise HTTPException(
                409,
                f'RISEx authorization verification failed: {exc}',
            ) from exc
        _require_verified_risex_session(evidence, binding)

        limiter = _limiter()
        master_hl = HyperliquidAdapter(limiter, network=settings.master_network)
        try:
            snapshot_started_order = await master_snapshot_started_order(required=True)
            source_snapshot = await master_hl.account_snapshot(
                settings.HYPERLIQUID_MASTER_ADDRESS,
                priority=Priority.RECONCILE,
            )
            master_positions = _positions(source_snapshot.perp_state)
            master_configs = position_configs(source_snapshot.perp_state)
            master_mids = observed_master_mids(await master_hl.mids(), snapshot_started_order)
            observation = await read_risex_reconcile_observation(
                account_address=binding.account_address,
                epoch_id=network_state.epoch_id,
                network=network,
                started_at=network_state.started_at,
            )
        except ReconcileObservationIndeterminate as exc:
            raise HTTPException(409, f'RISEx follower observation is indeterminate: {exc}') from exc
        except Exception as exc:
            raise HTTPException(
                503,
                f'Strategy activation preflight failed: {type(exc).__name__}: {exc}',
            ) from exc

        ent = await entitlement(
            db,
            user,
            portfolio_equity_override=observation.account_equity,
        )
        entitlement_error = _activation_entitlement_error(ent)
        if entitlement_error:
            raise HTTPException(409, entitlement_error)

        activation_started = datetime.now(UTC)
        async with position_ledger_lock(user.id):
            locked_state = await user_network_state(db, user.id)
            locked_binding = _require_usable_risex_binding(
                await _risex_activation_binding(db, user.id, for_update=True)
            )
            if (
                locked_state.provider != 'risex'
                or locked_state.network != network
                or locked_state.epoch_id != network_state.epoch_id
                or not _same_risex_binding(binding, locked_binding)
            ):
                raise HTTPException(
                    409,
                    'RISEx credential generation or destination binding rotated during activation',
                )
            await _require_risex_epoch_binding(
                db,
                epoch_id=locked_state.epoch_id,
                network=network,
                binding=locked_binding,
            )

            user.copy_state = CopyState.ACTIVE
            await audit(
                db,
                action='COPY_ACTIVATION_STARTED',
                actor_id=user.id,
                subject_id=user.id,
                after={
                    'master_network': settings.master_network,
                    'follower_network': network,
                    'execution_provider': 'risex',
                    'execution_epoch_id': str(locked_state.epoch_id),
                    'master_positions': len(
                        [x for x in master_positions.values() if x != 0]
                    ),
                    'follower_equity': str(observation.account_equity),
                    'entitlement_plan': ent.get('commercial_plan') or ent.get('plan'),
                    'entitlement_status': ent.get('status'),
                },
            )
            await db.commit()

            try:
                result = await _resume_risex_alignment(
                    db,
                    user,
                    observation=observation,
                    master_positions=master_positions,
                    master_equity=source_snapshot.account_value,
                    master_mids=master_mids,
                    master_configs=master_configs,
                    create_jobs=True,
                )
                published = await repair_stream(redis_client(), db)
                await audit(
                    db,
                    action='COPY_RESUMED',
                    actor_id=user.id,
                    subject_id=user.id,
                    after={
                        'master_network': settings.master_network,
                        'follower_network': network,
                        'execution_provider': 'risex',
                        'execution_epoch_id': str(locked_state.epoch_id),
                        'reconciliation': result,
                        'stream_published': published,
                    },
                )
                await db.commit()
                return {
                    'ok': True,
                    'copy_state': user.copy_state.value,
                    'network': network,
                    'master_positions': len(
                        [x for x in master_positions.values() if x != 0]
                    ),
                    'stream_published': published,
                    'reconciliation': result,
                }
            except Exception as exc:
                await _rollback_activation(
                    db,
                    user=user,
                    activation_started=activation_started,
                    network=network,
                    exc=exc,
                )
                if isinstance(exc, ReconcileObservationIndeterminate):
                    raise HTTPException(
                        409,
                        f'Strategy activation rolled back: {type(exc).__name__}: {exc}',
                    ) from exc
                raise HTTPException(
                    503,
                    f'Strategy activation failed and the account was paused: '
                    f'{type(exc).__name__}: {exc}',
                ) from exc

    if provider != 'hyperliquid':
        raise HTTPException(409, f'Unsupported execution provider: {provider}')

    account = (
        await db.execute(
            select(TradingAccount).where(TradingAccount.user_id == user.id)
        )
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(409, 'Connect a Hyperliquid trading account first')

    pending = (
        await db.execute(
            select(CopyJob.id)
            .where(
                CopyJob.user_id == user.id,
                CopyJob.state.in_(
                    [JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending:
        raise HTTPException(
            409,
            'Pending strategy jobs exist; wait for Queue to return to 0 before activation',
        )

    limiter = _limiter()
    master_hl = HyperliquidAdapter(limiter, network=settings.master_network)
    follower_hl = HyperliquidAdapter(limiter, network=network)

    try:
        snapshot_started_order = await master_snapshot_started_order(required=True)
        source_snapshot = await master_hl.account_snapshot(
            settings.HYPERLIQUID_MASTER_ADDRESS,
            priority=Priority.RECONCILE,
        )
        follower_snapshot = await follower_hl.account_snapshot(
            account.account_address,
            priority=Priority.RECONCILE,
        )
        master_positions = _positions(source_snapshot.perp_state)
        master_configs = position_configs(source_snapshot.perp_state)
        master_mids = observed_master_mids(await master_hl.mids(), snapshot_started_order)
        follower_mids = (
            master_mids
            if settings.master_network == network
            else await follower_hl.mids()
        )
    except Exception as exc:
        raise HTTPException(
            503,
            f'Strategy activation preflight failed: {type(exc).__name__}: {exc}',
        ) from exc

    ent = await entitlement(
        db,
        user,
        portfolio_equity_override=follower_snapshot.account_value,
    )
    entitlement_error = _activation_entitlement_error(ent)
    if entitlement_error:
        raise HTTPException(409, entitlement_error)

    activation_started = datetime.now(UTC)
    user.copy_state = CopyState.ACTIVE
    await audit(
        db,
        action='COPY_ACTIVATION_STARTED',
        actor_id=user.id,
        subject_id=user.id,
        after={
            'master_network': settings.master_network,
            'follower_network': network,
            'master_positions': len(
                [x for x in master_positions.values() if x != 0]
            ),
            'follower_equity': str(follower_snapshot.account_value),
            'entitlement_plan': ent.get('commercial_plan') or ent.get('plan'),
            'entitlement_status': ent.get('status'),
        },
    )
    await db.commit()

    try:
        result = await reconcile_user(
            db,
            follower_hl,
            user,
            master_positions=master_positions,
            master_equity=source_snapshot.account_value,
            mids=follower_mids,
            master_mids=master_mids,
            master_configs=master_configs,
        )
        published = await repair_stream(redis_client(), db)
        await audit(
            db,
            action='COPY_RESUMED',
            actor_id=user.id,
            subject_id=user.id,
            after={
                'master_network': settings.master_network,
                'follower_network': network,
                'reconciliation': result,
                'stream_published': published,
            },
        )
        await db.commit()
        return {
            'ok': True,
            'copy_state': user.copy_state.value,
            'network': network,
            'master_positions': len(
                [x for x in master_positions.values() if x != 0]
            ),
            'stream_published': published,
            'reconciliation': result,
        }
    except Exception as exc:
        await _rollback_activation(
            db,
            user=user,
            activation_started=activation_started,
            network=network,
            exc=exc,
        )
        raise HTTPException(
            503,
            f'Strategy activation failed and the account was paused: '
            f'{type(exc).__name__}: {exc}',
        ) from exc
