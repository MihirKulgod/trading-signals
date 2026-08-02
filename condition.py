import sys
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
        return self.evaluate(ctx)

    def is_fulfilled(self, ctx: "MarketContext", threshold: float = 0.0) -> bool:
        return self.evaluate(ctx) >= threshold

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"

class InstrumentNotFoundError(KeyError):
    def __init__(self, instrument_id):
        super().__init__(f"instrument_id {instrument_id!r} not found in instruments_data")
        self.instrument_id = instrument_id

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

    def get(self, data_src: dict):
        match data_src["type"]:
            case "value":
                return data_src["value"]
            case "reference":
                instrument_id = data_src["instrument_id"]
                timeframe_type = data_src["timeframe_type"]
                timeframe = data_src["timeframe"]
                col_name = data_src["col_name"]
                lookback = data_src.get("lookback", 0)

                _MISSING = object()

                df : pd.DataFrame = next(
                    (instrument["timeframes"][timeframe_type][timeframe]
                    for instrument in self.instruments_data if instrument["id"] == instrument_id),
                    _MISSING
                )

                if df is _MISSING:
                    raise InstrumentNotFoundError(instrument_id)

                # Gets the closest candle in the given timeframe before current time
                pos = df.index.get_indexer([self.current_time], method="ffill")[0] - lookback

                if col_name not in df.columns:
                    # Common with wide indicators on the start of a historical data chunk
                    return sys.float_info.epsilon
                
                return df[col_name].iloc[pos]
            case _:
                raise ValueError(f"Invalid data source type: {data_src["type"]}")
    
    def get_window(self, data_src: dict, width: int) -> list:
        """Fetches a window of values which ends on the current position"""
        result = []
        for lookback in range(width):
            data_src["lookback"] = lookback
            result.insert(0, self.get(data_src))
        return result

class And(Condition):
    def __init__(self, id, *children):
        super().__init__(id)
        self.children : list[Condition] = children
    def evaluate(self, ctx): return min(c(ctx) for c in self.children)

class Or(Condition):
    def __init__(self, id, *children):
        super().__init__(id)
        self.children : list[Condition] = children
    def evaluate(self, ctx): return max(c(ctx) for c in self.children)

class Not(Condition):
    def __init__(self, id, condition: Condition):
        super().__init__(id)
        self.condition : Condition = condition
    
    def evaluate(self, ctx): return -1 * (self.condition(ctx))

class NormalizedSpread(Condition):
    def __init__(self, id, a, b, normalizer):
        super().__init__(id)
        self.a, self.b, self.normalizer = a, b, normalizer

    def evaluate(self, ctx: MarketContext) -> float:
        return (ctx.get(self.a) - ctx.get(self.b)) / ctx.get(self.normalizer)

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

class RecentCrossoverUpward(Condition):
    def __init__(self, id, a, b, window, default): # Window must be positive
        super().__init__(id)
        self.a, self.b, self.window, self.default = a, b, window, default

    def evaluate(self, ctx: MarketContext) -> float:
        window = self.window + 1 # To check an x-wide window for crossovers, we need x+1 values

        vals_a = ctx.get_window(self.a, window)
        vals_b = ctx.get_window(self.b, window)

        vals = [(vals_a[i] - vals_b[i]) for i in range(window)]

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
}


def build_condition(spec: dict) -> Condition:
    id = spec["id"]
    # Combinators recurse into their child list.
    if spec["condition"] == "and":
        return And(id, *[build_condition(c) for c in spec["args"]])
    if spec["condition"] == "or":
        return Or(id, *[build_condition(c) for c in spec["args"]])
    if spec["condition"] == "not":
        return Not(id, build_condition(spec["args"][0]))
    if spec["condition"] == "kernel":
        args = dict(spec["args"])
        child = build_condition(args.pop("input"))
        return Kernel(id, child, **args)
    if spec["condition"] == "multiply":
        args = dict(spec["args"])
        child = build_condition(args.pop("input"))
        return Multiply(id, child, **args)

    cls = CONDITION_REGISTRY[spec["condition"]].cls
    return cls(id, **spec["args"])