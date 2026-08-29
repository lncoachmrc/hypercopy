from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, position_configs
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.api.deps import require_csrf, require_role
from app.core.config import Network, settings
from app.core.crypto import EncryptedCredential, crypto
from app.core.logging import get_logger
from app.db.redis import redis_client
from app.db.session import get_db
from app.models.entities import (
    AuditLog, CopyJob, CopyState, CredentialStatus, JobState, RiskHalt, RiskProfile,
    RiskState, Role, SigningCredential, SystemFlag, TradingAccount, User,
)
from app.schemas.admin import AdminAction, AdminReconcile
from app.services.audit import audit
from app.services.execution import live_trading_allowed
from app.services.metrics import system_snapshot
from app.services.networking import user_network_state
from app.services.queue import publish_job, repair_stream
from app.services.reconcile import master_snapshot, reconcile_user

router = APIRouter(prefix='/admin', tags=['admin'])
log = get_logger(__name__)
admin = require_role(Role.ADMIN, Role.SUPERADMIN)
superadmin = require_role(Role.SUPERADMIN)


def _limiter() -> WeightedRateLimiter:
    return WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))


def _master_adapter() -> HyperliquidAdapter:
    return HyperliquidAdapter(_limiter(), network=settings.master_network)


def _follower_adapter(network: Network) -> HyperliquidAdapter:
    return HyperliquidAdapter(_limiter(), network=network)


def _credential_blob(cred: SigningCredential) -> EncryptedCredential:
    return EncryptedCredential(
        cred.ciphertext_b64, cred.nonce_b64, cred.wrapped_dek_b64,
        cred.wrap_nonce_b64, cred.key_provider, cred.key_reference, cred.key_version,
    )


def _credential_active(cred: SigningCredential | None) -> bool:
    return bool(
        cred
        and cred.status in {CredentialStatus.ACTIVE, CredentialStatus.EXPIRING}
        and (cred.expires_at is None or cred.expires_at > datetime.now(UTC))
    )


def _position_config_sync_confirmation(network: Network) -> str:
    return f'SYNC {network.upper()} LEVERAGE'


