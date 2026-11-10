from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Callable

from sqlalchemy import and_, or_, select
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
    ExecutionEpoch,
    RISExTradingAccount,
    PositionLedger,
    RiskProfile,
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
from app.services.ai_profit_exit_collector import (
    collect_profit_exit_economics,
    collect_shadow_profit_exit_economics,
)
from app.services.ai_profit_exit_mode import read_profit_exit_mode
from app.services.ai_profit_exit_telemetry import record_shadow_observation
from app.services.ai_profit_exit_retention import RETENTION_VARIANT, read_retention_context
from app.services.ai_profit_exit_learning import (
    LEARNING_VERSION, read_shadow_learning_feedback,
)
from app.services.ai_profit_exit_risex_targets import refresh_risex_shadow_targets
from app.services.ai_profit_exit_risex_shadow import (
    RISEX_SHADOW_BASIS, collect_risex_shadow_profit_exit_economics,
)
from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.services.master_source_identity import is_master_source_user
from app.services.networking import user_network_state
from app.services.queue import publish_job
from app.services.reconcile import master_snapshot_started_order
from app.services.strategy_intents import master_position_from_state


log = logging.getLogger(__name__)


def _llm_enabled() -> bool:
    return os.getenv("LLM_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _shadow_retention_allowed(*, mode: ProfitExitFeatureMode, follower_position: Decimal) -> bool:
    """Research signal: SHORT-only. Never influence ON execution."""
    return mode is ProfitExitFeatureMode.SHADOW and follower_position < 0


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
        "When evaluation_basis identifies a shadow estimate the position is hypothetical, "
        "funding is not modeled and the result is record-only. Treat material uncertainty "
        "conservatively and use ABSTAIN when that uncertainty prevents a reliable choice."
    )
    if payload.get("evaluation_basis") == RISEX_SHADOW_BASIS:
        system += (
            " RISEx TESTNET shadow valuation uses the Hyperliquid master entry "
            "as a proxy and the published tier-1 taker fee as a hypothetical "
            "cost. Funding, actual account-specific fees, actual entry fills, "
            "and executable slippage are not verified. Never describe this as "
            "realized or complete net profit. This is record-only evidence."
        )
    feedback = payload.get("learning_feedback")
    if isinstance(feedback, dict) and feedback.get("status") == "exploratory":
        system += (
            " SHADOW RESEARCH FEEDBACK: use aggregate prior source-cycle, "
            "same-provider next-observation statistics as weak contextual "
            "evidence only, never as realized PnL or proof of causation. "
            "Do not infer an exit threshold, retrain a model, or override "
            "the master/risk controller using this exploratory feedback."
        )
    retention = payload.get("profit_retention")
    if (
        isinstance(retention, dict)
        and retention.get("variant") == RETENTION_VARIANT
        and retention.get("status") == "observed"
    ):
        system += (
            " SHADOW-ONLY PROFIT RETENTION RESEARCH: the supplied observed_peak_net_pnl "
            "comes from prior durable evaluation samples, not intraperiod market highs. "
            "Consider recent net-profit change and observed giveback when judging whether "
            "an already-profitable position should be closed now rather than held. "
            "A single sample provides no trend; do not infer unobserved prices or future "
            "returns, and do not introduce a fixed exit threshold or trailing-stop rule. "
            "If funding is incomplete or the evidence is weak, prefer ABSTAIN. "
            "Any CLOSE_PROFIT in SHADOW is only a recorded hypothesis, never an order."
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
    *,
    copy_state: CopyState,
) -> dict:
    economics = observation.economics
    return {
        "evaluation_slot": evaluation_slot,
        "copy_state": copy_state.value,
        "evaluation_basis": observation.basis,
        "pnl_complete": observation.pnl_complete,
        "funding_model": observation.funding_model,
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
        "residual_funding": str(economics.residual_funding) if economics else None,
        "estimated_exit_fee": str(economics.estimated_exit_fee) if economics else None,
        "net_pnl": str(economics.net_pnl) if economics else None,
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

    if mode is ProfitExitFeatureMode.SHADOW:
        # Reuse the same deterministic source-to-target sizing as RISEx
        # activation, with create_jobs=False. No provider order is emitted.
        try:
            await refresh_risex_shadow_targets(
                db, master_hl=master_hl, master_snapshot=master_snapshot,
                snapshot_started_order=snapshot_started_order,
            )
        except Exception:
            await db.rollback()
            log.warning(
                "RISEx shadow target refresh unavailable; proceeding with existing evidence",
                exc_info=True,
            )

    rows = (await db.execute(
        select(User, PositionLedger, TradingAccount, RiskProfile)
        .join(PositionLedger, PositionLedger.user_id == User.id)
        .outerjoin(TradingAccount, TradingAccount.user_id == User.id)
        .join(RiskProfile, RiskProfile.user_id == User.id)
        .where(
            User.state == UserState.ACTIVE,
            PositionLedger.managed.is_(True),
            or_(
                and_(
                    User.copy_state == CopyState.ACTIVE,
                    PositionLedger.size != 0,
                ),
                and_(
                    User.copy_state == CopyState.SHADOW,
                    PositionLedger.target_size != 0,
                ),
            ),
        )
        .order_by(User.id, PositionLedger.asset)
    )).all()

    evaluated = decisions = queued = abstained = 0
    shadow_candidates = shadow_profitable = shadow_decisions = 0
    learning_cache: dict[tuple[str, str, str, str], dict] = {}
    now = datetime.now(UTC)
    evaluation_slot = int(now.timestamp()) // settings.AI_PROFIT_EXIT_EVAL_SECONDS

    for user, ledger, account, risk in rows:
        evaluated += 1
        if is_master_source_user(user):
            abstained += 1
            continue
        try:
            destination = await user_network_state(db, user.id)
        except Exception:
            abstained += 1
            continue
        risex_account = None
        if destination.provider == "hyperliquid":
            if account is None:
                abstained += 1
                continue
        elif (
            destination.provider == "risex"
            and destination.network == "testnet"
            and mode is ProfitExitFeatureMode.SHADOW
            and user.copy_state == CopyState.SHADOW
        ):
            risex_account = (await db.execute(
                select(RISExTradingAccount).where(
                    RISExTradingAccount.user_id == user.id,
                )
            )).scalar_one_or_none()
            epoch = await db.get(ExecutionEpoch, destination.epoch_id)
            if (
                risex_account is None
                or epoch is None
                or epoch.user_id != user.id
                or epoch.ended_at is not None
                or epoch.provider != "risex"
                or epoch.network != "testnet"
                or not epoch.account_address
                or epoch.account_address.lower() != risex_account.account_address.lower()
            ):
                abstained += 1
                continue
        else:
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

        if user.copy_state == CopyState.ACTIVE:
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

        if destination.provider == "risex":
            # RISEx research always uses a fresh unauthenticated READ-ONLY
            # transport to the testnet. Never pass credentials or order clients.
            shadow_candidates += 1
            async with RISExReadOnlyHTTPTransport(
                base_url="https://api.testnet.rise.trade",
            ) as public_risex:
                observation = await collect_risex_shadow_profit_exit_economics(
                    public_risex,
                    asset=ledger.asset,
                    shadow_position=Decimal(str(ledger.target_size)),
                    master_perp_state=master_snapshot.perp_state,
                    slippage_bps=risk.max_slippage_bps,
                    network=destination.network,
                )
        else:
            follower_hl = follower_hl_for_network(destination.network)
            if follower_hl.limiter is None:
                abstained += 1
                continue
            if user.copy_state == CopyState.SHADOW:
                shadow_candidates += 1
                observation = await collect_shadow_profit_exit_economics(
                    follower_hl,
                    account_address=account.account_address,
                    asset=ledger.asset,
                    shadow_position=Decimal(str(ledger.target_size)),
                    master_perp_state=master_snapshot.perp_state,
                    slippage_bps=risk.max_slippage_bps,
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

        position_matches = (
            Decimal(str(ledger.target_size)) == observation.current_position
            if user.copy_state == CopyState.SHADOW
            else Decimal(str(ledger.size)) == observation.current_position
        )
        if mode is ProfitExitFeatureMode.SHADOW and user.copy_state == CopyState.SHADOW:
            # Record negative/zero PnL and incomplete prices even when the AI
            # must abstain. Only read-only market data was collected above.
            await record_shadow_observation(
                db,
                user_id=user.id,
                execution_epoch_id=destination.epoch_id,
                execution_provider=destination.provider,
                execution_network=destination.network,
                asset=ledger.asset,
                source_cycle_id=cycle.source_cycle_id,
                state_version=cycle.state_version,
                evaluation_slot=evaluation_slot,
                observation=observation,
                position_matches=position_matches,
            )

        if (
            not observation.complete
            or not observation.eligible
            or observation.current_position is None
            or observation.economics is None
            or observation.economics.net_pnl is None
            or observation.economics.net_pnl <= 0
            or not position_matches
        ):
            abstained += 1
            continue
        if user.copy_state == CopyState.SHADOW:
            shadow_profitable += 1

        inputs = _decision_inputs(
            observation,
            cycle,
            intelligence,
            evaluation_slot,
            copy_state=user.copy_state,
        )
        if destination.provider == "risex":
            inputs["estimated_fee_source"] = "risex_tier1_assumption_not_account_verified"
            inputs["actual_account_entry_verified"] = False
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
            evaluation_basis=observation.basis,
        )
        if await db.get(AIProfitExitDecision, decision_id) is not None:
            continue

        if _shadow_retention_allowed(mode=mode, follower_position=observation.current_position):
            # Research-only: historical samples never influence ON execution.
            # Match the exact source cycle/version and follower size to avoid
            # comparing PnL across scale-ins, reversals, or changed destinations.
            inputs["profit_retention"] = await read_retention_context(
                db,
                user_id=user.id,
                execution_epoch_id=destination.epoch_id,
                execution_provider=destination.provider,
                execution_network=destination.network,
                asset=ledger.asset,
                source_cycle_id=cycle.source_cycle_id,
                state_version=cycle.state_version,
                follower_position_size=observation.current_position,
                pnl_complete=observation.pnl_complete,
                current_net_pnl=observation.economics.net_pnl,
            )
            cache_key = (
                destination.provider, str(destination.network),
                ledger.asset, cycle.source_cycle_id,
            )
            if cache_key not in learning_cache:
                try:
                    learning_cache[cache_key] = await read_shadow_learning_feedback(
                        db,
                        provider=destination.provider,
                        network=str(destination.network),
                        asset=ledger.asset,
                        current_source_cycle_id=cycle.source_cycle_id,
                        now=now,
                    )
                except Exception:
                    log.warning(
                        "Shadow learning evidence unavailable; no adaptive hints applied",
                        extra={"provider": destination.provider, "asset": ledger.asset},
                        exc_info=True,
                    )
                    learning_cache[cache_key] = {
                        "version": LEARNING_VERSION, "status": "unavailable",
                        "policy_promotion": "disabled",
                    }
            inputs["learning_feedback"] = learning_cache[cache_key]

        action, reason, runtime = await _decide_with_ai(inputs)
        decision_now = datetime.now(UTC)
        operational = (
            mode is ProfitExitFeatureMode.ON
            and destination.provider == "hyperliquid"
            and user.copy_state == CopyState.ACTIVE
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
            pnl_complete=observation.pnl_complete,
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
        if user.copy_state == CopyState.SHADOW:
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
        "shadow_candidates": shadow_candidates,
        "shadow_profitable": shadow_profitable,
        "shadow_decisions": shadow_decisions,
    }
