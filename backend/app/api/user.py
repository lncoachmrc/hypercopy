from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from eth_account import Account

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Budget, WeightedRateLimiter
from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.api.deps import current_user, require_csrf
from app.core.config import Network, settings
from app.core.crypto import crypto
from app.core.security import hash_ip, normalize_address
from app.db.redis import redis_client
from app.db.session import get_db
from app.engine.sizing import EXCHANGE_MIN_NOTIONAL
from app.models.entities import AIProfitExitDecision, CopyJob, CopyState, CredentialStatus, Execution, ExecutionState, JobState, MasterEvent, PositionLedger, RiskHalt, RiskProfile, RiskState, SigningCredential, TradingAccount, User
from app.models.entities import RISExSigningCredential, RISExTradingAccount
from app.schemas.trading import ClosePositionsIn
from app.schemas.user import RiskProfileIn, TradingAccountIn, TradingNetworkIn, TradingProviderIn
from app.schemas.user import RISExTradingAccountIn
from app.services.audit import audit
from app.services.destination_switch import DestinationSwitchBlocked, destination_switch_blockers
from app.services.entitlement import entitlement
from app.services.execution import live_trading_allowed
from app.services.execution_reason import execution_reason_code, execution_reason_detail
from app.services.execution_destination import close_user_destination_epoch, set_user_destination, user_destination_state
from app.services.master_source_identity import (
    MASTER_SOURCE_FOLLOWER_BLOCK_REASON,
    MASTER_SOURCE_MODE,
    MASTER_SOURCE_NETWORK,
    follower_controls_enabled,
    is_master_source_user,
)
from app.services.metrics import dashboard_for_user
from app.services.networking import set_user_network, user_network_state
from app.services.queue import publish_job
from app.services.risex_order_preparation import assert_risex_environment_allowed
from app.services.reconcile import master_snapshot, reconcile_user
from app.security.risex_authorization_session import (
    RISExAuthorizationSessionEvidence,
    collect_authorization_session_evidence,
)
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_deployment_runtime import (
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)


_RISEX_TESTNET_API_URL = 'https://api.testnet.rise.trade'
_RISEX_TESTNET_RPC_URL = 'https://testnet.riselabs.xyz'


async def _verify_risex_signer_binding(
    *,
    account_address: str,
    signer_address: str,
) -> RISExAuthorizationSessionEvidence:
    async with RISExReadOnlyHTTPTransport(base_url=_RISEX_TESTNET_API_URL) as api:
        async with RISExReadOnlyRPCTransport(rpc_url=_RISEX_TESTNET_RPC_URL) as rpc:
            deployment = await collect_runtime_deployment_evidence(
                api,
                rpc,
                network='testnet',
            )
            preflight = evaluate_pinned_deployment_preflight(
                deployment,
                expected_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
            )
            if (
                preflight.verdict != 'PASS'
                or preflight.deployment_identity_verified is not True
            ):
                raise RuntimeError('RISEx deployment identity is not verified')

            evidence = await collect_authorization_session_evidence(
                rpc,
                authorization_address=deployment.domain_verifying_contract,
                account=account_address,
                signer=signer_address,
                block_tag=hex(deployment.block_number),
            )

    if evidence.account.lower() != account_address.lower():
        raise RuntimeError('RISEx authorization account binding mismatch')
    if evidence.signer.lower() != signer_address.lower():
        raise RuntimeError('RISEx authorization signer binding mismatch')
    if evidence.session_active is not True:
        raise RuntimeError('RISEx signer session is not active')
    if evidence.session_not_expired is not True:
        raise RuntimeError('RISEx signer session is expired')
    if evidence.perps_permission is not True:
        raise RuntimeError('RISEx signer lacks required Perps permission')
    return evidence


@dataclass(frozen=True, slots=True)
class _RISExCredentialBinding:
    account_id: uuid.UUID
    credential_id: uuid.UUID
    account_address: str
    signer_address: str
    generation: int
    status: CredentialStatus
    expires_at: datetime | None


async def _risex_credential_binding(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> _RISExCredentialBinding | None:
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
            RISExSigningCredential.risex_trading_account_id == RISExTradingAccount.id,
        )
        .where(RISExTradingAccount.user_id == user_id)
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).mappings().one_or_none()
    if row is None:
        return None
    return _RISExCredentialBinding(
        account_id=row['account_id'],
        credential_id=row['credential_id'],
        account_address=str(row['account_address']),
        signer_address=str(row['signer_address']),
        generation=int(row['generation']),
        status=row['status'],
        expires_at=row['expires_at'],
    )


