import sys
import copy
import math
import pandas as pd
import importlib.metadata # pandas-ta-openbb has a bug, requiring this to be imported first 
import pandas_ta as ta
import mplfinance as mpf

from tqdm import tqdm
from visualise import try_addplot
from abc import ABC, abstractmethod
from dataclasses import dataclass
from kiteconnect import KiteConnect
from data_retrieval import bracket_by_day, downsample_days, concat_days, get_instruments

class Condition(ABC):
    """
    Base class. Every Condition evaluates to a float "fulfillment"
    score against a MarketContext snapshot; by convention, >= 1.0 means
    fulfilled, with no fixed upper bound.
    """
    def __init__(self, id: str):
        self.id = id

    @abstractmethod
    def evaluate(self, ctx: "MarketContext") -> float:
        """Compute this condition's fulfillment score at ctx.current_time."""
        raise NotImplementedError

    def __call__(self, ctx: "MarketContext") -> float:
        key = (id(self), ctx.time_offset)
        if key not in ctx.memo:
            # Remember whether evaluating this subtree reached an earlier
            # session, so a memoised hit reports it too instead of looking
            # same-day to the enclosing window.
            outer, ctx.crossed_day = ctx.crossed_day, False
            value = self.evaluate(ctx)
            ctx.memo[key] = (value, ctx.crossed_day)
            ctx.crossed_day = outer or ctx.crossed_day
        result, crossed = ctx.memo[key]
        ctx.crossed_day = ctx.crossed_day or crossed
        ctx.trace[self.id] = result
        return result

    def is_fulfilled(self, ctx: "MarketContext", threshold: float = 0.0) -> bool:
        return self.evaluate(ctx) >= threshold

    def sub_conditions(self) -> list["Condition"]:
        """Direct child Conditions wrapped by this one (empty for leaf conditions)."""
        return []

    def walk(self):
        """Yields this condition and every nested condition, depth-first."""
        yield self
        for child in self.sub_conditions():
            yield from child.walk()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"

class InstrumentNotFoundError(KeyError):
    def __init__(self, instrument_id):
        super().__init__(f"instrument_id {instrument_id!r} not found in instruments_data")
        self.instrument_id = instrument_id

_warned_references: dict = {}

def reset_reference_warnings():
    _warned_references.clear()

def reference_warnings() -> list:
    return sorted((key, timestamp) for key, timestamp in _warned_references.items())

def warn_bad_reference(reason: str, data_src: dict, timestamp):
    key = (reason, data_src["instrument_id"], data_src["timeframe"], data_src["col_name"])
    if key in _warned_references:
        return
    _warned_references[key] = timestamp
    print(f"[warning] {reason}: {key[1]}/{key[2]}/{key[3]} (first seen at {timestamp})")

class DefinitionNotFoundError(KeyError):
    def __init__(self, definition_id):
        super().__init__(f"definition {definition_id!r} not found in definitions")
        self.definition_id = definition_id

class CircularDefinitionError(ValueError):
    def __init__(self, definition_id):
        super().__init__(f"definition {definition_id!r} references itself")
        self.definition_id = definition_id

class ColumnNotFoundError(KeyError):
    def __init__(self, column: str, dataframe: pd.DataFrame):
        self.column = column
        self.dataframe = dataframe
        self.message = f"Column named {column} not found in the following dataframe:\n{dataframe}"
        super().__init__(self.message)

    def __str__(self):
        return self.message

