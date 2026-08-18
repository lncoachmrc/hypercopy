from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.entities import MasterEvent
from app.services.ai_intelligence import _validated_analysis, learn_master_strategy, provider_chain


def event(asset: str, minutes: int, start: str, after: str, size: str = '1', price: str = '20') -> MasterEvent:
    return MasterEvent(
        exchange_event_id=f'{asset}-{minutes}-{start}-{after}',
        asset=asset,
        side='B',
        size=Decimal(size),
        price=Decimal(price),
        start_position=Decimal(start),
        position_after=Decimal(after),
        master_equity=Decimal('1000'),
        event_ts=datetime(2026, 8, 1, tzinfo=UTC) + timedelta(minutes=minutes),
        raw={},
        fencing_token=1,
    )


def test_master_learning_detects_scale_in_and_holding_time():
    profile = learn_master_strategy([
        event('BTC', 0, '0', '1'),
        event('BTC', 5, '1', '2'),
        event('BTC', 65, '2', '0', size='2'),
    ])
    assert profile['lookback_events'] == 3
    assert profile['assets_traded'] == 1
    assert profile['median_completed_hold_minutes'] == 65
    assert profile['top_assets'][0]['scale_ins'] == 1


def test_analysis_policy_is_clamped_and_enum_safe():
    result = _validated_analysis({
        'summary': 'x',
        'observed_patterns': ['a'],
        'confidence': 5,
        'capital_policy': {
            'buffer_pct': 0.99,
            'minimum_coverage_pct': 0.1,
            'preferred_coverage_pct': 4,
            'micro_position_policy': 'invent',
            'rebalance_urgency': 'panic',
        },
    })
    assert result['capital_policy']['buffer_pct'] == 0.30
    assert result['capital_policy']['minimum_coverage_pct'] == 0.60
    assert result['capital_policy']['preferred_coverage_pct'] == 0.99
    assert result['capital_policy']['micro_position_policy'] == 'ignore_until_executable'
    assert result['capital_policy']['rebalance_urgency'] == 'medium'
    assert result['confidence'] == 1.0


def test_provider_chain_honors_preference_and_fallback(monkeypatch):
    monkeypatch.setenv('LLM_PREFERRED_MODEL', 'anthropic:claude-test')
    monkeypatch.setenv('LLM_FALLBACK_MODELS', 'openai:gpt-test,deepseek:deepseek-chat')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-test')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'claude-test')
    monkeypatch.setenv('DEEPSEEK_MODEL', 'deepseek-chat')
    assert provider_chain()[:3] == [
        ('anthropic', 'claude-test'),
        ('openai', 'gpt-test'),
        ('deepseek', 'deepseek-chat'),
    ]

def test_analysis_accepts_object_observed_patterns():
    result = _validated_analysis({
        'summary': 'x',
        'observed_patterns': {
            'scaling': 'frequent',
            'micro_positions': 'limited',
        },
        'confidence': 0.7,
        'capital_policy': {},
    })

    assert result['observed_patterns'] == [
        'scaling: frequent',
        'micro_positions: limited',
    ]
