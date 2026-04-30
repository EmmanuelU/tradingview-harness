# TradingView MCP — crackincorrelationprofiler

Bot controls tradingview.com via Playwright. Human and bot share the same live charts. Paper orders execute directly through TV's browser UI.

## Architecture

```
MCP server (FastMCP, stdio)
  └── Playwright → Chrome (persistent profile, CDP port 9223)
        └── N browser tabs = N chart panes (unlimited)
              └── tradingview.com/chart/?symbol=X&interval=Y
                    └── TV Paper Trading panel (DOM-native orders)
```

## Key files

| File | Purpose |
|---|---|
| `mcp_server/server.py` | All MCP tools |
| `mcp_server/browser.py` | Playwright singleton, multi-tab, CDP-first |
| `mcp_server/paper_tv.py` | TV Paper Trading DOM executor |
| `mcp_server/rules_engine.py` | Rule system: conditions, validation, state, history |
| `systems/*.json` | Rule system files (signals + actions) |
| `rules.json` | Persistent layout — survives restarts |
| `.mcp.json` | Registers MCP server in Claude Code |
| `.claude/settings.json` | `enableAllProjectMcpServers: true` |

## Chrome profile

Persistent at `~/.crackincorrelation/chrome-profile`  
CDP port: **9223** (9222 reserved by agent-browser)  
SingletonLock auto-cleared on fresh launch.  
**Never call `browser.close()` from standalone scripts** — MCP server owns the context.

## How panes work

- Each symbol = separate browser tab (truly unlimited, plan-agnostic)
- Navigation: `?symbol=NASDAQ:AAPL&interval=D` — no UI clicking
- Price: `<title>` tag — `SYMBOL PRICE ▲/▼ CHANGE%`
- OHLCV: DOM header row (top 120px leaf text nodes)

## MCP tools

### Layout + price
| Tool | Args | What it does |
|---|---|---|
| `apply_layout` | `[{symbol, tf}]` | Open N tabs, save rules |
| `restore_layout` | — | Re-apply rules.json |
| `set_symbol` | `symbol, tf, pane=0` | Set one pane |
| `get_price` | `pane=0` | Live price + OHLCV |
| `get_all_prices` | — | All panes |
| `screenshot` | `pane=0, label` | Base64 PNG |
| `screenshot_all` | — | All panes |
| `add_indicator` | `name, pane=0` | RSI, MACD, EMA etc |
| `list_panes` | — | Tab count + layout |
| `get_status` | — | Health check |

### Rule systems
| Tool | Args | What it does |
|---|---|---|
| `load_systems` | `names?` | Load + open tabs (deduped). Omit = all. |
| `evaluate_rules` | `system_name?` | Live prices → eval → save state |
| `get_full_picture` | — | All rules + stats |
| `get_rule_state` | `system, rule_id` | One rule full history |
| `list_systems` | — | Disk vs loaded |
| `reload_systems` | — | Hot-reload from disk |

### Watch loop
| Tool | Args | What it does |
|---|---|---|
| `start_watch` | `interval=30, auto_trade=False` | Eval loop. `auto_trade=True` → fires paper orders |
| `stop_watch` | — | Stop loop |
| `watch_status` | — | Running + interval |

### Paper trading (TV-native DOM)
| Tool | Args | What it does |
|---|---|---|
| `paper_buy` | `symbol, qty=1, pane=0` | Buy via TV paper panel |
| `paper_sell` | `symbol, qty=1, pane=0` | Sell via TV paper panel |
| `get_positions` | `pane=0` | Open positions |
| `get_account` | `pane=0` | Equity, P&L, margin |
| `probe_paper_dom` | `pane=0` | Rediscover selectors after TV update |

## Turnkey startup

```
restore_layout()
get_all_prices()
load_systems(["paper_momentum"])
start_watch(30, auto_trade=True)   ← infinite loop
```

## paper_tv.py selectors (confirmed 2026-04-30)

```python
SEL_BUY_SIDE  = "[data-name='side-control-buy']"
SEL_SELL_SIDE = "[data-name='side-control-sell']"
SEL_QTY       = "input#quantity-field"          # id= not name=
SEL_PLACE     = "[data-name='place-and-modify-button']"
SEL_POSITIONS = "[data-name='Paper.positions-table']"
SEL_BUY_BTN   = "[data-name='buy-order-button']"
```

**Nuances:**
- `dispatch_event('click')` bypasses overlay interception on buy/sell buttons
- React inputs: use `HTMLInputElement.prototype.value` native setter + dispatch `input`/`change` events
- Paper panel connects via: open broker dialog → click Paper Trading span → click Connect button (empty text)
- Selector args passed as JS array — never interpolate CSS selectors with single quotes into JS strings
- `_ensure_paper_connected` checks for `place-and-modify-button` existence

## Rule system schema (v2)

```json
{
  "version": 2,
  "name": "paper_momentum",
  "panes": [{"symbol": "NASDAQ:AAPL", "tf": "1D"}],
  "rules": [{
    "id": "paper_aapl_long",
    "pane_symbol": "NASDAQ:AAPL",
    "pane_tf": "1D",
    "conditions": {"logic": "AND", "items": [
      {"field": "close", "op": ">", "field2": "open"},
      {"field": "close", "op": ">", "value": 270}
    ]},
    "actions": ["log", "buy:1"],
    "cooldown_evals": 3
  }]
}
```

Actions: `log` | `alert` | `screenshot` | `buy:N` | `sell:N`  
Fields: `open` `high` `low` `close` `price` `range` `body` `wick_upper` `wick_lower` `change_pct`  
Ops: `>` `<` `>=` `<=` `==` `!=` `crosses_above` `crosses_below`

## Timeframe codes

`1m 3m 5m 15m 30m 1h 2h 4h 1D 1W 1M`

## Zero liability

- Personal session only (own Chrome profile)
- No data extraction or redistribution
- No TradingView API scraping
- URL navigation = standard browser behavior
