"""
The Backtest and Live tabs.

These are the parts of the UI that drive long-running work, so nothing here
calls the engine directly: a backtest is handed to ``jobs.RUNNER`` and the page
polls the resulting ``Job`` on a timer, and the live engine is the separate
``live_service.SERVICE`` that this page only starts, stops and reads.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from urllib.parse import quote

import yaml
from nicegui import ui

import app_paths
import notifications
import sound
from app_logging import get_logger
from data_processing import format_signal_stat, signal_stats
from jobs import RUNNER

log = get_logger(__name__)

BACKTEST_JOB = "backtest"
MUTED = "text-sm text-gray-300"
SUBCARD_BG = "#37455C"
SELECTED_BG = "rgba(205, 41, 72, 0.25)"   # tint of the primary colour, for the current chart in a list

def _load_configs():
    config = yaml.safe_load(app_paths.strategy_path().read_text(encoding="utf-8"))
    settings = yaml.safe_load(app_paths.settings_path().read_text(encoding="utf-8"))
    return config, settings

def _settings_doc():
    """The live ruamel settings document the editor tabs share."""
    from ui import app

    return app.DOCS["settings"]

def _strategy_doc():
    from ui import app

    return app.DOCS["strategy"]

def _persist_settings() -> None:
    from ui import persistence

    doc = _settings_doc()
    try:
        persistence.validate_settings(doc)
    except Exception as e:
        ui.notify(f"settings invalid — not saved: {e}", type="negative", timeout=6000)
        return
    try:
        persistence.save_document(doc, app_paths.settings_path())
    except persistence.ExternalChangeError as e:
        ui.notify(f"{e} Use Reload in the header.", type="negative",
                  timeout=0, close_button="OK")

def backtest_options(settings) -> dict:
    """Run options as stored in settings, shared by the tab and single-block runs."""
    historical = settings.get("historical", {}) or {}
    backtest = settings.get("backtest", {}) or {}
    return {
        "start": historical.get("from") or str(date.today()),
        "end": historical.get("to") or "",
        "download": bool(backtest.get("download", False)),
        "reuse_signals": bool(backtest.get("reuse_signals", False)),
        "visualize": bool(backtest.get("visualize", False)),
    }

def _run_backtest_job():
    """Built here so the worker thread re-reads config at the moment it starts."""
    def target(job):
        import backtest

        job.report(0.0, "loading configuration")
        config, settings = _load_configs()
        options = backtest_options(settings)
        start = datetime.strptime(options["start"], "%Y-%m-%d").date()
        end = datetime.strptime(options["end"], "%Y-%m-%d").date() if options["end"] else date.today()

        job.report(0.0, "fetching candles and building indicators")
        df, cols, children, days = backtest.run_backtest(
            config, settings, start, end,
            download_data=options["download"],
            reuse_signals=options["reuse_signals"],
            visualize=options["visualize"],
            progress=job.report, chart_valid_days=True,
        )
        stats = signal_stats(df, list(days))
        return {
            "candles": len(df),
            "days": {c: stats[c] for c in cols},
            "tiers": {t: s for t, s in stats.items() if t not in cols},
            "children": children,
        }
    return target

def run_single_condition(strategy_doc, node_id: str):
    """
    Backtest one condition block on its own.

    The block is promoted to be the only top-level condition of a throwaway copy
    of the strategy, so the ordinary pipeline runs unchanged.
    """
    from condition import find_condition_spec

    config = json.loads(json.dumps(strategy_doc))  # plain dicts, detached from the editor
    spec = find_condition_spec(config, node_id)
    if spec is None:
        raise ValueError(f"condition {node_id!r} not found in the strategy")
    config["conditions"] = [spec]

    def target(job):
        import backtest

        job.report(0.0, f"preparing {node_id}")
        _, settings = _load_configs()
        options = backtest_options(settings)
        start = datetime.strptime(options["start"], "%Y-%m-%d").date()
        end = datetime.strptime(options["end"], "%Y-%m-%d").date() if options["end"] else date.today()

        # reuse_signals is forced off (the cache covers the whole strategy, not
        # this block); charts follow the same 'render charts' setting as any
        # other backtest, one per session so the block can be reviewed across
        # the whole range when it's on.
        df, cols, children, days = backtest.run_backtest(
            config, settings, start, end,
            download_data=options["download"], reuse_signals=False, visualize=options["visualize"],
            progress=job.report, cache_signals=False, chart_valid_days=True,
            merge_block=node_id,
        )
        stats = signal_stats(df, list(days))
        return {
            "candles": len(df),
            "days": {c: stats[c] for c in cols},
            "tiers": {t: s for t, s in stats.items() if t not in cols},
            "children": children,
        }

    return RUNNER.submit(f"{BACKTEST_JOB}:{node_id}", target)

@ui.refreshable
def backtest_tab() -> None:
    from ruamel.yaml.comments import CommentedMap

    doc = _settings_doc()
    historical = doc.setdefault("historical", CommentedMap())
    backtest = doc.setdefault("backtest", CommentedMap())
    historical.setdefault("from", str(date.today()))
    historical.setdefault("to", "")
    for key in ("download", "reuse_signals", "visualize"):
        backtest.setdefault(key, False)

    def edit(target, key):
        def handler(e):
            target[key] = e.value
            _persist_settings()
        return handler

    with ui.card().classes("w-full"):
        ui.label("Run a historical backtest").classes("font-medium")
        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.input(label="from (YYYY-MM-DD)", value=historical["from"],
                     on_change=edit(historical, "from")).props("dense")
            ui.input(label="to (blank = today)", value=historical["to"],
                     on_change=edit(historical, "to")).props("dense")
            ui.switch("download candles", value=backtest["download"],
                      on_change=edit(backtest, "download")) \
                .tooltip("On: fetch candles from Kite. Off: use the cached CSVs.")
            ui.switch("reuse cached signals", value=backtest["reuse_signals"],
                      on_change=edit(backtest, "reuse_signals"))
            ui.switch("render charts", value=backtest["visualize"],
                      on_change=edit(backtest, "visualize"))
        with ui.row().classes("items-center gap-2"):
            ui.button("Run backtest", icon="play_arrow", on_click=_start_backtest)
            ui.button("Cancel", icon="stop", on_click=_cancel_backtest).props("flat")

    _backtest_status()
    _signal_inspector()
    _indicator_inspector()

# --- cached-signal inspector -----------------------------------------------

# The frame is ~70 MB, so it is read once and kept until the file changes.
_SIGNALS: dict = {"key": None, "frame": None}
INSPECT: dict = {"column": None, "position": None, "residual": 0.0}
# Handles from the last build, so stepping repaints instead of rebuilding: the
# panel holds a select with hundreds of options and rebuilding it per row makes
# scrolling stutter.
_PANEL: dict = {"value": None, "caption": None, "counter": None, "moment": None}

# A mouse detent reports one large deltaY, but how large depends on the browser
# and OS (53, 100, 120 ...), so anything past DETENT counts as exactly one row
# and one minute per click holds on any device. Trackpads instead emit a stream
# of small values, which accumulate, carrying the remainder so that a slow drag
# still eventually moves.
WHEEL_DETENT = 50.0
WHEEL_NOTCH = 40.0


def _keep_wheel(element) -> None:
    """
    Stop a wheel over the time controls from scrolling the page as well as
    stepping through time. A second, client-only listener so the server still
    receives every delta; preventDefault works here because the element is
    not one of the roots browsers force wheel listeners to be passive on.
    """
    element.on("wheel", js_handler="(e) => e.preventDefault()")


def _signals_frame():
    import pandas as pd

    path = app_paths.cache_dir() / "signals.csv"
    if not path.is_file():
        return None
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    if _SIGNALS["key"] != key:
        _SIGNALS["frame"] = pd.read_csv(path, index_col="datetime", parse_dates=True)
        _SIGNALS["key"] = key
    return _SIGNALS["frame"]


def _cache_age():
    """(when the cached run was written, is the strategy newer than it)."""
    from datetime import datetime

    signals = app_paths.cache_dir() / "signals.csv"
    if not signals.is_file():
        return "never", False
    written = signals.stat().st_mtime
    strategy = app_paths.strategy_path()
    newer = strategy.is_file() and strategy.stat().st_mtime > written
    return datetime.fromtimestamp(written).strftime("%d %b %H:%M"), newer


def _child_ids() -> list:
    """Direct children of the inspected column, per the engine's own tree."""
    from ui import dashboard

    try:
        return dashboard.condition_tree(_strategy_doc()).get(INSPECT["column"], [])
    except Exception:
        return []


