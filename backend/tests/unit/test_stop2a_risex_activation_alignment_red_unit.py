"""RED contracts for STOP2-A RISEx activation alignment.

Production code is intentionally absent in this PR.  These tests fix the
provider-neutral reconciliation boundary that the GREEN implementation must
satisfy without duplicating Hyperliquid planning logic.
"""

from __future__ import annotations

import importlib
import inspect
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.adapters import risex as risex_adapter
from app.adapters import risex_signed_testnet_http
from app.api import activation
from app.core.crypto import crypto
from app.services import reconcile, risex_copy_execution


def _require_symbol(module, name: str):
    value = getattr(module, name, None)
    assert callable(value), f"RED: missing symbol {module.__name__}.{name}"
    return value


@asynccontextmanager
async def _no_op_lock(_user_id):
    yield


ACCOUNT = "0x" + ("44" * 20)


def _require_risex_observation_module():
    try:
        return importlib.import_module("app.services.risex_reconcile_observation")
    except ModuleNotFoundError:
        pytest.fail(
            "RED: missing module app.services.risex_reconcile_observation"
        )


def _require_exception(module, name: str):
    value = getattr(module, name, None)
    assert (
        isinstance(value, type)
        and issubclass(value, Exception)
    ), f"RED: missing exception {module.__name__}.{name}"
    return value


def _markets_payload() -> dict:
    return {
        "data": {
            "markets": [
                {
                    "market_id": "1",
                    "config": {
                        "name": "BTC/USDC",
                        "step_size": "0.000001",
                        "step_price": "0.1",
                        "maintenance_margin_factor": "75",
                        "max_leverage": "50",
                        "min_order_size": "0.0001",
                        "unlocked": True,
                        "open_interest_limit": "0",
                    },
                    "base_asset_symbol": "BTC/USDC",
                    "quote_asset_symbol": "USDC",
                    "underlying": "BTC/USDC",
                    "display_name": "BTC/USDC",
                    "mark_price": "86192.08",
                    "max_position_size": "100000000",
                    "open_interest": "14957.018174",
                    "post_only": False,
                    "reduce_only": False,
                    "active": True,
                },
                {
                    "market_id": "2",
                    "config": {
                        "name": "ETH/USDC",
                        "step_size": "0.0001",
                        "step_price": "0.01",
                        "maintenance_margin_factor": "75",
                        "max_leverage": "25",
                        "min_order_size": "0.001",
                        "unlocked": True,
                        "open_interest_limit": "0",
                    },
                    "base_asset_symbol": "ETH/USDC",
                    "quote_asset_symbol": "USDC",
                    "underlying": "ETH/USDC",
                    "display_name": "ETH/USDC",
                    "mark_price": "2450.50",
                    "max_position_size": "100000000",
                    "open_interest": "1000",
                    "post_only": False,
                    "reduce_only": False,
                    "active": True,
                },
            ]
        }
    }


def _portfolio_payload(*, free_collateral: object = "2000.857", market_id: str = "1", size: str = "-0.0002") -> dict:
    summary = {
        "usdc_balance": "2000",
        "collateral_margin_balance": "2000",
        "total_account_value": "2001.20",
        "total_notional": "17.238",
        "total_unrealized_pnl": "0.959",
        "free_collateral": free_collateral,
        "in_liquidation": False,
        "risk_level": "NORMAL",
    }
    return {
        "data": {
            "summary": summary,
            "positions": [
                {
                    "market_id": market_id,
                    "size": size,
                    "mark_price": "86192.08",
                    "avg_entry_price": "81397.08",
                    "liquidation_price": "50000",
                }
            ],
        }
    }


class _ReadOnlyAPI:
    public_read_only = True

    def __init__(self, *, portfolio: dict | None = None, markets: dict | None = None, events: list[str] | None = None):
        self.portfolio = portfolio or _portfolio_payload()
        self.markets = markets or _markets_payload()
        self.events = events

    async def get_json(self, path, params=None):
        if self.events is not None:
            self.events.append(f"read:{path}")
        if path == "/v1/markets":
            assert params is None
            return self.markets
        if path == "/v1/portfolio/details":
            assert params == {"account": ACCOUNT}
            return self.portfolio
        raise AssertionError(path)


async def _read_real_risex_observation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    portfolio: dict | None = None,
    markets: dict | None = None,
    events: list[str] | None = None,
):
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    module = _require_risex_observation_module()
    reader = _require_symbol(module, "read_risex_reconcile_observation")
    epoch_id = uuid.uuid4()
    observation = await reader(
        account_address=ACCOUNT,
        epoch_id=epoch_id,
        network="testnet",
        api=_ReadOnlyAPI(portfolio=portfolio, markets=markets, events=events),
    )
    return module, epoch_id, observation


