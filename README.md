# tradingview-harness

MCP server giving Claude full control of TradingView — unlimited chart panes, live DOM price feed, rule-driven signals, and paper trading execution. No external broker APIs. Everything through the browser.

## Architecture

```
Claude (MCP client)
  └── FastMCP server (stdio)
        └── Playwright → Chrome (persistent profile, port 9223)
              └── N browser tabs = N chart panes
                    └── tradingview.com/chart/?symbol=X&interval=Y
                          └── TV Paper Trading panel (DOM-native orders)
```

- **You see**: real TradingView charts in your browser  
- **Bot sees**: same charts via screenshot + live OHLCV from DOM  
- **Orders**: executed directly via TradingView Paper Trading (DOM clicks, no external API)  
- **Zero deps**: only `fastmcp` + `playwright`  
- **Zero liability**: personal session, URL navigation, no scraping

## Setup

```bash
pip install fastmcp playwright
python -m playwright install chromium
```

Register: `.mcp.json` already configured. Claude Code starts server automatically.

## Turnkey startup sequence

```
1. restore_layout()          # reopen all tabs from rules.json
2. get_all_prices()          # verify live prices
3. load_systems(["paper_momentum"])   # load trading rules
4. start_watch(30, auto_trade=True)  # infinite loop: signal → order
```

That's it. Runs forever until `stop_watch()`.

## Tools

### Layout

| Tool | Args | Description |
|---|---|---|
| `apply_layout(panes)` | `[{symbol, tf}]` | Open N tabs — saves to `rules.json` |
| `restore_layout()` | — | Re-apply `rules.json` after restart |
| `set_symbol(symbol, tf, pane)` | — | Set one pane, patch rules |
| `list_panes()` | — | Tab count + saved layout |
| `get_status()` | — | Health check |

### Price (DOM, zero-lag)

| Tool | Args | Description |
|---|---|---|
| `get_price(pane)` | — | Live price + OHLCV |
| `get_all_prices()` | — | All panes simultaneously |
| `screenshot(pane, label)` | — | Base64 PNG |
| `screenshot_all()` | — | All panes |
| `add_indicator(name, pane)` | — | RSI, MACD, EMA, etc. |

### Rule Systems

| Tool | Args | Description |
|---|---|---|
| `load_systems(names)` | `["name"]` or omit | Load from `systems/*.json`. Deduplicates panes. |
| `evaluate_rules(system_name)` | optional | Live prices → evaluate all rules → save state |
| `get_full_picture()` | — | All rules: trigger_rate, history, stats |
| `get_rule_state(system_name, rule_id)` | — | One rule full history |
| `list_systems()` | — | Disk vs loaded |
| `reload_systems()` | — | Hot-reload (pick up edits) |

### Background Watch + Auto-Trade

| Tool | Args | Description |
|---|---|---|
| `start_watch(interval_seconds, auto_trade)` | `30, False` | Eval loop. `auto_trade=True` → execute orders on trigger |
| `stop_watch()` | — | Stop loop |
| `watch_status()` | — | Running + interval |

### Paper Trading (TV-native, no broker API)

| Tool | Args | Description |
|---|---|---|
| `paper_buy(symbol, qty, pane)` | `"NASDAQ:AAPL", 1, 0` | Buy via TV paper panel |
| `paper_sell(symbol, qty, pane)` | `"NASDAQ:AAPL", 1, 0` | Sell via TV paper panel |
| `get_positions(pane)` | `0` | Open positions from paper panel |
| `get_account(pane)` | `0` | Account metrics (equity, P&L, margin) |
| `probe_paper_dom(pane)` | `0` | DOM probe — rediscover selectors after TV update |

## Rule systems

Files in `systems/*.json`. Two built-in examples:

- `systems/example_momentum.json` — AAPL green day, BTC above 75k (log only)
- `systems/paper_momentum.json` — same signals, auto-executes paper orders

### System schema (v2)

```json
{
  "version": 2,
  "name": "paper_momentum",
  "panes": [{"symbol": "NASDAQ:AAPL", "tf": "1D"}],
  "rules": [
    {
      "id": "paper_aapl_long",
      "pane_symbol": "NASDAQ:AAPL",
      "pane_tf": "1D",
      "conditions": {
        "logic": "AND",
        "items": [
          {"field": "close", "op": ">", "field2": "open"},
          {"field": "close", "op": ">", "value": 270}
        ]
      },
      "actions": ["log", "buy:1"],
      "cooldown_evals": 3
    }
  ]
}
```

**Actions:** `log` | `alert` | `screenshot` | `buy:N` | `sell:N`  
**Fields:** `open` `high` `low` `close` `price` `range` `body` `wick_upper` `wick_lower` `change_pct`  
**Ops:** `>` `<` `>=` `<=` `==` `!=` `crosses_above` `crosses_below`  
**Logic:** `AND` `OR` `NOT`

## Paper trading — nuances learned

| Issue | Fix applied |
|---|---|
| TV buy/sell buttons blocked by overlay | `dispatch_event('click')` bypasses interception |
| Broker dialog needs Paper Trading selected | JS `.click()` on `[data-name='select-broker-dialog']` span |
| Connect button has empty `innerText` | Find by position in `broker-login-dialog`, not text |
| Input uses `id` not `name` | Selector: `input#quantity-field` not `input[name=...]` |
| React inputs ignore direct `.value =` | Use native `HTMLInputElement.prototype.value` setter + dispatch `input`/`change` events |
| Orders 3000 limit warning | Cosmetic — orders still execute |
| Market closed → positions empty | Fills only during market hours; orders queue |
| Chrome singleton lock | Auto-cleared on fresh launch in `browser.py` |
| Standalone scripts vs MCP server | Never call `close()` — MCP server holds persistent context; scripts use CDP attach |
| Paper panel not persistent | `_ensure_paper_connected()` re-connects automatically each call |

## Chrome profile

Persistent at `~/.crackincorrelation/chrome-profile`  
CDP port: `9223` (9222 reserved by agent-browser)  
Login persists — TradingView session survives restarts.

## Restart sequence

1. Claude Code loads → MCP server starts automatically  
2. `restore_layout()` → all tabs reopen  
3. `get_all_prices()` → confirm live  
4. `load_systems(["paper_momentum"])` → load rules  
5. `start_watch(30, auto_trade=True)` → infinite loop running

## Timeframes

`1m 3m 5m 15m 30m 1h 2h 4h 1D 1W 1M`
