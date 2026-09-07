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

from description import readable_id
from notifications import is_met

MUTED = "text-sm text-gray-300"

# ---------------------------------------------------------------------------
# Score -> colour
# ---------------------------------------------------------------------------

OPEN_GREEN = (67, 160, 71)      # any score at or above zero
NEAR_ZERO = (246, 241, 51)      # just below zero: bright yellow (~#f6f133)
MID_NEGATIVE = (230, 126, 34)   # partway down: a clean orange, not muddy yellow-orange
FAR_NEGATIVE = (107, 15, 15)    # deeply negative: dark blood red
UNKNOWN_GREY = (130, 130, 130)  # no value to show
SKIPPED_SLATE = (74, 85, 104)   # evaluation never reached this block
MULTI_PANEL_DARK = (31, 41, 55)   # panels with several blocks: neutral shell, colour lives on each block

# |score| at which the gradient has fully reached FAR_NEGATIVE. Most
# borderline signals cluster in -1..0, so the log curve spends essentially
# all of its resolution there -- a -0.1 and a -0.4 need to look visibly
# different -- and anything past -1 is already indistinguishably "very
# negative" anyway.
SATURATION = 1.0

# Fraction of the weight range where the yellow->orange leg finishes and
# orange->red starts. A straight two-colour yellow-to-red blend spends too
# long looking like a washed-out yellow-orange; routing through a real
# orange, and reaching it early, gets to a visibly "this is going bad"
# colour well before the score is very negative.
ORANGE_AT = 0.3


def _rgb(score: Any) -> tuple[int, int, int]:
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return UNKNOWN_GREY
    if score >= 0:
        return OPEN_GREEN
    # Logarithmic so the crowded region just below zero stays distinguishable
    # instead of every losing score washing out to the same red.
    weight = min(1.0, math.log1p(abs(score)) / math.log1p(SATURATION))
    if weight <= ORANGE_AT:
        return _blend(NEAR_ZERO, MID_NEGATIVE, weight / ORANGE_AT)
    return _blend(MID_NEGATIVE, FAR_NEGATIVE, (weight - ORANGE_AT) / (1 - ORANGE_AT))


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
    key = json.dumps([plain.get("conditions", []), plain.get("definitions", [])], sort_keys=True)
    if _TREE_CACHE.get("key") != key:
        from condition import build_definitions, build_condition, selected_condition_specs

        children: dict[str, list[str]] = {}
        try:
            # Every definition is walked directly, not just ones a top-level
            # condition currently references via ref -- a definition backtested
            # on its own (the single-block button) still needs its subtree
            # linked for the inspector to navigate.
            definitions = build_definitions(plain)
            roots = list(definitions.built.values())
            roots += [build_condition(c, definitions) for c in selected_condition_specs(plain)]
            for root in roots:
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


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

PANEL_WIDTH = 265
PANEL_HEIGHT = 186
MIN_SIZE, MAX_SIZE = 0.6, 1.4
HANDLE = "dash-handle"
SHOW_ALL: set[int] = set()   # panels whose picker is showing every block

GHOST_CLASS = "dash-ghost"
# repaint() sets background/color/opacity as inline styles, which beat a
# plain class (SortableJS's default ghost styling included) -- !important is
# what actually lets the drop-preview look different from the live panel.
GHOST_CSS = f"""
.{GHOST_CLASS} {{
  opacity: 0.25 !important;
  background: #9ca3af !important;
  filter: grayscale(100%) !important;
}}
"""

PANEL_CLASS = "dash-panel"
# Container queries: every text element below sizes itself off the *actual*
# rendered space of its own panel (via cqh/cqw), not a single global number,
# so a sparse panel's text grows to fill the room a packed one doesn't have.
# The size slider still controls each panel's own box (panel_style); this is
# what makes the box's content actually use that space instead of only ever
# filling it at one fixed ratio. Tune the clamp(min, preferred, max) triples
# directly here -- they're the only numbers that matter for how this looks.
PANEL_CLASS_ROW = "dash-row"       # one row of a multi-row panel
PANEL_CLASS_CHILD = "dash-child-box"  # one rectangle in the children grid

