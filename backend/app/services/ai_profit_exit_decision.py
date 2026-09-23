from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Priority
from app.core.config import Network, settings
from app.models.entities import (
    AIProfitExitDecision,
    CopyJob,
    CopyState,
    JobState,
    MasterEvent,
    PositionLedger,
    RiskProfile,
    ShadowPositionLedger,
    TradingAccount,
    User,
    UserState,
)
from app.services.ai_intelligence import call_llm_with_failover, read_ai_intelligence
from app.services.ai_profit_exit import (
    PROFIT_EXIT_ORIGIN,
    ProfitExitAction,
    ProfitExitFeatureMode,
    ProfitExitIntentState,
    profit_exit_decision_id,
    profit_exit_job_id,
    read_current_source_cycle,
    read_operational_profit_exit_memory,
)
from app.services.ai_profit_exit_collector import collect_profit_exit_economics
from app.services.ai_profit_exit_mode import read_profit_exit_mode
from app.services.master_source_identity import is_master_source_user
from app.services.networking import user_network_state
from app.services.queue import publish_job
from app.services.shadow_positions import collect_shadow_profit_exit_economics
from app.services.reconcile import master_snapshot_started_order
from app.services.strategy_intents import master_position_from_state


def _llm_enabled() -> bool:
    return os.getenv("LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _validated_profit_exit_action(raw: object) -> tuple[ProfitExitAction, str]:
    if not isinstance(raw, dict):
        return ProfitExitAction.ABSTAIN, "LLM response is not a JSON object"
    action_raw = str(raw.get("action") or "").upper()
    try:
        action = ProfitExitAction(action_raw)
    except ValueError:
        return ProfitExitAction.ABSTAIN, "LLM returned an unsupported profit-exit action"
    reason = str(raw.get("reason") or "").strip()[:1200]
    if not reason:
        reason = "No model rationale supplied"
    return action, reason


async def _decide_with_ai(payload: dict) -> tuple[ProfitExitAction, str, dict]:
    if not _llm_enabled():
        return ProfitExitAction.ABSTAIN, "LLM is unavailable because LLM_ENABLED=false", {
            "provider": "unavailable",
            "model": "unavailable",
            "fallback_index": None,
            "failures": [],
        }

    system = (
        "You are TRAXION AI Profit Exit. Decide only whether an already-open follower "
        "position that is currently net profitable should remain open or be closed now. "
        "Return JSON only with action HOLD, CLOSE_PROFIT, or ABSTAIN and a concise reason. "
        "Do not propose entries, scale-ins, reversals, leverage changes, stop changes, "
        "fixed take-profit thresholds, fixed retracement rules, or time-based exits. "
        "CLOSE_PROFIT means close the full residual follower position now. Deterministic "
        "TRAXION safety controls independently decide whether that action is admissible. "
        "If economics_basis is SHADOW_SIMULATION_NO_FUNDING, the position is virtual and "
        "funding is unavailable: do not invent funding and treat that omission as explicit "
        "uncertainty in the hypothetical decision."
    )
    try:
        raw, runtime = await call_llm_with_failover(
            system,
            json.dumps(payload, separators=(",", ":"), default=str),
        )
    except Exception as exc:
        return ProfitExitAction.ABSTAIN, f"AI unavailable: {type(exc).__name__}: {exc}"[:1200], {
            "provider": "unavailable",
            "model": "unavailable",
            "fallback_index": None,
            "failures": [],
        }

    action, reason = _validated_profit_exit_action(raw)
    return action, reason, runtime


def _decision_inputs(
    observation,
    cycle,
    intelligence: dict,
    evaluation_slot: int,
    copy_state: CopyState,
) -> dict:
    economics = observation.economics
    return {
        "evaluation_slot": evaluation_slot,
        "copy_state": copy_state.value,
        "economics_basis": observation.economics_basis,
        "funding_included": observation.funding_included,
        "source_cycle_id": cycle.source_cycle_id,
        "source_state_version": cycle.state_version,
        "master_position": str(cycle.master_position),
        "follower_position": str(observation.current_position),
        "entry_price": str(observation.entry_price),
        "mark_price": str(observation.mark_price),
        "executable_exit_price": str(observation.executable_exit_price),
        "taker_fee_rate": str(observation.taker_fee_rate),
        "gross_price_pnl": str(economics.gross_price_pnl) if economics else None,
        "residual_entry_fees": str(economics.residual_entry_fees) if economics else None,
        "residual_funding": (
            str(economics.residual_funding)
            if economics and observation.funding_included
            else None
        ),
        "estimated_exit_fee": str(economics.estimated_exit_fee) if economics else None,
        "net_pnl": str(economics.net_pnl) if economics else None,
        "economics_note": observation.reason or None,
        "capital_intelligence": {
            "status": intelligence.get("status"),
            "summary": (intelligence.get("analysis") or {}).get("summary")
            if isinstance(intelligence.get("analysis"), dict)
            else None,
            "confidence": (intelligence.get("analysis") or {}).get("confidence")
            if isinstance(intelligence.get("analysis"), dict)
            else None,
        },
    }


async def evaluate_profit_exit_portfolio(
    db: AsyncSession,
    redis,
    *,
    master_hl: HyperliquidAdapter,
    follower_hl_for_network: Callable[[Network], HyperliquidAdapter],
) -> dict:
    mode = await read_profit_exit_mode(db)
    if mode is ProfitExitFeatureMode.OFF:
        return {"mode": mode.value, "evaluated": 0, "decisions": 0, "queued": 0, "abstained": 0}

    snapshot_started_order = await master_snapshot_started_order(required=True)
    if master_hl.limiter is None:
        raise RuntimeError("AI Profit Exit master reader requires shared Hyperliquid limiter")
    master_snapshot = await master_hl.account_snapshot(
        settings.HYPERLIQUID_MASTER_ADDRESS,
        priority=Priority.MASTER_STATE,
    )
    intelligence = await read_ai_intelligence(db)

    active_rows = (await db.execute(
        select(User, PositionLedger, TradingAccount, RiskProfile)
        .join(PositionLedger, PositionLedger.user_id == User.id)
        .join(TradingAccount, TradingAccount.user_id == User.id)
        .join(RiskProfile, RiskProfile.user_id == User.id)
        .where(
            User.state == UserState.ACTIVE,
            User.copy_state == CopyState.ACTIVE,
            PositionLedger.managed.is_(True),
            PositionLedger.size != 0,
        )
        .order_by(User.id, PositionLedger.asset)
    )).all()

    shadow_rows = (await db.execute(
        select(
            User,
            PositionLedger,
            TradingAccount,
            RiskProfile,
            ShadowPositionLedger,
        )
        .join(PositionLedger, PositionLedger.user_id == User.id)
        .join(TradingAccount, TradingAccount.user_id == User.id)
        .join(RiskProfile, RiskProfile.user_id == User.id)
        .join(
            ShadowPositionLedger,
            (ShadowPositionLedger.user_id == User.id)
            & (ShadowPositionLedger.asset == PositionLedger.asset)
            & (ShadowPositionLedger.shadow_started_at == User.shadow_started_at),
        )
        .where(
            User.state == UserState.ACTIVE,
            User.copy_state == CopyState.SHADOW,
            User.shadow_started_at.is_not(None),
            PositionLedger.managed.is_(True),
            ShadowPositionLedger.size != 0,
        )
        .order_by(User.id, ShadowPositionLedger.asset)
    )).all()

    rows = [
        (user, ledger, account, risk, None)
        for user, ledger, account, risk in active_rows
    ]
    rows.extend(shadow_rows)

    evaluated = decisions = queued = abstained = 0
    active_evaluated = shadow_evaluated = shadow_decisions = 0
    shadow_mids_by_network: dict[Network, dict[str, str]] = {}
    shadow_fee_by_account: dict[tuple[Network, str], Decimal] = {}
    now = datetime.now(UTC)
    evaluation_slot = int(now.timestamp()) // settings.AI_PROFIT_EXIT_EVAL_SECONDS

    for user, ledger, account, risk, shadow_position in rows:
        evaluated += 1
        is_shadow = user.copy_state == CopyState.SHADOW
        if is_shadow:
            shadow_evaluated += 1
        else:
            active_evaluated += 1

        if is_master_source_user(user):
            abstained += 1
            continue
        try:
            destination = await user_network_state(db, user.id)
        except Exception:
            abstained += 1
            continue
        if destination.provider != "hyperliquid":
            abstained += 1
            continue
        if shadow_position is not None and (
            shadow_position.execution_epoch_id != destination.epoch_id
            or shadow_position.execution_provider != destination.provider
            or shadow_position.execution_network != destination.network
        ):
            abstained += 1
            continue

        try:
            master_position = master_position_from_state(master_snapshot.perp_state, ledger.asset)
            cycle = await read_current_source_cycle(
                db,
                asset=ledger.asset,
                master_network=settings.master_network,
                snapshot_started_order=snapshot_started_order,
                current_master_position=master_position,
            )
        except Exception:
            cycle = None
        if cycle is None:
            abstained += 1
            continue

        if not is_shadow:
            memory = await read_operational_profit_exit_memory(
                db,
                user_id=user.id,
                execution_epoch_id=destination.epoch_id,
                execution_provider=destination.provider,
                execution_network=destination.network,
                asset=ledger.asset,
                source_cycle_id=cycle.source_cycle_id,
            )
            if memory is not None and memory.intent_state in {
                ProfitExitIntentState.PENDING.value,
                ProfitExitIntentState.AMBIGUOUS.value,
                ProfitExitIntentState.COMPLETED.value,
            }:
                continue

        open_event = await db.get(MasterEvent, cycle.open_event_id)
        if open_event is None:
            abstained += 1
            continue

        follower_hl = follower_hl_for_network(destination.network)
        if follower_hl.limiter is None:
            abstained += 1
            continue

        if shadow_position is not None:
            mids = shadow_mids_by_network.get(destination.network)
            if mids is None:
                try:
                    mids = await follower_hl.mids(
                        priority=Priority.RECONCILE,
                    )
                except Exception:
                    abstained += 1
                    continue
                shadow_mids_by_network[destination.network] = mids

            fee_key = (destination.network, account.account_address.lower())
            cached_fee = shadow_fee_by_account.get(fee_key)
            observation = await collect_shadow_profit_exit_economics(
                follower_hl,
                account_address=account.account_address,
                position=shadow_position,
                slippage_bps=risk.max_slippage_bps,
                market_mids=mids,
                taker_fee_rate=cached_fee,
            )
            if (
                cached_fee is None
                and observation.taker_fee_rate is not None
            ):
                shadow_fee_by_account[fee_key] = observation.taker_fee_rate

            observation_valid = bool(
                observation.eligible
                and observation.current_position is not None
                and observation.economics is not None
                and observation.economics.net_pnl is not None
                and observation.economics.net_pnl > 0
                and Decimal(str(shadow_position.size))
                == observation.current_position
            )
        else:
            observation = await collect_profit_exit_economics(
                follower_hl,
                account_address=account.account_address,
                asset=ledger.asset,
                history_start_ms=max(0, int(open_event.event_ts.timestamp() * 1000)),
                history_end_ms=None,
                slippage_bps=risk.max_slippage_bps,
            )
            observation_valid = bool(
                observation.complete
                and observation.eligible
                and observation.current_position is not None
                and observation.economics is not None
                and observation.economics.net_pnl is not None
                and observation.economics.net_pnl > 0
                and Decimal(str(ledger.size))
                == observation.current_position
            )

        if not observation_valid:
            abstained += 1
            continue

        inputs = _decision_inputs(
            observation,
            cycle,
            intelligence,
            evaluation_slot,
            user.copy_state,
        )

        decision_id = profit_exit_decision_id(
            user_id=user.id,
            execution_epoch_id=destination.epoch_id,
            execution_provider=destination.provider,
            execution_network=destination.network,
            asset=ledger.asset,
            source_cycle_id=cycle.source_cycle_id,
            state_version=cycle.state_version,
            follower_position=observation.current_position,
            evaluation_slot=evaluation_slot,
        )
        if await db.get(AIProfitExitDecision, decision_id) is not None:
            continue

        action, reason, runtime = await _decide_with_ai(inputs)
        decision_now = datetime.now(UTC)
        operational = (
            user.copy_state == CopyState.ACTIVE
            and mode is ProfitExitFeatureMode.ON
            and action is ProfitExitAction.CLOSE_PROFIT
        )
        job_id = profit_exit_job_id(decision_id) if operational else None
        decision = AIProfitExitDecision(
            id=decision_id,
            user_id=user.id,
            execution_epoch_id=destination.epoch_id,
            execution_provider=destination.provider,
            execution_network=destination.network,
            asset=ledger.asset,
            side="LONG" if observation.current_position > 0 else "SHORT",
            source_cycle_id=cycle.source_cycle_id,
            source_cycle_open_event_id=cycle.open_event_id,
            source_master_position=cycle.master_position,
            follower_position_size=observation.current_position,
            state_version=cycle.state_version,
            action=action.value,
            intent_state=ProfitExitIntentState.PENDING.value if operational else None,
            net_pnl=observation.economics.net_pnl,
            pnl_complete=bool(observation.economics.complete),
            position_verified_at=decision_now,
            decision_inputs=inputs,
            decision_reason=reason,
            model_provider=str(runtime.get("provider") or "unavailable"),
            model_name=str(runtime.get("model") or "unavailable"),
            model_version=None,
            decided_at=decision_now,
            expires_at=decision_now + timedelta(seconds=settings.AI_PROFIT_EXIT_DECISION_TTL_SECONDS),
            copy_job_id=job_id,
        )
        db.add(decision)

        job = None
        if operational:
            job = CopyJob(
                id=job_id,
                master_event_id=None,
                user_id=user.id,
                execution_epoch_id=destination.epoch_id,
                execution_provider=destination.provider,
                execution_network=destination.network,
                asset=ledger.asset,
                origin=PROFIT_EXIT_ORIGIN,
                state=JobState.QUEUED,
                context={
                    "ai_profit_exit_decision_id": str(decision_id),
                    "source_cycle_id": cycle.source_cycle_id,
                    "follower_network": destination.network,
                    "execution_provider": destination.provider,
                    "execution_epoch_id": str(destination.epoch_id),
                },
                correlation_id=decision_id.hex,
            )
            db.add(job)

        # Commit durable intent before the Redis delivery side effect.
        await db.commit()
        decisions += 1
        if is_shadow:
            shadow_decisions += 1

        if job is not None:
            try:
                await publish_job(redis, db, job)
                if job.state in {JobState.SKIPPED, JobState.DEAD}:
                    decision.intent_state = ProfitExitIntentState.FAILED.value
                    await db.commit()
                else:
                    await db.commit()
                    queued += 1
            except Exception:
                await db.rollback()
                # The committed QUEUED row has enqueued_at=NULL; normal repair_stream
                # republishes it without creating a second decision/job.
                queued += 1

        if action is ProfitExitAction.ABSTAIN:
            abstained += 1

    return {
        "mode": mode.value,
        "evaluated": evaluated,
        "decisions": decisions,
        "queued": queued,
        "abstained": abstained,
        "active_evaluated": active_evaluated,
        "shadow_evaluated": shadow_evaluated,
        "shadow_decisions": shadow_decisions,
    }
