from decimal import Decimal
from app.engine.reconcile import Discrepancy, classify

D=Decimal

def test_exact_target_has_no_job():
    d=classify(D('1'),D('1')); assert d.kind==Discrepancy.NONE and not d.needs_job

def test_sign_flip_requires_job():
    d=classify(D('1'),D('-1')); assert d.kind==Discrepancy.SIGN_FLIP and d.needs_job

def test_over_under_exposure():
    assert classify(D('1.2'),D('1')).kind==Discrepancy.OVEREXPOSURE
    assert classify(D('0.7'),D('1')).kind==Discrepancy.UNDEREXPOSURE

def test_zero_target_orphan():
    assert classify(D('0.5'),D('0')).kind==Discrepancy.ORPHAN_POSITION
