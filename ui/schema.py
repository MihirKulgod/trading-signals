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


# Discriminated on the ``type`` tag, exactly like MarketContext.get's match.
Operand = Annotated[
    Union[ValueOperand, ReferenceOperand],
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


# ---------------------------------------------------------------------------
# Conditions (recursive discriminated union on the ``condition`` tag)
# ---------------------------------------------------------------------------


class SpreadCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["normalized_spread", "above", "below"]
    id: str
    args: SpreadArgs


class SlopeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Literal["increasing", "decreasing"]
    id: str
    args: SlopeArgs


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
    args: RecentCrossoverUpwardArgs


class CombinatorCondition(BaseModel):
    """``and`` / ``or`` / ``not`` -- ``args`` is a list of child conditions."""

    model_config = ConfigDict(extra="forbid")

    condition: Literal["and", "or", "not"]
    id: str
    args: list["Condition"]

    @model_validator(mode="after")
    def _check_arity(self) -> "CombinatorCondition":
        if self.condition == "not" and len(self.args) != 1:
            raise ValueError("'not' must have exactly one child in args")
        if self.condition in ("and", "or") and len(self.args) < 1:
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
    args: MultiplyArgs


# The recursive union every condition slot (top-level or nested child) accepts.
Condition = Annotated[
    Union[CombinatorCondition, SpreadCondition, SlopeCondition, KernelCondition,
          MultiplyCondition, RecentCrossoverUpwardCondition],
    Field(discriminator="condition"),
]

# ``args`` fields forward-reference ``Condition``; resolve them now.
CombinatorCondition.model_rebuild()
KernelArgs.model_rebuild()
MultiplyArgs.model_rebuild()


# ---------------------------------------------------------------------------
# Strategy (config/strategy.yaml)
# ---------------------------------------------------------------------------


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
    # timeframe_type (e.g. "intraday") -> list of minute granularities
    timeframes: dict[str, list[int]]

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
    conditions: list["Condition"]


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


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display: Display
    historical: Historical
