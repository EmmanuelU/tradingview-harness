"""
Rules engine — load systems, evaluate conditions, track full state history.

System file schema (systems/*.json):
{
  "version": 1,
  "name": "string",
  "description": "string",
  "panes": [{"symbol": "NASDAQ:AAPL", "tf": "1D"}, ...],
  "rules": [
    {
      "id": "unique_id",
      "name": "Human label",
      "pane_symbol": "NASDAQ:AAPL",   // which pane this rule watches
      "pane_tf": "1D",
      "condition": {
        // Simple field compare:
        "field": "close",             // open|high|low|close|price|change_pct
        "op": ">",                    // > < >= <= == != crosses_above crosses_below
        "value": 280.0                // scalar
        // OR field vs field:
        "field2": "open"
      },
      "actions": ["log", "alert"]    // log|alert|screenshot
    }
  ]
}

State (written back into system file under each rule):
  "state": {
    "triggered": bool,
    "last_value": float|null,
    "prev_value": float|null,
    "trigger_count": int,
    "last_triggered": iso8601|null,
    "last_checked": iso8601|null,
    "history": [{"ts":..., "value":..., "triggered":bool}, ...]  // last 500
  }
"""

import json
import operator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SYSTEMS_DIR = Path(__file__).parent.parent / "systems"
HISTORY_LIMIT = 500

OPS = {
    ">":  operator.gt,
    "<":  operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


# ── system I/O ─────────────────────────────────────────────────────────────────

def list_system_files() -> list[Path]:
    return sorted(SYSTEMS_DIR.glob("*.json"))


def load_system(name: str) -> dict:
    path = SYSTEMS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"system not found: {path}")
    data = json.loads(path.read_text())
    data.setdefault("version", 1)
    data.setdefault("panes", [])
    data.setdefault("rules", [])
    data["_path"] = str(path)
    for rule in data["rules"]:
        rule.setdefault("actions", ["log"])
        s = rule.setdefault("state", {})
        s.setdefault("triggered", False)
        s.setdefault("last_value", None)
        s.setdefault("prev_value", None)
        s.setdefault("trigger_count", 0)
        s.setdefault("last_triggered", None)
        s.setdefault("last_checked", None)
        s.setdefault("history", [])
    return data


def save_system(data: dict):
    path = Path(data["_path"])
    out = {k: v for k, v in data.items() if not k.startswith("_")}
    path.write_text(json.dumps(out, indent=2))


def load_all_systems() -> list[dict]:
    systems = []
    for f in list_system_files():
        try:
            systems.append(load_system(f.stem))
        except Exception as e:
            print(f"warn: skip {f.name}: {e}")
    return systems


# ── pane registry ──────────────────────────────────────────────────────────────

def build_pane_registry(systems: list[dict]) -> list[dict]:
    """
    Deduplicate panes across all systems.
    Returns ordered list[{symbol, tf}] — index = browser tab index.
    Each system rule gets pane_index resolved.
    """
    seen: dict[tuple, int] = {}
    panes: list[dict] = []

    for sys in systems:
        for pane in sys.get("panes", []):
            key = (pane["symbol"], pane["tf"])
            if key not in seen:
                seen[key] = len(panes)
                panes.append({"symbol": pane["symbol"], "tf": pane["tf"]})

        for rule in sys.get("rules", []):
            key = (rule.get("pane_symbol", ""), rule.get("pane_tf", "1D"))
            rule["_pane_index"] = seen.get(key, 0)

    return panes


# ── condition evaluation ───────────────────────────────────────────────────────

def _get_field(price_data: dict, field: str) -> float | None:
    mapping = {
        "open":  "open",  "o": "open",
        "high":  "high",  "h": "high",
        "low":   "low",   "l": "low",
        "close": "close", "c": "close",
        "price": "close",
        "change_pct": "_change_pct",
    }
    key = mapping.get(field.lower())
    if not key:
        return None
    val = price_data.get(key)
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return None


