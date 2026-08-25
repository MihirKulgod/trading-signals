import argparse
import json
import os
import shutil
import warnings
from datetime import date, datetime

import pandas as pd
import yaml
from tqdm import tqdm

import app_paths
from app_logging import get_logger
from condition import (build_selected_conditions, disabled_condition_ids,
                       reference_warnings, reset_reference_warnings)
from data_processing import (active_windows, append_signal_aggregates, blocker_tally,
                             find_signal_days, find_valid_days, format_signal_stat,
                             generate_base, generate_signals, session_blockers,
                             signal_stats)
from data_retrieval import get_historical, parse_instruments
from login import get_kite
from visualise import examine_condition

CONFIG_PATH = app_paths.strategy_path()
SETTINGS_PATH = app_paths.settings_path()

CACHE_DIR = app_paths.cache_dir()
SIGNAL_CACHE_PATH = CACHE_DIR / "signals.csv"
CONDITION_COLS_CACHE_PATH = CACHE_DIR / "condition_cols.yaml"
CHILDREN_MAP_CACHE_PATH = CACHE_DIR / "children_map.yaml"
PATCHED_BLOCKS_PATH = CACHE_DIR / "patched_blocks.yaml"

OUTPUT_DIR = app_paths.output_dir()

SESSION_START = "09:15"
SESSION_END = "15:30"

def load_cached_signals():
    if not os.path.isfile(SIGNAL_CACHE_PATH):
        raise FileNotFoundError(
            f"No cached signals at {SIGNAL_CACHE_PATH}; run once without --reuse-signals"
        )
    df = pd.read_csv(SIGNAL_CACHE_PATH, index_col="datetime", parse_dates=True)
    with open(CONDITION_COLS_CACHE_PATH) as f:
        condition_cols = yaml.safe_load(f)
    with open(CHILDREN_MAP_CACHE_PATH) as f:
        children_map = yaml.safe_load(f)
    return df, condition_cols, children_map

def save_cached_signals(df, condition_cols, children_map):
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(SIGNAL_CACHE_PATH, index=True, sep=',', encoding="utf-8")
    with open(CONDITION_COLS_CACHE_PATH, 'w') as f:
        yaml.safe_dump(condition_cols, f, default_flow_style=False)
    with open(CHILDREN_MAP_CACHE_PATH, 'w') as f:
        yaml.safe_dump(children_map, f, default_flow_style=False)
    # A whole-strategy run makes the cache one coherent evaluation again.
    if os.path.isfile(PATCHED_BLOCKS_PATH):
        os.remove(PATCHED_BLOCKS_PATH)

def patched_blocks() -> list:
    if not os.path.isfile(PATCHED_BLOCKS_PATH):
        return []
    with open(PATCHED_BLOCKS_PATH) as f:
        return yaml.safe_load(f) or []

def merge_cached_signals(df, children_map, block_id):
    """
    Fold one block's freshly evaluated columns into the cached run.

    A block run covers part of the strategy over its own date range, so its
    columns replace whatever the cache held and read NaN outside that range --
    the run genuinely did not evaluate them there. Every other column is left
    alone, and condition_cols still describes the whole-strategy run, so
    --reuse-signals keeps working; the patched ids are recorded so it can say
    the file is no longer a single evaluation.
    """
    if not os.path.isfile(SIGNAL_CACHE_PATH):
        save_cached_signals(df, list(df.columns), children_map)
        return "created"

    cached, condition_cols, cached_children = load_cached_signals()
    # Read the marker before anything opens it for writing.
    patched = sorted(set(patched_blocks()) | {block_id})

    index = cached.index.union(df.index)
    incoming = df.reindex(index)
    merged = cached.reindex(index).drop(columns=list(df.columns), errors="ignore")
    merged = pd.concat([merged, incoming], axis=1)[
        list(dict.fromkeys(list(cached.columns) + list(df.columns)))]
    cached_children.update(children_map)

    os.makedirs(CACHE_DIR, exist_ok=True)
    merged.to_csv(SIGNAL_CACHE_PATH, index=True, sep=',', encoding="utf-8")
    with open(CHILDREN_MAP_CACHE_PATH, 'w') as f:
        yaml.safe_dump(cached_children, f, default_flow_style=False)
    with open(PATCHED_BLOCKS_PATH, 'w') as f:
        yaml.safe_dump(patched, f, default_flow_style=False)
    return f"merged {len(df.columns)} column(s)"

