"""
The live signal engine as a controllable service.

Previously this was a blocking ``while True`` in ``live.py``, which could only
be run as its own process and could not be started or stopped from a UI. Here
the loop owns a thread, exposes ``start``/``stop``, keeps the latest scores for
a dashboard to read, and survives an exception in one cycle instead of dying
silently mid-session.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import date

import pandas as pd
import yaml
from kiteconnect import KiteConnect

import app_paths
from app_logging import get_logger
from condition import disabled_condition_ids
from data_processing import generate_base
from data_retrieval import get_historical, historical_csv_path, parse_instruments
from live_candles import LiveCandleBuilder
from live_evaluation import LiveEvaluator
from login import get_kite
from notifications import Notifier
from streaming import start_ticker

log = get_logger(__name__)

RECOMPUTE_INTERVAL_SECONDS = 30
WINDOW_DAYS = 7
SESSION_START = "09:15"
SESSION_END = "15:30"

class LiveService:
    def __init__(self, recompute_seconds: int = RECOMPUTE_INTERVAL_SECONDS,
                 window_days: int = WINDOW_DAYS):
        self.recompute_seconds = recompute_seconds
        self.window_days = window_days
        self.state = "stopped"          # stopped | starting | running | error
        self.error: str | None = None
        self.last_run: pd.Timestamp | None = None
        self.scores: dict[str, float] = {}
        self.disabled: list[str] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ticker = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            log.info("live service already running")
            return
        self._stop.clear()
        self.error = None
        self.state = "starting"
        self._thread = threading.Thread(target=self._run, name="live-service", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._ticker is not None:
            try:
                self._ticker.close()
            except Exception as error:
                log.warning("closing ticker failed: %s", error)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.state = "stopped"
        log.info("live service stopped")

    def _run(self) -> None:
        try:
            evaluator, builder = self._bootstrap()
        except Exception as error:
            self.state = "error"
            self.error = f"{type(error).__name__}: {error}"
            log.error("live service failed to start\n%s", traceback.format_exc())
            return

        self.state = "running"
        last_recompute = 0.0
        while not self._stop.is_set():
            try:
                builder.drain()
                if time.monotonic() - last_recompute >= self.recompute_seconds:
                    now = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
                    self.scores = evaluator.run_once(now)
                    self.last_run = now
                    last_recompute = time.monotonic()
            except Exception as error:
                # One bad cycle must not kill the engine during market hours.
                self.error = f"{type(error).__name__}: {error}"
                log.error("live cycle failed (continuing)\n%s", traceback.format_exc())
                last_recompute = time.monotonic()
            self._stop.wait(1.0)

    def _bootstrap(self):
        config = yaml.safe_load(app_paths.strategy_path().read_text(encoding="utf-8"))

        self.disabled = disabled_condition_ids(config)
        if self.disabled:
            log.warning("%d condition(s) DISABLED and will not alert: %s",
                        len(self.disabled), ", ".join(self.disabled))

        kite: KiteConnect = get_kite()
        instruments_info = parse_instruments(kite, config)
        token_to_id = {i["token"]: i["id"] for i in instruments_info}
        csv_paths = {i["id"]: historical_csv_path(i["trading_symbol"], i["token"])
                     for i in instruments_info}

        instruments_data = get_historical(
            kite, instruments_info, date.today(), date.today(),
            SESSION_START, SESSION_END, download_data=True, wipe_file=False,
        )
        generate_base(config, instruments_data)

        builder = LiveCandleBuilder(instruments_data, token_to_id, csv_paths)
        notifier = Notifier()
        notifier.reset_daily_state()
        evaluator = LiveEvaluator(config, instruments_data, self.window_days, notifier)

        self._ticker = start_ticker(kite.api_key, kite.access_token,
                                    list(token_to_id.keys()), on_tick=builder.on_tick)
        log.info("live service started (recompute every %ss)", self.recompute_seconds)
        return evaluator, builder

SERVICE = LiveService()