def _parent_ids() -> list:
    """
    Direct parents of the inspected column: whichever nodes list it as a child.

    Inverted from the same forward tree the children list uses, rather than a
    separate lookup, so parent and child links can never disagree. A shared
    definition can have more than one parent, so this is a list, not one id.
    """
    from ui import dashboard

    try:
        tree = dashboard.condition_tree(_strategy_doc())
    except Exception:
        return []
    return sorted({parent for parent, children in tree.items()
                  if INSPECT["column"] in children})


def _paint_children() -> None:
    from ui import dashboard

    frame = _signals_frame()
    if frame is None:
        return
    position = INSPECT["position"]
    for child, label in _PANEL.get("children") or []:
        if child not in frame.columns:
            # The cache predates this block, so it was never evaluated.
            label.set_text("n/a")
            label.style("background:#e5e7eb;color:#6b7280")
            continue
        value = frame[child].iloc[position]
        label.set_text("—" if value != value else f"{value:+.3f}")
        label.style(f"background:{dashboard.score_colour(value)};"
                    f"color:{dashboard.text_colour(value)}")


def _paint() -> None:
    """Write the current row onto the existing elements."""
    from ui import dashboard

    frame = _signals_frame()
    if frame is None or _PANEL["value"] is None:
        return
    position = INSPECT["position"]
    stamp = frame.index[position]
    value = frame[INSPECT["column"]].iloc[position]
    try:
        _PANEL["value"].set_text("—" if value != value else f"{value:+.4f}")
        _PANEL["value"].style(f"background:{dashboard.score_colour(value)};"
                              f"color:{dashboard.text_colour(value)}")
        _PANEL["caption"].set_text(f"{INSPECT['column']} at {stamp:%Y-%m-%d %H:%M}")
        _PANEL["counter"].set_text(f"row {position + 1:,} of {len(frame):,}")
        _PANEL["moment"].set_value(stamp.strftime("%Y-%m-%d %H:%M"))
        _paint_children()
    except Exception:
        _PANEL["value"] = None          # elements from a previous page build


@ui.refreshable
def _parents_list() -> None:
    """The reverse of the children list: click to step up instead of down."""
    parents = _parent_ids()
    if not parents:
        ui.label("top level — no parent").classes(MUTED)
        return
    with ui.column().classes("gap-1 w-full"):
        for parent in parents:
            with ui.row().classes("items-center gap-1 no-wrap cursor-pointer") \
                    .on("click", lambda p=parent: _set_column(p)) \
                    .tooltip(f"Inspect {parent}"):
                ui.icon("arrow_upward").classes("text-xs")
                ui.label(parent).classes("text-xs truncate font-medium")


