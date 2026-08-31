"""
Config-editor UI (NiceGUI).

Step 3: the "simple" tabs (Display, Historical, General, Indicators) are now
editable, with dropdowns sourced from ``ui.vocabulary``. Editing mutates the raw
ruamel documents in place (so comments/order survive), and leaving a tab is
gated on validation: the tab's document is validated via ``ui.schema`` and, only
if valid, written to disk. An invalid tab blocks navigation and reports why.

The Conditions tab remains the read-only recursive view from step 2; making it
editable is step 4.
"""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any, Callable

from nicegui import ui
from pydantic import ValidationError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

import app_paths
import condition
import description
import secrets_store
from ui import operations, persistence, vocabulary

MUTED = "text-sm text-gray-500"

# ---------------------------------------------------------------------------
# Editor state: the two raw documents are the single source of truth.
# ---------------------------------------------------------------------------

DOCS: dict[str, Any] = {}
PATHS = {
    "settings": persistence.SETTINGS_PATH,
    "strategy": persistence.STRATEGY_PATH,
}
VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "settings": persistence.validate_settings,
    "strategy": persistence.validate_strategy,
}

# label -> which document that tab edits
TAB_DOC = {
    "Settings · Display": "settings",
    "Strategy · General": "strategy",
    "Strategy · Indicators": "strategy",
    "Strategy · Definitions": "strategy",
    "Strategy · Conditions": "strategy",
    "Run · Backtest": None,
    "Run · Live": None,
    "Run · Charts": None,
}


def _load_docs() -> None:
    DOCS["settings"] = persistence.load_document(persistence.SETTINGS_PATH)
    DOCS["strategy"] = persistence.load_document(persistence.STRATEGY_PATH)


# ---------------------------------------------------------------------------
# Small input helpers
# ---------------------------------------------------------------------------


def _new_map(pairs: list[tuple[str, Any]]) -> CommentedMap:
    m = CommentedMap()
    for k, v in pairs:
        m[k] = v
    return m


def _select(label, options, target, key, *, with_input=False, multiple=False, on_change=None):
    """A select whose option list always includes the target's current value."""
    current = target.get(key)
    opts = list(options)
    values = current if isinstance(current, list) else [current]
    for v in values:
        if v is not None and v not in opts:
            opts.append(v)
    sel = ui.select(
        opts, label=label, value=current, multiple=multiple,
        with_input=with_input,
        new_value_mode="add-unique" if with_input else None,
        on_change=on_change,
    ).classes("min-w-[180px]")
    sel.bind_value(target, key)
    return sel


def _text(label, target, key, *, forward=None, backward=None):
    inp = ui.input(label=label).classes("min-w-[180px]")
    inp.bind_value(target, key, forward=forward, backward=backward)
    return inp


# ---------------------------------------------------------------------------
# Settings · Display
# ---------------------------------------------------------------------------


