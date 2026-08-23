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


def test_degraded_ai_freezes_last_safe_factor_instead_of_increasing_exposure():
    policy = derive_ai_execution_policy(
        state(status='degraded'),
        requested=True,
        fallback_factor=Decimal('0.88'),
    )
    assert policy.requested_mode == 'on'
    assert policy.effective_mode == 'shadow'
    assert policy.effective is False
    assert policy.factor == Decimal('0.88')
    assert policy.buffer_pct == Decimal('0.12')
    assert 'degraded' in (policy.fallback_reason or '')


def test_missing_capital_policy_keeps_bounded_last_safe_factor():
    policy = derive_ai_execution_policy(
        {'status': 'ok'},
        requested=True,
        fallback_factor=Decimal('0.65'),
    )
    assert policy.effective is False
    assert policy.factor == Decimal('0.70')
    assert policy.buffer_pct == Decimal('0.30')
    assert policy.fallback_reason


def test_shadow_explicitly_restores_deterministic_factor_even_after_ai_on():
    policy = derive_ai_execution_policy(
        state(status='degraded'),
        requested=False,
        fallback_factor=Decimal('0.82'),
    )
    assert policy.effective_mode == 'shadow'
    assert policy.factor == Decimal('1')
    assert policy.buffer_pct == Decimal('0')