def evaluate_condition(cond: dict, price_data: dict, prev_price: dict | None) -> bool | None:
    """
    Returns True/False/None (None = data missing, skip).
    Supports: field op value, field op field2, crosses_above, crosses_below.
    Unknown fields → None (forwards compatible).
    """
    field = cond.get("field")
    op    = cond.get("op")
    if not field or not op:
        return None

    curr_val = _get_field(price_data, field)
    if curr_val is None:
        return None

    if op in ("crosses_above", "crosses_below"):
        if prev_price is None:
            return None
        prev_val = _get_field(prev_price, field)
        if prev_val is None:
            return None
        threshold = (
            _get_field(price_data, cond["field2"])
            if "field2" in cond
            else float(cond.get("value", 0))
        )
        if op == "crosses_above":
            return prev_val <= threshold < curr_val
        else:
            return prev_val >= threshold > curr_val

    if "field2" in cond:
        rhs = _get_field(price_data, cond["field2"])
    elif "value" in cond:
        try:
            rhs = float(cond["value"])
        except (ValueError, TypeError):
            return None
    else:
        return None

    if rhs is None:
        return None

    fn = OPS.get(op)
    if fn is None:
        return None  # unknown op → skip, forwards compat
    return fn(curr_val, rhs)


# ── evaluation loop ────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_system(system: dict, prices_by_pane: dict[int, dict]) -> list[dict]:
    """
    Evaluate all rules in one system against current price data.
    Mutates rule state in-place. Returns list of results.
    """
    results = []
    for rule in system.get("rules", []):
        pane_idx = rule.get("_pane_index", 0)
        price    = prices_by_pane.get(pane_idx)
        if not price:
            results.append({"rule_id": rule["id"], "skip": "no price data"})
            continue

        cond    = rule.get("condition", {})
        state   = rule["state"]
        prev    = {"close": state["prev_value"]} if state["prev_value"] is not None else None
        triggered = evaluate_condition(cond, price, prev)

        # Update state
        curr_val = _get_field(price, cond.get("field", "close"))
        state["prev_value"]  = state["last_value"]
        state["last_value"]  = curr_val
        state["last_checked"] = _now()

        if triggered is None:
            results.append({"rule_id": rule["id"], "skip": "insufficient data"})
            continue

        state["triggered"] = triggered
        if triggered:
            state["trigger_count"] += 1
            state["last_triggered"] = _now()

        # Append to history, cap at HISTORY_LIMIT
        state["history"].append({
            "ts": _now(), "value": curr_val, "triggered": triggered
        })
        if len(state["history"]) > HISTORY_LIMIT:
            state["history"] = state["history"][-HISTORY_LIMIT:]

        results.append({
            "rule_id":   rule["id"],
            "name":      rule.get("name", rule["id"]),
            "pane":      pane_idx,
            "symbol":    rule.get("pane_symbol"),
            "triggered": triggered,
            "value":     curr_val,
            "condition": f"{cond.get('field')} {cond.get('op')} {cond.get('value', cond.get('field2'))}",
            "count":     state["trigger_count"],
        })

    return results


def get_rule_history(system: dict, rule_id: str) -> dict:
    for rule in system.get("rules", []):
        if rule["id"] == rule_id:
            return {"rule_id": rule_id, "name": rule.get("name"), **rule["state"]}
    return {"error": f"rule {rule_id!r} not found"}


def full_picture(systems: list[dict]) -> dict:
    """Full state snapshot across all loaded systems."""
    out = {}
    for sys in systems:
        sys_rules = []
        for rule in sys.get("rules", []):
            s = rule["state"]
            sys_rules.append({
                "id":            rule["id"],
                "name":          rule.get("name"),
                "triggered":     s["triggered"],
                "trigger_count": s["trigger_count"],
                "last_value":    s["last_value"],
                "last_triggered":s["last_triggered"],
                "last_checked":  s["last_checked"],
                "history_len":   len(s["history"]),
            })
        out[sys["name"]] = {
            "description": sys.get("description", ""),
            "rules": sys_rules,
        }
    return out
