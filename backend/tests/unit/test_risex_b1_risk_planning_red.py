"""RED contracts for RISEx b1: read-only Risk Engine planning.

This slice must decide *whether* and *how much* RISEx may trade before any
permit, signature, transport, durable pre-POST record, or provider write exists.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.engine.risk import RiskAction
from app.services import risex_order_preparation
from app.services.effective_risk import resolve_effective_risk


def _planning_module():
    try:
        return importlib.import_module("app.services.risex_risk_planning")
    except ModuleNotFoundError:
        pytest.fail(
            "RED: app.services.risex_risk_planning is required for provider-read-only "
            "RISEx Risk Engine planning"
        )


def _market_payload(
    *,
    active: bool = True,
    reduce_only: bool = False,
    max_position_size: str = "100000000",
    maintenance_margin_factor: str = "75",
    max_leverage: str = "50",
) -> dict:
    return {
        "data": {
            "markets": [
                {
                    "market_id": "1",
                    "config": {
                        "name": "BTC/USDC",
                        "step_size": "0.000001",
                        "step_price": "0.1",
                        "maintenance_margin_factor": maintenance_margin_factor,
                        "max_leverage": max_leverage,
                        "min_order_size": "0.0001",
                        "unlocked": True,
                        "open_interest_limit": "0",
                    },
                    "base_asset_symbol": "BTC/USDC",
                    "quote_asset_symbol": "USDC",
                    "underlying": "BTC/USDC",
                    "display_name": "BTC/USDC",
                    "mark_price": "86192.08",
                    "max_position_size": max_position_size,
                    "open_interest": "14957.018174",
                    "post_only": False,
                    "reduce_only": reduce_only,
                    "active": active,
                }
            ]
        }
    }


def _portfolio_payload(
    *,
    free_collateral: str = "2000.857",
    total_account_value: str = "2001.20",
    total_notional: str = "17.238",
    in_liquidation: bool = False,
    risk_level: str = "NORMAL",
    size: str = "0.0002",
) -> dict:
    return {
        "data": {
            "summary": {
                "usdc_balance": "2000",
                "collateral_margin_balance": "2000",
                "total_account_value": total_account_value,
                "total_notional": total_notional,
                "total_unrealized_pnl": "0.959",
                "free_collateral": free_collateral,
                "in_liquidation": in_liquidation,
                "risk_level": risk_level,
            },
            "positions": [
                {
                    "market_id": "1",
                    "size": size,
                    "mark_price": "86192.08",
                    "avg_entry_price": "81397.08",
                    "liquidation_price": "50000",
                }
            ],
        }
    }


def _risk(**overrides):
    values = {
        "multiplier": Decimal("1"),
        "max_notional_per_trade": Decimal("1000000"),
        "max_total_exposure": Decimal("2000000"),
        "max_asset_exposure": Decimal("2000000"),
        "max_leverage": Decimal("75"),
        "max_positions": 50,
        "max_drawdown_pct": Decimal("20"),
        "max_daily_loss_pct": Decimal("10"),
        "min_notional": Decimal("10"),
        "max_slippage_bps": 25,
        "close_only": False,
        "allow_assets": [],
        "block_assets": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _entitlement() -> dict:
    return {"entitled": True, "limits": {}}


def _runtime(module, **overrides):
    values = {
        "user_active": True,
        "credential_active": True,
        "user_paused": False,
        "global_pause": False,
        "emergency_stop": False,
        "drawdown_halt": False,
        "daily_loss_halt": False,
        "asset_allowed": True,
    }
    values.update(overrides)
    runtime_type = getattr(module, "RISExRuntimeRiskFlags", None)
    assert runtime_type is not None, "RED: immutable RISExRuntimeRiskFlags is required"
    return runtime_type(**values)


def _job(*, master_position: str, master_equity: str = "1000", master_mark: str = "100"):
    return SimpleNamespace(
        id=__import__("uuid").UUID("12345678-1234-5678-1234-567812345678"),
        asset="BTC",
        origin="EVENT",
        context={
            "master_position": master_position,
            "master_equity": master_equity,
            "master_mark_price": master_mark,
            "mark_price": master_mark,
            "follower_network": "testnet",
        },
    )


@pytest.mark.asyncio
async def test_existing_market_metadata_exposes_verified_risk_fields() -> None:
    class ReadOnlyAPI:
        public_read_only = True

        async def get_json(self, path, params=None):
            assert path == "/v1/markets"
            assert params is None
            return _market_payload()

    market = await risex_order_preparation.resolve_market_metadata(
        ReadOnlyAPI(),
        symbol="BTC",
    )

    assert market.max_leverage == Decimal("50")
    assert market.active is True
    assert market.reduce_only is False
    assert market.max_position_size_raw == Decimal("100000000")
    assert market.mark_price == Decimal("86192.08")
    assert market.maintenance_margin_factor_raw == "75"


def test_b1_module_has_no_provider_write_or_signing_dependencies() -> None:
    module = _planning_module()
    source = inspect.getsource(module)

    for forbidden in (
        "RISExAdapter",
        "prepare_place_order_permit",
        "prepare_place_order_request",
        "RISExSignedTestnetHTTPTransport",
        "load_testnet_signer_credential",
        "private_key",
        ".place_ioc(",
        ".post(",
        ".commit(",
        ".flush(",
    ):
        assert forbidden not in source, (
            f"RED: b1 is read-only/pure and must not contain execution primitive {forbidden!r}"
        )


def test_real_portfolio_payload_maps_exactly_into_risk_snapshot() -> None:
    module = _planning_module()
    parse = getattr(module, "parse_risex_portfolio_details", None)
    assert callable(parse), "RED: strict /v1/portfolio/details parser is required"

    snapshot = parse(_portfolio_payload())

    assert snapshot.usdc_balance == Decimal("2000")
    assert snapshot.total_account_value == Decimal("2001.20")
    assert snapshot.total_notional == Decimal("17.238")
    assert snapshot.free_collateral == Decimal("2000.857")
    assert snapshot.in_liquidation is False
    assert snapshot.risk_level == "NORMAL"

    # Runtime semantic validation from the real testnet payload:
    # equity - free collateral ~= notional / market max leverage.
    reserved_margin = snapshot.total_account_value - snapshot.free_collateral
    theoretical = snapshot.total_notional / Decimal("50")
    assert reserved_margin == Decimal("0.343")
    assert abs(reserved_margin - theoretical) < Decimal("0.002")


def test_risex_risk_context_uses_provider_truth_and_exchange_leverage_cap() -> None:
    module = _planning_module()
    parse_portfolio = getattr(module, "parse_risex_portfolio_details", None)
    parse_market = getattr(module, "parse_risex_risk_market", None)
    build_context = getattr(module, "build_risex_risk_context", None)
    assert callable(parse_portfolio)
    assert callable(parse_market)
    assert callable(build_context)

    portfolio = parse_portfolio(_portfolio_payload())
    market = parse_market(_market_payload(), symbol="BTC")
    effective = resolve_effective_risk(
        _risk(max_leverage=Decimal("75")),
        _entitlement(),
        exchange_max_leverage=market.max_leverage,
    )
    ctx = build_context(
        portfolio=portfolio,
        market=market,
        effective_risk=effective,
        runtime=_runtime(module),
    )

    expected_asset_exposure = Decimal("0.0002") * Decimal("86192.08")
    assert ctx.account_equity == Decimal("2001.20")
    assert ctx.current_total_exposure == Decimal("17.238")
    assert ctx.current_asset_exposure == expected_asset_exposure
    assert ctx.free_margin == Decimal("2000.857")
    assert ctx.current_leverage == Decimal("17.238") / Decimal("2001.20")
    assert ctx.open_positions == 1
    assert ctx.is_new_market is False
    assert ctx.max_leverage == Decimal("50")


@pytest.mark.parametrize("bad_free_collateral", [None, "", "nan", "-1", "not-a-number"])
def test_unknown_or_invalid_free_collateral_denies_exposure_increase(
    bad_free_collateral,
) -> None:
    module = _planning_module()
    plan_intent = getattr(module, "plan_risex_order_intent", None)
    denied = getattr(module, "RISExRiskPlanningDenied", RuntimeError)
    assert callable(plan_intent)

    payload = _portfolio_payload()
    payload["data"]["summary"]["free_collateral"] = bad_free_collateral

    with pytest.raises(denied, match="free.?collateral|free.?margin|indeterminate"):
        plan_intent(
            job=_job(master_position="1"),
            portfolio_payload=payload,
            markets_payload=_market_payload(),
            risk=_risk(),
            entitlement_data=_entitlement(),
            runtime=_runtime(module),
            client_order_id=42,
        )


def test_market_max_leverage_caps_effective_risk_exactly_like_hyperliquid() -> None:
    module = _planning_module()
    parse_market = getattr(module, "parse_risex_risk_market", None)
    assert callable(parse_market)

    market = parse_market(_market_payload(max_leverage="50"), symbol="BTC")
    effective = resolve_effective_risk(
        _risk(max_leverage=Decimal("75")),
        _entitlement(),
        exchange_max_leverage=market.max_leverage,
    )

    assert effective.max_leverage == Decimal("50")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_market_payload(active=False), "inactive"),
        ({"data": {"markets": []}}, "market"),
    ],
)
def test_inactive_or_unknown_market_is_denied_locally(payload: dict, reason: str) -> None:
    module = _planning_module()
    plan_intent = getattr(module, "plan_risex_order_intent", None)
    denied = getattr(module, "RISExRiskPlanningDenied", RuntimeError)
    assert callable(plan_intent)

    with pytest.raises(denied, match=reason):
        plan_intent(
            job=_job(master_position="1"),
            portfolio_payload=_portfolio_payload(),
            markets_payload=payload,
            risk=_risk(),
            entitlement_data=_entitlement(),
            runtime=_runtime(module),
            client_order_id=42,
        )


def test_provider_reduce_only_market_rejects_opening_locally() -> None:
    module = _planning_module()
    plan_intent = getattr(module, "plan_risex_order_intent", None)
    denied = getattr(module, "RISExRiskPlanningDenied", RuntimeError)
    assert callable(plan_intent)

    with pytest.raises(denied, match="reduce.?only"):
        plan_intent(
            job=_job(master_position="1"),
            portfolio_payload=_portfolio_payload(size="0"),
            markets_payload=_market_payload(reduce_only=True),
            risk=_risk(),
            entitlement_data=_entitlement(),
            runtime=_runtime(module),
            client_order_id=42,
        )


def test_master_close_produces_immutable_reduce_only_risex_intent() -> None:
    module = _planning_module()
    plan_intent = getattr(module, "plan_risex_order_intent", None)
    assert callable(plan_intent)

    intent = plan_intent(
        job=_job(master_position="0", master_equity="1", master_mark="86192.08"),
        portfolio_payload=_portfolio_payload(size="0.0002"),
        markets_payload=_market_payload(reduce_only=True),
        risk=_risk(),
        entitlement_data=_entitlement(),
        runtime=_runtime(module),
        client_order_id=42,
    )

    assert isinstance(intent, risex_order_preparation.RISExOrderIntent)
    assert intent.symbol == "BTC"
    assert intent.is_buy is False
    assert intent.requested_size == Decimal("0.0002")
    assert intent.reduce_only is True
    assert intent.slippage_bps == 25
    assert intent.client_order_id == 42
    with pytest.raises(FrozenInstanceError):
        intent.requested_size = Decimal("1")  # type: ignore[misc]


def test_risk_authorized_size_is_floored_to_step_and_never_rounded_up_to_minimum() -> None:
    module = _planning_module()
    finalize = getattr(module, "finalize_risex_authorized_size", None)
    denied = getattr(module, "RISExRiskPlanningDenied", RuntimeError)
    parse_market = getattr(module, "parse_risex_risk_market", None)
    assert callable(finalize)
    assert callable(parse_market)
    market = parse_market(_market_payload(), symbol="BTC")

    assert finalize(Decimal("0.0001009"), market=market) == Decimal("0.000100")

    with pytest.raises(denied, match="minimum|min_order_size|below"):
        finalize(Decimal("0.0000999"), market=market)


def test_conservative_provider_position_limit_assertion_allows_btc_when_non_binding() -> None:
    module = _planning_module()
    check = getattr(module, "provider_position_limit_non_binding_under_risk_caps", None)
    parse_market = getattr(module, "parse_risex_risk_market", None)
    assert callable(check)
    assert callable(parse_market)

    market = parse_market(_market_payload(max_position_size="100000000"), symbol="BTC")
    effective = resolve_effective_risk(
        _risk(
            max_asset_exposure=Decimal("2000000"),
            max_total_exposure=Decimal("2000000"),
            max_leverage=Decimal("50"),
        ),
        _entitlement(),
        exchange_max_leverage=market.max_leverage,
    )

    result = check(
        market=market,
        account_equity=Decimal("25000"),
        effective_risk=effective,
    )

    # Conservative lower-bound interpretation:
    # 100,000,000 raw steps * 0.000001 = 100 BTC.
    # Risk Engine absolute upper bound:
    # min(asset cap, total cap, 25,000 * 50) / mark ~= 14.5 BTC.
    assert result.provider_limit_base_conservative == Decimal("100")
    assert result.risk_max_position_base < Decimal("15")
    assert result.non_binding is True
    assert result.unit_semantics_verified is False


def test_conservative_provider_position_limit_denies_open_when_it_could_bind() -> None:
    module = _planning_module()
    plan_intent = getattr(module, "plan_risex_order_intent", None)
    denied = getattr(module, "RISExRiskPlanningDenied", RuntimeError)
    assert callable(plan_intent)

    # Conservative lower bound is 10 BTC, while equity * leverage can authorize
    # roughly 14.5 BTC when other risk caps are deliberately set higher.
    with pytest.raises(denied, match="max_position_size|position limit|unit"):
        plan_intent(
            job=_job(master_position="1000", master_equity="1000", master_mark="100"),
            portfolio_payload=_portfolio_payload(
                total_account_value="25000",
                total_notional="0",
                free_collateral="25000",
                size="0",
            ),
            markets_payload=_market_payload(max_position_size="10000000"),
            risk=_risk(
                max_asset_exposure=Decimal("2000000"),
                max_total_exposure=Decimal("2000000"),
                max_leverage=Decimal("50"),
            ),
            entitlement_data=_entitlement(),
            runtime=_runtime(module),
            client_order_id=42,
        )


def test_maintenance_margin_factor_is_not_a_risk_gate_in_b1() -> None:
    module = _planning_module()
    plan_intent = getattr(module, "plan_risex_order_intent", None)
    assert callable(plan_intent)

    kwargs = dict(
        job=_job(master_position="1"),
        portfolio_payload=_portfolio_payload(size="0"),
        risk=_risk(),
        entitlement_data=_entitlement(),
        runtime=_runtime(module),
        client_order_id=42,
    )
    normal = plan_intent(
        markets_payload=_market_payload(maintenance_margin_factor="75"),
        **kwargs,
    )
    opaque = plan_intent(
        markets_payload=_market_payload(maintenance_margin_factor="opaque-provider-value"),
        **kwargs,
    )

    assert normal == opaque


@pytest.mark.parametrize(
    ("in_liquidation", "risk_level"),
    [(True, "NORMAL"), (False, "UNKNOWN_PROVIDER_STATE")],
)
def test_provider_risk_alarm_blocks_opening_but_not_reduce_only_close(
    in_liquidation: bool,
    risk_level: str,
) -> None:
    module = _planning_module()
    plan_intent = getattr(module, "plan_risex_order_intent", None)
    denied = getattr(module, "RISExRiskPlanningDenied", RuntimeError)
    assert callable(plan_intent)

    portfolio = _portfolio_payload(
        in_liquidation=in_liquidation,
        risk_level=risk_level,
        size="0.0002",
    )

    with pytest.raises(denied, match="liquidation|risk"):
        plan_intent(
            job=_job(master_position="1"),
            portfolio_payload=portfolio,
            markets_payload=_market_payload(),
            risk=_risk(),
            entitlement_data=_entitlement(),
            runtime=_runtime(module),
            client_order_id=42,
        )

    close_intent = plan_intent(
        job=_job(master_position="0", master_equity="1", master_mark="86192.08"),
        portfolio_payload=portfolio,
        markets_payload=_market_payload(),
        risk=_risk(),
        entitlement_data=_entitlement(),
        runtime=_runtime(module),
        client_order_id=42,
    )
    assert close_intent.reduce_only is True
