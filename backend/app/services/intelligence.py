from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.llm import LLMRouter, LLMUnavailable
from app.adapters.ratelimit import Priority
from app.core.config import settings
from app.engine.capital_optimizer import (
    build_capital_candidates,
    choose_deterministic_candidate,
    recommended_capital_for_coverage,
)
from app.engine.sizing import EXCHANGE_MIN_NOTIONAL
from app.models.entities import CopyState, EquitySnapshot, MasterEvent, RiskProfile, User, UserState
from app.models.intelligence import CapitalIntelligenceDecision, MasterStrategyProfile
from app.services.master_learning import learn_master_strategy

SYSTEM_PROMPT = """You are Traxion Capital Intelligence, a supervisory portfolio-replication model.
You do not create orders, sizes, leverage, prices, or assets. A deterministic engine has already built safe candidate portfolios from the master strategy and follower capital. Select exactly one candidate_id from the supplied list. Optimize fidelity to the master while respecting capital efficiency, strategy persistence, tracking error and buffer. Return JSON only with keys: candidate_id, confidence (0..1), summary (max 240 chars), signals (array max 4). Never invent a candidate."""


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or '0'))


def _positions(perp_state: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in perp_state.get('assetPositions', []):
        position = row.get('position', row)
        size = _d(position.get('szi', '0'))
        if size != 0:
            out[str(position.get('coin') or '')] = size
    return out


def _candidate_prompt(candidates: list[dict], profile: dict, eligible_equity: Decimal, recommended_capital: Decimal) -> str:
    compact_candidates = [
        {
            'candidate_id': c['id'],
            'label': c['label'],
            'buffer_pct': c['buffer_pct'],
            'coverage_pct': c['coverage_pct'],
            'tracking_error_pct': c['tracking_error_pct'],
            'selected_positions': len(c['selected_assets']),
        }
        for c in candidates
    ]
    top_assets = sorted(
        (
            {'asset': asset, **values}
            for asset, values in (profile.get('assets') or {}).items()
        ),
        key=lambda x: (x.get('fills', 0), x.get('persistence_score', 0)),
        reverse=True,
    )[:12]
    payload = {
        'follower_eligible_equity_usd': str(eligible_equity),
        'recommended_capital_for_target_coverage_usd': str(recommended_capital),
        'target_coverage_pct': settings.LLM_RECOMMENDED_COVERAGE_PCT,
        'master_strategy': {
            'event_count': profile.get('event_count', 0),
            'asset_count': profile.get('asset_count', 0),
            'observed_days': profile.get('observed_days', 0),
            'median_event_interval_seconds': profile.get('median_event_interval_seconds'),
            'micro_fill_ratio': profile.get('micro_fill_ratio', 0),
            'top_learned_assets': top_assets,
        },
        'candidates': compact_candidates,
    }
    return json.dumps(payload, separators=(',', ':'), ensure_ascii=False)


async def _learn_profile(db: AsyncSession) -> MasterStrategyProfile:
    since = datetime.now(UTC) - timedelta(days=settings.LLM_STRATEGY_WINDOW_DAYS)
    # Master event IDs are network-prefixed by the watcher. Never train a
    # current mainnet profile on historical testnet behavior (or vice versa).
    events = (await db.execute(
        select(MasterEvent).where(
            MasterEvent.event_ts >= since,
            MasterEvent.exchange_event_id.like(f'{settings.master_network}:%'),
        ).order_by(MasterEvent.event_ts)
    )).scalars().all()
    learned = learn_master_strategy(events)
    row = (await db.execute(select(MasterStrategyProfile).where(
        MasterStrategyProfile.network == settings.master_network,
        MasterStrategyProfile.master_address == settings.HYPERLIQUID_MASTER_ADDRESS,
    ))).scalar_one_or_none()
    if not row:
        row = MasterStrategyProfile(
            network=settings.master_network,
            master_address=settings.HYPERLIQUID_MASTER_ADDRESS,
            window_days=settings.LLM_STRATEGY_WINDOW_DAYS,
        )
        db.add(row)
    row.window_days = settings.LLM_STRATEGY_WINDOW_DAYS
    row.event_count = int(learned.get('event_count', 0))
    row.asset_count = int(learned.get('asset_count', 0))
    row.profile = learned
    row.learned_at = datetime.now(UTC)
    await db.flush()
    return row


async def refresh_capital_intelligence(db: AsyncSession, master_hl) -> dict:
    """Refresh learned master behavior and one bounded policy decision per user."""
    profile_row = await _learn_profile(db)
    snapshot = await master_hl.account_snapshot(settings.HYPERLIQUID_MASTER_ADDRESS, priority=Priority.MASTER_STATE)
    master_positions = _positions(snapshot.perp_state)
    master_equity = snapshot.account_value
    master_mids = await master_hl.mids()

    # Capital efficiency must be based on markets the follower can actually
    # trade. This matters especially for mainnet-master -> testnet-follower
    # validation where the universes are intentionally different.
    follower_hl = HyperliquidAdapter(master_hl.limiter, network=settings.follower_network)
    follower_available: set[str] = set()
    for asset in master_positions:
        try:
            await follower_hl.asset_spec(asset)
        except KeyError:
            continue
        follower_available.add(asset)

    profile = profile_row.profile or {}
    persistence = {
        asset: values.get('persistence_score', 0.5)
        for asset, values in (profile.get('assets') or {}).items()
    }
    users = (await db.execute(select(User).where(
        User.state == UserState.ACTIVE,
        User.copy_state.in_([CopyState.SHADOW, CopyState.ACTIVE]),
    ))).scalars().all()
    router = LLMRouter()
    decisions = 0

    for user in users:
        risk = (await db.execute(select(RiskProfile).where(RiskProfile.user_id == user.id))).scalar_one_or_none()
        if not risk:
            continue
        equity_row = (await db.execute(select(EquitySnapshot).where(
            EquitySnapshot.user_id == user.id
        ).order_by(EquitySnapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
        if not equity_row:
            continue
        follower_equity = _d(equity_row.account_value)
        eligible_equity = max(follower_equity - _d(equity_row.unmanaged_margin), Decimal(0))
        if eligible_equity <= 0:
            continue

        allowed_positions = {
            asset: size for asset, size in master_positions.items()
            if asset in follower_available
            and (not risk.allow_assets or asset in risk.allow_assets)
            and asset not in risk.block_assets
        }
        floor = max(_d(risk.min_notional), EXCHANGE_MIN_NOTIONAL)
        candidates = build_capital_candidates(
            master_positions=allowed_positions,
            master_mids=master_mids,
            master_equity=master_equity,
            follower_equity=eligible_equity,
            multiplier=_d(risk.multiplier),
            min_notional=floor,
            persistence=persistence,
        )
        recommended = recommended_capital_for_coverage(
            master_positions=allowed_positions,
            master_mids=master_mids,
            master_equity=master_equity,
            multiplier=_d(risk.multiplier),
            min_notional=floor,
            target_coverage_pct=_d(settings.LLM_RECOMMENDED_COVERAGE_PCT),
        )
        deterministic = choose_deterministic_candidate(candidates, _d(settings.LLM_RECOMMENDED_COVERAGE_PCT))
        selected = deterministic
        provider = None
        model = None
        attempts: list[dict] = []
        confidence = None
        summary = 'Deterministic capital optimizer; LLM advisory unavailable or disabled.'
        status = 'DETERMINISTIC'

        if settings.LLM_ENABLED and settings.LLM_CAPITAL_MODE != 'off':
            valid = {c['id']: c for c in candidates}
            try:
                result = await router.complete_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=_candidate_prompt(candidates, profile, eligible_equity, recommended),
                    allowed_candidate_ids=set(valid),
                )
                attempts = result.attempts
                selected = valid[str(result.data.get('candidate_id'))]
                provider = result.provider
                model = result.model
                try:
                    confidence = max(Decimal(0), min(Decimal(1), _d(result.data.get('confidence', '0.5'))))
                except Exception:
                    confidence = Decimal('0.5')
                summary = str(result.data.get('summary') or '').strip()[:500] or f'{selected["label"]} selected by {model}'
                status = 'OK'
            except LLMUnavailable as exc:
                attempts = exc.attempts
                if settings.LLM_CAPITAL_MODE == 'active':
                    selected = next(c for c in candidates if c['id'] == 'exact')
                    summary = 'All LLM providers unavailable; exact-ratio fail-safe applied.'
                    status = 'FALLBACK_EXACT'
                else:
                    status = 'LLM_UNAVAILABLE'
            except Exception as exc:
                attempts.append({'provider': provider, 'model': model, 'status': 'invalid', 'error': f'{type(exc).__name__}: {str(exc)[:240]}'})
                if settings.LLM_CAPITAL_MODE == 'active':
                    selected = next(c for c in candidates if c['id'] == 'exact')
                    summary = 'LLM output rejected; exact-ratio fail-safe applied.'
                    status = 'FALLBACK_EXACT'
                else:
                    status = 'LLM_INVALID'

        policy = {
            'candidate_id': selected['id'],
            'candidate_label': selected['label'],
            'selected_assets': selected['selected_assets'],
            'buffer_pct': selected['buffer_pct'],
            'allocation_scale': selected.get('allocation_scale', '1'),
            # Snapshot weights are retained for audit/UI only. Live targeting
            # always re-applies the structural policy to current master state.
            'signed_equity_weights': selected['signed_equity_weights'],
            'coverage_pct': selected['coverage_pct'],
            'tracking_error_pct': selected['tracking_error_pct'],
            'risk_multiplier': str(risk.multiplier),
            'min_notional': str(floor),
            'master_network': settings.master_network,
            'follower_network': settings.follower_network,
            'follower_available_assets': sorted(follower_available),
            'strategy_profile_learned_at': profile_row.learned_at.isoformat(),
        }
        db.add(CapitalIntelligenceDecision(
            user_id=user.id,
            mode=settings.LLM_CAPITAL_MODE,
            status=status,
            provider=provider,
            model=model,
            candidate_id=selected['id'],
            confidence=confidence,
            follower_equity=follower_equity,
            eligible_equity=eligible_equity,
            recommended_capital=recommended,
            coverage_pct=_d(selected['coverage_pct']),
            tracking_error_pct=_d(selected['tracking_error_pct']),
            policy=policy,
            provider_attempts=attempts,
            summary=summary,
        ))
        decisions += 1

    await db.commit()
    return {
        'users': decisions,
        'master_events': profile_row.event_count,
        'master_assets': profile_row.asset_count,
        'follower_available_master_assets': len(follower_available),
    }


async def active_policy_for_user(db: AsyncSession, user_id, *, risk_multiplier: Decimal, min_notional: Decimal) -> dict | None:
    """Return a fresh bounded policy only when AI capital mode is ACTIVE.

    A stale decision or a changed risk profile is ignored, which reverts the
    execution path to the existing exact-ratio algorithm.
    """
    if settings.LLM_CAPITAL_MODE != 'active':
        return None
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.LLM_DECISION_MAX_AGE_SECONDS)
    row = (await db.execute(select(CapitalIntelligenceDecision).where(
        CapitalIntelligenceDecision.user_id == user_id,
        CapitalIntelligenceDecision.created_at >= cutoff,
    ).order_by(CapitalIntelligenceDecision.created_at.desc()).limit(1))).scalar_one_or_none()
    if not row:
        return None
    policy = row.policy or {}
    if _d(policy.get('risk_multiplier')) != _d(risk_multiplier):
        return None
    if _d(policy.get('min_notional')) != _d(min_notional):
        return None
    if policy.get('master_network') != settings.master_network or policy.get('follower_network') != settings.follower_network:
        return None
    if policy.get('candidate_id') not in {'exact', 'smart_fidelity', 'smart_balanced', 'smart_defensive'}:
        return None
    if not isinstance(policy.get('selected_assets'), list):
        return None
    return policy
