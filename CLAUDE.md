# TradingView MCP — crackincorrelationprofiler

Bot controls tradingview.com via Playwright. Human and bot share the same live charts. Paper orders execute directly through TV's browser UI.

## Architecture

```
MCP server (FastMCP, stdio)
  └── Playwright → Chrome (persistent profile, CDP port 9223)
        └── N browser tabs = N chart panes (~15-20 symbol ceiling, TV perf)
              └── tradingview.com/chart/?symbol=X&interval=Y
                    └── TV Paper Trading panel (DOM-native orders, ~1370ms/order)
```

## Key files

| File | Purpose |
|---|---|
| `mcp_server/server.py` | All MCP tools |
| `mcp_server/browser.py` | Playwright singleton, CDP-first, crash recovery |
| `mcp_server/paper_tv.py` | TV Paper Trading DOM executor |
| `mcp_server/rules_engine.py` | Rule engine: conditions, cooldown, state, history |
| `mcp_server/tv_utils.py` | Shared: navigate, parse_title, read_ohlcv, dismiss_popups |
| `backtest.py` | Offline replay of price_log.jsonl through any rule system |
| `systems/*.json` | Rule system files (signals + actions) |
| `rules.json` | Persistent pane layout — survives restarts |
| `.mcp.json` | Registers MCP server in Claude Code |
| `.claude/settings.json` | `enableAllProjectMcpServers: true` |

## Runtime data files (gitignored — personal)

| File | What's in it |
|---|---|
| `price_log.jsonl` | Every DOM price tick (symbol, tf, OHLCV, ts) — never trimmed |
| `sim_log.jsonl` | Every paper order (side, qty, fill_price, rule_id, ok/fail) |
| `pnl_log.jsonl` | Realized P&L per closed trade (FIFO entry/exit match) |
| `event_log.jsonl` | System events: watch start/stop, errors, crash recovery, login |
| `rule_scores.json` | Aggregate win_rate, avg_pnl, total_pnl per entry rule |

## Observability data flow

```
DOM read (every interval)
    → price_log.jsonl           (raw ticks, foundation for backtest)
    → evaluate_system()
    → triggered rule
    → paper_tv.place_order()    (~1370ms, LIMIT at current price)
    → sim_log.jsonl             (fill_price parsed from button_text)
    → on sell: pnl_log.jsonl    (FIFO matched, P&L computed)
    → rule_scores.json          (additive win_rate / avg_pnl per rule)

event_log.jsonl                 (crashes, recovery, login expiry, watch start/stop)
```

## Chrome profile

Persistent at `~/.crackincorrelation/chrome-profile`
CDP port: **9223** (9222 reserved by agent-browser — never conflict)
SingletonLock auto-cleared on fresh launch.
**Never call `browser.close()` from standalone scripts** — MCP server owns the context.

## How panes work

- Each symbol = separate browser tab
- Navigation: `?symbol=NASDAQ:AAPL&interval=D` — no UI clicking
- Price: `<title>` tag — `SYMBOL PRICE ▲/▼ CHANGE%`
- OHLCV: DOM header row (top 120px leaf text nodes)
- Ceiling: ~15-20 tabs before TV degrades. `MAX_TABS=20` hard limit.

## MCP tools

### Layout + price
| Tool | Args | What it does |
|---|---|---|
| `apply_layout` | `[{symbol, tf}]` | Open N tabs, save rules.json |
| `restore_layout` | — | Re-apply rules.json, trim orphan tabs |
| `set_symbol` | `symbol, tf, pane=0` | Set one pane |
| `get_price` | `pane=0` | Live price + OHLCV |
| `get_all_prices` | — | All panes |
| `screenshot` | `pane=0, label` | Base64 PNG |
| `screenshot_all` | — | All panes |
| `add_indicator` | `name, pane=0` | RSI, MACD, EMA etc |
| `list_panes` | — | Tab count + layout |
| `get_status` | — | Tab count + saved layout |
| `health_check` | — | Chrome, login, tabs, watch, systems |
| `dismiss_popups` | `pane=0` | Dismiss TV announcement/modal dialogs |

