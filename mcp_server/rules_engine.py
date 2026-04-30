"""
Rules engine v2 — Rule wrapper, multi-condition, derived fields, cooldown,
validation at load, Python rules, version migration, infinite compatibility.

System file schema (systems/*.json) — v2:
{
  "version": 2,
  "name": "string",
  "description": "string",
  "panes": [{"symbol": "NASDAQ:AAPL", "tf": "1D"}, ...],
  "rules": [
    {
      "id":          "unique_snake_id",       // required
      "name":        "Human label",           // optional, defaults to id
      "pane_symbol": "NASDAQ:AAPL",           // required
      "pane_tf":     "1D",                    // required

      // Single condition (simple):
      "condition": {"field": "close", "op": ">", "value": 280},

      // OR multi-condition (AND / OR / NOT):
      "conditions": {
        "logic": "AND",                       // AND | OR | NOT
        "items": [
          {"field": "close", "op": ">", "value": 270},
          {"field": "close", "op": ">", "field2": "open"}
        ]
      },

      "actions":       ["log", "alert"],      // log | alert | screenshot
      "cooldown_evals": 0,                    // skip N evals after trigger (0 = off)

      // Python rule override (inline lambda string or external fn ref):
      // "python": "lambda p, prev: p['close'] > p['open'] * 1.02"
    }
  ]
}

Derived fields (usable in any condition.field):
  range        = high - low
  body         = abs(close - open)
  wick_upper   = high - max(close, open)
  wick_lower   = min(close, open) - low
  change_pct   = parsed from title (▲/▼)

Operators: > < >= <= == != crosses_above crosses_below
Logic:     AND OR NOT

Version migration: v1 → v2 automatic (condition → conditions wrapper).
Unknown fields: ignored (forwards compat).
Unknown ops/fields: ValidationError at load, not silent skip.
"""

import json
import operator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SYSTEMS_DIR    = Path(__file__).parent.parent / "systems"
HISTORY_LIMIT  = 500
CURRENT_VERSION = 2

# ── constants ──────────────────────────────────────────────────────────────────

VALID_OPS = frozenset({">", "<", ">=", "<=", "==", "!=", "crosses_above", "crosses_below"})
VALID_LOGICS = frozenset({"AND", "OR", "NOT"})
VALID_ACTIONS = frozenset({"log", "alert", "screenshot"})
DERIVED_FIELDS = frozenset({"range", "body", "wick_upper", "wick_lower"})
RAW_FIELDS     = frozenset({"open", "high", "low", "close", "price", "change_pct"})
ALL_FIELDS     = RAW_FIELDS | DERIVED_FIELDS

SCALAR_OPS = frozenset({">", "<", ">=", "<=", "==", "!="})
CROSS_OPS  = frozenset({"crosses_above", "crosses_below"})

