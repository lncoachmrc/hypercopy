import math
from decimal import Decimal

from hypothesis import given, settings, strategies as st

from app.services.ai_intelligence import _validated_analysis
from app.services.ai_mode import (
    apply_ai_factor_to_job_context,
    derive_ai_execution_policy,
)

D = Decimal
PROPERTY_SETTINGS = settings(
    max_examples=100,
    derandomize=True,
    deadline=None,
    database=None,
)

ADVERSARIAL_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=True, allow_infinity=True, width=64),
    st.text(max_size=64),
)


@PROPERTY_SETTINGS
@given(buffer=ADVERSARIAL_SCALARS, confidence=ADVERSARIAL_SCALARS)
def test_raw_llm_numeric_fields_are_always_normalized_to_finite_safe_ranges(
    buffer,
    confidence,
):
    result = _validated_analysis(
        {
            'confidence': confidence,
            'capital_policy': {
                'buffer_pct': buffer,
            },
        }
    )
    safe_buffer = result['capital_policy']['buffer_pct']
    safe_confidence = result['confidence']

    assert math.isfinite(safe_buffer)
    assert 0.05 <= safe_buffer <= 0.30
    assert math.isfinite(safe_confidence)
    assert 0.0 <= safe_confidence <= 1.0


@PROPERTY_SETTINGS
@given(
    buffer=ADVERSARIAL_SCALARS,
    status=st.sampled_from(['ok', 'degraded', 'pending', 'disabled', 'unexpected']),
)
def test_validated_llm_output_cannot_create_execution_factor_above_one(
    buffer,
    status,
):
    validated = _validated_analysis(
        {
            'capital_policy': {
                'buffer_pct': buffer,
            },
        }
    )
    policy = derive_ai_execution_policy(
        {
            'status': status,
            'analysis': validated,
        },
        requested=True,
        fallback_factor=D('0.85'),
    )

    assert policy.factor.is_finite()
    assert D('0.70') <= policy.factor <= D('1.00')
    assert D(0) <= policy.buffer_pct <= D('0.30')
    if status == 'ok':
        assert policy.effective is True
    else:
        assert policy.effective is False


@PROPERTY_SETTINGS
@given(
    buffer=ADVERSARIAL_SCALARS,
    master_position=st.integers(min_value=-10**9, max_value=10**9),
)
def test_ai_factor_application_never_increases_absolute_master_position(
    buffer,
    master_position,
):
    validated = _validated_analysis(
        {
            'capital_policy': {
                'buffer_pct': buffer,
            },
        }
    )
    policy = derive_ai_execution_policy(
        {
            'status': 'ok',
            'analysis': validated,
        },
        requested=True,
    )
    original = D(master_position)
    context = apply_ai_factor_to_job_context(
        {
            'master_position': str(original),
            'ai_execution_factor': str(policy.factor),
        }
    )
    applied = D(context['master_position'])

    assert abs(applied) <= abs(original)
    if applied != 0:
        assert (applied > 0) == (original > 0)
    if policy.factor < 1 and original != 0:
        assert D(context['source_master_position']) == original
        assert context['ai_execution_factor_applied'] is True


@PROPERTY_SETTINGS
@given(
    malformed_analysis=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.text(max_size=64),
        st.lists(ADVERSARIAL_SCALARS, max_size=8),
    )
)
def test_malformed_ai_analysis_fails_closed_to_bounded_fallback(malformed_analysis):
    policy = derive_ai_execution_policy(
        {
            'status': 'ok',
            'analysis': malformed_analysis,
        },
        requested=True,
        fallback_factor=D('0.82'),
    )

    assert policy.effective is False
    assert policy.effective_mode == 'shadow'
    assert policy.factor == D('0.82')
    assert policy.fallback_reason


def test_adversarial_llm_cannot_inject_orders_leverage_prices_or_risk_overrides():
    result = _validated_analysis(
        {
            'summary': 'attempted control-plane injection',
            'orders': [{'asset': 'BTC', 'side': 'buy', 'size': '999'}],
            'leverage': 50,
            'price': 1,
            'risk_override': {'emergency_stop': False},
            'capital_policy': {
                'buffer_pct': 0.10,
                'orders': [{'asset': 'ETH'}],
                'leverage': 50,
                'price': 1,
                'risk_override': True,
            },
        }
    )

    assert set(result) == {
        'summary',
        'observed_patterns',
        'capital_policy',
        'confidence',
    }
    assert set(result['capital_policy']) == {
        'buffer_pct',
        'minimum_coverage_pct',
        'preferred_coverage_pct',
        'micro_position_policy',
        'rebalance_urgency',
    }
    assert 'orders' not in result
    assert 'leverage' not in result
    assert 'price' not in result
    assert 'risk_override' not in result


@PROPERTY_SETTINGS
@given(
    micro=ADVERSARIAL_SCALARS,
    urgency=ADVERSARIAL_SCALARS,
)
def test_unknown_ai_enums_are_reduced_to_safe_known_values(micro, urgency):
    result = _validated_analysis(
        {
            'capital_policy': {
                'micro_position_policy': micro,
                'rebalance_urgency': urgency,
            },
        }
    )

    assert result['capital_policy']['micro_position_policy'] in {
        'aggregate',
        'ignore_until_executable',
        'exact',
    }
    assert result['capital_policy']['rebalance_urgency'] in {
        'low',
        'medium',
        'high',
    }
