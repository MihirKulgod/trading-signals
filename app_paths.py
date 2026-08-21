"""
Every filesystem location the application uses.

Nothing else in the codebase should build a path from a relative string, because
the correct base directory differs between running from source and running as a
frozen executable (where the working directory is arbitrary and the install
directory is usually read-only).

Resolution order for the base directory:
  1. $TRADING_SIGNALS_HOME, if set
  2. the per-user data directory, when running frozen
  3. the repository root, when a config/ directory sits next to this file
  4. the per-user data directory
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "trading-signals"
HOME_ENV_VAR = "TRADING_SIGNALS_HOME"

_REPO_ROOT = Path(__file__).resolve().parent

def is_frozen() -> bool:
    return getattr(sys, "frozen", False)

def base_dir() -> Path:
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    if not is_frozen() and (_REPO_ROOT / "config").is_dir():
        return _REPO_ROOT
    return Path(user_data_dir(APP_NAME, appauthor=False))

def config_dir() -> Path:
    return base_dir() / "config"

def data_dir() -> Path:
    return base_dir() / "data"

def historical_dir() -> Path:
    return data_dir() / "historical"

def cache_dir() -> Path:
    return base_dir() / "cache"

def output_dir() -> Path:
    return base_dir() / "output"

def log_dir() -> Path:
    return base_dir() / "logs"

def strategy_path() -> Path:
    return config_dir() / "strategy.yaml"

def settings_path() -> Path:
    return config_dir() / "settings.yaml"

def session_path() -> Path:
    """Per-day Kite session state (access token). Not a long-lived secret."""
    return data_dir() / "session.json"

def ensure_dirs() -> None:
    for directory in (config_dir(), historical_dir(), cache_dir(), output_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True)

def describe() -> str:
    mode = "frozen" if is_frozen() else ("repository" if base_dir() == _REPO_ROOT else "user data")
    return f"{base_dir()}  ({mode})"