@ui.refreshable
def _children_list() -> None:
    """Rebuilt when the column changes; its values are repainted on every step."""
    _PANEL["children"] = []
    children = _child_ids()
    ui.label(f"direct children ({len(children)})").classes(MUTED)
    if not children:
        ui.label("this block has none").classes(MUTED)
        return
    with ui.column().classes("gap-1 w-full").style("max-height:150px;overflow:auto"):
        for child in children:
            with ui.row().classes("items-center gap-2 no-wrap w-full cursor-pointer") \
                    .on("click", lambda c=child: _set_column(c)) \
                    .tooltip(f"Inspect {child}"):
                label = ui.label("").classes("text-xs font-semibold px-1 rounded") \
                    .style("min-width:58px;text-align:center")
                ui.label(child).classes("text-xs truncate")
            _PANEL["children"].append((child, label))
    _paint_children()


def _step(rows: int) -> None:
    frame = _signals_frame()
    if frame is None:
        return
    position = max(0, min(len(frame) - 1, (INSPECT["position"] or 0) + rows))
    if position == INSPECT["position"]:
        return
    INSPECT["position"] = position
    _paint()


def _wheel(event) -> None:
    delta = (event.args or {}).get("deltaY") or 0
    if abs(delta) >= WHEEL_DETENT:                   # a mouse click of the wheel
        INSPECT["residual"] = 0.0
        _step(-1 if delta > 0 else 1)                # scrolling down goes back in time
        return
    INSPECT["residual"] += delta
    steps = int(INSPECT["residual"] / WHEEL_NOTCH)   # truncates toward zero
    if not steps:
        return
    INSPECT["residual"] -= steps * WHEEL_NOTCH
    _step(-steps)


def _seek(text: str) -> None:
    """Jump to a typed moment. Only called on Enter or blur, never mid-edit."""
    import pandas as pd

    frame = _signals_frame()
    if frame is None or not str(text).strip():
        return
    try:
        wanted = pd.Timestamp(str(text).strip())
    except ValueError:
        ui.notify(f"Could not read {text!r} as a date and time", type="warning")
        _paint()                       # put the last good moment back in the box
        return
    # Nearest, because the typed moment may fall outside a session.
    INSPECT["position"] = int(frame.index.get_indexer([wanted], method="nearest")[0])
    _paint()


def _set_date(date_text: str) -> None:
    frame = _signals_frame()
    if frame is None or not date_text:
        return
    current = frame.index[INSPECT["position"] or 0]
    _seek(f"{date_text} {current.strftime('%H:%M')}")


def _set_column(column: str) -> None:
    if not column or column == INSPECT["column"]:
        return
    INSPECT["column"] = column
    if _PANEL.get("select") is not None:
        _PANEL["select"].set_value(column)   # keep the picker in step when a child is clicked
    _parents_list.refresh()
    _children_list.refresh()
    _paint()


@ui.refreshable
def _signal_inspector() -> None:
    frame = _signals_frame()
    if frame is None:
        ui.label("No cached signals — run a backtest with 'reuse cached signals' off.") \
            .classes(MUTED)
        return
    if not len(frame.columns) or not len(frame):
        ui.label("Cached signals file is empty.").classes(MUTED)
        return

    columns = sorted(frame.columns)
    if INSPECT["column"] not in columns:
        INSPECT["column"] = columns[0]
    if INSPECT["position"] is None:
        INSPECT["position"] = len(frame) - 1
    INSPECT["position"] = max(0, min(len(frame) - 1, INSPECT["position"]))
    stamp = frame.index[INSPECT["position"]]

    card = ui.card().classes("w-full")
    with card:
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("Inspect cached signals").classes("font-medium")
            ui.label(f"{len(frame):,} rows · {frame.index[0].date()} to "
                     f"{frame.index[-1].date()}").classes(MUTED)
            written, stale = _cache_age()
            ui.label(f"· run {written}").classes(MUTED)
            if stale:
                # These values were produced by an older strategy, so they can
                # disagree with freshly rendered charts.
                ui.badge("strategy edited since").props("color=warning") \
                    .tooltip("config/strategy.yaml is newer than this run — "
                             "re-run a backtest to refresh these values")
        outer = ui.row().classes("w-full gap-4 items-start no-wrap")
    with outer:
        left = ui.column().classes("grow gap-2 min-w-0")
        right = ui.column().classes("gap-1 shrink-0").style("width:250px")
    with left:
        with ui.row().classes("items-center gap-3 flex-wrap"):
            select = ui.select(columns, value=INSPECT["column"], label="column",
                               with_input=True,
                               on_change=lambda e: _set_column(e.value)) \
                .props("dense options-dense").classes("min-w-[280px]")
            # Scoped to just the time controls, not the whole card, so the
            # page can still be scrolled with the mouse anywhere else over it.
            time_controls = ui.row().classes("items-center gap-1")
            time_controls.on("wheel", _wheel, ["deltaY"])   # unthrottled: a dropped event loses its delta
            _keep_wheel(time_controls)
            with time_controls:
                # Committed on Enter or on leaving the field. Validating per keystroke
                # made a half-deleted time like "10:4" parse as 10:04 and jump.
                moment = ui.input(label="date and time",
                                  value=stamp.strftime("%Y-%m-%d %H:%M")).props("dense")
                moment.on("keydown.enter", lambda: _seek(moment.value))
                moment.on("blur", lambda: _seek(moment.value))
                with ui.menu().props("no-parent-event") as calendar:
                    ui.date(value=str(stamp.date()), on_change=lambda e: _set_date(e.value))
                    with ui.row().classes("justify-end"):
                        ui.button("Close", on_click=calendar.close).props("flat")
                with moment.add_slot("append"):
                    ui.icon("edit_calendar").on("click", calendar.open).classes("cursor-pointer")
                ui.button(icon="arrow_upward", on_click=lambda: _step(1)) \
                    .props("flat dense").tooltip("One minute later")
                ui.button(icon="arrow_downward", on_click=lambda: _step(-1)) \
                    .props("flat dense").tooltip("One minute earlier")
        with ui.row().classes("items-center gap-3 w-full"):
            value = ui.label("").classes("text-2xl font-semibold px-3 py-1 rounded")
            caption = ui.label("").classes(MUTED)
            ui.space()
            counter = ui.label("").classes(MUTED)
        ui.label("Scroll over the time field to step through the file a minute at a time.") \
            .classes(MUTED)
    with right:
        _parents_list()
        ui.separator()
        _children_list()

    _PANEL.update(value=value, caption=caption, counter=counter, moment=moment,
                  select=select)
    INSPECT["residual"] = 0.0
    _paint()


