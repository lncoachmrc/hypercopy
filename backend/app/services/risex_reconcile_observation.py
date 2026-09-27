from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.engine.sizing import AssetSpec
from app.services.reconcile import (
    FollowerReconcileObservation,
    ReconcileObservationIndeterminate,
)
from app.services.risex_order_preparation import assert_risex_environment_allowed
from app.services.risex_risk_planning import (
    RISExRiskPlanningDenied,
    parse_risex_portfolio_details,
    parse_risex_risk_market,
)


_RISEX_TESTNET_API_URL = 'https://api.testnet.rise.trade'


def _root(payload: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    data = payload.get('data')
    if data is None:
        return payload
    if not isinstance(data, Mapping):
        raise ReconcileObservationIndeterminate(
            f'RISEx {field} response envelope is indeterminate'
        )
    return data


def _market_symbol(row: Mapping[str, Any]) -> str:
    config = row.get('config')
    config_name = config.get('name') if isinstance(config, Mapping) else None
    for raw in (
        row.get('base_asset_symbol'),
        row.get('display_base_asset_symbol'),
        row.get('display_name'),
        row.get('underlying'),
        config_name,
    ):
        if isinstance(raw, str) and raw.strip():
            symbol = raw.strip().upper().split('/', 1)[0]
            if symbol:
                return symbol
    raise ReconcileObservationIndeterminate(
        'RISEx market symbol is indeterminate'
    )


def _sz_decimals_from_step(step: Decimal) -> int:
    normalized = step.normalize()
    exponent = normalized.as_tuple().exponent
    decimals = max(0, -int(exponent))
    expected = Decimal(1).scaleb(-decimals)
    if step != expected:
        raise ReconcileObservationIndeterminate(
            f'RISEx step_size {step} cannot be represented by AssetSpec'
        )
    return decimals


def _required_decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReconcileObservationIndeterminate(
            f'RISEx {field} is indeterminate'
        ) from exc
    if not parsed.is_finite():
        raise ReconcileObservationIndeterminate(
            f'RISEx {field} is indeterminate'
        )
    return parsed


def _market_observation(
    markets_payload: Mapping[str, Any],
) -> tuple[
    dict[int, str],
    dict[str, Decimal],
    dict[str, AssetSpec],
]:
    root = _root(markets_payload, 'markets')
    rows = root.get('markets')
    if not isinstance(rows, list):
        raise ReconcileObservationIndeterminate(
            'RISEx markets are indeterminate'
        )

    market_to_asset: dict[int, str] = {}
    marks: dict[str, Decimal] = {}
    specs: dict[str, AssetSpec] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ReconcileObservationIndeterminate(
                'RISEx market row is indeterminate'
            )
        asset = _market_symbol(raw)
        try:
            market = parse_risex_risk_market(markets_payload, symbol=asset)
        except RISExRiskPlanningDenied as exc:
            raise ReconcileObservationIndeterminate(str(exc)) from exc
        if market.market_id in market_to_asset:
            raise ReconcileObservationIndeterminate(
                f'RISEx market_id {market.market_id} is duplicated'
            )
        if market.max_leverage != market.max_leverage.to_integral_value():
            raise ReconcileObservationIndeterminate(
                f'RISEx max_leverage for {asset} is not integral'
            )
        min_notional = market.min_order_size * market.mark_price
        market_to_asset[market.market_id] = asset
        marks[asset] = market.mark_price
        specs[asset] = AssetSpec(
            asset,
            sz_decimals=_sz_decimals_from_step(market.step_size),
            max_leverage=int(market.max_leverage),
            only_isolated=False,
            min_notional=min_notional,
        )
    return market_to_asset, marks, specs


def _build_observation(
    *,
    account_address: str,
    epoch_id: uuid.UUID,
    network: str,
    started_at: datetime,
    markets_payload: Mapping[str, Any],
    portfolio_payload: Mapping[str, Any],
) -> FollowerReconcileObservation:
    try:
        portfolio = parse_risex_portfolio_details(portfolio_payload)
    except RISExRiskPlanningDenied as exc:
        raise ReconcileObservationIndeterminate(str(exc)) from exc

    if portfolio.free_collateral is None:
        raise ReconcileObservationIndeterminate(
            'RISEx free_collateral/free margin is indeterminate'
        )

    market_to_asset, marks, specs = _market_observation(markets_payload)
    positions: dict[str, Decimal] = {}
    liquidation_prices: dict[str, Decimal | None] = {}
    for position in portfolio.positions:
        asset = market_to_asset.get(position.market_id)
        if asset is None:
            raise ReconcileObservationIndeterminate(
                f'RISEx position market_id {position.market_id} is unknown'
            )
        positions[asset] = position.size
        marks[asset] = position.mark_price
        liquidation_prices[asset] = position.liquidation_price

    root = _root(portfolio_payload, 'portfolio')
    summary = root.get('summary')
    if not isinstance(summary, Mapping):
        raise ReconcileObservationIndeterminate(
            'RISEx portfolio summary is indeterminate'
        )
    collateral_balance = _required_decimal(
        summary.get('collateral_margin_balance'),
        'collateral_margin_balance',
    )
    unrealized_pnl = _required_decimal(
        summary.get('total_unrealized_pnl'),
        'total_unrealized_pnl',
    )

    return FollowerReconcileObservation(
        provider='risex',
        network=network,
        epoch_id=epoch_id,
        started_at=started_at,
        account_address=account_address,
        account_equity=portfolio.total_account_value,
        free_margin=portfolio.free_collateral,
        collateral_balance=collateral_balance,
        unrealized_pnl=unrealized_pnl,
        account_mode='risex',
        positions=positions,
        marks=marks,
        liquidation_prices=liquidation_prices,
        unmanaged_margin=None,
        follower_configs={},
        asset_specs=specs,
        asset_spec_resolver=None,
        fills_synced=0,
    )


async def read_risex_reconcile_observation(
    *,
    account_address: str,
    epoch_id: uuid.UUID,
    network: str = 'testnet',
    started_at: datetime | None = None,
    api: Any | None = None,
) -> FollowerReconcileObservation:
    observed_at = started_at or datetime.now(UTC)

    async def read_with(client: Any) -> FollowerReconcileObservation:
        markets_payload = await client.get_json('/v1/markets')
        assert_risex_environment_allowed(
            network=network,  # type: ignore[arg-type]
            env=os.environ,
        )
        portfolio_payload = await client.get_json(
            '/v1/portfolio/details',
            params={'account': account_address},
        )
        return _build_observation(
            account_address=account_address,
            epoch_id=epoch_id,
            network=network,
            started_at=observed_at,
            markets_payload=markets_payload,
            portfolio_payload=portfolio_payload,
        )

    if api is not None:
        return await read_with(api)

    async with RISExReadOnlyHTTPTransport(
        base_url=_RISEX_TESTNET_API_URL
    ) as owned_api:
        return await read_with(owned_api)