async def _position_config_sync_signing_material(
    db: AsyncSession,
    target: User,
) -> tuple[uuid.UUID, str, SigningCredential]:
    """Load the API-wallet material needed to enter the signed-action path.

    This is deliberately not the mutable policy decision. The material is
    revalidated by the single final authorization immediately before SDK submit.
    """
    account = (await db.execute(
        select(TradingAccount)
        .where(TradingAccount.user_id == target.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not account:
        raise HTTPException(409, 'Follower has no Hyperliquid trading account')
    cred = (await db.execute(
        select(SigningCredential)
        .where(SigningCredential.trading_account_id == account.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not _credential_active(cred):
        raise HTTPException(409, 'Trading credential is unavailable')
    assert cred is not None
    return account.id, account.account_address, cred


async def _fresh_position_config_sync_authorization(
    db: AsyncSession,
    target: User,
    expected_network: Network,
    *,
    expected_account_id: uuid.UUID,
    expected_account_address: str,
    expected_credential_id: uuid.UUID,
    asset: str,
    master_leverage: int,
    exchange_max_leverage: int,
    desired_is_cross: bool,
) -> tuple[int, bool]:
    """Authoritative mutable decision executed at the actual submission boundary."""
    current_network = (await user_network_state(db, target.id)).network
    if current_network != expected_network:
        raise HTTPException(409, 'Follower network changed during leverage synchronization')

    await db.refresh(target, attribute_names=['copy_state'])
    if target.copy_state != CopyState.PAUSED:
        raise HTTPException(409, 'Pause the follower before direct leverage synchronization')

    flags = (await db.execute(
        select(SystemFlag)
        .where(SystemFlag.slug.in_(('live_trading', 'global_pause', 'emergency_stop')))
        .execution_options(populate_existing=True)
    )).scalars().all()
    flag_state = {flag.slug: bool(flag.enabled) for flag in flags}
    if expected_network == 'mainnet' and (
        not settings.ENABLE_LIVE_TRADING or not flag_state.get('live_trading', False)
    ):
        raise HTTPException(409, 'Mainnet live-trading gate is closed')
    active_halts = sorted(
        slug for slug in ('global_pause', 'emergency_stop') if flag_state.get(slug, False)
    )
    if active_halts:
        raise HTTPException(409, f"Leverage synchronization blocked by system halt: {', '.join(active_halts)}")

    risk = (await db.execute(
        select(RiskProfile)
        .where(RiskProfile.user_id == target.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not risk:
        raise HTTPException(409, 'Follower risk profile is missing')
    allowed_asset = (not risk.allow_assets or asset in risk.allow_assets) and asset not in risk.block_assets
    if not allowed_asset:
        raise HTTPException(409, f'{asset} is not permitted by the follower Risk Engine')
    desired_leverage = max(1, min(master_leverage, int(risk.max_leverage), exchange_max_leverage))

    account = (await db.execute(
        select(TradingAccount)
        .where(TradingAccount.user_id == target.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        not account
        or account.id != expected_account_id
        or account.account_address.lower() != expected_account_address.lower()
    ):
        raise HTTPException(409, 'Follower trading account changed during leverage synchronization')
    cred = (await db.execute(
        select(SigningCredential)
        .where(SigningCredential.trading_account_id == account.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        not _credential_active(cred)
        or cred is None
        or cred.id != expected_credential_id
    ):
        raise HTTPException(409, 'Trading credential changed or became unavailable')

    return desired_leverage, desired_is_cross


@router.get('/system')
async def system(user: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    limiter = _limiter()
    try:
        rate = await limiter.snapshot()
    except Exception:
        rate = {'status': 'redis_unavailable'}
    flags = (await db.execute(select(SystemFlag))).scalars().all()
    data = await system_snapshot(db, rate)
    data['flags'] = {f.slug: f.enabled for f in flags}
    data['master_network'] = settings.master_network
    data['follower_network'] = 'per-user'
    data['default_follower_network'] = settings.follower_network
    data['live_trading_env_enabled'] = settings.ENABLE_LIVE_TRADING
    return data


@router.get('/master-state')
async def master_state(user: User = Depends(admin)):
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise HTTPException(409, 'HYPERLIQUID_MASTER_ADDRESS is not configured')
    hl = _master_adapter()
    try:
        snapshot = await hl.account_snapshot(settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.DIAGNOSTIC)
        configs = position_configs(snapshot.perp_state)
        positions = []
        for row in snapshot.perp_state.get('assetPositions', []):
            position = row.get('position', row)
            size = Decimal(str(position.get('szi', '0') or '0'))
            if size == 0:
                continue
            asset = str(position.get('coin') or '')
            cfg = configs.get(asset)
            positions.append({
                'asset': asset,
                'size': str(size),
                'unrealized_pnl': str(position.get('unrealizedPnl') or '0'),
                'leverage': cfg.leverage if cfg else None,
                'margin_mode': ('cross' if cfg.is_cross else 'isolated') if cfg else None,
            })
        positions.sort(key=lambda x: x['asset'])
        return {
            'network': settings.master_network,
            'address': settings.HYPERLIQUID_MASTER_ADDRESS,
            'account_mode': snapshot.abstraction,
            'equity': str(snapshot.account_value),
            'free_margin': str(snapshot.free_margin),
            'open_positions': len(positions),
            'positions': positions,
        }
    except Exception as exc:
        raise HTTPException(502, f'Master state read failed: {type(exc).__name__}: {exc}') from exc


async def _position_config_diagnostic(
    db: AsyncSession,
    target: User,
    asset: str,
    *,
    follower_hl: HyperliquidAdapter | None = None,
) -> dict:
    asset = asset.upper().strip()
    network_state = await user_network_state(db, target.id)
    network = network_state.network
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == target.id))).scalar_one_or_none()
    risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == target.id))).scalar_one_or_none()
    if not account or not risk:
        raise HTTPException(409, 'Follower trading account or risk profile is missing')
    if not settings.HYPERLIQUID_MASTER_ADDRESS:
        raise HTTPException(409, 'HYPERLIQUID_MASTER_ADDRESS is not configured')

    master_hl = _master_adapter()
    follower_hl = follower_hl or _follower_adapter(network)
    if follower_hl.network != network:
        raise HTTPException(409, 'Follower adapter does not match the selected execution network')
    try:
        master_state, follower_state, spec = await asyncio.gather(
            master_hl.user_state(settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.DIAGNOSTIC),
            follower_hl.user_state(account.account_address, priority=Priority.DIAGNOSTIC),
            follower_hl.asset_spec(asset),
        )
    except Exception as exc:
        raise HTTPException(502, f'Position configuration exchange read failed: {type(exc).__name__}: {exc}') from exc

    master_cfg = position_configs(master_state).get(asset)
    follower_cfg = position_configs(follower_state).get(asset)
    if not master_cfg:
        raise HTTPException(409, f'Master has no open {asset} position/configuration')

    desired_leverage = max(1, min(master_cfg.leverage, int(risk.max_leverage), spec.max_leverage))
    desired_is_cross = bool(master_cfg.is_cross and not spec.only_isolated)
    allowed_asset = (not risk.allow_assets or asset in risk.allow_assets) and asset not in risk.block_assets
    latest_job = (await db.execute(
        select(CopyJob).where(
            CopyJob.user_id == target.id,
            CopyJob.asset == asset,
            CopyJob.created_at >= network_state.started_at,
        ).order_by(CopyJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    return {
        'user_id': str(target.id),
        'asset': asset,
        'copy_state': target.copy_state.value,
        'master_network': settings.master_network,
        'follower_network': network,
        'master': {'leverage': master_cfg.leverage, 'margin_mode': 'cross' if master_cfg.is_cross else 'isolated'},
        'follower': None if not follower_cfg else {'leverage': follower_cfg.leverage, 'margin_mode': 'cross' if follower_cfg.is_cross else 'isolated'},
        'desired': {'leverage': desired_leverage, 'margin_mode': 'cross' if desired_is_cross else 'isolated'},
        'allowed_asset': allowed_asset,
        'risk_max_leverage': str(risk.max_leverage),
        'exchange_max_leverage': spec.max_leverage,
        'matches': bool(follower_cfg and follower_cfg.leverage == desired_leverage and follower_cfg.is_cross == desired_is_cross),
        'latest_job': None if not latest_job else {
            'id': str(latest_job.id),
            'origin': latest_job.origin,
            'state': latest_job.state.value,
            'last_error': latest_job.last_error,
            'created_at': latest_job.created_at,
            'context': {
                'master_leverage': (latest_job.context or {}).get('master_leverage'),
                'desired_follower_leverage': (latest_job.context or {}).get('desired_follower_leverage'),
                'leverage_sync_only': (latest_job.context or {}).get('leverage_sync_only'),
            },
        },
    }


@router.get('/users/{user_id}/position-config/{asset}')
async def position_config_diagnostic(user_id: uuid.UUID, asset: str, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')
    try:
        return await _position_config_diagnostic(db, target, asset)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f'Position configuration read failed: {type(exc).__name__}: {exc}') from exc


@router.post('/users/{user_id}/position-config/{asset}/sync', dependencies=[Depends(require_csrf)])
async def sync_position_config(user_id: uuid.UUID, asset: str, body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')
    network = (await user_network_state(db, target.id)).network
    expected_confirmation = _position_config_sync_confirmation(network)
    if body.confirmation != expected_confirmation:
        raise HTTPException(422, f'Confirmation must be {expected_confirmation}')

    asset = asset.upper().strip()
    follower_hl = _follower_adapter(network)
    diagnostic = await _position_config_diagnostic(db, target, asset, follower_hl=follower_hl)
    if diagnostic['matches']:
        return {'ok': True, 'changed': False, 'verified': True, 'diagnostic': diagnostic}

    # Ensure signing metadata is already loaded before entering the signed-action
    # path. The one mutable policy authorization itself runs later, inside
    # _signed_call, after signer/rate/cadence waits and immediately before SDK submit.
    await follower_hl.asset_spec(asset)
    account_id, account_address, cred = await _position_config_sync_signing_material(db, target)
    expected_credential_id = cred.id
    desired_is_cross = diagnostic['desired']['margin_mode'] == 'cross'
    submitted = {
        'leverage': int(diagnostic['desired']['leverage']),
        'is_cross': desired_is_cross,
    }

    async def _authorize_submission() -> tuple[int, bool]:
        leverage, is_cross = await _fresh_position_config_sync_authorization(
            db,
            target,
            network,
            expected_account_id=account_id,
            expected_account_address=account_address,
            expected_credential_id=expected_credential_id,
            asset=asset,
            master_leverage=int(diagnostic['master']['leverage']),
            exchange_max_leverage=int(diagnostic['exchange_max_leverage']),
            desired_is_cross=desired_is_cross,
        )
        submitted['leverage'] = leverage
        submitted['is_cross'] = is_cross
        return leverage, is_cross

    private_key = crypto.decrypt(_credential_blob(cred), user_id=str(target.id), account_id=str(account_id))
    try:
        try:
            response = await follower_hl.update_leverage(
                account_address=account_address,
                private_key=private_key,
                asset=asset,
                leverage=int(submitted['leverage']),
                is_cross=bool(submitted['is_cross']),
                before_submit=_authorize_submission,
            )
        except HTTPException:
            raise
        except Exception as exc:
            await audit(db, action='ADMIN_FOLLOWER_LEVERAGE_SYNC_FAILED', actor_id=actor.id, subject_id=target.id, reason=body.reason, after={'asset': asset, 'network': network, 'error': f'{type(exc).__name__}: {exc}'})
            await db.commit()
            raise HTTPException(502, f'Hyperliquid rejected leverage sync: {type(exc).__name__}: {exc}') from exc
    finally:
        private_key = ''

    try:
        follower_state = await follower_hl.user_state(account_address, priority=Priority.DIAGNOSTIC)
        follower_cfg = position_configs(follower_state).get(asset)
    except Exception as exc:
        raise HTTPException(502, f'Leverage update sent, but verification read failed: {type(exc).__name__}: {exc}') from exc

    expected_leverage = int(submitted['leverage'])
    expected_cross = bool(submitted['is_cross'])
    desired = {
        'leverage': expected_leverage,
        'margin_mode': 'cross' if expected_cross else 'isolated',
    }
    verified_match = bool(
        follower_cfg
        and follower_cfg.leverage == expected_leverage
        and follower_cfg.is_cross == expected_cross
    )
    if not verified_match:
        observed = None if not follower_cfg else {
            'leverage': follower_cfg.leverage,
            'margin_mode': 'cross' if follower_cfg.is_cross else 'isolated',
        }
        await audit(db, action='ADMIN_FOLLOWER_LEVERAGE_SYNC_UNVERIFIED', actor_id=actor.id, subject_id=target.id, reason=body.reason, after={'asset': asset, 'network': network, 'response': response, 'desired': desired, 'observed': observed})
        await db.commit()
        raise HTTPException(502, f'Hyperliquid acknowledged the leverage update but follower state is {observed}; expected {desired}')

    verified = dict(diagnostic)
    verified['desired'] = desired
    verified['follower'] = {'leverage': follower_cfg.leverage, 'margin_mode': 'cross' if follower_cfg.is_cross else 'isolated'}
    verified['matches'] = True
    await audit(db, action='ADMIN_FOLLOWER_LEVERAGE_SYNCED', actor_id=actor.id, subject_id=target.id, reason=body.reason, after={'asset': asset, 'network': network, 'response': response, 'diagnostic': verified})
    await db.commit()
    return {'ok': True, 'changed': True, 'verified': True, 'response': response, 'diagnostic': verified}


@router.get('/users')
async def users(user: User = Depends(admin), db: AsyncSession = Depends(get_db), limit: int = 100, offset: int = 0, q: str | None = None):
    query = select(User)
    if q:
        query = query.where(User.auth_wallet.ilike(f'%{q}%'))
    rows = (await db.execute(query.order_by(User.created_at.desc()).offset(offset).limit(min(limit, 200)))).scalars().all()
    result=[]
    for row in rows:
        network=(await user_network_state(db,row.id)).network
        result.append({'id': str(row.id), 'auth_wallet': row.auth_wallet, 'role': row.role.value, 'state': row.state.value, 'copy_state': row.copy_state.value, 'execution_network': network, 'created_at': row.created_at})
    return result


@router.get('/users/{user_id}')
async def user_detail(user_id: uuid.UUID, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')
    network=(await user_network_state(db,target.id)).network
    return {'id': str(target.id), 'auth_wallet': target.auth_wallet, 'role': target.role.value, 'state': target.state.value, 'copy_state': target.copy_state.value, 'execution_network': network, 'manual_trade_policy': target.manual_trade_policy.value}


@router.post('/users/{user_id}/pause', dependencies=[Depends(require_csrf)])
async def pause_user(user_id: uuid.UUID, body: AdminAction, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')
    target.copy_state = CopyState.PAUSED
    network=(await user_network_state(db,target.id)).network
    await audit(db, action='ADMIN_USER_PAUSE', actor_id=actor.id, subject_id=target.id, reason=body.reason, after={'follower_network': network})
    await db.commit()
    return {'ok': True}


@router.post('/users/{user_id}/resume', dependencies=[Depends(require_csrf)])
async def resume_user(user_id: uuid.UUID, body: AdminAction, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')
    network=(await user_network_state(db,target.id)).network
    if not await live_trading_allowed(db, network):
        raise HTTPException(409, 'Mainnet live-trading gate is closed')
    rs = (await db.execute(select(RiskState).where(RiskState.user_id == target.id))).scalar_one_or_none()
    if rs and rs.state != RiskHalt.NORMAL:
        raise HTTPException(409, f'Cannot resume while {rs.state.value} is active')
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == target.id))).scalar_one_or_none()
    if not account:
        raise HTTPException(409, 'Follower has no Hyperliquid trading account')
    limiter = _limiter()
    master_hl = HyperliquidAdapter(limiter, network=settings.master_network)
    follower_hl = HyperliquidAdapter(limiter, network=network)
    target.copy_state = CopyState.ACTIVE
    try:
        mp, meq, master_mids = await master_snapshot(master_hl)
        master_state = await master_hl.user_state(
            settings.HYPERLIQUID_MASTER_ADDRESS,
            priority=Priority.RECONCILE,
        )
        master_configs = position_configs(master_state)
        follower_mids = master_mids if settings.master_network == network else await follower_hl.mids()
        result = await reconcile_user(
            db,
            follower_hl,
            target,
            master_positions=mp,
            master_equity=meq,
            mids=follower_mids,
            master_mids=master_mids,
            master_configs=master_configs,
        )
        published = await repair_stream(redis_client(), db)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(503, f'Admin activation failed: {type(exc).__name__}: {exc}') from exc
    await audit(db, action='ADMIN_USER_RESUME', actor_id=actor.id, subject_id=target.id, reason=body.reason, after={
        'master_network': settings.master_network,
        'follower_network': network,
        'reconciliation': result,
        'stream_published': published,
    })
    await db.commit()
    return {
        'ok': True,
        'copy_state': target.copy_state.value,
        'network': network,
        'stream_published': published,
        'reconciliation': result,
    }


@router.post('/users/{user_id}/reconcile', dependencies=[Depends(require_csrf)])
async def queue_reconcile(user_id: uuid.UUID, body: AdminReconcile, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, 'User not found')
    network_state = await user_network_state(db, target.id)
    network = network_state.network

    existing = (await db.execute(
        select(CopyJob).where(
            CopyJob.user_id == target.id,
            CopyJob.origin == 'ADMIN_RECONCILE',
            CopyJob.created_at >= network_state.started_at,
            CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
        ).order_by(CopyJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if existing:
        await audit(
            db,
            action='ADMIN_RECONCILE_REUSED',
            actor_id=actor.id,
            subject_id=target.id,
            reason=body.reason,
            after={'job_id': str(existing.id), 'state': existing.state.value, 'follower_network': network},
        )
        await db.commit()
        return {
            'queued': True,
            'job_id': str(existing.id),
            'state': existing.state.value,
            'network': network,
            'stream_published': existing.enqueued_at is not None,
            'reused': True,
        }

    job = CopyJob(
        user_id=target.id,
        asset='__RECONCILE__',
        origin='ADMIN_RECONCILE',
        state=JobState.QUEUED,
        correlation_id=uuid.uuid4().hex,
        context={'reason': body.reason, 'master_network': settings.master_network, 'follower_network': network},
    )
    db.add(job)
    await db.flush()

    published = False
    try:
        await publish_job(redis_client(), db, job)
        published = True
    except Exception:
        job.enqueued_at = None
        log.warning(
            'Immediate admin reconcile publish failed; durable repair will retry',
            extra={'job_id': str(job.id), 'user_id': str(target.id), 'network': network},
            exc_info=True,
        )

    await audit(
        db,
        action='ADMIN_RECONCILE_REQUESTED',
        actor_id=actor.id,
        subject_id=target.id,
        reason=body.reason,
        after={'job_id': str(job.id), 'stream_published': published, 'follower_network': network},
    )
    await db.commit()
    return {
        'queued': True,
        'job_id': str(job.id),
        'state': job.state.value,
        'network': network,
        'stream_published': published,
        'reused': False,
    }


def _reconcile_job_payload(job: CopyJob) -> dict:
    context = job.context or {}
    return {
        'job_id': str(job.id),
        'state': job.state.value,
        'network': context.get('follower_network'),
        'last_error': job.last_error,
        'attempt_count': job.attempt_count,
        'next_attempt_at': job.next_attempt_at,
        'result': context.get('result'),
        'stream_published': int(context.get('stream_published') or 0),
        'completed_at': context.get('completed_at'),
    }


@router.get('/users/{user_id}/reconcile/{job_id}')
async def reconcile_status(user_id: uuid.UUID, job_id: uuid.UUID, actor: User = Depends(admin), db: AsyncSession = Depends(get_db)):
    job = await db.get(CopyJob, job_id)
    if not job or job.user_id != user_id or job.origin != 'ADMIN_RECONCILE':
        raise HTTPException(404, 'Reconciliation job not found')
    return _reconcile_job_payload(job)


async def _flag(db: AsyncSession, slug: str, enabled: bool, actor: User, reason: str):
    flag = await db.get(SystemFlag, slug)
    if not flag:
        flag = SystemFlag(slug=slug, enabled=enabled)
        db.add(flag)
    flag.enabled = enabled
    flag.reason = reason
    flag.updated_by = actor.id
    await audit(db, action=f'SYSTEM_FLAG_{slug.upper()}', actor_id=actor.id, reason=reason, after={'enabled': enabled})
    await db.commit()


@router.post('/system/pause', dependencies=[Depends(require_csrf)])
async def global_pause(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    await _flag(db, 'global_pause', True, actor, body.reason)
    return {'ok': True}


@router.post('/system/emergency-stop', dependencies=[Depends(require_csrf)])
async def emergency(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    if body.confirmation != 'EMERGENCY STOP':
        raise HTTPException(422, 'Confirmation must be EMERGENCY STOP')
    await _flag(db, 'emergency_stop', True, actor, body.reason)
    return {'ok': True, 'note': 'Open/increase is blocked; exits remain permitted'}


@router.post('/system/live-trading', dependencies=[Depends(require_csrf)])
async def live_trading(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    if body.confirmation != 'ENABLE MAINNET':
        raise HTTPException(422, 'Confirmation must be ENABLE MAINNET')
    if not settings.ENABLE_LIVE_TRADING:
        raise HTTPException(409, 'ENABLE_LIVE_TRADING environment gate is not enabled')
    await _flag(db, 'live_trading', True, actor, body.reason)
    return {'ok': True}


@router.post('/system/live-trading/disable', dependencies=[Depends(require_csrf)])
async def disable_live_trading(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    await _flag(db, 'live_trading', False, actor, body.reason)
    return {'ok': True}


@router.post('/system/resume', dependencies=[Depends(require_csrf)])
async def resume_system(body: AdminAction, actor: User = Depends(superadmin), db: AsyncSession = Depends(get_db)):
    await _flag(db, 'global_pause', False, actor, body.reason)
    await _flag(db, 'emergency_stop', False, actor, body.reason)
    return {'ok': True}


@router.get('/audit')
async def audit_log(actor: User = Depends(admin), db: AsyncSession = Depends(get_db), limit: int = 100, offset: int = 0):
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.ts.desc()).offset(offset).limit(min(limit, 200)))).scalars().all()
    return [{'id': str(x.id), 'action': x.action, 'actor_id': str(x.actor_id) if x.actor_id else None, 'subject_id': str(x.subject_id) if x.subject_id else None, 'reason': x.reason, 'ts': x.ts, 'before': x.before, 'after': x.after} for x in rows]