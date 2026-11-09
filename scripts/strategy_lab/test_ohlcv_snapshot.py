"""Contract tests for read-only OHLCV collector (no network)."""
from __future__ import annotations

import unittest

from ohlcv_snapshot import HOUR_MS, START, validate


class CandleValidationTests(unittest.TestCase):
    @staticmethod
    def row():
        return {
            "t": START, "T": START + HOUR_MS - 1,
            "s": "BTC", "i": "1h",
            "o": "100.0", "h": "103.0", "l": "99.0", "c": "101.0",
            "v": "5.00", "n": 12,
        }

    def test_valid_hourly_candle(self):
        assert len(validate("BTC", [self.row()])) == 1

    def test_incorrect_asset_rejected(self):
        with self.assertRaises(ValueError):
            validate("ETH", [self.row()])

    def test_incorrect_interval_rejected(self):
        data = self.row()
        data["i"] = "1m"
        with self.assertRaises(ValueError):
            validate("BTC", [data])

    def test_incomplete_candle_rejected(self):
        data = self.row()
        del data["c"]
        with self.assertRaises(ValueError):
            validate("BTC", [data])

    def test_future_or_misaligned_candle_rejected(self):
        data = self.row()
        data["t"] += 1
        with self.assertRaises(ValueError):
            validate("BTC", [data])

    def test_duplicate_candle_rejected(self):
        row = self.row()
        with self.assertRaises(ValueError):
            validate("BTC", [row, row])

    def test_negative_price_rejected(self):
        data = self.row()
        data["c"] = "-1"
        with self.assertRaises(ValueError):
            validate("BTC", [data])

    def test_high_low_inconsistent_rejected(self):
        data = self.row()
        data["h"] = "98"
        with self.assertRaises(ValueError):
            validate("BTC", [data])

    def test_non_finite_value_rejected(self):
        data = self.row()
        data["v"] = "NaN"
        with self.assertRaises(ValueError):
            validate("BTC", [data])


if __name__ == "__main__":
    unittest.main()
