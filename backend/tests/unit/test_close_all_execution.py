"""Regression coverage for explicit Hyperliquid close-all execution."""

from decimal import Decimal

from app.models.entities import CopyState
from app.services.execution import _effective_master_mark, _shadow_suppresses_exchange


def test_close_all_uses_follower_mark_across_networks():
    assert _effective_master_mark('CLOSE_ALL', Decimal('0'), Decimal('123.45'), False) == Decimal('123.45')


def test_regular_cross_network_job_still_requires_master_mark():
    assert _effective_master_mark('RECONCILE', Decimal('0'), Decimal('123.45'), False) == Decimal('0')


def test_close_all_is_not_suppressed_by_shadow_mode():
    assert _shadow_suppresses_exchange(CopyState.SHADOW, 'CLOSE_ALL') is False
    assert _shadow_suppresses_exchange(CopyState.SHADOW, 'RECONCILE') is True
