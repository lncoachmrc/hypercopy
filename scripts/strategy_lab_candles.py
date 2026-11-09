#!/usr/bin/env python3
"""Fetch PUBLIC historic Hyperliquid candles only; zero wallet or order access.

The official info/candleSnapshot API retains at most 5,000 bars per interval.
A partial archive is explicitly reported, never silently filled or interpolated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://api.hyperliquid.xyz/info"
INTERVAL_MS = {"1h": 3_600_000, "15m": 900_000}
MAX_CANDLES = 5000
MAX_RESPONSE_BYTES = 6_000_000
RETRY_CODES = {429, 500, 502, 503, 504}


def epoch_ms(text: str) -> int:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Date must include a UTC offset")
    return int(parsed.timestamp() * 1000)


def _positive(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid candle price") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("Non-positive or non-finite candle price")
    return number


def normalize_candles(
    response: object, *, coin: str, interval: str, start_ms: int, end_ms: int,
) -> list[dict]:
    """Reject malformed/time-inconsistent data before making it available to replay."""
    if interval not in INTERVAL_MS:
        raise ValueError("Unsupported interval")
    if not isinstance(response, list) or len(response) > MAX_CANDLES:
        raise ValueError("Malformed or over-limit candle response")
    step = INTERVAL_MS[interval]
    seen = set()
    result = []
    for raw in response:
        if not isinstance(raw, dict):
            raise ValueError("Candle is not an object")
        t, T = raw.get("t"), raw.get("T")
        if isinstance(t, bool) or isinstance(T, bool) or not isinstance(t, int) or not isinstance(T, int):
            raise ValueError("Invalid candle timestamps")
        if t in seen or t % step != 0 or T != t + step - 1:
            raise ValueError("Duplicate, unaligned or incorrectly closed candle")
        if t < start_ms or T >= end_ms:
            raise ValueError("Candle lies outside requested COMPLETED window")
        if raw.get("s") != coin or raw.get("i") != interval:
            raise ValueError("Unexpected candle symbol/interval")
        prices = {k: _positive(raw.get(k)) for k in ("o", "h", "l", "c")}
        if not (prices["l"] <= min(prices["o"], prices["c"])
                and max(prices["o"], prices["c"]) <= prices["h"]):
            raise ValueError("Inconsistent OHLC")
        seen.add(t)
        result.append({
            "asset": coin, "interval": interval, "t": t, "T": T,
            **{k: str(prices[k]) for k in ("o", "h", "l", "c")},
        })
    result.sort(key=lambda bar: bar["t"])
    return result


def request_public_candles(
    coin: str, *, interval: str, start_ms: int, end_ms: int,
    timeout: int = 25, retries: int = 4,
) -> list[dict]:
    """Public, read-only HTTP POST to /info. Never POST /exchange."""
    payload = json.dumps({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms - 1},
    }).encode("utf-8")
    for attempt in range(retries):
        try:
            req = Request(
                API_URL, data=payload, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "TRAXION-StrategyLab/1.0"},
            )
            with urlopen(req, timeout=timeout) as reply:
                if reply.status != 200:
                    raise RuntimeError(f"Unexpected HTTP status: {reply.status}")
                body = reply.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("Excessive candle response")
                return normalize_candles(
                    json.loads(body), coin=coin, interval=interval,
                    start_ms=start_ms, end_ms=end_ms,
                )
        except HTTPError as exc:
            if exc.code not in RETRY_CODES or attempt + 1 == retries:
                raise RuntimeError(f"Public candle endpoint HTTP {exc.code}") from exc
        except (TimeoutError, URLError) as exc:
            if attempt + 1 == retries:
                raise RuntimeError(f"Public candle endpoint unavailable: {type(exc).__name__}") from exc
        time.sleep(min(15, 3 * (2 ** attempt)))
    raise AssertionError("retry loop exhausted")


def collect(
    *,
    assets: list[str], interval: str, start_ms: int, end_ms: int,
    output_dir: Path, between_requests: float = 2.5,
    request_fn=request_public_candles,
) -> dict:
    if start_ms >= end_ms or interval not in INTERVAL_MS:
        raise ValueError("Invalid interval/window")
    if end_ms > int(time.time() * 1000) - INTERVAL_MS[interval]:
        raise ValueError("The entire window must be historical and closed")
    if not assets or len(assets) > 100 or between_requests < 0:
        raise ValueError("Invalid asset count/throttle")
    if len(set(assets)) != len(assets) or any(not re.fullmatch(r"[A-Z0-9]{1,20}", x) for x in assets):
        raise ValueError("Duplicate or unsafe asset")
    output_dir.mkdir(parents=True, exist_ok=True)
    candle_path = output_dir / "candles.jsonl"
    status = []
    digest = hashlib.sha256()
    total = 0
    with candle_path.open("wb") as output:
        for i, coin in enumerate(assets):
            if i:
                time.sleep(between_requests)
            try:
                rows = request_fn(coin, interval=interval, start_ms=start_ms, end_ms=end_ms)
                # Normalize even if a test/request adapter returns unsorted rows.
                rows = normalize_candles([
                    {"s": coin, "i": interval, **{k: r[k] for k in ("t", "T", "o", "h", "l", "c")}}
                    for r in rows
                ], coin=coin, interval=interval, start_ms=start_ms, end_ms=end_ms)
            except (ValueError, RuntimeError) as exc:
                status.append({"asset": coin, "status": "error", "reason": type(exc).__name__})
                continue
            for row in rows:
                data = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                output.write(data)
                digest.update(data)
            total += len(rows)
            status.append({
                "asset": coin, "status": "ok" if rows else "no_candles",
                "count": len(rows), "first_t": rows[0]["t"] if rows else None,
                "last_T": rows[-1]["T"] if rows else None,
                "earliest_requested_hour_present": bool(rows and rows[0]["t"] <= start_ms),
            })
            print(f"market {i+1}/{len(assets)} {coin}: {len(rows)} public {interval} bars",flush=True)
    report = {
        "source": API_URL, "api_type": "candleSnapshot",
        "interval": interval, "start_ms": start_ms, "end_ms": end_ms,
        "data_sha256": digest.hexdigest(), "bars": total,
        "asset_count": len(assets), "assets": status,
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "limitations": [
            "Public OHLC bars are not executable bid/ask prices",
            "At most the latest 5000 bars per asset/interval are available",
            "Missing assets/history have not been imputed",
            "No user addresses, orders, private keys or trading actions were accessed",
        ],
    }
    (output_dir / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"PUBLIC_ARCHIVE: {total} bars from {len(assets)} requested assets; SHA256={digest.hexdigest()}",flush=True)
    if total == 0:
        raise RuntimeError("No verified candles collected; archive is unusable")
    return report


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", required=True, help="Comma-separated perpetual asset symbols, NO addresses")
    p.add_argument("--interval", default="1h", choices=tuple(INTERVAL_MS))
    p.add_argument("--start", required=True, help="ISO8601 UTC time")
    p.add_argument("--end", required=True, help="ISO8601 UTC time")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--sleep", type=float, default=2.5)
    args = p.parse_args()
    try:
        collect(
            assets=[s.strip() for s in args.assets.split(",")],
            interval=args.interval, start_ms=epoch_ms(args.start),
            end_ms=epoch_ms(args.end), output_dir=args.output_dir,
            between_requests=args.sleep,
        )
    except Exception as exc:
        print(f"Strategy Lab data collection failed: {type(exc).__name__}: {exc}",file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
