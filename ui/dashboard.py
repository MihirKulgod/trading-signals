"""
The live dashboard: a configurable grid of condition monitors.

Each panel watches one condition block and shows its score, plus a rectangle per
direct child so a combination's internals are visible at a glance. Colour is the
primary signal, so the mapping from score to colour lives here and is used by
both the panel and its children.
"""

from __future__ import annotations

import math
from typing import Any

from nicegui import ui

MUTED = "text-sm text-gray-500"

# ---------------------------------------------------------------------------
# Score -> colour
# ---------------------------------------------------------------------------

OPEN_GREEN = (67, 160, 71)      # any score at or above zero
NEAR_ZERO = (240, 162, 2)       # just below zero: orangey yellow
FAR_NEGATIVE = (107, 15, 15)    # deeply negative: dark blood red
UNKNOWN_GREY = (130, 130, 130)  # NaN: never evaluated this cycle

# |score| at which the gradient has fully reached FAR_NEGATIVE. Scores are
# normalised, so most live between -3 and +1; the log curve spends its
# resolution there and flattens out beyond.
SATURATION = 10.0


def score_colour(score: Any) -> str:
    """CSS colour for a score. The scale is shared by panels and children."""
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return _css(UNKNOWN_GREY)
    if score >= 0:
        return _css(OPEN_GREEN)
    # Logarithmic so the crowded region just below zero stays distinguishable
    # instead of every losing score washing out to the same red.
    weight = min(1.0, math.log1p(abs(score)) / math.log1p(SATURATION))
    return _css(_blend(NEAR_ZERO, FAR_NEGATIVE, weight))


def text_colour(score: Any) -> str:
    """Black or white, whichever stays readable on ``score_colour(score)``."""
    red, green, blue = _rgb(score)
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#000000" if luminance > 0.6 else "#ffffff"


def _rgb(score: Any) -> tuple[int, int, int]:
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return UNKNOWN_GREY
    if score >= 0:
        return OPEN_GREEN
    weight = min(1.0, math.log1p(abs(score)) / math.log1p(SATURATION))
    return _blend(NEAR_ZERO, FAR_NEGATIVE, weight)


def _blend(start, end, weight: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * weight) for a, b in zip(start, end))


def _css(rgb) -> str:
    return "#%02x%02x%02x" % rgb


def format_score(score: Any) -> str:
    if score is None or (isinstance(score, float) and math.isnan(score)):
        return "—"
    return f"{score:+.3f}"


# ---------------------------------------------------------------------------
# The condition tree, built from the strategy document
# ---------------------------------------------------------------------------

# Built via the engine rather than read off the document, because a ref resolves
# to its target: the ids a panel shows must be the ids live actually traces.
_TREE_CACHE: dict[str, Any] = {}


def condition_tree(strategy_doc) -> dict[str, list[str]]:
    """node id -> direct child ids, for every node the live engine evaluates."""
    import json

    from condition import build_selected_conditions

    plain = json.loads(json.dumps(strategy_doc))
    key = json.dumps(plain.get("conditions", []), sort_keys=True)
    if _TREE_CACHE.get("key") != key:
        children: dict[str, list[str]] = {}
        try:
            for root in build_selected_conditions(plain):
                for node in root.walk():
                    children[node.id] = [child.id for child in node.sub_conditions()]
        except Exception:
            children = {}
        _TREE_CACHE.update(key=key, children=children)
    return _TREE_CACHE["children"]


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

PANEL_STYLE = "width:265px;height:180px"
EDITING: set[int] = set()


def _panels(settings_doc) -> Any:
    from ruamel.yaml.comments import CommentedSeq

    return settings_doc.setdefault("dashboard", CommentedSeq())


@ui.refreshable
def dashboard_section(settings_doc, strategy_doc, scores: dict, save) -> None:
    panels = _panels(settings_doc)
    tree = condition_tree(strategy_doc)

    with ui.row().classes("items-center gap-2 w-full"):
        ui.label("Dashboard").classes("font-medium")
        ui.button(icon="add", on_click=lambda: _add_panel(panels, save)) \
            .props("flat dense").tooltip("Add a panel")
        ui.space()
        if not scores:
            ui.label("no scores yet — start the engine").classes(MUTED)

    if len(panels) == 0:
        ui.label("Empty. Add a panel and choose a condition block to monitor.") \
            .classes(MUTED)
        return

    with ui.row().classes("w-full gap-3 flex-wrap items-start"):
        for index, panel in enumerate(panels):
            _panel(index, panel, panels, tree, scores, save)


