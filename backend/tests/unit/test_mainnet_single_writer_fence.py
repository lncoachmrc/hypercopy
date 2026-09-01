from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from eth_account import Account

from app.adapters.hyperliquid import HyperliquidAdapter, MainnetWriterFenceError
from app.core.config import Settings, settings
from app.engine.sizing import AssetSpec


WRITER_ENV = "cdaedfa0-6623-4cba-9c09-94247d5d47e6"
OTHER_ENV = "72c18813-23da-4856-98ed-f3700f6b7960"


def _set_writer_identity(monkeypatch, *, expected: str, actual: str) -> None:
    monkeypatch.setattr(settings, "TRAXION_MAINNET_WRITER_ENVIRONMENT_ID", expected)
    monkeypatch.setattr(settings, "RAILWAY_ENVIRONMENT_ID", actual)


def _adapter(monkeypatch, network: str) -> HyperliquidAdapter:
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())
    return HyperliquidAdapter(None, network=network)


@pytest.mark.asyncio
async def test_testnet_signed_action_ignores_mainnet_writer_identity(monkeypatch):
    _set_writer_identity(monkeypatch, expected="", actual="")
    adapter = _adapter(monkeypatch, "testnet")
    submitted = 0

    def submit():
        nonlocal submitted
        submitted += 1
        return {"status": "ok"}

    assert await adapter._signed_call("0x" + "22" * 20, "0x" + "11" * 20, submit) == {"status": "ok"}
    assert submitted == 1


@pytest.mark.asyncio
async def test_mainnet_matching_designated_writer_allows_signed_action(monkeypatch):
    _set_writer_identity(monkeypatch, expected=WRITER_ENV, actual=WRITER_ENV)
    adapter = _adapter(monkeypatch, "mainnet")
    submitted = 0

    def submit():
        nonlocal submitted
        submitted += 1
        return {"status": "ok"}

    assert await adapter._signed_call("0x" + "22" * 20, "0x" + "11" * 20, submit) == {"status": "ok"}
    assert submitted == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (WRITER_ENV, OTHER_ENV),
        ("", WRITER_ENV),
        (WRITER_ENV, ""),
    ],
    ids=["environment-mismatch", "missing-designated-writer", "missing-railway-environment"],
)
async def test_mainnet_invalid_writer_identity_fails_closed_before_sdk_call(monkeypatch, expected: str, actual: str):
    _set_writer_identity(monkeypatch, expected=expected, actual=actual)
    adapter = _adapter(monkeypatch, "mainnet")
    submitted = 0

    def submit():
        nonlocal submitted
        submitted += 1
        return {"status": "ok"}

    with pytest.raises(MainnetWriterFenceError, match="NO EXCHANGE ACTION WAS SENT"):
        await adapter._signed_call("0x" + "22" * 20, "0x" + "11" * 20, submit)

    assert submitted == 0


class _OrderedLimiter:
    def __init__(self, events: list[str]):
        self._redis = MagicMock()
        self._events = events

    async def acquire(self, *_args, **_kwargs):
        self._events.append("ip_budget")


class _OrderedAddressTracker:
    def __init__(self, events: list[str]):
        self._events = events

    async def wait_for_existing_backoff(self, _address: str):
        self._events.append("pre_wait")

    async def wait_if_backed_off(self, _address: str):
        self._events.append("final_backoff")

    async def record_action_attempt(self, _address: str):
        self._events.append("address_cadence")

    async def mark_throttled(self, _address: str):
        self._events.append("mark_throttled")


@pytest.mark.asyncio
async def test_mainnet_writer_fence_is_last_check_immediately_before_sdk_submit(monkeypatch):
    events: list[str] = []
    limiter = _OrderedLimiter(events)
    monkeypatch.setattr("app.adapters.hyperliquid.Info", MagicMock())
    adapter = HyperliquidAdapter(limiter, network="mainnet")
    adapter.address_limits = _OrderedAddressTracker(events)
    _set_writer_identity(monkeypatch, expected=WRITER_ENV, actual=WRITER_ENV)

    @asynccontextmanager
    async def ordered_signer_lock(_signer: str):
        events.append("signer_lock")
        yield

    async def before_submit():
        events.append("before_submit")

    original_fence = adapter._assert_mainnet_writer_authorized

    def ordered_fence():
        events.append("writer_fence")
        original_fence()

    def submit():
        events.append("sdk_submit")
        return {"status": "ok"}

    monkeypatch.setattr("app.adapters.hyperliquid.signer_action_lock", ordered_signer_lock)
    monkeypatch.setattr(adapter, "_assert_mainnet_writer_authorized", ordered_fence)

    assert await adapter._signed_call(
        "0x" + "22" * 20,
        "0x" + "11" * 20,
        submit,
        before_submit=before_submit,
    ) == {"status": "ok"}

    assert events == [
        "pre_wait",
        "signer_lock",
        "ip_budget",
        "final_backoff",
        "address_cadence",
        "before_submit",
        "writer_fence",
        "sdk_submit",
    ]
    assert events[-2:] == ["writer_fence", "sdk_submit"]


