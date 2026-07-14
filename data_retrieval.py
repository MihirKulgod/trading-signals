import os
import csv
import time

import pandas as pd

from kiteconnect import KiteConnect
from datetime import timedelta, time

CSV_HEADERS = ["datetime", "open", "high", "low", "close", "volume"]
TIMEZONE = "Asia/Kolkata"

def get_instruments(kite: KiteConnect, exchange: str) -> pd.DataFrame:
    return pd.DataFrame(kite.instruments(exchange))

def write_candle(candle, filepath):
    file_exists = os.path.isfile(filepath)
    
    with open(filepath, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADERS)
        writer.writerow([
            candle["date"].replace(tzinfo=None).isoformat(),
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
            candle["volume"],
        ])

def fetch_candles(kite: KiteConnect, instrument_token, start_date, end_date, interval="minute", write_path="", wipe_file=False):
    """
    Fetches historical candle data for a given instrument token from the Kite API and writes it to a CSV file.

    Start_date and end_date should be datetime objects. The range is inclusive of both dates.
    """
    curr_date = start_date
    
    if wipe_file and write_path:
        with open(write_path, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)

    while curr_date <= end_date:
        # Max 60 days of data can be fetched in one call
        window_end_date = curr_date + timedelta(days=min((end_date - curr_date).days + 1, 60))
        
        display_end_date = window_end_date - timedelta(days=1)  # Adjust for inclusive range
        print(f"Fetching data from {curr_date.day}/{curr_date.month}/{curr_date.year} to {display_end_date.day}/{display_end_date.month}/{display_end_date.year}..")
        
        candles = kite.historical_data(
            instrument_token=instrument_token,
            from_date=curr_date,
            to_date=window_end_date,
            interval=interval
        )
        if write_path:
            for candle in candles:
                write_candle(candle, write_path)

        curr_date = window_end_date

        if curr_date >= end_date:
            break

        # To avoid hitting rate limits of the API
        time.sleep(0.4)
        
def read_candles(filepath, start_date=None, end_date=None):
    """
    Reads candles from a .csv file with columns [datetime, open, high, low, close, volume]
    Inclusive of both start_date and end_date, down to the exact timestamp given.
    """
    df = pd.read_csv(filepath)
    df["datetime"] = (
        pd.to_datetime(df["datetime"], utc=True)
          .dt.tz_convert(TIMEZONE)
          .dt.tz_localize(None)
    )
    df = df.set_index("datetime").sort_index()

    if start_date is not None:
        df = df[df.index >= pd.Timestamp(start_date)]
    if end_date is not None:
        df = df[df.index <= pd.Timestamp(end_date)]

    return df

def bracket_by_day(df):
    return {date: group for date, group in df.groupby(df.index.date)}
    
# Aggregate functions to use for each column when downsampling a day's candle data
OHLC_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

def downsample_day(day_df, timeframe):
    # Origin of resampling = time of day's first candle
    return day_df.resample(timeframe, origin=day_df.index[0]).agg(OHLC_AGG).dropna()

def downsample_days(days_dict, timeframe):
    return {date: downsample_day(day_df, timeframe) for date, day_df in days_dict.items()}

# Handling rolling instruments
def near_month_rule(candidates, target_date):
    active = candidates[candidates["expiry"].apply(pd.Timestamp) >= target_date]
    return active.sort_values("expiry").iloc[0]

def next_month_rule(candidates, target_date):
    active = candidates[candidates["expiry"].apply(pd.Timestamp) >= target_date].sort_values("expiry")
    return active.iloc[1] if len(active) > 1 else active.iloc[0]

EXPIRY_RULE_REGISTRY = {
    "near_month": near_month_rule,
    "next_month": next_month_rule,
}

def concat_days(days_dict, start_date=None, end_date=None):
    """
    Selects a range of days from a {date: DataFrame} dict and concatenates
    them into a single DataFrame, in chronological order.
    Inclusive of both start_date and end_date.
    """
    start = pd.Timestamp(start_date).date() if start_date is not None else None
    end = pd.Timestamp(end_date).date() if end_date is not None else None

    selected_dates = sorted(
        date for date in days_dict
        if (start is None or date >= start) and (end is None or date <= end)
    )

    return pd.concat([days_dict[date] for date in selected_dates])

def resolve_instrument(kite: KiteConnect, exchange: str, spec: dict, target_date=None) -> dict:
    """spec is one instrument entry from the config. Works for any index/segment."""
    instruments_df = get_instruments(kite, exchange)

    if "trading_symbol" in spec:
        # Non-rolling instrument (a spot index, e.g.)
        match = instruments_df[instruments_df["tradingsymbol"] == spec["trading_symbol"]]
        return match.iloc[0].to_dict()

    if "name" in spec and "instrument_type" in spec:
        # Rolling instrument (futures/options), resolved via a named rule
        candidates = instruments_df[
            (instruments_df["name"] == spec["name"]) &
            (instruments_df["instrument_type"] == spec["instrument_type"])
        ]
        rule = spec.get("expiry_rule", "near_month")
        return EXPIRY_RULE_REGISTRY[rule](candidates, pd.Timestamp(target_date or pd.Timestamp.now())).to_dict()

    raise ValueError(f"Don't know how to resolve instrument spec: {spec}")

def parse_instruments(config, kite: KiteConnect):
    
    result = []
    exchanges = config["general"]["exchanges"]
    for exchange in exchanges:
        exchange_name = exchange["exchange"]
        instruments = exchange["instruments"]

        for instrument in instruments:
            kite_instrument = resolve_instrument(kite, exchange_name, instrument, pd.Timestamp.now())
            print(f"------\\\nInstrument Spec = {instrument}\nInstrument Found = {kite_instrument}\n------/")

            result.append({
                "id": instrument["id"],
                "trading_symbol": kite_instrument["tradingsymbol"],
                "token": kite_instrument["instrument_token"],
                "timeframes": instrument["timeframes"],
            })
    return result


            