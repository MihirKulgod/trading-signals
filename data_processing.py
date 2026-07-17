
import pandas as pd
import importlib.metadata # pandas-ta-openbb has a bug, requiring this to be imported first 
import os
from tqdm import tqdm
import pandas_ta as ta
import mplfinance as mpf

from condition import build_condition, Condition, MarketContext, ColumnNotFoundError
from data_retrieval import bracket_by_day, concat_days, downsample_days
from visualise import try_addplot

def generate_base(config, instruments_data):
    """Generates data for all the timeframes specified and applies the strategy to generate initial indicators"""
    # Prepare aliases to replace auto-generated headers
    aliases = []
    for indicator in config["ta"]:
        a = indicator["alias"]
        if isinstance(a, list):
            aliases.extend(a)
        else:
            aliases.append(a)

    # Prepare strategy to generate basic indicators
    SavedStrategy = ta.Strategy(
        name=config["name"],
        description=config["description"],
        ta=config["ta"],
    )
    
    tasks = [(instrument, tf) for instrument in instruments_data for tf in instrument["timeframes"]["intraday"]]
    with tqdm(total=len(tasks), desc="[Processing]") as pbar:
        for i, instrument in enumerate(instruments_data):
            candles_1m_days = bracket_by_day(instrument["candles"])
            for j, timeframe_minutes in enumerate(instrument["timeframes"]["intraday"]):
                # Generate higher timeframes than the base provided
                tf = f"{timeframe_minutes}min"
                pbar.set_description(f"[Processing {instrument["trading_symbol"]}/{tf}]")
                df = concat_days(downsample_days(candles_1m_days, tf))
                old_cols = df.columns

                # Apply the strategy for basic indicators
                df.ta.study(SavedStrategy)
                new_cols = df.columns

                # Replace auto-generated headers with aliases
                col_map = dict(zip(new_cols[len(old_cols):], aliases))
                df.rename(columns=col_map, inplace=True)

                # Map list of timeframes to the generated data
                instrument["timeframes"]["intraday"][j] = {
                    tf: df
                }

                pbar.update(tasks.index((instrument, timeframe_minutes))+1)

            # Update the instruments_data reference
            result = {}
            for tf in instrument["timeframes"]["intraday"]:
                result.update(tf)
            instrument["timeframes"]["intraday"] = result
            instruments_data[i] = instrument

def generate_signals(config, instruments_data):    
    # Retrieve the Timestamp indices for signal generation
    df = pd.DataFrame(index=instruments_data[0]["candles"].index)

    # Apply conditions as new indicators across the main timeframe
    condition_cols = []
    for c in config["conditions"]:
        condition_cols.append(c["id"])
        condition = build_condition(c)
        evaluate_condition(condition, instruments_data, df)
    return df, condition_cols

def find_signal_onsets(df: pd.DataFrame, condition_cols: list[str]):
    result = {}
    for col in condition_cols:
        if col not in df.columns:
            raise ColumnNotFoundError(col, df)
        onset_timestamps = []
        onsets = (df[col] >= 0) & (df[col].shift(1) < 0)
        onset_timestamps = df.index[onsets]
        result[col] = onset_timestamps
    return result

def evaluate_condition(condition: Condition, instruments_data: dict, df: pd.DataFrame):
    scores = []
    for timestamp in tqdm(df.index, desc=f"[Evaluating signal {condition.id}]"):
        ctx = MarketContext(instruments_data, timestamp)
        scores.append(condition(ctx))
    df[condition.id] = scores

# Aggregate functions to run on signals when viewing a timeframe larger than 1m
SIGNAL_AGGREGATES = ["min", "max", "last"]

def append_signal_aggregates(output_df, condition_values, output_timeframe):
    days = bracket_by_day(condition_values)
    aggregated_days = []

    for date, day_df in days.items():
        agg = day_df.resample(output_timeframe, origin=day_df.index[0]).agg(SIGNAL_AGGREGATES)
        agg.columns = [f"{condition}_{stat}" for condition, stat in agg.columns]
        aggregated_days.append(agg)

    aggregated = pd.concat(aggregated_days)
    return output_df.join(aggregated)

def examine_condition(examination_window: int, condition_id: str, timestamp: pd.Timestamp, df: pd.DataFrame, display_panels: list[dict]):
    output_path = f"output/{condition_id}/{timestamp.date()}/{timestamp.time().strftime('%H:%M')}.png"

    pos = df.index.get_indexer([timestamp], method="bfill")[0]
    if pos == -1:
        print(f"Couldn't find a suitable timestamp to plot {condition_id} onset at {timestamp}!")
        return

    window = df.iloc[max(0, pos - (examination_window - 1)):pos + 1]

    addplots = []

    for i, panel in enumerate(display_panels):
        for item in panel.items():
            id, color = item
            try_addplot(id, window, addplots, i, None, color)

    for agg in SIGNAL_AGGREGATES:
        col = f"{condition_id}_{agg}"
        try_addplot(col, window, addplots, 3, "Condition", "black")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    mpf.plot(
        window,
        type="candle",
        style="yahoo",
        volume=False,
        columns=["open", "high", "low", "close", "volume"],
        addplot=addplots,
        figsize=(20, 8),
        savefig=dict(fname=output_path, dpi=300),
    )