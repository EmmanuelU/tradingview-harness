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

mcp = FastMCP("tradingview")

RULES_FILE = Path(__file__).parent.parent / "rules.json"
TV_CHART   = "https://www.tradingview.com/chart/"

# TV URL interval codes
TF_CODES = {
    "1m": "1",   "3m": "3",   "5m": "5",   "15m": "15",  "30m": "30",
    "1h": "60",  "2h": "120", "4h": "240",
    "1D": "D",   "1W": "W",   "1M": "M",
}


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


# ── nav + read ─────────────────────────────────────────────────────────────────

async def _navigate(page, symbol: str, tf: str):
    """Navigate page to TV chart URL with symbol + interval baked in. Zero clicking."""
    code = TF_CODES.get(tf, "D")
    url  = f"{TV_CHART}?symbol={symbol}&interval={code}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    # Poll until title contains a real price (not TV's splash title).
    # TV sets title to "SYMBOL PRICE ..." once chart renders — typically <2s.
    ticker_root = symbol.split(":")[-1]  # "NASDAQ:AAPL" → "AAPL"
    for _ in range(30):                  # max 6s (30 × 200ms)
        title = await page.title()
        if ticker_root in title and any(ch.isdigit() for ch in title):
            break
        await page.wait_for_timeout(200)


async def _parse_title(page) -> dict:
    """
    TV sets <title> to live ticker: 'AAPL 270.22 ▲ +0.50%'
    Returns {symbol, price, direction, change}.
    """
    title = await page.title()
    parts = title.split()
    result = {"symbol": parts[0] if parts else "?", "raw_title": title}
    if len(parts) >= 2:
        result["price"] = parts[1]
    if len(parts) >= 4:
        result["direction"] = parts[2]   # ▲ or ▼
        result["change"]    = parts[3]
    return result


async def _read_ohlcv(page) -> dict:
    """Read OHLCV from DOM header row (top 120px, text-only leaf nodes)."""
    texts = await page.evaluate("""() =>
        [...document.querySelectorAll('*')]
            .filter(el => el.children.length === 0 && el.offsetParent !== null
                       && el.getBoundingClientRect().top < 120)
            .map(el => el.innerText?.trim())
            .filter(t => t && t.length > 0)
    """)
    ohlcv: dict = {}
    key_map = {"O": "open", "H": "high", "L": "low", "C": "close"}
    last = None
    for t in texts:
        if t in key_map:
            last = key_map[t]
        elif last:
            ohlcv[last] = t
            last = None
    return ohlcv


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
    """Re-open all panes from rules.json. Call after any restart or crash."""
    data  = _load_rules()
    panes = data.get("panes", [])
    if not panes:
        return "no saved panes — call apply_layout first"
    return await apply_layout(panes)


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

    async def _execute_actions(r: dict):
        for action in r.get("actions", []):
            parts  = action.split(":")
            verb   = parts[0].lower()
            qty    = float(parts[1]) if len(parts) > 1 else 1.0
            symbol = r["symbol"]

            # Resolve full TV symbol from active panes
            pane_data = _active_panes[r["pane"]] if r["pane"] < len(_active_panes) else {}
            tv_symbol = pane_data.get("symbol", symbol)

            if verb == "buy":
                result = await paper_tv.place_order(tv_symbol, "buy", qty, r["pane"])
                print(f"[WATCH] AUTO-BUY {tv_symbol} x{qty} → ok={result.get('ok')} {result.get('button_text','')}", flush=True)
            elif verb == "sell":
                result = await paper_tv.place_order(tv_symbol, "sell", qty, r["pane"])
                print(f"[WATCH] AUTO-SELL {tv_symbol} x{qty} → ok={result.get('ok')} {result.get('button_text','')}", flush=True)

    async def _loop():
        while True:
            try:
                results = await evaluate_rules()
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
                            print(f"[WATCH] auto_trade error: {e}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[WATCH] error: {e}", flush=True)
            await asyncio.sleep(interval_seconds)

    if _watch_task and not _watch_task.done():
        _watch_task.cancel()
    _watch_interval = interval_seconds
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
    """Check if background watch is running and its interval."""
    running = bool(_watch_task and not _watch_task.done())
    return {"running": running, "interval_seconds": _watch_interval if running else None}


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