### Rule systems
| Tool | Args | What it does |
|---|---|---|
| `load_systems` | `names?` | Load + open tabs (deduped). Omit = all. |
| `evaluate_rules` | `system_name?` | Live prices → eval → save state + price_log |
| `get_full_picture` | — | All rules + stats |
| `get_rule_state` | `system, rule_id` | One rule full history |
| `list_systems` | — | Disk vs loaded |
| `reload_systems` | — | Hot-reload from disk |

### Watch loop
| Tool | Args | What it does |
|---|---|---|
| `start_watch` | `interval=30, auto_trade=False` | Eval loop. `auto_trade=True` → fires paper orders |
| `stop_watch` | — | Stop loop |
| `watch_status` | — | Running, interval, sim_positions |

### Paper trading (TV-native DOM)
| Tool | Args | What it does |
|---|---|---|
| `paper_buy` | `symbol, qty=1, pane=0` | Buy via TV paper panel |
| `paper_sell` | `symbol, qty=1, pane=0` | Sell via TV paper panel |
| `get_positions` | `pane=0` | Positions (requires Paper Trading tab active) |
| `get_account` | `pane=0` | Equity, P&L, margin |
| `probe_paper_dom` | `pane=0` | Rediscover selectors after TV update |

### Observability + logs
| Tool | Args | What it does |
|---|---|---|
| `get_sim_log` | `last_n=20` | Trade history from sim_log.jsonl |
| `clear_sim_log` | — | Wipe sim_log before fresh run |
| `get_price_log` | `last_n=20, symbol?` | Price tick history |
| `get_pnl_log` | `last_n=20` | Realized P&L per closed trade |
| `get_rule_scores` | — | Win rate + avg P&L per rule |
| `get_event_log` | `last_n=30` | System events log |

## Turnkey startup

```
restore_layout()
load_systems(["paper_momentum"])
start_watch(60, auto_trade=True)   ← infinite loop, reconciles positions from sim_log
```

## Crash recovery (automatic)

3 consecutive errors → `recover()` → relaunch Chrome → `restore_layout()` → `reload_systems()`
Logged to `event_log.jsonl`. Login expiry detected via URL check — orders skipped + logged.

## paper_tv.py selectors (confirmed 2026-04-30, data-name = build-stable)

```python
SEL_BUY_SIDE  = "[data-name='side-control-buy']"
SEL_SELL_SIDE = "[data-name='side-control-sell']"
SEL_QTY       = "input#quantity-field"          # id= NOT name= — critical
SEL_PLACE     = "[data-name='place-and-modify-button']"
SEL_POSITIONS = "[data-name='Paper.positions-table']"   # only visible on Paper Trading tab
SEL_BUY_BTN   = "[data-name='buy-order-button']"
```

**Execution nuances (hard-won):**
- `dispatch_event('click')` bypasses overlay interception — never use `locator.click()` on buy/sell
- React inputs: `HTMLInputElement.prototype.value` native setter + dispatch `input`/`change` events
- Pass selectors as JS array arg — never interpolate CSS selectors with single quotes into f-string JS
- `_ensure_paper_connected` calls `dismiss_popups` first, then checks for `place-and-modify-button`
- Orders are **LIMIT at current price** — fills immediately when market is open
- 3000 orders warning = cosmetic only, orders still execute
- `Paper.positions-table` only in DOM when bottom panel is on Paper Trading tab
  → use `_sim_positions` dict (in-memory, reconciled from sim_log) as source of truth

**Position tracking:**
- `_sim_positions: dict[str, float]` — in-memory, rebuilt from sim_log on `start_watch`
- `_sim_entries: dict[str, list]` — FIFO open entries for P&L matching, also rebuilt from sim_log
- Cooldown bug fixed: `rule.update()` returns post-cooldown triggered value (pre-fix: cooldowns were ignored)

## Paper trading execution benchmarks (2026-04-30, market closed)

| Operation | Latency |
|---|---|
| Price read (per pane) | ~283ms |
| Paper order (buy or sell) | ~1370ms |
| Popup dismissal | ~200ms |
| Full cycle (2 panes, eval + orders) | ~3-4s |

