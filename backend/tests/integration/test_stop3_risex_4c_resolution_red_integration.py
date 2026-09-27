"""RED integration contracts for STOP 3 RISEx 4C ambiguity resolution."""

from __future__ import annotations

import importlib
import inspect
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.adapters.risex_types import ProviderReadUnavailable
from app.db.position_ledger_lock import (
    position_ledger_lock,
    position_ledger_lock_engine,
)
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionEpoch,
    ExecutionState,
    JobState,
    PositionLedger,
    TradingAccount,
    User,
    UserState,
)
from app.services import risex_worker_submission
from app.services.execution_resolution import (
    resolve_ambiguous_executions as resolve_hyperliquid_ambiguous_executions,
)
from app.workers import execution_worker, watcher


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


ACCOUNT = "0x274F1CDd4D54f62753Ef199b490F09C94320a1C3"
AUTHORIZATION = "0x" + ("aa" * 20)
BIG_CLIENT_ORDER_ID_TEXT = "11892285924151961225"
BIG_CLIENT_ORDER_ID = int(BIG_CLIENT_ORDER_ID_TEXT)
OTHER_OBSERVED_CLIENT_ORDER_ID_TEXT = "1402917565528103158"


def _abi_address_hex(address: str) -> str:
    return bytes.fromhex(address[2:]).rjust(32, bytes([0])).hex()


def _abi_uint_hex(value: int) -> str:
    return value.to_bytes(32, "big").hex()


def _expected_nonce_call_data(*, used: bool) -> str:
    if used:
        return (
            "0xdcd621a2"
            + _abi_address_hex(ACCOUNT)
            + _abi_uint_hex(7)
            + _abi_uint_hex(13)
        )
    return "0x8c1009b5" + _abi_address_hex(ACCOUNT)



@pytest_asyncio.fixture(autouse=True)
async def _dispose_pools_after_test():
    try:
        yield
    finally:
        await engine.dispose()
        await position_ledger_lock_engine.dispose()


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


def _market_payload() -> dict[str, Any]:
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
                    "mark_price": "81360.1",
                    "max_position_size": "100000000",
                    "open_interest": "100",
                    "post_only": False,
                    "reduce_only": False,
                    "active": True,
                }
            ]
        }
    }


def _portfolio_payload(*, size: str) -> dict[str, Any]:
    return {
        "data": {
            "summary": {
                "usdc_balance": "2000",
                "collateral_margin_balance": "2000",
                "total_account_value": "2001.20",
                "total_notional": "17.238",
                "total_unrealized_pnl": "0.959",
                "free_collateral": "2000.857",
                "in_liquidation": False,
                "risk_level": "NORMAL",
            },
            "positions": [
                {
                    "market_id": "1",
                    "size": size,
                    "mark_price": "81360.1",
                    "avg_entry_price": "81397.08",
                    "liquidation_price": "50000",
                }
            ],
        }
    }


def _history_order(
    *,
    client_order_id: str = BIG_CLIENT_ORDER_ID_TEXT,
    status: str = "ORDER_STATUS_FILLED",
    filled_size: str = "0.5",
    avg_price: str | None = "81430.3",
    sender: str = ACCOUNT,
    order_id: str = "order-observed-big",
    created_at: str | None = None,
) -> dict[str, Any]:
    created_ns = created_at or str(
        int((datetime.now(UTC) + timedelta(seconds=1)).timestamp() * 1_000_000_000)
    )
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
        "created_at": created_ns,
        "tx_hash": "0x" + ("55" * 32),
        "client_order_id": client_order_id,
        "wide_order_id": "224552",
        "resting_order_id": "112276",
    }
    if avg_price is not None:
        order["avg_price"] = avg_price
    return order