class MarketContext:
    def __init__(self, instruments_data: list[dict], current_time: pd.Timestamp):
        self.instruments_data, self.current_time = instruments_data, current_time
        self.trace: dict[str, float] = {}
        self.memo: dict[tuple, tuple] = {}
        self.time_offset = 0
        # Set by get() when a lookup lands on an earlier session, so a window
        # restricted to same_day can drop that offset.
        self.crossed_day = False

    def get(self, data_src: dict):
        match data_src["type"]:
            case "value":
                return data_src["value"]
            case "reference":
                instrument_id = data_src["instrument_id"]
                timeframe_type = data_src["timeframe_type"]
                timeframe = data_src["timeframe"]
                col_name = data_src["col_name"]
                lookback = data_src.get("lookback", 0) + self.time_offset

                _MISSING = object()

                instrument = next(
                    (i for i in self.instruments_data if i["id"] == instrument_id),
                    _MISSING
                )

                if instrument is _MISSING:
                    raise InstrumentNotFoundError(instrument_id)

                developing = instrument.get("developing", {}).get(timeframe_type, {})
                is_developing = timeframe in developing

                if lookback == 0 and is_developing:
                    # The bucket-to-date candle: everything known at current_time.
                    df = developing[timeframe]
                    pos = df.index.get_indexer([self.current_time], method="ffill")[0]
                else:
                    df = instrument["timeframes"][timeframe_type][timeframe]
                    pos = df.index.get_indexer([self.current_time], method="ffill")[0] - lookback
                    # The bucket holding current_time is still forming, so the last
                    # closed one sits before it. A developing timeframe spends
                    # lookback 0 on the partial candle, making t-1 that bucket.
                    if not is_developing:
                        pos -= 1

                if col_name not in df.columns:
                    # Common with wide indicators on the start of a historical data chunk
                    warn_bad_reference("column missing", data_src, self.current_time)
                    return sys.float_info.epsilon

                if pos < 0:
                    # Before any usable candle; iloc would otherwise wrap around and
                    # read from the far end of the frame.
                    warn_bad_reference("before first candle", data_src, self.current_time)
                    return math.nan

                if df.index[pos].date() != self.current_time.date():
                    self.crossed_day = True

                value = df[col_name].iloc[pos]
                if pd.isna(value):
                    warn_bad_reference("value is NaN", data_src, self.current_time)
                return value
            case _:
                raise ValueError(f"Invalid data source type: {data_src["type"]}")
    
    def get_window(self, data_src: dict, width: int) -> list:
        """Fetches a window of values which ends on the current position"""
        result = []
        for lookback in range(width):
            data_src["lookback"] = lookback
            result.insert(0, self.get(data_src))
        return result

def resolve_operand(ctx: MarketContext, operand):
    if isinstance(operand, Condition):
        return operand(ctx)
    return ctx.get(operand)

def has_nan(values) -> bool:
    return any(math.isnan(value) for value in values)

class And(Condition):
    def __init__(self, id, *children):
        super().__init__(id)
        self.children : list[Condition] = children
    def evaluate(self, ctx):
        scores = [c(ctx) for c in self.children]
        return math.nan if has_nan(scores) else min(scores)
    def sub_conditions(self): return list(self.children)

class Or(Condition):
    def __init__(self, id, *children):
        super().__init__(id)
        self.children : list[Condition] = children
    def evaluate(self, ctx):
        scores = [c(ctx) for c in self.children]
        return math.nan if has_nan(scores) else max(scores)
    def sub_conditions(self): return list(self.children)

class Sequential(Condition):
    def __init__(self, id, *children):
        super().__init__(id)
        self.children : list[Condition] = children

    def evaluate(self, ctx):
        scores = []
        for child in self.children:
            score = child(ctx)
            scores.append(score)
            if not (score >= 0):
                return score
        return min(scores)

    def sub_conditions(self): return list(self.children)

class Not(Condition):
    def __init__(self, id, condition: Condition):
        super().__init__(id)
        self.condition : Condition = condition

    def evaluate(self, ctx): return -1 * (self.condition(ctx))
    def sub_conditions(self): return [self.condition]

