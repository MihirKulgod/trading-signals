import os
import csv
from datetime import datetime, timedelta, time
import time

CSV_HEADERS = ["datetime", "open", "high", "low", "close", "volume"]

def write_candle(candle, filepath):
    file_exists = os.path.isfile(filepath)
    
    with open(filepath, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADERS)
        writer.writerow([
            candle["date"].isoformat(),
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
            candle["volume"],
        ])
        
def read_candles(filepath, start_date=None, end_date=None):
    candles = []
    with open(filepath, mode="r", newline="") as f:
        reader = csv.DictReader(f)  # uses header row automatically
        for row in reader:
            candle = {
                "date": datetime.fromisoformat(row["datetime"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
            if start_date:
                if candle["date"] < start_date:
                    continue
            if end_date:
                if candle["date"] > end_date:
                    break
            candles.append(candle)
    return candles

def read_candles_days(filepath, start_date=None, end_date=None):
    """
    Returns candles sorted into brackets for each date
    """

    def simplify(candle):
        return {
            "time": candle["date"].time(),
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
        }

    candles = read_candles(filepath, start_date, end_date)
    candles_days = []
    isNewDay = True
    for candle in candles:
        # Looping condition
        if candles_days:
            if candle["date"].date() == candles_days[-1]["date"]:
                # Still on the same day
                candles_days[-1]["candles"].append(simplify(candle))
                continue
            else:
                # Candle is on a new day
                isNewDay = True
        if isNewDay:
            candles_days.append({
                "date": candle["date"].date(),
                "candles": [simplify(candle)],
            })
            isNewDay = False
    return candles_days  

def group_candles(candles, factor=5):
    """
    Groups the candles in a given day by the given factor. Used to create larger timeframes from smaller ones, by factor multiplier
    The input candles must have a 'time' field, not a 'date' field as returned by the KiteConnect API
    """
    


def fetch_candles(kite, instrument_token, start_date, end_date, interval="minute", write_path="", wipe_file=False):
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