OP_FNS: dict[str, Any] = {
    ">":  operator.gt,
    "<":  operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


# ── validation ─────────────────────────────────────────────────────────────────

class ValidationError(Exception):
    pass


def _validate_condition(cond: dict, path: str = "condition"):
    field  = cond.get("field")
    op     = cond.get("op")
    if not field:
        raise ValidationError(f"{path}: missing 'field'")
    if field not in ALL_FIELDS:
        raise ValidationError(f"{path}: unknown field {field!r} — valid: {sorted(ALL_FIELDS)}")
    if not op:
        raise ValidationError(f"{path}: missing 'op'")
    if op not in VALID_OPS:
        raise ValidationError(f"{path}: unknown op {op!r} — valid: {sorted(VALID_OPS)}")
    if "value" not in cond and "field2" not in cond:
        raise ValidationError(f"{path}: need 'value' or 'field2'")
    if "field2" in cond and cond["field2"] not in ALL_FIELDS:
        raise ValidationError(f"{path}: unknown field2 {cond['field2']!r}")
    if "inverse" in cond and not isinstance(cond["inverse"], bool):
        raise ValidationError(f"{path}: 'inverse' must be bool")


def _validate_conditions(conditions: dict, path: str = "conditions"):
    logic = conditions.get("logic")
    if logic not in VALID_LOGICS:
        raise ValidationError(f"{path}: logic must be AND|OR|NOT, got {logic!r}")
    items = conditions.get("items", [])
    if logic == "NOT" and len(items) != 1:
        raise ValidationError(f"{path}: NOT requires exactly 1 item")
    if logic in ("AND", "OR") and len(items) < 2:
        raise ValidationError(f"{path}: {logic} requires ≥2 items")
    for i, item in enumerate(items):
        _validate_condition(item, f"{path}.items[{i}]")


def _validate_rule(rule: dict) -> list[str]:
    errors = []
    for req in ("id", "pane_symbol", "pane_tf"):
        if req not in rule:
            errors.append(f"missing required field: {req!r}")
    if errors:
        return errors  # can't continue without required fields

    if "condition" not in rule and "conditions" not in rule and "python" not in rule:
        errors.append("rule needs one of: 'condition', 'conditions', 'python'")

    try:
        if "condition" in rule:
            _validate_condition(rule["condition"])
        if "conditions" in rule:
            _validate_conditions(rule["conditions"])
    except ValidationError as e:
        errors.append(str(e))

    for action in rule.get("actions", []):
        if action not in VALID_ACTIONS:
            errors.append(f"unknown action {action!r} — valid: {sorted(VALID_ACTIONS)}")

    cooldown = rule.get("cooldown_evals", 0)
    if not isinstance(cooldown, int) or cooldown < 0:
        errors.append("cooldown_evals must be non-negative int")

    return errors


# ── version migration ──────────────────────────────────────────────────────────

def _migrate_v1_to_v2(data: dict) -> dict:
    """v1 had flat 'condition'. v2 keeps it but also supports 'conditions'."""
    data["version"] = 2
    # v1 rules with single 'condition' are valid v2 — no structural change needed
    return data


MIGRATIONS = {1: _migrate_v1_to_v2}


def _migrate(data: dict) -> dict:
    v = data.get("version", 1)
    while v < CURRENT_VERSION:
        fn = MIGRATIONS.get(v)
        if fn is None:
            break
        data = fn(data)
        v = data.get("version", v + 1)
    return data


# ── Rule wrapper ───────────────────────────────────────────────────────────────

@dataclass
class Rule:
    """
    Universal rule wrapper. Every rule — JSON or Python — becomes a Rule.
    Validates at construction. Evaluates via .check(). Updates own state.
    """
    id:           str
    name:         str
    pane_symbol:  str
    pane_tf:      str
    actions:      list[str]
    cooldown:     int                      # evals to skip after trigger
    _condition:   dict | None = field(default=None, repr=False)
    _conditions:  dict | None = field(default=None, repr=False)
    _python_fn:   Callable | None = field(default=None, repr=False)

    # runtime — not in __init__
    pane_index:   int = field(default=0, init=False)
    _cooldown_remaining: int = field(default=0, init=False)
    state: dict = field(default_factory=lambda: {
        "triggered": False,
        "last_value": None,
        "prev_value": None,
        "last_price": None,   # full OHLCV dict — used as prev on next eval
        "trigger_count": 0,
        "last_triggered": None,
        "last_checked": None,
        "history": [],
    }, init=False)

    # ── derived fields ──────────────────────────────────────────────────────

    @staticmethod
    def _resolve_field(price: dict, fname: str) -> float | None:
        mapping = {
            "open": "open",  "o": "open",
            "high": "high",  "h": "high",
            "low":  "low",   "l": "low",
            "close":"close", "c": "close",
            "price":"close",
        }
        if fname in mapping:
            raw = price.get(mapping[fname])
        elif fname == "range":
            h, l = price.get("high"), price.get("low")
            raw = None if (h is None or l is None) else str(float(str(h).replace(",","")) - float(str(l).replace(",","")))
        elif fname == "body":
            c, o = price.get("close"), price.get("open")
            raw = None if (c is None or o is None) else str(abs(float(str(c).replace(",","")) - float(str(o).replace(",",""))))
        elif fname == "wick_upper":
            h = price.get("high"); c = price.get("close"); o = price.get("open")
            if any(v is None for v in [h, c, o]):
                return None
            raw = str(float(str(h).replace(",","")) - max(float(str(c).replace(",","")), float(str(o).replace(",",""))))
        elif fname == "wick_lower":
            l = price.get("low"); c = price.get("close"); o = price.get("open")
            if any(v is None for v in [l, c, o]):
                return None
            raw = str(min(float(str(c).replace(",","")), float(str(o).replace(",",""))) - float(str(l).replace(",","")))
        elif fname == "change_pct":
            raw = price.get("_change_pct") or price.get("change")
        else:
            return None
        if raw is None:
            return None
        try:
            return float(str(raw).replace(",", "").replace("%", "").replace("−", "-").strip())
        except (ValueError, TypeError):
            return None

    # ── single condition eval ───────────────────────────────────────────────

    def _eval_one(self, cond: dict, price: dict, prev: dict | None) -> bool | None:
        op  = cond["op"]
        lhs = self._resolve_field(price, cond["field"])
        if lhs is None:
            return None

        if op in CROSS_OPS:
            if prev is None:
                return None
            prev_lhs = self._resolve_field(prev, cond["field"])
            if prev_lhs is None:
                return None
            rhs = (self._resolve_field(price, cond["field2"])
                   if "field2" in cond
                   else float(cond.get("value", 0)))
            if rhs is None:
                return None
            if op == "crosses_above":
                result = prev_lhs <= rhs < lhs
            else:
                result = prev_lhs >= rhs > lhs
        else:
            rhs = (self._resolve_field(price, cond["field2"])
                   if "field2" in cond
                   else float(cond.get("value")))
            if rhs is None:
                return None
            fn = OP_FNS.get(op)
            result = fn(lhs, rhs) if fn else None

        if result is not None and cond.get("inverse", False):
            result = not result
        return result

    # ── multi-condition eval ─────────────────────────────────────────────────

    def _eval_multi(self, conditions: dict, price: dict, prev: dict | None) -> bool | None:
        logic = conditions["logic"]
        items = conditions["items"]
        results = [self._eval_one(c, price, prev) for c in items]

        if logic == "NOT":
            r = results[0]
            return None if r is None else not r
        if logic == "AND":
            if any(r is None for r in results):
                return None
            return all(results)
        if logic == "OR":
            if all(r is None for r in results):
                return None
            return any(r for r in results if r is not None)
        return None

    # ── public check ─────────────────────────────────────────────────────────

    def check(self, price: dict, prev: dict | None) -> bool | None:
        """Evaluate rule. Returns True/False/None (None = data missing)."""
        if self._python_fn:
            try:
                return bool(self._python_fn(price, prev))
            except Exception as e:
                print(f"[rules] python rule {self.id!r} error: {e}", flush=True)
                return None
        if self._conditions:
            return self._eval_multi(self._conditions, price, prev)
        if self._condition:
            return self._eval_one(self._condition, price, prev)
        return None

    def primary_value(self, price: dict) -> float | None:
        """The primary field value for display/history."""
        field = None
        if self._condition:
            field = self._condition.get("field", "close")
        elif self._conditions:
            field = self._conditions["items"][0].get("field", "close")
        return self._resolve_field(price, field or "close")

    # ── state update ─────────────────────────────────────────────────────────

    def update(self, triggered: bool | None, price: dict):
        s = self.state
        val = self.primary_value(price)
        now = datetime.now(timezone.utc).isoformat()

        s["prev_value"]  = s["last_value"]
        s["last_value"]  = val
        s["last_price"]  = price   # full OHLCV — used as prev on the NEXT eval
        s["last_checked"] = now

        if triggered is None:
            return  # no state change on missing data

        # cooldown gate
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            triggered = False  # suppress re-trigger during cooldown

        s["triggered"] = triggered
        if triggered:
            s["trigger_count"] += 1
            s["last_triggered"] = now
            self._cooldown_remaining = self.cooldown

        s["history"].append({"ts": now, "value": val, "triggered": triggered})
        if len(s["history"]) > HISTORY_LIMIT:
            s["history"] = s["history"][-HISTORY_LIMIT:]

    def to_dict(self) -> dict:
        """Serialise back to JSON-compatible dict (for saving to system file)."""
        state_out = {k: v for k, v in self.state.items() if k != "last_price"}
        d: dict = {
            "id": self.id, "name": self.name,
            "pane_symbol": self.pane_symbol, "pane_tf": self.pane_tf,
            "actions": self.actions, "cooldown_evals": self.cooldown,
            "_cooldown_remaining": self._cooldown_remaining,
            "state": state_out,
        }
        if self._condition:   d["condition"]  = self._condition
        if self._conditions:  d["conditions"] = self._conditions
        if self._python_fn:   d["python"]     = f"<fn:{self._python_fn.__name__}>"
        return d


# ── Rule factory ───────────────────────────────────────────────────────────────

def rule_from_dict(raw: dict) -> Rule:
    """
    Compile a raw rule dict → Rule wrapper.
    Validates fully. Raises ValidationError with all errors listed.
    """
    errors = _validate_rule(raw)
    if errors:
        rule_id = raw.get("id", "?")
        raise ValidationError(f"rule {rule_id!r} invalid:\n  " + "\n  ".join(errors))

    python_fn = None
    if "python" in raw:
        try:
            python_fn = eval(raw["python"])  # noqa: S307 — user-controlled systems only
        except Exception as e:
            raise ValidationError(f"rule {raw['id']!r}: python eval failed: {e}") from e

    rule = Rule(
        id          = raw["id"],
        name        = raw.get("name", raw["id"]),
        pane_symbol = raw["pane_symbol"],
        pane_tf     = raw["pane_tf"],
        actions     = raw.get("actions", ["log"]),
        cooldown    = raw.get("cooldown_evals", 0),
        _condition  = raw.get("condition"),
        _conditions = raw.get("conditions"),
        _python_fn  = python_fn,
    )
    # Restore persisted state
    if "state" in raw:
        rule.state.update(raw["state"])
    # Restore cooldown counter so active cooldowns survive save/reload
    rule._cooldown_remaining = raw.get("_cooldown_remaining", 0)
    return rule


# ── system I/O ─────────────────────────────────────────────────────────────────

def list_system_files() -> list[Path]:
    return sorted(SYSTEMS_DIR.glob("*.json"))


def load_system(name: str) -> dict:
    """Load + migrate + validate system. Returns dict with compiled Rule objects."""
    path = SYSTEMS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"system not found: {path}")

    data = json.loads(path.read_text())
    data = _migrate(data)
    data.setdefault("panes", [])
    data.setdefault("stats", {
        "eval_count": 0, "total_triggers": 0, "skipped": 0,
        "trigger_rate": 0.0, "last_eval": None,
    })
    data["_path"]  = str(path)
    data["_rules"] = []  # compiled Rule objects

    load_errors = []
    for raw in data.get("rules", []):
        try:
            data["_rules"].append(rule_from_dict(raw))
        except ValidationError as e:
            load_errors.append(str(e))

    if load_errors:
        raise ValidationError(
            f"system {name!r} has {len(load_errors)} invalid rule(s):\n"
            + "\n".join(load_errors)
        )
    return data