class NormalizedSpread(Condition):
    def __init__(self, id, a, b, normalizer):
        super().__init__(id)
        self.a, self.b, self.normalizer = a, b, normalizer

    def evaluate(self, ctx: MarketContext) -> float:
        return (resolve_operand(ctx, self.a) - resolve_operand(ctx, self.b)) / resolve_operand(ctx, self.normalizer)

    def sub_conditions(self):
        return [op for op in (self.a, self.b, self.normalizer) if isinstance(op, Condition)]

# Aliases
Above = NormalizedSpread

class Below(Not):
    def __init__(self, id, a, b, normalizer):
        super().__init__(id, NormalizedSpread(f"{id}_inner", a, b, normalizer))

class Increasing(NormalizedSpread):
    def __init__(self, id, col: dict, normalizer, lookback):
        a = col.copy()
        a_lookback = a.get("lookback", 0)
        b = col.copy()
        b["lookback"] = a_lookback + lookback
        super().__init__(id, a, b, normalizer)

class Decreasing(Not):
    def __init__(self, id, col: dict, normalizer, lookback):
        super().__init__(id, Increasing(f"{id}_inner", col, normalizer, lookback))

class Compare(Condition):
    def __init__(self, id, a, b, c, x, direction, normalizer):
        super().__init__(id)
        self.a, self.b, self.c, self.x = a, b, c, x
        self.direction = direction
        self.normalizer = normalizer

    def evaluate(self, ctx: MarketContext) -> float:
        a = resolve_operand(ctx, self.a)
        threshold = resolve_operand(ctx, self.b) + resolve_operand(ctx, self.c) * self.x
        margin = (threshold - a) if self.direction == "<" else (a - threshold)
        return margin / resolve_operand(ctx, self.normalizer)

    def sub_conditions(self):
        return [op for op in (self.a, self.b, self.c, self.normalizer)
                if isinstance(op, Condition)]

class CandleWick(Condition):
    def __init__(self, id, candle: dict, side, normalizer):
        super().__init__(id)
        self.candle, self.side, self.normalizer = candle, side, normalizer

    def evaluate(self, ctx: MarketContext) -> float:
        def col(name):
            src = dict(self.candle)
            src["col_name"] = name
            return ctx.get(src)
        open_, high, low, close = col("open"), col("high"), col("low"), col("close")
        if self.side == "upper":
            wick = high - max(open_, close)
        else:
            wick = min(open_, close) - low
        return wick / resolve_operand(ctx, self.normalizer)

    def sub_conditions(self):
        return [self.normalizer] if isinstance(self.normalizer, Condition) else []

class CandleBody(NormalizedSpread):
    def __init__(self, id, candle: dict, normalizer):
        a = dict(candle)
        a["col_name"] = "close"
        b = dict(candle)
        b["col_name"] = "open"
        super().__init__(id, a, b, normalizer)

class Kernel(Condition):
    def __init__(self, id, condition: Condition, center=0.0, width=1.0, peak=1.0, floor=0.0, sharpness=1.0):
        super().__init__(id)
        self.condition = condition
        self.center, self.width = center, width
        self.peak, self.floor, self.sharpness = peak, floor, sharpness

    def evaluate(self, ctx: MarketContext) -> float:
        d = self.condition(ctx) - self.center
        hump = self.peak * math.exp(-((d / self.width) ** 2))
        plateau = self.floor / (1.0 + math.exp(-d / self.sharpness))
        return hump + plateau - (self.peak)

    def sub_conditions(self): return [self.condition]

class RecentCrossoverUpward(Condition):
    def __init__(self, id, a, b, window, default): # Window must be positive
        super().__init__(id)
        self.a, self.b, self.window, self.default = a, b, window, default

    def evaluate(self, ctx: MarketContext) -> float:
        window = self.window + 1 # To check an x-wide window for crossovers, we need x+1 values

        vals_a = ctx.get_window(self.a, window)
        vals_b = ctx.get_window(self.b, window)

        vals = [(vals_a[i] - vals_b[i]) for i in range(window)]

        if has_nan(vals):
            return math.nan

        last_crossover : float = self.default # Default value if no crossovers are found in the window
        i = 1
        while i < window:
            if vals[i-1] < 0 and vals[i] >= 0:
                last_crossover = window - i - 1
            i += 1

        if last_crossover < 0:
            return last_crossover
        else:
            return 1 / (1 + last_crossover)