def _orders_page(
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
        *,
        orders_pages: dict[int, dict[str, Any]] | None = None,
        position_size: str = "0.5",
        orders_error: Exception | None = None,
        malformed_orders: bool = False,
        transaction_probe: Callable[[], bool] | None = None,
        on_orders_read: Callable[[], Any] | None = None,
    ) -> None:
        self.orders_pages = orders_pages or {1: _orders_page([])}
        self.position_size = position_size
        self.orders_error = orders_error
        self.malformed_orders = malformed_orders
        self.transaction_probe = transaction_probe
        self.on_orders_read = on_orders_read
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls = 0

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.transaction_probe is not None:
            assert self.transaction_probe() is False, (
                "RED: no SQLAlchemy transaction may remain open during RISEx reads"
            )
        self.calls.append((path, params))
        if path == "/v1/orders":
            if self.orders_error is not None:
                raise self.orders_error
            if self.on_orders_read is not None:
                result = self.on_orders_read()
                if inspect.isawaitable(result):
                    await result
            if self.malformed_orders:
                return {
                    "data": {
                        "orders": "malformed",
                        "page": 1,
                        "has_next_page": False,
                    }
                }
            page = int((params or {}).get("page", 1))
            return self.orders_pages.get(page, _orders_page([], page=page))
        if path == "/v1/markets":
            return _market_payload()
        if path == "/v1/portfolio/details":
            return _portfolio_payload(size=self.position_size)
        if path == "/v1/orders/open":
            return {"data": {"orders": [], "total_orders": 0}}
        if path == "/v1/trade-history":
            return {
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
        raise AssertionError(f"unexpected RISEx GET: {path}")

    async def post_json(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        self.post_calls += 1
        raise AssertionError("4C resolver must never POST")


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
        transaction_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.consumed = consumed
        self.nonce_anchor = nonce_anchor
        self.nonce_bitmap_index = nonce_bitmap_index
        self.state_anchor = nonce_anchor if state_anchor is None else state_anchor
        self.error = error
        self.transaction_probe = transaction_probe
        self.calls = 0
        self.eth_call_count = 0

    async def call(self, method: str, params: list[object]) -> object:
        if self.transaction_probe is not None:
            assert self.transaction_probe() is False, (
                "RED: no SQLAlchemy transaction may remain open during RISEx RPC reads"
            )
        self.calls += 1
        if self.error is not None:
            raise self.error
        if method == "eth_blockNumber":
            assert params == []
            return "0x64"
        assert method == "eth_call"
        assert len(params) == 2
        request, block_tag = params
        assert isinstance(request, dict)
        assert request.get("to") == AUTHORIZATION
        assert block_tag == "0x64"
        self.eth_call_count += 1
        phase = ((self.eth_call_count - 1) % 2) + 1
        if phase == 1:
            assert request.get("data") == _expected_nonce_call_data(used=True)
            return "0x" + (1 if self.consumed else 0).to_bytes(32, "big").hex()
        assert request.get("data") == _expected_nonce_call_data(used=False)
        bitmap = (1 << self.nonce_bitmap_index) if self.consumed else 0
        return "0x" + (
            self.state_anchor.to_bytes(32, "big")
            + bitmap.to_bytes(32, "big")
        ).hex()


async def _seed_case(
    *,
    suffix: str,
    execution_state: ExecutionState = ExecutionState.SUBMITTING,
    job_state: JobState = JobState.RETRYING,
    ledger_size: Decimal = Decimal("0.1"),
    client_order_id: int = BIG_CLIENT_ORDER_ID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    created_at = now - timedelta(seconds=60)
    user_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    wallet = "0x" + uuid.uuid4().hex + "00000000"

    async with SessionLocal() as db:
        user = User(
            id=user_id,
            auth_wallet=wallet,
            state=UserState.SUSPENDED,
            execution_provider="risex",
        )
        db.add(user)
        await db.flush()
        db.add(
            ExecutionEpoch(
                id=epoch_id,
                user_id=user_id,
                provider="risex",
                network="testnet",
                account_address=ACCOUNT,
                credential_version=1,
                started_at=created_at - timedelta(minutes=1),
            )
        )
        await db.flush()
        user.active_execution_epoch_id = epoch_id
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset="BTC",
                origin="EVENT",
                state=job_state,
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={
                    "execution_provider": "risex",
                    "follower_network": "testnet",
                },
                created_at=created_at,
            )
        )
        await db.flush()
        db.add(
            PositionLedger(
                user_id=user_id,
                asset="BTC",
                size=ledger_size,
                target_size=Decimal("0.5"),
                mark_price=Decimal("80000"),
                managed=True,
                last_execution_id=(
                    execution_id
                    if execution_state
                    in {
                        ExecutionState.FILLED,
                        ExecutionState.CANCELED,
                        ExecutionState.REJECTED,
                    }
                    else None
                ),
                exchange_verified_at=(
                    now
                    if execution_state
                    in {
                        ExecutionState.FILLED,
                        ExecutionState.CANCELED,
                        ExecutionState.REJECTED,
                    }
                    else None
                ),
            )
        )
        db.add(
            Execution(
                id=execution_id,
                copy_job_id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                attempt_kind="o",
                cloid="0x" + uuid.uuid4().hex,
                client_order_id=client_order_id,
                reserved_exposure_usdc=Decimal("50"),
                nonce_anchor=7,
                nonce_bitmap_index=13,
                state=execution_state,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.5"),
                reduce_only=False,
                limit_px=Decimal("82000"),
                exchange_oid=(
                    "already-resolved"
                    if execution_state == ExecutionState.FILLED
                    else None
                ),
                filled_size=(
                    Decimal("0.5")
                    if execution_state == ExecutionState.FILLED
                    else Decimal("0")
                ),
                avg_price=(
                    Decimal("81430.3")
                    if execution_state == ExecutionState.FILLED
                    else None
                ),
                resolved_at=(
                    now
                    if execution_state
                    in {
                        ExecutionState.FILLED,
                        ExecutionState.CANCELED,
                        ExecutionState.REJECTED,
                    }
                    else None
                ),
                response={
                    "risex_4b_bis": {
                        "submission_status": (
                            "TERMINAL_EVIDENCE_PERSISTED"
                            if execution_state
                            in {
                                ExecutionState.FILLED,
                                ExecutionState.CANCELED,
                                ExecutionState.REJECTED,
                            }
                            else "POST_IN_FLIGHT"
                        )
                    }
                },
                created_at=created_at,
            )
        )
        await db.commit()

    return user_id, epoch_id, job_id, execution_id


async def _add_job(
    *,
    user_id: uuid.UUID,
    epoch_id: uuid.UUID,
    asset: str,
    state: JobState = JobState.RETRYING,
    with_execution: ExecutionState | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    job_id = uuid.uuid4()
    execution_id = uuid.uuid4() if with_execution is not None else None
    async with SessionLocal() as db:
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset=asset,
                origin="EVENT",
                state=state,
                attempt_count=1,
                correlation_id=f"4c-peer-{uuid.uuid4().hex}",
                context={
                    "execution_provider": "risex",
                    "follower_network": "testnet",
                },
            )
        )
        await db.flush()
        if execution_id is not None and with_execution is not None:
            db.add(
                Execution(
                    id=execution_id,
                    copy_job_id=job_id,
                    user_id=user_id,
                    execution_epoch_id=epoch_id,
                    execution_provider="risex",
                    execution_network="testnet",
                    attempt_kind="o",
                    cloid="0x" + uuid.uuid4().hex,
                    client_order_id=1 + (uuid.uuid4().int % ((1 << 64) - 1)),
                    reserved_exposure_usdc=Decimal("10"),
                    nonce_anchor=8,
                    nonce_bitmap_index=14,
                    state=with_execution,
                    asset=asset,
                    is_buy=True,
                    requested_size=Decimal("0.1"),
                    reduce_only=False,
                    limit_px=Decimal("82000"),
                    response={
                        "risex_4b_bis": {"submission_status": "POST_IN_FLIGHT"}
                    },
                )
            )
        await db.commit()
    return job_id, execution_id


