import queue
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from data_retrieval import write_candle

@dataclass
class DevelopingCandle:
    minute: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

class LiveCandleBuilder:
    def __init__(self, instruments_data: list[dict], token_to_id: dict[int, str],
                 csv_paths: dict[str, str], session_start="09:15", session_end="15:30"):
        self.instruments_data = instruments_data
        self.token_to_id = token_to_id
        self.csv_paths = csv_paths
        self.session_start = datetime.strptime(session_start, "%H:%M").time()
        self.session_end = datetime.strptime(session_end, "%H:%M").time()
        self.queue: "queue.Queue[dict]" = queue.Queue()
        self.developing: dict[int, DevelopingCandle] = {}
        self.last_cumulative_volume: dict[int, float] = {}

    def on_tick(self, tick: dict) -> None:
        self.queue.put(tick)

    def drain(self) -> None:
        while True:
            try:
                tick = self.queue.get_nowait()
            except queue.Empty:
                break
            instrument_token = tick["instrument_token"]
            if instrument_token not in self.token_to_id:
                continue
            timestamp = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
            self._apply_tick(instrument_token, timestamp, tick)

    def _apply_tick(self, instrument_token: int, timestamp: pd.Timestamp, tick: dict) -> None:
        if not (self.session_start <= timestamp.time() < self.session_end):
            return

        ltp = tick["last_price"]
        volume_traded = tick.get("volume_traded")
        volume_delta = 0.0
        if volume_traded is not None:
            previous = self.last_cumulative_volume.get(instrument_token, volume_traded)
            volume_delta = max(volume_traded - previous, 0.0)
            self.last_cumulative_volume[instrument_token] = volume_traded

        minute = timestamp.floor("min")
        candle = self.developing.get(instrument_token)

        if candle is None or minute > candle.minute:
            if candle is not None:
                self._finalize_candle(self.token_to_id[instrument_token], candle)
            self.developing[instrument_token] = DevelopingCandle(
                minute=minute, open=ltp, high=ltp, low=ltp, close=ltp, volume=volume_delta,
            )
        else:
            candle.high = max(candle.high, ltp)
            candle.low = min(candle.low, ltp)
            candle.close = ltp
            candle.volume += volume_delta

    def _finalize_candle(self, instrument_id: str, candle: DevelopingCandle) -> None:
        write_candle({
            "date": candle.minute.to_pydatetime(),
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }, self.csv_paths[instrument_id])

        instrument = next(i for i in self.instruments_data if i["id"] == instrument_id)
        new_row = pd.DataFrame(
            [[candle.open, candle.high, candle.low, candle.close, candle.volume]],
            columns=["open", "high", "low", "close", "volume"],
            index=pd.DatetimeIndex([candle.minute], name="datetime"),
        )
        instrument["candles"] = pd.concat([instrument["candles"], new_row]).sort_index()