class Multiply(Condition):
    def __init__(self, id, condition: Condition, x=1.0):
        super().__init__(id)
        self.condition = condition
        self.x = x

    def evaluate(self, ctx: MarketContext) -> float:
        return self.x * self.condition(ctx)

    def sub_conditions(self): return [self.condition]

class Boost(Condition):
    def __init__(self, id, base: Condition, bonus: Condition, k=1.0):
        super().__init__(id)
        self.base = base
        self.bonus = bonus
        self.k = k

    def evaluate(self, ctx: MarketContext) -> float:
        b = self.base(ctx)
        bonus = self.bonus(ctx)
        if math.isnan(b) or math.isnan(bonus):
            return math.nan
        factor = 1.0 + self.k * max(0.0, bonus)
        return b * factor if b > 0 else b / factor

    def sub_conditions(self): return [self.base, self.bonus]

class SessionMinute(Condition):
    """
    Minutes since midnight at the instant being evaluated.

    Read from the clock, not from a candle: at a session's first bars nothing has
    closed yet, so a ``time_of_day`` column would fall back to the previous day's
    last candle and report the afternoon.
    """

    def evaluate(self, ctx: MarketContext) -> float:
        return ctx.current_time.hour * 60 + ctx.current_time.minute

class WindowQuantifier(Condition):
    """
    Evaluates a child across the last ``width`` closed candles.

    Offsets run 0..width-1, and a reference resolves offset 0 to the most recent
    closed candle, so the newest candle in the window is one the market has
    finished. Excluding a forming candle is a property of the timeframe (see
    ``developing``), not of the window -- were a timeframe ever marked
    developing, offset 0 would become its forming candle and this would need to
    start at 1 instead.

    With ``same_day`` an offset that reaches an earlier session is dropped, so a
    block cannot fire at the open on the back of yesterday's candles.
    """

    def __init__(self, id, condition: Condition, width, same_day=True):
        super().__init__(id)
        self.condition = condition
        self.width = width
        self.same_day = same_day

    def scores(self, ctx: MarketContext) -> list:
        saved_offset = ctx.time_offset
        found = []
        for offset in range(self.width - 1, -1, -1):
            ctx.time_offset = saved_offset + offset
            ctx.crossed_day = False
            score = self.condition(ctx)
            if not (self.same_day and ctx.crossed_day):
                found.append(score)
        ctx.time_offset = saved_offset
        ctx.crossed_day = False
        return found

    def sub_conditions(self): return [self.condition]

class ExistsInWindow(WindowQuantifier):
    def evaluate(self, ctx: MarketContext) -> float:
        scores = self.scores(ctx)
        # No candle left in this session yet -- unknown rather than false.
        return math.nan if not scores or has_nan(scores) else max(scores)

class ForAllInWindow(WindowQuantifier):
    def evaluate(self, ctx: MarketContext) -> float:
        scores = self.scores(ctx)
        return math.nan if not scores or has_nan(scores) else min(scores)

    def sub_conditions(self): return [self.condition]

# ---------------------------------------------------------------------------
# Self-describing condition registry
#
# Each entry maps a condition name to a ConditionSpec: the class that builds it,
# plus the arguments it takes. The config-editor UI reads these arg specs to
# render the right fields automatically -- so to add a new condition type, add
# its class above and a single entry here, and it appears (correctly formed) in
# the editor with no UI changes.
#
# ArgSpec.kind is one of:
#   "operand"   -- a value-or-reference input (a MarketContext.get data_src dict)
#   "reference" -- must be a column reference (e.g. a slope's source column)
#   "int"       -- an integer parameter       (e.g. lookback)
#   "children"  -- nested child conditions     (combinators: and / or / not);
#                  min_children / max_children bound the arity (None = unbounded)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgSpec:
    name: str
    kind: str
    min_children: int | None = None
    max_children: int | None = None
    options: tuple = ()


