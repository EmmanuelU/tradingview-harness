"""TradingView MCP — unlimited panes, URL-based nav, DOM price, rules engine."""

import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from fastmcp import FastMCP
from browser import ensure_tv, get_page, page_count
import paper_tv
import tv_utils
from rules_engine import (
    load_system, load_all_systems, save_system,
    build_pane_registry, evaluate_system,
    get_rule_history, full_picture, list_system_files,
)

# In-process registry — rebuilt each load_systems call
_active_systems: list[dict] = []
_active_panes:   list[dict] = []

# Background watch task
_watch_task: Optional[asyncio.Task] = None
_watch_interval: int = 0

# In-memory position tracker: short_symbol → qty held
# Updated on every successful order. Reconciled from sim_log on start_watch.
_sim_positions: dict[str, float] = {}

mcp = FastMCP("tradingview")

ROOT       = Path(__file__).parent.parent
RULES_FILE = ROOT / "rules.json"
SIM_LOG    = ROOT / "sim_log.jsonl"
PRICE_LOG  = ROOT / "price_log.jsonl"
PNL_LOG    = ROOT / "pnl_log.jsonl"
EVENT_LOG  = ROOT / "event_log.jsonl"
RULE_SCORES = ROOT / "rule_scores.json"

# FIFO open-entry tracker: sym_short → [{ts, qty, fill_price, rule_id}]
_sim_entries: dict[str, list[dict]] = {}


def _append_jsonl(path: Path, obj: dict):
    try:
        with path.open("a") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception:
        pass


def _log_trade(entry: dict):
    _append_jsonl(SIM_LOG, entry)


def _log_price(pane: int, symbol: str, tf: str, price: dict):
    _append_jsonl(PRICE_LOG, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pane": pane, "symbol": symbol, "tf": tf,
        **{k: price.get(k) for k in ("price", "open", "high", "low", "close", "direction", "change")},
    })


def _log_event(event: str, detail: str = ""):
    _append_jsonl(EVENT_LOG, {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event, "detail": str(detail),
    })


def _update_rule_scores(entry_rule: str, pnl: float):
    try:
        scores: dict = json.loads(RULE_SCORES.read_text()) if RULE_SCORES.exists() else {}
        s = scores.setdefault(entry_rule, {
            "trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "win_rate": 0.0,
        })
        s["trades"]    += 1
        s["total_pnl"]  = round(s["total_pnl"] + pnl, 6)
        s["wins"]      += 1 if pnl > 0 else 0
        s["losses"]    += 1 if pnl <= 0 else 0
        s["avg_pnl"]    = round(s["total_pnl"] / s["trades"], 6)
        s["win_rate"]   = round(s["wins"] / s["trades"], 4)
        RULE_SCORES.write_text(json.dumps(scores, indent=2))
    except Exception as e:
        print(f"[SIM] rule_scores error: {e}", flush=True)


def _compute_and_log_pnl(sym_short: str, tv_symbol: str, qty: float,
                          exit_price: float | None, exit_rule: str):
    if not exit_price:
        return
    entries = _sim_entries.get(sym_short, [])
    remaining = qty
    while remaining > 0 and entries:
        e = entries[0]
        used = min(e["qty"], remaining)
        if e.get("fill_price"):
            pnl     = round((exit_price - e["fill_price"]) * used, 6)
            pnl_pct = round((exit_price - e["fill_price"]) / e["fill_price"] * 100, 4)
            _append_jsonl(PNL_LOG, {
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": tv_symbol, "qty": used,
                "entry_price": e["fill_price"], "exit_price": exit_price,
                "pnl": pnl, "pnl_pct": pnl_pct,
                "entry_rule": e["rule_id"], "exit_rule": exit_rule,
                "entry_ts": e["ts"],
            })
            _update_rule_scores(e["rule_id"], pnl)
        e["qty"] -= used
        remaining -= used
        if e["qty"] <= 0:
            entries.pop(0)
    if entries:
        _sim_entries[sym_short] = entries
    else:
        _sim_entries.pop(sym_short, None)


