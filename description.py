"""
Human-readable descriptions of condition blocks, rendered as Markdown.

Renders a raw condition spec (the dict shape used in strategy.yaml) as prose
close to how the combinations doc states its rules, e.g.

    *Spot* T15 **MACD Line** (t) > *Spot* T15 **MACD Line** (t-1) + *Spot* T15 **ATR** * `0.1`

Instrument names (Spot/Future) are italicised -- context, not the point --
while indicator/column names are bold, since those are what a reader is
actually scanning for. Exact numbers and clock times are code-formatted so
they stand out as literal values rather than prose.

References are never expanded -- a ref prints as "Reference: **<name>**" --
so a shared definition is spelled out once, under the Definitions section,
instead of being repeated at every use.
"""

from condition import CONDITION_REGISTRY

INDENT = "    "

# Leaf condition types with no children of their own -- a self-contained
# formula, as opposed to a combinator/window/kernel that wraps other blocks.
ATOMIC_TYPES = {
    "above", "below", "compare", "normalized_spread",
    "increasing", "decreasing", "recent_crossover_upward", "session_minute",
}

# Tokens that read as acronyms rather than words when an id is turned into a
# title. Anything shaped like a letter followed by digits (t60, c01, s1) is
# upper-cased too, so only genuine words are left to capitalise.
_ACRONYMS = {"rsi", "ema", "macd", "atr", "vwap", "ohlc", "vwma", "s1", "s2", "s3"}

_INSTRUMENT_NAMES = {"nifty_spot": "Spot", "nifty_fut": "Future"}

# Columns that no indicator declares: raw candle fields and derived extras.
_BASE_COLUMNS = {
    "open": "Open", "high": "High", "low": "Low", "close": "Close",
    "volume": "Volume", "session_atr": "Session ATR", "time_of_day": "Time of Day",
    "body": "Body", "upper_wick": "Upper Wick", "lower_wick": "Lower Wick",
}

_KIND_LABELS = {
    "ema": "EMA", "sma": "SMA", "rsi": "RSI", "atr": "ATR", "vwap": "VWAP",
    "zscore": "Z-Score", "macd": "MACD",
}

# Only moving averages are named by their length, the way the playbook does it
# ("13 EMA" vs. "34 EMA"); a lone ATR or RSI needs no such disambiguation.
_LENGTH_IN_LABEL = {"ema", "sma", "wma", "hma", "dema", "tema"}

_MACD_LABELS = {
    "macd_line": "MACD Line", "macd_signal": "MACD Signal",
    "macd_histogram": "MACD Histogram",
}


def readable_id(node_id) -> str:
    """this-format -> This Format, keeping acronyms and codes upper-case."""
    if not node_id:
        return ""
    words = []
    for token in str(node_id).replace("_", "-").split("-"):
        if not token:
            continue
        lowered = token.lower()
        if lowered in _ACRONYMS or (lowered[0].isalpha() and lowered[1:].isdigit()):
            words.append(token.upper())
        else:
            words.append(token.capitalize())
    return " ".join(words)


