import copy
import csv
import os
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from kiteconnect import KiteConnect
from tqdm import tqdm

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

def fetch_historical_candles(kite: KiteConnect, instrument_token, start_date, end_date, interval="minute", write_path="", wipe_file=False):
    """
    Fetches historical candle data for a given instrument token from the Kite API and writes it to a CSV file.

    Start_date and end_date should be datetime objects. The range is inclusive of both dates.
    """
    curr_date = start_date
    
    if wipe_file and write_path:
        write_path = Path(write_path)
        write_path.parent.mkdir(parents=True, exist_ok=True)
        with open(write_path, mode="w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADERS)
    
    total_days = (end_date - start_date).days + 1

    with tqdm(total=total_days, unit="day", desc=f"[Fetching data]") as pbar:
        while curr_date <= end_date:
            # Max 60 days of data can be fetched in one call
            window_end_date = curr_date + timedelta(days=min((end_date - curr_date).days + 1, 60))
            
            display_end_date = window_end_date - timedelta(days=1)  # Adjust for inclusive range
            tqdm.write(f"[Fetching data from {curr_date.day}/{curr_date.month}/{curr_date.year} to {display_end_date.day}/{display_end_date.month}/{display_end_date.year}..]")
            
            candles = kite.historical_data(
                instrument_token=instrument_token,
                from_date=curr_date,
                to_date=window_end_date,
                interval=interval
            )
            if write_path:
                for candle in candles:
                    write_candle(candle, write_path)

            days_advanced = (window_end_date - curr_date).days
            pbar.update(days_advanced)

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
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    # Downloads append, and a re-download of the same day writes those minutes
    # again; the newest copy wins.
    df = df[~df.index.duplicated(keep="last")]

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
    return pd.concat([days_dict[date] for date in days_dict])

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

def parse_instruments(kite: KiteConnect, config):
    """Retrieves the necessary info on which instruments and timeframes are required according to the config"""
    result = []
    exchanges = config["general"]["exchanges"]
    for exchange in exchanges:
        exchange_name = exchange["exchange"]
        instruments = exchange["instruments"]

        for instrument in instruments:
            kite_instrument = resolve_instrument(kite, exchange_name, instrument, pd.Timestamp.now())

            result.append({
                "id": instrument["id"],
                "trading_symbol": kite_instrument["tradingsymbol"],
                "token": kite_instrument["instrument_token"],
                # Copied: generate_base replaces entries in place, and a YAML
                # anchor can make two instruments share one timeframes list.
                "timeframes": copy.deepcopy(instrument["timeframes"]),
            })

            print(f"Parsed Instrument Info = {result[-1]}")
    return result

def historical_csv_path(trading_symbol: str, token) -> str:
    import app_paths
    return str(app_paths.historical_dir() / f"{trading_symbol}-{token}.csv")

class InsufficientHistoryError(Exception):
    """The candles on disk do not span the range a run asked for."""

# Absorbs weekends and public holidays, where an edge of the requested range
# legitimately has no session.
COVERAGE_TOLERANCE = timedelta(days=4)

def check_coverage(candles, start_date: date, end_date: date, trading_symbol: str) -> None:
    """
    Stop a run whose cached candles miss part of the requested range.

    Silently proceeding is worse than failing: the run would report a period it
    never evaluated, and indicators would warm up from the wrong first bar.
    """
    if candles.empty:
        raise InsufficientHistoryError(
            f"{trading_symbol}: no cached candles at all, but {start_date}..{end_date} "
            "was requested. Re-run with candle download enabled."
        )

    first, last = candles.index[0].date(), candles.index[-1].date()
    missing = []
    if first - start_date > COVERAGE_TOLERANCE:
        missing.append(f"starts {(first - start_date).days} days late ({first})")
    if end_date - last > COVERAGE_TOLERANCE:
        missing.append(f"ends {(end_date - last).days} days early ({last})")
    if missing:
        raise InsufficientHistoryError(
            f"{trading_symbol}: cached candles cover {first}..{last}, but "
            f"{start_date}..{end_date} was requested — {'; '.join(missing)}. "
            "Re-run with candle download enabled, or narrow the date range."
        )

def get_historical(kite: KiteConnect, instruments_info, start_date: date, end_date: date, start_time, stop_time, download_data=True, wipe_file=True, require_coverage=True):
    result = []
    if not download_data:
        print("Skipping data download..")
    for instrument in tqdm(instruments_info, desc="[Instrument data download]", disable=not download_data):
        token = instrument["token"]
        output_path = historical_csv_path(instrument["trading_symbol"], token)
        if download_data:
            fetch_historical_candles(
                kite,
                token,
                start_date,
                end_date,
                "minute",
                output_path,
                wipe_file,
            )
        candles = read_candles(output_path)
        outside_session = candles[~candles.index.isin(candles.between_time(start_time, stop_time).index)]
        print(f"{len(outside_session)} candles were found outside the specified range of {start_time}-{stop_time} [{instrument["trading_symbol"]}]")
        candles = candles.between_time(start_time, stop_time)

        # Live asks for a generous span and judges sufficiency by session count
        # instead, so a young futures contract does not abort the engine.
        if require_coverage:
            check_coverage(candles, start_date, end_date, instrument["trading_symbol"])
        candles = candles[(candles.index.date >= start_date) & (candles.index.date <= end_date)]

        instrument.pop("token")
        instrument["candles"] = candles

        result.append(instrument)
    return result