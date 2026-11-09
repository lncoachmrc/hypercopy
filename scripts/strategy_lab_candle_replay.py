#!/usr/bin/env python3
"""Causal, cost-scenario hourly replay. Research, not an executable trade rule.

Never use a candle's intra-bar high/low as a fill. A trailing-giveback signal
sees a *completed* candle; simulated exit uses the NEXT completed candle
with no master fill in between. There is no inference about the AI's choices.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal as D, InvalidOperation
from pathlib import Path

from strategy_lab_candles import INTERVAL_MS, normalize_candles

REQUIRED_COLUMNS = (
    "event_ts", "asset", "side", "size", "price",
    "start_position", "position_after", "master_equity",
)


def timestamp_ms(raw: str) -> int:
    stamp = datetime.fromisoformat(raw.replace(" ", "T"))
    if stamp.utcoffset() is None:
        raise ValueError("Fill requires timezone-aware timestamp")
    return int(stamp.timestamp() * 1000)


@dataclass(frozen=True)
class Fill:
    ms: int
    asset: str
    before: D
    after: D
    signed_size: D
    price: D
    original_index: int


@dataclass(frozen=True)
class Leg:
    ms: int
    signed_size: D
    price: D


@dataclass(frozen=True)
class Cycle:
    asset: str
    side: str
    start_ms: int
    end_ms: int
    legs: tuple[Leg, ...]


def load_fills(path: Path) -> list[Fill]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != REQUIRED_COLUMNS:
            raise ValueError("Unexpected source-fill CSV columns")
        fills = []
        for index, row in enumerate(reader):
            price = D(row["price"])
            size = D(row["size"])
            before = D(row["start_position"])
            after = D(row["position_after"])
            signed_size = size if row["side"] == "B" else -size
            if (row["side"] not in ("B", "A")
                    or any(not x.is_finite() for x in (price, size, before, after))
                    or price <= 0 or size <= 0 or before + signed_size != after):
                raise ValueError(f"Inconsistent fill at CSV row {index + 2}")
            fills.append(Fill(
                timestamp_ms(row["event_ts"]), row["asset"], before,
                after, signed_size, price, index,
            ))
    return fills


def canonical_fills(fills: list[Fill]) -> list[Fill]:
    """Resolve at most two equal-time fills, preferring position continuity."""
    grouped: dict[str, dict[int, list[Fill]]] = defaultdict(lambda: defaultdict(list))
    for item in fills:
        grouped[item.asset][item.ms].append(item)
    output = []
    for asset, stamps in grouped.items():
        # Exact dynamic programming permits the second equal-time permutation
        # to be chosen based on the next timestamp, not a greedy local choice.
        states = [(0, [], None)]
        for ms in sorted(stamps):
            group = stamps[ms]
            if len(group) > 2:
                raise ValueError(f"{asset}: >2 same-time fills; causal order unavailable")
            permutations = [group] if len(group) == 1 else [group, list(reversed(group))]
            next_states = []
            for order in permutations:
                internal = sum(a.after != b.before for a, b in zip(order, order[1:]))
                candidates = [
                    (cost + internal + int(prev is not None and prev != order[0].before),
                     ordered + order, order[-1].after)
                    for cost, ordered, prev in states
                ]
                next_states.append(min(candidates, key=lambda state: state[0]))
            states = next_states
        gaps, ordered, _ = min(states, key=lambda state: state[0])
        if gaps:
            raise ValueError(f"{asset}: {gaps} missing causal fill transitions")
        output.extend(ordered)
    return sorted(output, key=lambda r: (r.ms, r.asset))


def make_cycles(fills: list[Fill]) -> tuple[list[Cycle], int]:
    by_asset = defaultdict(list)
    for row in canonical_fills(fills):
        by_asset[row.asset].append(row)
    cycles: list[Cycle] = []
    censored = 0
    for asset, rows in by_asset.items():
        current = None
        prior_after = None
        for fill in rows:
            if prior_after is not None and prior_after != fill.before:
                raise ValueError(f"{asset}: broken source position chain")
            prior_after = fill.after
            if current is None:
                if fill.before != 0:
                    current = {"start": None, "side": "LONG" if fill.before > 0 else "SHORT", "legs": []}
                elif fill.after != 0:
                    current = {"start": fill.ms, "side": "LONG" if fill.after > 0 else "SHORT", "legs": []}
                else:
                    continue
            reversal = fill.before != 0 and fill.after != 0 and (fill.before > 0) != (fill.after > 0)
            if reversal:
                # One exchange fill closes the old exposure and opens the new side.
                current["legs"].append(Leg(fill.ms, -fill.before, fill.price))
            else:
                current["legs"].append(Leg(fill.ms, fill.signed_size, fill.price))
            if fill.after == 0 or reversal:
                if current["start"] is None:
                    censored += 1
                else:
                    cycles.append(Cycle(asset, current["side"], current["start"], fill.ms, tuple(current["legs"])))
                current = None
                if reversal:
                    current = {
                        "start": fill.ms,
                        "side": "LONG" if fill.after > 0 else "SHORT",
                        "legs": [Leg(fill.ms, fill.after, fill.price)],
                    }
        if current is not None:
            # The original export must include all closed cycles. Never invent an exit.
            if current["start"] is None:
                censored += 1
            else:
                censored += 1
    return sorted(cycles, key=lambda c: (c.start_ms, c.asset)), censored


def load_archive(path: Path) -> tuple[dict[str, list[dict]], dict]:
    manifest_path = path / "manifest.json"
    candle_path = path / "candles.jsonl"
    manifest = json.loads(manifest_path.read_text())
    actual = hashlib.sha256(candle_path.read_bytes()).hexdigest()
    if actual != manifest.get("data_sha256"):
        raise ValueError("Candle archive hash mismatch")
    if manifest.get("interval") not in INTERVAL_MS:
        raise ValueError("Unsupported archive interval")
    raw = defaultdict(list)
    for line in candle_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        raw[row["asset"]].append(row)
    bars = {}
    for asset, records in raw.items():
        bars[asset] = normalize_candles(
            [{"s": asset, "i": manifest["interval"], **{k: x[k] for k in ("t", "T", "o", "h", "l", "c")}} for x in records],
            coin=asset, interval=manifest["interval"],
            start_ms=manifest["start_ms"], end_ms=manifest["end_ms"],
        )
    return bars, manifest


def pnl_at(legs: tuple[Leg, ...], at_ms: int, mark: D, *, fee_bps: D, slip_bps: D) -> tuple[D, D]:
    """PnL if residual exposure were closed at mark with adverse exit slippage."""
    cash = D(0)
    pos = D(0)
    turnover = D(0)
    for leg in legs:
        if leg.ms > at_ms:
            break
        cash -= leg.signed_size * leg.price
        pos += leg.signed_size
        turnover += abs(leg.signed_size * leg.price)
    execution_px = mark * (D(1) - slip_bps / D(10_000) if pos > 0 else D(1) + slip_bps / D(10_000))
    estimated_net = cash + pos * execution_px - (turnover + abs(pos * execution_px)) * fee_bps / D(10_000)
    return estimated_net, pos


def replay_cycle(
    cycle: Cycle, bars: list[dict], *, threshold_pct: D,
    min_peak_usdc: D, min_current_usdc: D, fee_bps: D, slip_bps: D,
) -> dict:
    step = INTERVAL_MS["1h"]
    if threshold_pct <= 0 or threshold_pct >= 100:
        raise ValueError("Threshold must be strictly between 0 and 100")
    if min_peak_usdc < 0 or min_current_usdc < 0 or fee_bps < 0 or slip_bps < 0 or slip_bps >= 10_000:
        raise ValueError("Invalid cost/scenario values")
    baseline_cash = -sum((leg.signed_size * leg.price for leg in cycle.legs), D(0))
    baseline_fees = sum((abs(leg.signed_size * leg.price) for leg in cycle.legs), D(0)) * fee_bps / D(10_000)
    baseline_net = baseline_cash - baseline_fees
    at = [bar for bar in bars if cycle.start_ms <= bar["T"] < cycle.end_ms]
    peak = None
    prior_samples = 0
    if not at:
        return {"status": "no_complete_candle_while_open", "baseline_net": baseline_net}
    by_t = {b["t"]: b for b in bars}
    for bar in at:
        decision_ms = bar["T"]
        current, size = pnl_at(cycle.legs, decision_ms, D(bar["c"]), fee_bps=fee_bps, slip_bps=slip_bps)
        if not size:
            continue
        peak = current if peak is None else max(peak, current)
        prior_samples += 1
        if prior_samples < 2 or peak <= min_peak_usdc or current <= min_current_usdc:
            continue
        giveback = (peak - current) / peak * 100
        if giveback < threshold_pct:
            continue
        # Wait one FULL subsequent completed candle before assuming execution.
        next_bar = by_t.get(bar["t"] + step)
        if not next_bar or next_bar["T"] >= cycle.end_ms:
            continue
        if any(decision_ms < leg.ms <= next_bar["T"] for leg in cycle.legs):
            continue  # Master changed target in this intervening window.
        eventual, next_pos = pnl_at(
            cycle.legs, next_bar["T"], D(next_bar["c"]),
            fee_bps=fee_bps, slip_bps=slip_bps,
        )
        if next_pos != size:
            raise AssertionError("Position changed despite master-fill fence")
        return {
            "status": "hypothetical_exit",
            "baseline_net": baseline_net, "alternative_net": eventual,
            "delta_net": eventual - baseline_net, "giveback_at_signal_pct": giveback,
            "signal_ms": decision_ms, "simulated_exit_ms": next_bar["T"],
            "bars_seen": prior_samples, "scenario_only": True,
        }
    return {
        "status": "no_signal_before_master_exit",
        "baseline_net": baseline_net, "bars_seen": prior_samples,
    }


def compare(
    fills: list[Fill], candles: dict[str, list[dict]], *,
    fee_bps: D, slip_bps: D, thresholds: tuple[D, ...] = (D(25), D(40), D(60)),
    min_peak_usdc: D = D(1), min_current_usdc: D = D("0.5"),
) -> dict:
    cycles, censored = make_cycles(fills)
    output = {
        "events": len(fills), "complete_cycles": len(cycles),
        "left_or_right_censored": censored,
        "assumed_fee_bps_per_fill": str(fee_bps),
        "assumed_exit_slip_bps": str(slip_bps),
        "holdout_status": "retrospective_scenarios_not_out_of_sample",
        "disclaimer": "No AI replay, no executable bid/ask, no funding, no latency, and no profit guarantee",
        "scenarios": {},
    }
    for threshold in thresholds:
        results = []
        for c in cycles:
            rows = candles.get(c.asset)
            if not rows:
                results.append({"status": "no_archive_for_asset"})
                continue
            results.append(replay_cycle(
                c, rows, threshold_pct=threshold, min_peak_usdc=min_peak_usdc,
                min_current_usdc=min_current_usdc, fee_bps=fee_bps, slip_bps=slip_bps,
            ))
        affected = [r for r in results if r["status"] == "hypothetical_exit"]
        evaluated = [r for r in results if r["status"] in {"hypothetical_exit", "no_signal_before_master_exit"}]
        output["scenarios"][str(threshold)] = {
            "cycles_with_candle_evidence": len(evaluated),
            "hypothetical_exits": len(affected),
            "positive_deltas": sum(r["delta_net"] > 0 for r in affected),
            "negative_deltas": sum(r["delta_net"] < 0 for r in affected),
            "delta_sum_usdc_estimated": str(sum((r["delta_net"] for r in affected), D(0))),
            "baseline_net_estimated_on_evaluated": str(sum((r["baseline_net"] for r in evaluated), D(0))),
            "scenario_net_estimated_on_evaluated": str(sum(
                (r.get("alternative_net", r["baseline_net"]) for r in evaluated), D(0)
            )),
            "missing_asset_cycles": sum(r["status"] == "no_archive_for_asset" for r in results),
            "insufficient_hourly_coverage_cycles": sum(r["status"] == "no_complete_candle_while_open" for r in results),
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fills", required=True, type=Path)
    parser.add_argument("--candles", required=True, type=Path, help="Directory with public candle archive")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fee-bps", type=D, default=D(5))
    parser.add_argument("--exit-slip-bps", type=D, default=D(10))
    args = parser.parse_args()
    bars, manifest = load_archive(args.candles)
    if manifest["interval"] != "1h":
        raise ValueError("Use fully closed 1h candles for this replay method")
    result = compare(load_fills(args.fills), bars, fee_bps=args.fee_bps, slip_bps=args.exit_slip_bps)
    result["candle_archive_sha256"] = manifest["data_sha256"]
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
