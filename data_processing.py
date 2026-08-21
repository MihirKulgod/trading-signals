
import math
import pandas as pd
import importlib.metadata # pandas-ta-openbb has a bug, requiring this to be imported first 
import os
from tqdm import tqdm
import pandas_ta as ta
import mplfinance as mpf

from condition import (build_condition, build_definitions, build_selected_conditions,
                       Condition, MarketContext, ColumnNotFoundError)
from data_retrieval import bracket_by_day, concat_days, downsample_days
from visualise import try_addplot

def aliased_ta_config(ta_config):
    """
    Rewrites each indicator entry so pandas_ta names its own output columns via
    col_names. Without this the aliases are matched positionally, which silently
    mislabels every column after any indicator pandas_ta skipped for lack of data.
    """
    result = []
    for indicator in ta_config:
        entry = {key: value for key, value in indicator.items() if key != "alias"}
        alias = indicator["alias"]
        entry["col_names"] = tuple(alias) if isinstance(alias, list) else (alias,)
        result.append(entry)
    return result

DERIVED_ATR_LENGTH = 14

def add_derived_columns(df):
    """
    Columns derived from the frame itself rather than from a pandas_ta indicator.
    Conditions reference them by name like any other column, so which ones a
    strategy uses stays a config decision.

    session_atr: ATR whose True Range never spans a session boundary. Days are
    concatenated before indicators run, so at each day's first bar the standard
    True Range measures the overnight gap rather than that bar's range.
    """
    df["time_of_day"] = df.index.hour * 60 + df.index.minute

    true_range = ta.true_range(df["high"], df["low"], df["close"])
    if true_range is None:
        # Frame too short for the indicator; the developing frame starts this way.
        df["session_atr"] = math.nan
        return
    first_of_day = ~df.index.normalize().duplicated()
    true_range[first_of_day] = (df["high"] - df["low"])[first_of_day]
    session_atr = ta.rma(true_range, length=DERIVED_ATR_LENGTH)
    if session_atr is None:
        df["session_atr"] = math.nan
        return
    # rma emits a value from the first bar; blank the warmup so partially
    # converged values stay NaN rather than looking like real averages.
    session_atr.iloc[: DERIVED_ATR_LENGTH - 1] = math.nan
    df["session_atr"] = session_atr

def build_strategy(config):
    return ta.Strategy(
        name=config["name"],
        description=config["description"],
        ta=aliased_ta_config(config["ta"]),
    )

def timeframe_entries(instrument) -> list[tuple[int, bool]]:
    """(minutes, developing) per configured timeframe; a bare int means not developing."""
    entries = []
    for entry in instrument["timeframes"]["intraday"]:
        if isinstance(entry, dict):
            entries.append((int(entry["minutes"]), bool(entry.get("developing", False))))
        else:
            entries.append((int(entry), False))
    return entries

