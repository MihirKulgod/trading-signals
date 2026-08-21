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
from app_logging import get_logger
from data_processing import format_signal_stat, signal_stats
from jobs import RUNNER

log = get_logger(__name__)

BACKTEST_JOB = "backtest"
MUTED = "text-sm text-gray-500"

def _load_configs():
    config = yaml.safe_load(app_paths.strategy_path().read_text(encoding="utf-8"))
    settings = yaml.safe_load(app_paths.settings_path().read_text(encoding="utf-8"))
    return config, settings

def _settings_doc():
    """The live ruamel settings document the editor tabs share."""
    from ui import app

    return app.DOCS["settings"]

def _persist_settings() -> None:
    from ui import persistence

    doc = _settings_doc()
    try:
        persistence.validate_settings(doc)
    except Exception as e:
        ui.notify(f"settings invalid — not saved: {e}", type="negative", timeout=6000)
        return
    persistence.save_document(doc, app_paths.settings_path())

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
            progress=job.report,
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
        # this block) and charts forced on (they are the point of the button),
        # one per session so the block can be reviewed across the whole range.
        df, cols, children, days = backtest.run_backtest(
            config, settings, start, end,
            download_data=options["download"], reuse_signals=False, visualize=True,
            progress=job.report, cache_signals=False, chart_valid_days=True,
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

def latest_job():
    """Most recently submitted backtest, whether whole-strategy or a single block."""
    jobs = [j for name, j in RUNNER.all().items() if name.startswith(BACKTEST_JOB)]
    return jobs[-1] if jobs else None

def _start_backtest() -> None:
    job = RUNNER.get(BACKTEST_JOB)
    if job is not None and not job.finished:
        ui.notify("A backtest is already running", type="warning")
        return
    _persist_settings()
    RUNNER.submit(BACKTEST_JOB, _run_backtest_job())
    ui.notify("Backtest started")
    _backtest_status.refresh()

def _cancel_backtest() -> None:
    job = RUNNER.get(BACKTEST_JOB)
    if job is None or job.finished:
        ui.notify("No backtest running", type="warning")
        return
    job.cancel()
    ui.notify("Cancelling after the current candle…")

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

    _live_scores()

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

@ui.refreshable
def _live_scores() -> None:
    from live_service import SERVICE

    if not SERVICE.scores:
        ui.label("No scores yet — the engine publishes after its first cycle.").classes(MUTED)
        return
    ui.label(f"last cycle: {SERVICE.last_run}").classes(MUTED)
    with ui.card().classes("w-full"):
        for cid, score in sorted(SERVICE.scores.items()):
            with ui.row().classes("items-center gap-3 w-full"):
                ui.badge("open" if score >= 0 else "shut").props(
                    "color=positive" if score >= 0 else "color=grey")
                ui.label(cid).classes("font-medium")
                ui.space()
                ui.label(f"{score:+.3f}").classes(MUTED)

CHARTS_ROUTE = "/charts"

def mount_charts() -> None:
    """Serve the rendered charts so the Charts tab can display them in place."""
    from nicegui import app as nicegui_app

    app_paths.output_dir().mkdir(parents=True, exist_ok=True)
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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("could not read active windows for %s", block)
        return {}

# Module-level so the selection survives leaving and re-entering the tab, which
# re-runs the builder. Stored by name rather than Path so it still matches after
# the output folder is rescanned.
CHART_STATE: dict = {"block": None, "image": None}

@ui.refreshable
def charts_tab() -> None:
    blocks = _chart_blocks()
    if not blocks:
        ui.label("No charts yet — run a backtest with 'render charts' on, "
                 "or use a condition block's run button.").classes(MUTED)
        return

    if CHART_STATE["block"] not in blocks:  # first visit, or the block is gone
        CHART_STATE["block"] = blocks[0]
        CHART_STATE["image"] = None

    with ui.card().classes("w-full"):
        with ui.row().classes("items-center gap-3"):
            ui.select(blocks, value=CHART_STATE["block"], label="condition block",
                      on_change=lambda e: _select_block(e.value)) \
                .props("dense").classes("min-w-[280px]")
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
    if not images:
        ui.label("This block has no rendered sessions.").classes(MUTED)
        return

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
                with ui.row().classes("items-center gap-2 cursor-pointer rounded px-1"
                                      + (" bg-blue-100" if current else "")) \
                        .on("click", lambda n=path.name: _show_image(n)):
                    ui.badge("hit" if hit else "—").props(
                        "color=positive" if hit else "color=grey")
                    ui.label(path.stem).classes(
                        "text-sm" + ("" if hit else " text-gray-500"))
        with ui.column().classes("grow"):
            ui.label(selected.stem).classes("font-medium")
            # Chart names carry spaces, so the src has to be percent-encoded.
            src = f"{CHARTS_ROUTE}/{quote(block)}/{quote(selected.name)}"
            ui.image(src).classes("w-full")
            _active_windows(block, selected)

def _active_windows(block: str, selected) -> None:
    runs = _chart_windows(block).get(selected.stem[:10], [])  # names start YYYY-MM-DD
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

def refresh_operations() -> None:
    """Called on a timer by the page so job and engine state stay current."""
    _backtest_status.refresh()
    _live_scores.refresh()
