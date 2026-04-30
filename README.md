# tradingview-harness

MCP server that gives Claude full control of TradingView — unlimited chart panes, live DOM price feed, persistent layout across restarts.

## How it works

```
Claude (MCP client)
  └── FastMCP server (stdio)
        └── Playwright → Chrome (persistent profile)
              └── N browser tabs = N chart panes
                    └── tradingview.com/chart/?symbol=X&interval=Y
```

- **You see**: real TradingView charts in your browser  
- **Bot sees**: same charts via screenshot + live OHLCV from DOM  
- **Zero lag**: DOM reads from `<title>` tag + header text nodes  
- **Zero liability**: personal session, URL navigation, no scraping

## Setup

### 1. Install dependencies

```bash
pip install fastmcp playwright
python -m playwright install chromium
```

### 2. Register MCP server in Claude Code

`.mcp.json` is already configured. On first open, Claude Code will prompt to approve it. Or set `enableAllProjectMcpServers: true` in `.claude/settings.json`.

### 3. Run

Open this project in Claude Code. The MCP server starts automatically.

Then call:

```
restore_layout()        # re-open saved panes after restart
get_all_prices()        # verify all panes live
```

## Tools

| Tool | Description |
|---|---|
| `apply_layout(panes)` | Open N tabs, set each symbol — saves to `rules.json` |
| `restore_layout()` | Re-apply `rules.json` after restart or crash |
| `set_symbol(symbol, tf, pane)` | Set one pane, patch rules |
| `get_price(pane)` | Live price + OHLCV from DOM |
| `get_all_prices()` | Price from every open pane |
| `screenshot(pane)` | Base64 PNG — bot sees what you see |
| `screenshot_all()` | Screenshot every pane |
| `add_indicator(name, pane)` | Add RSI, MACD, EMA, etc. |
| `list_panes()` | Tab count + saved layout |
| `get_status()` | Health check |

## Layout rules

```json
[
  {"symbol": "NASDAQ:AAPL",     "tf": "1D"},
  {"symbol": "BINANCE:BTCUSDT", "tf": "4h"},
  {"symbol": "FX:EURUSD",       "tf": "1h"}
]
```

Timeframes: `1m 3m 5m 15m 30m 1h 2h 4h 1D 1W 1M`

Saved to `rules.json` automatically. Copy `rules.example.json` to start.

## Chrome profile

Persistent at `~/.crackincorrelation/chrome-profile`  
CDP port: `9223`  
Profile survives restarts — TradingView session (login) stays intact.

## Restart sequence

1. Claude Code loads → MCP server starts  
2. Call `restore_layout()` → all tabs re-open  
3. Call `get_all_prices()` → confirm live
