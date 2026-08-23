from decimal import Decimal

from app.services.ai_mode import derive_ai_execution_policy


def state(*, status='ok', buffer_pct=0.10):
    return {
        'status': status,
        'analysis': {
            'capital_policy': {
                'buffer_pct': buffer_pct,
            },
        },
    }


def test_shadow_mode_has_no_execution_influence():
    policy = derive_ai_execution_policy(state(), requested=False)
    assert policy.requested_mode == 'shadow'
    assert policy.effective_mode == 'shadow'
    assert policy.effective is False
    assert policy.factor == Decimal('1')


def test_on_mode_can_only_reduce_deterministic_capital_allocation():
    policy = derive_ai_execution_policy(state(buffer_pct=0.10), requested=True)
    assert policy.requested_mode == 'on'
    assert policy.effective_mode == 'on'
    assert policy.effective is True
    assert policy.buffer_pct == Decimal('0.1')
    assert policy.factor == Decimal('0.9')
    assert policy.factor <= Decimal('1')


def test_on_mode_clamps_ai_buffer_to_thirty_percent():
    policy = derive_ai_execution_policy(state(buffer_pct=0.99), requested=True)
    assert policy.buffer_pct == Decimal('0.30')
    assert policy.factor == Decimal('0.70')


def test_degraded_ai_falls_back_to_shadow_effectively():
    policy = derive_ai_execution_policy(state(status='degraded'), requested=True)
    assert policy.requested_mode == 'on'
    assert policy.effective_mode == 'shadow'
    assert policy.effective is False
    assert policy.factor == Decimal('1')
    assert 'degraded' in (policy.fallback_reason or '')


def test_missing_capital_policy_falls_back_to_shadow():
    policy = derive_ai_execution_policy({'status': 'ok'}, requested=True)
    assert policy.effective is False
    assert policy.factor == Decimal('1')
    assert policy.fallback_reason
