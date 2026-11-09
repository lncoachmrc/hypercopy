"""RISEx TESTNET evaluation branch. Never creates a CopyJob or signs orders."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, select

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.core.config import settings
from app.models.entities import (
    AIProfitExitDecision, CopyState, ExecutionEpoch, MasterEvent,
    PositionLedger, RiskProfile, RISExTradingAccount, User, UserState,
)
from app.services.ai_exit_feedback import FEEDBACK_VERSION, read_exit_feedback
from app.services.ai_profit_exit import (
    ProfitExitAction, ProfitExitFeatureMode, profit_exit_decision_id, read_current_source_cycle,
)
from app.services.ai_profit_exit_mode import read_profit_exit_mode
from app.services.ai_profit_exit_retention import read_retention_context
from app.services.ai_profit_exit_telemetry import record_shadow_observation
from app.services.execution_destination import user_destination_state
from app.services.master_source_identity import is_master_source_user
from app.services.risex_profit_exit_collector import collect_risex_shadow_profit_exit
from app.services.strategy_intents import master_position_from_state


def risex_shadow_candidates():
    return (select(User, PositionLedger, RISExTradingAccount, RiskProfile, ExecutionEpoch)
        .select_from(User)
        .join(PositionLedger, PositionLedger.user_id == User.id)
        .join(RISExTradingAccount, RISExTradingAccount.user_id == User.id)
        .join(RiskProfile, RiskProfile.user_id == User.id)
        .join(ExecutionEpoch, and_(ExecutionEpoch.id == User.active_execution_epoch_id,
                                  ExecutionEpoch.user_id == User.id))
        .where(User.state == UserState.ACTIVE, User.copy_state == CopyState.SHADOW,
               User.execution_provider == 'risex',
               ExecutionEpoch.provider == 'risex', ExecutionEpoch.network == 'testnet',
               ExecutionEpoch.ended_at.is_(None), PositionLedger.managed.is_(True),
               PositionLedger.target_size != 0)
        .order_by(User.id, PositionLedger.asset))


def _bound(user, ledger, account, epoch) -> bool:
    return (user.state == UserState.ACTIVE and user.copy_state == CopyState.SHADOW
        and user.execution_provider == 'risex'
        and user.active_execution_epoch_id == epoch.id and epoch.user_id == user.id
        and epoch.provider == 'risex' and epoch.network == 'testnet' and epoch.ended_at is None
        and account.user_id == user.id and ledger.user_id == user.id and ledger.managed
        and isinstance(epoch.account_address, str) and isinstance(account.account_address, str)
        and len(epoch.account_address) == 42 and epoch.account_address.startswith('0x')
        and epoch.account_address.lower() == account.account_address.lower())


async def evaluate_risex_shadow_portfolio(db, *, mode, master_snapshot,
        snapshot_started_order, intelligence, evaluation_slot, decide, inputs_builder, api=None) -> dict:
    stats = dict(evaluated=0, decisions=0, observations=0, abstained=0, errors=0, queued=0)
    if mode is not ProfitExitFeatureMode.SHADOW or os.getenv('ENABLE_LIVE_TRADING') != 'false':
        return stats
    rows = (await db.execute(risex_shadow_candidates())).all()
    if not rows:
        return stats

    async def evaluate(client):
        for user, ledger, account, risk, epoch in rows:
            stats['evaluated'] += 1
            try:
                if not _bound(user, ledger, account, epoch) or is_master_source_user(user):
                    stats['abstained'] += 1
                    continue
                # User.execution_network is a legacy SQL column, deliberately
                # absent from the ORM model. Reuse the canonical state reader
                # to check user/epoch agreement rather than inventing an ORM field.
                destination = await user_destination_state(db, user.id)
                if (destination.epoch_id != epoch.id or destination.provider != 'risex'
                        or destination.network != 'testnet'):
                    stats['abstained'] += 1
                    continue
                cycle = await read_current_source_cycle(
                    db, asset=ledger.asset, master_network=settings.master_network,
                    snapshot_started_order=snapshot_started_order,
                    current_master_position=master_position_from_state(master_snapshot.perp_state, ledger.asset),
                )
                if cycle is None or await db.get(MasterEvent, cycle.open_event_id) is None:
                    stats['abstained'] += 1
                    continue
                obs = await collect_risex_shadow_profit_exit(
                    api=client, network='testnet', asset=ledger.asset,
                    shadow_position=Decimal(str(ledger.target_size)),
                    master_perp_state=master_snapshot.perp_state, slippage_bps=risk.max_slippage_bps,
                )
                observed_at = datetime.now(UTC)
                matches = obs.current_position == Decimal(str(ledger.target_size))
                scope = dict(user_id=user.id, execution_epoch_id=epoch.id,
                             execution_provider='risex', execution_network='testnet',
                             asset=ledger.asset, source_cycle_id=cycle.source_cycle_id,
                             state_version=cycle.state_version)
                await record_shadow_observation(db, **scope, evaluation_slot=evaluation_slot,
                                                 observation=obs, position_matches=matches)
                stats['observations'] += 1
                if (not obs.complete or not obs.eligible or not matches or obs.economics is None
                        or obs.economics.net_pnl is None or obs.economics.net_pnl <= 0):
                    stats['abstained'] += 1
                    continue
                decision_id = profit_exit_decision_id(
                    **scope, follower_position=obs.current_position,
                    evaluation_slot=evaluation_slot, evaluation_basis=obs.basis,
                )
                if await db.get(AIProfitExitDecision, decision_id) is not None:
                    continue
                inputs = inputs_builder(obs, cycle, intelligence, evaluation_slot, copy_state=user.copy_state)
                inputs.update(decision_mode='SHADOW', execution_provider='risex', execution_network='testnet',
                              learning_policy_version=FEEDBACK_VERSION, residual_funding=None,
                              fee_model='configured_assumption', entry_price_model='master_cross_venue_proxy')
                if obs.current_position < 0:
                    inputs['profit_retention'] = await read_retention_context(
                        db, **scope, follower_position_size=obs.current_position,
                        pnl_complete=False, current_net_pnl=obs.economics.net_pnl,
                        evaluation_basis=obs.basis, funding_model=obs.funding_model,
                    )
                    inputs['exit_feedback'] = await read_exit_feedback(
                        db, user_id=user.id, epoch_id=epoch.id, provider='risex', network='testnet',
                        asset=ledger.asset, side='SHORT', basis=obs.basis, pnl_complete=False,
                        funding_model=obs.funding_model, current_cycle=cycle.source_cycle_id, as_of=observed_at,
                    )
                action, reason, runtime = await decide(inputs)
                # A mode/destination change while the provider was running must
                # not be mistaken for an observation of the new execution epoch.
                current = await user_destination_state(db, user.id)
                if (current.epoch_id != epoch.id or current.provider != 'risex' or current.network != 'testnet'
                        or await read_profit_exit_mode(db) is not ProfitExitFeatureMode.SHADOW):
                    stats['abstained'] += 1
                    continue
                now = datetime.now(UTC)
                db.add(AIProfitExitDecision(
                    id=decision_id, **scope, side='SHORT' if obs.current_position < 0 else 'LONG',
                    source_cycle_open_event_id=cycle.open_event_id, source_master_position=cycle.master_position,
                    follower_position_size=obs.current_position, action=action.value,
                    intent_state=None, copy_job_id=None, net_pnl=obs.economics.net_pnl,
                    pnl_complete=False, position_verified_at=observed_at, decision_inputs=inputs,
                    decision_reason=reason, model_provider=str(runtime.get('provider') or 'unavailable'),
                    model_name=str(runtime.get('model') or 'unavailable'), model_version=FEEDBACK_VERSION,
                    decided_at=now, expires_at=now+timedelta(seconds=settings.AI_PROFIT_EXIT_DECISION_TTL_SECONDS),
                ))
                await db.commit()
                stats['decisions'] += 1
                if action is ProfitExitAction.ABSTAIN:
                    stats['abstained'] += 1
            except Exception:
                await db.rollback()
                stats['errors'] += 1
                stats['abstained'] += 1

    if api is not None:
        await evaluate(api)
    else:
        async with RISExReadOnlyHTTPTransport(base_url='https://api.testnet.rise.trade') as client:
            await evaluate(client)
    return stats