def _number(value) -> str:
    """Drop the trailing '.0' that YAML floats carry, keep everything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _code(text) -> str:
    """A literal value -- number, clock time -- set apart as code."""
    return f"`{text}`"


def _clock(minutes) -> str:
    total = int(minutes)
    return f"{total // 60:02d}:{total % 60:02d}"


def column_labels(strategy: dict) -> dict:
    """
    Column name -> display label, built from the strategy's own indicator list
    so a 13-length EMA reads as "13 EMA", the way the playbook writes it.
    """
    labels = dict(_BASE_COLUMNS)
    for indicator in strategy.get("ta") or []:
        kind = str(indicator.get("kind", ""))
        label = _KIND_LABELS.get(kind, kind.upper() or "?")
        length = indicator.get("length")
        alias = indicator.get("alias")
        if isinstance(alias, list):
            for name in alias:
                labels[name] = _MACD_LABELS.get(name, readable_id(name))
        elif alias:
            labels[alias] = f"{length} {label}" if length and kind in _LENGTH_IN_LABEL else label
    return labels


class Describer:
    def __init__(self, strategy: dict = None):
        strategy = strategy or {}
        self.columns = column_labels(strategy)
        self.instruments = dict(_INSTRUMENT_NAMES)
        for exchange in (strategy.get("general") or {}).get("exchanges") or []:
            for instrument in exchange.get("instruments") or []:
                name = instrument.get("id")
                if name and name not in self.instruments:
                    self.instruments[name] = readable_id(name)

    # --- operands ----------------------------------------------------------

    def _timeframe(self, timeframe) -> str:
        text = str(timeframe or "")
        return f"T{text[:-3]}" if text.endswith("min") else text

    def _lookback_suffix(self, lookback) -> str:
        return "(t)" if not lookback else f"(t-{int(lookback)})"

    def operand(self, operand, annotate: bool = False, field_override: str = None) -> str:
        """
        A single value in an expression. ``annotate`` forces the (t)/(t-n)
        marker on; without it the marker appears only where it carries
        information, i.e. when the operand actually reaches into the past.
        ``field_override`` lets a caller (e.g. a candle phrase) put its own
        label -- "Candle Body" -- in the bold slot instead of the column name.
        """
        if operand is None:
            return "?"
        if not isinstance(operand, dict):
            return _code(_number(operand))
        kind = operand.get("type")
        if kind == "value":
            return _code(_number(operand.get("value")))
        if kind == "condition":
            return self.inline(operand.get("input"))
        if kind == "reference":
            instrument = self.instruments.get(operand.get("instrument_id"),
                                              readable_id(operand.get("instrument_id")))
            column = operand.get("col_name")
            field = field_override or (self.columns.get(column, readable_id(column))
                                       if column else "Candle")
            parts = [f"*{instrument}*" if instrument else "",
                     self._timeframe(operand.get("timeframe")),
                     f"**{field}**"]
            text = " ".join(part for part in parts if part)
            lookback = operand.get("lookback") or 0
            if annotate or lookback:
                text += f" {self._lookback_suffix(lookback)}"
            return text
        return str(operand)

    def _is_session_minute(self, operand) -> bool:
        return (isinstance(operand, dict) and operand.get("type") == "condition"
                and isinstance(operand.get("input"), dict)
                and operand["input"].get("condition") == "session_minute")

    def _lookback_of(self, operand) -> int:
        if isinstance(operand, dict) and operand.get("type") == "reference":
            return int(operand.get("lookback") or 0)
        return 0

    def _pair(self, a, b) -> tuple[str, str]:
        """
        Render two compared operands, marking both with (t)/(t-n) only when
        they sit at different points in time -- that is where the distinction
        matters, and where the playbook itself writes it out.
        """
        annotate = self._lookback_of(a) != self._lookback_of(b)
        return self.operand(a, annotate), self.operand(b, annotate)

    def _offset(self, c, x) -> str:
        """The '+ ATR * 0.1' tail of a compare, with the sign folded in."""
        if x in (None, 0, 0.0):
            return ""
        magnitude = self.operand(c)
        sign = "-" if float(x) < 0 else "+"
        scale = _code(_number(abs(float(x))))
        if magnitude == _code("1"):
            return f" {sign} {scale}"
        return f" {sign} {magnitude} * {scale}"

    # --- quick formula (atomic blocks only) ---------------------------------
    #
    # A short, plain-text formula for a childless block's header -- no
    # instrument or timeframe, since the point is reading a block's meaning
    # at a glance without expanding it or tracking which argument is which.
    # Every operand always carries its (t)/(t-n) marker, even at (t), since
    # without the surrounding instrument/timeframe context there's nothing
    # else to signal "this one's current".

    def _quick_operand(self, operand) -> str:
        if operand is None:
            return "?"
        if not isinstance(operand, dict):
            return _number(operand)
        kind = operand.get("type")
        if kind == "value":
            return _number(operand.get("value"))
        if kind == "condition":
            return self.quick_formula(operand.get("input")) or "?"
        if kind == "reference":
            column = operand.get("col_name")
            label = self.columns.get(column, readable_id(column)) if column else "Candle"
            return f"{label} {self._lookback_suffix(operand.get('lookback') or 0)}"
        return str(operand)

    def _quick_offset(self, c, x) -> str:
        if x in (None, 0, 0.0):
            return ""
        magnitude = self._quick_operand(c)
        sign = "-" if float(x) < 0 else "+"
        scale = _number(abs(float(x)))
        if magnitude == "1":
            return f" {sign} {scale}"
        return f" {sign} {magnitude} * {scale}"

    def quick_formula(self, spec) -> str:
        """None for anything with children; a short formula for a leaf."""
        if not isinstance(spec, dict) or spec.get("condition") not in ATOMIC_TYPES:
            return None
        cond_type = spec.get("condition")
        args = spec.get("args") or {}

        if cond_type == "session_minute":
            return "Time of day"
        if cond_type in ("above", "below"):
            symbol = ">" if cond_type == "above" else "<"
            a_raw, b_raw = args.get("a"), args.get("b")
            if self._is_session_minute(a_raw) and isinstance(b_raw, dict) and b_raw.get("type") == "value":
                return f"Time of day {symbol} {_clock(b_raw.get('value'))}"
            if self._is_session_minute(b_raw) and isinstance(a_raw, dict) and a_raw.get("type") == "value":
                return f"{_clock(a_raw.get('value'))} {symbol} Time of day"
            return f"{self._quick_operand(a_raw)} {symbol} {self._quick_operand(b_raw)}"
        if cond_type == "normalized_spread":
            return f"{self._quick_operand(args.get('a'))} - {self._quick_operand(args.get('b'))}"
        if cond_type == "compare":
            symbol = ">" if args.get("direction") == ">" else "<"
            a, b = self._quick_operand(args.get("a")), self._quick_operand(args.get("b"))
            return f"{a} {symbol} {b}{self._quick_offset(args.get('c'), args.get('x'))}"
        if cond_type in ("increasing", "decreasing"):
            verb = "rising" if cond_type == "increasing" else "falling"
            return f"{self._quick_operand(args.get('col'))} {verb}"
        if cond_type == "recent_crossover_upward":
            a, b = self._quick_operand(args.get("a")), self._quick_operand(args.get("b"))
            return f"{a} crosses above {b}"
        return None

    # --- leaf expressions --------------------------------------------------

    def _comparison(self, spec: dict, symbol: str) -> str:
        args = spec.get("args") or {}
        a_raw, b_raw = args.get("a"), args.get("b")
        # A clock gate reads far better as a time than as minutes-since-midnight.
        if self._is_session_minute(a_raw) and isinstance(b_raw, dict) \
                and b_raw.get("type") == "value":
            return f"**Time of day** {symbol} {_code(_clock(b_raw.get('value')))}"
        if self._is_session_minute(b_raw) and isinstance(a_raw, dict) \
                and a_raw.get("type") == "value":
            return f"{_code(_clock(a_raw.get('value')))} {symbol} **Time of day**"
        a, b = self._pair(a_raw, b_raw)
        return f"{a} {symbol} {b}"

    def inline(self, spec) -> str:
        """A condition rendered as a phrase, for use inside an expression."""
        if not isinstance(spec, dict):
            return "?"
        cond_type = spec.get("condition")
        args = spec.get("args") or {}

        if cond_type == "ref":
            return f"Reference: **{readable_id(args.get('target'))}**"
        if cond_type == "session_minute":
            return "**Time of day**"
        if cond_type == "above":
            return self._comparison(spec, ">")
        if cond_type == "below":
            return self._comparison(spec, "<")
        if cond_type == "normalized_spread":
            a, b = self._pair(args.get("a"), args.get("b"))
            return f"{a} - {b}"
        if cond_type == "compare":
            symbol = ">" if args.get("direction") == ">" else "<"
            a, b = self._pair(args.get("a"), args.get("b"))
            return f"{a} {symbol} {b}{self._offset(args.get('c'), args.get('x'))}"
        if cond_type in ("increasing", "decreasing"):
            verb = "rising" if cond_type == "increasing" else "falling"
            span = int(args.get("lookback") or 1)
            candles = "candle" if span == 1 else "candles"
            return f"{self.operand(args.get('col'))} is *{verb}* over {span} {candles}"
        if cond_type == "recent_crossover_upward":
            a, b = self.operand(args.get("a")), self.operand(args.get("b"))
            return f"{a} crossed above {b} within the last {int(args.get('window') or 0)} candles"
        if cond_type == "multiply":
            return f"{_code(_number(args.get('x')))} * ({self.inline(args.get('input'))})"
        if cond_type == "not":
            children = args if isinstance(args, list) else []
            inner = self.inline(children[0]) if children else "?"
            return f"**NOT** ({inner})"
        if cond_type in ("and", "or"):
            joiner = " AND " if cond_type == "and" else " OR "
            children = args if isinstance(args, list) else []
            return "(" + joiner.join(self.inline(child) for child in children) + ")"
        return self._fallback(spec)

    def _fallback(self, spec: dict) -> str:
        """Anything without a bespoke phrasing still shows its own arguments."""
        cond_type = spec.get("condition")
        args = spec.get("args") or {}
        entry = CONDITION_REGISTRY.get(cond_type)
        parts = []
        if entry and isinstance(args, dict):
            for arg in entry.args:
                if arg.name not in args:
                    continue
                if arg.kind in ("operand", "reference", "candle_reference"):
                    parts.append(f"{arg.name}: {self.operand(args[arg.name])}")
                elif arg.kind != "condition":
                    parts.append(f"{arg.name}={_code(_number(args[arg.name]))}")
        detail = ", ".join(parts)
        return f"**{readable_id(cond_type)}** ({detail})" if detail else f"**{readable_id(cond_type)}**"

    # --- block structure ---------------------------------------------------

    def _window_header(self, spec: dict, quantifier: str) -> str:
        args = spec.get("args") or {}
        width = int(args.get("width") or 0)
        scope = "" if args.get("same_day", True) is not False \
            else ", reaching into the previous session if needed"
        if width == 1:
            return f"**In the last closed candle**{scope}:"
        return f"**In {quantifier} of the last {width} closed candles**{scope}:"

    def lines(self, spec, depth: int = 1) -> list[str]:
        """The block as a nested Markdown list: combinators nest, leaves are
        list items. depth=1 is the top of the list (no indent)."""
        pad = INDENT * (depth - 1)
        if not isinstance(spec, dict):
            return [f"{pad}- ?"]
        cond_type = spec.get("condition")
        args = spec.get("args")

        if cond_type in ("and", "or", "sequential"):
            headers = {"and": "**All of:**", "or": "**Any of:**",
                       "sequential": "**In order, each step gating the next:**"}
            out = [f"{pad}- {headers[cond_type]}"]
            for child in args or []:
                out += self.lines(child, depth + 1)
            return out

        if cond_type == "not":
            children = args if isinstance(args, list) else []
            out = [f"{pad}- **NOT:**"]
            for child in children:
                out += self.lines(child, depth + 1)
            return out

        if cond_type in ("exists_in_window", "for_all_in_window"):
            quantifier = "at least one" if cond_type == "exists_in_window" else "every one"
            out = [f"{pad}- {self._window_header(spec, quantifier)}"]
            out += self.lines((args or {}).get("input"), depth + 1)
            return out

        if cond_type == "boost":
            k = _code(_number((args or {}).get("k")))
            out = [f"{pad}- **Boost** (k={k}) -- base:"]
            out += self.lines((args or {}).get("base"), depth + 1)
            out.append(f"{pad}- scaled up by:")
            out += self.lines((args or {}).get("bonus"), depth + 1)
            return out

        if cond_type == "kernel":
            detail = ", ".join(f"{key}={_code(_number((args or {}).get(key)))}"
                               for key in ("center", "width", "peak", "floor", "sharpness")
                               if (args or {}).get(key) is not None)
            out = [f"{pad}- **Kernel** ({detail}) applied to:"]
            out += self.lines((args or {}).get("input"), depth + 1)
            return out

        if cond_type == "multiply":
            factor = _code(_number((args or {}).get("x")))
            out = [f"{pad}- {factor} times:"]
            out += self.lines((args or {}).get("input"), depth + 1)
            return out

        return [f"{pad}- {self.inline(spec)}"]

    def block(self, spec: dict) -> str:
        """One block: its title as a heading, then its logic as a list."""
        if not isinstance(spec, dict):
            return ""
        title = f"### {readable_id(spec.get('id'))}"
        if spec.get("enabled") is False:
            title += " *(disabled)*"
        return "\n".join([title, ""] + self.lines(spec, 1))


def describe_block(spec: dict, strategy: dict = None) -> str:
    return Describer(strategy).block(spec)


def describe_strategy(strategy: dict) -> str:
    """
    The whole strategy: shared definitions first, then the conditions that use
    them, so a reference is always explained before it is relied on.
    """
    describer = Describer(strategy)
    out = [f"# {strategy.get('name') or 'Strategy'}"]
    if strategy.get("description"):
        out += ["", f"*{strategy['description']}*"]

    for heading, key in (("Definitions", "definitions"), ("Conditions", "conditions")):
        blocks = strategy.get(key) or []
        out += ["", f"## {heading}"]
        if not blocks:
            out += ["", f"*(no {key})*"]
            continue
        for spec in blocks:
            out += ["", describer.block(spec), "", "---"]
    if out[-1] == "---":
        out.pop()
    return "\n".join(out).rstrip() + "\n"