def _require_usable_risex_credential(binding: _RISExCredentialBinding) -> None:
    if binding.status not in {CredentialStatus.ACTIVE, CredentialStatus.EXPIRING}:
        raise HTTPException(409, 'RISEx credential is not active')
    if binding.expires_at is None or binding.expires_at <= datetime.now(UTC):
        raise HTTPException(409, 'RISEx credential is expired or has no verifiable expiry')


def _same_risex_binding(
    before: _RISExCredentialBinding,
    after: _RISExCredentialBinding,
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


router = APIRouter(tags=['user'])


def _limiter() -> WeightedRateLimiter:
    return WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))


def _follower_hl(network: Network) -> HyperliquidAdapter:
    return HyperliquidAdapter(_limiter(), network=network)


def _master_hl() -> HyperliquidAdapter:
    return HyperliquidAdapter(_limiter(), network=settings.master_network)


def _require_follower_user(user: User) -> None:
    if is_master_source_user(user):
        raise HTTPException(409, MASTER_SOURCE_FOLLOWER_BLOCK_REASON)


def _network_switch_blockers(*, copy_state: str, has_open_managed: bool, has_pending_jobs: bool, has_unresolved_execution: bool) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    if copy_state != CopyState.PAUSED.value:
        blockers.append({'code': 'pause', 'message': 'Metti la strategia in PAUSA.'})
    if has_open_managed:
        blockers.append({'code': 'positions', 'message': 'Chiudi tutte le posizioni gestite da TRAXION.'})
    if has_pending_jobs:
        blockers.append({'code': 'jobs', 'message': 'Attendi che non ci siano job QUEUED, PROCESSING o RETRYING.'})
    if has_unresolved_execution:
        blockers.append({'code': 'executions', 'message': 'Attendi la risoluzione delle esecuzioni SUBMITTING o UNKNOWN.'})
    return blockers


async def _network_switch_status(db: AsyncSession, user: User, started_at: datetime) -> dict:
    open_managed = (await db.execute(select(PositionLedger.id).where(
        PositionLedger.user_id == user.id,
        PositionLedger.managed.is_(True),
        PositionLedger.size != 0,
    ).limit(1))).scalar_one_or_none()
    pending = (await db.execute(select(CopyJob.id).where(
        CopyJob.user_id == user.id,
        CopyJob.created_at >= started_at,
        CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
    ).limit(1))).scalar_one_or_none()
    unresolved = (await db.execute(select(Execution.id).where(
        Execution.user_id == user.id,
        Execution.created_at >= started_at,
        Execution.state.in_([ExecutionState.SUBMITTING, ExecutionState.UNKNOWN]),
    ).limit(1))).scalar_one_or_none()
    blockers = _network_switch_blockers(
        copy_state=user.copy_state.value,
        has_open_managed=bool(open_managed),
        has_pending_jobs=bool(pending),
        has_unresolved_execution=bool(unresolved),
    )
    return {'ready': not blockers, 'blockers': blockers}


async def _serialize_user(db: AsyncSession, user: User) -> dict:
    network_state = await user_network_state(db, user.id)
    destination = await user_destination_state(db, user.id)
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    cred = None
    if account:
        cred = (await db.execute(select(SigningCredential).where(SigningCredential.trading_account_id == account.id))).scalar_one_or_none()
    rs = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    switch_status = await _network_switch_status(db, user, network_state.started_at)
    local_destination_blockers = await destination_switch_blockers(db, user.id, destination.epoch_id)
    is_master = is_master_source_user(user)
    destination_blocker_payload = (
        [{'code': 'master_source', 'message': MASTER_SOURCE_FOLLOWER_BLOCK_REASON}]
        if is_master
        else [{'code': blocker.code, 'message': blocker.message} for blocker in local_destination_blockers]
    )
    return {
        'id': str(user.id), 'auth_wallet': user.auth_wallet, 'role': user.role.value, 'state': user.state.value,
        'copy_state': user.copy_state.value, 'manual_trade_policy': user.manual_trade_policy.value,
        'display_name': user.display_name, 'email': user.email, 'shadow_started_at': user.shadow_started_at,
        'risk_state': rs.state.value if rs else RiskHalt.NORMAL.value,
        'master_network': settings.master_network,
        'follower_network': network_state.network,
        'network_started_at': network_state.started_at,
        'execution_provider': destination.provider,
        'execution_network': destination.network,
        'destination_switch_ready': False if is_master else not local_destination_blockers,
        'destination_switch_blockers': destination_blocker_payload,
        'network_switch_ready': False if is_master else switch_status['ready'],
        'network_switch_blockers': ([{'code': 'master_source', 'message': MASTER_SOURCE_FOLLOWER_BLOCK_REASON}] if is_master else switch_status['blockers']),
        'is_master_source': is_master,
        'operational_mode': MASTER_SOURCE_MODE if is_master else 'FOLLOWER',
        'operational_network': MASTER_SOURCE_NETWORK if is_master else network_state.network,
        'follower_controls_enabled': follower_controls_enabled(user),
        'trading_account': None if not account else {
            'account_address': account.account_address,
            'network': network_state.network,
            'agent_address': account.agent_address,
            'agent_name': account.agent_name,
            'verified_at': account.verified_at,
            'credential_status': cred.status.value if cred else None,
            'expires_at': cred.expires_at if cred else None,
        },
    }