@dataclass(frozen=True)
class ConditionSpec:
    cls: type
    args: tuple[ArgSpec, ...]

    @property
    def is_combinator(self) -> bool:
        return any(a.kind == "children" for a in self.args)


CONDITION_REGISTRY: dict[str, ConditionSpec] = {
    # Combinators -- args is a list of child conditions.
    "and": ConditionSpec(And, (ArgSpec("args", "children", min_children=1),)),
    "or":  ConditionSpec(Or,  (ArgSpec("args", "children", min_children=1),)),
    "not": ConditionSpec(Not, (ArgSpec("args", "children", min_children=1, max_children=1),)),
    # Ordered tiers: a child is only evaluated if every earlier one passed.
    "sequential": ConditionSpec(Sequential, (ArgSpec("args", "children", min_children=1),)),

    # Spread family -- (a - b) / normalizer.
    "normalized_spread": ConditionSpec(NormalizedSpread, (
        ArgSpec("a", "operand"), ArgSpec("b", "operand"), ArgSpec("normalizer", "operand"))),
    "above": ConditionSpec(Above, (
        ArgSpec("a", "operand"), ArgSpec("b", "operand"), ArgSpec("normalizer", "operand"))),
    "below": ConditionSpec(Below, (
        ArgSpec("a", "operand"), ArgSpec("b", "operand"), ArgSpec("normalizer", "operand"))),

    # Slope family -- a column vs. its own lagged value, normalized.
    "increasing": ConditionSpec(Increasing, (
        ArgSpec("col", "reference"), ArgSpec("normalizer", "operand"), ArgSpec("lookback", "int"))),
    "decreasing": ConditionSpec(Decreasing, (
        ArgSpec("col", "reference"), ArgSpec("normalizer", "operand"), ArgSpec("lookback", "int"))),

    # a  <or>  b + c * x
    "compare": ConditionSpec(Compare, (
        ArgSpec("x", "float"), ArgSpec("direction", "choice", options=("<", ">")),
        ArgSpec("a", "operand"), ArgSpec("b", "operand"), ArgSpec("c", "operand"),
        ArgSpec("normalizer", "operand"))),

    "candle_body": ConditionSpec(CandleBody, (
        ArgSpec("candle", "candle_reference"), ArgSpec("normalizer", "operand"))),

    "candle_wick": ConditionSpec(CandleWick, (
        ArgSpec("side", "choice", options=("upper", "lower")),
        ArgSpec("candle", "candle_reference"), ArgSpec("normalizer", "operand"))),

    "kernel": ConditionSpec(Kernel, (
        ArgSpec("input", "condition"),
        ArgSpec("center", "float"), ArgSpec("width", "float"),
        ArgSpec("peak", "float"), ArgSpec("floor", "float"),
        ArgSpec("sharpness", "float"))),

    "recent_crossover_upward": ConditionSpec(RecentCrossoverUpward, (
        ArgSpec("a", "reference"), ArgSpec("b", "reference"), ArgSpec("window", "int"),
        ArgSpec("default", "float"))),

    "multiply": ConditionSpec(Multiply, (
        ArgSpec("x", "float"), ArgSpec("input", "condition"))),

    "boost": ConditionSpec(Boost, (
        ArgSpec("k", "float"),
        ArgSpec("base", "condition"), ArgSpec("bonus", "condition"))),

    "exists_in_window": ConditionSpec(ExistsInWindow, (
        ArgSpec("input", "condition"), ArgSpec("width", "int"),
        ArgSpec("same_day", "bool"))),

    "for_all_in_window": ConditionSpec(ForAllInWindow, (
        ArgSpec("input", "condition"), ArgSpec("width", "int"),
        ArgSpec("same_day", "bool"))),

    # Clock, not candle data -- takes no arguments.
    "session_minute": ConditionSpec(SessionMinute, ()),

    "ref": ConditionSpec(None, (ArgSpec("target", "definition_id"),)),
}