async def _cleanup(user_id: uuid.UUID) -> None:
    async with SessionLocal() as db:
        await db.execute(
            delete(PositionLedger).where(PositionLedger.user_id == user_id)
        )
        await db.execute(delete(Execution).where(Execution.user_id == user_id))
        await db.execute(delete(CopyJob).where(CopyJob.user_id == user_id))
        await db.execute(
            delete(ExecutionEpoch).where(ExecutionEpoch.user_id == user_id)
        )
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _run_resolver(
    db,
    *,
    api: FakeAPI,
    rpc: FakeRPC,
    user_id: uuid.UUID,
):
    _module, resolver = _require_symbol("resolve_risex_ambiguous_executions")
    return await resolver(
        db,
        api=api,
        rpc=rpc,
        authorization_address=AUTHORIZATION,
        user_id=user_id,
        max_history_pages=3,
    )


def _reservation_is_active(execution: Execution) -> bool:
    return (
        execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        and Decimal(execution.reserved_exposure_usdc or 0) > 0
    )


async def _durable(
    *,
    execution_id: uuid.UUID,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[Execution, CopyJob, PositionLedger]:
    async with SessionLocal() as db:
        execution = await db.get(Execution, execution_id)
        job = await db.get(CopyJob, job_id)
        ledger = (
            await db.execute(
                select(PositionLedger).where(
                    PositionLedger.user_id == user_id,
                    PositionLedger.asset == "BTC",
                )
            )
        ).scalar_one()
        assert execution is not None and job is not None
        return execution, job, ledger


@pytest.mark.asyncio
async def test_i1_free_nonce_stays_unresolved_reserved_retrying_and_never_posts_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(suffix="free")
    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=False)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert _reservation_is_active(execution)
        assert job.state == JobState.RETRYING
        assert ledger.size == Decimal("0.1")
        assert api.post_calls == 0
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i2_consumed_exact_filled_settles_execution_ledger_and_done_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(suffix="filled")
    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert execution.filled_size == Decimal("0.5")
        assert execution.avg_price == Decimal("81430.3")
        assert not _reservation_is_active(execution)
        assert job.state == JobState.DONE
        assert ledger.size == Decimal("0.5")
        assert ledger.last_execution_id == execution_id
        assert api.post_calls == 0
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_history_lookback_accepts_order_truncated_to_previous_second_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="history-lookback-previous-second"
    )
    async with SessionLocal() as db:
        seeded = await db.get(Execution, execution_id)
        assert seeded is not None
        previous_second_ns = (
            int(seeded.created_at.timestamp()) - 1
        ) * 1_000_000_000

    api = FakeAPI(
        orders_pages={
            1: _orders_page(
                [_history_order(created_at=str(previous_second_ns))]
            )
        },
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert execution.filled_size == Decimal("0.5")
        assert job.state == JobState.DONE
        assert ledger.size == Decimal("0.5")
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i3_consumed_partial_fill_is_filled_with_real_size_and_done_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(suffix="partial")
    # documentato, non osservato il 27/09
    partial = _history_order(
        status="ORDER_STATUS_CANCELLED",
        filled_size="0.2",
        avg_price="81360.1",
    )
    api = FakeAPI(
        orders_pages={1: _orders_page([partial])},
        position_size="0.3",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert execution.filled_size == Decimal("0.2")
        assert execution.avg_price == Decimal("81360.1")
        assert job.state == JobState.DONE
        assert ledger.size == Decimal("0.3")
        assert api.post_calls == 0
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i4_consumed_zero_fill_cancel_settles_canceled_ledger_and_skipped_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(suffix="cancel")
    # documentato, non osservato il 27/09
    canceled = _history_order(
        status="ORDER_STATUS_CANCELLED",
        filled_size="0",
        avg_price="0",
    )
    api = FakeAPI(
        orders_pages={1: _orders_page([canceled])},
        position_size="0.1",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.CANCELED
        assert execution.filled_size == Decimal("0")
        assert not _reservation_is_active(execution)
        assert job.state == JobState.SKIPPED
        assert ledger.size == Decimal("0.1")
        assert ledger.last_execution_id == execution_id
        assert api.post_calls == 0
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "filled_size"),
    [
        ("ORDER_STATUS_FILLED", "0.5"),
        # documentato, non osservato il 27/09
        ("ORDER_STATUS_CANCELLED", "0"),
        # documentato, non osservato il 27/09
        ("ORDER_STATUS_OPEN", "0"),
        # documentato, non osservato il 27/09
        ("ORDER_STATUS_NONE", "0"),
        ("ORDER_STATUS_FUTURE_UNKNOWN", "0"),
    ],
)
async def test_i5_history_statuses_never_create_rejected_execution_integration(
    status: str,
    filled_size: str,
) -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix=f"never-rejected-{status}"
    )
    api = FakeAPI(
        orders_pages={
            1: _orders_page(
                [
                    _history_order(
                        status=status,
                        filled_size=filled_size,
                        avg_price="81430.3" if Decimal(filled_size) > 0 else "0",
                    )
                ]
            )
        },
        position_size="0.5" if Decimal(filled_size) > 0 else "0.1",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, _job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state != ExecutionState.REJECTED
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i6_open_order_remains_unresolved_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(suffix="open")
    # documentato, non osservato il 27/09
    api = FakeAPI(
        orders_pages={
            1: _orders_page(
                [_history_order(status="ORDER_STATUS_OPEN", filled_size="0", avg_price="0")]
            )
        }
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert _reservation_is_active(execution)
        assert job.state == JobState.RETRYING
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i7_unavailable_order_history_remains_unresolved_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="history-unavailable"
    )
    api = FakeAPI(orders_error=TimeoutError("synthetic history timeout"))
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert job.state == JobState.RETRYING
        assert _reservation_is_active(execution)
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i8_malformed_order_history_remains_unresolved_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="history-malformed"
    )
    api = FakeAPI(malformed_orders=True)
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert job.state == JobState.RETRYING
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i9_unavailable_nonce_rpc_remains_unresolved_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="rpc-unavailable"
    )
    api = FakeAPI(orders_pages={1: _orders_page([_history_order()])})
    rpc = FakeRPC(
        consumed=True,
        error=ProviderReadUnavailable("synthetic nonce RPC unavailable"),
    )

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert job.state == JobState.RETRYING
        assert _reservation_is_active(execution)
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i10_inconsistent_nonce_evidence_remains_unresolved_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="nonce-inconsistent"
    )
    api = FakeAPI(orders_pages={1: _orders_page([_history_order()])})
    rpc = FakeRPC(consumed=True, state_anchor=8)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert job.state == JobState.RETRYING
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i11_restart_with_already_resolved_execution_is_idempotent_and_never_posts_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="already-resolved",
        execution_state=ExecutionState.FILLED,
        job_state=JobState.DONE,
        ledger_size=Decimal("0.5"),
    )
    api = FakeAPI(orders_pages={1: _orders_page([_history_order()])})
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert execution.filled_size == Decimal("0.5")
        assert execution.avg_price == Decimal("81430.3")
        assert job.state == JobState.DONE
        assert ledger.size == Decimal("0.5")
        assert ledger.last_execution_id == execution_id
        assert api.post_calls == 0
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i12_read_only_resolver_works_with_signed_write_window_disabled_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "false")
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="window-disabled"
    )
    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert job.state == JobState.DONE
        assert api.post_calls == 0
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i13_no_database_transaction_is_open_during_4c_network_reads_integration() -> None:
    user_id, _epoch_id, _job_id, _execution_id = await _seed_case(
        suffix="transaction-boundary"
    )

    observations: list[bool] = []

    try:
        async with SessionLocal() as db:
            def transaction_probe() -> bool:
                current = db.in_transaction()
                observations.append(current)
                return current

            api = FakeAPI(
                orders_pages={1: _orders_page([_history_order()])},
                position_size="0.5",
                transaction_probe=transaction_probe,
            )
            rpc = FakeRPC(
                consumed=True,
                transaction_probe=transaction_probe,
            )
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        assert observations, "4C resolver must perform at least one provider/RPC read"
        assert all(value is False for value in observations), (
            "RED: no SQLAlchemy transaction may remain open during any 4C network read"
        )
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i14_ledger_baseline_change_during_reads_rolls_back_without_writing_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="baseline-race"
    )

    async def change_ledger() -> None:
        async with SessionLocal() as concurrent:
            ledger = (
                await concurrent.execute(
                    select(PositionLedger).where(
                        PositionLedger.user_id == user_id,
                        PositionLedger.asset == "BTC",
                    )
                )
            ).scalar_one()
            ledger.size = Decimal("0.2")
            await concurrent.commit()

    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
        on_orders_read=change_ledger,
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert job.state == JobState.RETRYING
        assert ledger.size == Decimal("0.2")
        assert ledger.last_execution_id is None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i15_unresolved_peer_same_user_asset_blocks_4c_settlement_integration() -> None:
    user_id, epoch_id, job_id, execution_id = await _seed_case(
        suffix="peer-fence"
    )
    _peer_job_id, peer_execution_id = await _add_job(
        user_id=user_id,
        epoch_id=epoch_id,
        asset="BTC",
        with_execution=ExecutionState.UNKNOWN,
    )
    assert peer_execution_id is not None
    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        async with SessionLocal() as db:
            peer = await db.get(Execution, peer_execution_id)
        assert peer is not None
        assert execution.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}
        assert peer.state == ExecutionState.UNKNOWN
        assert job.state == JobState.RETRYING
        assert ledger.size == Decimal("0.1")
        assert ledger.last_execution_id is None
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i16_resolving_execution_releases_229_same_asset_submission_fence_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, epoch_id, _job_id, _execution_id = await _seed_case(
        suffix="release-229-fence"
    )
    new_job_id, _ = await _add_job(
        user_id=user_id,
        epoch_id=epoch_id,
        asset="BTC",
        state=JobState.PROCESSING,
    )
    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)
    credential_calls: list[uuid.UUID] = []

    class ReachedCredentialResolution(RuntimeError):
        pass

    async def reached_credential_resolution(_db, job):
        credential_calls.append(job.id)
        raise ReachedCredentialResolution("same-asset fence released")

    monkeypatch.setattr(
        risex_worker_submission,
        "assert_risex_worker_write_allowed",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        risex_worker_submission,
        "resolve_risex_worker_credential",
        reached_credential_resolution,
    )

    try:
        async with SessionLocal() as db:
            new_job = await db.get(CopyJob, new_job_id)
            assert new_job is not None
            before = await risex_worker_submission.prepare_risex_worker_submission(
                db,
                new_job,
                readiness_assertions={"operatorhub_bypass_disabled": True},
            )
            assert before is None
            assert credential_calls == []

        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        async with SessionLocal() as db:
            new_job = await db.get(CopyJob, new_job_id)
            assert new_job is not None
            with pytest.raises(
                ReachedCredentialResolution,
                match="same-asset fence released",
            ):
                await risex_worker_submission.prepare_risex_worker_submission(
                    db,
                    new_job,
                    readiness_assertions={"operatorhub_bypass_disabled": True},
                )
        assert credential_calls == [new_job_id]
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i18_risex_resolver_is_wired_only_into_existing_execution_worker_maintenance_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = getattr(execution_worker, "resolve_risex_ambiguous_executions", None)
    assert callable(resolver), (
        "RED: execution-worker must import the RISEx 4C resolver into maintenance"
    )

    calls = 0
    test_stop = execution_worker.asyncio.Event()
    original_sleep = execution_worker.asyncio.sleep

    async def fake_resolver(*_args: object, **_kwargs: object) -> dict[str, int]:
        nonlocal calls
        calls += 1
        test_stop.set()
        return {"resolved": 0, "unresolved": 0}

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_reconcile(*_args: object, **_kwargs: object) -> bool:
        return True

    async def yielding_sleep(_seconds: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(
        execution_worker,
        "resolve_risex_ambiguous_executions",
        fake_resolver,
    )
    monkeypatch.setattr(execution_worker, "stop", test_stop)
    monkeypatch.setattr(
        execution_worker,
        "_quarantine_stale_admin_leverage_jobs",
        no_op,
    )
    monkeypatch.setattr(execution_worker, "release_stale_jobs", no_op)
    monkeypatch.setattr(execution_worker, "repair_stream", no_op)
    monkeypatch.setattr(execution_worker, "monitor_credential_expiry", no_op)
    monkeypatch.setattr(execution_worker.Worker, "_poll_risex_control_once", no_op)
    monkeypatch.setattr(execution_worker.Worker, "_maintain_risex_window_once", no_op)
    monkeypatch.setattr(
        execution_worker.Worker,
        "_run_reconcile_with_deadline",
        no_reconcile,
    )
    monkeypatch.setattr(execution_worker.Worker, "heartbeat", no_op)
    monkeypatch.setattr(execution_worker.asyncio, "sleep", yielding_sleep)

    worker = object.__new__(execution_worker.Worker)
    worker.redis = None
    try:
        await execution_worker.asyncio.wait_for(worker.maintenance(), timeout=0.5)
    except TimeoutError:
        pytest.fail(
            "RED: maintenance imported the RISEx resolver but never awaited it",
            pytrace=False,
        )

    assert calls == 1, (
        "RED: one execution-worker maintenance cycle must actually await "
        "resolve_risex_ambiguous_executions"
    )
    assert "resolve_risex_ambiguous_executions" not in inspect.getsource(
        execution_worker.Worker.consume
    )
    assert "resolve_risex_ambiguous_executions" not in inspect.getsource(
        execution_worker.Worker._run_risex_copy_job
    )


def test_i19_no_new_watcher_stream_or_worker_process_for_risex_4c_integration() -> None:
    watcher_source = inspect.getsource(watcher)
    worker_run_source = inspect.getsource(execution_worker.Worker.run)
    workers_dir = Path(execution_worker.__file__).resolve().parent
    risex_worker_files = [
        path.name
        for path in workers_dir.glob("*.py")
        if "risex" in path.name.lower()
    ]

    assert "risex_execution_resolution" not in watcher_source
    assert "asyncio.create_task(self.consume())" in worker_run_source
    assert "asyncio.create_task(self.maintenance())" in worker_run_source
    assert risex_worker_files == []


def test_i20_hyperliquid_ambiguity_resolver_and_loop_remain_unchanged_integration() -> None:
    assert resolve_hyperliquid_ambiguous_executions.__module__ == (
        "app.services.execution_resolution"
    )
    source = inspect.getsource(execution_worker.Worker.run_reconcile_if_leader)
    assert "follower_hl=self.follower_hl(network)" in source
    assert "resolution=await resolve_ambiguous_executions(db,follower_hl)" in source
    assert "reconcile_active_users(" in source


@pytest.mark.asyncio
async def test_i21_no_candidates_builds_no_transports_and_performs_zero_provider_io_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, resolver = _require_symbol("resolve_risex_ambiguous_executions")
    signature = inspect.signature(resolver)
    for name in ("api", "rpc", "authorization_address", "user_id"):
        assert signature.parameters[name].default is None
    assert "max_history_pages" in signature.parameters
    assert "batch_limit" in signature.parameters

    constructed = {"api": 0, "rpc": 0}

    def fail_api(**_kwargs):
        constructed["api"] += 1
        raise AssertionError("RED: HTTP transport must not be built without candidates")

    def fail_rpc(**_kwargs):
        constructed["rpc"] += 1
        raise AssertionError("RED: RPC transport must not be built without candidates")

    monkeypatch.setattr(module, "RISExReadOnlyHTTPTransport", fail_api, raising=False)
    monkeypatch.setattr(module, "RISExReadOnlyRPCTransport", fail_rpc, raising=False)

    async with SessionLocal() as db:
        result = await resolver(
            db,
            user_id=uuid.uuid4(),
            max_history_pages=3,
            batch_limit=10,
        )

    assert constructed == {"api": 0, "rpc": 0}
    assert result.get("candidates", 0) == 0


@pytest.mark.asyncio
async def test_i22_global_candidate_selection_starts_from_risex_execution_without_trading_account_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="global-selection-no-trading-account"
    )
    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            trading_account = (
                await db.execute(
                    select(TradingAccount).where(TradingAccount.user_id == user_id)
                )
            ).scalar_one_or_none()
            assert trading_account is None

            _module, resolver = _require_symbol("resolve_risex_ambiguous_executions")
            await resolver(
                db,
                api=api,
                rpc=rpc,
                authorization_address=AUTHORIZATION,
                user_id=None,
                max_history_pages=3,
                batch_limit=20,
            )

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert job.state == JobState.DONE
        assert ledger.last_execution_id == execution_id
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i23_one_candidate_failure_does_not_block_another_user_in_same_batch_integration() -> None:
    first_user, _first_epoch, first_job, first_execution = await _seed_case(
        suffix="batch-isolation-first"
    )
    second_user, _second_epoch, second_job, second_execution = await _seed_case(
        suffix="batch-isolation-second",
        client_order_id=int(OTHER_OBSERVED_CLIENT_ORDER_ID_TEXT),
    )

    class IsolatingAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__(position_size="0.5")
            self.order_reads = 0

        async def get_json(self, path: str, *, params=None):
            if path == "/v1/orders":
                self.order_reads += 1
                if self.order_reads == 1:
                    return {
                        "data": {
                            "orders": "malformed",
                            "page": 1,
                            "has_next_page": False,
                        }
                    }
                return _orders_page(
                    [
                        _history_order(),
                        _history_order(
                            client_order_id=OTHER_OBSERVED_CLIENT_ORDER_ID_TEXT,
                            order_id="order-observed-second",
                        ),
                    ]
                )
            return await super().get_json(path, params=params)

    api = IsolatingAPI()
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            _module, resolver = _require_symbol("resolve_risex_ambiguous_executions")
            result = await resolver(
                db,
                api=api,
                rpc=rpc,
                authorization_address=AUTHORIZATION,
                user_id=None,
                max_history_pages=3,
                batch_limit=20,
            )

        async with SessionLocal() as db:
            first = await db.get(Execution, first_execution)
            second = await db.get(Execution, second_execution)
            first_durable_job = await db.get(CopyJob, first_job)
            second_durable_job = await db.get(CopyJob, second_job)
            assert first is not None and second is not None
            assert first_durable_job is not None and second_durable_job is not None

            states = {first.state, second.state}
            assert ExecutionState.FILLED in states
            assert (
                ExecutionState.SUBMITTING in states
                or ExecutionState.UNKNOWN in states
            )
            assert JobState.DONE in {
                first_durable_job.state,
                second_durable_job.state,
            }
            assert JobState.RETRYING in {
                first_durable_job.state,
                second_durable_job.state,
            }
        assert result.get("resolved", 0) == 1
        assert result.get("unresolved", 0) >= 1
    finally:
        await _cleanup(first_user)
        await _cleanup(second_user)


