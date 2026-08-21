import os
import sys
import shutil
import time
import warnings
from datetime import date, datetime

import pandas as pd
import yaml
from kiteconnect import KiteConnect
from tqdm import tqdm

from data_processing import (append_signal_aggregates, find_signal_days,
                             generate_base, generate_signals)
from data_retrieval import get_historical, parse_instruments
from login import get_kite
from streaming import start_ticker
from visualise import examine_condition

CACHE_DIR = "cache"
SIGNAL_CACHE_PATH = f"{CACHE_DIR}/signals.csv"
CONDITION_COLS_CACHE_PATH = f"{CACHE_DIR}/condition_cols.yaml"
CHILDREN_MAP_CACHE_PATH = f"{CACHE_DIR}/children_map.yaml"

CONFIG_DIR = "config"
CONFIG_PATH = f"{CONFIG_DIR}/strategy.yaml"
SETTINGS_PATH = f"{CONFIG_DIR}/settings.yaml"

reuse_data = True
reuse_signals = False

warnings.filterwarnings("ignore", module="mplfinance")

with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)
with open(SETTINGS_PATH) as f:
    settings = yaml.safe_load(f)

kite : KiteConnect = get_kite()

# Which charts need to be tracked and examined
instruments_info = parse_instruments(kite, config)

instrument_tokens = [instrument["token"] for instrument in instruments_info]
kws = start_ticker(kite.api_key, kite.access_token, instrument_tokens)

time.sleep(5)

sys.exit()

instruments_data = get_historical(
    kite,
    instruments_info,
    datetime.strptime(settings["historical"]["from"], "%Y-%m-%d").date(),
    date.today(),
    "09:15",
    "15:30",
    not reuse_data
)

generate_base(config, instruments_data)

if reuse_signals:
    print("Reading cached signal values..")
    df = pd.read_csv(SIGNAL_CACHE_PATH, index_col="datetime", parse_dates=True)
    with open(CONDITION_COLS_CACHE_PATH) as f:
        condition_cols = yaml.safe_load(f)
    with open(CHILDREN_MAP_CACHE_PATH) as f:
        children_map = yaml.safe_load(f)
else:
    df, condition_cols, children_map = generate_signals(config, instruments_data)

    print("Caching generated signal values..")
    pd.DataFrame(df).to_csv(SIGNAL_CACHE_PATH, index=True, sep=',', encoding="utf-8")
    with open(CONDITION_COLS_CACHE_PATH, 'w') as f:
        yaml.safe_dump(condition_cols, f, default_flow_style=False)
    with open(CHILDREN_MAP_CACHE_PATH, 'w') as f:
        yaml.safe_dump(children_map, f, default_flow_style=False)

days = find_signal_days(df, condition_cols)

# For graphing the signal values
output_instrument_id = settings["display"]["instrument_id"]
output_timeframe = settings["display"]["timeframe"]

# df holds a column per condition node (top-level and nested), so aggregating
# every column -- not just condition_cols -- makes child scores available to plot.
condition_values = df
output_df = next(
    instrument_data["timeframes"]["intraday"][output_timeframe]
    for instrument_data in instruments_data
    if instrument_data["id"] == output_instrument_id
)

signal_aggregates = settings["display"]["signal_aggregates"]
output_df = append_signal_aggregates(output_df, condition_values, output_timeframe, signal_aggregates)

if os.path.isdir("output"):
    print("Removing cached output images..")
    shutil.rmtree("output")

# Plot the onset lead-up for each condition signal
tasks = [(c, t) for c in days for t in days[c]]
for condition_col, timestamp in tqdm(tasks, desc="[Visualizing]"):
    examine_condition(
        settings["display"]["examination_window"],
        condition_col,
        timestamp,
        output_df,
        settings["display"]["display_panels"],
        signal_aggregates,
        children_map.get(condition_col, []),
    )