class DefinitionResolver:
    def __init__(self, specs: dict):
        self.specs = specs
        self.built: dict[str, Condition] = {}
        self.building: set[str] = set()

    def __contains__(self, name) -> bool:
        return name in self.specs

    def __getitem__(self, name) -> Condition:
        if name in self.built:
            return self.built[name]
        if name in self.building:
            raise CircularDefinitionError(name)
        if name not in self.specs:
            raise DefinitionNotFoundError(name)
        self.building.add(name)
        built = build_condition(self.specs[name], self)
        self.building.discard(name)
        self.built[name] = built
        return built

def build_definitions(config: dict) -> DefinitionResolver:
    resolver = DefinitionResolver({d["id"]: d for d in config.get("definitions", []) or []})
    for name in list(resolver.specs):
        resolver[name]
    return resolver

def disabled_condition_ids(config: dict) -> list:
    return [c["id"] for c in (config.get("conditions") or []) if not c.get("enabled", True)]

def selected_condition_specs(config: dict, only: list = None) -> list:
    specs = config.get("conditions") or []
    if only:
        by_id = {c["id"]: c for c in specs}
        missing = [name for name in only if name not in by_id]
        if missing:
            raise ValueError(
                f"unknown condition id(s) {missing}; available: {list(by_id)}"
            )
        return [by_id[name] for name in only]
    return [c for c in specs if c.get("enabled", True)]

