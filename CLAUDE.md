# TradingView MCP — crackincorrelationprofiler

Bot controls tradingview.com via Playwright. Human and bot share the same live charts.

## Architecture

```
MCP server (FastMCP, stdio)
  └── Playwright → Chrome (persistent profile)
        └── N browser tabs = N chart panes (unlimited)
              └── tradingview.com/chart/?symbol=X&interval=Y
```

## Key files

| File | Purpose |
|---|---|
| `mcp_server/server.py` | All MCP tools |
| `mcp_server/browser.py` | Playwright singleton, multi-tab, CDP-first |
| `rules.json` | Persistent layout — survives restarts |
| `.mcp.json` | Registers MCP server in Claude Code |
| `.claude/settings.json` | `enableAllProjectMcpServers: true` |

## Chrome profile

Persistent at `~/.crackincorrelation/chrome-profile`  
CDP port: **9223** (9222 reserved by agent-browser)  
SingletonLock auto-cleared on fresh launch.

## How panes work

- Each symbol = separate browser tab (truly unlimited, plan-agnostic)
- Navigation uses URL params: `?symbol=NASDAQ:AAPL&interval=D` — no UI clicking
- Live price read from `<title>` tag: format = `SYMBOL PRICE ▲/▼ CHANGE%`
- OHLCV read from DOM header row (top 120px leaf text nodes)

## MCP tools

| Tool | Args | What it does |
|---|---|---|
| `apply_layout` | `[{symbol, tf}, ...]` or `{symbol: tf}` | Open N tabs, set each symbol, save rules |
| `restore_layout` | — | Re-apply rules.json after restart/crash |
| `set_symbol` | `symbol, timeframe, pane=0` | Set one pane, patch rules |
| `get_price` | `pane=0` | Live price + OHLCV from DOM |
| `get_all_prices` | — | Price from every open pane |
| `screenshot` | `pane=0, label` | Base64 PNG of one pane |
| `screenshot_all` | — | Base64 PNG of every pane |
| `add_indicator` | `name, pane=0` | Click indicators dialog, add by name |
| `list_panes` | — | Tab count + saved layout |
| `get_status` | — | Health check |

## Timeframe codes

`1m 3m 5m 15m 30m 1h 2h 4h 1D 1W 1M`

## rules.json schema

```json
{
  "panes": [
    {"symbol": "NASDAQ:AAPL",     "tf": "1D"},
    {"symbol": "BINANCE:BTCUSDT", "tf": "4h"},
    {"symbol": "FX:EURUSD",       "tf": "1h"}
  ],
  "last_applied": "2026-04-30T02:30:38Z"
}
```

## Restart sequence

1. Claude Code starts → loads MCP server via `.mcp.json`
2. Call `restore_layout()` → re-opens all tabs from rules.json
3. Call `get_all_prices()` → verify all panes live

## Zero liability

- Personal session only (own Chrome profile)
- No data extraction or redistribution
- No TradingView API scraping
- URL navigation is standard browser behavior
