# Trading Signals

A condition-driven engine for building and running intraday trading strategies against live market data. Raw candles across multiple timeframes and instruments feed a tree of composable conditions (comparisons, AND/OR/NOT logic, rolling-window checks, boosts) that score continuously rather than just firing true/false, so nested rules combine cleanly into higher-level signals. The same strategy definition runs unchanged in three modes: historical backtesting, a live engine against streaming data, and an interactive editor/dashboard.

This is a **signal engine, not an auto-trader** — it surfaces when a strategy's conditions are met; a human still decides whether to place the trade.

## Quick start (running the app)

1. Download the latest Windows build from [Releases](../../releases) and extract the zip.
2. Run `Trading Signals.exe`. On first launch it creates an empty (but valid) strategy/settings config for you to build on.
3. Open the app's credentials screen (or run `main.py credentials` from a source checkout) to store your Kite Connect API key/secret — these are kept in the OS keyring, never written to a config file.
4. Use the editor to define conditions, run a backtest to see how they'd have performed, then switch to live mode during market hours.

## Running from source

Requires Python 3.12+.

```
pip install -r requirements.txt
python main.py            # launch the editor/dashboard (default mode)
python main.py backtest   # run a backtest headlessly
python main.py live       # run the live engine headlessly until interrupted
python main.py doctor     # print resolved paths and credential status
python main.py credentials  # prompt for and store each credential in the OS keyring
```

## Configuration

Strategy logic and app settings are plain YAML, not code:

- **`config/strategy.yaml`** — the condition tree: instruments/timeframes to track, technical indicators to compute, and the definitions/conditions that reference them.
- **`config/settings.yaml`** — dashboard layout, backtest parameters, and notification rules.

The config directory is private to whoever runs the app — it's not part of this repository, and a fresh install starts with an empty one. By default it resolves next to the app (or a per-user data directory when installed), and can be pointed anywhere via the `TRADING_SIGNALS_HOME` environment variable.

## How it works

- **Condition tree** (`condition.py`) — leaf conditions (`above`, `below`, `compare`, `increasing`, crossover checks, etc.) and combinators (`and`, `or`, `not`, windowed quantifiers) build a scored tree. `and`/`or` fold to `min`/`max` of their children, so a parent's score is always consistent with what its children just evaluated.
- **Live vs. closed candles** — each data reference can read either the last fully closed candle or the current, still-forming one (with correct historical offsets either way), so a strategy can mix fast-reacting and confirmation-oriented conditions deliberately.
- **Three run modes, one definition** — `backtest.py` replays history, `live_service.py` runs the same tree against a live tick stream in a background thread, and the `ui/` editor lets you build and inspect both without leaving the app.
- **Dashboard** — configurable panels per condition, with visibility rules and an optional "force-hide after N seconds" timeout so a signal doesn't linger on screen indefinitely.
- **Notifications** — rules fire on a condition's rising/falling/level transitions; an optional sound (volume and pitch adjustable) plays at most once per cycle even if several rules trigger together.

## Credentials

Kite Connect API key/secret and login credentials are stored via the OS keyring (`secrets_store.py`), never in a config file or `.env`. `python main.py doctor` reports what's currently set without revealing values.
