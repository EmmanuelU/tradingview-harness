#!/usr/bin/env python3
"""
Offline backtest — replay price_log.jsonl through a rule system. Zero browser deps.

Usage:
  python3 backtest.py paper_momentum
  python3 backtest.py paper_momentum --symbol NASDAQ:AAPL
  python3 backtest.py paper_momentum --from 2026-04-30T06:00:00
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "mcp_server"))

from rules_engine import load_system, evaluate_system, build_pane_registry

PRICE_LOG = ROOT / "price_log.jsonl"


def _parse_args():
    args = sys.argv[1:]
    system_name = args[0] if args else "paper_momentum"
    filt_symbol = None
    filt_from   = None
    i = 1
    while i < len(args):
        if args[i] == "--symbol" and i + 1 < len(args):
            filt_symbol = args[i + 1]; i += 2
        elif args[i] == "--from" and i + 1 < len(args):
            filt_from = args[i + 1]; i += 2
        else:
            i += 1
    return system_name, filt_symbol, filt_from


def _load_ticks(filt_symbol, filt_from) -> list[dict]:
    try:
        lines = [l for l in PRICE_LOG.read_text().strip().split("\n") if l.strip()]
    except FileNotFoundError:
        print("price_log.jsonl not found — run start_watch first to collect live price data")
        sys.exit(1)

    ticks = [json.loads(l) for l in lines]
    if filt_symbol:
        ticks = [t for t in ticks if t.get("symbol") == filt_symbol]
    if filt_from:
        ticks = [t for t in ticks if t.get("ts", "") >= filt_from]
    ticks.sort(key=lambda t: t["ts"])
    return ticks


def _batch_ticks(ticks: list[dict], gap_seconds: int = 10) -> list[list[dict]]:
    """Group consecutive ticks within gap_seconds into one eval cycle."""
    if not ticks:
        return []
    batches, current = [], [ticks[0]]
    for tick in ticks[1:]:
        prev_ts = datetime.fromisoformat(current[-1]["ts"])
        curr_ts = datetime.fromisoformat(tick["ts"])
        if (curr_ts - prev_ts).total_seconds() <= gap_seconds:
            current.append(tick)
        else:
            batches.append(current)
            current = [tick]
    batches.append(current)
    return batches


def run(system_name: str, filt_symbol=None, filt_from=None):
    system = load_system(system_name)
    panes  = build_pane_registry([system])
    pane_by_sym = {p["symbol"]: i for i, p in enumerate(panes)}

    ticks   = _load_ticks(filt_symbol, filt_from)
    batches = _batch_ticks(ticks)

    print(f"=== Backtest: {system_name} ===")
    print(f"Ticks: {len(ticks)} | Cycles: {len(batches)}")
    if filt_symbol: print(f"Symbol filter: {filt_symbol}")
    if filt_from:   print(f"From: {filt_from}")
    print()

    positions: dict[str, float]        = {}
    entries:   dict[str, list[dict]]   = {}
    trades:    list[dict]              = []

    for batch in batches:
        prices_by_pane: dict[int, dict] = {}
        for tick in batch:
            idx = pane_by_sym.get(tick.get("symbol"))
            if idx is not None:
                prices_by_pane[idx] = tick

        if not prices_by_pane:
            continue

        results = evaluate_system(system, prices_by_pane)
        cycle_ts = batch[-1]["ts"]

        for r in results:
            if not r.get("triggered"):
                continue
            for action in r.get("actions", []):
                parts    = action.split(":")
                verb     = parts[0].lower()
                qty      = float(parts[1]) if len(parts) > 1 else 1.0
                sym      = r["symbol"].split(":")[-1]
                price_v  = r.get("value")

                if verb == "buy" and sym not in positions:
                    positions[sym] = qty
                    entries.setdefault(sym, []).append({
                        "qty": qty, "price": price_v, "ts": cycle_ts, "rule": r["rule_id"]
                    })
                    print(f"  {cycle_ts[:19]}  BUY  {sym} x{qty} @ {price_v}  [{r['rule_id']}]")

                elif verb == "sell" and sym in positions:
                    held = positions.pop(sym)
                    sym_entries = entries.get(sym, [])
                    rem = qty
                    while rem > 0 and sym_entries:
                        e    = sym_entries[0]
                        used = min(e["qty"], rem)
                        if e.get("price") and price_v:
                            pnl     = round((price_v - e["price"]) * used, 6)
                            pnl_pct = round((price_v - e["price"]) / e["price"] * 100, 4)
                            trades.append({
                                "sym": sym, "entry": e["price"], "exit": price_v,
                                "qty": used, "pnl": pnl, "pnl_pct": pnl_pct,
                                "entry_rule": e["rule"], "exit_rule": r["rule_id"],
                            })
                            print(
                                f"  {cycle_ts[:19]}  SELL {sym} x{used} @ {price_v}"
                                f"  PnL={pnl:+.4f} ({pnl_pct:+.2f}%)"
                                f"  [{r['rule_id']}]"
                            )
                        e["qty"] -= used
                        rem -= used
                        if e["qty"] <= 0:
                            sym_entries.pop(0)
                    if sym_entries:
                        entries[sym] = sym_entries
                    else:
                        entries.pop(sym, None)

    print()
    print(f"=== Results ===")
    print(f"Trades closed: {len(trades)}")
    if trades:
        total_pnl = sum(t["pnl"] for t in trades)
        wins      = sum(1 for t in trades if t["pnl"] > 0)
        print(f"Win rate:      {wins}/{len(trades)} = {wins/len(trades)*100:.1f}%")
        print(f"Total PnL:     {total_pnl:+.6f}")
        print(f"Avg PnL/trade: {total_pnl/len(trades):+.6f}")
    if positions:
        print(f"Open positions: {positions}")


if __name__ == "__main__":
    sysname, fsym, ffrom = _parse_args()
    run(sysname, fsym, ffrom)