def tier_columns(df, condition_cols, children_map):
    """Direct children of each top-level condition (the tiers of a sequential)."""
    return [tier for cid in condition_cols
            for tier in children_map.get(cid, []) if tier in df.columns]

def report_days(df, condition_cols, days, children_map=None):
    stats = signal_stats(df, list(days))
    blockers = session_blockers(df, list(days), children_map or {})
    print(f"\nEvaluated {len(df)} candles across {len(condition_cols)} conditions")
    for condition_id in condition_cols:
        print(f"  {condition_id}: {format_signal_stat(stats[condition_id])}")
        tally = blocker_tally(blockers.get(condition_id, {}))
        if tally:
            worst = ", ".join(f"{child} ({count})" for child, count in tally[:3])
            print(f"      held back by: {worst}")
        for tier in (children_map or {}).get(condition_id, []):
            if tier in stats:
                print(f"      {tier}: {format_signal_stat(stats[tier])}")

def report_reference_warnings():
    problems = reference_warnings()
    if not problems:
        return
    print(f"\n{len(problems)} reference problem(s) seen while evaluating:")
    for (reason, instrument_id, timeframe, col_name), timestamp in problems:
        print(f"  {reason}: {instrument_id}/{timeframe}/{col_name} (first seen at {timestamp})")
    print("Scores that depended on these references are unreliable.")

WINDOWS_FILE = "windows.json"

def save_active_windows(condition_id, sessions: dict) -> None:
    """
    Write the stretches a condition was true next to its charts, so the viewer
    can list them without reloading the signal frame.
    """
    folder = OUTPUT_DIR / str(condition_id)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        str(day): [[start.strftime("%H:%M"), end.strftime("%H:%M"), int(bars)]
                   for start, end, bars in runs]
        for day, runs in sessions.items()
    }
    (folder / WINDOWS_FILE).write_text(json.dumps(payload, indent=1), encoding="utf-8")

def chart_name(timestamp, minutes: int, blocker) -> str:
    """Filename stem stating the session's outcome."""
    if minutes:
        return f"{timestamp.date()} HIT {minutes}min"
    if blocker:
        return f"{timestamp.date()} blocked by {blocker[0]}"
    return f"{timestamp.date()}"

def render_day_charts(settings, instruments_data, df, children_map, days):
    display = settings["display"]
    output_timeframe = display["timeframe"]
    signal_aggregates = display["signal_aggregates"]

    output_df = next(
        instrument["timeframes"]["intraday"][output_timeframe]
        for instrument in instruments_data
        if instrument["id"] == display["instrument_id"]
    )
    output_df = append_signal_aggregates(output_df, df, output_timeframe, signal_aggregates)

    # Clear only the conditions about to be re-rendered, so running one block
    # does not wipe images belonging to every other condition.
    for condition_id in days:
        stale = OUTPUT_DIR / str(condition_id)
        if stale.is_dir():
            shutil.rmtree(stale)

    dates = pd.Series(df.index.date, index=df.index)
    minutes_by_day = {c: (df[c] >= 0).groupby(dates).sum().to_dict() for c in days}
    blockers = session_blockers(df, list(days), children_map)

    windows = active_windows(df, list(days))
    for condition_id in days:
        save_active_windows(condition_id, windows.get(condition_id, {}))

    tasks = [(c, t) for c in days for t in days[c]]
    for condition_col, timestamp in tqdm(tasks, desc="[Visualizing]"):
        examine_condition(
            display["examination_window"],
            condition_col,
            timestamp,
            output_df,
            display["display_panels"],
            signal_aggregates,
            children_map.get(condition_col, []),
            name=chart_name(
                timestamp,
                int(minutes_by_day[condition_col].get(timestamp.date(), 0)),
                blockers.get(condition_col, {}).get(timestamp.date()),
            ),
        )

