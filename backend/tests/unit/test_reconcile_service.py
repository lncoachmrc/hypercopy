from decimal import Decimal

from app.models.entities import CopyState
from app.services.reconcile import _reconciliation_basis

D = Decimal


def test_shadow_basis_defaults_missing_previous_target_to_zero():
    assert _reconciliation_basis(CopyState.SHADOW, None, D('1.25')) == D('0')


def test_shadow_basis_preserves_previous_target():
    assert _reconciliation_basis(CopyState.SHADOW, D('0.75'), D('1.25')) == D('0.75')


def test_active_basis_uses_real_position():
    assert _reconciliation_basis(CopyState.ACTIVE, None, D('1.25')) == D('1.25')
