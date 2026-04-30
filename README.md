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

### Layout

| Tool | Description |
|---|---|
| `apply_layout(panes)` | Open N tabs, set each symbol — saves to `rules.json` |
| `restore_layout()` | Re-apply `rules.json` after restart or crash |
| `set_symbol(symbol, tf, pane)` | Set one pane, patch rules |
| `list_panes()` | Tab count + saved layout |
| `get_status()` | Health check |

### Price

| Tool | Description |
|---|---|
| `get_price(pane)` | Live price + OHLCV from DOM |
| `get_all_prices()` | Price from every open pane simultaneously |
| `screenshot(pane, label)` | Base64 PNG — bot sees what you see |
| `screenshot_all()` | Screenshot every pane |
| `add_indicator(name, pane)` | Add RSI, MACD, EMA, etc. |

### Rule Systems

| Tool | Description |
|---|---|
| `load_systems(names)` | Load systems from `systems/*.json`. Deduplicates panes across systems. Omit `names` to load all. |
| `evaluate_rules(system_name)` | Fetch live prices (parallel) → evaluate all rules → save state. Omit `system_name` for all. |
| `get_full_picture()` | Full state snapshot: every rule + system-level stats (trigger_rate, eval_count) |
| `get_rule_state(system_name, rule_id)` | State + full history for one rule |
| `list_systems()` | Systems on disk vs loaded |
| `reload_systems()` | Hot-reload active systems from disk (pick up rule edits) |

### Background Watch

| Tool | Description |
|---|---|
| `start_watch(interval_seconds)` | Evaluate rules every N seconds in background. Logs triggers to stdout. Default 30s. |
| `stop_watch()` | Stop background watch loop |
| `watch_status()` | Check if watch is running and its interval |

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
