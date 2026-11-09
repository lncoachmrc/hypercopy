"""Unit tests: causal OHLC proxy, sizing, fee and partial-cycle invariants."""
import unittest
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import gzip
import json
import sys

sys.path.insert(0, str(Path(__file__).parent))
from strategy_lab_replay import (
    Fill, Cycle, canonical_per_asset, reconstruct_cycles, baseline_net,
    candidate_observations, choose_exit, read_candles, HOUR_MS,
)

H = HOUR_MS


class StrategyLabTests(unittest.TestCase):
    def cycle(self, side="LONG", partial=False):
        if side == "LONG":
            fills = [
                Fill("BTC", int(H*0.1), D(0), D(1), D(100), 0),
                Fill("BTC", int(H*4.5), D(1), D(0), D(104), 1),
            ]
        else:
            fills = [
                Fill("BTC", int(H*0.1), D(0), D(-1), D(100), 0),
                Fill("BTC", int(H*4.5), D(-1), D(0), D(96), 1),
            ]
        if partial:
            if side != "LONG":
                raise ValueError("Only LONG partial fills configured")
            fills.insert(1, Fill("BTC", int(H*2.5), D(1), D("0.5"), D(103), 2))
            fills[-1] = Fill("BTC", int(H*4.5), D("0.5"), D(0), D(104), 1)
        return Cycle("BTC", side, int(H*0.1), int(H*4.5), fills)

    def bars(self, closes):
        return [
            dict(
                t=i*H,
                o=D(str(closes[i-1] if i else 100)),
                c=D(str(v)),
                h=max(D(110), D(str(v))),
                l=min(D(90), D(str(v))),
            )
            for i, v in enumerate(closes)
        ]

    def test_baseline_is_sum_of_all_signed_fills(self):
        long, short, part = self.cycle(), self.cycle("SHORT"), self.cycle(partial=True)
        self.assertEqual(long.gross, D(4))
        self.assertEqual(short.gross, D(4))
        self.assertEqual(part.gross, D("3.5"))
        self.assertEqual(baseline_net(long, D(0), D(0)), D(4))
        self.assertLess(baseline_net(long, D("4.5"), D(3)), D(4))

    def test_first_sample_and_no_intrabar_high_lookahead(self):
        observations = candidate_observations(
            self.cycle(), self.bars([100, 100.1, 100.2, 100.3, 101, 103]),
            D(0), D(0), D(0),
        )
        self.assertTrue(observations)
        self.assertFalse(any(x["giveback"] and x["giveback"] > 0 for x in observations))
        self.assertIsNone(choose_exit(observations, D(".20"), D(".001")))

    def test_drawdown_after_peak_causes_next_open_exit(self):
        observations = candidate_observations(
            self.cycle(), self.bars([100, 108, 106, 105, 104, 103]),
            D(0), D(0), D(0),
        )
        self.assertEqual([round(float(x["net_est"]), 1) for x in observations], [0, 8, 6, 5])
        self.assertIsNone(observations[0]["giveback"])
        self.assertEqual(observations[1]["giveback"], D(0))
        selected = choose_exit(observations, D(".20"), D(".01"))
        self.assertIsNotNone(selected)
        self.assertEqual(selected["net_est"], D(6))
        self.assertEqual(selected["alt_pnl"], D(6))

    def test_short_slippage_is_worse_on_buy_to_cover(self):
        bars = self.bars([100, 95, 96, 96, 96, 96])
        observations = candidate_observations(self.cycle("SHORT"), bars, D(0), D(0), D(20))
        self.assertEqual(observations[0]["position"], D(-1))
        self.assertEqual(observations[0]["alt_pnl"], D(100) - bars[1]["o"]*D("1.002"))

    def test_source_version_reset_after_scale_out(self):
        observations = candidate_observations(
            self.cycle(partial=True), self.bars([100, 110, 108, 105, 104, 104]),
            D(0), D(0), D(0),
        )
        self.assertTrue(any(x["state"] == 1 for x in observations))
        self.assertTrue(any(x["state"] == 2 for x in observations))
        self.assertEqual(next(x for x in observations if x["state"] == 2)["giveback"], D(0))

    def test_partial_realized_profits_do_not_make_losing_residual_eligible(self):
        cycle = self.cycle(partial=True)
        bars = self.bars([100, 110, 108, 98, 98, 98])
        observed = candidate_observations(cycle, bars, D(0), D(0), D(0))
        after_partial = [x for x in observed if x["state"] == 2][0]
        # The partial close realized +1.5; the remaining position at 98
        # is *not* profitable, even if cumulative cycle PnL can be positive.
        self.assertLess(after_partial["net_est"], D(0))
        self.assertIsNone(after_partial["giveback"])

    def test_reversal_splits_cycle_and_preserves_cashflows(self):
        fills = [
            Fill("BTC", 1, D(0), D(1), D(100), 0),
            Fill("BTC", 2, D(1), D(-1), D(105), 1),
            Fill("BTC", 3, D(-1), D(0), D(103), 2),
        ]
        cycles = [x for x in reconstruct_cycles(fills) if x.complete]
        self.assertEqual(len(cycles), 2)
        self.assertEqual([x.gross for x in cycles], [D(5), D(2)])

    def test_equal_time_order_uses_continuity_not_csv_order(self):
        fills = [
            Fill("BTC", 100, D(1), D(2), D(100), 1),
            Fill("BTC", 100, D(0), D(1), D(100), 0),
            Fill("BTC", 200, D(2), D(0), D(105), 2),
        ]
        ordered = canonical_per_asset(fills)
        self.assertEqual([x.before for x in ordered], [D(0), D(1), D(2)])
        self.assertTrue(ordered[0].tied)

    def test_candle_validation_rejects_broken_ohlc(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)/"a.json.gz"
            payload = {
                "source": "Hyperliquid public candleSnapshot", "interval": "1h",
                "markets": {"BTC": [{"t": 0, "o": "100", "h": "90", "l": "80", "c": "100"}]},
            }
            with gzip.open(path, "wt") as stream:
                json.dump(payload, stream)
            with self.assertRaises(ValueError):
                read_candles(path)

    def test_closed_candle_and_contiguous_next_open_required(self):
        bars = self.bars([100, 108, 107])
        bars[2]["t"] += H
        self.assertEqual(len(candidate_observations(self.cycle(), bars, D(0), D(0), D(0))), 1)


if __name__ == "__main__":
    unittest.main()
