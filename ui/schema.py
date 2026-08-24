"""
Pydantic models describing the engine's YAML configuration files.

This module is the single source of truth for the *structure* of
``config/strategy.yaml`` and ``config/settings.yaml``. It is intentionally free
of any engine imports (no pandas / kiteconnect / pandas_ta), so it can be loaded
quickly and standalone by the config-editor UI as well as by the engine.

The models mirror what ``condition.build_condition`` / ``MarketContext.get`` /
``data_retrieval.resolve_instrument`` actually accept, so validating against
these models is equivalent to asking "will the engine understand this config?".
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Operands (a condition leaf's inputs -- see MarketContext.get)
# ---------------------------------------------------------------------------


class ValueOperand(BaseModel):
    """A literal constant, e.g. ``{type: value, value: 70.0}``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["value"]
    value: float


class ReferenceOperand(BaseModel):
    """A reference to a column of an instrument/timeframe's dataframe."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["reference"]
    instrument_id: str
    timeframe_type: str
    timeframe: str
    col_name: str
    lookback: int = 0


class ConditionOperand(BaseModel):
    """
    A nested condition used where a raw value would normally go, so computed
    scores (candle_body, kernel, ...) can be compared against values/references.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["condition"]
    input: "Condition"


# Discriminated on the ``type`` tag, exactly like MarketContext.get's match.
Operand = Annotated[
    Union[ValueOperand, ReferenceOperand, ConditionOperand],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Leaf-condition argument shapes
# ---------------------------------------------------------------------------


class SpreadArgs(BaseModel):
    """Args for normalized_spread / above / below: ``(a - b) / normalizer``."""

    model_config = ConfigDict(extra="forbid")

    a: Operand
    b: Operand
    normalizer: Operand


class SlopeArgs(BaseModel):
    """Args for increasing / decreasing: a column vs. its lagged self."""

    model_config = ConfigDict(extra="forbid")

    col: ReferenceOperand
    normalizer: Operand
    lookback: int


class CandleReferenceOperand(BaseModel):
    """
    A candle on one instrument/timeframe. Unlike ReferenceOperand there is no
    ``col_name``: the condition using it picks the columns (e.g. candle_body
    reads ``close`` and ``open``).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["reference"]
    instrument_id: str
    timeframe_type: str
    timeframe: str
    lookback: int = 0


class CandleBodyArgs(BaseModel):
    """Args for candle_body: ``(close - open) / normalizer`` for one candle."""

    model_config = ConfigDict(extra="forbid")

    candle: CandleReferenceOperand
    normalizer: Operand


# ---------------------------------------------------------------------------
# Conditions (recursive discriminated union on the ``condition`` tag)
# ---------------------------------------------------------------------------


class SpreadCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["normalized_spread", "above", "below"]
    id: str
    enabled: bool = True
    args: SpreadArgs


class SlopeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["increasing", "decreasing"]
    id: str
    enabled: bool = True
    args: SlopeArgs


class CandleBodyCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["candle_body"]
    id: str
    enabled: bool = True
    args: CandleBodyArgs


class CandleWickArgs(BaseModel):
    """Args for candle_wick: upper or lower wick length of one candle."""

    model_config = ConfigDict(extra="forbid")

    candle: CandleReferenceOperand
    side: Literal["upper", "lower"]
    normalizer: Operand


class CandleWickCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["candle_wick"]
    id: str
    enabled: bool = True
    args: CandleWickArgs


class CompareArgs(BaseModel):
    """Args for compare: is ``a`` on the chosen side of ``b + c * x``."""

    model_config = ConfigDict(extra="forbid")

    a: Operand
    b: Operand
    c: Operand
    x: float
    direction: Literal["<", ">"]
    normalizer: Operand


class CompareCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["compare"]
    id: str
    enabled: bool = True
    args: CompareArgs


class RecentCrossoverUpwardArgs(BaseModel):
    """Args for recent_crossover_upward: recency of ``a`` crossing up over ``b``."""

    model_config = ConfigDict(extra="forbid")

    a: ReferenceOperand
    b: ReferenceOperand
    window: int
    default: float


class RecentCrossoverUpwardCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["recent_crossover_upward"]
    id: str
    enabled: bool = True
    args: RecentCrossoverUpwardArgs


class CombinatorCondition(BaseModel):
    """``and`` / ``or`` / ``not`` -- ``args`` is a list of child conditions."""

    model_config = ConfigDict(extra="forbid")

    condition: Literal["and", "or", "not", "sequential"]
    id: str
    enabled: bool = True
    args: list["Condition"]

    @model_validator(mode="after")
    def _check_arity(self) -> "CombinatorCondition":
        if self.condition == "not" and len(self.args) != 1:
            raise ValueError("'not' must have exactly one child in args")
        if self.condition in ("and", "or", "sequential") and len(self.args) < 1:
            raise ValueError(f"'{self.condition}' must have at least one child in args")
        return self


class KernelArgs(BaseModel):
    """Shape a nested condition's output: ``input`` plus the kernel's float params."""

    model_config = ConfigDict(extra="forbid")

    input: "Condition"
    center: float = 0.0
    width: float = 1.0
    peak: float = 1.0
    floor: float = 0.0
    sharpness: float = 1.0


class KernelCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["kernel"]
    id: str
    enabled: bool = True
    args: KernelArgs


class MultiplyArgs(BaseModel):
    """Args for multiply: ``x`` times the wrapped condition's value."""

    model_config = ConfigDict(extra="forbid")

    input: "Condition"
    x: float = 1.0


class MultiplyCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["multiply"]
    id: str
    enabled: bool = True
    args: MultiplyArgs


class BoostArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: "Condition"
    bonus: "Condition"
    k: float = 1.0


class BoostCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["boost"]
    id: str
    enabled: bool = True
    args: BoostArgs


class ExistsInWindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: "Condition"
    width: int
    same_day: bool = True


class ExistsInWindowCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["exists_in_window"]
    id: str
    enabled: bool = True
    args: ExistsInWindowArgs


class ForAllInWindowArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: "Condition"
    width: int
    same_day: bool = True


class ForAllInWindowCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["for_all_in_window"]
    id: str
    enabled: bool = True
    args: ForAllInWindowArgs


class RefArgs(BaseModel):
    """Args for ref: the id of an entry in the top-level ``definitions`` list."""

    model_config = ConfigDict(extra="forbid")

    target: str


class RefCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["ref"]
    id: str
    enabled: bool = True
    args: RefArgs


class SessionMinuteArgs(BaseModel):
    """session_minute reads the clock, so it takes no arguments."""

    model_config = ConfigDict(extra="forbid")


class SessionMinuteCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["session_minute"]
    id: str
    enabled: bool = True
    args: SessionMinuteArgs = SessionMinuteArgs()


# The recursive union every condition slot (top-level or nested child) accepts.
Condition = Annotated[
    Union[CombinatorCondition, SpreadCondition, SlopeCondition, CandleBodyCondition,
          CompareCondition, CandleWickCondition,
          KernelCondition, MultiplyCondition, BoostCondition, ExistsInWindowCondition,
          ForAllInWindowCondition, RecentCrossoverUpwardCondition,
          SessionMinuteCondition, RefCondition],
    Field(discriminator="condition"),
]


def walk_condition(cond) -> "list":
    """Yield a validated condition model and every nested condition under it."""
    out = [cond]
    args = cond.args
    if isinstance(args, list):
        for child in args:
            out.extend(walk_condition(child))
    else:
        for field in ("input", "base", "bonus"):
            child = getattr(args, field, None)
            if child is not None:
                out.extend(walk_condition(child))
        for field in ("a", "b", "c", "normalizer"):
            operand = getattr(args, field, None)
            if operand is not None and getattr(operand, "type", None) == "condition":
                out.extend(walk_condition(operand.input))
    return out

# ``args`` fields forward-reference ``Condition``; resolve them now.
ConditionOperand.model_rebuild()
SpreadArgs.model_rebuild()
SlopeArgs.model_rebuild()
CandleBodyArgs.model_rebuild()
CompareArgs.model_rebuild()
CandleWickArgs.model_rebuild()
CombinatorCondition.model_rebuild()
KernelArgs.model_rebuild()
MultiplyArgs.model_rebuild()
BoostArgs.model_rebuild()
ExistsInWindowArgs.model_rebuild()
ForAllInWindowArgs.model_rebuild()
SpreadCondition.model_rebuild()
SlopeCondition.model_rebuild()
CandleBodyCondition.model_rebuild()
CompareCondition.model_rebuild()
CandleWickCondition.model_rebuild()


# ---------------------------------------------------------------------------
# Strategy (config/strategy.yaml)
# ---------------------------------------------------------------------------


class TimeframeSpec(BaseModel):
    """
    One configured granularity. ``developing`` makes ``lookback: 0`` resolve to
    the partially formed candle at the current moment instead of the last
    completed one; a bare int is shorthand for ``developing: false``.
    """

    model_config = ConfigDict(extra="forbid")

    minutes: int
    developing: bool = False


class Instrument(BaseModel):
    """
    Either a non-rolling spot instrument (``trading_symbol``) or a rolling one
    (``name`` + ``instrument_type`` + ``expiry_rule``). Mirrors resolve_instrument.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    # Spot identity
    trading_symbol: Optional[str] = None
    # Rolling identity
    name: Optional[str] = None
    instrument_type: Optional[str] = None
    expiry_rule: Optional[str] = None
    # timeframe_type (e.g. "intraday") -> list of granularities
    timeframes: dict[str, list[Union[int, TimeframeSpec]]]

    @model_validator(mode="after")
    def _check_identity(self) -> "Instrument":
        is_spot = self.trading_symbol is not None
        is_rolling = self.name is not None and self.instrument_type is not None
        if is_spot and is_rolling:
            raise ValueError(
                f"instrument {self.id!r}: has both a spot (trading_symbol) and a "
                "rolling (name+instrument_type) identity; pick one"
            )
        if not is_spot and not is_rolling:
            raise ValueError(
                f"instrument {self.id!r}: needs either trading_symbol (spot) or "
                "name+instrument_type (rolling)"
            )
        return self


class Exchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchange: str
    instruments: list[Instrument]


class General(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exchanges: list[Exchange]


class Indicator(BaseModel):
    """
    One pandas_ta indicator. Params beyond ``kind``/``alias`` vary per indicator
    (length, fast, slow, signal, ...), so extra keys are allowed and preserved.
    ``alias`` is a single name, or a list for multi-output indicators (MACD).
    """

    model_config = ConfigDict(extra="allow")

    kind: str
    alias: Union[str, list[str]]


class Strategy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    general: General
    ta: list[Indicator]
    definitions: list["Condition"] = Field(default_factory=list)
    conditions: list["Condition"]

    @model_validator(mode="after")
    def _check_ref_targets(self) -> "Strategy":
        defined = {d.id for d in self.definitions}
        for root in list(self.definitions) + list(self.conditions):
            for node in walk_condition(root):
                if node.condition == "ref" and node.args.target not in defined:
                    raise ValueError(
                        f"condition {node.id!r}: ref target {node.args.target!r} "
                        "is not present in definitions"
                    )
        return self

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "Strategy":
        origin = {}
        for section, roots in (("definitions", self.definitions), ("conditions", self.conditions)):
            for root in roots:
                for node in walk_condition(root):
                    if node.id in origin:
                        raise ValueError(
                            f"duplicate condition id {node.id!r}: used in {origin[node.id]} "
                            f"and again in {section}; ids must be unique because scores, "
                            "chart columns and notifications are keyed by them"
                        )
                    origin[node.id] = section
        return self

    @model_validator(mode="after")
    def _check_enabled_placement(self) -> "Strategy":
        top_level = {id(c) for c in self.conditions}
        for root in list(self.definitions) + list(self.conditions):
            for node in walk_condition(root):
                if id(node) in top_level:
                    continue
                if "enabled" in node.model_fields_set:
                    raise ValueError(
                        f"condition {node.id!r}: 'enabled' may only be set on top-level "
                        "entries in conditions:, not on nested conditions or definitions"
                    )
        return self


Strategy.model_rebuild()


# ---------------------------------------------------------------------------
# Settings (config/settings.yaml)
# ---------------------------------------------------------------------------


class Display(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Each dict is one panel: {alias_column_name: matplotlib_color, ...}
    display_panels: list[dict[str, str]]
    examination_window: int
    instrument_id: str
    timeframe: str
    signal_aggregates: list[str]


class Historical(BaseModel):
    # ``from`` is a Python keyword, so store it as ``from_`` with an alias.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str = ""


class Backtest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    download: bool = False
    reuse_signals: bool = False
    visualize: bool = False


class DashboardPanel(BaseModel):
    """One monitor on the live dashboard grid."""

    model_config = ConfigDict(extra="forbid")

    block: str = ""
    name: str = ""


class Dashboard(BaseModel):
    """The live dashboard grid: panel size, and the panels themselves."""

    model_config = ConfigDict(extra="forbid")

    size: float = Field(default=1.0, ge=0.6, le=1.4)
    panels: list[DashboardPanel] = []


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display: Display
    historical: Historical
    backtest: Backtest = Backtest()
    dashboard: Dashboard = Dashboard()