# --- cached-indicator inspector ---------------------------------------------

# One cached frame per (instrument, timeframe), each independently kept until
# its file's mtime/size changes -- the same rule _signals_frame uses.
_INDICATOR_FRAMES: dict = {}
INSPECT_IND: dict = {"instrument": None, "timeframe": None, "developing": False,
                     "column": None, "position": None, "residual": 0.0}
_PANEL_IND: dict = {"value": None, "caption": None, "counter": None, "moment": None,
                    "select": None}


def _indicator_dir():
    return app_paths.cache_dir() / "indicators"


def _indicator_combos() -> list:
    """(instrument_id, timeframe, developing) triples with a cached frame."""
    directory = _indicator_dir()
    if not directory.is_dir():
        return []
    combos = []
    for path in directory.glob("*__*.csv"):
        stem = path.stem
        developing = stem.endswith("__developing")
        if developing:
            stem = stem[: -len("__developing")]
        instrument_id, _, timeframe = stem.partition("__")
        combos.append((instrument_id, timeframe, developing))
    return sorted(combos)


def _indicator_frame(instrument_id: str, timeframe: str, developing: bool = False):
    import pandas as pd

    path = _indicator_dir() / f"{instrument_id}__{timeframe}{'__developing' if developing else ''}.csv"
    if not path.is_file():
        return None
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cache_key = (instrument_id, timeframe, developing)
    cached = _INDICATOR_FRAMES.get(cache_key)
    if cached is None or cached["key"] != key:
        cached = {"key": key, "frame": pd.read_csv(path, index_col="datetime", parse_dates=True)}
        _INDICATOR_FRAMES[cache_key] = cached
    return cached["frame"]


def _current_indicator_frame():
    if not INSPECT_IND["instrument"] or not INSPECT_IND["timeframe"]:
        return None
    return _indicator_frame(INSPECT_IND["instrument"], INSPECT_IND["timeframe"],
                            INSPECT_IND["developing"])


def _indicator_paint() -> None:
    """Write the current row onto the existing elements."""
    frame = _current_indicator_frame()
    if frame is None or _PANEL_IND["value"] is None:
        return
    position = INSPECT_IND["position"]
    stamp = frame.index[position]
    value = frame[INSPECT_IND["column"]].iloc[position]
    try:
        _PANEL_IND["value"].set_text("—" if value != value else f"{value:,.4f}")
        _PANEL_IND["caption"].set_text(f"{INSPECT_IND['column']} at {stamp:%Y-%m-%d %H:%M}")
        _PANEL_IND["counter"].set_text(f"row {position + 1:,} of {len(frame):,}")
        _PANEL_IND["moment"].set_value(stamp.strftime("%Y-%m-%d %H:%M"))
    except Exception:
        _PANEL_IND["value"] = None       # elements from a previous page build


def _indicator_step(rows: int) -> None:
    frame = _current_indicator_frame()
    if frame is None:
        return
    position = max(0, min(len(frame) - 1, (INSPECT_IND["position"] or 0) + rows))
    if position == INSPECT_IND["position"]:
        return
    INSPECT_IND["position"] = position
    _indicator_paint()


def _indicator_wheel(event) -> None:
    delta = (event.args or {}).get("deltaY") or 0
    if abs(delta) >= WHEEL_DETENT:                   # a mouse click of the wheel
        INSPECT_IND["residual"] = 0.0
        _indicator_step(-1 if delta > 0 else 1)      # scrolling down goes back in time
        return
    INSPECT_IND["residual"] += delta
    steps = int(INSPECT_IND["residual"] / WHEEL_NOTCH)
    if not steps:
        return
    INSPECT_IND["residual"] -= steps * WHEEL_NOTCH
    _indicator_step(-steps)


def _indicator_seek(text: str) -> None:
    """Jump to a typed moment. Only called on Enter or blur, never mid-edit."""
    import pandas as pd

    frame = _current_indicator_frame()
    if frame is None or not str(text).strip():
        return
    try:
        wanted = pd.Timestamp(str(text).strip())
    except ValueError:
        ui.notify(f"Could not read {text!r} as a date and time", type="warning")
        _indicator_paint()
        return
    INSPECT_IND["position"] = int(frame.index.get_indexer([wanted], method="nearest")[0])
    _indicator_paint()


def _indicator_set_date(date_text: str) -> None:
    frame = _current_indicator_frame()
    if frame is None or not date_text:
        return
    current = frame.index[INSPECT_IND["position"] or 0]
    _indicator_seek(f"{date_text} {current.strftime('%H:%M')}")


def _indicator_set_column(column: str) -> None:
    if not column or column == INSPECT_IND["column"]:
        return
    INSPECT_IND["column"] = column
    _indicator_paint()


