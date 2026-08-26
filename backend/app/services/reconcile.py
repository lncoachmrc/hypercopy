from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, PositionConfig, fill_event_id, position_configs
from app.adapters.ratelimit import Priority
from app.core.config import settings
from app.db.position_ledger_lock import position_ledger_lock
from app.engine.sizing import EXCHANGE_MIN_NOTIONAL, FollowerState, MasterExposure, compute_target
from app.models.entities import CopyJob, CopyState, EquitySnapshot, Execution, ExecutionState, Fill, JobState, PositionLedger, ReconciliationRun, RiskHalt, RiskProfile, RiskState, TradingAccount, User, UserState
from app.services.ai_mode import read_ai_execution_policy
from app.services.audit import audit
from app.services.networking import user_network_state

log = __import__('app.core.logging', fromlist=['get_logger']).get_logger(__name__)

_LIQUIDITY_REJECT_MARKERS = (
    'could not immediately match against any resting orders',
    'no liquidity',
    'marketordernoliquidityrejected',
    'ioccancelrejected',
)

_ACTIVE_AMBIGUITY_JOB_STATES = {JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING}
_TERMINAL_AMBIGUITY_JOB_STATES = {JobState.DONE, JobState.SKIPPED, JobState.DEAD}


def _positions(state: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in state.get('assetPositions', []):
        p = row.get('position', row)
        out[str(p.get('coin'))] = Decimal(str(p.get('szi', '0')))
    return out


def _reconciliation_basis(
    copy_state: CopyState,
    previous_target: Decimal | None,
    real: Decimal,
) -> Decimal:
    if copy_state == CopyState.SHADOW:
        return previous_target if previous_target is not None else Decimal(0)
    return real


def _is_liquidity_reject(reason: str | None) -> bool:
    text = (reason or '').lower()
    return any(marker in text for marker in _LIQUIDITY_REJECT_MARKERS)


def _safe_ambiguity_reduction(real: Decimal, desired_target: Decimal) -> bool:
    """Only a same-side reduction or full close is safe while outcome is unknown."""
    if real == 0 or abs(desired_target) >= abs(real):
        return False
    return desired_target == 0 or real * desired_target > 0


async def _liquidity_backoff_seconds(db: AsyncSession, user_id, asset: str, started_at: datetime) -> int:
    rows = (await db.execute(
        select(Execution).where(
            Execution.user_id == user_id,
            Execution.asset == asset,
            Execution.created_at >= started_at,
            Execution.state.in_([ExecutionState.REJECTED, ExecutionState.CANCELED, ExecutionState.FILLED]),
        ).order_by(Execution.created_at.desc()).limit(8)
    )).scalars().all()
    if not rows or rows[0].state == ExecutionState.FILLED:
        return 0

    consecutive = 0
    latest_at = None
    for execution in rows:
        if execution.state not in {ExecutionState.REJECTED, ExecutionState.CANCELED} or not _is_liquidity_reject(execution.reject_reason):
            break
        if latest_at is None:
            latest_at = execution.created_at
        consecutive += 1

    if not latest_at or consecutive == 0:
        return 0
    delay_seconds = min(60 * (2 ** (consecutive - 1)), 600)
    remaining = (latest_at + timedelta(seconds=delay_seconds) - datetime.now(UTC)).total_seconds()
    return max(int(remaining), 0)


async def _terminal_action_rejection_blocks_unchanged_intent(
    db: AsyncSession,
    user_id,
    asset: str,
    started_at: datetime,
    *,
    network: str,
    previous_target: Decimal | None,
    previous_real: Decimal,
    desired_target: Decimal,
    real: Decimal,
) -> bool:
    """Honor retry_policy=NONE only while the rejected intent is unchanged.

    The execution path writes the planned target into PositionLedger before the
    durable SUBMITTING commit. Therefore `previous_target` and `previous_real`
    are the target and local exchange basis that existed when the latest action
    rejection was persisted. A changed target or changed authoritative exchange
    position releases the fence and lets reconciliation create a fresh job/CLOID.
    """

    latest = (await db.execute(
        select(CopyJob).where(
            CopyJob.user_id == user_id,
            CopyJob.asset == asset,
            CopyJob.origin.in_(['EVENT', 'RECONCILE']),
            CopyJob.created_at >= started_at,
        ).order_by(CopyJob.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if latest is None or latest.state != JobState.SKIPPED:
        return False

    metadata = (latest.context or {}).get('last_action_error') or {}
    if metadata.get('retry_policy') != 'NONE':
        return False
    if str(metadata.get('network') or '') != str(network):
        return False
    return previous_target == desired_target and previous_real == real


async def _sync_missing_fills(db: AsyncSession, hl: HyperliquidAdapter, user: User, account_address: str, started_at: datetime) -> int:
    missing = (await db.execute(
        select(Execution).where(
            Execution.user_id == user.id,
            Execution.created_at >= started_at,
            Execution.state == ExecutionState.FILLED,
            Execution.exchange_oid.is_not(None),
            ~select(Fill.id).where(Fill.execution_id == Execution.id).exists(),
        ).order_by(Execution.created_at).limit(100)
    )).scalars().all()
    if not missing:
        return 0
    start_ms = max(int(min(x.created_at for x in missing).timestamp() * 1000) - 60_000, 0)
    exchange_fills = await hl.user_fills_by_time(account_address, start_ms)
    by_oid: dict[str, list[dict]] = {}
    for fill in exchange_fills:
        oid = str(fill.get('oid', ''))
        if oid:
            by_oid.setdefault(oid, []).append(fill)
    inserted = 0
    for execution in missing:
        for fill in by_oid.get(str(execution.exchange_oid), []):
            ts_ms = int(fill.get('time') or int(execution.created_at.timestamp() * 1000))
            stmt = insert(Fill).values(
                exchange_fill_id=f'{hl.network}:{fill_event_id(fill)}', execution_id=execution.id,
                user_id=user.id, asset=str(fill.get('coin') or execution.asset),
                size=Decimal(str(fill.get('sz', '0'))), price=Decimal(str(fill.get('px', '0'))),
                side=str(fill.get('side') or fill.get('dir') or '')[:8],
                closed_pnl=Decimal(str(fill.get('closedPnl', '0'))) if fill.get('closedPnl') not in (None, '') else None,
                fee=Decimal(str(fill.get('fee', '0'))) if fill.get('fee') not in (None, '') else None,
                ts=datetime.fromtimestamp(ts_ms / 1000, UTC),
            ).on_conflict_do_nothing(index_elements=['exchange_fill_id'])
            result = await db.execute(stmt)
            inserted += int(getattr(result, 'rowcount', 0) or 0)
    return inserted


async def master_snapshot(hl: HyperliquidAdapter) -> tuple[dict[str, Decimal], Decimal, dict[str, str]]:
    snapshot = await hl.account_snapshot(settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.MASTER_STATE)
    mids = await hl.mids()
    return _positions(snapshot.perp_state), snapshot.account_value, mids


async def reconcile_user(
    db: AsyncSession,
    hl: HyperliquidAdapter,
    user: User,
    *,
    master_positions: dict[str, Decimal],
    master_equity: Decimal,
    mids: dict[str, str],
    master_mids: dict[str, str] | None = None,
    master_configs: dict[str, PositionConfig] | None = None,
    create_jobs: bool = True,
) -> dict:
    async with position_ledger_lock(user.id):
        return await _reconcile_user_locked(
            db,
            hl,
            user,
            master_positions=master_positions,
            master_equity=master_equity,
            mids=mids,
            master_mids=master_mids,
            master_configs=master_configs,
            create_jobs=create_jobs,
        )


async def _reconcile_user_locked(
    db: AsyncSession,
    hl: HyperliquidAdapter,
    user: User,
    *,
    master_positions: dict[str, Decimal],
    master_equity: Decimal,
    mids: dict[str, str],
    master_mids: dict[str, str] | None = None,
    master_configs: dict[str, PositionConfig] | None = None,
    create_jobs: bool = True,
) -> dict:
    network_state = await user_network_state(db, user.id)
    network = network_state.network
    if hl.network != network:
        raise RuntimeError(f'Follower adapter {hl.network} does not match user network {network}')

    follower_mids = mids
    source_mids = master_mids or mids
    source_configs = master_configs or {}
    run = ReconciliationRun(user_id=user.id, before={}, after={})
    db.add(run); await db.flush()
    jobs_created = 0
    try:
        account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
        if not account:
            run.status = 'SKIPPED'; run.finished_at = datetime.now(UTC); await db.commit(); return {'status': 'SKIPPED', 'jobs_created': 0}

        snapshot = await hl.account_snapshot(account.account_address)
        real_state = snapshot.perp_state
        equity = snapshot.account_value
        free_margin = snapshot.free_margin
        account_mode = snapshot.abstraction
        follower_configs = position_configs(real_state)

        try:
            synced_fills = await _sync_missing_fills(db, hl, user, account.account_address, network_state.started_at)
        except Exception:
            synced_fills = 0
            log.warning('Deferred fill-history synchronization', extra={'user_id': str(user.id), 'network': network}, exc_info=True)

        real_positions = _positions(real_state)
        risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
        if not risk_state:
            risk_state = RiskState(user_id=user.id, peak_equity=equity, day_start_equity=equity, day_key=datetime.now(UTC).date().isoformat())
            db.add(risk_state)
        risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one_or_none()
        ai_policy = await read_ai_execution_policy(db)
        ai_factor = ai_policy.factor

        distances = []
        for row in real_state.get('assetPositions', []):
            pos = row.get('position', row)
            asset_name = str(pos.get('coin') or '')
            liq_raw = pos.get('liquidationPx')
            mark_raw = follower_mids.get(asset_name)
            try:
                if liq_raw not in (None, '', '0') and mark_raw not in (None, '', '0'):
                    liq = Decimal(str(liq_raw)); mark_px = Decimal(str(mark_raw))
                    if mark_px > 0:
                        distances.append(abs(mark_px - liq) / mark_px * Decimal(100))
            except Exception:
                continue
        min_liq_distance = min(distances) if distances else None
        risk_state.liquidation_distance_pct = min_liq_distance
        risk_state.near_liquidation = bool(min_liq_distance is not None and min_liq_distance < Decimal('15'))

        if risk:
            risk_state.peak_equity = max(risk_state.peak_equity or equity, equity)
            dd = ((risk_state.peak_equity - equity) / risk_state.peak_equity * 100) if risk_state.peak_equity and risk_state.peak_equity > 0 else Decimal(0)
            if dd >= risk.max_drawdown_pct:
                risk_state.state = RiskHalt.DRAWDOWN_HALT
                risk_state.reason = f'Drawdown {dd:.2f}% >= {risk.max_drawdown_pct}%'
            today = datetime.now(UTC).date().isoformat()
            if risk_state.day_key != today:
                risk_state.day_key, risk_state.day_start_equity = today, equity
            daily_loss = ((risk_state.day_start_equity - equity) / risk_state.day_start_equity * 100) if risk_state.day_start_equity and risk_state.day_start_equity > 0 else Decimal(0)
            if daily_loss >= risk.max_daily_loss_pct:
                risk_state.state = RiskHalt.DAILY_LOSS_HALT
                risk_state.reason = f'Daily loss {daily_loss:.2f}% >= {risk.max_daily_loss_pct}%'

        ledger_rows = (await db.execute(select(PositionLedger).where(PositionLedger.user_id == user.id))).scalars().all()
        ledger_by_asset = {x.asset: x for x in ledger_rows}

        unmanaged_margin = Decimal(0)
        for row in real_state.get('assetPositions', []):
            pos = row.get('position', row)
            asset_name = str(pos.get('coin') or '')
            ledger = ledger_by_asset.get(asset_name)
            if ledger is not None and not ledger.managed:
                try:
                    unmanaged_margin += abs(Decimal(str(pos.get('marginUsed', '0') or '0')))
                except Exception:
                    pass

        unresolved_rows = (await db.execute(
            select(Execution.asset, CopyJob.state)
            .join(CopyJob, CopyJob.id == Execution.copy_job_id)
            .where(
                Execution.user_id == user.id,
                Execution.created_at >= network_state.started_at,
                Execution.state.in_([ExecutionState.SUBMITTING, ExecutionState.UNKNOWN]),
            )
        )).all()
        live_ambiguity_assets = {
            asset for asset, job_state in unresolved_rows
            if job_state in _ACTIVE_AMBIGUITY_JOB_STATES
        }
        terminal_ambiguity_assets = {
            asset for asset, job_state in unresolved_rows
            if job_state in _TERMINAL_AMBIGUITY_JOB_STATES
        }

        assets = set(master_positions) | set(real_positions) | set(ledger_by_asset)
        discrepancies = []
        liquidity_backoffs = []
        multiplier = risk.multiplier if risk else Decimal(1)
        min_notional = max(risk.min_notional if risk else EXCHANGE_MIN_NOTIONAL, EXCHANGE_MIN_NOTIONAL)

        for asset in assets:
            real = real_positions.get(asset, Decimal(0))
            ledger = ledger_by_asset.get(asset)
            follower_mark = Decimal(str(follower_mids.get(asset, '0') or '0'))
            if not ledger:
                ledger = PositionLedger(user_id=user.id, asset=asset, size=real, target_size=Decimal(0), mark_price=follower_mark, managed=asset in master_positions, exchange_verified_at=datetime.now(UTC))
                db.add(ledger); ledger_by_asset[asset] = ledger
            if asset in master_positions and master_positions.get(asset, Decimal(0)) != 0:
                ledger.managed = True

            before = ledger.size
            previous_target = ledger.target_size
            ledger.size = real
            ledger.mark_price = follower_mark
            ledger.exchange_verified_at = datetime.now(UTC)
            if before != real:
                discrepancies.append({'asset': asset, 'ledger': str(before), 'real': str(real)})

            master_pos = master_positions.get(asset, Decimal(0))
            master_mark = Decimal(str(source_mids.get(asset, '0') or '0'))
            base_target = Decimal(0)
            desired_target = Decimal(0)
            if master_pos != 0 and master_equity > 0 and master_mark > 0 and follower_mark > 0:
                base_target = compute_target(
                    MasterExposure(asset, master_pos, master_mark, master_equity),
                    FollowerState(str(user.id), equity, unmanaged_margin, real, multiplier),
                    follower_mark,
                )
                desired_target = base_target * ai_factor
            ledger.target_size = desired_target

            master_config = source_configs.get(asset)
            follower_config = follower_configs.get(asset)
            ledger.master_leverage = master_config.leverage if master_pos != 0 and master_config else None
            ledger.master_is_cross = master_config.is_cross if master_pos != 0 and master_config else None
            ledger.follower_leverage = follower_config.leverage if real != 0 and follower_config else None
            ledger.follower_is_cross = follower_config.is_cross if real != 0 and follower_config else None

            if not create_jobs or user.copy_state == CopyState.PAUSED:
                continue

            has_live_ambiguity = asset in live_ambiguity_assets
            has_terminal_ambiguity = asset in terminal_ambiguity_assets
            ambiguity_safe_reduction = (
                not has_live_ambiguity
                and has_terminal_ambiguity
                and _safe_ambiguity_reduction(real, desired_target)
            )
            if has_live_ambiguity or (has_terminal_ambiguity and not ambiguity_safe_reduction):
                continue

            allowed_asset = bool(risk) and (not risk.allow_assets or asset in risk.allow_assets) and asset not in risk.block_assets
            desired_leverage = None
            desired_is_cross = None
            leverage_mismatch = False
            if user.copy_state == CopyState.ACTIVE and master_pos != 0 and master_config and risk and allowed_asset:
                try:
                    spec = await hl.asset_spec(asset)
                    desired_leverage = max(1, min(master_config.leverage, int(risk.max_leverage), spec.max_leverage))
                    desired_is_cross = bool(master_config.is_cross and not spec.only_isolated)
                    leverage_mismatch = real != 0 and (
                        follower_config is None
                        or follower_config.leverage != desired_leverage
                        or follower_config.is_cross != desired_is_cross
                    )
                except Exception:
                    desired_leverage = None
                    desired_is_cross = None
            if ambiguity_safe_reduction:
                # A reduction does not require a leverage mutation. Avoid any
                # unrelated exchange action while the older execution is still ambiguous.
                leverage_mismatch = False

            basis = _reconciliation_basis(user.copy_state, previous_target, real)
            drift_notional = abs(desired_target - basis) * follower_mark if follower_mark > 0 else Decimal(0)
            if drift_notional < min_notional and not leverage_mismatch:
                continue

            if master_pos == 0 and real != 0 and not ledger.managed and user.manual_trade_policy.value != 'STRICT':
                continue

            pending = (await db.execute(select(CopyJob.id).where(
                CopyJob.user_id == user.id,
                CopyJob.asset == asset,
                CopyJob.origin == 'RECONCILE',
                CopyJob.created_at >= network_state.started_at,
                CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
            ).limit(1))).scalar_one_or_none()
            if pending:
                continue

            if drift_notional >= min_notional and await _terminal_action_rejection_blocks_unchanged_intent(
                db,
                user.id,
                asset,
                network_state.started_at,
                network=network,
                previous_target=previous_target,
                previous_real=before,
                desired_target=desired_target,
                real=real,
            ):
                continue

            increasing_exposure = (
                real == 0 and desired_target != 0
                or real * desired_target > 0 and abs(desired_target) > abs(real)
            )
            if user.copy_state == CopyState.ACTIVE and increasing_exposure and drift_notional >= min_notional:
                wait_seconds = await _liquidity_backoff_seconds(db, user.id, asset, network_state.started_at)
                if wait_seconds > 0:
                    liquidity_backoffs.append({'asset': asset, 'seconds': wait_seconds})
                    continue

            context = {
                'master_position': str(master_pos),
                'master_equity': str(master_equity),
                'master_mark_price': str(master_mark),
                'mark_price': str(master_mark),
                'master_network': settings.master_network,
                'follower_network': network,
                'ai_mode': ai_policy.effective_mode,
                'ai_execution_influence': ai_policy.effective,
                'ai_execution_factor': str(ai_factor),
                'ai_target_without_influence': str(base_target),
            }
            if ambiguity_safe_reduction:
                context['ambiguity_safe_reduction'] = True
            if master_config is not None:
                context['master_leverage'] = master_config.leverage
                context['master_is_cross'] = master_config.is_cross
            if desired_leverage is not None:
                context['desired_follower_leverage'] = desired_leverage
                context['desired_follower_is_cross'] = desired_is_cross
                context['leverage_sync_only'] = bool(
                    leverage_mismatch
                    and drift_notional < min_notional
                    and not ambiguity_safe_reduction
                )

            db.add(CopyJob(
                user_id=user.id,
                asset=asset,
                origin='RECONCILE',
                state=JobState.QUEUED,
                correlation_id=uuid.uuid4().hex,
                context=context,
            ))
            jobs_created += 1

        db.add(EquitySnapshot(
            user_id=user.id,
            account_value=equity,
            free_margin=free_margin,
            unmanaged_margin=unmanaged_margin,
            collateral_balance=snapshot.collateral_balance,
            unrealized_pnl=snapshot.unrealized_pnl,
            account_mode=account_mode,
            taken_at=datetime.now(UTC),
        ))

        run.status = 'OK'; run.discrepancy_type = 'DRIFT' if discrepancies else 'NONE'; run.before = {'discrepancies': discrepancies}; run.after = {'network': network, 'equity': str(equity), 'free_margin': str(free_margin), 'unmanaged_margin': str(unmanaged_margin), 'fills_synced': synced_fills, 'account_mode': account_mode, 'liquidity_backoffs': liquidity_backoffs, 'jobs_created': jobs_created, 'ai_mode': ai_policy.effective_mode, 'ai_execution_factor': str(ai_factor)}; run.finished_at = datetime.now(UTC)
        await audit(db, action='RECONCILIATION_COMPLETED', subject_id=user.id, after={'network': network, 'discrepancies': discrepancies, 'account_mode': account_mode, 'equity': str(equity), 'liquidity_backoffs': liquidity_backoffs, 'jobs_created': jobs_created, 'ai_mode': ai_policy.effective_mode, 'ai_execution_factor': str(ai_factor)})
        await db.commit()
        return {'status': 'OK', 'network': network, 'discrepancies': discrepancies, 'equity': str(equity), 'account_mode': account_mode, 'liquidity_backoffs': liquidity_backoffs, 'jobs_created': jobs_created, 'ai_mode': ai_policy.effective_mode, 'ai_execution_factor': str(ai_factor)}
    except Exception as exc:
        run.status = 'FAILED'; run.error = f'{type(exc).__name__}: {exc}'; run.finished_at = datetime.now(UTC); await db.commit(); raise


async def reconcile_active_users(
    db: AsyncSession,
    hl: HyperliquidAdapter,
    limit: int | None = None,
    *,
    master_hl: HyperliquidAdapter | None = None,
) -> int:
    source_hl = master_hl or hl
    source_snapshot = await source_hl.account_snapshot(settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.MASTER_STATE)
    mp = _positions(source_snapshot.perp_state)
    me = source_snapshot.account_value
    source_mids = await source_hl.mids()
    source_configs = position_configs(source_snapshot.perp_state)
    follower_mids = source_mids if source_hl.network == hl.network else await hl.mids()
    query = select(User).join(TradingAccount, TradingAccount.user_id == User.id).where(
        User.state == UserState.ACTIVE,
        User.copy_state.in_([CopyState.ACTIVE, CopyState.SHADOW, CopyState.PAUSED]),
    ).order_by(User.created_at)
    users = (await db.execute(query)).scalars().all()
    reconciled = 0
    for user in users:
        network_state = await user_network_state(db, user.id)
        if network_state.network != hl.network:
            continue
        await reconcile_user(
            db, hl, user,
            master_positions=mp,
            master_equity=me,
            mids=follower_mids,
            master_mids=source_mids,
            master_configs=source_configs,
        )
        reconciled += 1
        if limit is not None and reconciled >= limit:
            break
    return reconciled