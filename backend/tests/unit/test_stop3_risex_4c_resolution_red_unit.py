"""RED contracts for STOP 3 RISEx 4C deterministic ambiguity resolution."""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.adapters.risex_types import ProviderReadUnavailable
from app.models.entities import ExecutionState


ACCOUNT = "0x274F1CDd4D54f62753Ef199b490F09C94320a1C3"
OTHER_ACCOUNT = "0x" + ("44" * 20)
AUTHORIZATION = "0x" + ("aa" * 20)
BIG_CLIENT_ORDER_ID_TEXT = "11892285924151961225"
BIG_CLIENT_ORDER_ID = int(BIG_CLIENT_ORDER_ID_TEXT)
OTHER_OBSERVED_CLIENT_ORDER_ID_TEXT = "1402917565528103158"
SEARCH_START = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
SEARCH_START_NS = int(SEARCH_START.timestamp() * 1_000_000_000)


def _require_symbol(name: str):
    try:
        module = importlib.import_module("app.services.risex_execution_resolution")
    except ModuleNotFoundError:
        pytest.fail(
            f"RED: expected app.services.risex_execution_resolution.{name}",
            pytrace=False,
        )
    value = getattr(module, name, None)
    assert callable(value), (
        f"RED: expected app.services.risex_execution_resolution.{name}"
    )
    return module, value


def _order(
    *,
    client_order_id: str = BIG_CLIENT_ORDER_ID_TEXT,
    status: str = "ORDER_STATUS_FILLED",
    sender: str = ACCOUNT,
    filled_size: str = "0.5",
    avg_price: str | None = "81430.3",
    created_at: str | None = None,
    order_id: str = "order-observed-big",
) -> dict[str, Any]:
    order: dict[str, Any] = {
        "id": order_id,
        "price": "81360.1",
        "size": "0.5",
        "market_id": "1",
        "side": "BUY",
        "type": "LIMIT",
        "time_in_force": "IOC",
        "expiry": "0",
        "reduce_only": False,
        "cancel_reason": "",
        "block_number": "123456",
        "log_index": "7",
        "filled_size": filled_size,
        "status": status,
        "sender": sender,
        "created_at": created_at or str(SEARCH_START_NS + 1_000_000_000),
        "tx_hash": "0x" + ("55" * 32),
        "client_order_id": client_order_id,
        "wide_order_id": "224552",
        "resting_order_id": "112276",
    }
    if avg_price is not None:
        order["avg_price"] = avg_price
    return order


def _page(
    orders: list[dict[str, Any]],
    *,
    page: int = 1,
    has_next_page: bool = False,
) -> dict[str, Any]:
    return {
        "data": {
            "orders": orders,
            "page": page,
            "has_next_page": has_next_page,
        }
    }


