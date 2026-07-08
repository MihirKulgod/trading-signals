from dotenv import load_dotenv
from datetime import datetime
import os
from kiteconnect import KiteConnect

from data_retrieval import fetch_candles

load_dotenv()

kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
kite.set_access_token(os.getenv("KITE_ACCESS_TOKEN"))

NIFTY_50_INSTR_TOK = "256265"
NIFTY_26_AUG_FUT_INSTR_TOK = "14866434"

NIFTY_50_DATA_FILEPATH = "nifty_50_candles.csv"

minute_prefix = "60" # Can be empty (1m), 3, 5, 10, 15, 30, 60

fetch_candles(
    kite=kite,
    instrument_token=NIFTY_50_INSTR_TOK,
    start_date=datetime(2025, 7, 1),
    end_date=datetime(2026, 7, 3),
    interval=f"{minute_prefix}minute",
    write_path=f"nifty_50_{minute_prefix}m.csv",
    wipe_file=True,
)