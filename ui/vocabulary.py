"""
Derives the option-lists ("vocabulary") that drive the editor's dropdowns.

Everything here is computed from the *current* config documents (so editing the
General tab immediately changes what the Display/Conditions tabs offer) plus a
few option sets sourced from the engine's own registries -- imported lazily so
this module, and the editor, stay light and don't drag in pandas_ta/kiteconnect
unless those options are actually requested.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Derived from the strategy document
# ---------------------------------------------------------------------------


def _instruments(strategy_doc: Any):
    for exchange in strategy_doc.get("general", {}).get("exchanges", []):
        for instr in exchange.get("instruments", []):
            yield instr


def instrument_ids(strategy_doc: Any) -> list[str]:
    """All instrument ``id``s defined under general.exchanges[].instruments[]."""
    return [str(i.get("id")) for i in _instruments(strategy_doc) if i.get("id") is not None]


def timeframe_types(strategy_doc: Any) -> list[str]:
    """All timeframe-type keys used by any instrument (e.g. ``intraday``)."""
    seen: dict[str, None] = {}
    for instr in _instruments(strategy_doc):
        for k in instr.get("timeframes", {}):
            seen.setdefault(str(k), None)
    return list(seen)


def _format_tf(minutes: int) -> str:
    return f"{minutes}min"


def timeframes_for(strategy_doc: Any, instrument_id: str | None) -> list[str]:
    """Formatted timeframes (``"1min"`` ...) available for one instrument."""
    out: list[str] = []
    for instr in _instruments(strategy_doc):
        if instrument_id is not None and str(instr.get("id")) != str(instrument_id):
            continue
        for mins in _flatten_timeframes(instr):
            tf = _format_tf(mins)
            if tf not in out:
                out.append(tf)
    return out


def all_timeframes(strategy_doc: Any) -> list[str]:
    """Formatted timeframes available across every instrument (de-duplicated)."""
    out: list[str] = []
    for instr in _instruments(strategy_doc):
        for mins in _flatten_timeframes(instr):
            tf = _format_tf(mins)
            if tf not in out:
                out.append(tf)
    return out


def _flatten_timeframes(instr: Any) -> list[int]:
    out: list[int] = []
    for entries in instr.get("timeframes", {}).values():
        for entry in entries:
            out.append(int(entry["minutes"]) if isinstance(entry, dict) else int(entry))
    return out


# Raw candle columns present on every timeframe's dataframe regardless of the
# ta: indicator list (pandas_ta's rename only touches columns it added).
BASE_CANDLE_COLUMNS = ["open", "high", "low", "close", "time_of_day", "session_atr"]


def alias_names(strategy_doc: Any) -> list[str]:
    """All indicator aliases plus the raw OHLC candle columns."""
    out: list[str] = []
    for ind in strategy_doc.get("ta", []):
        alias = ind.get("alias")
        names = alias if isinstance(alias, list) else [alias]
        for n in names:
            if n is not None and str(n) not in out:
                out.append(str(n))
    for col in BASE_CANDLE_COLUMNS:
        if col not in out:
            out.append(col)
    return out


def definition_ids(strategy_doc: Any) -> list[str]:
    """Ids of the shared conditions under the top-level ``definitions`` list."""
    return [
        str(d.get("id"))
        for d in (strategy_doc.get("definitions") or [])
        if d.get("id") is not None
    ]


# ---------------------------------------------------------------------------
# Sourced from the engine's registries (imported lazily, with safe fallbacks)
# ---------------------------------------------------------------------------


def expiry_rules() -> list[str]:
    try:
        from data_retrieval import EXPIRY_RULE_REGISTRY

        return list(EXPIRY_RULE_REGISTRY.keys())
    except Exception:
        return ["near_month", "next_month"]


# Fallback arg specs for the built-in condition types, used only if the engine's
# ``condition`` module can't be imported. Mirrors condition.CONDITION_REGISTRY.
_FALLBACK_ARG_SPECS: dict[str, list[dict]] = {
    "and": [{"name": "args", "kind": "children", "min": 1, "max": None}],
    "or": [{"name": "args", "kind": "children", "min": 1, "max": None}],
    "not": [{"name": "args", "kind": "children", "min": 1, "max": 1}],
    "normalized_spread": [{"name": "a", "kind": "operand"}, {"name": "b", "kind": "operand"},
                          {"name": "normalizer", "kind": "operand"}],
    "above": [{"name": "a", "kind": "operand"}, {"name": "b", "kind": "operand"},
              {"name": "normalizer", "kind": "operand"}],
    "below": [{"name": "a", "kind": "operand"}, {"name": "b", "kind": "operand"},
              {"name": "normalizer", "kind": "operand"}],
    "increasing": [{"name": "col", "kind": "reference"}, {"name": "normalizer", "kind": "operand"},
                   {"name": "lookback", "kind": "int"}],
    "decreasing": [{"name": "col", "kind": "reference"}, {"name": "normalizer", "kind": "operand"},
                   {"name": "lookback", "kind": "int"}],
}


def condition_arg_specs() -> dict[str, list[dict]]:
    """
    Map each condition type to its argument descriptors, sourced from the engine's
    ``CONDITION_REGISTRY`` so the editor stays in sync with what the engine builds.
    Falls back to the built-in copy if ``condition`` can't be imported.
    """
    try:
        from condition import CONDITION_REGISTRY

        return {
            name: [
                {"name": a.name, "kind": a.kind, "min": a.min_children, "max": a.max_children,
                 "options": list(a.options)}
                for a in spec.args
            ]
            for name, spec in CONDITION_REGISTRY.items()
        }
    except Exception:
        return {k: list(v) for k, v in _FALLBACK_ARG_SPECS.items()}


def condition_types() -> list[str]:
    """Every registered condition type (combinators + leaves)."""
    return list(condition_arg_specs().keys())


# ---------------------------------------------------------------------------
# Static option sets
# ---------------------------------------------------------------------------

# Common pandas resample aggregations meaningful for signal scores.
SIGNAL_AGGREGATE_OPTIONS = ["min", "max", "last", "first", "mean", "sum", "std"]

# Curated matplotlib colours for panel styling (free text also allowed in the UI).
COLOR_OPTIONS = [
    "black", "gray", "magenta", "blue", "orange", "green",
    "red", "teal", "purple", "brown", "salmon", "darkcyan",
    "silver",
]

# Common pandas_ta indicator kinds (free text also allowed in the UI).
INDICATOR_KIND_OPTIONS = [
    "ema", "sma", "wma", "vwap", "macd", "rsi", "atr",
    "bbands", "stoch", "adx", "obv", "cci",
]

INSTRUMENT_TYPE_OPTIONS = ["FUT", "CE", "PE", "EQ"]