class _FakeExchange:
    def __init__(self):
        self.order_calls = 0
        self.leverage_calls = 0

    def set_expires_after(self, _value):
        return None

    def order(self, *_args, **_kwargs):
        self.order_calls += 1
        return {"status": "ok"}

    def update_leverage(self, *_args, **_kwargs):
        self.leverage_calls += 1
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_mainnet_fence_blocks_close_all_style_ioc_before_exchange_order(monkeypatch):
    _set_writer_identity(monkeypatch, expected=WRITER_ENV, actual=OTHER_ENV)
    adapter = _adapter(monkeypatch, "mainnet")
    exchange = _FakeExchange()
    account = Account.create()

    async def fake_asset_spec(asset: str):
        return AssetSpec(asset, sz_decimals=5, max_leverage=20)

    async def no_strategy_intent(**_kwargs):
        # CLOSE_ALL/admin IOCs intentionally have no durable strategy intent.
        return None

    monkeypatch.setattr(adapter, "asset_spec", fake_asset_spec)
    monkeypatch.setattr(adapter, "_exchange", lambda _local, _account: exchange)
    monkeypatch.setattr("app.services.strategy_intents.current_strategy_intent_for_cloid", no_strategy_intent)

    with pytest.raises(MainnetWriterFenceError, match="NO EXCHANGE ACTION WAS SENT"):
        await adapter.place_ioc(
            account_address="0x" + "22" * 20,
            private_key=account.key.hex(),
            asset="BTC",
            is_buy=False,
            size=Decimal("0.01"),
            mark_price=Decimal("100000"),
            slippage_bps=25,
            reduce_only=True,
            cloid="0x" + "33" * 16,
        )

    assert exchange.order_calls == 0


@pytest.mark.asyncio
async def test_mainnet_fence_blocks_update_leverage_and_admin_sync_boundary(monkeypatch):
    _set_writer_identity(monkeypatch, expected=WRITER_ENV, actual=OTHER_ENV)
    adapter = _adapter(monkeypatch, "mainnet")
    exchange = _FakeExchange()
    account = Account.create()

    async def fake_asset_spec(asset: str):
        return AssetSpec(asset, sz_decimals=5, max_leverage=20)

    monkeypatch.setattr(adapter, "asset_spec", fake_asset_spec)
    monkeypatch.setattr(adapter, "_exchange", lambda _local, _account: exchange)

    with pytest.raises(MainnetWriterFenceError, match="NO EXCHANGE ACTION WAS SENT"):
        await adapter.update_leverage(
            account_address="0x" + "22" * 20,
            private_key=account.key.hex(),
            asset="BTC",
            leverage=3,
            is_cross=True,
        )

    # ADMIN_LEVERAGE_SYNC reaches the same adapter update_leverage boundary.
    assert exchange.leverage_calls == 0


def _production_live_settings(**overrides) -> Settings:
    values = {
        "APP_ENV": "production",
        "SESSION_SECRET": "s" * 32,
        "ENABLE_LIVE_TRADING": True,
        "KEK_PROVIDER": "local_rsa",
        "TRAXION_MAINNET_WRITER_ENVIRONMENT_ID": WRITER_ENV,
        "RAILWAY_ENVIRONMENT_ID": WRITER_ENV,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_live_startup_accepts_matching_writer_environment():
    cfg = _production_live_settings()
    assert cfg.TRAXION_MAINNET_WRITER_ENVIRONMENT_ID == WRITER_ENV
    assert cfg.RAILWAY_ENVIRONMENT_ID == WRITER_ENV


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        (WRITER_ENV, OTHER_ENV),
        ("", WRITER_ENV),
        (WRITER_ENV, ""),
    ],
    ids=["environment-mismatch", "missing-designated-writer", "missing-railway-environment"],
)
def test_production_live_startup_rejects_invalid_writer_environment(expected: str, actual: str):
    with pytest.raises(ValueError, match="MAINNET single-writer fence failed"):
        _production_live_settings(
            TRAXION_MAINNET_WRITER_ENVIRONMENT_ID=expected,
            RAILWAY_ENVIRONMENT_ID=actual,
        )


def test_production_with_live_trading_disabled_does_not_require_writer_identity():
    cfg = Settings(
        _env_file=None,
        APP_ENV="production",
        SESSION_SECRET="s" * 32,
        ENABLE_LIVE_TRADING=False,
        KEK_PROVIDER="local_rsa",
        TRAXION_MAINNET_WRITER_ENVIRONMENT_ID="",
        RAILWAY_ENVIRONMENT_ID="",
    )
    assert cfg.ENABLE_LIVE_TRADING is False