def _indicator_set_combo(instrument_id: str, timeframe: str, developing: bool = False) -> None:
    """Switching instrument, timeframe, or developing swaps the whole frame,
    so the column list and row count both need rebuilding, not just repainting."""
    key = (instrument_id, timeframe, developing)
    if key == (INSPECT_IND["instrument"], INSPECT_IND["timeframe"], INSPECT_IND["developing"]):
        return
    INSPECT_IND["instrument"] = instrument_id
    INSPECT_IND["timeframe"] = timeframe
    INSPECT_IND["developing"] = developing
    INSPECT_IND["column"] = None
    INSPECT_IND["position"] = None
    _indicator_inspector.refresh()


@ui.refreshable
def _indicator_inspector() -> None:
    combos = _indicator_combos()
    if not combos:
        ui.label("No cached indicators — run a backtest to populate this.").classes(MUTED)
        return

    if (INSPECT_IND["instrument"], INSPECT_IND["timeframe"], INSPECT_IND["developing"]) not in combos:
        INSPECT_IND["instrument"], INSPECT_IND["timeframe"], INSPECT_IND["developing"] = combos[0]

    frame = _current_indicator_frame()
    if frame is None or not len(frame.columns) or not len(frame):
        ui.label("Cached indicator file is empty.").classes(MUTED)
        return

    columns = sorted(frame.columns)
    if INSPECT_IND["column"] not in columns:
        INSPECT_IND["column"] = columns[0]
    if INSPECT_IND["position"] is None:
        INSPECT_IND["position"] = len(frame) - 1
    INSPECT_IND["position"] = max(0, min(len(frame) - 1, INSPECT_IND["position"]))
    stamp = frame.index[INSPECT_IND["position"]]

    instruments = sorted({i for i, _, _ in combos})
    timeframes = sorted({tf for i, tf, _ in combos if i == INSPECT_IND["instrument"]},
                        key=lambda t: int(t[:-3]) if t.endswith("min") else t)
    has_developing = (INSPECT_IND["instrument"], INSPECT_IND["timeframe"], True) in combos

    card = ui.card().classes("w-full")
    with card:
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label("Inspect cached indicators").classes("font-medium")
            ui.label(f"{len(frame):,} rows · {frame.index[0].date()} to "
                     f"{frame.index[-1].date()}").classes(MUTED)

        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.select(instruments, value=INSPECT_IND["instrument"], label="instrument",
                      on_change=lambda e: _indicator_set_combo(
                          e.value, next(tf for i, tf, _ in combos if i == e.value))) \
                .props("dense options-dense").classes("min-w-[140px]")
            ui.select(timeframes, value=INSPECT_IND["timeframe"], label="timeframe",
                      on_change=lambda e: _indicator_set_combo(
                          INSPECT_IND["instrument"], e.value)) \
                .props("dense options-dense").classes("min-w-[100px]")
            if has_developing:
                ui.switch("developing", value=INSPECT_IND["developing"],
                          on_change=lambda e: _indicator_set_combo(
                              INSPECT_IND["instrument"], INSPECT_IND["timeframe"], e.value)) \
                    .props("dense").tooltip(
                        "Bucket-to-date values, one row per minute, instead of one "
                        "row per closed candle -- lets you inspect e.g. 15min RSI at 9:32")
            select = ui.select(columns, value=INSPECT_IND["column"], label="column",
                               with_input=True,
                               on_change=lambda e: _indicator_set_column(e.value)) \
                .props("dense options-dense").classes("min-w-[200px]")
            # Scoped to just the time controls, not the whole card, so the
            # page can still be scrolled with the mouse anywhere else over it.
            time_controls = ui.row().classes("items-center gap-1")
            time_controls.on("wheel", _indicator_wheel, ["deltaY"])   # unthrottled, as with signals
            _keep_wheel(time_controls)
            with time_controls:
                # Committed on Enter or on leaving the field, same as the signal
                # inspector -- validating per keystroke breaks a half-typed time.
                moment = ui.input(label="date and time",
                                  value=stamp.strftime("%Y-%m-%d %H:%M")).props("dense")
                moment.on("keydown.enter", lambda: _indicator_seek(moment.value))
                moment.on("blur", lambda: _indicator_seek(moment.value))
                with ui.menu().props("no-parent-event") as calendar:
                    ui.date(value=str(stamp.date()),
                           on_change=lambda e: _indicator_set_date(e.value))
                    with ui.row().classes("justify-end"):
                        ui.button("Close", on_click=calendar.close).props("flat")
                with moment.add_slot("append"):
                    ui.icon("edit_calendar").on("click", calendar.open).classes("cursor-pointer")
                ui.button(icon="arrow_upward", on_click=lambda: _indicator_step(1)) \
                    .props("flat dense").tooltip("One row later")
                ui.button(icon="arrow_downward", on_click=lambda: _indicator_step(-1)) \
                    .props("flat dense").tooltip("One row earlier")
        with ui.row().classes("items-center gap-3 w-full"):
            value = ui.label("").classes(
                "text-2xl font-semibold px-3 py-1 rounded").style(f"background:{SUBCARD_BG}")
            caption = ui.label("").classes(MUTED)
            ui.space()
            counter = ui.label("").classes(MUTED)
        ui.label("Scroll over the time field to step through the file a row at a time.") \
            .classes(MUTED)

    _PANEL_IND.update(value=value, caption=caption, counter=counter, moment=moment,
                      select=select)
    INSPECT_IND["residual"] = 0.0
    _indicator_paint()


def latest_job():
    """Most recently submitted backtest, whether whole-strategy or a single block."""
    jobs = [j for name, j in RUNNER.all().items() if name.startswith(BACKTEST_JOB)]
    return max(jobs, key=lambda job: job.sequence) if jobs else None

def running_backtest():
    """Any backtest in flight, whole-strategy or a single block."""
    for job in RUNNER.all().values():
        if job.name.startswith(BACKTEST_JOB) and not job.finished:
            return job
    return None