@pytest.mark.asyncio
async def test_i24_maintenance_times_out_4c_resolver_and_still_repairs_and_heartbeats_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver_started = 0
    repair_calls = 0
    heartbeat_calls = 0
    test_stop = execution_worker.asyncio.Event()
    original_sleep = execution_worker.asyncio.sleep

    async def hanging_resolver(*_args: object, **_kwargs: object) -> dict[str, int]:
        nonlocal resolver_started
        resolver_started += 1
        await execution_worker.asyncio.Event().wait()
        return {"resolved": 0}

    async def fake_repair(*_args: object, **_kwargs: object) -> int:
        nonlocal repair_calls
        repair_calls += 1
        return 0

    async def fake_heartbeat(*_args: object, **_kwargs: object) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        test_stop.set()

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_reconcile(*_args: object, **_kwargs: object) -> bool:
        return True

    async def yielding_sleep(_seconds: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(
        execution_worker,
        "resolve_risex_ambiguous_executions",
        hanging_resolver,
        raising=False,
    )
    monkeypatch.setattr(
        execution_worker,
        "_RISEX_4C_RESOLUTION_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr(execution_worker, "stop", test_stop)
    monkeypatch.setattr(execution_worker, "repair_stream", fake_repair)
    monkeypatch.setattr(
        execution_worker,
        "_quarantine_stale_admin_leverage_jobs",
        no_op,
    )
    monkeypatch.setattr(execution_worker, "release_stale_jobs", no_op)
    monkeypatch.setattr(execution_worker, "monitor_credential_expiry", no_op)
    monkeypatch.setattr(execution_worker.Worker, "_poll_risex_control_once", no_op)
    monkeypatch.setattr(execution_worker.Worker, "_maintain_risex_window_once", no_op)
    monkeypatch.setattr(
        execution_worker.Worker,
        "_run_reconcile_with_deadline",
        no_reconcile,
    )
    monkeypatch.setattr(execution_worker.Worker, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(execution_worker.asyncio, "sleep", yielding_sleep)

    worker = object.__new__(execution_worker.Worker)
    worker.redis = None

    try:
        await execution_worker.asyncio.wait_for(worker.maintenance(), timeout=0.5)
    except TimeoutError:
        pytest.fail(
            "RED: a hung RISEx 4C resolver must not stall maintenance",
            pytrace=False,
        )

    assert resolver_started == 1
    assert repair_calls == 2, (
        "RED: the same maintenance cycle must execute both repair_stream phases"
    )
    assert heartbeat_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_epoch",
    ["network", "provider", "account"],
)
async def test_i25_invalid_risex_epoch_is_skipped_without_provider_io_or_writes_integration(
    invalid_epoch: str,
) -> None:
    user_id, epoch_id, job_id, execution_id = await _seed_case(
        suffix=f"invalid-epoch-{invalid_epoch}"
    )

    async with SessionLocal() as db:
        epoch = await db.get(ExecutionEpoch, epoch_id)
        assert epoch is not None
        if invalid_epoch == "network":
            epoch.network = "mainnet"
        elif invalid_epoch == "provider":
            epoch.provider = "hyperliquid"
        else:
            epoch.account_address = None
        await db.commit()

    api = FakeAPI(orders_pages={1: _orders_page([_history_order()])})
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            _module, resolver = _require_symbol("resolve_risex_ambiguous_executions")
            await resolver(
                db,
                api=api,
                rpc=rpc,
                authorization_address=AUTHORIZATION,
                user_id=user_id,
                max_history_pages=3,
                batch_limit=10,
            )

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state in {
            ExecutionState.SUBMITTING,
            ExecutionState.UNKNOWN,
        }
        assert job.state == JobState.RETRYING
        assert ledger.size == Decimal("0.1")
        assert api.calls == []
        assert rpc.calls == 0
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i26_terminal_resolution_persists_nonsecret_4c_audit_evidence_integration() -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="audit-evidence"
    )
    order = _history_order()
    api = FakeAPI(
        orders_pages={1: _orders_page([order])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)

    try:
        async with SessionLocal() as db:
            await _run_resolver(db, api=api, rpc=rpc, user_id=user_id)

        execution, job, _ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert execution.exchange_oid == order["id"]
        assert job.state == JobState.DONE

        evidence = (execution.response or {}).get("risex_4c")
        assert evidence == {
            "nonce_block_number": 100,
            "tx_hash": order["tx_hash"],
            "order_block_number": order["block_number"],
            "status": order["status"],
            "source": "/v1/orders",
        }
        serialized = repr(evidence).lower()
        assert "signature" not in serialized
        assert "private" not in serialized
        assert "secret" not in serialized
    finally:
        await _cleanup(user_id)


@pytest.mark.asyncio
async def test_i27_4c_settlement_holds_position_ledger_lock_against_concurrent_writer_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, _epoch_id, job_id, execution_id = await _seed_case(
        suffix="position-lock-behavior"
    )
    api = FakeAPI(
        orders_pages={1: _orders_page([_history_order()])},
        position_size="0.5",
    )
    rpc = FakeRPC(consumed=True)
    module, resolver = _require_symbol("resolve_risex_ambiguous_executions")
    original_apply = module._apply_snapshot_under_settlement_lock
    contender_entered = execution_worker.asyncio.Event()
    contender_task = None

    async def contender() -> None:
        async with position_ledger_lock(user_id):
            contender_entered.set()

    async def observed_apply(db, execution, snapshot):
        nonlocal contender_task
        contender_task = execution_worker.asyncio.create_task(contender())
        try:
            await execution_worker.asyncio.wait_for(
                contender_entered.wait(),
                timeout=0.05,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError(
                "RED: concurrent same-user ledger writer entered before 4C "
                "settlement released position_ledger_lock"
            )
        return await original_apply(db, execution, snapshot)

    monkeypatch.setattr(
        module,
        "_apply_snapshot_under_settlement_lock",
        observed_apply,
    )

    try:
        async with SessionLocal() as db:
            await resolver(
                db,
                api=api,
                rpc=rpc,
                authorization_address=AUTHORIZATION,
                user_id=user_id,
                max_history_pages=3,
                batch_limit=10,
            )

        assert contender_task is not None
        await execution_worker.asyncio.wait_for(contender_task, timeout=0.5)
        assert contender_entered.is_set()

        execution, job, ledger = await _durable(
            execution_id=execution_id,
            job_id=job_id,
            user_id=user_id,
        )
        assert execution.state == ExecutionState.FILLED
        assert job.state == JobState.DONE
        assert ledger.last_execution_id == execution_id
    finally:
        if contender_task is not None and not contender_task.done():
            contender_task.cancel()
            await execution_worker.asyncio.gather(
                contender_task,
                return_exceptions=True,
            )
        await _cleanup(user_id)
