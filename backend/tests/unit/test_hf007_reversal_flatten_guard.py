from decimal import Decimal

import pytest

from app.services.execution import close_leg_flattened


@pytest.mark.parametrize(
    ("requested", "filled", "residual", "expected"),
    [
        (Decimal("1"), Decimal("1"), Decimal("0"), True),
        (Decimal("1"), Decimal("0.4"), Decimal("0.6"), False),
        (Decimal("1"), Decimal("0"), Decimal("1"), False),
        (Decimal("1"), None, Decimal("0"), False),
        (Decimal("1"), Decimal("1"), Decimal("0.002"), False),
        (Decimal("1"), Decimal("1"), Decimal("-0.002"), False),
        (Decimal("1"), Decimal("0.999"), Decimal("0"), False),
    ],
)
def test_close_leg_flattened_requires_exchange_and_ledger_confirmation(
    requested: Decimal,
    filled: Decimal | None,
    residual: Decimal,
    expected: bool,
):
    assert close_leg_flattened(requested, filled, residual, 3) is expected


@pytest.mark.parametrize("sz_decimals", [0, 1, 2, 3, 5])
def test_close_leg_flattened_accepts_residual_strictly_below_one_lot(sz_decimals: int):
    lot = Decimal(1).scaleb(-sz_decimals)

    assert close_leg_flattened(
        Decimal("1"),
        Decimal("1"),
        lot / Decimal("10"),
        sz_decimals,
    ) is True


@pytest.mark.parametrize("sz_decimals", [0, 1, 2, 3, 5])
def test_close_leg_flattened_rejects_residual_exactly_at_one_lot(sz_decimals: int):
    lot = Decimal(1).scaleb(-sz_decimals)

    assert close_leg_flattened(
        Decimal("1"),
        Decimal("1"),
        lot,
        sz_decimals,
    ) is False
