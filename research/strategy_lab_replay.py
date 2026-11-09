#!/usr/bin/env python3
"""TRAXION offline Profit Exit replay, never a trading signal or order router.

Uses public Hyperliquid 1h candles and user-supplied master fill CSV.
Retrospective exploratory *proxy* for Profit Retention v1, NOT LLM replay.
Stdlib only. No remote calls or secrets and never writes to the repository.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import statistics
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

D = Decimal
HOUR_MS = 3_600_000
COLUMNS = ("event_ts", "asset", "side", "size", "price", "start_position", "position_after", "master_equity")


def millis(s: str) -> int:
    return int(datetime.fromisoformat(s.replace(" ", "T").replace("Z", "+00:00")).timestamp() * 1000)


@dataclass(frozen=True)
class Fill:
    asset: str
    ts: int
    before: Decimal
    after: Decimal
    price: Decimal
    row: int
    tied: bool = False

    @property
    def delta(self) -> Decimal:
        return self.after - self.before


@dataclass
class Cycle:
    asset: str
    side: str
    start: int | None
    end: int | None
    trades: list[Fill]
    tied: bool = False

    @property
    def complete(self) -> bool:
        return self.start is not None and self.end is not None

    @property
    def gross(self) -> Decimal:
        return -sum((x.delta * x.price for x in self.trades), D(0))


def read_fills(path: Path) -> list[Fill]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        if tuple(rd.fieldnames or ()) != COLUMNS:
            raise ValueError("Unsupported master fill CSV schema")
        out = []
        for n, row in enumerate(rd):
            size, price, before, after = [D(row[x]) for x in ("size", "price", "start_position", "position_after")]
            if not all(x.is_finite() for x in (size, price, before, after)) or size <= 0 or price <= 0:
                raise ValueError(f"Invalid numeric fill on row {n+2}")
            if row["side"] not in {"A", "B"} or before + (size if row["side"] == "B" else -size) != after:
                raise ValueError(f"Invalid position arithmetic on row {n+2}")
            out.append(Fill(row["asset"], millis(row["event_ts"]), before, after, price, n))
    return out


def canonical_per_asset(fills: list[Fill]) -> list[Fill]:
    """Infer equal-time ordering by state continuity; never assert true causality."""
    by_ts: dict[int, list[Fill]] = defaultdict(list)
    for f in fills:
        by_ts[f.ts].append(f)
    # For this historical export each tied group has at most two fills.
    # Dynamic programming optimizes chain continuity globally within one asset.
    states: list[tuple[int, list[Fill], Fill | None]] = [(0, [], None)]
    for ts in sorted(by_ts):
        batch = by_ts[ts]
        if len(batch) > 2:
            raise ValueError(">2 same-timestamp fills require causal_order input")
        perm = [batch] if len(batch) == 1 else [batch, batch[::-1]]
        candidates = []
        for ordered in perm:
            internal = sum(a.after != b.before for a, b in zip(ordered, ordered[1:]))
            best = min(
                ((cost + internal + int(last is not None and last.after != ordered[0].before),
                 selected + [Fill(x.asset,x.ts,x.before,x.after,x.price,x.row,len(batch)>1) for x in ordered],
                 ordered[-1])
                for cost, selected, last in states), key=lambda x: x[0]
            )
            candidates.append(best)
        states = candidates
    cost, out, _ = min(states,key=lambda x:x[0])
    if cost:
        raise ValueError(f"Cannot reconstruct consistent master position chain: {cost} breaks")
    return out


def reconstruct_cycles(fills: list[Fill]) -> list[Cycle]:
    groups: dict[str, list[Fill]] = defaultdict(list)
    for f in fills: groups[f.asset].append(f)
    cycles: list[Cycle] = []
    for asset, events in groups.items():
        ordered = canonical_per_asset(events)
        current: Cycle | None = None
        for fill in ordered:
            before, after = fill.before, fill.after
            if current is None:
                if before != 0:
                    current = Cycle(asset, "LONG" if before > 0 else "SHORT", None, None, [], False)
                elif after != 0:
                    current = Cycle(asset, "LONG" if after > 0 else "SHORT", fill.ts, None, [], False)
            if current is None: continue
            reverse = before != 0 and after != 0 and ((before > 0) != (after > 0))
            if reverse:
                current.trades.append(Fill(asset,fill.ts,before,D(0),fill.price,fill.row,fill.tied))
                current.tied |= fill.tied
                current.end = fill.ts
                cycles.append(current)
                current = Cycle(asset, "LONG" if after > 0 else "SHORT", fill.ts, None,
                                [Fill(asset,fill.ts,D(0),after,fill.price,fill.row,fill.tied)],fill.tied)
            else:
                current.trades.append(fill)
                current.tied |= fill.tied
                if after == 0:
                    current.end = fill.ts
                    cycles.append(current)
                    current = None
        if current is not None:
            cycles.append(current)
    return cycles


def read_candles(path: Path) -> dict[str, list[dict]]:
    with gzip.open(path,"rt",encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8") as fh:
        data=json.load(fh)
    if data.get("interval") != "1h" or data.get("source") != "Hyperliquid public candleSnapshot":
        raise ValueError("Unsupported or unverified candle source")
    out: dict[str,list[dict]]={}
    for coin, candles in data["markets"].items():
        bars = []
        for x in candles:
            t = x["t"]
            if not isinstance(t,int) or t%HOUR_MS or any(D(x[k]) <= 0 for k in ("o","h","l","c")):
                raise ValueError(f"Malformed {coin} candle")
            prices = {k: D(x[k]) for k in ("o","h","l","c")}
            if prices["h"] < max(prices["o"],prices["c"],prices["l"]) or prices["l"] > min(prices["o"],prices["c"],prices["h"]):
                raise ValueError(f"Inconsistent {coin} OHLC")
            bars.append({"t":t,**prices})
        bars.sort(key=lambda x:x["t"])
        if len({x["t"] for x in bars}) != len(bars): raise ValueError(f"Duplicate {coin} candles")
        out[coin] = bars
    return out


def position_and_cashflow_before(cycle: Cycle, ts: int, fee_bps: Decimal, fill_slip_bps: Decimal):
    """Replay the authoritative *residual* entry price and cost pool.

    Already-realized partial reductions contribute to cycle cashflow but NOT
    to eligibility for a new profit-only close. This mirrors Profit Exit's
    actual residual economics, where reducing exposure shrinks old fee pools.
    """
    pos, cash, costs, count = D(0), D(0), D(0), 0
    entry: Decimal | None = None
    residual_costs = D(0)
    for f in cycle.trades:
        if f.ts > ts: break
        previous = pos
        d = f.delta
        pos = f.after
        cash -= d*f.price
        trade_cost = abs(d*f.price)*(fee_bps+fill_slip_bps)/D(10000)
        costs += trade_cost
        count += 1
        if previous == 0:
            entry = f.price
            residual_costs = trade_cost
        elif pos == 0:
            entry = None
            residual_costs = D(0)
        elif previous*pos > 0:
            if abs(pos) > abs(previous):
                assert entry is not None
                entry = (abs(previous)*entry + abs(d)*f.price)/abs(pos)
                residual_costs += trade_cost
            elif abs(pos) < abs(previous):
                residual_costs *= abs(pos)/abs(previous)
            else:
                raise ValueError('No-change trade in master history')
        else:
            raise ValueError('A reversal must be split into separate cycles')
    return pos,cash,costs,count,entry,residual_costs


def baseline_net(cycle: Cycle, fee_bps: Decimal, fill_slip_bps: Decimal) -> Decimal:
    fees = sum((abs(f.delta*f.price)*(fee_bps+fill_slip_bps)/D(10000) for f in cycle.trades),D(0))
    return cycle.gross - fees


def candidate_observations(cycle: Cycle, bars: list[dict],fee_bps: Decimal,fill_slip_bps: Decimal, forced_slip_bps: Decimal) -> list[dict]:
    """Every signal uses a closed 1h bar; execution only at *next* bar open."""
    if not cycle.complete or not bars: return []
    out, peak, last_state = [], None, None
    # Legacy CSV lacks causal_order; reset comparable peak at each master fill.
    for i in range(len(bars)-1):
        b,next_bar=bars[i],bars[i+1]
        # Bar close time is t+1h-1ms. We can only act from t+1h onwards.
        signal_ts=b["t"]+HOUR_MS-1
        if not (cycle.start < signal_ts < cycle.end):continue
        if next_bar["t"]!=b["t"]+HOUR_MS or next_bar["t"]>=cycle.end:continue
        pos,cash,costs,state,entry,residual_costs=position_and_cashflow_before(cycle,signal_ts,fee_bps,fill_slip_bps)
        if pos == 0 or state == 0:continue
        if (cycle.side == "LONG") != (pos > 0): raise ValueError("Cycle side changed without reversal")
        close_cost=abs(pos*b["c"])*(fee_bps+forced_slip_bps)/D(10000)
        if entry is None:raise ValueError("Nonzero residual position without entry price")
        net_est=(b["c"]-entry)*pos-residual_costs-close_cost
        if state!=last_state:
            peak = None
            last_state=state
        if net_est>0 and (peak is None or net_est>peak):peak=net_est
        giveback=(peak-net_est)/peak if peak is not None and net_est>0 else None
        next_px=next_bar["o"]*(D(1)-forced_slip_bps/D(10000) if pos>0 else D(1)+forced_slip_bps/D(10000))
        forced_exit_fee=abs(pos*next_bar["o"])*fee_bps/D(10000)
        alt_pnl = cash+pos*next_px-costs-forced_exit_fee
        notional=abs(pos*b["c"])
        out.append({"signal_ts":signal_ts,"state":state,"net_est":net_est,
                    "peak":peak,"giveback":giveback,"peak_return":(peak/notional if peak is not None else None),
                    "alt_pnl":alt_pnl,"position":pos})
    return out


def choose_exit(observations:list[dict],giveback_threshold:Decimal,peak_return_min:Decimal)->dict|None:
    for obs in observations:
        if (obs["net_est"]>0 and obs["peak"] is not None
            and obs["peak_return"] is not None and obs["peak_return"]>=peak_return_min
            and obs["giveback"] is not None and obs["giveback"]>=giveback_threshold):
            return obs
    return None


def summarize(cycles:list[Cycle], candles:dict[str,list[dict]],fee_bps:Decimal,fill_slip_bps:Decimal,forced_slip_bps:Decimal,threshold:Decimal,minimum:Decimal,cutoff_ms:int)->dict:
    totals = {x:{"n":0,"baseline":D(0),"strategy":D(0),"exits":0,"wins_base":0,"wins_alt":0} for x in ("train","test","cross")}
    covered,missing,short,no_exit_bars,invalid_price,tie_cycles=0,0,0,0,0,0
    comparisons=[]
    for c in cycles:
        if not c.complete:continue
        bars=candles.get(c.asset,[])
        if not bars:
            missing+=1;continue
        # Exclude inconsistent-price assets: wrong ticker mapping or unreliable bars.
        by_hour={b["t"]:b for b in bars}
        outlier=False
        for f in c.trades:
            b=by_hour.get((f.ts//HOUR_MS)*HOUR_MS)
            if b and (f.price < b["l"]*D("0.98") or f.price > b["h"]*D("1.02")):
                outlier=True;break
        if outlier:
            invalid_price+=1;continue
        obs=candidate_observations(c,bars,fee_bps,fill_slip_bps,forced_slip_bps)
        if not obs:
            no_exit_bars+=1;continue
        covered+=1
        if c.tied: tie_cycles+=1
        cohort = "train" if c.end<cutoff_ms else ("test" if c.start>=cutoff_ms else "cross")
        base=baseline_net(c,fee_bps,fill_slip_bps)
        exited=choose_exit(obs,threshold,minimum)
        alt=exited["alt_pnl"] if exited else base
        s=totals[cohort]
        s["n"]+=1;s["baseline"]+=base;s["strategy"]+=alt
        s["exits"]+=int(exited is not None)
        s["wins_base"]+=int(base>0);s["wins_alt"]+=int(alt>0)
        comparisons.append({"asset":c.asset,"start":c.start,"end":c.end,"baseline":base,"alternative":alt,"exited":exited is not None,"has_ties":c.tied,"cohort":cohort})
    summarized={}
    for key,val in totals.items():
        summarized[key]={**{k:(str(v) if isinstance(v,D) else v) for k,v in val.items()},"delta":str(val["strategy"]-val["baseline"])}
    return {"cohorts":summarized,"coverage":{"eligible_cycles":covered,"missing_market":missing,"invalid_fill_vs_candle":invalid_price,"no_actionable_hourly_pair":no_exit_bars,"ambiguous_tied_cycles":tie_cycles},"comparisons":comparisons}


def run(fills:Path, candles:Path, cutoff:str="2026-09-22T00:00:00+00:00")->dict:
    rows=read_fills(fills); cycles=reconstruct_cycles(rows)
    market=read_candles(candles)
    complete=[c for c in cycles if c.complete]
    if len(rows)==1766 and len(complete)!=211:
        raise ValueError(f"Cycle reconstruction diverges from baseline: {len(complete)} != 211")
    gross=sum((c.gross for c in complete),D(0))
    if len(rows)==1766 and abs(gross-D("-53.609563"))>D("0.00001"):
        raise ValueError(f"Gross baseline mismatch {gross}")
    fee,fill_slip,forced_slip=D("4.5"),D("3"),D("5")
    cutoff_ms=millis(cutoff)
    candidates=[(D(x),D(y)) for x in ("0.20","0.35","0.50","0.70") for y in ("0.0025","0.005","0.01")]
    result=[]
    for giveback,peak_min in candidates:
        ev=summarize(complete,market,fee,fill_slip,forced_slip,giveback,peak_min,cutoff_ms)
        result.append({"giveback":str(giveback),"min_peak_return":str(peak_min),**{k:v for k,v in ev.items() if k!="comparisons"}})
    # Select on first chronological cohort ONLY. Missing candles are not imputed.
    good=[x for x in result if x["cohorts"]["train"]["n"]>=15]
    selected=max(good,key=lambda x:(D(x["cohorts"]["train"]["delta"]),-D(x["giveback"]))) if good else None
    detail=None;robust=[]
    if selected:
        g,pm=D(selected["giveback"]),D(selected["min_peak_return"])
        detail=summarize(complete,market,fee,fill_slip,forced_slip,g,pm,cutoff_ms)
        # A variant that only works under one optimistic cost assumption is not robust.
        for slip in (D("2"),D("5"),D("10"),D("20")):
            ev=summarize(complete,market,fee,fill_slip,slip,g,pm,cutoff_ms)
            robust.append({"forced_slippage_bps":str(slip),"test_delta":ev["cohorts"]["test"]["delta"],"test_exits":ev["cohorts"]["test"]["exits"]})
    def diagnostic(c):return {"asset":c.asset,"side":c.side,"start":c.start,"end":c.end,"gross":str(c.gross),"tied":c.tied}
    return {"type":"retrospective_hypothetical_proxy_replay","source_events":len(rows),"source_assets":len(set(f.asset for f in rows)),
            "complete_cycles":len(complete),"left_censored_cycles":sum(c.start is None for c in cycles),
            "all_complete_baseline_gross":str(gross),"train_cutoff":cutoff,
            "candle_interval":"1h","eligibility":"same-cycle same-fill-version sampled net-positive, next-bar-open execution",
            "commission_bps_per_trade":str(fee),"copied_fill_slippage_bps":str(fill_slip),"forced_exit_slippage_bps":str(forced_slip),
            "parameter_grid":result,"train_selected":{"giveback":selected["giveback"],"min_peak_return":selected["min_peak_return"]} if selected else None,
            "selected_evaluation":{k:v for k,v in (detail or {}).items() if k!="comparisons"},"cost_sensitivity":robust,
            "warnings":["OHLC hourly closes do not reveal intrabar price order or true maximum favorable excursion",
                        "The 2026-09-22 split was discussed before this replay; not a pristine untouched holdout",
                        "Funding, dynamic exchange fees, live spread, partial fills and agent decisions are not observed",
                        "Policy thresholds are retrospective proxies, not the LLM's decisions or live trading recommendations",
                        "Equal-time fill ordering is inferred from position continuity; causal order unavailable"]}


def main()->None:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--fills',type=Path,required=True)
    ap.add_argument('--candles',type=Path,required=True)
    ap.add_argument('--out',type=Path)
    args=ap.parse_args()
    result=run(args.fills,args.candles)
    payload=json.dumps(result,indent=2,ensure_ascii=False)
    if args.out:args.out.write_text(payload,encoding="utf-8")
    else:print(payload)


if __name__=='__main__':main()