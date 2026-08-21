"""
Logging setup for the whole application.

A packaged application has no console, so everything that used to be printed has
to reach a file instead. ``configure`` is called once by the entrypoint; every
module then uses ``get_logger(__name__)``.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

import app_paths

LOG_FILE = "trading-signals.log"
MAX_BYTES = 2_000_000
BACKUP_COUNT = 5

_configured = False

def configure(level: int = logging.INFO, to_console: bool = True) -> None:
    global _configured
    if _configured:
        return

    app_paths.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(level)

    file_handler = RotatingFileHandler(
        app_paths.log_dir() / LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"))
    root.addHandler(file_handler)

    if to_console:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(levelname)-7s %(name)-18s %(message)s"))
        root.addHandler(console)

    logging.getLogger("nicegui").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)

    _configured = True
    get_logger(__name__).info("logging to %s", app_paths.log_dir() / LOG_FILE)

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