@pytest.mark.asyncio
async def test_risex_activation_uses_shared_reconciliation_planner_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    user = SimpleNamespace(id=uuid.uuid4())
    observation = SimpleNamespace(provider="normalized", network="testnet")
    master_positions = {"BTC": Decimal("0.25")}
    shared = _require_symbol(reconcile, "reconcile_observed_follower")
    hl_reader = _require_symbol(reconcile, "read_hyperliquid_reconcile_observation")
    risex_alignment = _require_symbol(activation, "_resume_risex_alignment")
    assert shared is not None and hl_reader is not None

    calls: list[str] = []

    async def shared_spy(*_args, **kwargs):
        obs = kwargs.get("observation")
        calls.append(str(getattr(obs, "provider", "unknown")))
        return {"status": "OK", "jobs_created": 0}

    async def hyperliquid_reader(*_args, **_kwargs):
        return SimpleNamespace(provider="hyperliquid", network="testnet")

    monkeypatch.setattr(reconcile, "is_master_source_user", lambda _user: False)
    monkeypatch.setattr(reconcile, "position_ledger_lock", _no_op_lock)
    monkeypatch.setattr(reconcile, "read_hyperliquid_reconcile_observation", hyperliquid_reader)
    monkeypatch.setattr(reconcile, "reconcile_observed_follower", shared_spy)
    monkeypatch.setattr(activation, "reconcile_observed_follower", shared_spy, raising=False)

    await reconcile.reconcile_user(
        object(),
        SimpleNamespace(network="testnet"),
        user,
        master_positions=master_positions,
        master_equity=Decimal("1000"),
        mids={"BTC": "100"},
    )
    await risex_alignment(
        object(),
        user,
        observation=SimpleNamespace(provider="risex", network="testnet"),
        master_positions=master_positions,
        master_equity=Decimal("1000"),
        master_mids={"BTC": "100"},
        master_configs=None,
        create_jobs=True,
    )

    assert calls == ["hyperliquid", "risex"], (
        "RED: Hyperliquid reconcile_user() and the RISEx activation branch must "
        "delegate behaviorally to the same reconcile_observed_follower callable"
    )


@pytest.mark.asyncio
async def test_risex_activation_alignment_only_creates_copyjobs_before_worker_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    risex_alignment = _require_symbol(activation, "_resume_risex_alignment")
    _require_symbol(reconcile, "reconcile_observed_follower")

    async def forbidden_async(*_args, **_kwargs):
        raise AssertionError("RED: activation crossed the worker-only signed-write boundary")

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("RED: activation decrypted a credential or built signed transport")

    async def shared_spy(*_args, **_kwargs):
        return {"status": "OK", "jobs_created": 1}

    monkeypatch.setattr(risex_adapter.RISExAdapter, "place_ioc", forbidden_async)
    monkeypatch.setattr(risex_copy_execution, "claim_risex_first_post", forbidden_async)
    monkeypatch.setattr(crypto, "decrypt", forbidden_sync)
    monkeypatch.setattr(
        risex_signed_testnet_http,
        "RISExSignedTestnetHTTPTransport",
        forbidden_sync,
    )
    monkeypatch.setattr(activation, "RISExSignedTestnetHTTPTransport", forbidden_sync, raising=False)
    monkeypatch.setattr(activation, "reconcile_observed_follower", shared_spy, raising=False)
    monkeypatch.setattr(reconcile, "reconcile_observed_follower", shared_spy)

    result = await risex_alignment(
        object(),
        SimpleNamespace(id=uuid.uuid4()),
        observation=SimpleNamespace(provider="risex", network="testnet"),
        master_positions={"BTC": Decimal("0.25")},
        master_equity=Decimal("1000"),
        master_mids={"BTC": "100"},
        master_configs=None,
        create_jobs=True,
    )
    assert result == {"status": "OK", "jobs_created": 1}


@pytest.mark.asyncio
async def test_risex_activation_contract_does_not_require_flat_master_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    user = SimpleNamespace(id=uuid.uuid4())
    observation = SimpleNamespace(provider="risex", network="testnet")
    risex_alignment = _require_symbol(activation, "_resume_risex_alignment")
    _require_symbol(reconcile, "reconcile_observed_follower")

    observed_master_positions: list[dict[str, Decimal]] = []

    async def shared_spy(*_args, **kwargs):
        observed_master_positions.append(dict(kwargs["master_positions"]))
        return {"status": "OK", "jobs_created": 1}

    monkeypatch.setattr(activation, "reconcile_observed_follower", shared_spy, raising=False)
    result = await risex_alignment(
        object(),
        user,
        observation=observation,
        master_positions={"BTC": Decimal("0.25")},
        master_equity=Decimal("1000"),
        master_mids={"BTC": "100"},
        master_configs=None,
        create_jobs=True,
    )

    assert observed_master_positions == [{"BTC": Decimal("0.25")}]
    assert result["status"] == "OK"


def test_risex_activation_environment_gate_precedes_provider_reads_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    _require_symbol(activation, "assert_risex_environment_allowed")
    _require_symbol(activation, "read_risex_reconcile_observation")
    source = inspect.getsource(activation.resume_copy_immediate)
    gate_index = source.index("assert_risex_environment_allowed(")
    read_index = source.index("read_risex_reconcile_observation(")
    assert gate_index < read_index, (
        "RED: assert_risex_environment_allowed() must run before the first RISEx provider read"
    )