def _panel(index, panel, panels, tree, scores, save) -> None:
    block = panel.get("block") or ""
    score = scores.get(block) if block else None
    background = score_colour(score) if block else _css(UNKNOWN_GREY)

    with ui.card().classes("p-2 gap-1").style(
            f"{PANEL_STYLE};background:{background};color:{text_colour(score)}"):
        if index in EDITING:
            _editor(index, panel, panels, tree, save)
            return

        with ui.row().classes("items-center gap-1 w-full no-wrap"):
            ui.label(panel.get("name") or "(unnamed)") \
                .classes("font-medium truncate").style("flex:1;min-width:0")
            ui.button(icon="edit", on_click=lambda i=index: _edit(i)) \
                .props("flat dense size=sm").style("color:inherit")
            ui.button(icon="close",
                      on_click=lambda i=index: _remove_panel(panels, i, save)) \
                .props("flat dense size=sm").style("color:inherit")

        ui.label(format_score(score)).classes("text-2xl font-semibold leading-none")
        _children(block, tree, scores)


def _children(block, tree, scores) -> None:
    """One rectangle per direct child. Children keep to a single level."""
    child_ids = tree.get(block, []) if block else []
    if not child_ids:
        return
    with ui.row().classes("w-full gap-1 flex-wrap content-start") \
            .style("flex:1;min-height:0;overflow:hidden"):
        for child_id in child_ids:
            child_score = scores.get(child_id)
            with ui.column().classes("items-center justify-center rounded p-1 gap-0").style(
                    f"flex:1 1 0;min-width:52px;background:{score_colour(child_score)};"
                    f"color:{text_colour(child_score)}"):
                ui.label(child_id).classes("truncate w-full text-center") \
                    .style("font-size:9px;line-height:1.1").tooltip(child_id)
                ui.label(format_score(child_score)) \
                    .style("font-size:10px;font-weight:600;line-height:1.2")


def _editor(index, panel, panels, tree, save) -> None:
    options = sorted(tree) or []
    with ui.column().classes("w-full gap-1").style("color:#000"):
        ui.select(options, value=panel.get("block") or None, label="condition block",
                  with_input=True,
                  on_change=lambda e, p=panel: _set(p, "block", e.value, save)) \
            .props("dense options-dense").classes("w-full")
        ui.input(label="display name", value=panel.get("name") or "",
                 on_change=lambda e, p=panel: _set(p, "name", e.value, save)) \
            .props("dense").classes("w-full")
        with ui.row().classes("items-center gap-1 w-full"):
            ui.button("Done", on_click=lambda i=index: _done(i)).props("flat dense size=sm")
            ui.space()
            ui.button(icon="delete",
                      on_click=lambda i=index: _remove_panel(panels, i, save)) \
                .props("flat dense size=sm color=negative")


# --- mutations -------------------------------------------------------------


def _set(panel, key, value, save) -> None:
    panel[key] = value or ""
    save()


def _add_panel(panels, save) -> None:
    from ruamel.yaml.comments import CommentedMap

    panel = CommentedMap()
    panel["block"] = ""
    panel["name"] = ""
    panels.append(panel)
    EDITING.add(len(panels) - 1)   # a new panel opens straight into its editor
    save()
    dashboard_section.refresh()


def _remove_panel(panels, index, save) -> None:
    del panels[index]
    EDITING.clear()                # indices shift, so no editor survives a removal
    save()
    dashboard_section.refresh()


def _edit(index) -> None:
    EDITING.add(index)
    dashboard_section.refresh()


def _done(index) -> None:
    EDITING.discard(index)
    dashboard_section.refresh()


def editing() -> bool:
    """True while a panel's editor is open, so a timer must not refresh over it."""
    return bool(EDITING)