def find_condition_spec(config: dict, node_id: str):
    """The spec of any condition in the config, at any depth, or None."""
    def search(node):
        if isinstance(node, dict):
            if node.get("id") == node_id and "condition" in node:
                return node
            for value in node.values():
                found = search(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = search(item)
                if found is not None:
                    return found
        return None

    for section in ("conditions", "definitions"):
        found = search(config.get(section) or [])
        if found is not None:
            return found
    return None

def build_selected_conditions(config: dict, only: list = None) -> list:
    definitions = build_definitions(config)
    built = [build_condition(c, definitions) for c in selected_condition_specs(config, only)]
    return list({id(c): c for c in built}.values())

def build_operand(operand, definitions: DefinitionResolver = None):
    if isinstance(operand, dict) and operand.get("type") == "condition":
        return build_condition(operand["input"], definitions)
    return operand

def build_condition(spec: dict, definitions: DefinitionResolver = None) -> Condition:
    id = spec["id"]
    if spec["condition"] == "ref":
        target = spec["args"]["target"]
        if definitions is None or target not in definitions:
            raise DefinitionNotFoundError(target)
        return definitions[target]
    # Combinators recurse into their child list.
    if spec["condition"] == "and":
        return And(id, *[build_condition(c, definitions) for c in spec["args"]])
    if spec["condition"] == "or":
        return Or(id, *[build_condition(c, definitions) for c in spec["args"]])
    if spec["condition"] == "not":
        return Not(id, build_condition(spec["args"][0], definitions))
    if spec["condition"] == "sequential":
        return Sequential(id, *[build_condition(c, definitions) for c in spec["args"]])
    if spec["condition"] == "kernel":
        args = dict(spec["args"])
        child = build_condition(args.pop("input"), definitions)
        return Kernel(id, child, **args)
    if spec["condition"] == "multiply":
        args = dict(spec["args"])
        child = build_condition(args.pop("input"), definitions)
        return Multiply(id, child, **args)
    if spec["condition"] == "boost":
        args = dict(spec["args"])
        base = build_condition(args.pop("base"), definitions)
        bonus = build_condition(args.pop("bonus"), definitions)
        return Boost(id, base, bonus, **args)
    if spec["condition"] == "exists_in_window":
        args = dict(spec["args"])
        child = build_condition(args.pop("input"), definitions)
        return ExistsInWindow(id, child, **args)
    if spec["condition"] == "for_all_in_window":
        args = dict(spec["args"])
        child = build_condition(args.pop("input"), definitions)
        return ForAllInWindow(id, child, **args)

    registry_entry = CONDITION_REGISTRY[spec["condition"]]
    args = dict(spec.get("args") or {})
    for arg in registry_entry.args:
        if arg.kind == "operand" and arg.name in args:
            args[arg.name] = build_operand(args[arg.name], definitions)
    return registry_entry.cls(id, **args)

# ---------------------------------------------------------------------------
# Directional reversal
#
# Flips a raw condition spec (the same dict shape used in strategy.yaml,
# before build_condition turns it into a Condition) from an Up-trade reading
# to a Down-trade one, or back. Operates on specs directly and mutates them
# in place, since the config editor works on the raw YAML document.
#
# Id convention: an 'up'/'down' hyphen-token in an id swaps to the other; an
# id with neither gets '-reversed' appended (or stripped, if already
# present). This makes repeated reversal idempotent and lets a reversed ref
# target be found (or created once) by name alone.
# ---------------------------------------------------------------------------

_DIRECTION_TOKENS = {"up": "down", "down": "up"}

def reverse_id(node_id: str) -> str:
    tokens = node_id.split("-")
    for i, token in enumerate(tokens):
        if token in _DIRECTION_TOKENS:
            tokens[i] = _DIRECTION_TOKENS[token]
            return "-".join(tokens)
    if tokens and tokens[-1] == "reversed":
        return "-".join(tokens[:-1])
    return f"{node_id}-reversed"

class UnsupportedReversalError(ValueError):
    def __init__(self, cond_type, node_id):
        super().__init__(f"don't know how to reverse condition type {cond_type!r} (id={node_id!r})")
        self.cond_type, self.node_id = cond_type, node_id

def _find_definition_spec(definitions: list, target: str):
    for definition in definitions:
        if definition.get("id") == target:
            return definition
    return None

def _is_literal_value(operand) -> bool:
    return isinstance(operand, dict) and operand.get("type") == "value"

def _is_rsi_operand(operand) -> bool:
    return isinstance(operand, dict) and operand.get("col_name") == "rsi"

def _is_session_minute(operand) -> bool:
    return (isinstance(operand, dict) and operand.get("type") == "condition"
            and isinstance(operand.get("input"), dict)
            and operand["input"].get("condition") == "session_minute")

def _nested_condition(operand):
    if isinstance(operand, dict) and operand.get("type") == "condition":
        return operand.get("input")
    return None

# Combinator arg lists are shaped [{condition, id, args}, ...] rather than
# following an ArgSpec("kind"="operand") entry, so and/or/sequential/not are
# special-cased; everything else is walked generically off CONDITION_REGISTRY.
def _condition_children(spec: dict) -> list:
    """Every nested raw condition spec directly inside `spec`."""
    cond_type = spec.get("condition")
    if cond_type in ("and", "or", "sequential", "not"):
        return list(spec.get("args") or [])
    entry = CONDITION_REGISTRY.get(cond_type)
    if entry is None:
        return []
    args = spec.get("args") or {}
    children = []
    for arg in entry.args:
        if arg.kind == "condition":
            child = args.get(arg.name)
            if child is not None:
                children.append(child)
        elif arg.kind in ("operand", "reference", "candle_reference"):
            nested = _nested_condition(args.get(arg.name))
            if nested is not None:
                children.append(nested)
    return children

def is_time_gate(spec: dict, definitions: list, seen: frozenset = frozenset()) -> bool:
    """A clock-based (non-directional) leaf: session_minute vs. a fixed minute."""
    cond_type = spec.get("condition")
    if cond_type == "session_minute":
        return True
    if cond_type == "ref":
        target = (spec.get("args") or {}).get("target")
        if target is None or target in seen:
            return False
        definition = _find_definition_spec(definitions, target)
        return definition is not None and is_time_gate(definition, definitions, seen | {target})
    if cond_type in ("above", "below"):
        args = spec.get("args") or {}
        return _is_session_minute(args.get("a")) or _is_session_minute(args.get("b"))
    return False

# Types with no inherent up/down meaning of their own; reversal only needs to
# recurse into whatever they wrap (a combinator's children, a window's input,
# a kernel/multiply/boost's nested condition). candle_body has no direction
# either -- the wrapping above/below + its literal threshold carries that.
_STRUCTURAL_TYPES = {
    "and", "or", "not", "sequential", "exists_in_window", "for_all_in_window",
    "kernel", "multiply", "boost", "session_minute", "candle_body",
}

def reverse_spec(spec: dict, definitions: list) -> None:
    """Flip `spec`'s directional meaning in place (Up <-> Down). `definitions`
    is the live (mutable) definitions list, used to look up or create a
    reversed counterpart for any 'ref' encountered."""
    if is_time_gate(spec, definitions):
        return

    cond_type = spec.get("condition")
    spec["id"] = reverse_id(spec.get("id", ""))

    if cond_type == "ref":
        target = spec["args"]["target"]
        mirror = reverse_id(target)
        if _find_definition_spec(definitions, mirror) is None:
            original = _find_definition_spec(definitions, target)
            if original is None:
                raise DefinitionNotFoundError(target)
            duplicate = copy.deepcopy(original)
            definitions.append(duplicate)
            reverse_spec(duplicate, definitions)
        spec["args"]["target"] = mirror
        return

    if cond_type in ("above", "below"):
        args = spec["args"]
        a, b = args.get("a"), args.get("b")
        inner_a, inner_b = _nested_condition(a), _nested_condition(b)
        # A wick's magnitude threshold doesn't flip -- only which side of the
        # candle it measures does, so this bypasses the outer swap entirely.
        wick = inner_a if (inner_a or {}).get("condition") == "candle_wick" else \
               inner_b if (inner_b or {}).get("condition") == "candle_wick" else None
        if wick is not None:
            reverse_spec(wick, definitions)
            return
        spec["condition"] = "below" if cond_type == "above" else "above"
        if _is_rsi_operand(a) and _is_literal_value(b):
            b["value"] = 100.0 - float(b["value"])
        elif _is_rsi_operand(b) and _is_literal_value(a):
            a["value"] = 100.0 - float(a["value"])
        elif _is_literal_value(b):
            b["value"] = -float(b["value"])
        elif _is_literal_value(a):
            a["value"] = -float(a["value"])
        if inner_a is not None:
            reverse_spec(inner_a, definitions)
        if inner_b is not None:
            reverse_spec(inner_b, definitions)
        return

    if cond_type == "compare":
        args = spec["args"]
        if args.get("direction") in ("<", ">"):
            args["direction"] = ">" if args["direction"] == "<" else "<"
        if "x" in args:
            args["x"] = -float(args["x"])
        for key in ("a", "b", "c"):
            inner = _nested_condition(args.get(key))
            if inner is not None:
                reverse_spec(inner, definitions)
        return

    if cond_type == "candle_wick":
        side = spec["args"].get("side")
        if side == "upper":
            spec["args"]["side"] = "lower"
        elif side == "lower":
            spec["args"]["side"] = "upper"
        return

    if cond_type in ("increasing", "decreasing"):
        spec["condition"] = "decreasing" if cond_type == "increasing" else "increasing"
        return

    if cond_type in _STRUCTURAL_TYPES:
        for child in _condition_children(spec):
            reverse_spec(child, definitions)
        return

    raise UnsupportedReversalError(cond_type, spec.get("id"))