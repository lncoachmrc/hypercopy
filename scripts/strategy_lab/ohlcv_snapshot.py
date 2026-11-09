"""Read-only Hyperliquid OHLCV capture for a historical Strategy Lab pilot.

No wallet, no execution, no credentials, no client order. CI artifact only.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.hyperliquid.xyz/info"
ASSETS = ("BTC", "ETH", "SOL", "LTC", "LINK", "GMX", "FOGO", "HBAR", "FET", "ADA", "AVAX", "XLM")
HOUR_MS = 3_600_000
START = int(datetime(2026, 8, 28, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 10, 4, tzinfo=UTC).timestamp() * 1000) - 1
FIELDS = ("t", "T", "s", "i", "o", "h", "l", "c", "v", "n")


def validate(asset: str, data: object) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError("Unexpected response, expected list")
    candles = []
    for row in data:
        if not isinstance(row, dict) or not set(FIELDS) <= row.keys():
            raise ValueError("Incomplete candle response")
        if row["s"] != asset or row["i"] != "1h":
            raise ValueError("Wrong market or interval")
        if type(row["t"]) is not int or type(row["T"]) is not int:
            raise ValueError("Invalid timestamps")
        t = row["t"]
        if t % HOUR_MS != 0 or row["T"] != t + HOUR_MS - 1 or t < START or t > END:
            raise ValueError("Candle outside exact requested hour interval")
        if candles and t <= candles[-1]["t"]:
            raise ValueError("Unsorted or repeated candle")
        prices = {key: Decimal(str(row[key])) for key in ("o", "h", "l", "c", "v")}
        if not all(p.is_finite() for p in prices.values()):
            raise ValueError("Nonfinite OHLCV")
        if min(prices[k] for k in ("o", "h", "l", "c")) <= 0 or prices["v"] < 0:
            raise ValueError("Invalid OHLCV values")
        if prices["h"] < max(prices[k] for k in ("o", "l", "c")) or prices["l"] > min(prices[k] for k in ("o", "h", "c")):
            raise ValueError("Invalid OHLC ordering")
        if type(row["n"]) is not int or row["n"] < 0:
            raise ValueError("Invalid trade count")
        candles.append(row)
    return candles


def fetch(asset: str) -> list[dict]:
    payload = json.dumps({"type": "candleSnapshot", "req": {
        "coin": asset, "interval": "1h", "startTime": START, "endTime": END,
    }}).encode()
    req = Request(API, payload, headers={"Content-Type": "application/json", "User-Agent": "TRAXION-Strategy-Lab-ReadOnly"}, method="POST")
    for attempt in range(3):
        try:
            with urlopen(req, timeout=18) as response:
                if response.status != 200:
                    raise RuntimeError("Unexpected HTTP status")
                raw = response.read(3_000_001)
                if len(raw) > 3_000_000:
                    raise ValueError("Candle body too large")
                return validate(asset, json.loads(raw))
        except (HTTPError, URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Market data request failed: {type(exc).__name__}") from exc
            time.sleep(1 + attempt)
    raise RuntimeError("Unreachable")


def capture(target: Path) -> dict:
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": API, "method": "POST /info candleSnapshot", "interval": "1h",
        "start_ms": START, "end_ms": END, "assets": {},
        "caution": "Trade-price candles not executable bid/ask. No future candles may enter a signal.",
    }
    for asset in ASSETS:
        try:
            candles = fetch(asset)
            if not candles:
                manifest["assets"][asset] = {"status": "empty"}
                continue
            file = target / f"{asset}_1h.csv"
            with file.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows({key: row[key] for key in FIELDS} for row in candles)
            gaps = sum((b["t"] - a["t"]) // HOUR_MS - 1 for a, b in zip(candles, candles[1:]))
            first, last = candles[0]["t"], candles[-1]["t"]
            manifest["assets"][asset] = {
                "status": "full" if first == START and last == END - (HOUR_MS - 1) else "partial",
                "count": len(candles), "gaps": gaps, "first_ms": first, "last_ms": last,
                "file": file.name, "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            }
        except (ValueError, RuntimeError) as exc:
            manifest["assets"][asset] = {"status": "error", "reason": str(exc)[:150]}
        time.sleep(0.4)
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    result = capture(args.out)
    for asset, rec in result["assets"].items():
        print(f"{asset}: {rec['status']} count={rec.get('count', 0)} gaps={rec.get('gaps', 0)}")
    if not any(rec["status"] in ("full", "partial") for rec in result["assets"].values()):
        raise SystemExit("No validated OHLCV response; no dataset is usable")