PANEL_CSS = f"""
.{PANEL_CLASS} {{ container-type: size; container-name: dash-panel; }}
.dash-score {{ font-size: clamp(0.85rem, 14cqh, 2.4rem); line-height: 1.1; }}
.dash-note  {{ font-size: clamp(0.55rem, 8cqh, 1.25rem); }}

/* Rows share the panel's fixed leftover height (flex: 1 1 0 on each), so
   a row's own box shrinks as more rows are added -- cqh here is that row's
   own height, not the whole panel's, which is what makes text respond to
   row *count*, not just to the panel getting bigger via the size slider. */
.{PANEL_CLASS_ROW} {{ container-type: size; container-name: dash-row; flex: 1 1 0; min-height: 0; }}
.dash-row-label {{ font-size: clamp(0.5rem, 70cqh, 1.45rem); }}
.dash-row-value {{ font-size: clamp(0.5rem, 70cqh, 1.45rem); }}
.dash-row-meter {{ font-size: clamp(0.45rem, 40cqh, 1.0rem); }}

/* Same idea for the children grid: each box already shrinks in width as
   more of them wrap into a row (flex: 1 1 0), so cqw against the box
   itself (not the panel) is what makes text respond to child count. */
.{PANEL_CLASS_CHILD} {{ container-type: inline-size; container-name: dash-child; }}
.dash-child-label {{ font-size: clamp(0.5rem, 20cqw, 1.05rem); line-height: 1.15; }}
.dash-child-value {{ font-size: clamp(0.55rem, 20cqw, 1.3rem); line-height: 1.25; }}
.dash-child-meter  {{ font-size: clamp(0.5rem, 18cqw, 1.1rem); line-height: 1.15; }}
"""

# Element handles kept from the last build, so a tick can repaint values in
# place. Rebuilding the grid each second cancelled any drag in progress.
LIVE: dict[str, Any] = {"ring": None, "status": None, "panels": [], "show_hidden": False,
                        "compact_children": []}