def save_system(data: dict):
    path = Path(data["_path"])
    rules_out = [r.to_dict() for r in data.get("_rules", [])]
    out = {k: v for k, v in data.items() if not k.startswith("_")}
    out["rules"] = rules_out
    # stats persisted if present
    if "stats" in data:
        out["stats"] = data["stats"]
    path.write_text(json.dumps(out, indent=2))


def load_all_systems() -> list[dict]:
    systems, errors = [], []
    for f in list_system_files():
        try:
            systems.append(load_system(f.stem))
        except (ValidationError, Exception) as e:
            errors.append(f"  ✗ {f.name}: {e}")
    if errors:
        print("load_all_systems warnings:\n" + "\n".join(errors))
    return systems


# ── pane registry ──────────────────────────────────────────────────────────────

def build_pane_registry(systems: list[dict]) -> list[dict]:
    """Deduplicate panes. Assign pane_index to each compiled Rule."""
    seen: dict[tuple, int] = {}
    panes: list[dict] = []

    for sys in systems:
        for pane in sys.get("panes", []):
            key = (pane["symbol"], pane["tf"])
            if key not in seen:
                seen[key] = len(panes)
                panes.append({"symbol": pane["symbol"], "tf": pane["tf"]})

        for rule in sys.get("_rules", []):
            key = (rule.pane_symbol, rule.pane_tf)
            if key not in seen:
                seen[key] = len(panes)
                panes.append({"symbol": rule.pane_symbol, "tf": rule.pane_tf})
            rule.pane_index = seen[key]

    return panes


