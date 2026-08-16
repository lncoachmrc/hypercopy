from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter, fill_event_id
from app.adapters.ratelimit import Priority
from app.models.entities import CopyJob, CopyState, EquitySnapshot, Execution, ExecutionState, Fill, JobState, PositionLedger, ReconciliationRun, RiskHalt, RiskProfile, RiskState, TradingAccount, User, UserState
from app.services.audit import audit

log = __import__('app.core.logging', fromlist=['get_logger']).get_logger(__name__)


def _positions(state: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in state.get('assetPositions', []):
        p = row.get('position', row)
        out[str(p.get('coin'))] = Decimal(str(p.get('szi', '0')))
    return out


async def _sync_missing_fills(db: AsyncSession, hl: HyperliquidAdapter, user: User, account_address: str) -> int:
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
    settings = __import__('app.core.config', fromlist=['settings']).settings
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
    create_jobs: bool = True,
) -> dict:
    follower_mids = mids
    source_mids = master_mids or mids
    run = ReconciliationRun(user_id=user.id, before={}, after={})
    db.add(run); await db.flush()
    try:
        account = (await db.execute(select(TradingAccount).where(TradingAccount.user_id == user.id))).scalar_one_or_none()
        if not account:
            run.status = 'SKIPPED'; run.finished_at = datetime.now(UTC); await db.commit(); return {'status': 'SKIPPED'}

        snapshot = await hl.account_snapshot(account.account_address)
        real_state = snapshot.perp_state
        equity = snapshot.account_value
        free_margin = snapshot.free_margin
        account_mode = snapshot.abstraction

        try:
            synced_fills = await _sync_missing_fills(db, hl, user, account.account_address)
        except Exception:
            synced_fills = 0
            log.warning('Deferred fill-history synchronization', extra={'user_id': str(user.id)}, exc_info=True)
        real_positions = _positions(real_state)
        risk_state = (await db.execute(select(RiskState).where(RiskState.user_id == user.id))).scalar_one_or_none()
        if not risk_state:
            risk_state = RiskState(user_id=user.id, peak_equity=equity, day_start_equity=equity, day_key=datetime.now(UTC).date().isoformat())
            db.add(risk_state)
        risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one_or_none()

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
            follower_mark = Decimal(str(follower_mids.get(asset, '0')))
            if not ledger:
                ledger = PositionLedger(user_id=user.id, asset=asset, size=real, mark_price=follower_mark, managed=asset in master_positions, exchange_verified_at=datetime.now(UTC))
                db.add(ledger); ledger_by_asset[asset] = ledger
            before = ledger.size
            ledger.size = real
            ledger.mark_price = follower_mark
            ledger.exchange_verified_at = datetime.now(UTC)
            if before != real:
                discrepancies.append({'asset': asset, 'ledger': str(before), 'real': str(real)})

            if create_jobs and asset in unresolved_assets:
                continue
            if create_jobs and asset in master_positions and master_positions[asset] != 0:
                already = (await db.execute(select(CopyJob.id).where(CopyJob.user_id == user.id, CopyJob.asset == asset, CopyJob.origin == 'RECONCILE', CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING])).limit(1))).scalar_one_or_none()
                if not already:
                    master_mark = str(source_mids.get(asset, '0'))
                    db.add(CopyJob(
                        user_id=user.id, asset=asset, origin='RECONCILE', state=JobState.QUEUED,
                        correlation_id=uuid.uuid4().hex,
                        context={'master_position': str(master_positions[asset]), 'master_equity': str(master_equity), 'master_mark_price': master_mark, 'mark_price': master_mark},
                    ))
            elif create_jobs and real != 0 and asset not in master_positions:
                should_close = bool(ledger.managed) or user.manual_trade_policy.value == 'STRICT'
                if should_close:
                    already = (await db.execute(select(CopyJob.id).where(
                        CopyJob.user_id == user.id, CopyJob.asset == asset,
                        CopyJob.origin == 'RECONCILE',
                        CopyJob.state.in_([JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING]),
                    ).limit(1))).scalar_one_or_none()
                    if not already:
                        close_mark = str(source_mids.get(asset, follower_mids.get(asset, '0')))
                        db.add(CopyJob(
                            user_id=user.id, asset=asset, origin='RECONCILE', state=JobState.QUEUED,
                            correlation_id=uuid.uuid4().hex,
                            context={'master_position': '0', 'master_equity': str(master_equity), 'master_mark_price': close_mark, 'mark_price': close_mark},
                        ))

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
        db.add(EquitySnapshot(user_id=user.id, account_value=equity, free_margin=free_margin, unmanaged_margin=unmanaged_margin, taken_at=datetime.now(UTC)))

        run.status = 'OK'; run.discrepancy_type = 'DRIFT' if discrepancies else 'NONE'; run.before = {'discrepancies': discrepancies}; run.after = {'equity': str(equity), 'free_margin': str(free_margin), 'unmanaged_margin': str(unmanaged_margin), 'fills_synced': synced_fills, 'account_mode': account_mode}; run.finished_at = datetime.now(UTC)
        await audit(db, action='RECONCILIATION_COMPLETED', subject_id=user.id, after={'discrepancies': discrepancies, 'account_mode': account_mode, 'equity': str(equity)})
        await db.commit()
        return {'status': 'OK', 'discrepancies': discrepancies, 'equity': str(equity), 'account_mode': account_mode}
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
    mp, me, source_mids = await master_snapshot(source_hl)
    follower_mids = source_mids if source_hl is hl else await hl.mids()
    query = select(User).join(TradingAccount, TradingAccount.user_id == User.id).where(
        User.state == UserState.ACTIVE,
        User.copy_state.in_([CopyState.ACTIVE, CopyState.SHADOW, CopyState.PAUSED]),
    ).order_by(User.created_at)
    if limit is not None:
        query = query.limit(limit)
    users = (await db.execute(query)).scalars().all()
    for user in users:
        await reconcile_user(db, hl, user, master_positions=mp, master_equity=me, mids=follower_mids, master_mids=source_mids)
    return len(users)
