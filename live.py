import time
from datetime import date

import pandas as pd
import yaml
from kiteconnect import KiteConnect

from data_processing import generate_base
from data_retrieval import get_historical, historical_csv_path, parse_instruments
from live_candles import LiveCandleBuilder
from live_evaluation import LiveEvaluator
from login import get_kite
from notifications import Notifier
from streaming import start_ticker

CONFIG_PATH = "config/strategy.yaml"
SETTINGS_PATH = "config/settings.yaml"

RECOMPUTE_INTERVAL_SECONDS = 30
WINDOW_DAYS = 3

def main():
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    with open(SETTINGS_PATH) as f:
        settings = yaml.safe_load(f)

    kite: KiteConnect = get_kite()

    instruments_info = parse_instruments(kite, config)
    token_to_id = {i["token"]: i["id"] for i in instruments_info}
    csv_paths = {i["id"]: historical_csv_path(i["trading_symbol"], i["token"]) for i in instruments_info}
    instrument_tokens = list(token_to_id.keys())

    instruments_data = get_historical(
        kite, instruments_info, date.today(), date.today(), "09:15", "15:30",
        download_data=True, wipe_file=False,
    )
    generate_base(config, instruments_data)

    builder = LiveCandleBuilder(instruments_data, token_to_id, csv_paths)
    notifier = Notifier()
    notifier.reset_daily_state()
    evaluator = LiveEvaluator(config, instruments_data, WINDOW_DAYS, notifier)

    start_ticker(kite.api_key, kite.access_token, instrument_tokens, on_tick=builder.on_tick)

    last_recompute = time.monotonic()
    while True:
        builder.drain()
        time_left = RECOMPUTE_INTERVAL_SECONDS - (time.monotonic() - last_recompute)
        print(f"Time left till compute: {int(time_left)}")
        if time_left <= 0:
            now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
            evaluator.run_once(now)
            last_recompute = time.monotonic()
        time.sleep(1)

if __name__ == "__main__":
    main()
