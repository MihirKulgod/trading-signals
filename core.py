import pandas as pd
from abc import ABC, abstractmethod
from data_retrieval import bracket_by_day, downsample_days, concat_days
import yaml

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

    def is_fulfilled(self, ctx: "MarketContext", threshold: float = 1.0) -> bool:
        return self.evaluate(ctx) >= threshold

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"

class MarketContext:
    def __init__(self, dfs: dict[str, pd.DataFrame], current_time: pd.Timestamp):
        self.dfs, self.current_time = dfs, current_time

    def get(self, col_name: str, timeframe: str, lookback=0):
        df = self.dfs[timeframe]
        # Gets the closest candle in the given timeframe before current time
        pos = df.index.get_indexer([self.current_time], method="ffill")[0] - lookback
        return df[col_name].iloc[pos]

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
    def __init__(self, id, a, b, normalizer, timeframe):
        super().__init__(id)
        self.a, self.b, self.normalizer, self.timeframe = a, b, normalizer, timeframe

    def evaluate(self, ctx: MarketContext) -> float:
        return (ctx.get(self.a, self.timeframe) - ctx.get(self.b, self.timeframe)) / ctx.get(self.normalizer, self.timeframe)
    
class FixedThreshold(Condition):
    def __init__(self, id, a, b, normalizer, timeframe):
        super().__init__(id)
        self.a, self.b, self.normalizer, self.timeframe = a, b, normalizer, timeframe

    def evaluate(self, ctx: MarketContext) -> float:
        return (ctx.get(self.a, self.timeframe) - self.b) / self.normalizer

CONDITION_REGISTRY = {
    "normalized_spread": NormalizedSpread,
    "fixed_threshold": FixedThreshold,
    # ... one entry per atomic condition type you define
}

def build_condition(spec: dict) -> Condition:
    id = spec["id"]
    # Recursive cases
    if spec["condition"] == "and":
        return And(id, *[build_condition(c) for c in spec["args"]])
    if spec["condition"] == "or":
        return Or(id, *[build_condition(c) for c in spec["args"]])
    if spec["condition"] == "not":
        return Not(id, build_condition(spec["args"][0]))
    
    cls = CONDITION_REGISTRY[spec["condition"]]
    return cls(id, **spec["args"])

def generate_signals(df: pd.DataFrame):
    with open("strategy/strategy_config.yaml") as f:
        config = yaml.safe_load(f)
    
    candles_1m = bracket_by_day(df)

    timeframes = config["general"]["timeframes"]["intraday"]
    # Work in progress