"""TradingView MCP — unlimited panes, URL-based nav, DOM price, full persistence."""

import asyncio
import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from fastmcp import FastMCP
from browser import ensure_tv, get_page, page_count

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
    await page.wait_for_timeout(2500)


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


if __name__ == "__main__":
    mcp.run(transport="stdio")
