from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Discrepancy(str, Enum):
    NONE = 'NONE'
    MISSING_EXECUTION = 'MISSING_EXECUTION'
    OVEREXPOSURE = 'OVEREXPOSURE'
    UNDEREXPOSURE = 'UNDEREXPOSURE'
    ORPHAN_POSITION = 'ORPHAN_POSITION'
    SIGN_FLIP = 'SIGN_FLIP'


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    kind: Discrepancy
    drift_pct: Decimal
    needs_job: bool


def classify(real: Decimal, target: Decimal, *, tolerance_pct: Decimal = Decimal('5')) -> ReconcileDecision:
    if real == target:
        return ReconcileDecision(Discrepancy.NONE, Decimal(0), False)
    if target == 0:
        return ReconcileDecision(Discrepancy.ORPHAN_POSITION, Decimal('100'), real != 0)
    if real != 0 and (real > 0) != (target > 0):
        return ReconcileDecision(Discrepancy.SIGN_FLIP, Decimal('100'), True)
    drift = abs(real - target) / max(abs(target), Decimal('0.00000001')) * Decimal(100)
    if drift <= tolerance_pct:
        return ReconcileDecision(Discrepancy.NONE, drift, False)
    if abs(real) > abs(target):
        return ReconcileDecision(Discrepancy.OVEREXPOSURE, drift, True)
    return ReconcileDecision(Discrepancy.UNDEREXPOSURE, drift, True)