@ui.refreshable
def _display_tab() -> None:
    strategy = DOCS["strategy"]
    display = DOCS["settings"].setdefault("display", CommentedMap())

    # --- panels ---
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Panels").classes("font-medium")
            ui.button(icon="add", on_click=lambda: _add_panel(display)) \
                .props("flat dense").tooltip("Add panel")
        panels = display.setdefault("display_panels", CommentedSeq())
        for p_idx, panel in enumerate(panels):
            with ui.card().classes("w-full bg-gray-50"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.label(f"Panel {p_idx}").classes(MUTED)
                    ui.space()
                    ui.button(icon="add", on_click=lambda pl=panel: _add_panel_col(pl)) \
                        .props("flat dense").tooltip("Add column")
                    ui.button(icon="delete", on_click=lambda i=p_idx: _remove_panel(display, i)) \
                        .props("flat dense color=negative").tooltip("Remove panel")
                alias_opts = vocabulary.alias_names(strategy)
                for col_name in list(panel.keys()):
                    with ui.row().classes("items-center gap-2"):
                        # Column name is the dict *key*; renaming rebuilds the map.
                        ui.select(
                            _with_current(alias_opts, col_name), label="column",
                            value=col_name, with_input=True, new_value_mode="add-unique",
                            on_change=lambda e, pl=panel, old=col_name:
                                _rename_panel_col(pl, old, e.value),
                        ).props("dense").classes("min-w-[160px]")
                        # Color is the dict *value*; bind straight to it.
                        _select("color", vocabulary.COLOR_OPTIONS, panel, col_name,
                                with_input=True).props("dense")
                        ui.button(icon="delete",
                                  on_click=lambda pl=panel, c=col_name: _remove_panel_col(pl, c)) \
                            .props("flat dense color=negative")

    # --- scalar / enum fields ---
    with ui.card().classes("w-full"):
        ui.number(label="examination_window", value=display.get("examination_window"),
                  precision=0, format="%d",
                  on_change=lambda e: display.__setitem__(
                      "examination_window", int(e.value) if e.value is not None else None)) \
            .classes("min-w-[180px]")

        instr_ids = vocabulary.instrument_ids(strategy)
        _select("instrument_id", instr_ids, display, "instrument_id")

        # Offer every instrument's timeframes (superset) so changing instrument_id
        # doesn't require a full-tab refresh (which risks binding cascades).
        tfs = vocabulary.all_timeframes(strategy) \
            or vocabulary.timeframes_for(strategy, display.get("instrument_id"))
        _select("timeframe", tfs, display, "timeframe")

        _select("signal_aggregates", vocabulary.SIGNAL_AGGREGATE_OPTIONS,
                display, "signal_aggregates", multiple=True, with_input=True)


def _with_current(options: list, current: Any) -> list:
    opts = list(options)
    if current is not None and current not in opts:
        opts.append(current)
    return opts


def _rename_panel_col(panel: CommentedMap, old: str, new: str) -> None:
    if not new or new == old:
        return
    items = [((new if k == old else k), v) for k, v in panel.items()]
    panel.clear()
    for k, v in items:
        panel[k] = v
    _display_tab.refresh()


def _add_panel(display: CommentedMap) -> None:
    display.setdefault("display_panels", CommentedSeq()).append(_new_map([("", "black")]))
    _display_tab.refresh()


def _remove_panel(display: CommentedMap, idx: int) -> None:
    del display["display_panels"][idx]
    _display_tab.refresh()


def _add_panel_col(panel: CommentedMap) -> None:
    key = "new_column"
    n = 0
    while key in panel:
        n += 1
        key = f"new_column_{n}"
    panel[key] = "black"
    _display_tab.refresh()


def _remove_panel_col(panel: CommentedMap, col_name: str) -> None:
    del panel[col_name]
    _display_tab.refresh()


# ---------------------------------------------------------------------------
# Strategy · General
# ---------------------------------------------------------------------------


@ui.refreshable
def _general_tab() -> None:
    general = DOCS["strategy"].setdefault("general", CommentedMap())
    exchanges = general.setdefault("exchanges", CommentedSeq())
    for exchange in exchanges:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2 w-full"):
                _text("exchange", exchange, "exchange").props("dense")
                ui.space()
                ui.button(icon="add", on_click=lambda ex=exchange: _add_instrument(ex)) \
                    .props("flat dense").tooltip("Add instrument")
            for i_idx, instr in enumerate(exchange.get("instruments", [])):
                _instrument_editor(exchange, i_idx, instr)


def _instrument_editor(exchange: CommentedMap, idx: int, instr: CommentedMap) -> None:
    is_spot = instr.get("trading_symbol") is not None
    with ui.card().classes("w-full bg-gray-50"):
        with ui.row().classes("items-center gap-2 w-full"):
            _text("id", instr, "id").props("dense")
            ui.toggle({"spot": "spot", "rolling": "rolling"},
                      value="spot" if is_spot else "rolling",
                      on_change=lambda e, ins=instr: _set_instrument_kind(ins, e.value)) \
                .props("dense")
            ui.space()
            ui.button(icon="delete",
                      on_click=lambda ex=exchange, i=idx: _remove_instrument(ex, i)) \
                .props("flat dense color=negative")
        with ui.row().classes("items-center gap-2"):
            if is_spot:
                _text("trading_symbol", instr, "trading_symbol").props("dense")
            else:
                _text("name", instr, "name").props("dense")
                _select("instrument_type", vocabulary.INSTRUMENT_TYPE_OPTIONS,
                        instr, "instrument_type", with_input=True).props("dense")
                _select("expiry_rule", vocabulary.expiry_rules(),
                        instr, "expiry_rule", with_input=True).props("dense")
        _timeframes_editor(instr)


def _timeframes_editor(instr: CommentedMap) -> None:
    tfs = instr.setdefault("timeframes", CommentedMap())
    for tf_type in list(tfs.keys()):
        with ui.row().classes("items-center gap-2"):
            ui.input(label=f"{tf_type} (minutes, comma-sep)",
                     value=",".join(str(_tf_minutes(x)) for x in tfs[tf_type]),
                     on_change=lambda e, t=tf_type: _set_timeframes(tfs, t, e.value)) \
                .classes("min-w-[220px]").props("dense")
        ui.label("Developing: read the partially formed candle at t rather than "
                 "the last completed one.").classes(MUTED)
        with ui.row().classes("items-center gap-4 flex-wrap"):
            for idx, entry in enumerate(tfs[tf_type]):
                ui.switch(f"{_tf_minutes(entry)}min",
                          value=isinstance(entry, dict) and bool(entry.get("developing")),
                          on_change=lambda e, t=tf_type, i=idx: _set_developing(tfs, t, i, e.value))


def _tf_minutes(entry) -> int:
    return int(entry["minutes"]) if isinstance(entry, dict) else int(entry)


def _set_timeframes(tfs: CommentedMap, tf_type: str, raw: str) -> None:
    """
    Rewrite the minute list, keeping the developing flag of surviving entries.
    Edits the sequence in place so a YAML anchor shared by several instruments
    keeps pointing at it, matching how the developing switches behave.
    """
    seq = tfs[tf_type]
    flagged = {_tf_minutes(e) for e in seq if isinstance(e, dict) and e.get("developing")}
    seq[:] = [_new_timeframe(m) if m in flagged else m for m in _parse_int_list(raw)]


def _set_developing(tfs: CommentedMap, tf_type: str, index: int, on: bool) -> None:
    minutes = _tf_minutes(tfs[tf_type][index])
    tfs[tf_type][index] = _new_timeframe(minutes) if on else minutes


def _new_timeframe(minutes: int) -> CommentedMap:
    return _new_map([("minutes", minutes), ("developing", True)])


def _parse_int_list(s: str) -> list[int]:
    out = []
    for tok in str(s).split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(int(tok))
            except ValueError:
                pass
    return out


def _set_instrument_kind(instr: CommentedMap, kind: str) -> None:
    if kind == "spot":
        for k in ("name", "instrument_type", "expiry_rule"):
            instr.pop(k, None)
        instr.setdefault("trading_symbol", "")
    else:
        instr.pop("trading_symbol", None)
        instr.setdefault("name", "")
        instr.setdefault("instrument_type", "FUT")
        instr.setdefault("expiry_rule", "near_month")
    _general_tab.refresh()


def _add_instrument(exchange: CommentedMap) -> None:
    instruments = exchange.setdefault("instruments", CommentedSeq())
    instruments.append(_new_map([
        ("id", "new_instrument"),
        ("trading_symbol", ""),
        ("timeframes", _new_map([("intraday", CommentedSeq([1, 5, 15, 60]))])),
    ]))
    _general_tab.refresh()


def _remove_instrument(exchange: CommentedMap, idx: int) -> None:
    del exchange["instruments"][idx]
    _general_tab.refresh()


# ---------------------------------------------------------------------------
# Strategy · Indicators
# ---------------------------------------------------------------------------


@ui.refreshable
def _indicators_tab() -> None:
    ta = DOCS["strategy"].setdefault("ta", CommentedSeq())
    with ui.card().classes("w-full"):
        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Indicators").classes("font-medium")
            ui.button(icon="add", on_click=lambda: _add_indicator(ta)) \
                .props("flat dense").tooltip("Add indicator")
        for idx, ind in enumerate(ta):
            _indicator_editor(ta, idx, ind)


def _indicator_editor(ta: CommentedSeq, idx: int, ind: CommentedMap) -> None:
    with ui.card().classes("w-full bg-gray-50"):
        with ui.row().classes("items-center gap-2 w-full"):
            _select("kind", vocabulary.INDICATOR_KIND_OPTIONS, ind, "kind",
                    with_input=True).props("dense")
            alias = ind.get("alias")
            ui.input(label="alias(es), comma-sep",
                     value=", ".join(alias) if isinstance(alias, list) else str(alias),
                     on_change=lambda e, i=ind: i.__setitem__("alias", _parse_alias(e.value))) \
                .classes("min-w-[220px]").props("dense")
            ui.space()
            ui.button(icon="delete", on_click=lambda i=idx: _remove_indicator(ta, i)) \
                .props("flat dense color=negative")
        params = [k for k in ind.keys() if k not in ("kind", "alias")]
        if params:
            with ui.row().classes("items-center gap-2"):
                for k in params:
                    ui.input(label=k, value=str(ind[k]),
                             on_change=lambda e, kk=k, i=ind: i.__setitem__(kk, _coerce_scalar(e.value))) \
                        .classes("min-w-[120px]").props("dense")


def _parse_alias(s: str):
    parts = [p.strip() for p in str(s).split(",") if p.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return parts


def _coerce_scalar(s: str):
    s = str(s).strip()
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def _add_indicator(ta: CommentedSeq) -> None:
    ta.append(_new_map([("kind", "ema"), ("length", 20), ("alias", "new_alias")]))
    _indicators_tab.refresh()


def _remove_indicator(ta: CommentedSeq, idx: int) -> None:
    del ta[idx]
    _indicators_tab.refresh()


# ---------------------------------------------------------------------------
# Strategy · Conditions (recursive editor)
# ---------------------------------------------------------------------------

# The editor is driven entirely by each condition type's self-describing arg
# specs (from condition.CONDITION_REGISTRY, via vocabulary). Adding a new
# condition type in the engine makes it appear here with no changes below.
_ARG_SPECS_CACHE: dict[str, list[dict]] = {}


def _arg_specs() -> dict[str, list[dict]]:
    if not _ARG_SPECS_CACHE:
        _ARG_SPECS_CACHE.update(vocabulary.condition_arg_specs())
    return _ARG_SPECS_CACHE


def _specs_for(cond_type: str | None) -> list[dict]:
    return _arg_specs().get(cond_type, [])


def _is_combinator(cond_type: str | None) -> bool:
    return any(a["kind"] == "children" for a in _specs_for(cond_type))


def _accepts_more_children(cond_type: str | None, count: int) -> bool:
    for a in _specs_for(cond_type):
        if a["kind"] == "children":
            return a["max"] is None or count < a["max"]
    return False


def _type_signature(cond_type: str | None):
    """Identity of a type's arg shape; equal signatures can share existing args."""
    specs = _specs_for(cond_type)
    if any(a["kind"] == "children" for a in specs):
        return ("__children__",)
    return tuple((a["name"], a["kind"]) for a in specs)


def _new_id() -> str:
    n = STATE.get("id_counter", 0) + 1
    STATE["id_counter"] = n
    return f"cond_{n}"


# --- default node/operand/arg builders -------------------------------------


def _default_value_operand() -> CommentedMap:
    return _new_map([("type", "value"), ("value", 0.0)])


def _default_reference_operand() -> CommentedMap:
    s = DOCS["strategy"]
    ids = vocabulary.instrument_ids(s)
    tts = vocabulary.timeframe_types(s)
    tfs = vocabulary.all_timeframes(s)
    cols = vocabulary.alias_names(s)
    return _new_map([
        ("type", "reference"),
        ("instrument_id", ids[0] if ids else ""),
        ("timeframe_type", tts[0] if tts else "intraday"),
        ("timeframe", tfs[0] if tfs else "1min"),
        ("col_name", cols[0] if cols else ""),
    ])


def _default_condition_operand() -> CommentedMap:
    return _new_map([("type", "condition"), ("input", _default_leaf_condition())])


def _default_args_for_type(cond_type: str) -> Any:
    """Build a fresh ``args`` structure matching a condition type's arg specs."""
    specs = _specs_for(cond_type)
    if any(a["kind"] == "children" for a in specs):
        return CommentedSeq()
    m = CommentedMap()
    for a in specs:
        if a["kind"] in ("operand", "reference"):
            # Default operands to references (a value default of 0 would risk a
            # divide-by-zero normalizer); the user can switch to a literal value.
            m[a["name"]] = _default_reference_operand()
        elif a["kind"] == "int":
            m[a["name"]] = 1
        elif a["kind"] == "float":
            m[a["name"]] = 1.0
        elif a["kind"] == "bool":
            m[a["name"]] = True
        elif a["kind"] == "choice":
            options = a.get("options") or []
            m[a["name"]] = options[0] if options else ""
        elif a["kind"] == "definition_id":
            ids = vocabulary.definition_ids(DOCS["strategy"])
            m[a["name"]] = ids[0] if ids else ""
        elif a["kind"] == "condition":
            m[a["name"]] = _default_leaf_condition()
    return m


def _default_leaf_condition() -> CommentedMap:
    return _new_map([
        ("condition", "above"),
        ("id", _new_id()),
        ("args", _default_args_for_type("above")),
    ])


# --- structural mutations (each refreshes the tab) -------------------------


def _reshape_condition(node: CommentedMap, new_type: str) -> None:
    """Change a node's condition type, resetting args only if the shape changed."""
    if _type_signature(node.get("condition")) != _type_signature(new_type):
        node["args"] = _default_args_for_type(new_type)
    node["condition"] = new_type
    _refresh_editors()


def _set_operand_type(parent: CommentedMap, key: str, new_type: str) -> None:
    if new_type == "value":
        parent[key] = _default_value_operand()
    elif new_type == "condition":
        parent[key] = _default_condition_operand()
    else:
        parent[key] = _default_reference_operand()
    _refresh_editors()


def _set_lookback(operand: CommentedMap, value: Any) -> None:
    """Reference lookback: keep the key only when non-zero (files stay clean)."""
    n = int(value) if value not in (None, "") else 0
    if n:
        operand["lookback"] = n
    else:
        operand.pop("lookback", None)


def _add_child(node: CommentedMap) -> None:
    node.setdefault("args", CommentedSeq()).append(_default_leaf_condition())
    _refresh_editors()


def _add_top_condition(conditions: CommentedSeq) -> None:
    conditions.append(_default_leaf_condition())
    _refresh_editors()


def _remove_condition(parent_list: CommentedSeq, index: int) -> None:
    del parent_list[index]
    _refresh_editors()


# --- clipboard: copy / cut / paste any condition subtree -------------------


def _run_condition(node: CommentedMap) -> None:
    """Backtest a single block using the editor's current (unsaved) strategy."""
    node_id = node.get("id")
    try:
        persistence.validate_strategy(DOCS["strategy"])
    except ValidationError as error:
        first = str(error).splitlines()[1] if "\n" in str(error) else str(error)
        ui.notify(f"Fix the strategy before running: {first}", type="negative", timeout=6000)
        return
    operations.run_single_condition(DOCS["strategy"], node_id)
    ui.notify(f"Backtesting {node_id} — see Run · Backtest for progress")


def _show_description(node: CommentedMap) -> None:
    text = description.describe_block(node, DOCS["strategy"])
    with ui.dialog() as dialog, ui.card().classes("w-[950px] max-w-[95vw]"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label(f"Description — {description.readable_id(node.get('id'))}") \
                .classes("font-medium")
            ui.space()
            ui.button(icon="close", on_click=dialog.close).props("flat dense")
        ui.label("References are named, not expanded; export the strategy to read them.") \
            .classes(MUTED)
        with ui.element("div").classes("w-full overflow-auto").style("max-height:70vh"):
            ui.markdown(text).classes("w-full")
    dialog.open()


def _export_description() -> None:
    """Write the whole strategy out as Markdown, definitions first."""
    path = app_paths.output_dir() / "strategy_description.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(description.describe_strategy(DOCS["strategy"]), encoding="utf-8")
    except OSError as error:
        ui.notify(f"Could not write {path}: {error}", type="negative", timeout=8000)
        return
    ui.notify(f"Exported to {path}")


def _reverse_condition(node: CommentedMap) -> None:
    """Flip a block's directional meaning in place (Up <-> Down trade)."""
    definitions = DOCS["strategy"].setdefault("definitions", CommentedSeq())
    old_id = node.get("id")
    try:
        condition.reverse_spec(node, definitions)
    except (condition.UnsupportedReversalError, condition.DefinitionNotFoundError,
            condition.IncorrectReferenceError) as error:
        ui.notify(f"Could not reverse {old_id}: {error}", type="negative", timeout=8000)
        return
    ui.notify(f"Reversed {old_id} -> {node['id']}")
    _refresh_editors()


def _copy_condition(node: CommentedMap) -> None:
    STATE["clipboard"] = copy.deepcopy(node)
    ui.notify("Condition copied")
    _refresh_editors()


def _cut_condition(node: CommentedMap, on_remove) -> None:
    STATE["clipboard"] = copy.deepcopy(node)
    on_remove()  # removes the source from its slot and refreshes


def _paste_append(lst: CommentedSeq) -> None:
    node = copy.deepcopy(STATE["clipboard"])
    node["id"] = _new_id()  # avoid duplicate top-level ids (they name score columns)
    lst.append(node)
    _refresh_editors()


def _paste_replace(args: CommentedMap, key: str) -> None:
    node = copy.deepcopy(STATE["clipboard"])
    node["id"] = _new_id()
    args[key] = node
    _refresh_editors()


def _paste_button(on_paste, tooltip: str):
    btn = ui.button(icon="content_paste", on_click=lambda cb=on_paste: cb()) \
        .props("flat dense").tooltip(tooltip)
    if STATE.get("clipboard") is None:
        btn.props("disable")
    return btn


# Per-node collapse state, keyed by object identity so it survives tab refreshes
# and each node (parent or child) toggles independently.
def _toggle_collapsed(node: CommentedMap) -> None:
    coll = STATE.setdefault("collapsed", set())
    key = id(node)
    coll.discard(key) if key in coll else coll.add(key)
    _refresh_editors()


def _walk_condition_nodes(node: Any):
    """Yield every condition node in a raw document, at any nesting depth."""
    if isinstance(node, dict):
        if "condition" in node and "id" in node:
            yield node
        for value in node.values():
            yield from _walk_condition_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_condition_nodes(item)


def _collapse_all() -> None:
    strategy = DOCS.get("strategy") or {}
    STATE["collapsed"] = {
        id(node)
        for key in ("definitions", "conditions")
        for root in (strategy.get(key) or [])
        for node in _walk_condition_nodes(root)
    }


# Render simple scalar fields (numbers) first, larger nested fields last.
_KIND_ORDER = {"int": 0, "float": 0, "bool": 0, "choice": 0, "definition_id": 0,
               "operand": 1, "reference": 1, "condition": 2}


# Definitions and Conditions share the same editor helpers, so a structural
# change in either must re-render both tabs.
def _refresh_editors() -> None:
    _definitions_tab.refresh()
    _conditions_tab.refresh()


# --- operand + condition editors -------------------------------------------


def _operand_editor(parent: CommentedMap, key: str, label: str, depth: int = 0, *,
                    force_reference=False) -> None:
    op = parent.get(key)
    if not isinstance(op, dict):
        op = _default_reference_operand() if force_reference else _default_value_operand()
        parent[key] = op

    s = DOCS["strategy"]
    with ui.card().classes("w-full bg-gray-50"):
        with ui.row().classes("items-center gap-2"):
            ui.label(label).classes("font-medium")
            if force_reference:
                ui.badge("reference").props("color=teal")
            else:
                ui.select(["value", "reference", "condition"], value=op.get("type"), label="type",
                          on_change=lambda e, p=parent, k=key: _set_operand_type(p, k, e.value)) \
                    .props("dense").classes("min-w-[130px]")

        if op.get("type") == "value":
            ui.number(label="value", value=op.get("value"),
                      on_change=lambda e, o=op: o.__setitem__(
                          "value", float(e.value) if e.value is not None else None)) \
                .props("dense").classes("min-w-[140px]")
        elif op.get("type") == "condition":
            _nested_condition_editor(op, "input", depth)
        else:
            with ui.row().classes("items-center gap-2 flex-wrap"):
                _select("instrument_id", vocabulary.instrument_ids(s), op, "instrument_id").props("dense")
                _select("timeframe_type", vocabulary.timeframe_types(s), op, "timeframe_type",
                        with_input=True).props("dense")
                _select("timeframe", vocabulary.all_timeframes(s), op, "timeframe").props("dense")
                _select("col_name", vocabulary.alias_names(s), op, "col_name",
                        with_input=True).props("dense")
                ui.number(label="lookback", value=op.get("lookback", 0), precision=0, format="%d",
                          on_change=lambda e, o=op: _set_lookback(o, e.value)) \
                    .props("dense").classes("min-w-[110px]")


def _int_field(args: CommentedMap, name: str) -> None:
    ui.number(label=name, value=args.get(name, 1), precision=0, format="%d",
              on_change=lambda e, a=args, n=name: a.__setitem__(
                  n, int(e.value) if e.value is not None else None)) \
        .props("dense").classes("min-w-[110px]")


def _float_field(args: CommentedMap, name: str) -> None:
    ui.number(label=name, value=args.get(name, 1.0),
              on_change=lambda e, a=args, n=name: a.__setitem__(
                  n, float(e.value) if e.value is not None else None)) \
        .props("dense").classes("min-w-[110px]")


def _bool_field(args: CommentedMap, name: str) -> None:
    ui.switch(name, value=bool(args.get(name, True)),
              on_change=lambda e, a=args, n=name: a.__setitem__(n, bool(e.value)))


def _choice_field(args: CommentedMap, name: str, options) -> None:
    _select(name, options or [], args, name).props("dense")


def _definition_id_field(args: CommentedMap, name: str) -> None:
    _select(name, vocabulary.definition_ids(DOCS["strategy"]), args, name).props("dense")


# --- read-only preview of a ref's target -----------------------------------

# Greyed and inert: a ref shows what it points at, but the definition is only
# editable where it lives, under Strategy · Definitions.
PREVIEW_CLASSES = "opacity-60 pointer-events-none select-none"
PREVIEW_MAX_DEPTH = 12


def _find_definition(target) -> CommentedMap | None:
    for definition in DOCS["strategy"].get("definitions") or []:
        if definition.get("id") == target:
            return definition
    return None


def _operand_summary(op) -> str:
    """One-line form of an operand, for the preview only."""
    if not isinstance(op, dict):
        return str(op)
    if op.get("type") == "value":
        return str(op.get("value"))
    if op.get("type") == "reference":
        # A candle reference carries no col_name; the condition picks the columns.
        parts = [op.get("instrument_id"), op.get("timeframe"), op.get("col_name")]
        text = "/".join(str(part) for part in parts if part)
        lookback = op.get("lookback") or 0
        return f"{text} (t-{lookback})" if lookback else text
    return ""  # condition operands render as a nested node instead


def _node_detail(node) -> str:
    """Scalar args and operand summaries, joined for a single preview line."""
    cond_type = node.get("condition")
    args = node.get("args") or {}
    if _is_combinator(cond_type) or not isinstance(args, dict):
        return ""
    parts = []
    for a in _specs_for(cond_type):
        if a["kind"] in ("int", "float", "bool", "choice", "definition_id"):
            if a["name"] in args:
                parts.append(f"{a['name']}={args[a['name']]}")
        elif a["kind"] in ("operand", "reference"):
            summary = _operand_summary(args.get(a["name"]))
            if summary:
                parts.append(f"{a['name']}: {summary}")
    return " · ".join(parts)


def _preview_rows(node, depth: int = 0, seen: frozenset = frozenset()) -> list[dict]:
    """
    Flatten a definition into rows for the read-only preview, following any refs
    it owns. Kept free of UI calls so the traversal can be tested directly.
    """
    if not isinstance(node, dict) or depth > PREVIEW_MAX_DEPTH:
        return []
    cond_type = node.get("condition")
    rows = [{"depth": depth, "type": str(cond_type), "id": str(node.get("id", "")),
             "detail": _node_detail(node), "note": None}]
    args = node.get("args")

    if cond_type == "ref":
        rows += _target_rows((args or {}).get("target"), depth + 1, seen)
    elif _is_combinator(cond_type):
        for child in args or []:
            rows += _preview_rows(child, depth + 1, seen)
    elif isinstance(args, dict):
        for a in _specs_for(cond_type):
            value = args.get(a["name"])
            if a["kind"] == "condition":
                rows += _preview_rows(value, depth + 1, seen)
            elif a["kind"] in ("operand", "reference") \
                    and isinstance(value, dict) and value.get("type") == "condition":
                rows += _preview_rows(value.get("input"), depth + 1, seen)
    return rows


def _target_rows(target, depth: int, seen: frozenset) -> list[dict]:
    if target in seen:
        return [{"depth": depth, "type": "ref", "id": str(target),
                 "detail": "", "note": "cycle"}]
    definition = _find_definition(target)
    if definition is None:
        return [{"depth": depth, "type": "ref", "id": str(target),
                 "detail": "", "note": "missing"}]
    return _preview_rows(definition, depth, seen | {target})


def _ref_preview(node: CommentedMap) -> None:
    target = (node.get("args") or {}).get("target")
    if not target:
        return
    with ui.card().classes(f"w-full bg-gray-100 {PREVIEW_CLASSES}"):
        ui.label(f"preview of '{target}' — edit under Strategy · Definitions") \
            .classes("text-xs text-gray-500")
        for row in _target_rows(target, 0, frozenset()):
            with ui.row().classes("items-baseline gap-2 w-full") \
                    .style(f"margin-left:{row['depth'] * 14}px"):
                if row["note"] == "cycle":
                    ui.label(f"↺ {row['id']} (shown above)").classes("text-xs text-warning")
                    continue
                if row["note"] == "missing":
                    ui.label(f"⚠ {row['id']} is not in definitions").classes("text-xs text-negative")
                    continue
                ui.badge(row["type"]).props("color=grey")
                ui.label(row["id"]).classes("text-sm")
                if row["detail"]:
                    ui.label(row["detail"]).classes("text-xs text-gray-500")


# --- structure overlay ------------------------------------------------------


def _node_key(path: tuple) -> str:
    """Unique per position in the tree, since one definition can appear twice."""
    return "/".join(str(part) for part in path)


def _structure_nodes(node, seen: frozenset = frozenset(), path: tuple = ("root",)) -> list[dict]:
    """
    ``ui.tree`` nodes for a condition, with every ref replaced by what it points
    at, so the result reads as the condition's semantics rather than its wiring.
    """
    if not isinstance(node, dict) or len(path) > PREVIEW_MAX_DEPTH:
        return []
    cond_type = node.get("condition")

    if cond_type == "ref":
        target = (node.get("args") or {}).get("target")
        if target in seen:
            return [{"id": _node_key(path), "label": f"↺ {target} (shown above)"}]
        definition = _find_definition(target)
        if definition is None:
            return [{"id": _node_key(path), "label": f"⚠ {target} is not in definitions"}]
        # Take the ref's place rather than nesting under it.
        return _structure_nodes(definition, seen | {target}, path)

    label = f"{cond_type}   ·   {node.get('id', '')}"
    detail = _node_detail(node)
    if detail:
        label += f"   ·   {detail}"

    children: list[dict] = []
    args = node.get("args")
    if _is_combinator(cond_type):
        for index, child in enumerate(args or []):
            children += _structure_nodes(child, seen, path + (index,))
    elif isinstance(args, dict):
        for spec in _specs_for(cond_type):
            value = args.get(spec["name"])
            if spec["kind"] == "condition":
                children += _structure_nodes(value, seen, path + (spec["name"],))
            elif spec["kind"] in ("operand", "reference") \
                    and isinstance(value, dict) and value.get("type") == "condition":
                children += _structure_nodes(value.get("input"), seen, path + (spec["name"],))

    entry = {"id": _node_key(path), "label": label}
    if children:
        entry["children"] = children
    return [entry]


def _show_structure(node: CommentedMap) -> None:
    nodes = _structure_nodes(node)
    with ui.dialog() as dialog, ui.card().classes("w-[950px] max-w-[95vw]"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.label(f"Structure — {node.get('id', '')}").classes("font-medium")
            ui.space()
            ui.button(icon="unfold_more", on_click=lambda: tree.expand()) \
                .props("flat dense").tooltip("Expand all")
            ui.button(icon="unfold_less", on_click=lambda: tree.collapse()) \
                .props("flat dense").tooltip("Collapse all")
            ui.button(icon="close", on_click=dialog.close).props("flat dense")
        ui.label("Read-only. References are replaced by the definition they point at.") \
            .classes(MUTED)
        with ui.scroll_area().classes("w-full").style("height:70vh"):
            tree = ui.tree(nodes, label_key="label").props("dense no-connectors")
            tree.expand()
    dialog.open()


def _nested_condition_editor(args: CommentedMap, key: str, depth: int) -> None:
    child = args.get(key)
    if not isinstance(child, dict):
        child = _default_leaf_condition()
        args[key] = child
    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("items-center gap-2"):
            ui.label(key).classes(MUTED)
            _paste_button(lambda a=args, k=key: _paste_replace(a, k), "Paste as input")
        _condition_editor(child, depth + 1)  # required slot -> no remove button


def _move_in_list(lst: CommentedSeq, old_index: int, new_index: int) -> None:
    if old_index == new_index or not (0 <= old_index < len(lst)):
        return
    lst.insert(new_index, lst.pop(old_index))
    _refresh_editors()


# Handles are scoped by item depth so a nested list's handles never match its
# parent container's selector, which would make both sortables fire at once.
def _handle_class(depth: int) -> str:
    return f"drag-handle-d{depth}"


def _sortable_column(lst: CommentedSeq, depth: int):
    container = ui.column().classes("w-full gap-2")
    container.make_sortable(
        handle=f".{_handle_class(depth)}",
        on_end=lambda e, target=lst: _move_in_list(target, e.old_index, e.new_index),
    )
    return container


def _set_enabled(node: CommentedMap, value: bool) -> None:
    if value:
        node.pop("enabled", None)
    else:
        node["enabled"] = False


def _set_all_enabled(nodes: CommentedSeq, value: bool) -> None:
    for node in nodes:
        _set_enabled(node, value)
    _refresh_editors()


# Toggles between ascending/descending on each press, independently for the
# Definitions and Conditions tabs.
_SORT_ASCENDING = {"definitions": True, "conditions": True}


def _sort_top_level(lst: CommentedSeq, key: str) -> None:
    ascending = _SORT_ASCENDING[key]
    lst.sort(key=lambda item: str(item.get("id") or "").lower(), reverse=not ascending)
    _SORT_ASCENDING[key] = not ascending


def _definition_usage() -> dict[str, set]:
    """Definition id -> ids of the top-level conditions that use it, whether
    directly or through another definition's own reference."""
    definitions = DOCS["strategy"].get("definitions") or []
    conditions = DOCS["strategy"].get("conditions") or []
    by_id = {d.get("id"): d for d in definitions}

    def walk(spec, seen_specs):
        if id(spec) in seen_specs:
            return
        seen_specs.add(id(spec))
        if spec.get("condition") == "ref":
            target = (spec.get("args") or {}).get("target")
            if target is not None:
                yield target
                definition = by_id.get(target)
                if definition is not None:
                    yield from walk(definition, seen_specs)
        for child in condition._condition_children(spec):
            yield from walk(child, seen_specs)

    usage: dict[str, set] = {}
    for top in conditions:
        for target in walk(top, set()):
            usage.setdefault(target, set()).add(top.get("id"))
    return usage


def _notify_usage(node_id: str, condition_ids: set) -> None:
    if not condition_ids:
        ui.notify(f"{node_id} is not used by any condition", type="warning")
        return
    ui.notify(f"{node_id} is used in: {', '.join(sorted(condition_ids))}", timeout=8000)


def _condition_editor(node: CommentedMap, depth: int, on_remove=None, show_enabled=False,
                      draggable=False, usage_condition_ids: set = None) -> None:
    cond_type = node.get("condition")
    is_combinator = _is_combinator(cond_type)
    type_opts = _with_current(vocabulary.condition_types(), cond_type)
    collapsed = id(node) in STATE.setdefault("collapsed", set())

    with ui.card().classes("w-full").style(f"margin-left:{depth * 16}px"):
        with ui.row().classes("items-center gap-2 w-full"):
            if draggable:
                ui.icon("drag_indicator") \
                    .classes(f"{_handle_class(depth)} cursor-move text-gray-400") \
                    .tooltip("Drag to reorder")
            ui.button(icon="chevron_right" if collapsed else "expand_more",
                      on_click=lambda n=node: _toggle_collapsed(n)) \
                .props("flat dense").tooltip("Expand" if collapsed else "Collapse")
            if show_enabled:
                ui.switch(value=node.get("enabled", True),
                          on_change=lambda e, n=node: _set_enabled(n, e.value)) \
                    .props("dense").tooltip("Evaluate this condition in backtests and live runs")
            ui.select(type_opts, value=cond_type, label="type",
                      on_change=lambda e, n=node: _reshape_condition(n, e.value)) \
                .props("dense").classes("min-w-[150px]")
            _text("id", node, "id").props("dense")
            if usage_condition_ids is not None:
                count = len(usage_condition_ids)
                ui.label(f"Used in {count} condition{'' if count == 1 else 's'}") \
                    .classes("text-xs text-gray-400 cursor-pointer") \
                    .on("click", lambda n=node, u=usage_condition_ids: _notify_usage(n.get("id"), u))
            ui.space()
            if is_combinator and _accepts_more_children(cond_type, len(node.get("args") or [])):
                ui.button(icon="add", on_click=lambda n=node: _add_child(n)) \
                    .props("flat dense").tooltip("Add child")
                _paste_button(lambda n=node: _paste_append(n.setdefault("args", CommentedSeq())),
                              "Paste as child")
            ui.button(icon="account_tree", on_click=lambda n=node: _show_structure(n)) \
                .props("flat dense").tooltip("View this block's full structure")
            ui.button(icon="notes", on_click=lambda n=node: _show_description(n)) \
                .props("flat dense").tooltip("Describe this block in plain English")
            ui.button(icon="swap_vert", on_click=lambda n=node: _reverse_condition(n)) \
                .props("flat dense").tooltip("Reverse: flip this block's Up/Down trade logic in place")
            ui.button(icon="play_arrow", on_click=lambda n=node: _run_condition(n)) \
                .props("flat dense").tooltip("Backtest this block on its own")
            ui.button(icon="content_copy", on_click=lambda n=node: _copy_condition(n)) \
                .props("flat dense").tooltip("Copy")
            if on_remove is not None:
                ui.button(icon="content_cut",
                          on_click=lambda n=node, cb=on_remove: _cut_condition(n, cb)) \
                    .props("flat dense").tooltip("Cut")
                ui.button(icon="delete", on_click=lambda cb=on_remove: cb()) \
                    .props("flat dense color=negative")

        if cond_type in description.ATOMIC_TYPES:
            formula = description.Describer(DOCS["strategy"]).quick_formula(node)
            if formula:
                ui.label(formula).classes("text-xl font-semibold")

        if collapsed:
            return

        if is_combinator:
            children = node.setdefault("args", CommentedSeq())
            if len(children) == 0:
                ui.label("(no children yet — add at least one)").classes(MUTED)
            with _sortable_column(children, depth + 1):
                for c_idx, child in enumerate(children):
                    _condition_editor(child, depth + 1, draggable=True,
                                      on_remove=lambda cl=children, ci=c_idx: _remove_condition(cl, ci))
        else:
            args = node.setdefault("args", CommentedMap())
            for a in sorted(_specs_for(cond_type), key=lambda a: _KIND_ORDER.get(a["kind"], 1)):
                if a["kind"] in ("operand", "reference"):
                    _operand_editor(args, a["name"], a["name"], depth,
                                    force_reference=(a["kind"] == "reference"))
                elif a["kind"] == "int":
                    _int_field(args, a["name"])
                elif a["kind"] == "float":
                    _float_field(args, a["name"])
                elif a["kind"] == "bool":
                    _bool_field(args, a["name"])
                elif a["kind"] == "choice":
                    _choice_field(args, a["name"], a.get("options"))
                elif a["kind"] == "definition_id":
                    _definition_id_field(args, a["name"])
                elif a["kind"] == "condition":
                    _nested_condition_editor(args, a["name"], depth)

            if cond_type == "ref":
                _ref_preview(node)


def _sort_and_refresh(lst: CommentedSeq, key: str, tab) -> None:
    _sort_top_level(lst, key)
    tab.refresh()


@ui.refreshable
def _definitions_tab() -> None:
    definitions = DOCS["strategy"].setdefault("definitions", CommentedSeq())
    with ui.row().classes("items-center gap-2"):
        ui.label("Definitions").classes("font-medium")
        ui.button(icon="add", on_click=lambda: _add_top_condition(definitions)) \
            .props("flat dense").tooltip("Add definition")
        _paste_button(lambda: _paste_append(definitions), "Paste definition")
        ui.button(icon="sort_by_alpha",
                  on_click=lambda: _sort_and_refresh(definitions, "definitions", _definitions_tab)) \
            .props("flat dense").tooltip("Sort by id (toggles ascending/descending)")
    ui.label("Shared conditions, referenced from elsewhere via the 'ref' type.").classes(MUTED)
    if len(definitions) == 0:
        ui.label("No definitions yet.").classes(MUTED)
    usage = _definition_usage()
    with _sortable_column(definitions, 0):
        for idx, definition in enumerate(definitions):
            _condition_editor(definition, depth=0, draggable=True,
                              usage_condition_ids=usage.get(definition.get("id"), set()),
                              on_remove=lambda dl=definitions, i=idx: _remove_condition(dl, i))


@ui.refreshable
def _conditions_tab() -> None:
    conditions = DOCS["strategy"].setdefault("conditions", CommentedSeq())
    with ui.row().classes("items-center gap-2"):
        ui.label("Conditions").classes("font-medium")
        ui.button(icon="add", on_click=lambda: _add_top_condition(conditions)) \
            .props("flat dense").tooltip("Add top-level condition")
        _paste_button(lambda: _paste_append(conditions), "Paste condition")
        ui.button(icon="sort_by_alpha",
                  on_click=lambda: _sort_and_refresh(conditions, "conditions", _conditions_tab)) \
            .props("flat dense").tooltip("Sort by id (toggles ascending/descending)")
        ui.button("Enable all", on_click=lambda: _set_all_enabled(conditions, True)) \
            .props("flat dense")
        ui.button("Disable all", on_click=lambda: _set_all_enabled(conditions, False)) \
            .props("flat dense")
    if len(conditions) == 0:
        ui.label("No conditions defined.").classes(MUTED)
    with _sortable_column(conditions, 0):
        for idx, cond in enumerate(conditions):
            _condition_editor(cond, depth=0, show_enabled=True, draggable=True,
                              on_remove=lambda cl=conditions, i=idx: _remove_condition(cl, i))


# ---------------------------------------------------------------------------
# Save-on-switch controller + page
# ---------------------------------------------------------------------------

STATE = {"current": None, "reverting": False, "clipboard": None, "collapsed": set()}
_TAB_BUILDERS: dict[str, Any] = {
    "Settings · Display": _display_tab,
    "Strategy · General": _general_tab,
    "Strategy · Indicators": _indicators_tab,
    "Strategy · Definitions": _definitions_tab,
    "Strategy · Conditions": _conditions_tab,
    "Run · Backtest": operations.backtest_tab,
    "Run · Live": operations.live_tab,
    "Run · Charts": operations.charts_tab,
}


def _commit(tab_label: str) -> bool:
    """Validate and save the document backing ``tab_label``. Returns success."""
    doc_key = TAB_DOC.get(tab_label)
    if doc_key is None:
        return True  # Run tabs edit no document

    doc = DOCS[doc_key]
    try:
        VALIDATORS[doc_key](doc)
    except ValidationError as e:
        first = str(e).splitlines()[1] if "\n" in str(e) else str(e)
        ui.notify(f"{doc_key} invalid — not saved: {first}", type="negative", timeout=6000)
        return False
    try:
        persistence.save_document(doc, PATHS[doc_key])
    except persistence.ExternalChangeError as e:
        ui.notify(f"{e} Use Reload above.", type="negative", timeout=0, close_button="OK")
        return False
    ui.notify(f"Saved {PATHS[doc_key].name}", type="positive")
    return True


def _refresh_all_tabs() -> None:
    _collapse_all()
    for builder in _TAB_BUILDERS.values():
        refresh = getattr(builder, "refresh", None)
        if refresh is not None:
            refresh()
    _banners.refresh()


def _reload_docs() -> None:
    """Discard the in-memory documents and read both files again."""
    _load_docs()
    _refresh_all_tabs()
    ui.notify("Reloaded from disk — unsaved edits discarded", type="warning")


def _export_document(doc_key: str) -> None:
    ui.download(PATHS[doc_key], filename=PATHS[doc_key].name)


async def _import_document(doc_key: str, event) -> None:
    """Replace strategy.yaml/settings.yaml with an uploaded file, provided it
    would still validate -- a bad import is rejected before anything on disk
    changes."""
    text = await event.file.text()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        VALIDATORS[doc_key](persistence.load_document(tmp_path))
    except Exception as error:
        ui.notify(f"Not imported — would make {PATHS[doc_key].name} invalid: {error}",
                  type="negative", timeout=8000)
        return
    finally:
        tmp_path.unlink(missing_ok=True)
    PATHS[doc_key].write_text(text, encoding="utf-8")
    _load_docs()
    _refresh_all_tabs()
    ui.notify(f"Imported {PATHS[doc_key].name}")


# QUploader has no prop for this -- its header always shows a byte/percent
# subtitle even at rest, which dwarfs a plain "Export" button beside it.
_IMPORT_EXPORT_CSS = """
.import-upload.q-uploader { min-height: 0 !important; }
.import-upload .q-uploader__header { min-height: 0 !important; padding: 4px 12px !important; }
.import-upload .q-uploader__subtitle { display: none !important; }
.import-upload .q-uploader__header-content .q-btn {
  min-height: 24px !important; min-width: 24px !important; padding: 0 !important;
}
.import-upload .q-uploader__header-content .q-btn .q-icon { font-size: 18px !important; }
.import-upload .q-uploader__title { font-size: 0.8rem !important; line-height: 1.5 !important; }
"""


def _show_import_export() -> None:
    with ui.dialog() as dialog, ui.card():
        ui.label("Import / export config files").classes("font-medium")
        ui.label("Export gives you the file as currently saved on disk. "
                  "Import replaces it immediately if the upload is valid.").classes(MUTED)
        for doc_key, label in (("strategy", "Strategy"), ("settings", "Settings")):
            with ui.row().classes("items-center gap-3 w-full no-wrap"):
                ui.label(label).classes("w-20 shrink-0")
                ui.button("Export", icon="download",
                          on_click=lambda k=doc_key: _export_document(k)) \
                    .props("outline dense no-caps").classes("shrink-0")
                ui.upload(on_upload=lambda e, k=doc_key: _import_document(k, e),
                         auto_upload=True, label="Import") \
                    .props("flat borderless dense no-caps hide-upload-btn accept=.yaml,.yml") \
                    .classes("shrink-0 import-upload").style("width:160px")
        with ui.row().classes("justify-end w-full"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


# label shown next to each field, in the order the menu displays them --
# User ID first and unmasked (it's just an account identifier), then the
# actual secrets, masked and blank until something is typed to replace them.
_ACCOUNT_FIELDS = (
    ("USER_ID", "User ID"),
    ("KITE_API_KEY", "API Key"),
    ("KITE_API_SECRET", "API Secret"),
    ("PASSWORD", "Password"),
    ("TOTP_SECRET", "TOTP Secret"),
)


def _save_account_fields(fields: dict) -> None:
    """Same semantics as `main.py credentials`: blank keeps the existing
    value, anything else replaces it in the keyring immediately."""
    changed = []
    for key, field in fields.items():
        value = field.value
        if not value:
            continue
        secrets_store.set_credential(key, value)
        changed.append(key)
        if key != "USER_ID":
            field.set_value("")
    ui.notify(f"Updated: {', '.join(changed)}" if changed else "No changes", type="positive")


def _account_menu() -> None:
    status = secrets_store.credential_status()
    fields: dict[str, Any] = {}
    with ui.column().classes("p-3 gap-2").style("min-width:260px"):
        ui.label("Kite account").classes("font-medium")
        user_key, user_label = _ACCOUNT_FIELDS[0]
        fields[user_key] = ui.input(
            label=user_label, value=secrets_store.get_credential(user_key) or "") \
            .props("dense")
        ui.separator()
        ui.label("Leave a field blank to keep its current value.").classes(MUTED)
        for key, label in _ACCOUNT_FIELDS[1:]:
            fields[key] = ui.input(
                label=f"{label} [{'set' if status[key] else 'MISSING'}]",
                password=True, password_toggle_button=True).props("dense")
        ui.button("Save to keyring", icon="save",
                  on_click=lambda: _save_account_fields(fields)).props("flat")


@ui.refreshable
def _banners() -> None:
    for label, key in (("strategy", "strategy"), ("settings", "settings")):
        try:
            VALIDATORS[key](DOCS[key])
            ui.badge(f"{label}: valid").props("color=positive")
        except ValidationError:
            ui.badge(f"{label}: invalid").props("color=negative")


@ui.page("/")
def index() -> None:
    ui.add_css(_IMPORT_EXPORT_CSS)
    _load_docs()
    _collapse_all()
    STATE["current"] = "Settings · Display"
    STATE["reverting"] = False

    def on_tab_change(e) -> None:
        if STATE["reverting"]:
            STATE["reverting"] = False
            STATE["current"] = e.value
            return
        leaving = STATE["current"]
        if leaving is not None and not _commit(leaving):
            STATE["reverting"] = True
            tabs.set_value(leaving)
            _banners.refresh()  # surface the now-invalid in-memory state
            return
        STATE["current"] = e.value
        _banners.refresh()
        builder = _TAB_BUILDERS.get(e.value)
        if builder is not None:
            builder.refresh()  # pick up vocabulary changes from other tabs

    with ui.header().classes("items-center justify-between"):
        ui.label("Trading Signals · Config Editor").classes("text-lg font-semibold")
        with ui.row().classes("gap-3 items-center"):
            _banners()
            ui.button("Save tab", icon="save",
                      on_click=lambda: (_commit(STATE["current"]), _banners.refresh())) \
                .props("flat color=white")
            ui.button("Reload", icon="refresh", on_click=_reload_docs) \
                .props("flat color=white") \
                .tooltip("Re-read both config files, discarding unsaved edits")
            ui.button("Export description", icon="description", on_click=_export_description) \
                .props("flat color=white") \
                .tooltip("Write the whole strategy to output/strategy_description.md")
            ui.button("Import / export", icon="import_export", on_click=_show_import_export) \
                .props("flat color=white") \
                .tooltip("Download or replace strategy.yaml / settings.yaml directly")
            account_btn = ui.button(icon="account_circle").props("flat color=white") \
                .tooltip("Update Kite credentials")
            with ui.menu().props("no-parent-event") as account_menu:
                _account_menu()
            account_btn.on("click", account_menu.open)

    with ui.tabs(on_change=on_tab_change).classes("w-full") as tabs:
        for label in TAB_DOC:
            ui.tab(label)

    with ui.tab_panels(tabs, value="Settings · Display").classes("w-full"):
        for label, builder in _TAB_BUILDERS.items():
            with ui.tab_panel(label):
                builder()

    ui.timer(1.0, operations.refresh_operations)


def main() -> None:
    operations.mount_charts()
    ui.run(title="Config Editor", port=8080, reload=False, show=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