def _start_backtest() -> None:
    # Runs share the output folder and the signal cache, so only one at a time.
    busy = running_backtest()
    if busy is not None:
        ui.notify(f"{busy.name} is still running", type="warning")
        return
    _persist_settings()
    RUNNER.submit(BACKTEST_JOB, _run_backtest_job())
    ui.notify("Backtest started")
    _backtest_status.refresh()

def _cancel_backtest() -> None:
    # Cancels whatever the panel is showing, so a single-block run can be
    # stopped as readily as a whole-strategy one.
    job = latest_job()
    if job is None or job.finished:
        job = running_backtest()
    if job is None or job.finished:
        ui.notify("No backtest running", type="warning")
        return
    job.cancel()
    ui.notify(f"Cancelling {job.name}…")

@ui.refreshable
def _backtest_status() -> None:
    job = latest_job()
    if job is None:
        ui.label("No backtest has been run yet.").classes(MUTED)
        return

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-3"):
            ui.badge(job.state).props(
                "color=positive" if job.state == "done" else
                "color=negative" if job.state == "failed" else "color=primary")
            ui.label(job.message or "").classes(MUTED)
        ui.linear_progress(value=job.progress, show_value=False).classes("w-full")

        if job.state == "failed":
            ui.label(job.error or "").classes("text-negative text-sm")
        elif job.state == "done" and job.result:
            result = job.result
            ui.label(f"Evaluated {result['candles']} candles").classes(MUTED)
            for cid, stat in result["days"].items():
                with ui.row().classes("items-baseline gap-2"):
                    ui.label(cid).classes("font-medium")
                    ui.label(format_signal_stat(stat)).classes(MUTED)
                    for tier in result["children"].get(cid, []):
                        if tier in result["tiers"]:
                            ui.label(f"· {tier}: {format_signal_stat(result['tiers'][tier])}") \
                                .classes(MUTED)

@ui.refreshable
def live_tab() -> None:
    from live_service import SERVICE
    from ui import dashboard

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-3"):
            ui.label("Live signal engine").classes("font-medium")
            ui.badge(SERVICE.state).props(
                "color=positive" if SERVICE.state == "running" else
                "color=negative" if SERVICE.state == "error" else "color=grey")
        with ui.row().classes("items-center gap-2"):
            ui.button("Start", icon="play_arrow", on_click=_start_live) \
                .props("" if SERVICE.state != "running" else "disable")
            ui.button("Stop", icon="stop", on_click=_stop_live).props("flat")
        if SERVICE.error:
            ui.label(SERVICE.error).classes("text-negative text-sm")
        if SERVICE.disabled:
            ui.label(f"{len(SERVICE.disabled)} condition(s) disabled and will not alert: "
                     + ", ".join(SERVICE.disabled)).classes("text-warning text-sm")

    with ui.card().classes("w-full"):
        dashboard.dashboard_section(
            _settings_doc(), _strategy_doc(), SERVICE, _persist_settings)

    with ui.card().classes("w-full"):
        _notifications_section(_settings_doc(), _strategy_doc(), _persist_settings)

# --- notification rules ------------------------------------------------------

NOTIFICATION_KINDS = ["state", "children_met"]
NOTIFICATION_EDGES = ["rising", "falling", "level"]

def _notification_rules(settings_doc):
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    node = settings_doc.setdefault("notifications", CommentedMap())
    node.setdefault("rules", CommentedSeq())
    return node["rules"]

def _new_rule_id() -> str:
    import uuid

    return f"notif_{uuid.uuid4().hex[:8]}"

def _add_rule(rules, save) -> None:
    from ruamel.yaml.comments import CommentedMap

    rule = CommentedMap()
    rule["id"] = _new_rule_id()
    rule["label"] = ""
    rule["target"] = ""
    rule["kind"] = "state"
    rule["params"] = CommentedMap()
    rule["edge"] = "rising"
    rule["enabled"] = True
    rule["notification_sound"] = False
    rules.append(rule)
    save()
    _notifications_section.refresh()

def _remove_rule(rules, index, save) -> None:
    del rules[index]
    save()
    _notifications_section.refresh()

def _confirm_delete_rule(rules, index, save) -> None:
    from ui import dashboard

    dashboard._confirm_delete("Delete this notification rule?",
                              lambda: _remove_rule(rules, index, save))

def _set_rule_field(rule, key, value, save) -> None:
    rule[key] = value
    save()

def _set_rule_min_met(rule, value, save) -> None:
    from ruamel.yaml.comments import CommentedMap

    n = int(value) if value not in (None, "") else 1
    rule.setdefault("params", CommentedMap())["min_met"] = max(1, n)
    save()

def _set_volume(settings_doc, value, save) -> None:
    """Called on slider release: persists the choice, applies it to the
    running live service immediately, and previews it -- same "drag only
    restyles, release commits" pattern as the dashboard's size slider."""
    from ruamel.yaml.comments import CommentedMap
    from live_service import SERVICE

    node = settings_doc.setdefault("notifications", CommentedMap())
    volume = max(0, min(100, int(value)))
    node["volume"] = volume
    save()
    SERVICE.volume = volume
    sound.play(notifications.NOTIFICATION_SOUND_PATH, volume)