@router.get('/me')
async def me(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await _serialize_user(db, user)


@router.put('/trading-network', dependencies=[Depends(require_csrf)])
async def trading_network(body: TradingNetworkIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    current = await user_network_state(db, user.id)
    network: Network = body.network
    if network == current.network:
        return await _serialize_user(db, user)

    status = await _network_switch_status(db, user, current.started_at)
    if not status['ready']:
        instructions = ' '.join(item['message'] for item in status['blockers'])
        raise HTTPException(409, f'Rete non ancora pronta al cambio. {instructions}')

    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    removed_agent = bool(account)
    risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()

    # The central destination boundary must authorize the old destination while
    # its account/credential, ledger and risk evidence are still present.
    try:
        next_state = await set_user_network(db, user.id, network)
    except DestinationSwitchBlocked as exc:
        messages = [blocker.message for blocker in exc.assessment.blockers]
        detail = ' '.join(messages) if messages else 'Impossibile verificare in sicurezza la destinazione corrente.'
        raise HTTPException(409, f'Rete non ancora pronta al cambio. {detail}') from exc

    # Destructive local cleanup happens only after the authoritative transition
    # check succeeds. The entire endpoint remains one DB transaction, so any
    # later failure rolls the epoch transition and cleanup back together.
    if account:
        await db.delete(account)
        await db.flush()

    await db.execute(delete(PositionLedger).where(PositionLedger.user_id == user.id))
    if risk_state:
        await db.delete(risk_state)

    await audit(
        db,
        action='TRADING_NETWORK_CHANGED',
        actor_id=user.id,
        subject_id=user.id,
        before={'network': current.network, 'started_at': current.started_at.isoformat()},
        after={
            'network': next_state.network,
            'started_at': next_state.started_at.isoformat(),
            'previous_api_wallet_removed': removed_agent,
        },
    )
    await db.commit()
    return await _serialize_user(db, user)


@router.put('/trading-provider', dependencies=[Depends(require_csrf)])
async def trading_provider(body: TradingProviderIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    try:
        current = await user_destination_state(db, user.id)
        current_provider = current.provider
        current_network = current.network
        current_epoch_id = current.epoch_id
    except RuntimeError as exc:
        if str(exc) != 'User has no active execution destination epoch':
            raise
        current_row = (
            await db.execute(
                text(
                    'SELECT execution_provider, execution_network, active_execution_epoch_id '
                    'FROM users WHERE id = :user_id'
                ),
                {'user_id': user.id},
            )
        ).mappings().one()
        if current_row['active_execution_epoch_id'] is not None:
            raise
        current_provider = current_row['execution_provider']
        current_network = current_row['execution_network']
        current_epoch_id = None

    risex_binding: _RISExCredentialBinding | None = None
    if body.provider == 'risex':
        try:
            assert_risex_environment_allowed(
                network=current_network,
                env=os.environ,
            )
        except SignedTestnetBlocked as exc:
            raise HTTPException(409, str(exc)) from exc

        snapshot = await _risex_credential_binding(db, user.id)
        if snapshot is None:
            raise HTTPException(409, 'Connect and verify a RISEx credential before selecting RISEx')
        _require_usable_risex_credential(snapshot)

        try:
            await _verify_risex_signer_binding(
                account_address=snapshot.account_address,
                signer_address=snapshot.signer_address,
            )
        except Exception as exc:
            raise HTTPException(
                409,
                'RISEx account/signer authorization could not be verified',
            ) from exc

        # Do not hold the user row lock across the live provider read above.
        # From this point through destination creation, serialize credential
        # rotation and provider switching, then force a fresh locked reread.
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        locked_binding = await _risex_credential_binding(
            db,
            user.id,
            for_update=True,
        )
        if locked_binding is None or not _same_risex_binding(snapshot, locked_binding):
            raise HTTPException(
                409,
                'RISEx credential changed during verification; retry with the current binding',
            )
        _require_usable_risex_credential(locked_binding)
        risex_binding = locked_binding

        if current_provider == 'risex':
            if current_epoch_id is None:
                raise HTTPException(409, 'RISEx destination has no active execution epoch')
            epoch_binding = (
                await db.execute(
                    text(
                        'SELECT account_address, credential_version '
                        'FROM execution_epochs WHERE id = :epoch_id AND ended_at IS NULL'
                    ),
                    {'epoch_id': current_epoch_id},
                )
            ).mappings().one_or_none()
            if (
                epoch_binding is None
                or not epoch_binding['account_address']
                or str(epoch_binding['account_address']).lower()
                != locked_binding.account_address.lower()
                or epoch_binding['credential_version'] != locked_binding.generation
            ):
                raise HTTPException(
                    409,
                    'Active RISEx epoch does not match the verified current credential',
                )
            return await _serialize_user(db, user)

    if body.provider == current_provider:
        return await _serialize_user(db, user)

    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    removed_api_wallet = current_provider == 'hyperliquid' and account is not None

    try:
        next_destination = await set_user_destination(
            db,
            user.id,
            provider=body.provider,
            network=current_network,
            account_address=risex_binding.account_address if risex_binding else None,
            credential_version=risex_binding.generation if risex_binding else None,
        )
    except DestinationSwitchBlocked as exc:
        blockers = [
            {'code': blocker.code, 'message': blocker.message}
            for blocker in exc.assessment.blockers
        ]
        if not blockers:
            blockers = [{
                'code': 'destination_unreadable',
                'message': exc.assessment.reason or 'Current execution destination cannot be verified safely.',
            }]
        raise HTTPException(
            409,
            detail={
                'code': 'destination_switch_blocked',
                'state': exc.assessment.state.value,
                'reason': exc.assessment.reason,
                'blockers': blockers,
            },
        ) from exc

    if current_provider == 'hyperliquid' and account:
        await db.delete(account)
        await db.flush()
    await db.execute(delete(PositionLedger).where(PositionLedger.user_id == user.id))
    if risk_state:
        await db.delete(risk_state)

    await audit(
        db,
        action='TRADING_PROVIDER_CHANGED',
        actor_id=user.id,
        subject_id=user.id,
        before={
            'provider': current_provider,
            'network': current_network,
            'epoch_id': str(current_epoch_id) if current_epoch_id is not None else None,
        },
        after={
            'provider': next_destination.provider,
            'network': next_destination.network,
            'epoch_id': str(next_destination.epoch_id) if next_destination.epoch_id is not None else None,
            'previous_api_wallet_removed': removed_api_wallet,
        },
    )
    await db.commit()
    return await _serialize_user(db, user)

@router.get('/dashboard')
async def dashboard(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    data = await dashboard_for_user(db, user.id); data['user'] = await _serialize_user(db, user); data['entitlements'] = await entitlement(db, user); return data


@router.get('/positions')
async def positions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    network = (await user_network_state(db, user.id)).network
    rows = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user.id).order_by(PositionLedger.asset))).scalars().all()
    risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one_or_none()
    min_notional = max(risk.min_notional if risk else EXCHANGE_MIN_NOTIONAL, EXCHANGE_MIN_NOTIONAL)
    out = []
    for r in rows:
        mark = r.mark_price or Decimal(0)
        delta = r.target_size - r.size
        delta_notional = abs(delta) * mark if mark > 0 else Decimal(0)
        if r.managed and mark <= 0:
            status = 'UNAVAILABLE'
            reason = f'Mercato non disponibile su {network.upper()}'
        elif delta == 0:
            status = 'ON_TARGET'
            reason = None
        elif delta_notional < min_notional:
            status = 'BELOW_MIN'
            reason = f'Delta ${delta_notional:.2f} sotto minimo ${min_notional:.0f}'
        else:
            status = 'READY'
            reason = None
        out.append({
            'asset': r.asset,
            'current_size': r.size,
            'target_size': r.target_size,
            'delta': delta,
            'mark_price': mark,
            'delta_notional': delta_notional,
            'status': status,
            'reason': reason,
            'managed': r.managed,
            'master_leverage': r.master_leverage,
            'master_is_cross': r.master_is_cross,
            'follower_leverage': r.follower_leverage,
            'follower_is_cross': r.follower_is_cross,
            'exchange_verified_at': r.exchange_verified_at,
        })
    return out


@router.get('/executions')
async def executions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db), limit: int = 50, offset: int = 0, state: str | None = None, asset: str | None = None):
    network_state = await user_network_state(db, user.id)
    q = (
        select(Execution, CopyJob, MasterEvent)
        .join(CopyJob, CopyJob.id == Execution.copy_job_id)
        .outerjoin(MasterEvent, MasterEvent.id == CopyJob.master_event_id)
        .where(
            Execution.user_id == user.id,
            CopyJob.created_at >= network_state.started_at,
        )
    )
    if state: q = q.where(Execution.state == state)
    if asset: q = q.where(Execution.asset == asset)
    rows = (await db.execute(q.order_by(Execution.created_at.desc()).offset(offset).limit(min(limit, 200)))).all()

    ai_job_ids = [
        job.id
        for _execution, job, _master_event in rows
        if str(job.origin or '').upper() == 'AI_PROFIT_EXIT'
    ]
    ai_reasons: dict[uuid.UUID, str] = {}
    if ai_job_ids:
        decision_rows = (await db.execute(
            select(
                AIProfitExitDecision.copy_job_id,
                AIProfitExitDecision.decision_reason,
                AIProfitExitDecision.decided_at,
            )
            .where(AIProfitExitDecision.copy_job_id.in_(ai_job_ids))
            .order_by(AIProfitExitDecision.decided_at.desc())
        )).all()
        for job_id, decision_reason, _decided_at in decision_rows:
            if job_id is not None and job_id not in ai_reasons:
                ai_reasons[job_id] = decision_reason

    out = []
    for execution, job, master_event in rows:
        ctx = job.context or {}
        leverage = ctx.get('desired_follower_leverage', ctx.get('master_leverage'))
        is_cross = ctx.get('desired_follower_is_cross')
        if is_cross is None:
            is_cross = ctx.get('master_is_cross')
        reason_code = execution_reason_code(execution, job, master_event)
        reason_detail = execution_reason_detail(
            execution,
            ai_decision_reason=ai_reasons.get(job.id),
        )
        out.append({
            'id': str(execution.id),
            'asset': execution.asset,
            'state': execution.state.value,
            'is_buy': execution.is_buy,
            'requested_size': execution.requested_size,
            'filled_size': execution.filled_size,
            'avg_price': execution.avg_price,
            'reduce_only': execution.reduce_only,
            'reject_reason': execution.reject_reason,
            'reason_code': reason_code,
            'reason_detail': reason_detail,
            'origin': job.origin,
            'cloid': execution.cloid,
            'leverage': leverage,
            'is_cross': is_cross,
            'network': ctx.get('follower_network', network_state.network),
            'created_at': execution.created_at,
        })
    return out