# ── evaluation ─────────────────────────────────────────────────────────────────

def _cond_str(rule: "Rule") -> str:
    if rule._condition:
        c = rule._condition
        inv = " [INV]" if c.get("inverse") else ""
        rhs = c.get("value", c.get("field2", "?"))
        return f"{c['field']} {c['op']} {rhs}{inv}"
    if rule._conditions:
        logic = rule._conditions["logic"]
        parts = []
        for c in rule._conditions["items"]:
            inv = " [INV]" if c.get("inverse") else ""
            parts.append(f"{c['field']} {c['op']} {c.get('value', c.get('field2','?'))}{inv}")
        return f"({f' {logic} '.join(parts)})"
    return "<python>"


def _update_system_stats(system: dict, results: list[dict]):
    """Accumulate system-level performance stats. Self-improvement loop data."""
    stats = system.setdefault("stats", {})
    # Forward-compat: add any missing keys (handles old stats blobs on disk)
    stats.setdefault("eval_count", 0)
    stats.setdefault("total_triggers", 0)
    stats.setdefault("skipped", 0)
    stats.setdefault("evaluatable_evals", 0)
    stats.setdefault("trigger_rate", 0.0)
    stats.setdefault("last_eval", None)
    stats["eval_count"] += 1
    fired      = sum(1 for r in results if r.get("triggered") is True)
    skipped    = sum(1 for r in results if "skip" in r)
    evaluatable = len(results) - skipped    # rules that actually ran
    stats["total_triggers"]    += fired
    stats["skipped"]           += skipped
    stats["evaluatable_evals"] += evaluatable
    # rate = triggers / opportunities (excludes skipped)
    stats["trigger_rate"] = round(
        stats["total_triggers"] / max(stats["evaluatable_evals"], 1), 4
    )
    stats["last_eval"] = datetime.now(timezone.utc).isoformat()