@pytest.mark.asyncio
async def test_risex_reader_maps_equity_and_free_margin_from_provider_truth_r1_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _module, epoch_id, observation = await _read_real_risex_observation(monkeypatch)

    assert observation.provider == "risex"
    assert observation.network == "testnet"
    assert observation.epoch_id == epoch_id
    assert observation.account_address == ACCOUNT
    assert observation.account_equity == Decimal("2001.20")
    assert observation.free_margin == Decimal("2000.857")

    module = _require_risex_observation_module()
    reader = _require_symbol(module, "read_risex_reconcile_observation")
    indeterminate = _require_exception(reconcile, "ReconcileObservationIndeterminate")
    payload = _portfolio_payload()
    payload["data"]["summary"].pop("free_collateral")
    with pytest.raises(indeterminate, match="free.?collateral|free.?margin|indeterminate"):
        await reader(
            account_address=ACCOUNT,
            epoch_id=uuid.uuid4(),
            network="testnet",
            api=_ReadOnlyAPI(portfolio=payload),
        )


@pytest.mark.asyncio
async def test_risex_reader_maps_market_ids_signed_sizes_marks_and_liquidation_r2_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _module, _epoch_id, observation = await _read_real_risex_observation(monkeypatch)
    assert observation.positions == {"BTC": Decimal("-0.0002")}
    assert observation.marks["BTC"] == Decimal("86192.08")
    assert observation.liquidation_prices["BTC"] == Decimal("50000")

    module = _require_risex_observation_module()
    reader = _require_symbol(module, "read_risex_reconcile_observation")
    indeterminate = _require_exception(reconcile, "ReconcileObservationIndeterminate")
    with pytest.raises(indeterminate, match="market|unknown|indeterminate"):
        await reader(
            account_address=ACCOUNT,
            epoch_id=uuid.uuid4(),
            network="testnet",
            api=_ReadOnlyAPI(portfolio=_portfolio_payload(market_id="99")),
        )


@pytest.mark.asyncio
async def test_risex_reader_builds_asset_specs_only_from_verified_market_constraints_r3_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _module, _epoch_id, observation = await _read_real_risex_observation(monkeypatch)
    spec = observation.asset_specs["BTC"]

    assert spec.name == "BTC"
    assert spec.sz_decimals == 6
    assert spec.max_leverage == 50
    assert spec.min_notional == Decimal("8.619208")


@pytest.mark.asyncio
async def test_risex_reader_never_invents_unmanaged_margin_r4_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _module, _epoch_id, observation = await _read_real_risex_observation(monkeypatch)
    assert observation.unmanaged_margin is None


@pytest.mark.asyncio
async def test_risex_reader_is_read_only_and_environment_gate_precedes_first_read_r5_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    module = _require_risex_observation_module()
    reader = _require_symbol(module, "read_risex_reconcile_observation")
    events: list[str] = []

    def gate(*, network, env):
        assert network == "testnet"
        assert env["ENABLE_LIVE_TRADING"] == "false"
        events.append("gate")
        return network

    async def forbidden_async(*_args, **_kwargs):
        raise AssertionError("RED: read-only RISEx reader crossed a signed-write boundary")

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("RED: read-only RISEx reader used credential decryption or signed transport")

    monkeypatch.setattr(module, "assert_risex_environment_allowed", gate)
    monkeypatch.setattr(risex_adapter.RISExAdapter, "place_ioc", forbidden_async)
    monkeypatch.setattr(risex_copy_execution, "claim_risex_first_post", forbidden_async)
    monkeypatch.setattr(crypto, "decrypt", forbidden_sync)
    monkeypatch.setattr(
        risex_signed_testnet_http,
        "RISExSignedTestnetHTTPTransport",
        forbidden_sync,
    )
    monkeypatch.setattr(module, "RISExSignedTestnetHTTPTransport", forbidden_sync, raising=False)

    observation = await reader(
        account_address=ACCOUNT,
        epoch_id=uuid.uuid4(),
        network="testnet",
        api=_ReadOnlyAPI(events=events),
    )

    assert observation.account_equity == Decimal("2001.20")
    assert events == [
        "gate",
        "read:/v1/markets",
        "read:/v1/portfolio/details",
    ]


def test_hyperliquid_reconcile_public_contract_remains_unchanged_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    signature = inspect.signature(reconcile.reconcile_user)
    assert list(signature.parameters) == [
        "db",
        "hl",
        "user",
        "master_positions",
        "master_equity",
        "mids",
        "master_mids",
        "master_configs",
        "create_jobs",
    ]
    assert signature.parameters["master_positions"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(reconcile.reconcile_user)
    assert "is_master_source_user(user)" in source
    assert "position_ledger_lock(user.id)" in source