def bucket_labels(candles_1m_days, minutes: int) -> pd.Series:
    """
    The downsample bucket each 1-minute row belongs to. Mirrors downsample_day,
    which anchors each day's buckets on that day's first candle.
    """
    width = pd.Timedelta(minutes=minutes)
    parts = []
    for day in candles_1m_days.values():
        start = day.index[0]
        parts.append(pd.Series(start + ((day.index - start) // width) * width, index=day.index))
    return pd.concat(parts)

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]

def build_developing_frame(candles_1m_days, minutes: int, strategy, completed, description=""):
    """
    Indicators for the partially formed candle at every minute.

    Row t holds the strategy applied to (every completed bucket before t) plus a
    bucket-to-date candle covering bucket_start..t, so a condition evaluated at t
    sees exactly what a live feed would.
    """
    ohlcv = completed[OHLCV_COLUMNS]
    candles = concat_days(candles_1m_days)

    bucket = bucket_labels(candles_1m_days, minutes)
    grouped = candles.groupby(bucket)
    partial = pd.DataFrame({
        "open": grouped["open"].transform("first"),
        "high": grouped["high"].cummax(),
        "low": grouped["low"].cummin(),
        "close": candles["close"],
        "volume": grouped["volume"].cumsum(),
    })

    positions = completed.index.get_indexer(bucket, method="ffill")
    rows = []
    for i, position in enumerate(tqdm(positions, desc=f"[Developing {description}]", leave=False)):
        if position < 1:
            # No completed bucket yet, so the frame would be the partial candle
            # alone. pandas_ta needs two rows and prints "[!] VWAP requires an
            # ordered DatetimeIndex." on one; this early in the range every
            # indicator would be warmup NaN anyway, so only derive what we can.
            frame = partial.iloc[[i]].copy()
            add_derived_columns(frame)
            rows.append(frame.reindex(columns=completed.columns).iloc[-1])
            continue
        frame = pd.concat([ohlcv.iloc[:position], partial.iloc[[i]]])
        frame.ta.study(strategy)
        add_derived_columns(frame)
        rows.append(frame.iloc[-1])
    return pd.DataFrame(rows, index=candles.index)

def build_timeframes(config, instruments_data, candles_for, progress_label="Processing"):
    """Builds the completed frame per timeframe, plus a developing frame where configured."""
    SavedStrategy = build_strategy(config)
    tasks = [(i, tf) for i in instruments_data for tf in timeframe_entries(i)]

    with tqdm(total=len(tasks), desc=f"[{progress_label}]") as pbar:
        for instrument in instruments_data:
            candles_1m_days = candles_for(instrument)
            completed, developing = {}, {}
            for minutes, is_developing in timeframe_entries(instrument):
                tf = f"{minutes}min"
                pbar.set_description(f"[{progress_label} {instrument['trading_symbol']}/{tf}]")

                df = concat_days(downsample_days(candles_1m_days, tf))
                # Apply the strategy for basic indicators (columns are named via col_names)
                df.ta.study(SavedStrategy)
                add_derived_columns(df)
                completed[tf] = df

                if is_developing:
                    developing[tf] = build_developing_frame(
                        candles_1m_days, minutes, SavedStrategy, df,
                        f"{instrument['trading_symbol']}/{tf}")

                pbar.update(1)

            instrument["timeframes"]["intraday"] = completed
            instrument["developing"] = {"intraday": developing}

def generate_base(config, instruments_data):
    """Generates data for all the timeframes specified and applies the strategy to generate initial indicators"""
    build_timeframes(config, instruments_data,
                     lambda instrument: bracket_by_day(instrument["candles"]))

def generate_base_window(config, instruments_data, window_days: int):
    def windowed(instrument):
        days = bracket_by_day(instrument["candles"])
        return dict(sorted(days.items())[-window_days:])

    build_timeframes(config, instruments_data, windowed)

def generate_signals(config, instruments_data, only=None, progress=None):
    # Retrieve the Timestamp indices for signal generation
    df = pd.DataFrame(index=instruments_data[0]["candles"].index)

    # Apply conditions as new indicators across the main timeframe. Each top-level
    # condition's evaluation also traces every nested condition's score (see
    # evaluate_condition), so df ends up with a column per node in the tree, not
    # just the top-level ids -- this is what lets composite onset plots overlay
    # their children's signals.
    conditions = build_selected_conditions(config, only)
    condition_cols = [c.id for c in conditions]

    children_map = {}
    for condition in conditions:
        for node in condition.walk():
            subs = node.sub_conditions()
            if subs:
                children_map[node.id] = [child.id for child in subs]

    evaluate_conditions(conditions, instruments_data, df, progress)
    return df, condition_cols, children_map

def signal_stats(df: pd.DataFrame, condition_cols: list[str]):
    """
    Per condition: days it was true, sessions it produced a value at all, and
    minutes it was true.

    A session only counts once the condition has a value there, so warmup days
    where a wide EMA is still NaN are excluded from the denominator. The same
    applies to a sequential's later tiers, which are only evaluated on the bars
    where the tier before them opened.
    """
    dates = pd.Series(df.index.date, index=df.index)
    stats = {}
    for col in condition_cols:
        if col not in df.columns:
            raise ColumnNotFoundError(col, df)
        true = df[col] >= 0
        stats[col] = {
            "days": int(dates[true].nunique()),
            "sessions": int(dates[df[col].notna()].nunique()),
            "minutes": int(true.sum()),
        }
    return stats

def format_signal_stat(stat: dict) -> str:
    return f"{stat['days']} / {stat['sessions']} days, {stat['minutes']} min"

def session_blockers(df: pd.DataFrame, condition_cols: list[str], children_map: dict):
    """
    Per condition, per session: which child held it back, and by how much.

    Taken at the bar where the parent scored highest -- the moment it came
    closest to opening. Composite conditions score as the min of their children,
    so the lowest-scoring child there is the one to loosen. Sessions the
    condition already fired on are reported with a blocker of None.
    """
    dates = pd.Series(df.index.date, index=df.index)
    result = {}
    for col in condition_cols:
        children = [c for c in children_map.get(col, []) if c in df.columns]
        if col not in df.columns or not children:
            continue
        per_session = {}
        for day, rows in df.groupby(dates):
            parent = rows[col]
            if not parent.notna().any():
                continue
            best = parent.idxmax()
            if parent.loc[best] >= 0:
                per_session[day] = None
                continue
            scores = rows.loc[best, children]
            if not scores.notna().any():
                continue
            per_session[day] = (scores.idxmin(), float(scores.min()), float(parent.loc[best]))
        result[col] = per_session
    return result

def blocker_tally(blockers: dict) -> list[tuple[str, int]]:
    """Which child blocked the most sessions, worst first."""
    counts = {}
    for entry in blockers.values():
        if entry is not None:
            counts[entry[0]] = counts.get(entry[0], 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])

def find_signal_days(df: pd.DataFrame, condition_cols: list[str]):
    result = {}
    for col in condition_cols:
        if col not in df.columns:
            raise ColumnNotFoundError(col, df)

        true_timestamps = df.index[df[col] >= 0]
        result[col] = pd.DatetimeIndex(
            true_timestamps.to_series().groupby(true_timestamps.date).first()
        )
    return result

def find_valid_days(df: pd.DataFrame, condition_cols: list[str]):
    """
    Every session the condition produced a value, anchored on that session's last
    evaluated bar so a chart drawn there covers the whole day rather than the
    lead-up to a signal. Matches the denominator reported by signal_stats.
    """
    result = {}
    for col in condition_cols:
        if col not in df.columns:
            raise ColumnNotFoundError(col, df)

        evaluated = df.index[df[col].notna()]
        result[col] = pd.DatetimeIndex(
            evaluated.to_series().groupby(evaluated.date).last()
        )
    return result

def evaluate_conditions(conditions: list[Condition], instruments_data: dict, df: pd.DataFrame,
                        progress=None):
    ids = []
    for condition in conditions:
        for node in condition.walk():
            if node.id not in ids:
                ids.append(node.id)
    columns = {id_: [] for id_ in ids}
    total = len(df.index)
    for done, timestamp in enumerate(tqdm(df.index, desc="[Evaluating signals]")):
        if progress is not None and done % 50 == 0:
            progress(done / total if total else 1.0,
                     f"evaluating {done}/{total} candles")
        ctx = MarketContext(instruments_data, timestamp)
        for condition in conditions:
            condition(ctx)
        for id_ in ids:
            # A sequential tier that was short-circuited never ran, so it has no
            # trace entry; NaN records "not evaluated" rather than a real score.
            columns[id_].append(ctx.trace.get(id_, math.nan))
    for id_, values in columns.items():
        df[id_] = values

def append_signal_aggregates(output_df, condition_values, output_timeframe, signal_aggregates):
    days = bracket_by_day(condition_values)
    aggregated_days = []

    for date, day_df in days.items():
        agg = day_df.resample(output_timeframe, origin=day_df.index[0]).agg(signal_aggregates)
        agg.columns = [f"{condition}_{stat}" for condition, stat in agg.columns]
        aggregated_days.append(agg)

    aggregated = pd.concat(aggregated_days)
    return output_df.join(aggregated)