def dashboard_config(settings_doc) -> Any:
    """The dashboard block, migrating the older bare-list form in passing."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    node = settings_doc.get("dashboard")
    if isinstance(node, list):
        migrated = CommentedMap()
        migrated["size"] = 1.0
        migrated["panels"] = node
        node = migrated
    elif not isinstance(node, dict):
        node = CommentedMap()
    settings_doc["dashboard"] = node
    node.setdefault("size", 1.0)
    node.setdefault("panels", CommentedSeq())
    node.setdefault("compact_children", CommentedSeq())
    return node


def _panels(settings_doc) -> Any:
    return dashboard_config(settings_doc)["panels"]


def panel_style(size: float) -> str:
    return f"width:{round(PANEL_WIDTH * size)}px;height:{round(PANEL_HEIGHT * size)}px"


def _countdown(service) -> None:
    """The ring plus a status word; both are updated in place by repaint()."""
    with ui.row().classes("items-center gap-2 no-wrap"):
        LIVE["ring"] = ui.circular_progress(
            value=0.0, min=0.0, max=1.0, size="34px", show_value=False) \
            .tooltip("Time until the next signal recalculation")
        LIVE["status"] = ui.label("").classes("text-xs font-medium") \
            .tooltip("Time since this recalculation was due to start")
    _paint_countdown(service)


def _paint_countdown(service) -> None:
    fraction, overdue = countdown_state(service)
    running = getattr(service, "state", "") == "running"
    due = overdue > 0 and running
    LIVE["ring"].set_value(1.0 if due else fraction)
    LIVE["ring"].props(f"color={'red' if due else ('primary' if running else 'grey-5')}")
    LIVE["status"].set_text("idle" if not running else (humanise(overdue) if due else ""))
    LIVE["status"].style("color:" + ("#dc2626" if due else "#6b7280"))


@ui.refreshable
def dashboard_section(settings_doc, strategy_doc, service, save) -> None:
    config = dashboard_config(settings_doc)
    panels = config["panels"]
    size = float(config.get("size", 1.0) or 1.0)
    tree = condition_tree(strategy_doc)
    disabled = disabled_ids(strategy_doc)
    compact_children = config["compact_children"]
    LIVE["panels"] = []
    LIVE["tree"] = tree
    LIVE["compact_children"] = compact_children

    with ui.row().classes("items-center gap-3 w-full"):
        ui.label("Dashboard").classes("font-medium")
        _countdown(service)
        ui.space()
        ui.label("size").classes(MUTED)
        # Dragging only restyles; the value is stored on release, so a drag
        # does not rewrite settings.yaml on every step.
        slider = ui.slider(min=MIN_SIZE, max=MAX_SIZE, step=0.05, value=size,
                           on_change=lambda e: resize(clamp_size(e.value))) \
            .props("dense").style("width:130px") \
            .tooltip("Smaller panels fit more per row")
        slider.on("change", lambda _: _set_size(config, slider.value, save))
        ui.switch("show hidden", value=LIVE["show_hidden"],
                  on_change=lambda e, s=service: _toggle_show_hidden(e.value, s)) \
            .props("dense").tooltip(
                "Force-show panels a visibility condition is currently hiding, "
                "so you can still edit them (marked with a dashed border)")
        ui.button(icon="add", on_click=lambda: _add_panel(panels, tree, strategy_doc, save)) \
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
        ghost_class=GHOST_CLASS,
    )
    with grid:
        for index, panel in enumerate(panels):
            _panel(index, panel, panels, tree, disabled, strategy_doc, size, save, compact_children, service)
    repaint(service)


def _panel(index, panel, panels, tree, disabled, strategy_doc, size, save, compact_children, service) -> None:
    block = panel.get("block") or ""
    visible_when = panel.get("visible_when")
    rows_cfg = panel.get("rows") or []
    card = ui.card().classes(f"p-2 gap-1 {PANEL_CLASS}").style(panel_style(size))
    record = {"block": block, "card": card, "disabled": block in disabled,
              "visible_when": visible_when if isinstance(visible_when, dict) else None,
              "score": None, "note": None, "children": [], "rows": []}

    with card:
        with ui.row().classes("items-center gap-1 w-full no-wrap"):
            ui.icon("drag_indicator").classes(f"{HANDLE} cursor-move") \
                .style("opacity:0.6").tooltip("Drag to reorder")
            ui.label(panel.get("name") or "(unnamed)") \
                .classes("font-medium truncate").style("flex:1;min-width:0")
            bad_ids = _invalid_targets(panel, tree)
            if bad_ids:
                ui.badge("Invalid").props("color=negative") \
                    .tooltip("Unknown id(s): " + ", ".join(bad_ids))
            ui.button(icon="edit",
                      on_click=lambda i=index, p=panel: _edit(i, p, panels, tree, strategy_doc, save)) \
                .props("flat dense size=sm").style("color:inherit")
            ui.button(icon="close",
                      on_click=lambda i=index, p=panel: _confirm_delete(
                          f"Delete panel '{p.get('name') or '(unnamed)'}'?",
                          lambda: _remove_panel(panels, i, save))) \
                .props("flat dense size=sm").style("color:inherit")

        if rows_cfg:
            _rows(record, rows_cfg, tree)
        else:
            with ui.row().classes("items-baseline gap-2 w-full no-wrap"):
                record["score"] = ui.label("").classes("font-semibold dash-score")
                record["note"] = ui.label("").classes("dash-note").style("opacity:0.85")
            if not panel.get("hide_children"):
                _children(record, block, tree, compact_children, save, service)

    LIVE["panels"].append(record)


def _invalid_targets(panel, tree) -> list[str]:
    """
    Every id this panel references that doesn't exist in the strategy (a
    condition renamed or deleted out from under a saved dashboard config).
    An unset field isn't an error -- only a non-empty id that fails to
    resolve is.
    """
    bad = []
    rows_cfg = panel.get("rows") or []
    block = panel.get("block") or ""
    if not rows_cfg and block and block not in tree:
        bad.append(block)
    for row in rows_cfg:
        target = row.get("target") or ""
        if target and target not in tree:
            bad.append(target)
    vw = panel.get("visible_when")
    if isinstance(vw, dict):
        target = vw.get("target") or ""
        if target and target not in tree:
            bad.append(target)
        also_target = vw.get("also_target") or ""
        if also_target and also_target not in tree:
            bad.append(also_target)
    return bad


def _visible_children(tree, node_id: str) -> list[str]:
    """
    Direct children worth showing. An id ending '_inner'/'-inner' is a
    synthetic wrapper a condition's own implementation introduces (e.g.
    Below is built as Not(Above), and that Above gets id '{id}_inner') --
    nobody wrote it in the strategy, so it's never worth surfacing as a
    child block or counting in a met/total meter.
    """
    return [c for c in (tree.get(node_id) or [])
            if not (c.endswith("_inner") or c.endswith("-inner"))]


def _rows(record, rows_cfg, tree) -> None:
    """
    Several independently-labelled targets in one panel, e.g. a tri-state
    'T60 Up / Unclear / Down' readout, instead of the usual single block.
    Space is tighter with multiple rows, so each one only gets a compact
    met/total meter for its own children (same math _children() uses), not
    a full rectangle-per-child grid.
    """
    with ui.column().classes("w-full gap-0").style("flex:1;min-height:0;overflow:hidden"):
        for row_cfg in rows_cfg:
            target = row_cfg.get("target") or ""
            label_text = row_cfg.get("label") or target
            invalid = bool(target) and target not in tree
            child_ids = _visible_children(tree, target) if target else []
            with ui.row().classes(f"{PANEL_CLASS_ROW} items-center justify-between w-full gap-1 no-wrap"):
                ui.label(label_text).classes("dash-row-label truncate") \
                    .style(f"flex:1;min-width:0;{'color:#dc2626' if invalid else ''}") \
                    .tooltip("Unknown id" if invalid else target)
                meter = None
                if child_ids:
                    meter = ui.label("").classes("dash-row-meter").style("opacity:0.85") \
                        .tooltip(f"met / total of {target}'s own children")
                value = ui.label("").classes("dash-row-value font-semibold px-1 rounded")
            record["rows"].append((target, value, meter, child_ids))


def _short_child_label(child_id: str, parent_id: str) -> str:
    """
    Drop whatever leading '-'-separated tokens a child shares with its
    parent (e.g. c01-t15's child c01-t15-ema-up reads as just "Ema Up"),
    then run the rest through readable_id -- so a block's own id prefix
    doesn't get repeated across every one of its children's labels.
    """
    child_parts = child_id.split("-")
    parent_parts = parent_id.split("-") if parent_id else []
    i = 0
    while i < len(child_parts) - 1 and i < len(parent_parts) and child_parts[i] == parent_parts[i]:
        i += 1
    return readable_id("-".join(child_parts[i:]) or child_id)


def _toggle_compact(child_id: str, compact_children, save, service) -> None:
    """
    Flips a child between showing its score and showing just name + a
    met/total meter -- persisted so the choice survives a reload, not just
    kept for this browser session. Repaints immediately rather than waiting
    for the next tick, or the click would sit unreflected for up to a
    second.
    """
    if child_id in compact_children:
        compact_children.remove(child_id)
    else:
        compact_children.append(child_id)
    save()
    repaint(service)


def _children(record, block, tree, compact_children, save, service) -> None:
    """
    One rectangle per direct child. Children keep to a single level, but a
    child with children of its own also gets a met/total meter beneath its
    score, e.g. '3/6', for how many of those grandchildren currently score
    positive -- and a small tooltip listing each grandchild's own current
    score, so the meter's count can be checked without opening the editor.
    Just names and numbers, no descriptions, and short enough to never need
    to scroll inside the tooltip itself.

    Clicking a child with a meter toggles it into a compact mode -- name and
    meter only, no score -- coloured off the met/total fraction instead of
    the child's own score.
    """
    child_ids = _visible_children(tree, block) if block else []
    if not child_ids:
        return
    with ui.row().classes("w-full gap-1 flex-wrap content-stretch") \
            .style("flex:1;min-height:0;overflow:hidden;align-items:stretch"):
        for child_id in child_ids:
            grandchild_ids = _visible_children(tree, child_id)
            box = ui.column().classes(f"{PANEL_CLASS_CHILD} items-center justify-center rounded p-1 gap-0") \
                .style("flex:1 1 0;min-width:52px")
            if grandchild_ids:
                box.classes("cursor-pointer") \
                    .on("click", lambda cid=child_id: _toggle_compact(cid, compact_children, save, service))
            gc_labels = []
            with box:
                if grandchild_ids:
                    with ui.tooltip().style("max-width:240px;overflow:visible"):
                        with ui.column().classes("gap-0"):
                            for gid in grandchild_ids:
                                with ui.row().classes("items-center justify-between gap-2 no-wrap w-full"):
                                    ui.label(_short_child_label(gid, child_id)).classes("text-xs")
                                    gc_labels.append(ui.label("").classes("text-xs font-semibold"))
                ui.label(_short_child_label(child_id, block)).classes("dash-child-label truncate w-full text-center")
                value = ui.label("").classes("dash-child-value font-semibold")
                meter = None
                if grandchild_ids:
                    meter = ui.label("").classes("dash-child-meter").style("opacity:0.85")
            record["children"].append((child_id, box, value, meter, grandchild_ids, gc_labels))


def _panel_visible(visible_when, scores, tree, engine_ran) -> bool:
    """
    A panel with no visible_when is always shown. One that has it stays
    hidden until the engine has actually produced a value for its target --
    showing a gated panel off of nothing would be misleading, not helpful.

    ``also_target`` is an optional second condition ANDed with the first --
    e.g. a combination that's only live once its T60 regime AND the
    post-open time gate both hold, which a single target can't express.
    """
    if visible_when is None:
        return True
    if not engine_ran:
        return False
    target = visible_when.get("target")
    if not target:
        return False
    kind = visible_when.get("kind", "state")
    params = visible_when.get("params") or {}
    if not is_met(kind, target, scores, tree, params):
        return False
    also_target = visible_when.get("also_target")
    if also_target:
        also_kind = visible_when.get("also_kind", "state")
        also_params = visible_when.get("also_params") or {}
        if not is_met(also_kind, also_target, scores, tree, also_params):
            return False
    return True


def _toggle_show_hidden(value: bool, service) -> None:
    """
    Panels a visibility condition is hiding aren't just invisible, they're
    display:none -- completely uninteractive, so their own edit button can't
    be clicked either. This is the only way back in to fix one until its
    condition happens to become true on its own.
    """
    LIVE["show_hidden"] = value
    repaint(service)


def repaint(service) -> None:
    """
    Update scores and colours on the existing elements.

    Deliberately not a refresh: rebuilding the grid on every tick tore out the
    element being dragged, so a reorder only survived if it beat the timer.
    Gated panels are handled the same way -- always present in the DOM, just
    toggled via display:none -- so a visibility flip can never interrupt a
    drag either, and drag order (make_sortable) is unaffected either way.
    """
    if not LIVE["panels"] and LIVE["ring"] is None:
        return
    scores = getattr(service, "node_scores", {}) or {}
    engine_ran = getattr(service, "last_run", None) is not None
    tree = LIVE.get("tree") or {}
    compact_children = LIVE.get("compact_children") or []
    try:
        if LIVE["ring"] is not None:
            _paint_countdown(service)
        for record in LIVE["panels"]:
            state, value = block_state(record["block"], scores, engine_ran)
            actually_visible = _panel_visible(record["visible_when"], scores, tree, engine_ran)
            forced = LIVE["show_hidden"] and not actually_visible
            visible = actually_visible or LIVE["show_hidden"]
            border = "border:2px dashed #6b7280;" if forced else "border:2px solid transparent;"
            # A rows panel (e.g. T60) holds several unrelated blocks with no
            # overall score of its own, so its shell shouldn't be tinted by
            # any one of them. A panel with a single block (children grid or
            # not) does have a real overall score -- just green when it's
            # positive, the same dark shell otherwise, not the full
            # gradient (that belongs to the children, not the parent).
            multi = bool(record["rows"])
            if multi:
                bg, fg = _css(MULTI_PANEL_DARK), "#e5e7eb"
            elif state == "value" and value >= 0:
                bg, fg = _css(OPEN_GREEN), _on(OPEN_GREEN)
            else:
                bg, fg = _css(MULTI_PANEL_DARK), "#e5e7eb"
            record["card"].style(f"display:{'flex' if visible else 'none'};"
                                 f"{border}"
                                 f"background:{bg};"
                                 f"color:{fg};"
                                 f"opacity:{0.55 if record['disabled'] else 1}")
            if record["score"] is not None:
                record["score"].set_text(format_score(value))
                record["note"].set_text(
                    "disabled" if record["disabled"]
                    else ("" if state == "value" else STATE_LABEL[state]))
            for child_id, box, label, meter, grandchild_ids, gc_labels in record["children"]:
                child_state, child_value = block_state(child_id, scores, engine_ran)
                met = 0
                if meter is not None:
                    for gid, gc_label in zip(grandchild_ids, gc_labels):
                        gc_state, gc_value = block_state(gid, scores, engine_ran)
                        gc_label.set_text("\u00b7" if gc_state == "skipped"
                                          else format_score(gc_value))
                        if gc_state == "value" and gc_value >= 0:
                            met += 1
                    meter.set_text(f"{met}/{len(grandchild_ids)}")
                compact = bool(grandchild_ids) and child_id in compact_children
                if compact and child_state == "value":
                    # Same green-to-red scale a score uses, just fed the
                    # met/total fraction instead: X/X lands on the score>=0
                    # boundary, 0/X on the fully-saturated negative end.
                    pseudo_score = (met / len(grandchild_ids) - 1) * SATURATION
                    box.style(f"background:{score_colour(pseudo_score)};"
                              f"color:{text_colour(pseudo_score)}")
                else:
                    # No real score yet (nan/skipped/waiting/unset) -- the
                    # met/total fraction would just read as "0 met" and paint
                    # dark red, which looks like a real bad result rather
                    # than "nothing to show", so this keeps the same neutral
                    # grey the normal (non-compact) view already uses.
                    box.style(f"background:{state_colour(child_state, child_value)};"
                              f"color:{state_text_colour(child_state, child_value)}")
                label.set_visibility(not compact)
                if not compact:
                    label.set_text("\u00b7" if child_state == "skipped"
                                   else format_score(child_value))
            for target, value_el, meter, child_ids in record["rows"]:
                row_state, row_value = block_state(target, scores, engine_ran)
                value_el.style(f"background:{state_colour(row_state, row_value)};"
                               f"color:{state_text_colour(row_state, row_value)}")
                value_el.set_text("\u00b7" if row_state == "skipped"
                                  else format_score(row_value))
                if meter is not None:
                    met = sum(1 for cid in child_ids
                              if block_state(cid, scores, engine_ran)[0] == "value"
                              and block_state(cid, scores, engine_ran)[1] >= 0)
                    meter.set_text(f"{met}/{len(child_ids)}")
    except Exception:
        # Elements from a previous page build; the next render replaces them.
        LIVE["panels"] = []


def resize(size: float) -> None:
    """
    Panel size is only CSS, so apply it without rebuilding the grid. Text
    inside no longer needs a separate push: it's sized off the container's
    own dimensions (see PANEL_CSS), so changing the box here is enough for
    font sizes to follow on their own.
    """
    for record in LIVE["panels"]:
        record["card"].style(panel_style(size))


# --- mutations -------------------------------------------------------------


def clamp_size(value) -> float:
    if value is None:
        return 1.0
    return max(MIN_SIZE, min(MAX_SIZE, float(value)))


def _set_size(config, value, save) -> None:
    """Called on slider release; dragging only calls resize()."""
    size = clamp_size(value)
    config["size"] = round(size, 2)
    resize(size)
    save()


def _set(panel, key, value, save) -> None:
    panel[key] = value or ""
    save()


def _as_map(entry: dict):
    from ruamel.yaml.comments import CommentedMap

    panel = CommentedMap()
    panel["block"] = entry.get("block", "")
    panel["name"] = entry.get("name", "")
    return panel


def _add_panel(panels, tree, strategy_doc, save) -> None:
    panels.append(_as_map({}))
    SHOW_ALL.clear()
    save()
    dashboard_section.refresh()
    _edit(len(panels) - 1, panels[-1], panels, tree, strategy_doc, save)   # opens straight into its editor


def _reset_panels(panels, strategy_doc, save) -> None:
    panels[:] = [_as_map(entry) for entry in default_panels(strategy_doc)]
    SHOW_ALL.clear()
    save()
    dashboard_section.refresh()


def _confirm_delete(message: str, action) -> None:
    """A modal yes/no gate in front of a destructive action -- deleting was
    otherwise a single accidental click with no way back."""
    with ui.dialog() as dialog, ui.card().classes("gap-3"):
        ui.label(message)
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def _confirmed() -> None:
                dialog.close()
                action()

            ui.button("Delete", on_click=_confirmed).props("flat color=negative")
    dialog.open()


def _remove_panel(panels, index, save) -> None:
    del panels[index]
    SHOW_ALL.clear()               # indices shift, so stale per-index state can't survive
    save()
    dashboard_section.refresh()


def _remove_panel_and_close(panels, index, save, dialog) -> None:
    _remove_panel(panels, index, save)
    dialog.close()


def _move(panels, old_index, new_index, save) -> None:
    if old_index == new_index:
        return
    panels.insert(new_index, panels.pop(old_index))
    SHOW_ALL.clear()
    save()
    dashboard_section.refresh()


def _edit(index, panel, panels, tree, strategy_doc, save) -> None:
    """
    Opens the panel's editor as a centered modal dialog (dimmed backdrop,
    same as every other dialog in this app) instead of swapping the panel's
    own small card content -- there isn't room in there for a form.

    The dialog's content is its own fresh refreshable, separate from
    dashboard_section's: a structural change inside the form (toggling
    'show every block', adding/removing a row, flipping conditional
    visibility) only needs to redraw the dialog, not the whole grid behind
    it, and doing so can't interrupt a drag in progress out there either.
    """
    SHOW_ALL.discard(index)

    @ui.refreshable
    def _body() -> None:
        _editor(index, panel, panels, tree, strategy_doc, save, dialog, _body.refresh)

    with ui.dialog() as dialog, ui.card().classes("w-[480px] max-w-[95vw] gap-2"):
        _body()
    dialog.open()


def _finish_edit(dialog) -> None:
    """Done: the grid only needs to reflect field edits once you're done with
    them, matching how they've never live-updated behind the form either."""
    dashboard_section.refresh()
    dialog.close()


