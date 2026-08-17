from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import settings
from app.db.session import get_db
from app.models.entities import User
from app.models.intelligence import CapitalIntelligenceDecision, MasterStrategyProfile

router = APIRouter(tags=['intelligence'])


@router.get('/intelligence')
async def intelligence_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    decision = (await db.execute(select(CapitalIntelligenceDecision).where(
        CapitalIntelligenceDecision.user_id == user.id,
    ).order_by(CapitalIntelligenceDecision.created_at.desc()).limit(1))).scalar_one_or_none()
    profile = (await db.execute(select(MasterStrategyProfile).where(
        MasterStrategyProfile.network == settings.master_network,
        MasterStrategyProfile.master_address == settings.HYPERLIQUID_MASTER_ADDRESS,
    ).order_by(MasterStrategyProfile.learned_at.desc()).limit(1))).scalar_one_or_none()

    strategy = None
    if profile:
        learned = profile.profile or {}
        top_assets = sorted(
            (
                {'asset': asset, **values}
                for asset, values in (learned.get('assets') or {}).items()
            ),
            key=lambda x: (x.get('fills', 0), x.get('persistence_score', 0)),
            reverse=True,
        )[:6]
        strategy = {
            'event_count': profile.event_count,
            'asset_count': profile.asset_count,
            'window_days': profile.window_days,
            'observed_days': learned.get('observed_days'),
            'micro_fill_ratio': learned.get('micro_fill_ratio'),
            'median_event_interval_seconds': learned.get('median_event_interval_seconds'),
            'top_assets': top_assets,
            'learned_at': profile.learned_at,
        }

    last = None
    if decision:
        attempts = decision.provider_attempts or []
        last = {
            'status': decision.status,
            'provider': decision.provider,
            'model': decision.model,
            'candidate_id': decision.candidate_id,
            'candidate_label': (decision.policy or {}).get('candidate_label'),
            'confidence': decision.confidence,
            'follower_equity': decision.follower_equity,
            'eligible_equity': decision.eligible_equity,
            'recommended_capital': decision.recommended_capital,
            'coverage_pct': decision.coverage_pct,
            'tracking_error_pct': decision.tracking_error_pct,
            'buffer_pct': (decision.policy or {}).get('buffer_pct'),
            'selected_positions': len((decision.policy or {}).get('selected_assets') or []),
            'summary': decision.summary,
            'fallback_count': sum(1 for x in attempts if x.get('status') == 'failed'),
            'attempts': [
                {'provider': x.get('provider'), 'model': x.get('model'), 'status': x.get('status')}
                for x in attempts
            ],
            'created_at': decision.created_at,
        }

    return {
        'enabled': settings.LLM_ENABLED,
        'mode': settings.LLM_CAPITAL_MODE,
        'preferred_model': settings.LLM_PREFERRED_MODEL,
        'models': {
            'openai': settings.OPENAI_MODEL,
            'anthropic': settings.ANTHROPIC_MODEL,
            'deepseek': settings.DEEPSEEK_MODEL,
        },
        'provider_order': [x.strip() for x in settings.LLM_PROVIDER_ORDER.split(',') if x.strip()],
        'last': last,
        'strategy': strategy,
    }