def evaluate_system(system: dict, prices_by_pane: dict[int, dict]) -> list[dict]:
    results = []
    for rule in system.get("_rules", []):
        price = prices_by_pane.get(rule.pane_index)
        if not price:
            results.append({"rule_id": rule.id, "skip": "no price data for pane"})
            continue

        prev = rule.state.get("last_price") or (
            {"close": rule.state["prev_value"]} if rule.state.get("prev_value") else None
        )
        triggered = rule.check(price, prev)
        rule.update(triggered, price)

        if triggered is None:
            results.append({"rule_id": rule.id, "skip": "insufficient data"})
            continue

        results.append({
            "rule_id":   rule.id,
            "name":      rule.name,
            "pane":      rule.pane_index,
            "symbol":    rule.pane_symbol,
            "triggered": triggered,
            "value":     rule.state["last_value"],
            "condition": _cond_str(rule),
            "count":     rule.state["trigger_count"],
            "actions":   rule.actions,
        })

    _update_system_stats(system, results)
    return results


def get_rule_history(system: dict, rule_id: str) -> dict:
    for rule in system.get("_rules", []):
        if rule.id == rule_id:
            return {"rule_id": rule_id, "name": rule.name, **rule.state}
    return {"error": f"rule {rule_id!r} not found"}


def full_picture(systems: list[dict]) -> dict:
    out = {}
    for sys in systems:
        out[sys["name"]] = {
            "description": sys.get("description", ""),
            "version": sys.get("version"),
            "stats": sys.get("stats", {}),
            "rules": [{
                "id":            r.id,
                "name":          r.name,
                "triggered":     r.state["triggered"],
                "trigger_count": r.state["trigger_count"],
                "last_value":    r.state["last_value"],
                "last_triggered":r.state["last_triggered"],
                "last_checked":  r.state["last_checked"],
                "history_len":   len(r.state["history"]),
                "cooldown_remaining": r._cooldown_remaining,
                "condition":     _cond_str(r),
            } for r in sys.get("_rules", [])],
        }
    return out