def _toggle_all(index, value, refresh) -> None:
    SHOW_ALL.add(index) if value else SHOW_ALL.discard(index)
    refresh()


def _toggle_hide_children(panel, value, save) -> None:
    panel["hide_children"] = value
    save()


def _toggle_visible_when(panel, value, save, refresh) -> None:
    """Turning this off removes the key entirely, matching 'no visible_when
    means always shown' -- the field structurally changes, so this refreshes."""
    from ruamel.yaml.comments import CommentedMap

    if value:
        panel["visible_when"] = CommentedMap([("target", ""), ("kind", "state")])
    else:
        panel.pop("visible_when", None)
    save()
    refresh()


def _set_visible_when(panel, key, value, save, refresh) -> None:
    vw = panel.get("visible_when")
    if not isinstance(vw, dict):
        return
    vw[key] = value
    save()
    if key == "kind":
        refresh()   # min-met field appears only for children_met


def _set_visible_when_min_met(panel, value, save) -> None:
    from ruamel.yaml.comments import CommentedMap

    vw = panel.get("visible_when")
    if not isinstance(vw, dict):
        return
    n = int(value) if value not in (None, "") else 1
    vw.setdefault("params", CommentedMap())["min_met"] = max(1, n)
    save()


def _toggle_also_visible_when(panel, value, save, refresh) -> None:
    """Turning this off drops also_target entirely, matching 'no also_target
    means single-condition visibility' -- the field structurally changes."""
    vw = panel.get("visible_when")
    if not isinstance(vw, dict):
        return
    if value:
        vw["also_target"], vw["also_kind"] = "", "state"
    else:
        vw.pop("also_target", None)
        vw.pop("also_kind", None)
        vw.pop("also_params", None)
    save()
    refresh()