def _reconcile_positions():
    """Rebuild _sim_positions + _sim_entries from sim_log. Called on start_watch."""
    global _sim_positions, _sim_entries
    _sim_positions = {}
    _sim_entries   = {}
    try:
        lines = SIM_LOG.read_text().strip().split("\n")
        for line in lines:
            if not line.strip():
                continue
            rec  = json.loads(line)
            if not rec.get("ok"):
                continue
            sym  = rec["symbol"].split(":")[-1]
            side = rec.get("side", "")
            qty  = float(rec.get("qty", 1))
            if side == "buy":
                _sim_positions[sym] = _sim_positions.get(sym, 0.0) + qty
                _sim_entries.setdefault(sym, []).append({
                    "ts": rec["ts"], "qty": qty,
                    "fill_price": rec.get("fill_price"),
                    "rule_id": rec.get("rule_id"),
                })
            elif side == "sell":
                held = max(0.0, _sim_positions.get(sym, 0.0) - qty)
                if held == 0.0:
                    _sim_positions.pop(sym, None)
                else:
                    _sim_positions[sym] = held
                rem = qty
                ents = _sim_entries.get(sym, [])
                while rem > 0 and ents:
                    used = min(ents[0]["qty"], rem)
                    ents[0]["qty"] -= used
                    rem -= used
                    if ents[0]["qty"] <= 0:
                        ents.pop(0)
                if ents:
                    _sim_entries[sym] = ents
                else:
                    _sim_entries.pop(sym, None)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[SIM] reconcile error: {e}", flush=True)


# ── persistence ────────────────────────────────────────────────────────────────

def _load_rules() -> dict:
    try:
        return json.loads(RULES_FILE.read_text())
    except Exception:
        return {"panes": [], "last_applied": None}