@router.post('/trading-account', dependencies=[Depends(require_csrf)])
async def link_trading_account(body: TradingAccountIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    network = (await user_network_state(db, user.id)).network
    if network == 'mainnet' and settings.APP_ENV == 'production' and settings.KEK_PROVIDER == 'env':
        raise HTTPException(409, 'La produzione MAINNET richiede il provider KMS esterno prima di salvare una credenziale operativa')

    account_address = normalize_address(user.auth_wallet)
    master_address = normalize_address(settings.HYPERLIQUID_MASTER_ADDRESS) if settings.HYPERLIQUID_MASTER_ADDRESS else ''
    same_principal = (
        settings.master_network == network
        and master_address
        and account_address == master_address
    )
    if same_principal:
        raise HTTPException(422, 'The master Hyperliquid account cannot also be a follower on the same network')

    expected_agent_address = None
    if body.agent_address:
        try:
            expected_agent_address = normalize_address(body.agent_address)
        except Exception as exc:
            raise HTTPException(422, 'Invalid API Wallet address') from exc

    try:
        verification = await _follower_hl(network).verify_agent(
            account_address,
            body.agent_private_key,
            expected_agent_address=expected_agent_address,
        )
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc

    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if account and account.account_address.lower() != account_address.lower():
        open_managed = (await db.execute(select(PositionLedger.id).where(
            PositionLedger.user_id == user.id, PositionLedger.managed.is_(True), PositionLedger.size != 0
        ).limit(1))).scalar_one_or_none()
        if open_managed:
            raise HTTPException(409, 'Close all TRAXION-managed positions before changing the Hyperliquid account')
    if not account:
        account = TradingAccount(user_id=user.id, account_address=account_address, agent_address=verification.agent_address, agent_name=verification.name)
        db.add(account); await db.flush()
    else:
        account.account_address, account.agent_address, account.agent_name, account.verified_at = account_address, verification.agent_address, verification.name, datetime.now(UTC)
        old = (await db.execute(select(SigningCredential).where(SigningCredential.trading_account_id == account.id))).scalar_one_or_none()
        if old: await db.delete(old); await db.flush()
    blob = crypto.encrypt(body.agent_private_key, user_id=str(user.id), account_id=str(account.id))
    expires = datetime.fromtimestamp(verification.valid_until/1000, UTC) if verification.valid_until else None
    db.add(SigningCredential(trading_account_id=account.id, ciphertext_b64=blob.ciphertext_b64, nonce_b64=blob.nonce_b64, wrapped_dek_b64=blob.wrapped_dek_b64, wrap_nonce_b64=blob.wrap_nonce_b64, key_provider=blob.key_provider, key_reference=blob.key_reference, key_version=blob.key_version, agent_fingerprint=hashlib.sha256(verification.agent_address.encode()).hexdigest(), expires_at=expires, status=CredentialStatus.ACTIVE))
    await close_user_destination_epoch(db, user.id)
    await set_user_destination(
        db,
        user.id,
        provider='hyperliquid',
        network=network,
        account_address=account_address,
        credential_version=blob.key_version,
    )
    if settings.DEFAULT_SHADOW_MODE and user.copy_state == CopyState.PAUSED:
        user.copy_state = CopyState.SHADOW
        user.shadow_started_at = datetime.now(UTC)
    await audit(db, action='TRADING_ACCOUNT_LINKED', actor_id=user.id, subject_id=user.id, ip_hash=hash_ip(request.client.host if request.client else None), after={'account': account_address[:8]+'…', 'agent': verification.agent_address[:8]+'…', 'network': network, 'copy_state': user.copy_state.value, 'expires_at': expires.isoformat() if expires else None})
    await db.commit(); return await _serialize_user(db, user)


@router.delete('/trading-account', dependencies=[Depends(require_csrf)], status_code=204)
async def unlink_trading_account(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    open_managed = (await db.execute(select(PositionLedger.id).where(
        PositionLedger.user_id == user.id, PositionLedger.managed.is_(True), PositionLedger.size != 0
    ).limit(1))).scalar_one_or_none()
    if open_managed:
        raise HTTPException(409, 'Close all TRAXION-managed positions before removing the trading credential')
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if account: await db.delete(account)
    await close_user_destination_epoch(db, user.id)
    user.copy_state = CopyState.PAUSED
    await audit(db, action='TRADING_ACCOUNT_UNLINKED', actor_id=user.id, subject_id=user.id)
    await db.commit()


@router.get('/risk-profile')
async def get_risk(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one(); return {c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in {'id','user_id','created_at','updated_at'}}


@router.put('/risk-profile', dependencies=[Depends(require_csrf)])
async def put_risk(body: RiskProfileIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    row = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one()
    values = body.model_dump()
    before = {
        k: str(getattr(row, k)) if hasattr(getattr(row, k), 'as_tuple') else getattr(row, k)
        for k in values
    }
    for k, v in values.items():
        setattr(row, k, v)
    await audit(
        db,
        action='RISK_PROFILE_UPDATED',
        actor_id=user.id,
        subject_id=user.id,
        reason='Selected risk profile persisted; entitlement caps resolve at runtime',
        before=before,
        after={k: str(v) if hasattr(v, 'as_tuple') else v for k, v in values.items()},
    )
    await db.commit(); return await get_risk(user, db)


@router.post('/copy/pause', dependencies=[Depends(require_csrf)])
async def pause(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    user.copy_state = CopyState.PAUSED; await audit(db, action='COPY_PAUSED', actor_id=user.id, subject_id=user.id); await db.commit(); return await _serialize_user(db, user)


@router.post('/copy/shadow', dependencies=[Depends(require_csrf)])
async def shadow(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not account:
        raise HTTPException(409, 'Connect a Hyperliquid trading account first')
    network = (await user_network_state(db, user.id)).network
    user.copy_state = CopyState.SHADOW
    user.shadow_started_at = datetime.now(UTC)
    await audit(db, action='COPY_SHADOW_ENABLED', actor_id=user.id, subject_id=user.id, after={'master_network': settings.master_network, 'follower_network': network})
    await db.commit()
    return await _serialize_user(db, user)


@router.post('/copy/resume', dependencies=[Depends(require_csrf)])
async def resume(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    rs = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
    if rs and rs.state != RiskHalt.NORMAL: raise HTTPException(409, f'Cannot resume while {rs.state.value} is active')
    account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
    if not account: raise HTTPException(409, 'Connect a Hyperliquid trading account first')
    network = (await user_network_state(db, user.id)).network
    if not await live_trading_allowed(db, network):
        raise HTTPException(409, 'Mainnet live-trading gate is closed')
    try:
        master_hl = _master_hl()
        follower_hl = _follower_hl(network)
        mp, meq, master_mids = await master_snapshot(master_hl)
        follower_mids = master_mids if settings.master_network == network else await follower_hl.mids()
        await reconcile_user(
            db, follower_hl, user,
            master_positions=mp, master_equity=meq,
            mids=follower_mids, master_mids=master_mids,
        )
    except Exception as exc:
        raise HTTPException(503, 'Reconciliation must succeed before strategy execution can resume') from exc
    user.copy_state = CopyState.ACTIVE
    await audit(db, action='COPY_RESUMED', actor_id=user.id, subject_id=user.id, after={'master_network': settings.master_network, 'follower_network': network})
    await db.commit()
    return await _serialize_user(db, user)


@router.post('/copy/close-positions', dependencies=[Depends(require_csrf)])
async def close_positions(body: ClosePositionsIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    _require_follower_user(user)
    network_state = await user_network_state(db, user.id)
    network = network_state.network
    rows = (await db.execute(select(PositionLedger).where(
        PositionLedger.user_id == user.id, PositionLedger.managed.is_(True), PositionLedger.size != 0
    ))).scalars().all()
    pending_assets = set((await db.execute(select(CopyJob.asset).where(
        CopyJob.user_id == user.id, CopyJob.origin == 'CLOSE_ALL',
        CopyJob.created_at >= network_state.started_at,
        CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
    ))).scalars().all())
    previous_copy_state = user.copy_state.value
    user.copy_state = CopyState.PAUSED
    jobs: list[CopyJob] = []
    for row in rows:
        if row.asset in pending_assets:
            continue
        mark = row.mark_price or Decimal(0)
        job = CopyJob(
            user_id=user.id, asset=row.asset, origin='CLOSE_ALL', state=JobState.QUEUED,
            correlation_id=__import__('uuid').uuid4().hex,
            context={
                'master_position': '0', 'master_equity': '1',
                'master_mark_price': str(mark), 'mark_price': str(mark),
                'master_network': settings.master_network, 'follower_network': network,
                'explicit_close': True,
            },
        )
        db.add(job); jobs.append(job)
    await db.flush()
    await db.commit()

    deferred_enqueue = 0
    redis = redis_client()
    for job in jobs:
        try:
            await publish_job(redis, db, job)
        except Exception:
            job.enqueued_at = None
            deferred_enqueue += 1

    await audit(
        db, action='CLOSE_POSITIONS_REQUESTED', actor_id=user.id, subject_id=user.id, reason=body.reason,
        after={
            'jobs': len(jobs), 'network': network, 'copy_state_before': previous_copy_state,
            'copy_state_after': CopyState.PAUSED.value, 'deferred_enqueue': deferred_enqueue,
        },
    )
    await db.commit()
    return {'queued': len(jobs), 'paused': True, 'deferred_enqueue': deferred_enqueue}


@router.post('/risex-trading-account', dependencies=[Depends(require_csrf)])
async def link_risex_trading_account(
    body: RISExTradingAccountIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_follower_user(user)

    admission_network = (
        await db.execute(
            text('SELECT execution_network FROM users WHERE id = :user_id'),
            {'user_id': user.id},
        )
    ).scalar_one()
    try:
        assert_risex_environment_allowed(
            network=admission_network,
            env=os.environ,
        )
    except SignedTestnetBlocked as exc:
        raise HTTPException(409, str(exc)) from exc

    try:
        account_address = normalize_address(body.account_address)
        authenticated_wallet = normalize_address(user.auth_wallet)
    except Exception as exc:
        raise HTTPException(422, 'Invalid RISEx account address') from exc
    if account_address != authenticated_wallet:
        raise HTTPException(422, 'RISEx account must match the authenticated wallet')

    try:
        signer_address = Account.from_key(body.signer_private_key).address
        normalized_signer_address = normalize_address(signer_address)
    except Exception as exc:
        raise HTTPException(422, 'Invalid RISEx signer credential') from exc
    if normalized_signer_address == account_address:
        raise HTTPException(422, 'The authenticated wallet cannot also be the RISEx signer')

    try:
        verification = await _verify_risex_signer_binding(
            account_address=account_address,
            signer_address=signer_address,
        )
    except Exception as exc:
        raise HTTPException(
            422,
            'RISEx account/signer authorization could not be verified',
        ) from exc

    if (
        verification.session_active is not True
        or verification.session_not_expired is not True
        or verification.perps_permission is not True
    ):
        raise HTTPException(
            422,
            'RISEx signer authorization is inactive, expired, or lacks Perps permission',
        )

    # Validation and provider verification above are deliberately side-effect free.
    # user_network_state() may bootstrap a missing execution epoch, so call it only
    # after every 422-producing credential check has succeeded.
    network = (await user_network_state(db, user.id)).network
    if network != admission_network:
        raise HTTPException(409, 'Execution network changed during RISEx verification')
    if network != 'testnet':
        raise HTTPException(
            409,
            'RISEx credential onboarding remains testnet-only until the mainnet gate is accepted',
        )

    locked_user = (
        await db.execute(select(User).where(User.id == user.id).with_for_update())
    ).scalar_one()
    if (
        locked_user.execution_provider == 'risex'
        and locked_user.active_execution_epoch_id is not None
    ):
        rotation_blockers = await destination_switch_blockers(
            db,
            user.id,
            locked_user.active_execution_epoch_id,
        )
        if any(blocker.code == 'unresolved_executions' for blocker in rotation_blockers):
            raise HTTPException(
                409,
                'RISEx credential rotation is blocked while the active epoch has unresolved executions',
            )

    locked_network = (
        await db.execute(
            text('SELECT execution_network FROM users WHERE id = :user_id'),
            {'user_id': user.id},
        )
    ).scalar_one()
    if locked_network != admission_network:
        raise HTTPException(409, 'Execution network changed during RISEx verification')

    shared_account_owner = (
        await db.execute(
            select(RISExTradingAccount.user_id).where(
                RISExTradingAccount.account_address == account_address,
                RISExTradingAccount.user_id != user.id,
            )
        )
    ).scalar_one_or_none()
    if shared_account_owner is not None:
        raise HTTPException(409, 'RISEx account is already linked to another user')

    shared_signer_owner = (
        await db.execute(
            select(RISExTradingAccount.user_id)
            .join(
                RISExSigningCredential,
                RISExSigningCredential.risex_trading_account_id == RISExTradingAccount.id,
            )
            .where(
                RISExSigningCredential.signer_address == signer_address,
                RISExTradingAccount.user_id != user.id,
            )
        )
    ).scalar_one_or_none()
    if shared_signer_owner is not None:
        raise HTTPException(409, 'RISEx signer is already linked to another user')

    account = (
        await db.execute(
            select(RISExTradingAccount)
            .where(RISExTradingAccount.user_id == user.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        account = RISExTradingAccount(
            user_id=user.id,
            account_address=account_address,
            verified_at=datetime.now(UTC),
        )
        db.add(account)
        await db.flush()
        generation = 1
    else:
        account.account_address = account_address
        account.verified_at = datetime.now(UTC)
        previous = (
            await db.execute(
                select(RISExSigningCredential).where(
                    RISExSigningCredential.risex_trading_account_id == account.id
                )
            )
        ).scalar_one_or_none()
        generation = 1 if previous is None else previous.generation + 1
        if previous is not None:
            await db.delete(previous)
            await db.flush()

    blob = crypto.encrypt(
        body.signer_private_key,
        user_id=str(user.id),
        account_id=str(account.id),
    )
    session_expiration = getattr(verification, 'session_expiration', None)
    expires_at = (
        datetime.fromtimestamp(int(session_expiration), UTC)
        if session_expiration
        else None
    )
    db.add(
        RISExSigningCredential(
            risex_trading_account_id=account.id,
            signer_address=signer_address,
            ciphertext_b64=blob.ciphertext_b64,
            nonce_b64=blob.nonce_b64,
            wrapped_dek_b64=blob.wrapped_dek_b64,
            wrap_nonce_b64=blob.wrap_nonce_b64,
            key_provider=blob.key_provider,
            key_reference=blob.key_reference,
            key_version=blob.key_version,
            generation=generation,
            expires_at=expires_at,
            status=CredentialStatus.ACTIVE,
        )
    )
    await db.flush()

    await close_user_destination_epoch(db, user.id)
    destination = await set_user_destination(
        db,
        user.id,
        provider='risex',
        network=network,
        account_address=account_address,
        credential_version=generation,
    )

    await audit(
        db,
        action='RISEX_TRADING_ACCOUNT_LINKED',
        actor_id=user.id,
        subject_id=user.id,
        ip_hash=hash_ip(request.client.host if request.client else None),
        after={
            'account': account_address[:8] + '…',
            'signer': signer_address[:8] + '…',
            'network': network,
            'credential_generation': generation,
            'execution_epoch_id': str(destination.epoch_id),
            'expires_at': expires_at.isoformat() if expires_at else None,
        },
    )
    await db.commit()
    return await _serialize_user(db, user)