def _set_also_visible_when_min_met(panel, value, save) -> None:
    from ruamel.yaml.comments import CommentedMap

    vw = panel.get("visible_when")
    if not isinstance(vw, dict):
        return
    n = int(value) if value not in (None, "") else 1
    vw.setdefault("also_params", CommentedMap())["min_met"] = max(1, n)
    save()


def _add_row(panel, save, refresh) -> None:
    """Rows replace the usual single-block view, so the block field stays
    (harmless -- rendering ignores it once rows are non-empty) but adding
    the first row is what switches a panel into multi-row mode."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    rows = panel.setdefault("rows", CommentedSeq())
    rows.append(CommentedMap([("label", ""), ("target", "")]))
    save()
    refresh()


def _remove_row(panel, index, save, refresh) -> None:
    rows = panel.get("rows")
    if isinstance(rows, list) and 0 <= index < len(rows):
        del rows[index]
    save()
    refresh()


def _set_row_field(row, key, value, save) -> None:
    row[key] = value
    save()


def _options_with(options: list, current) -> list:
    """
    A select's value must be one of its options, or nicegui raises ValueError
    right in __init__ -- before the dialog can even open. A condition id that
    got renamed or deleted out from under a saved panel would otherwise crash
    the editor shut, with no way back in to fix it. Appending the stale value
    keeps the field open and editable (it'll show as an odd extra entry,
    matching the 'Invalid' badge already shown on the panel itself) instead
    of hiding the problem behind a crash.
    """
    if current and current not in options:
        return options + [current]
    return options


def _editor(index, panel, panels, tree, strategy_doc, save, dialog, refresh) -> None:
    """A panel's full edit form, inside the modal _edit() opened."""
    options = sorted(tree.keys()) if index in SHOW_ALL else top_level_ids(strategy_doc)
    vw = panel.get("visible_when")
    rows = panel.get("rows") or []
    with ui.column().classes("w-full gap-1"):
        ui.select(_options_with(options, panel.get("block")), value=panel.get("block") or None,
                  label="block", with_input=True,
                  on_change=lambda e: _set(panel, "block", e.value, save)) \
            .props("dense").classes("w-full") \
            .tooltip("Ignored once this panel has rows below")
        ui.input(label="name", value=panel.get("name", ""),
                 on_change=lambda e: _set(panel, "name", e.value, save)) \
            .props("dense").classes("w-full")
        ui.switch("show every block", value=index in SHOW_ALL,
                  on_change=lambda e, i=index: _toggle_all(i, e.value, refresh)).props("dense")
        ui.switch("hide children", value=bool(panel.get("hide_children")),
                  on_change=lambda e, p=panel: _toggle_hide_children(p, e.value, save)) \
            .props("dense").tooltip("Show just the score, without a rectangle per child")
        ui.switch("conditionally visible", value=isinstance(vw, dict),
                  on_change=lambda e, p=panel: _toggle_visible_when(p, e.value, save, refresh)) \
            .props("dense").tooltip("Only show this panel while another block is met")
        if isinstance(vw, dict):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.select(_options_with(sorted(tree.keys()), vw.get("target")), value=vw.get("target") or None,
                          label="visible when", with_input=True,
                          on_change=lambda e, p=panel: _set_visible_when(p, "target", e.value, save, refresh)) \
                    .props("dense").classes("min-w-[200px]").style("flex:1")
                ui.select(["state", "children_met"], value=vw.get("kind", "state"), label="kind",
                          on_change=lambda e, p=panel: _set_visible_when(p, "kind", e.value, save, refresh)) \
                    .props("dense").classes("min-w-[120px]")
                if vw.get("kind") == "children_met":
                    ui.number(label="min met", value=(vw.get("params") or {}).get("min_met", 1),
                             precision=0, format="%d", min=1,
                             on_change=lambda e, p=panel: _set_visible_when_min_met(p, e.value, save)) \
                        .props("dense").classes("min-w-[90px]")
            ui.switch("also require", value=bool(vw.get("also_target")),
                      on_change=lambda e, p=panel: _toggle_also_visible_when(p, e.value, save, refresh)) \
                .props("dense").tooltip("AND a second condition in, e.g. a time gate alongside a T60 state")
            if vw.get("also_target"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.select(_options_with(sorted(tree.keys()), vw.get("also_target")),
                              value=vw.get("also_target") or None, label="and also", with_input=True,
                              on_change=lambda e, p=panel: _set_visible_when(p, "also_target", e.value, save, refresh)) \
                        .props("dense").classes("min-w-[200px]").style("flex:1")
                    ui.select(["state", "children_met"], value=vw.get("also_kind", "state"), label="kind",
                              on_change=lambda e, p=panel: _set_visible_when(p, "also_kind", e.value, save, refresh)) \
                        .props("dense").classes("min-w-[120px]")
                    if vw.get("also_kind") == "children_met":
                        ui.number(label="min met", value=(vw.get("also_params") or {}).get("min_met", 1),
                                 precision=0, format="%d", min=1,
                                 on_change=lambda e, p=panel: _set_also_visible_when_min_met(p, e.value, save)) \
                            .props("dense").classes("min-w-[90px]")

        ui.separator()
        ui.label("Rows: multiple blocks in one panel (leave empty for the usual "
                 "single block + full children view)").classes(MUTED)
        for r_idx, row in enumerate(rows):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.input(label="label", value=row.get("label", ""),
                         on_change=lambda e, r=row: _set_row_field(r, "label", e.value, save)) \
                    .props("dense").classes("min-w-[120px]")
                ui.select(_options_with(sorted(tree.keys()), row.get("target")),
                          value=row.get("target") or None, label="target", with_input=True,
                          on_change=lambda e, r=row: _set_row_field(r, "target", e.value, save)) \
                    .props("dense").classes("min-w-[160px]").style("flex:1")
                ui.button(icon="delete",
                          on_click=lambda p=panel, i=r_idx: _confirm_delete(
                              "Delete this row?", lambda: _remove_row(p, i, save, refresh))) \
                    .props("flat dense color=negative")
        ui.button(icon="add", on_click=lambda p=panel: _add_row(p, save, refresh)) \
            .props("flat dense").tooltip("Add row")

        with ui.row().classes("items-center gap-1 w-full justify-end"):
            ui.button(icon="delete",
                      on_click=lambda i=index, p=panel: _confirm_delete(
                          f"Delete panel '{p.get('name') or '(unnamed)'}'?",
                          lambda: _remove_panel_and_close(panels, i, save, dialog))) \
                .props("flat dense color=negative")
            ui.button("Done", icon="check", on_click=lambda: _finish_edit(dialog)) \
                .props("flat dense")