class FakeAPI:
    public_read_only = True

    def __init__(
        self,
        pages: dict[int, dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        trade_history_payload: dict[str, Any] | None = None,
    ) -> None:
        self.pages = pages or {1: _page([])}
        self.error = error
        self.trade_history_payload = trade_history_payload or {
            "data": {
                "trades": [
                    {
                        "order_id": "order-observed-big",
                        "client_order_id": BIG_CLIENT_ORDER_ID_TEXT,
                        "price": "81360.1",
                        "avg_price": "0",
                    }
                ]
            }
        }
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        if self.error is not None:
            raise self.error
        if path == "/v1/trade-history":
            return self.trade_history_payload
        assert path == "/v1/orders", f"unexpected read path: {path}"
        page = int((params or {}).get("page", 1))
        return self.pages.get(page, _page([], page=page))


class FakeRPC:
    public_read_only = True

    def __init__(
        self,
        *,
        consumed: bool,
        nonce_anchor: int = 7,
        nonce_bitmap_index: int = 13,
        state_anchor: int | None = None,
        error: Exception | None = None,
        malformed_block: bool = False,
    ) -> None:
        self.consumed = consumed
        self.nonce_anchor = nonce_anchor
        self.nonce_bitmap_index = nonce_bitmap_index
        self.state_anchor = nonce_anchor if state_anchor is None else state_anchor
        self.error = error
        self.malformed_block = malformed_block
        self.eth_call_count = 0

    async def call(self, method: str, params: list[object]) -> object:
        if self.error is not None:
            raise self.error
        if method == "eth_blockNumber":
            return "not-hex" if self.malformed_block else "0x64"
        assert method == "eth_call"
        self.eth_call_count += 1
        if self.eth_call_count == 1:
            return "0x" + (1 if self.consumed else 0).to_bytes(32, "big").hex()
        bitmap = (1 << self.nonce_bitmap_index) if self.consumed else 0
        return "0x" + (
            self.state_anchor.to_bytes(32, "big")
            + bitmap.to_bytes(32, "big")
        ).hex()


@pytest.mark.asyncio
async def test_u1_resolution_module_has_no_signed_write_surface_unit() -> None:
    api = FakeAPI({1: _page([_order()])})
    assert api.public_read_only is True
    module, _reader = _require_symbol("read_risex_order_history")
    _require_symbol("resolve_risex_ambiguous_executions")

    source = inspect.getsource(module)
    forbidden = (
        "RISExSignedTestnetHTTPTransport",
        "place_ioc",
        "resolve_risex_worker_credential",
        "decrypt",
        "unwrap",
    )
    for token in forbidden:
        assert token not in source, (
            f"RED: RISEx 4C resolution must remain read-only; found {token}"
        )


@pytest.mark.asyncio
async def test_u2_consistent_free_nonce_is_unresolved_unit() -> None:
    api = FakeAPI({1: _page([_order()])})
    rpc = FakeRPC(consumed=False)
    _module, collect = _require_symbol("_collect_risex_4c_resolution_evidence")

    result = await collect(
        api,
        rpc,
        authorization_address=AUTHORIZATION,
        account=ACCOUNT,
        nonce_anchor=7,
        nonce_bitmap_index=13,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=3,
    )

    assert result is None
    assert not api.calls, (
        "RED: a free nonce cannot be promoted to terminal by order-history evidence"
    )


@pytest.mark.asyncio
async def test_u3_consumed_nonce_without_exact_order_is_unresolved_unit() -> None:
    api = FakeAPI({1: _page([])})
    rpc = FakeRPC(consumed=True)
    _module, collect = _require_symbol("_collect_risex_4c_resolution_evidence")

    result = await collect(
        api,
        rpc,
        authorization_address=AUTHORIZATION,
        account=ACCOUNT,
        nonce_anchor=7,
        nonce_bitmap_index=13,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=3,
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["inconsistent", "unavailable", "malformed"])
async def test_u4_nonce_failure_modes_remain_unresolved_unit(
    failure_kind: str,
) -> None:
    api = FakeAPI({1: _page([_order()])})
    if failure_kind == "inconsistent":
        rpc = FakeRPC(consumed=True, state_anchor=8)
    elif failure_kind == "unavailable":
        rpc = FakeRPC(
            consumed=True,
            error=ProviderReadUnavailable("synthetic read failure"),
        )
    else:
        rpc = FakeRPC(consumed=True, malformed_block=True)

    _module, collect = _require_symbol("_collect_risex_4c_resolution_evidence")
    result = await collect(
        api,
        rpc,
        authorization_address=AUTHORIZATION,
        account=ACCOUNT,
        nonce_anchor=7,
        nonce_bitmap_index=13,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=3,
    )

    assert result is None


@pytest.mark.asyncio
async def test_u5_exact_match_uses_uint64_client_id_and_epoch_sender_unit() -> None:
    other = _order(
        client_order_id=OTHER_OBSERVED_CLIENT_ORDER_ID_TEXT,
        order_id="order-observed-small",
    )
    target = _order()
    api = FakeAPI({1: _page([other, target])})
    module, reader = _require_symbol("read_risex_order_history")

    result = await reader(
        api,
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=3,
    )

    assert result is not None
    assert result["client_order_id"] == BIG_CLIENT_ORDER_ID
    assert result["provider_order_id"] == "order-observed-big"
    assert result["raw_order"]["wide_order_id"] == "224552"
    assert result["raw_order"]["resting_order_id"] == "112276"

    wrong_sender_api = FakeAPI({1: _page([_order(sender=OTHER_ACCOUNT)])})
    assert (
        await reader(
            wrong_sender_api,
            account=ACCOUNT,
            client_order_id=BIG_CLIENT_ORDER_ID,
            execution_created_at=SEARCH_START,
            max_pages=3,
        )
        is None
    )

    reader_source = inspect.getsource(reader)
    module_source = inspect.getsource(module)
    assert "float(" not in reader_source, (
        "RED: client_order_id must be parsed as a Python int, never float"
    )
    assert "wide_order_id" not in module_source
    assert "resting_order_id" not in module_source
    assert BIG_CLIENT_ORDER_ID > (1 << 63)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "filled_size", "expected_state"),
    [
        ("ORDER_STATUS_FILLED", "0.5", ExecutionState.FILLED),
        # documentato, non osservato il 27/09
        ("ORDER_STATUS_CANCELLED", "0.2", ExecutionState.FILLED),
        # documentato, non osservato il 27/09
        ("ORDER_STATUS_CANCELLED", "0", ExecutionState.CANCELED),
        # documentato, non osservato il 27/09
        ("ORDER_STATUS_OPEN", "0", None),
        # documentato, non osservato il 27/09
        ("ORDER_STATUS_NONE", "0", None),
        ("ORDER_STATUS_FUTURE_UNKNOWN", "0", None),
    ],
)
async def test_u6_order_status_mapping_never_synthesizes_rejected_unit(
    status: str,
    filled_size: str,
    expected_state: ExecutionState | None,
) -> None:
    api = FakeAPI(
        {
            1: _page(
                [
                    _order(
                        status=status,
                        filled_size=filled_size,
                        avg_price="81430.3" if Decimal(filled_size) > 0 else "0",
                    )
                ]
            )
        }
    )
    _module, reader = _require_symbol("read_risex_order_history")

    result = await reader(
        api,
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=3,
    )

    if expected_state is None:
        assert result is None
    else:
        assert result is not None
        assert result["execution_state"] == expected_state
        if Decimal(filled_size) > 0:
            assert result["filled_size"] == Decimal(filled_size)
    if result is not None:
        assert result["execution_state"] != ExecutionState.REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_avg_price", "expected"),
    [
        ("81430.3", Decimal("81430.3")),
        ("0", None),
        ("", None),
        (None, None),
    ],
)
async def test_u7_average_price_comes_only_from_order_history_unit(
    order_avg_price: str | None,
    expected: Decimal | None,
) -> None:
    api = FakeAPI(
        {1: _page([_order(avg_price=order_avg_price)])},
        trade_history_payload={
            "data": {
                "trades": [
                    {
                        "order_id": "order-observed-big",
                        "client_order_id": BIG_CLIENT_ORDER_ID_TEXT,
                        "price": "81360.1",
                        "avg_price": "0",
                    }
                ]
            }
        },
    )
    _module, reader = _require_symbol("read_risex_order_history")

    result = await reader(
        api,
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=3,
    )

    assert result is not None
    assert result["avg_price"] == expected
    assert all(path != "/v1/trade-history" for path, _params in api.calls)


