from kiteconnect import KiteTicker
from datetime import datetime
from typing import Callable

def start_ticker(api_key, access_token, instrument_tokens, on_tick: Callable[[dict], None]):
    kws = KiteTicker(api_key, access_token)

    def on_connect(ws, response):
        ws.subscribe(instrument_tokens)
        ws.set_mode(ws.MODE_QUOTE, instrument_tokens)

    def on_ticks(ws, ticks):
        for tick in ticks:
            on_tick(tick)

    def on_close(ws, code, reason):
        print(f"Connection closed: {code} {reason}")

    def on_error(ws, code, reason):
        print(f"Error: {code} {reason}")

    def on_reconnect(ws, attempts_count):
        print(f"Reconnecting, attempt {attempts_count}")

    kws.on_connect = on_connect
    kws.on_ticks = on_ticks
    kws.on_close = on_close
    kws.on_error = on_error
    kws.on_reconnect = on_reconnect

    kws.connect(threaded=True)
    return kws