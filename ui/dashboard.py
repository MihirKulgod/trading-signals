"""
The live dashboard: a configurable grid of condition monitors.

Each panel watches one condition block and shows its score, plus a rectangle per
direct child so a combination's internals are visible at a glance. Colour is the
primary signal, so the mapping from score to colour lives here and is used by
both the panel and its children.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from nicegui import ui

MUTED = "text-sm text-gray-500"

# ---------------------------------------------------------------------------
# Score -> colour
# ---------------------------------------------------------------------------

OPEN_GREEN = (67, 160, 71)      # any score at or above zero
NEAR_ZERO = (240, 162, 2)       # just below zero: orangey yellow
FAR_NEGATIVE = (107, 15, 15)    # deeply negative: dark blood red
UNKNOWN_GREY = (130, 130, 130)  # no value to show
SKIPPED_SLATE = (74, 85, 104)   # evaluation never reached this block

# |score| at which the gradient has fully reached FAR_NEGATIVE. Scores are
# normalised, so most live between -3 and +1; the log curve spends its
# resolution there and flattens out beyond.
SATURATION = 10.0


def _rgb(score: Any) -> tuple[int, int, int]:
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return UNKNOWN_GREY
    if score >= 0:
        return OPEN_GREEN
    # Logarithmic so the crowded region just below zero stays distinguishable
    # instead of every losing score washing out to the same red.
    weight = min(1.0, math.log1p(abs(score)) / math.log1p(SATURATION))
    return _blend(NEAR_ZERO, FAR_NEGATIVE, weight)


def score_colour(score: Any) -> str:
    """CSS colour for a score. The scale is shared by panels and children."""
    return _css(_rgb(score))


def text_colour(score: Any) -> str:
    """Black or white, whichever stays readable on ``score_colour(score)``."""
    return _on(_rgb(score))


def _on(rgb) -> str:
    red, green, blue = rgb
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#000000" if luminance > 0.6 else "#ffffff"


def _blend(start, end, weight: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * weight) for a, b in zip(start, end))


def _css(rgb) -> str:
    return "#%02x%02x%02x" % rgb


# ---------------------------------------------------------------------------
# What a panel is currently showing
# ---------------------------------------------------------------------------

# A block missing from the trace is not the same as one that scored NaN: the
# engine records every node it evaluates, so absence means evaluation never got
# there -- a sequential tier behind a closed gate. Both are reported instead of
# writing a stand-in score, because NaN is what marks "not evaluated" for
# signal_stats, session_blockers and find_valid_days downstream.
def block_state(block: str, scores: dict, engine_ran: bool) -> tuple[str, Any]:
    if not block:
        return "unset", None
    if not engine_ran:
        return "waiting", None
    if block not in scores:
        return "skipped", None
    value = scores[block]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "nan", None
    return "value", value


STATE_LABEL = {
    "unset": "pick a block",
    "waiting": "engine not started",
    "skipped": "not reached",
    "nan": "no value",
}


def state_colour(state: str, value: Any) -> str:
    if state == "value":
        return score_colour(value)
    if state == "skipped":
        return _css(SKIPPED_SLATE)
    return _css(UNKNOWN_GREY)


def state_text_colour(state: str, value: Any) -> str:
    if state == "value":
        return text_colour(value)
    return _on(SKIPPED_SLATE if state == "skipped" else UNKNOWN_GREY)


def format_score(value: Any) -> str:
    return "—" if value is None else f"{value:+.3f}"


# ---------------------------------------------------------------------------
# The condition tree, built from the strategy document
# ---------------------------------------------------------------------------

# Built via the engine rather than read off the document, because a ref resolves
# to its target: the ids a panel shows must be the ids live actually traces.
_TREE_CACHE: dict[str, Any] = {}


def _plain(doc) -> dict:
    import json

    return json.loads(json.dumps(doc))


def condition_tree(strategy_doc) -> dict[str, list[str]]:
    """node id -> direct child ids, for every node the live engine evaluates."""
    import json

    plain = _plain(strategy_doc)
    key = json.dumps(plain.get("conditions", []), sort_keys=True)
    if _TREE_CACHE.get("key") != key:
        from condition import build_selected_conditions

        children: dict[str, list[str]] = {}
        try:
            for root in build_selected_conditions(plain):
                for node in root.walk():
                    children[node.id] = [child.id for child in node.sub_conditions()]
        except Exception:
            children = {}
        _TREE_CACHE.update(key=key, children=children)
    return _TREE_CACHE["children"]


def top_level_ids(strategy_doc) -> list[str]:
    return [str(c.get("id")) for c in (strategy_doc.get("conditions") or [])
            if c.get("id")]


def disabled_ids(strategy_doc) -> set[str]:
    """Top-level blocks switched off; 'enabled' is only valid there."""
    return {str(c.get("id")) for c in (strategy_doc.get("conditions") or [])
            if c.get("enabled") is False and c.get("id")}


def default_name(block: str) -> str:
    """comb02-s1 -> 'Combination 2 S1'."""
    match = re.fullmatch(r"comb0*(\d+)(?:-(.+))?", block or "")
    if not match:
        return block or ""
    name = f"Combination {int(match.group(1))}"
    return f"{name} {match.group(2).upper()}" if match.group(2) else name


def default_panels(strategy_doc) -> list[dict]:
    return [{"block": block, "name": default_name(block)}
            for block in top_level_ids(strategy_doc)]


# ---------------------------------------------------------------------------
# Recalculation countdown
# ---------------------------------------------------------------------------


def humanise(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def countdown_state(service) -> tuple[float, float]:
    """(fraction of the interval elapsed, seconds overdue)."""
    interval = max(1, getattr(service, "recompute_seconds", 30))
    last_run = getattr(service, "last_run", None)
    if last_run is None:
        return 0.0, 0.0
    import pandas as pd

    elapsed = (pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None) - last_run).total_seconds()
    elapsed = max(0.0, elapsed)
    return min(1.0, elapsed / interval), max(0.0, elapsed - interval)


def _countdown(service) -> None:
    fraction, overdue = countdown_state(service)
    running = getattr(service, "state", "") == "running"
    due = overdue > 0
    colour = "red" if (due and running) else ("primary" if running else "grey-5")
    with ui.row().classes("items-center gap-2 no-wrap"):
        ui.circular_progress(value=1.0 if due else fraction, min=0.0, max=1.0,
                             size="34px", show_value=False, color=colour) \
            .tooltip("Time until the next signal recalculation")
        if not running:
            ui.label("idle").classes(MUTED)
        elif due:
            ui.label(humanise(overdue)).classes("text-xs text-red-600 font-medium") \
                .tooltip("Time since this recalculation was due to start")


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

PANEL_STYLE = "width:265px;height:186px"
HANDLE = "dash-handle"
EDITING: set[int] = set()
SHOW_ALL: set[int] = set()   # panels whose picker is showing every block


def _panels(settings_doc) -> Any:
    from ruamel.yaml.comments import CommentedSeq

    return settings_doc.setdefault("dashboard", CommentedSeq())


@ui.refreshable
def dashboard_section(settings_doc, strategy_doc, service, save) -> None:
    panels = _panels(settings_doc)
    tree = condition_tree(strategy_doc)
    scores = getattr(service, "node_scores", {}) or {}
    engine_ran = getattr(service, "last_run", None) is not None
    disabled = disabled_ids(strategy_doc)

    with ui.row().classes("items-center gap-3 w-full"):
        ui.label("Dashboard").classes("font-medium")
        _countdown(service)
        ui.space()
        ui.button(icon="add", on_click=lambda: _add_panel(panels, save)) \
            .props("flat dense").tooltip("Add a panel")
        ui.button(icon="grid_view",
                  on_click=lambda: _reset_panels(panels, strategy_doc, save)) \
            .props("flat dense").tooltip("Reset to one panel per combination")

    if len(panels) == 0:
        ui.label("Empty. Add a panel, or reset to one per combination.").classes(MUTED)
        return

    grid = ui.row().classes("w-full gap-3 flex-wrap items-start")
    grid.make_sortable(
        handle=f".{HANDLE}",
        on_end=lambda e, target=panels: _move(target, e.old_index, e.new_index, save),
    )
    with grid:
        for index, panel in enumerate(panels):
            _panel(index, panel, panels, tree, scores, engine_ran, disabled,
                   strategy_doc, save)


def _panel(index, panel, panels, tree, scores, engine_ran, disabled,
           strategy_doc, save) -> None:
    block = panel.get("block") or ""
    state, value = block_state(block, scores, engine_ran)
    is_disabled = block in disabled
    background = state_colour(state, value)
    foreground = state_text_colour(state, value)

    style = f"{PANEL_STYLE};background:{background};color:{foreground}"
    if is_disabled:
        style += ";opacity:0.55"

    with ui.card().classes("p-2 gap-1").style(style):
        if index in EDITING:
            _editor(index, panel, panels, tree, strategy_doc, save)
            return

        with ui.row().classes("items-center gap-1 w-full no-wrap"):
            ui.icon("drag_indicator").classes(f"{HANDLE} cursor-move") \
                .style("opacity:0.6").tooltip("Drag to reorder")
            ui.label(panel.get("name") or "(unnamed)") \
                .classes("font-medium truncate").style("flex:1;min-width:0")
            ui.button(icon="edit", on_click=lambda i=index: _edit(i)) \
                .props("flat dense size=sm").style("color:inherit")
            ui.button(icon="close",
                      on_click=lambda i=index: _remove_panel(panels, i, save)) \
                .props("flat dense size=sm").style("color:inherit")

        with ui.row().classes("items-baseline gap-2 w-full no-wrap"):
            ui.label(format_score(value)).classes("text-2xl font-semibold leading-none")
            if is_disabled:
                ui.badge("disabled").props("color=grey-8")
            elif state != "value":
                ui.label(STATE_LABEL[state]).classes("text-xs").style("opacity:0.85")

        _children(block, tree, scores, engine_ran)


def _children(block, tree, scores, engine_ran) -> None:
    """One rectangle per direct child. Children keep to a single level."""
    child_ids = tree.get(block, []) if block else []
    if not child_ids:
        return
    with ui.row().classes("w-full gap-1 flex-wrap content-start") \
            .style("flex:1;min-height:0;overflow:hidden"):
        for child_id in child_ids:
            state, value = block_state(child_id, scores, engine_ran)
            with ui.column().classes("items-center justify-center rounded p-1 gap-0").style(
                    f"flex:1 1 0;min-width:52px;background:{state_colour(state, value)};"
                    f"color:{state_text_colour(state, value)}"):
                ui.label(child_id).classes("truncate w-full text-center") \
                    .style("font-size:9px;line-height:1.1").tooltip(child_id)
                ui.label("·" if state == "skipped" else format_score(value)) \
                    .style("font-size:10px;font-weight:600;line-height:1.2") \
                    .tooltip(STATE_LABEL.get(state, ""))


def _editor(index, panel, panels, tree, strategy_doc, save) -> None:
    show_all = index in SHOW_ALL
    options = sorted(tree) if show_all else top_level_ids(strategy_doc)
    current = panel.get("block") or None
    if current and current not in options:
        options = [current] + list(options)

    with ui.column().classes("w-full gap-1").style("color:#000"):
        ui.select(options, value=current, label="condition block", with_input=True,
                  on_change=lambda e, p=panel: _set(p, "block", e.value, save)) \
            .props("dense options-dense").classes("w-full")
        ui.input(label="display name", value=panel.get("name") or "",
                 on_change=lambda e, p=panel: _set(p, "name", e.value, save)) \
            .props("dense").classes("w-full")
        with ui.row().classes("items-center gap-1 w-full no-wrap"):
            ui.checkbox("all blocks", value=show_all,
                        on_change=lambda e, i=index: _toggle_all(i, e.value)) \
                .props("dense size=xs").tooltip("Show every block, not just combinations")
            ui.space()
            ui.button("Done", on_click=lambda i=index: _done(i)).props("flat dense size=sm")
            ui.button(icon="delete",
                      on_click=lambda i=index: _remove_panel(panels, i, save)) \
                .props("flat dense size=sm color=negative")


# --- mutations -------------------------------------------------------------


def _set(panel, key, value, save) -> None:
    panel[key] = value or ""
    save()


def _as_map(entry: dict):
    from ruamel.yaml.comments import CommentedMap

    panel = CommentedMap()
    panel["block"] = entry.get("block", "")
    panel["name"] = entry.get("name", "")
    return panel


def _add_panel(panels, save) -> None:
    panels.append(_as_map({}))
    EDITING.clear()
    EDITING.add(len(panels) - 1)   # a new panel opens straight into its editor
    save()
    dashboard_section.refresh()


def _reset_panels(panels, strategy_doc, save) -> None:
    panels[:] = [_as_map(entry) for entry in default_panels(strategy_doc)]
    EDITING.clear()
    SHOW_ALL.clear()
    save()
    dashboard_section.refresh()


def _remove_panel(panels, index, save) -> None:
    del panels[index]
    EDITING.clear()                # indices shift, so no editor survives a removal
    SHOW_ALL.clear()
    save()
    dashboard_section.refresh()


def _move(panels, old_index, new_index, save) -> None:
    if old_index == new_index:
        return
    panels.insert(new_index, panels.pop(old_index))
    EDITING.clear()
    SHOW_ALL.clear()
    save()
    dashboard_section.refresh()


def _edit(index) -> None:
    EDITING.add(index)
    dashboard_section.refresh()


def _done(index) -> None:
    EDITING.discard(index)
    SHOW_ALL.discard(index)
    dashboard_section.refresh()


def _toggle_all(index, value) -> None:
    SHOW_ALL.add(index) if value else SHOW_ALL.discard(index)
    dashboard_section.refresh()


def editing() -> bool:
    """True while a panel's editor is open, so a timer must not refresh over it."""
    return bool(EDITING)
