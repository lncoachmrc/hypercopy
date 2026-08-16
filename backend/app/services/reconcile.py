from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, fill_event_id
from app.adapters.ratelimit import Priority
from app.core.logging import get_logger
from app.engine.reconcile import classify
from app.models.entities import CopyJob, CopyState, EquitySnapshot, Execution, ExecutionState, Fill, JobState, PositionLedger, ReconciliationRun, RiskHalt, RiskProfile, RiskState, TradingAccount, User, UserState
from app.services.audit import audit

log = get_logger(__name__)


def _positions(state: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in state.get('assetPositions', []):
        p = row.get('position', row)
        out[str(p.get('coin'))] = Decimal(str(p.get('szi', '0')))
    return out


def _account_value(state: dict) -> Decimal:
    return Decimal(str(state.get('marginSummary', {}).get('accountValue', '0')))


def _free_margin(state: dict) -> Decimal:
    # Hyperliquid clearinghouseState exposes withdrawable as the immediately
    # available collateral. Fall back conservatively to account value minus
    # total margin used if the field is unavailable.
    raw = state.get('withdrawable')
    if raw not in (None, ''):
        return max(Decimal(str(raw)), Decimal(0))
    summary = state.get('marginSummary', {})
    value = Decimal(str(summary.get('accountValue', '0')))
    used = Decimal(str(summary.get('totalMarginUsed', '0')))
    return max(value - used, Decimal(0))


async def _sync_missing_fills(db: AsyncSession, hl: HyperliquidAdapter, user: User, account_address: str) -> int:
    """Eventually materialize real exchange fills without inventing fill ids.

    Order acknowledgements can tell us total size/average price but do not always
    carry each fill's `tid`. During reconciliation we fetch the real fill stream
    only when a FILLED execution lacks fill rows, match by exchange OID, and use
    Hyperliquid's own hash/oid/tid tuple as the persistent unique id.
    """
    missing = (await db.execute(
        select(Execution).where(
            Execution.user_id == user.id,
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
                exchange_fill_id=fill_event_id(fill), execution_id=execution.id,
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
    state = await hl.user_state(__import__('app.core.config', fromlist=['settings']).settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.MASTER_STATE)
    mids = await hl.mids()
    return _positions(state), _account_value(state), mids


async def reconcile_user(db: AsyncSession, hl: HyperliquidAdapter, user: User, *, master_positions: dict[str, Decimal], master_equity: Decimal, mids: dict[str, str], create_jobs: bool = True) -> dict:
    run = ReconciliationRun(user_id=user.id, before={}, after={})
    db.add(run); await db.flush()
    try:
        account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
        if not account:
            run.status = 'SKIPPED'; run.finished_at = datetime.now(UTC); await db.commit(); return {'status': 'SKIPPED'}
        real_state = await hl.user_state(account.account_address)
        # Fill materialization is useful for audit/history but must not starve the
        # correctness-critical position reconciliation when its high-weight
        # history request has no budget. Retry on the next cycle.
        try:
            synced_fills = await _sync_missing_fills(db, hl, user, account.account_address)
        except Exception:
            synced_fills = 0
            log.warning('Deferred fill-history synchronization', extra={'user_id': str(user.id)}, exc_info=True)
        real_positions = _positions(real_state)
        equity = _account_value(real_state)
        free_margin = _free_margin(real_state)
        risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
        if not risk_state:
            risk_state = RiskState(user_id=user.id, peak_equity=equity, day_start_equity=equity, day_key=datetime.now(UTC).date().isoformat())
            db.add(risk_state)
        risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one_or_none()
        # Compute the minimum liquidation distance from the real exchange
        # positions. Open/increase operations are halted below 15%, while
        # reductions remain permitted by the Risk Engine.
        distances = []
        for row in real_state.get('assetPositions', []):
            pos = row.get('position', row)
            asset_name = str(pos.get('coin') or '')
            liq_raw = pos.get('liquidationPx')
            mark_raw = mids.get(asset_name)
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
        unresolved_assets = set((await db.execute(
            select(Execution.asset).where(
                Execution.user_id == user.id,
                Execution.state.in_([ExecutionState.SUBMITTING, ExecutionState.UNKNOWN]),
            )
        )).scalars().all())
        assets = set(master_positions) | set(real_positions) | set(ledger_by_asset)
        discrepancies = []
        for asset in assets:
            real = real_positions.get(asset, Decimal(0))
            ledger = ledger_by_asset.get(asset)
            if not ledger:
                ledger = PositionLedger(user_id=user.id, asset=asset, size=real, mark_price=Decimal(str(mids.get(asset, '0'))), managed=asset in master_positions, exchange_verified_at=datetime.now(UTC))
                db.add(ledger); ledger_by_asset[asset] = ledger
            before = ledger.size
            ledger.size = real
            ledger.mark_price = Decimal(str(mids.get(asset, '0')))
            ledger.exchange_verified_at = datetime.now(UTC)
            if before != real:
                discrepancies.append({'asset': asset, 'ledger': str(before), 'real': str(real)})

            # Position target is calculated by execution service; reconciliation
            # creates a durable job from the current master snapshot so the same
            # pure sizing engine is used on hot and cold paths.
            if create_jobs and asset in unresolved_assets:
                # Exactly-once safety beats liveness: a new target job must not
                # race an external effect whose Cloid is still ambiguous. The
                # execution reconciler/admin must resolve that intent first.
                continue
            if create_jobs and asset in master_positions and master_positions[asset] != 0:
                already = (await db.execute(select(CopyJob.id).where(CopyJob.user_id == user.id, CopyJob.asset == asset, CopyJob.origin == 'RECONCILE', CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING])).limit(1))).scalar_one_or_none()
                if not already:
                    db.add(CopyJob(user_id=user.id, asset=asset, origin='RECONCILE', state=JobState.QUEUED, correlation_id=uuid.uuid4().hex, context={'master_position': str(master_positions[asset]), 'master_equity': str(master_equity), 'mark_price': str(mids.get(asset, '0'))}))
            elif create_jobs and real != 0 and asset not in master_positions:
                # A position previously managed by HyperCopy must converge to
                # zero when the master exits, regardless of the user's manual
                # trade policy. Truly unmanaged/orphan positions are left alone
                # under COEXIST/MANUAL_WINS and closed only under STRICT.
                should_close = bool(ledger.managed) or user.manual_trade_policy.value == 'STRICT'
                if should_close:
                    already = (await db.execute(select(CopyJob.id).where(
                        CopyJob.user_id == user.id, CopyJob.asset == asset,
                        CopyJob.origin == 'RECONCILE',
                        CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
                    ).limit(1))).scalar_one_or_none()
                    if not already:
                        db.add(CopyJob(user_id=user.id, asset=asset, origin='RECONCILE', state=JobState.QUEUED, correlation_id=uuid.uuid4().hex, context={'master_position': '0', 'master_equity': str(master_equity), 'mark_price': str(mids.get(asset, '0'))}))

        # Eligible equity excludes margin tied to unmanaged positions. This is
        # persisted once per reconciliation so the hot execution path requires
        # no per-follower state read from Hyperliquid.
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
        db.add(EquitySnapshot(
            user_id=user.id, account_value=equity, free_margin=free_margin,
            unmanaged_margin=unmanaged_margin, taken_at=datetime.now(UTC),
        ))

        run.status = 'OK'; run.discrepancy_type = 'DRIFT' if discrepancies else 'NONE'; run.before = {'discrepancies': discrepancies}; run.after = {'equity': str(equity), 'free_margin': str(free_margin), 'unmanaged_margin': str(unmanaged_margin), 'fills_synced': synced_fills}; run.finished_at = datetime.now(UTC)
        await audit(db, action='RECONCILIATION_COMPLETED', subject_id=user.id, after={'discrepancies': discrepancies})
        await db.commit()
        return {'status': 'OK', 'discrepancies': discrepancies, 'equity': str(equity)}
    except Exception as exc:
        run.status = 'FAILED'; run.error = f'{type(exc).__name__}: {exc}'; run.finished_at = datetime.now(UTC); await db.commit(); raise


async def reconcile_active_users(db: AsyncSession, hl: HyperliquidAdapter, limit: int | None = None) -> int:
    mp, me, mids = await master_snapshot(hl)
    query = select(User).join(TradingAccount, TradingAccount.user_id == User.id).where(
        User.state == UserState.ACTIVE,
        User.copy_state.in_([CopyState.ACTIVE, CopyState.SHADOW, CopyState.PAUSED]),
    ).order_by(User.created_at)
    if limit is not None:
        query = query.limit(limit)
    users = (await db.execute(query)).scalars().all()
    for user in users:
        await reconcile_user(db, hl, user, master_positions=mp, master_equity=me, mids=mids)
    return len(users)