At 1D/4h/1h timeframes: all latencies irrelevant.

## Backtest

```bash
python3 backtest.py paper_momentum
python3 backtest.py paper_momentum --symbol NASDAQ:AAPL
python3 backtest.py paper_momentum --from 2026-04-30T06:00:00
```

Replays `price_log.jsonl` offline — zero browser, zero TV dependency. Requires price_log data collected from live watch runs.

## Data shapes (exact — no guessing)

### Price dict (DOM read, all values are STRINGS)
```python
{"pane": int, "symbol": str,   # short "AAPL" not "NASDAQ:AAPL"
 "price": str, "direction": str, "change": str,
 "open": str, "high": str, "low": str, "close": str}
```
Convert to float: `float(str(v).replace(",",""))`

### Rule trigger result
```python
{"rule_id": str, "name": str, "pane": int,
 "symbol": str,      # short — resolve via _active_panes[pane]["symbol"]
 "triggered": bool, "value": float, "condition": str,
 "count": int, "actions": list[str]}   # ["log", "buy:1", "sell:0.001"]
```

### sim_log entry
```python
{"ts": str, "rule_id": str, "symbol": str, "side": str, "qty": float,
 "signal_value": float, "fill_price": float,   # parsed from button_text
 "ok": bool, "button_text": str, "error": str}
```

### pnl_log entry
```python
{"ts": str, "symbol": str, "qty": float,
 "entry_price": float, "exit_price": float,
 "pnl": float, "pnl_pct": float,
 "entry_rule": str, "exit_rule": str, "entry_ts": str}
```

### OrderRequest / OrderResult (designed, not yet built — needed before live brokers)
```python
@dataclass
class OrderRequest:
    symbol: str       # TV full "NASDAQ:AAPL"
    side: str         # "buy" | "sell"
    qty: float
    order_type: str   # "market" | "limit"
    price: float      # rule last_value; None for market
    rule_id: str
    pane: int

@dataclass
class OrderResult:
    ok: bool
    order_id: str     # None for paper_tv
    fill_price: float
    filled_qty: float
    status: str       # "filled"|"pending"|"rejected"|"error"
    broker: str       # "paper_tv"|"alpaca"|"binance"|"oanda"
    timestamp: str
    raw: dict
```

## Broker architecture (designed, not yet built)

```
Signal (done) → Risk layer (missing) → Executor abstraction (missing)
                                            ├── paper_tv.py       ← done
                                            ├── broker_alpaca.py  ← stocks
                                            ├── broker_binance.py ← crypto
                                            └── broker_oanda.py   ← forex
```
Mode: `TRADE_MODE=paper|live` env var
Interface: `executor.execute(OrderRequest) → OrderResult`
Rules unchanged between paper and live — only executor swaps.

## New broker probe protocol

1. Connect broker in TV Trade panel (manual once)
2. `probe_paper_dom(pane=0)` → capture all `data-name` elements
3. `dispatch_event('click')` on buy button → open order form
4. Re-probe: map inputs, buttons to OrderRequest fields
5. Verify React native setter pattern (usually required)
6. Add `broker_X.py` with same `place_order(symbol, side, qty, pane)` interface
7. Confirm selectors are `data-name`/`id` — reject class-hash selectors

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
Logic: `AND` `OR` `NOT`
Cooldown: `cooldown_evals: N` — skip N evals after trigger. Post-cooldown value returned by `update()`.

## What's NOT built (honest)

- `executor.py` abstraction + OrderRequest/OrderResult dataclasses
- Risk layer (max drawdown, daily loss kill switch)
- Live broker wrappers (alpaca, binance, oanda)
- Rule mutation from rule_scores (read scores → adjust thresholds → save → reload)
- 50+ symbol parallel testing (browser tab ceiling ~15-20; needs external price feed)

## Timeframe codes

`1m 3m 5m 15m 30m 1h 2h 4h 1D 1W 1M`

## Zero liability

- Personal session only (own Chrome profile)
- No data extraction or redistribution
- No TradingView API scraping
- URL navigation = standard browser behavior