def report_selection(config, only):
    selected = [c.id for c in build_selected_conditions(config, only)]
    print(f"\nEvaluating {len(selected)} condition(s): {selected}")
    if only:
        print("  (--only given; the enabled toggle is ignored)")
    else:
        skipped = disabled_condition_ids(config)
        if skipped:
            print(f"  skipping {len(skipped)} disabled: {skipped}")
    return selected

def run_backtest(config, settings, start_date: date, end_date: date,
                 download_data=False, reuse_signals=False, visualize=True, only=None,
                 progress=None, cache_signals=True, chart_valid_days=False,
                 merge_block=None):
    selected = report_selection(config, only)
    kite = get_kite()

    instruments_info = parse_instruments(kite, config)
    instruments_data = get_historical(
        kite, instruments_info, start_date, end_date,
        SESSION_START, SESSION_END, download_data,
    )

    generate_base(config, instruments_data)

    if reuse_signals:
        print("Reading cached signal values..")
        df, condition_cols, children_map = load_cached_signals()
        if set(condition_cols) != set(selected):
            raise ValueError(
                f"cached signals cover {sorted(condition_cols)} but this run selects "
                f"{sorted(selected)}; re-run without --reuse-signals"
            )
        patched = patched_blocks()
        if patched:
            print(f"WARNING: the cache has been patched by single-block runs "
                  f"({', '.join(patched)}), so it is no longer one evaluation. "
                  "Re-run without --reuse-signals for coherent results.")
    else:
        reset_reference_warnings()
        df, condition_cols, children_map = generate_signals(config, instruments_data, only, progress)
        if merge_block is not None:
            print(f"Merging {merge_block} into the cached signal values..")
            print(f"  {merge_cached_signals(df, children_map, merge_block)}")
        elif cache_signals:
            print("Caching generated signal values..")
            save_cached_signals(df, condition_cols, children_map)

    chart_cols = condition_cols + tier_columns(df, condition_cols, children_map)
    days = find_signal_days(df, chart_cols)
    report_days(df, condition_cols, days, children_map)
    report_reference_warnings()

    if visualize:
        # chart_valid_days draws every session the condition was evaluated, not
        # just the ones it fired on, so a single block can be reviewed over time.
        charted = find_valid_days(df, chart_cols) if chart_valid_days else days
        render_day_charts(settings, instruments_data, df, children_map, charted)

    return df, condition_cols, children_map, days

def parse_args(default_start: str):
    parser = argparse.ArgumentParser(
        description="Run a historical backtest of the configured strategy.")
    parser.add_argument("--download", action="store_true",
                        help="re-download historical candles instead of reusing the cached CSVs")
    parser.add_argument("--reuse-signals", action="store_true",
                        help="reuse cached condition scores instead of re-evaluating them")
    parser.add_argument("--no-visualize", dest="visualize", action="store_false",
                        help="skip rendering the per-onset charts")
    parser.add_argument("--from", dest="start", default=default_start, metavar="YYYY-MM-DD",
                        help=f"first date to evaluate (default: {default_start})")
    parser.add_argument("--to", dest="end", default=None, metavar="YYYY-MM-DD",
                        help="last date to evaluate (default: today)")
    parser.add_argument("--only", nargs="+", default=None, metavar="ID",
                        help="evaluate only these condition ids, ignoring the enabled toggle")
    return parser.parse_args()

def main():
    warnings.filterwarnings("ignore", module="mplfinance")

    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    with open(SETTINGS_PATH) as f:
        settings = yaml.safe_load(f)

    args = parse_args(settings["historical"]["from"])
    start_date = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()

    run_backtest(
        config, settings, start_date, end_date,
        download_data=args.download,
        reuse_signals=args.reuse_signals,
        visualize=args.visualize,
        only=args.only,
    )

if __name__ == "__main__":
    main()
