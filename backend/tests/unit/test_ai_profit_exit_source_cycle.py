from datetime import UTC, datetime, timedelta
from decimal import Decimal
import uuid

from app.models.entities import MasterEvent
from app.services.ai_profit_exit import resolve_current_source_cycle


BASE = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _event(
    order: int | None,
    start: str,
    after: str,
    *,
    asset: str = "BTC",
    seconds: int = 0,
) -> MasterEvent:
    return MasterEvent(
        id=uuid.uuid4(),
        exchange_event_id=uuid.uuid4().hex,
        asset=asset,
        side="B" if Decimal(after) > Decimal(start) else "A",
        size=abs(Decimal(after) - Decimal(start)),
        price=Decimal("100"),
        start_position=Decimal(start),
        position_after=Decimal(after),
        master_equity=Decimal("1000"),
        event_ts=BASE + timedelta(seconds=seconds),
        raw={"_hypercopy_network": "mainnet"},
        fencing_token=1,
        causal_order=order,
    )


def test_master_event_exposes_nullable_durable_causal_order():
    column = MasterEvent.__table__.c.causal_order

    # Legacy events may predate this feature, so the DB migration must remain
    # additive. New AI-eligible events will require a populated causal order.
    assert column.nullable is True


def test_long_scale_in_and_reduction_remain_one_verified_cycle():
    opened = _event(100, "0", "1", seconds=1)
    scale_in = _event(101, "1", "2", seconds=2)
    reduced = _event(102, "2", "1.25", seconds=3)

    cycle = resolve_current_source_cycle(
        [opened, scale_in, reduced],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("1.25"),
    )

    assert cycle is not None
    assert cycle.open_event_id == opened.id
    assert cycle.source_cycle_id == f"mainnet:BTC:{opened.id}"
    assert cycle.state_version == 102
    assert cycle.master_position == Decimal("1.25")
    assert cycle.side == "LONG"


def test_short_scale_in_and_reduction_remain_one_verified_cycle():
    opened = _event(200, "0", "-1", seconds=1)
    scale_in = _event(201, "-1", "-2", seconds=2)
    reduced = _event(202, "-2", "-0.5", seconds=3)

    cycle = resolve_current_source_cycle(
        [opened, scale_in, reduced],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("-0.5"),
    )

    assert cycle is not None
    assert cycle.open_event_id == opened.id
    assert cycle.state_version == 202
    assert cycle.side == "SHORT"


def test_reversal_starts_a_new_source_cycle():
    old_open = _event(300, "0", "1", seconds=1)
    reversal = _event(301, "1", "-0.5", seconds=2)
    continuation = _event(302, "-0.5", "-1", seconds=3)

    cycle = resolve_current_source_cycle(
        [old_open, reversal, continuation],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("-1"),
    )

    assert cycle is not None
    assert cycle.open_event_id == reversal.id
    assert cycle.source_cycle_id == f"mainnet:BTC:{reversal.id}"
    assert cycle.state_version == 302
    assert cycle.side == "SHORT"


def test_flat_then_reopen_creates_a_new_cycle():
    first_open = _event(400, "0", "1", seconds=1)
    close = _event(401, "1", "0", seconds=2)
    second_open = _event(402, "0", "0.25", seconds=3)

    cycle = resolve_current_source_cycle(
        [first_open, close, second_open],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("0.25"),
    )

    assert cycle is not None
    assert cycle.open_event_id == second_open.id
    assert cycle.state_version == 402


def test_flat_current_master_has_no_active_cycle():
    opened = _event(500, "0", "1", seconds=1)
    closed = _event(501, "1", "0", seconds=2)

    assert resolve_current_source_cycle(
        [opened, closed],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("0"),
    ) is None


def test_legacy_unversioned_current_cycle_fails_closed():
    opened = _event(None, "0", "1", seconds=1)
    continued = _event(601, "1", "2", seconds=2)

    assert resolve_current_source_cycle(
        [opened, continued],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("2"),
    ) is None


def test_missing_history_without_verified_open_fails_closed():
    continued = _event(700, "1", "2", seconds=1)

    assert resolve_current_source_cycle(
        [continued],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("2"),
    ) is None


def test_broken_position_chain_fails_closed():
    opened = _event(800, "0", "1", seconds=1)
    gap = _event(801, "2", "3", seconds=2)

    assert resolve_current_source_cycle(
        [opened, gap],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("3"),
    ) is None


def test_snapshot_position_must_match_latest_verified_event():
    opened = _event(900, "0", "1", seconds=1)

    assert resolve_current_source_cycle(
        [opened],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("2"),
    ) is None


def test_wrong_asset_and_network_do_not_enter_cycle():
    btc = _event(1000, "0", "1", seconds=1)
    eth = _event(1001, "0", "5", asset="ETH", seconds=2)

    wrong_network = _event(1002, "1", "2", seconds=3)
    wrong_network.raw = {"_hypercopy_network": "testnet"}

    cycle = resolve_current_source_cycle(
        [btc, eth, wrong_network],
        asset="BTC",
        master_network="mainnet",
        current_master_position=Decimal("1"),
    )

    assert cycle is not None
    assert cycle.open_event_id == btc.id
    assert cycle.state_version == 1000