def _save_rules(panes: list[dict]):
    RULES_FILE.write_text(json.dumps({
        "panes": panes,
        "last_applied": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


def _normalize(rules: Union[list, dict]) -> list[dict]:
    """Accept list[{symbol,tf}] or dict{symbol:tf}."""
    if isinstance(rules, list):
        return [{"symbol": p["symbol"], "tf": p.get("tf", p.get("timeframe", "1D"))} for p in rules]
    return [{"symbol": s, "tf": t} for s, t in rules.items()]


# nav + read delegated to tv_utils (avoids circular import with paper_tv)
_navigate   = tv_utils.navigate
_parse_title = tv_utils.parse_title
_read_ohlcv  = tv_utils.read_ohlcv


# ── tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def apply_layout(rules: Union[list, dict]) -> str:
    """
    Open unlimited panes (one browser tab per symbol). Saves to rules.json.

    Preferred:
      [{"symbol": "NASDAQ:AAPL", "tf": "1D"},
       {"symbol": "BINANCE:BTCUSDT", "tf": "4h"},
       {"symbol": "FX:EURUSD", "tf": "1h"}]

    Also accepts: {"NASDAQ:AAPL": "1D", "BINANCE:BTCUSDT": "4h"}

    After any restart, call restore_layout() to re-open all tabs.
    """
    panes = _normalize(rules)
    results = []
    for i, pane in enumerate(panes):
        page = await ensure_tv(i)
        await _navigate(page, pane["symbol"], pane["tf"])
        ticker = await _parse_title(page)
        results.append(f"[{i}] {ticker['symbol']} @ {pane['tf']} | price={ticker.get('price','?')}")
    _save_rules(panes)
    n = await page_count()
    return "\n".join(results) + f"\n✓ {len(panes)} panes | {n} tabs | saved → {RULES_FILE.name}"


@mcp.tool()
async def restore_layout() -> str:
    """Re-open all panes from rules.json. Closes orphan tabs. Call after any restart or crash."""
    from browser import close_orphan_tabs
    data  = _load_rules()
    panes = data.get("panes", [])
    if not panes:
        return "no saved panes — call apply_layout first"
    result = await apply_layout(panes)
    await close_orphan_tabs(len(panes))   # trim any excess tabs
    return result


@mcp.tool()
async def health_check() -> dict:
    """
    Full system health: Chrome status, login state, tab count, watch loop, systems loaded.
    Run this after any crash or unexpected behavior.
    """
    from browser import is_healthy, is_logged_in, page_count as _page_count, get_page
    data   = _load_rules()
    n_tabs = 0
    chrome_ok   = False
    login_ok    = False
    login_urls  = []

    try:
        chrome_ok = await is_healthy()
        if chrome_ok:
            n_tabs = await _page_count()
            for i in range(min(n_tabs, 5)):
                try:
                    page = await get_page(i)
                    logged = await is_logged_in(page)
                    login_urls.append({"pane": i, "url": page.url[:60], "logged_in": logged})
                    if not logged:
                        login_ok = False
                except Exception:
                    pass
            login_ok = all(p["logged_in"] for p in login_urls)
    except Exception as e:
        chrome_ok = False

    return {
        "chrome":       "✓ healthy" if chrome_ok else "✗ down",
        "login":        "✓ ok" if login_ok else "✗ session expired or unknown",
        "tabs_open":    n_tabs,
        "saved_panes":  len(data.get("panes", [])),
        "watch_running": bool(_watch_task and not _watch_task.done()),
        "watch_interval": _watch_interval if _watch_task and not _watch_task.done() else None,
        "systems_loaded": [s["name"] for s in _active_systems],
        "pages":        login_urls,
    }


@mcp.tool()
async def set_symbol(symbol: str, timeframe: str = "1D", pane: int = 0) -> str:
    """
    Set symbol + timeframe on one pane. Updates rules.json.
    symbol: 'NASDAQ:AAPL' | 'BINANCE:BTCUSDT' | 'FX:EURUSD'
    timeframe: '1m' '5m' '15m' '1h' '4h' '1D' '1W' '1M'
    pane: tab index (0-based)
    """
    page = await ensure_tv(pane)
    await _navigate(page, symbol, timeframe)
    ticker = await _parse_title(page)
    # Patch rules for this pane slot
    data  = _load_rules()
    saved = data.get("panes", [])
    while len(saved) <= pane:
        saved.append({"symbol": "", "tf": "1D"})
    saved[pane] = {"symbol": symbol, "tf": timeframe}
    _save_rules(saved)
    return f"[pane {pane}] {ticker['symbol']} @ {timeframe} | price={ticker.get('price','?')}"


@mcp.tool()
async def get_price(pane: int = 0) -> dict:
    """
    Live price + OHLCV from TradingView DOM. Zero-lag, no external API.
    pane: tab index (0-based).
    Returns: {pane, symbol, price, direction, change, open, high, low, close}
    """
    page   = await ensure_tv(pane)
    ticker = await _parse_title(page)
    ohlcv  = await _read_ohlcv(page)
    return {"pane": pane, **ticker, **ohlcv}


@mcp.tool()
async def get_all_prices() -> list:
    """Live price + OHLCV from every open pane simultaneously."""
    n = await page_count()
    results = []
    for i in range(n):
        try:
            page   = await ensure_tv(i)
            ticker = await _parse_title(page)
            ohlcv  = await _read_ohlcv(page)
            results.append({"pane": i, **ticker, **ohlcv})
        except Exception as e:
            results.append({"pane": i, "error": str(e)})
    return results


@mcp.tool()
async def screenshot(pane: int = 0, label: Optional[str] = None) -> str:
    """
    Screenshot a specific pane. Returns base64 PNG.
    Bot sees exactly what user sees on that tab.
    """
    page = await ensure_tv(pane)
    png  = await page.screenshot(full_page=False, type="png")
    b64  = base64.b64encode(png).decode()
    tag  = f"[pane{pane}" + (f"·{label}" if label else "") + "] "
    return f"{tag}data:image/png;base64,{b64}"


@mcp.tool()
async def screenshot_all() -> list:
    """Screenshot every open pane. Returns list of {pane, symbol, image}."""
    n       = await page_count()
    results = []
    for i in range(n):
        try:
            page   = await ensure_tv(i)
            ticker = await _parse_title(page)
            png    = await page.screenshot(full_page=False, type="png")
            results.append({
                "pane":   i,
                "symbol": ticker.get("symbol", "?"),
                "image":  f"data:image/png;base64,{base64.b64encode(png).decode()}",
            })
        except Exception as e:
            results.append({"pane": i, "error": str(e)})
    return results


@mcp.tool()
async def add_indicator(name: str, pane: int = 0) -> str:
    """Add indicator by name on a specific pane. e.g. 'RSI', 'MACD', 'EMA'."""
    page = await ensure_tv(pane)
    try:
        await page.locator('[data-name="open-indicators-dialog"]').first.click(timeout=3000)
    except Exception:
        await page.keyboard.press("Alt+i")
    await page.wait_for_timeout(700)
    await page.keyboard.type(name, delay=40)
    await page.wait_for_timeout(700)
    try:
        await page.locator('[data-name="menu-inner"] [class*="item"]').first.click(timeout=2000)
    except Exception:
        await page.keyboard.press("Enter")
    await page.wait_for_timeout(400)
    await page.keyboard.press("Escape")
    return f"[pane {pane}] indicator added: {name}"


@mcp.tool()
async def list_panes() -> dict:
    """Show all open panes, saved symbols, tab count."""
    data = _load_rules()
    n    = await page_count()
    return {
        "open_tabs":    n,
        "saved_panes":  data.get("panes", []),
        "last_applied": data.get("last_applied"),
    }


@mcp.tool()
async def dismiss_popups(pane: int = 0) -> dict:
    """
    Manually dismiss TV announcement/modal popups on a pane.
    Called automatically each watch cycle and before every paper order.
    """
    page = await ensure_tv(pane)
    dismissed = await tv_utils.dismiss_popups(page)
    return {"pane": pane, "dismissed": dismissed}


@mcp.tool()
async def get_status() -> str:
    """Quick health check: tab count + saved layout."""
    data  = _load_rules()
    n     = await page_count()
    panes = data.get("panes", [])
    lines = [f"open_tabs: {n}", f"saved_panes: {len(panes)}"]
    for i, p in enumerate(panes):
        lines.append(f"  [{i}] {p.get('symbol')} @ {p.get('tf')}")
    lines.append(f"last_applied: {data.get('last_applied')}")
    return "\n".join(lines)


# ── rule system tools ──────────────────────────────────────────────────────────

@mcp.tool()
async def load_systems(names: Optional[list] = None) -> str:
    """
    Load rule systems. Deduplicates panes across all systems.
    Opens exactly the tabs needed (shared panes = shared tab).

    names: list of system names (without .json) — or omit to load ALL systems.
    Example: load_systems(["example_momentum", "example_levels"])
    """
    global _active_systems, _active_panes

    if names:
        systems = [load_system(n) for n in names]
    else:
        systems = load_all_systems()

    if not systems:
        return "no systems found in systems/"

    _active_panes   = build_pane_registry(systems)
    _active_systems = systems

    # Open exactly the deduplicated tabs
    results = []
    for i, pane in enumerate(_active_panes):
        page = await ensure_tv(i)
        await _navigate(page, pane["symbol"], pane["tf"])
        ticker = await _parse_title(page)
        results.append(f"  [{i}] {pane['symbol']} @ {pane['tf']} | {ticker.get('price','?')}")

    _save_rules(_active_panes)

    sys_names = [s["name"] for s in _active_systems]
    return (
        f"systems loaded: {sys_names}\n"
        f"panes (deduped): {len(_active_panes)}\n"
        + "\n".join(results)
    )


@mcp.tool()
async def evaluate_rules(system_name: Optional[str] = None) -> list:
    """
    Evaluate all rules against live DOM prices. Each system runs independently.
    Prices fetched in parallel across all panes. Saves state back to system files.
    system_name: evaluate one system only — or omit for all loaded systems.
    Returns list of rule results with triggered status.
    """
    global _active_systems, _active_panes

    if not _active_systems:
        return [{"error": "no systems loaded — call load_systems first"}]

    # Fetch all pane prices in parallel — one read per tab, zero blocking
    async def _fetch_pane(i: int) -> tuple[int, dict]:
        try:
            page   = await ensure_tv(i)
            ticker = await _parse_title(page)
            ohlcv  = await _read_ohlcv(page)
            combined = {**ticker, **ohlcv}
            # Fallback: if OHLCV empty, inject title price as close so rules can still fire
            if not ohlcv and ticker.get("price"):
                combined["close"] = ticker["price"]
                combined["open"]  = ticker["price"]
            return i, combined
        except Exception:
            return i, {}

    fetched = await asyncio.gather(*[_fetch_pane(i) for i in range(len(_active_panes))])
    prices_by_pane: dict[int, dict] = dict(fetched)

    # Persist every tick — foundation for backtest + P&L analysis
    for i, price in prices_by_pane.items():
        if price and i < len(_active_panes):
            p = _active_panes[i]
            _log_price(i, p["symbol"], p["tf"], price)

    # Each system evaluates independently — isolated state, no cross-contamination
    target = [s for s in _active_systems if not system_name or s["name"] == system_name]
    all_results = []
    for sys in target:
        results = evaluate_system(sys, prices_by_pane)
        save_system(sys)
        all_results.extend(results)

    return all_results


@mcp.tool()
async def get_full_picture() -> dict:
    """
    Full state snapshot across all loaded systems.
    Shows every rule: triggered, count, last value, last triggered, history length.
    """
    if not _active_systems:
        return {"error": "no systems loaded — call load_systems first"}
    return full_picture(_active_systems)


@mcp.tool()
async def get_rule_state(system_name: str, rule_id: str) -> dict:
    """
    Full state + history for one specific rule.
    system_name: e.g. 'example_momentum'
    rule_id: e.g. 'aapl_above_270'
    """
    for sys in _active_systems:
        if sys["name"] == system_name:
            return get_rule_history(sys, rule_id)
    return {"error": f"system {system_name!r} not loaded"}


@mcp.tool()
async def list_systems() -> dict:
    """List all system files on disk and which are currently loaded."""
    on_disk  = [f.stem for f in list_system_files()]
    loaded   = [s["name"] for s in _active_systems]
    return {"on_disk": on_disk, "loaded": loaded, "panes": _active_panes}


@mcp.tool()
async def reload_systems() -> str:
    """Hot-reload all currently active systems from disk (picks up rule edits)."""
    if not _active_systems:
        return "no systems loaded"
    names = [s["name"] for s in _active_systems]
    return await load_systems(names)


@mcp.tool()
async def start_watch(interval_seconds: int = 30, auto_trade: bool = False) -> str:
    """
    Start background rule evaluation loop. Runs evaluate_rules() every N seconds.
    Logs triggered rules. Replaces any running watch.

    interval_seconds: 5–3600 (default 30)
    auto_trade: if True, rules with action 'buy'/'sell' auto-execute paper orders
                e.g. action 'buy:2' → buy 2 units; bare 'buy' → buy 1 unit
    """
    global _watch_task, _watch_interval

    interval_seconds = max(5, min(3600, interval_seconds))

    # ── crash-immune recovery ───────────────────────────────────────────────

    async def _recover_chrome() -> bool:
        """Recover Chrome + restore layout + reload systems. Returns True on success."""
        from browser import recover, is_healthy
        ok = await recover()
        if not ok:
            return False
        # Re-open tabs
        try:
            await restore_layout()
        except Exception as e:
            print(f"[WATCH] restore_layout after recovery failed: {e}", flush=True)
            return False
        # Reload active systems (in-memory state was lost)
        if _active_systems:
            names = [s["name"] for s in _active_systems]
            try:
                await load_systems(names)
            except Exception as e:
                print(f"[WATCH] reload_systems after recovery failed: {e}", flush=True)
        return await is_healthy()

    # ── execution ───────────────────────────────────────────────────────────

    async def _execute_actions(r: dict):
        from browser import is_logged_in, ensure_tv as _ensure_tv
        for action in r.get("actions", []):
            parts     = action.split(":")
            verb      = parts[0].lower()
            qty       = float(parts[1]) if len(parts) > 1 else 1.0
            pane_data = _active_panes[r["pane"]] if r["pane"] < len(_active_panes) else {}
            tv_symbol = pane_data.get("symbol", r["symbol"])
            sym_short = tv_symbol.split(":")[-1]

            if verb not in ("buy", "sell"):
                continue

            # Login guard — never execute if session expired
            try:
                page = await _ensure_tv(r["pane"])
                if not await is_logged_in(page):
                    msg = f"TV session expired — skipping {verb} {tv_symbol}"
                    print(f"[WATCH] SKIP {verb} {tv_symbol} — TV session expired", flush=True)
                    _log_event("login_expired", msg)
                    continue
            except Exception as e:
                print(f"[WATCH] SKIP {verb} {tv_symbol} — page check failed: {e}", flush=True)
                _log_event("page_check_failed", f"{verb} {tv_symbol}: {e}")
                continue

            # Position guard — in-memory, no DOM dependency
            held = _sim_positions.get(sym_short, 0.0)
            if verb == "buy" and held > 0:
                print(f"[WATCH] SKIP BUY {tv_symbol} — already holding {held}", flush=True)
                continue
            if verb == "sell" and held == 0:
                print(f"[WATCH] SKIP SELL {tv_symbol} — no position to close", flush=True)
                continue

            ts     = datetime.now(timezone.utc).isoformat()
            result = await paper_tv.place_order(tv_symbol, verb, qty, r["pane"])
            status = "✓" if result.get("ok") else "✗"
            print(
                f"[WATCH] {status} AUTO-{verb.upper()} {tv_symbol} x{qty} "
                f"| {result.get('button_text') or result.get('error','')}",
                flush=True,
            )
            # Parse fill price from button_text: "Buy\n1 AAPL @ 270.22 LIMIT"
            fill_price = None
            btn = result.get("button_text") or ""
            if " @ " in btn:
                try:
                    fill_price = float(btn.split(" @ ")[1].split()[0].replace(",", ""))
                except Exception:
                    pass

            if result.get("ok"):
                if verb == "buy":
                    _sim_positions[sym_short] = _sim_positions.get(sym_short, 0.0) + qty
                    _sim_entries.setdefault(sym_short, []).append({
                        "ts": ts, "qty": qty, "fill_price": fill_price, "rule_id": r["rule_id"],
                    })
                elif verb == "sell":
                    _compute_and_log_pnl(sym_short, tv_symbol, qty, fill_price, r["rule_id"])
                    held = max(0.0, _sim_positions.get(sym_short, 0.0) - qty)
                    if held == 0.0:
                        _sim_positions.pop(sym_short, None)
                    else:
                        _sim_positions[sym_short] = held
            _log_trade({
                "ts": ts, "rule_id": r["rule_id"], "symbol": tv_symbol,
                "side": verb, "qty": qty, "signal_value": r.get("value"),
                "fill_price": fill_price,
                "ok": result.get("ok"), "button_text": result.get("button_text"),
                "error": result.get("error"),
            })
            if not result.get("ok"):
                print(f"[WATCH] ORDER FAILED: {result}", flush=True)

    # ── main loop ───────────────────────────────────────────────────────────

    _consecutive_errors = 0
    MAX_CONSECUTIVE     = 3    # Chrome recovery threshold

    async def _loop():
        nonlocal _consecutive_errors
        _reconcile_positions()   # rebuild from sim_log on every (re)start
        print(f"[WATCH] positions reconciled: {_sim_positions}", flush=True)
        _log_event("watch_start", f"interval={interval_seconds}s auto_trade={auto_trade} systems={[s['name'] for s in _active_systems]}")
        while True:
            try:
                # Dismiss any TV popups that could block DOM reads
                for i in range(len(_active_panes)):
                    try:
                        page = await ensure_tv(i)
                        await tv_utils.dismiss_popups(page)
                    except Exception:
                        pass

                results = await evaluate_rules()
                _consecutive_errors = 0   # reset on success

                triggered = [r for r in results if r.get("triggered")]
                for r in triggered:
                    print(
                        f"[WATCH] TRIGGERED {r['rule_id']} | {r['symbol']} "
                        f"| {r['condition']} | val={r['value']}",
                        flush=True,
                    )
                    if auto_trade:
                        try:
                            await _execute_actions(r)
                        except Exception as e:
                            print(f"[WATCH] execute error: {e}", flush=True)

            except asyncio.CancelledError:
                _log_event("watch_stop", "cancelled")
                raise
            except Exception as e:
                _consecutive_errors += 1
                print(f"[WATCH] error ({_consecutive_errors}/{MAX_CONSECUTIVE}): {e}", flush=True)
                _log_event("watch_error", f"({_consecutive_errors}/{MAX_CONSECUTIVE}): {e}")

                if _consecutive_errors >= MAX_CONSECUTIVE:
                    print("[WATCH] threshold reached — recovering Chrome...", flush=True)
                    _log_event("chrome_recovery_start", f"after {_consecutive_errors} errors")
                    recovered = await _recover_chrome()
                    if recovered:
                        _consecutive_errors = 0
                        print("[WATCH] recovered — resuming", flush=True)
                        _log_event("chrome_recovery_ok", "")
                    else:
                        print("[WATCH] recovery failed — sleeping 60s before retry", flush=True)
                        _log_event("chrome_recovery_failed", "sleeping 60s")
                        await asyncio.sleep(60)

            await asyncio.sleep(interval_seconds)

    if _watch_task and not _watch_task.done():
        _watch_task.cancel()
    _watch_interval    = interval_seconds
    _consecutive_errors = 0
    _watch_task = asyncio.create_task(_loop())
    return (
        f"watch started — interval={interval_seconds}s | "
        f"auto_trade={auto_trade} | systems={[s['name'] for s in _active_systems]}"
    )


@mcp.tool()
async def stop_watch() -> str:
    """Stop the background rule evaluation loop."""
    global _watch_task, _watch_interval
    if _watch_task and not _watch_task.done():
        _watch_task.cancel()
        _watch_task = None
        _watch_interval = 0
        return "watch stopped"
    return "no watch running"


@mcp.tool()
async def watch_status() -> dict:
    """Check if background watch is running, interval, and current sim positions."""
    running = bool(_watch_task and not _watch_task.done())
    return {
        "running": running,
        "interval_seconds": _watch_interval if running else None,
        "sim_positions": dict(_sim_positions),
    }


@mcp.tool()
async def get_sim_log(last_n: int = 20) -> list:
    """
    Read last N trade entries from sim_log.jsonl. Persists across restarts.
    Returns list of {ts, rule_id, symbol, side, qty, value, ok, button_text, error}.
    """
    try:
        lines = SIM_LOG.read_text().strip().split("\n")
        return [json.loads(l) for l in lines[-last_n:] if l.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def clear_sim_log() -> str:
    """Wipe sim_log.jsonl. Use before a fresh simulation run."""
    try:
        SIM_LOG.write_text("")
        return "sim_log cleared"
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
async def get_price_log(last_n: int = 20, symbol: Optional[str] = None) -> list:
    """
    Read last N price ticks from price_log.jsonl.
    symbol: filter to one symbol e.g. 'NASDAQ:AAPL' (optional)
    """
    try:
        lines = [l for l in PRICE_LOG.read_text().strip().split("\n") if l.strip()]
        entries = [json.loads(l) for l in lines]
        if symbol:
            entries = [e for e in entries if e.get("symbol") == symbol]
        return entries[-last_n:]
    except FileNotFoundError:
        return []
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def get_pnl_log(last_n: int = 20) -> list:
    """
    Read realized P&L entries from pnl_log.jsonl.
    Each entry: {ts, symbol, qty, entry_price, exit_price, pnl, pnl_pct, entry_rule, exit_rule, entry_ts}
    """
    try:
        lines = [l for l in PNL_LOG.read_text().strip().split("\n") if l.strip()]
        return [json.loads(l) for l in lines[-last_n:]]
    except FileNotFoundError:
        return []
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def get_rule_scores() -> dict:
    """
    Aggregate P&L performance per rule from rule_scores.json.
    Shows: trades, wins, losses, total_pnl, avg_pnl, win_rate per rule.
    """
    try:
        return json.loads(RULE_SCORES.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def get_event_log(last_n: int = 30) -> list:
    """
    Read last N system events from event_log.jsonl.
    Events: watch_start/stop, watch_error, chrome_recovery_*, login_expired, page_check_failed.
    """
    try:
        lines = [l for l in EVENT_LOG.read_text().strip().split("\n") if l.strip()]
        return [json.loads(l) for l in lines[-last_n:]]
    except FileNotFoundError:
        return []
    except Exception as e:
        return [{"error": str(e)}]


# ── paper trading DOM probe ────────────────────────────────────────────────────

@mcp.tool()
async def probe_paper_dom(pane: int = 0) -> dict:
    """
    Live DOM probe for paper trading panel — account, positions, order entry selectors.
    Run after connecting Paper Trading broker in TV.
    """
    page = await ensure_tv(pane)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)

    return await page.evaluate("""() => {
        const qa = sel => [...document.querySelectorAll(sel)];

        const panelTabs = qa('[data-name="round-tabs-buttons"] button, [data-name="round-tabs-anchors"] button')
            .filter(el => el.offsetParent !== null)
            .map(el => ({text: el.innerText?.trim(), dn: el.getAttribute('data-name')}));

        const bottomDataNames = qa('[data-name]')
            .filter(el => {
                const r = el.getBoundingClientRect();
                return r.top > window.innerHeight * 0.55 && el.offsetParent !== null && el.innerText?.trim();
            })
            .map(el => ({name: el.getAttribute('data-name'), text: el.innerText?.trim().slice(0,80)}));

        const inputs = qa('input')
            .filter(el => el.offsetParent !== null)
            .map(el => ({name: el.name, type: el.type, ph: el.placeholder, val: el.value}));

        const bottomText = qa('*')
            .filter(el => {
                const r = el.getBoundingClientRect();
                return el.children.length === 0 && el.offsetParent !== null
                    && r.top > window.innerHeight * 0.55 && el.innerText?.trim().length > 0;
            })
            .map(el => el.innerText?.trim())
            .filter(t => t.length < 100)
            .slice(0, 50);

        const orderForm = qa('[class*="orderEntry"],[class*="OrderEntry"],[class*="order-entry"],[class*="orderTicket"],[class*="tradePanel"],[class*="brokerPage"]')
            .filter(el => el.offsetParent !== null)
            .map(el => ({cls: el.className.slice(0,80), text: el.innerText?.trim().slice(0,200)}));

        return { panelTabs, bottomDataNames, inputs, bottomText, orderForm };
    }""")


# ── paper trading tools ────────────────────────────────────────────────────────

@mcp.tool()
async def paper_buy(symbol: str, qty: float = 1, pane: int = 0) -> dict:
    """
    Place a paper BUY order via TradingView Paper Trading (DOM-native, zero deps).
    symbol: TV format — 'NASDAQ:AAPL' | 'BINANCE:BTCUSDT'
    qty: shares/units (default 1)
    pane: chart tab index (default 0)
    """
    return await paper_tv.place_order(symbol, "buy", qty, pane)


@mcp.tool()
async def paper_sell(symbol: str, qty: float = 1, pane: int = 0) -> dict:
    """
    Place a paper SELL order via TradingView Paper Trading (DOM-native, zero deps).
    symbol: TV format — 'NASDAQ:AAPL' | 'BINANCE:BTCUSDT'
    qty: shares/units (default 1)
    pane: chart tab index (default 0)
    """
    return await paper_tv.place_order(symbol, "sell", qty, pane)


@mcp.tool()
async def get_positions(pane: int = 0) -> list:
    """Open paper positions from TradingView paper trading panel."""
    return await paper_tv.get_positions(pane)


@mcp.tool()
async def get_account(pane: int = 0) -> dict:
    """Account metrics from TradingView paper trading panel (equity, P&L, etc)."""
    return await paper_tv.get_account_metrics(pane)


if __name__ == "__main__":
    mcp.run(transport="stdio")