@pytest.mark.asyncio
async def test_u8_history_pagination_window_duplicates_and_page_cap_are_fail_closed_unit() -> None:
    page1 = _page(
        [_order(client_order_id=OTHER_OBSERVED_CLIENT_ORDER_ID_TEXT)],
        page=1,
        has_next_page=True,
    )
    page2 = _page([_order()], page=2, has_next_page=False)
    api = FakeAPI({1: page1, 2: page2})
    _module, reader = _require_symbol("read_risex_order_history")

    found = await reader(
        api,
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=2,
    )
    assert found is not None
    assert found["client_order_id"] == BIG_CLIENT_ORDER_ID

    capped = await reader(
        FakeAPI({1: page1, 2: page2}),
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=1,
    )
    assert capped is None

    too_old = _order(created_at=str(SEARCH_START_NS - 1))
    old_result = await reader(
        FakeAPI({1: _page([too_old])}),
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=2,
    )
    assert old_result is None

    duplicate_result = await reader(
        FakeAPI(
            {
                1: _page(
                    [
                        _order(order_id="duplicate-a"),
                        _order(order_id="duplicate-b"),
                    ]
                )
            }
        ),
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=2,
    )
    assert duplicate_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["timeout", "malformed_json", "missing_field"])
async def test_u9_history_failures_return_unresolved_without_terminal_exception_unit(
    failure_kind: str,
) -> None:
    if failure_kind == "timeout":
        api = FakeAPI(error=TimeoutError("synthetic timeout"))
    elif failure_kind == "malformed_json":
        api = FakeAPI(
            {1: {"data": {"orders": "not-a-list", "page": 1, "has_next_page": False}}}
        )
    else:
        missing = _order()
        missing.pop("client_order_id")
        api = FakeAPI({1: _page([missing])})

    _module, reader = _require_symbol("read_risex_order_history")
    result = await reader(
        api,
        account=ACCOUNT,
        client_order_id=BIG_CLIENT_ORDER_ID,
        execution_created_at=SEARCH_START,
        max_pages=2,
    )
    assert result is None
