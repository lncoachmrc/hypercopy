from decimal import Decimal

from app.models.entities import CopyState
from app.services.reconcile import _reconciliation_basis

D = Decimal


def test_shadow_basis_starts_flat_without_virtual_position():
    assert _reconciliation_basis(
        CopyState.SHADOW,
        D('0.75'),
        D('1.25'),
        shadow_current=None,
    ) == D('0')


def test_shadow_basis_uses_virtual_position_not_previous_target_or_exchange():
    assert _reconciliation_basis(
        CopyState.SHADOW,
        D('0.75'),
        D('1.25'),
        shadow_current=D('0.40'),
    ) == D('0.40')


def test_active_basis_uses_real_position():
    assert _reconciliation_basis(
        CopyState.ACTIVE,
        None,
        D('1.25'),
        shadow_current=D('0.40'),
    ) == D('1.25')