@ui.refreshable
def _notifications_section(settings_doc, strategy_doc, save) -> None:
    from ui import dashboard

    rules = _notification_rules(settings_doc)
    target_options = sorted(dashboard.condition_tree(strategy_doc).keys())

    with ui.row().classes("items-center gap-2"):
        ui.label("Notifications").classes("font-medium")
        ui.button(icon="add", on_click=lambda: _add_rule(rules, save)) \
            .props("flat dense").tooltip("Add a notification rule")
    with ui.row().classes("items-center gap-2"):
        ui.label("Volume").classes(MUTED)
        volume = int((settings_doc.get("notifications") or {}).get("volume", 100))
        volume_label = ui.label(f"{volume}%").classes(MUTED).style("width:36px")
        volume_slider = ui.slider(min=0, max=100, step=1, value=volume,
                                  on_change=lambda e: volume_label.set_text(f"{int(e.value)}%")) \
            .props("dense").style("width:160px") \
            .tooltip("Notification sound volume -- release to save and preview")
        volume_slider.on("change", lambda _: _set_volume(settings_doc, volume_slider.value, save))
    ui.label("Delivery isn't wired up yet -- rules just log until a channel is chosen. "
             "The notification sound (if enabled below) plays regardless.") \
        .classes(MUTED)
    if len(rules) == 0:
        ui.label("No rules yet.").classes(MUTED)
        return

    for index, rule in enumerate(rules):
        with ui.row().classes("items-center gap-2 w-full flex-wrap"):
            ui.input(label="label", value=rule.get("label", ""),
                     on_change=lambda e, r=rule: _set_rule_field(r, "label", e.value, save)) \
                .props("dense").classes("min-w-[220px]").style("flex:2")
            ui.select(target_options, value=rule.get("target") or None, label="target",
                     with_input=True,
                     on_change=lambda e, r=rule: _set_rule_field(r, "target", e.value, save)) \
                .props("dense").classes("min-w-[220px]").style("flex:2")
            ui.select(NOTIFICATION_KINDS, value=rule.get("kind", "state"), label="kind",
                     on_change=lambda e, r=rule: _set_rule_field(r, "kind", e.value, save)) \
                .props("dense").classes("min-w-[130px]")
            if rule.get("kind") == "children_met":
                ui.number(label="min met", value=(rule.get("params") or {}).get("min_met", 1),
                         precision=0, format="%d", min=1,
                         on_change=lambda e, r=rule: _set_rule_min_met(r, e.value, save)) \
                    .props("dense").classes("min-w-[90px]")
            ui.select(NOTIFICATION_EDGES, value=rule.get("edge", "rising"), label="edge",
                     on_change=lambda e, r=rule: _set_rule_field(r, "edge", e.value, save)) \
                .props("dense").classes("min-w-[110px]")
            ui.switch(value=rule.get("enabled", True),
                     on_change=lambda e, r=rule: _set_rule_field(r, "enabled", e.value, save)) \
                .props("dense").tooltip("Enabled")
            ui.switch("Notification Sound", value=rule.get("notification_sound", False),
                     on_change=lambda e, r=rule: _set_rule_field(r, "notification_sound", e.value, save)) \
                .props("dense").tooltip("Play the notification sound when this rule fires")
            ui.button(icon="delete",
                      on_click=lambda i=index: _confirm_delete_rule(rules, i, save)) \
                .props("flat dense color=negative")

def _start_live() -> None:
    from live_service import SERVICE
    SERVICE.start()
    ui.notify("Live engine starting…")
    live_tab.refresh()

def _stop_live() -> None:
    from live_service import SERVICE
    SERVICE.stop()
    ui.notify("Live engine stopped")
    live_tab.refresh()

CHARTS_ROUTE = "/charts"
PLACEHOLDER_IMAGE = "_no_chart.png"

def _ensure_placeholder_image() -> None:
    """A dark 'No image generated' image, shown in place of a real chart when
    a run had 'render charts' off. Generated once and reused after that."""
    path = app_paths.output_dir() / PLACEHOLDER_IMAGE
    if path.is_file():
        return
    from PIL import Image, ImageDraw, ImageFont

    width, height, text = 1600, 640, "No image generated"
    image = Image.new("RGB", (width, height), color=(20, 20, 20))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (right - left)) / 2 - left, (height - (bottom - top)) / 2 - top),
              text, fill=(235, 235, 235), font=font)
    image.save(path)

def mount_charts() -> None:
    """Serve the rendered charts so the Charts tab can display them in place."""
    from nicegui import app as nicegui_app

    app_paths.output_dir().mkdir(parents=True, exist_ok=True)
    _ensure_placeholder_image()
    nicegui_app.add_static_files(CHARTS_ROUTE, str(app_paths.output_dir()))

def _chart_blocks() -> list[str]:
    root = app_paths.output_dir()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())

def _chart_images(block: str) -> list:
    folder = app_paths.output_dir() / block
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.png"), key=lambda p: p.name)

def _chart_windows(block: str) -> dict:
    """Stretches the block was true, written beside its charts by the backtest."""
    from backtest import WINDOWS_FILE

    path = app_paths.output_dir() / block / WINDOWS_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("could not read active windows for %s", block)
        return {}
    # A file written before windows.json carried a label is a plain
    # {day: [[start, end, bars], ...]} -- fall back to the bare date rather
    # than crash on it; the next run will overwrite it with the new shape.
    return {
        day: value if isinstance(value, dict) else {"windows": value, "label": day}
        for day, value in raw.items()
    }

# Module-level so the selection survives leaving and re-entering the tab, which
# re-runs the builder. Stored by name rather than Path so it still matches after
# the output folder is rescanned.
CHART_STATE: dict = {"block": None, "image": None}

@ui.refreshable
def charts_tab() -> None:
    blocks = _chart_blocks()
    if not blocks:
        ui.label("Nothing here yet — run a backtest, or use a condition "
                 "block's run button.").classes(MUTED)
        return

    if CHART_STATE["block"] not in blocks:  # first visit, or the block is gone
        CHART_STATE["block"] = blocks[0]
        CHART_STATE["image"] = None

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-3"):
            ui.select(blocks, value=CHART_STATE["block"], label="condition block",
                      with_input=True, on_change=lambda e: _select_block(e.value)) \
                .props("dense options-dense").classes("min-w-[280px]")
            ui.button(icon="refresh", on_click=charts_tab.refresh) \
                .props("flat dense").tooltip("Rescan the output folder")

    _chart_viewer()

