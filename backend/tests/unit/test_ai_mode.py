from decimal import Decimal

from app.services.ai_mode import apply_ai_factor_to_job_context, derive_ai_execution_policy


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


def test_degraded_ai_never_uses_stale_factor_above_latest_valid_analysis():
    policy = derive_ai_execution_policy(
        state(status='degraded', buffer_pct=0.30),
        requested=True,
        fallback_factor=Decimal('0.90'),
    )
    assert policy.effective is False
    assert policy.factor == Decimal('0.70')
    assert policy.buffer_pct == Decimal('0.30')


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


def test_ai_factor_is_persisted_into_execution_master_position():
    context = apply_ai_factor_to_job_context({
        'master_position': '2',
        'ai_execution_factor': '0.75',
        'ai_target_without_influence': '1.5',
    })
    assert context['source_master_position'] == '2'
    assert context['master_position'] == '1.50'
    assert context['ai_execution_factor_applied'] is True


def test_ai_factor_one_leaves_execution_context_unchanged():
    original = {'master_position': '2', 'ai_execution_factor': '1'}
    assert apply_ai_factor_to_job_context(original) == original
