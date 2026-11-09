"""Deterministic offline tests; never contact Hyperliquid or Railway."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_lab_candles import collect, normalize_candles
from strategy_lab_candle_replay import (
    Cycle, Leg, canonical_fills, compare, load_archive, load_fills,
    make_cycles, pnl_at, replay_cycle,
)


H = 3_600_000


def bar(t: int, close: str, *, high: str | None = None, low: str | None = None):
    p = D(close)
    hi = str(max(D(high) if high is not None else p, p))
    lo = str(min(D(low) if low is not None else p, p))
    return {
        "asset": "BTC", "interval": "1h", "t": t, "T": t + H - 1,
        "o": close, "c": close, "h": hi, "l": lo,
    }


class PublicArchiveTests(unittest.TestCase):
    def test_valid_bar_is_preserved_as_decimal_text(self):
        items = [dict(bar(0, "100.125"), s="BTC", i="1h")]
        result = normalize_candles(items, coin="BTC", interval="1h", start_ms=0, end_ms=H)
        self.assertEqual(result[0]["c"], "100.125")
        self.assertEqual(result[0]["T"], H - 1)

    def test_same_hour_is_rejected(self):
        data = [dict(bar(0, "100"), s="BTC", i="1h")] * 2
        with self.assertRaises(ValueError):
            normalize_candles(data, coin="BTC", interval="1h", start_ms=0, end_ms=H)

    def test_candle_after_requested_end_is_rejected(self):
        data = [dict(bar(H, "100"), s="BTC", i="1h")]
        with self.assertRaises(ValueError):
            normalize_candles(data, coin="BTC", interval="1h", start_ms=0, end_ms=H)

    def test_invalid_ohlc_does_not_enter_archive(self):
        data = [dict(bar(0, "100"), s="BTC", i="1h", h="99")]
        with self.assertRaises(ValueError):
            normalize_candles(data, coin="BTC", interval="1h", start_ms=0, end_ms=H)

    def test_mock_collection_keeps_missing_assets_explicit_and_hashes_archive(self):
        def fake(coin, *, interval, start_ms, end_ms):
            if coin == "ETH":
                return []
            return [bar(0, "100")]
        with tempfile.TemporaryDirectory() as tmp:
            result = collect(
                assets=["BTC", "ETH"], interval="1h", start_ms=0, end_ms=H,
                output_dir=Path(tmp), between_requests=0, request_fn=fake,
            )
            self.assertEqual(result["bars"], 1)
            self.assertEqual(result["assets"][1]["status"], "no_candles")
            actual = hashlib.sha256((Path(tmp) / "candles.jsonl").read_bytes()).hexdigest()
            self.assertEqual(result["data_sha256"], actual)
            bars, manifest = load_archive(Path(tmp))
            self.assertEqual(len(bars["BTC"]), 1)
            self.assertEqual(manifest["bars"], 1)

    def test_corrupt_archive_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "candles.jsonl").write_text("{}\n")
            (path / "manifest.json").write_text(json.dumps({
                "interval": "1h", "start_ms": 0, "end_ms": H,
                "data_sha256": "bad",
            }))
            with self.assertRaisesRegex(ValueError, "hash"):
                load_archive(path)


class ReplayTests(unittest.TestCase):
    def cycle(self, end_ms: int = 4 * H + H // 2):
        return Cycle(
            "BTC", "LONG", 0, end_ms,
            (Leg(0, D(1), D(100)), Leg(end_ms, D(-1), D(104))),
        )

    def b(self, closes=("110", "106", "105", "103", "103")):
        return [bar(i * H, v) for i, v in enumerate(closes)]

    def replay(self, cycle=None, bars=None, threshold=D(40)):
        return replay_cycle(
            cycle or self.cycle(), bars if bars is not None else self.b(),
            threshold_pct=threshold, min_peak_usdc=D(1), min_current_usdc=D("0.5"),
            fee_bps=D(0), slip_bps=D(0),
        )

    def test_giveback_signal_is_causal_and_execution_one_full_bar_later(self):
        r = self.replay()
        self.assertEqual(r["status"], "hypothetical_exit")
        self.assertEqual(r["signal_ms"], 2 * H - 1)
        self.assertEqual(r["simulated_exit_ms"], 3 * H - 1)
        self.assertEqual(r["alternative_net"], D(5))
        self.assertEqual(r["baseline_net"], D(4))
        self.assertEqual(r["delta_net"], D(1))
        self.assertTrue(r["scenario_only"])

    def test_intrabar_extremes_do_not_become_hypothetical_execution_prices(self):
        bars = [bar(i * H, "100", high="150", low="50") for i in range(5)]
        self.assertEqual(self.replay(bars=bars)["status"], "no_signal_before_master_exit")

    def test_first_candle_never_triggers_an_exit(self):
        bars = [bar(0, "110")]
        self.assertEqual(self.replay(bars=bars)["status"], "no_signal_before_master_exit")

    def test_master_change_between_decision_and_exit_cancels_signal(self):
        c = Cycle("BTC", "LONG", 0, 3 * H + H // 2, (
            Leg(0, D(1), D(100)),
            Leg(2 * H + 100, D(1), D(106)),
            Leg(3 * H + H // 2, D(-2), D(104)),
        ))
        self.assertNotEqual(self.replay(cycle=c)["status"], "hypothetical_exit")

    def test_market_gap_prevents_assumed_next_candle(self):
        bars = [bar(0, "110"), bar(H, "106"), bar(3 * H, "103")]
        self.assertEqual(self.replay(bars=bars)["status"], "no_signal_before_master_exit")

    def test_transaction_costs_can_eliminate_apparent_profit(self):
        net, pos = pnl_at(
            (Leg(0, D(1), D(100)),), H - 1, D(100),
            fee_bps=D(10), slip_bps=D(25),
        )
        self.assertEqual(pos, D(1))
        self.assertLess(net, D(0))

    def test_no_asset_data_never_turns_into_fabricated_profit(self):
        result = compare(
            [ ], {}, fee_bps=D(5), slip_bps=D(10),
        )
        self.assertEqual(result["complete_cycles"], 0)
        self.assertEqual(result["scenarios"]["40"]["hypothetical_exits"], 0)
        self.assertEqual(result["scenarios"]["40"]["delta_sum_usdc_estimated"], "0")

    def test_read_csv_and_reconstruct_flat_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fills.csv"
            with path.open("w", newline="") as out:
                writer = csv.writer(out)
                writer.writerow((
                    "event_ts", "asset", "side", "size", "price",
                    "start_position", "position_after", "master_equity",
                ))
                writer.writerow(("2026-09-01T00:00:00+00:00", "BTC", "B", "1", "100", "0", "1", "1000"))
                writer.writerow(("2026-09-01T05:00:00+00:00", "BTC", "A", "1", "105", "1", "0", "1000"))
            fills = load_fills(path)
            cycles, censored = make_cycles(fills)
            self.assertEqual(censored, 0)
            self.assertEqual(len(cycles), 1)
            self.assertEqual(cycles[0].side, "LONG")
            self.assertEqual(
                -sum((leg.signed_size * leg.price for leg in cycles[0].legs), D(0)), D(5),
            )

    def test_reversal_creates_two_distinct_cycles(self):
        from strategy_lab_candle_replay import Fill
        f = [
            Fill(0, "BTC", D(0), D(1), D(1), D(100), 0),
            Fill(H, "BTC", D(1), D(-1), D(-2), D(105), 1),
            Fill(2 * H, "BTC", D(-1), D(0), D(1), D(104), 2),
        ]
        cycles, _ = make_cycles(f)
        self.assertEqual(len(cycles), 2)
        self.assertEqual([c.side for c in cycles], ["LONG", "SHORT"])
        self.assertEqual(
            [-sum((l.signed_size * l.price for l in c.legs), D(0)) for c in cycles],
            [D(5), D(1)],
        )

    def test_invalid_scenario_parameters_fail(self):
        with self.assertRaises(ValueError):
            self.replay(threshold=D(0))


if __name__ == "__main__":
    unittest.main()