def _select_block(block: str) -> None:
    CHART_STATE["block"] = block
    CHART_STATE["image"] = None
    _chart_viewer.refresh()

@ui.refreshable
def _chart_viewer() -> None:
    block = CHART_STATE["block"]
    images = _chart_images(block) if block else []
    if images:
        _rendered_chart_viewer(block, images)
        return

    days = sorted(_chart_windows(block).keys()) if block else []
    if not days:
        ui.label("This block has no rendered sessions.").classes(MUTED)
        return
    _placeholder_chart_viewer(block, days)

def _rendered_chart_viewer(block: str, images: list) -> None:
    names = [p.name for p in images]
    if CHART_STATE["image"] not in names:  # re-rendered charts change their names
        CHART_STATE["image"] = names[0]
    selected = images[names.index(CHART_STATE["image"])]

    hits = [p for p in images if "HIT" in p.name]
    ui.label(f"{len(images)} sessions · {len(hits)} hit").classes(MUTED)

    with ui.row().classes("w-full gap-4 items-start no-wrap"):
        with ui.column().classes("min-w-[320px] max-h-[70vh] overflow-auto"):
            for path in images:
                hit = "HIT" in path.name
                current = path.name == selected.name
                with ui.row().classes("items-center gap-2 cursor-pointer rounded px-1") \
                        .style(f"background:{SELECTED_BG}" if current else "") \
                        .on("click", lambda n=path.name: _show_image(n)):
                    ui.badge("hit" if hit else "—").props(
                        "color=positive" if hit else "color=grey")
                    ui.label(path.stem).classes(
                        "text-sm" + ("" if hit else " text-gray-300"))
        with ui.column().classes("grow"):
            ui.label(selected.stem).classes("font-medium")
            # Chart names carry spaces, so the src has to be percent-encoded.
            src = f"{CHARTS_ROUTE}/{quote(block)}/{quote(selected.name)}"
            ui.image(src).classes("w-full")
            _active_windows(block, selected.stem[:10])

def _placeholder_chart_viewer(block: str, days: list) -> None:
    """The last run had 'render charts' off, so there is no PNG per session --
    show the placeholder image and the same HIT/blocked wording a rendered
    filename would carry, which is still cheap to compute and saved
    regardless."""
    windows = _chart_windows(block)
    if CHART_STATE["image"] not in days:
        CHART_STATE["image"] = days[0]
    selected_day = CHART_STATE["image"]
    selected_label = windows.get(selected_day, {}).get("label", selected_day)

    hits = [d for d in days if "HIT" in windows.get(d, {}).get("label", "")]
    ui.label(f"{len(days)} sessions · {len(hits)} hit · images not rendered "
             "(run with 'render charts' on to see them)").classes(MUTED)

    with ui.row().classes("w-full gap-4 items-start no-wrap"):
        with ui.column().classes("min-w-[320px] max-h-[70vh] overflow-auto"):
            for day in days:
                label = windows.get(day, {}).get("label", day)
                hit = "HIT" in label
                current = day == selected_day
                with ui.row().classes("items-center gap-2 cursor-pointer rounded px-1") \
                        .style(f"background:{SELECTED_BG}" if current else "") \
                        .on("click", lambda d=day: _show_image(d)):
                    ui.badge("hit" if hit else "—").props(
                        "color=positive" if hit else "color=grey")
                    ui.label(label).classes(
                        "text-sm" + ("" if hit else " text-gray-300"))
        with ui.column().classes("grow"):
            ui.label(selected_label).classes("font-medium")
            ui.image(f"{CHARTS_ROUTE}/{quote(PLACEHOLDER_IMAGE)}").classes("w-full")
            _active_windows(block, selected_day)

def _active_windows(block: str, day: str) -> None:
    runs = _chart_windows(block).get(day, {}).get("windows", [])
    if not runs:
        return
    total = sum(bars for _, _, bars in runs)
    ui.label(f"Active for {total} min over {len(runs)} "
             f"window{'' if len(runs) == 1 else 's'}").classes("font-medium")
    with ui.row().classes("items-center gap-2 flex-wrap"):
        for start, end, bars in runs:
            span = start if start == end else f"{start} – {end}"
            ui.badge(f"{span}  ({bars} min)").props("color=positive outline")

def _show_image(name: str) -> None:
    CHART_STATE["image"] = name
    _chart_viewer.refresh()

def _chart_selection_is_stale() -> bool:
    """True once the currently selected block/image no longer matches what's
    on disk -- e.g. a finished run deleted and re-rendered a block's charts
    under new filenames. Checked before refreshing the Charts tab so it only
    rebuilds (and resets scroll position) when it actually has to."""
    block = CHART_STATE["block"]
    if block is None:
        return False
    if block not in _chart_blocks():
        return True
    images = _chart_images(block)
    if images:
        return CHART_STATE["image"] not in {p.name for p in images}
    days = _chart_windows(block)
    return bool(days) and CHART_STATE["image"] not in days

def refresh_operations() -> None:
    """Called on a timer by the page so job and engine state stay current."""
    from ui import dashboard

    _backtest_status.refresh()
    # Rebuilding the Charts tab resets its scroll position and flashes the
    # image, so it's only worth doing once the selection is actually stale,
    # not on every tick.
    if _chart_selection_is_stale():
        _chart_viewer.refresh()
    # Repaint rather than refresh: rebuilding the grid would cancel a drag in
    # progress and close an open editor.
    from live_service import SERVICE

    dashboard.repaint(SERVICE)
