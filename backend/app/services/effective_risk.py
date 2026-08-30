from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EffectiveRisk:
    """Runtime risk limits after commercial and exchange caps are applied.

    RiskProfile remains the user's selected configuration. This value object is
    intentionally ephemeral so plan upgrades/downgrades take effect immediately
    without rewriting or losing the user's strategy preferences.
    """

    multiplier: Decimal
    max_notional_per_trade: Decimal
    max_total_exposure: Decimal
    max_asset_exposure: Decimal
    max_leverage: Decimal
    max_positions: int


def _decimal_cap(selected: Decimal, limits: dict[str, Any], key: str) -> Decimal:
    raw = limits.get(key)
    if raw is None:
        return Decimal(str(selected))
    return min(Decimal(str(selected)), Decimal(str(raw)))


def resolve_effective_risk(
    risk,
    entitlement_data: dict[str, Any] | None,
    *,
    exchange_max_leverage: int | Decimal | None = None,
) -> EffectiveRisk:
    """Resolve the risk values that may actually reach execution.

    Commercial limits constrain multiplier, per-trade notional and position
    count. Exchange leverage is an independent technical ceiling. Other user
    risk limits remain exactly as selected. Entitlement validity itself stays a
    separate RiskContext gate so reductions/closures remain possible when a
    subscription becomes non-entitled.
    """

    limits = dict((entitlement_data or {}).get('limits') or {})
    selected_positions = int(risk.max_positions)
    max_positions = selected_positions
    if limits.get('max_positions') is not None:
        max_positions = min(selected_positions, int(limits['max_positions']))

    max_leverage = Decimal(str(risk.max_leverage))
    if exchange_max_leverage is not None:
        max_leverage = min(max_leverage, Decimal(str(exchange_max_leverage)))

    return EffectiveRisk(
        multiplier=_decimal_cap(risk.multiplier, limits, 'max_multiplier'),
        max_notional_per_trade=_decimal_cap(
            risk.max_notional_per_trade,
            limits,
            'max_notional_per_trade',
        ),
        max_total_exposure=Decimal(str(risk.max_total_exposure)),
        max_asset_exposure=Decimal(str(risk.max_asset_exposure)),
        max_leverage=max_leverage,
        max_positions=max_positions,
    )
