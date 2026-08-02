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
from typing import Any, Callable

from nicegui import ui
from pydantic import ValidationError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ui import persistence, vocabulary

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
    "Settings · Historical": "settings",
    "Strategy · General": "strategy",
    "Strategy · Indicators": "strategy",
    "Strategy · Conditions": "strategy",
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
# Settings · Historical
# ---------------------------------------------------------------------------


@ui.refreshable
def _historical_tab() -> None:
    historical = DOCS["settings"].setdefault("historical", CommentedMap())
    with ui.card().classes("w-full"):
        # The text input is the single writer to historical['from']. The date
        # picker reads it one-way and writes back through the input, so the two
        # controls never form a two-way binding cycle on the same key.
        inp = ui.input(label="from (YYYY-MM-DD)")
        inp.bind_value(historical, "from")
        with ui.menu().props("no-parent-event") as menu:
            ui.date(on_change=lambda e: inp.set_value(e.value)) \
                .bind_value_from(historical, "from")
            with ui.row().classes("justify-end"):
                ui.button("Close", on_click=menu.close).props("flat")
        with inp.add_slot("append"):
            ui.icon("edit_calendar").on("click", menu.open).classes("cursor-pointer")


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
                     value=",".join(str(x) for x in tfs[tf_type]),
                     on_change=lambda e, t=tf_type: tfs.__setitem__(t, _parse_int_list(e.value))) \
                .classes("min-w-[220px]").props("dense")


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
    _conditions_tab.refresh()


def _set_operand_type(parent: CommentedMap, key: str, new_type: str) -> None:
    parent[key] = (
        _default_value_operand() if new_type == "value" else _default_reference_operand()
    )
    _conditions_tab.refresh()


def _set_lookback(operand: CommentedMap, value: Any) -> None:
    """Reference lookback: keep the key only when non-zero (files stay clean)."""
    n = int(value) if value not in (None, "") else 0
    if n:
        operand["lookback"] = n
    else:
        operand.pop("lookback", None)


def _add_child(node: CommentedMap) -> None:
    node.setdefault("args", CommentedSeq()).append(_default_leaf_condition())
    _conditions_tab.refresh()


def _add_top_condition(conditions: CommentedSeq) -> None:
    conditions.append(_default_leaf_condition())
    _conditions_tab.refresh()


def _remove_condition(parent_list: CommentedSeq, index: int) -> None:
    del parent_list[index]
    _conditions_tab.refresh()


# --- clipboard: copy / cut / paste any condition subtree -------------------


def _copy_condition(node: CommentedMap) -> None:
    STATE["clipboard"] = copy.deepcopy(node)
    ui.notify("Condition copied")
    _conditions_tab.refresh()


def _cut_condition(node: CommentedMap, on_remove) -> None:
    STATE["clipboard"] = copy.deepcopy(node)
    on_remove()  # removes the source from its slot and refreshes


def _paste_append(lst: CommentedSeq) -> None:
    node = copy.deepcopy(STATE["clipboard"])
    node["id"] = _new_id()  # avoid duplicate top-level ids (they name score columns)
    lst.append(node)
    _conditions_tab.refresh()


def _paste_replace(args: CommentedMap, key: str) -> None:
    node = copy.deepcopy(STATE["clipboard"])
    node["id"] = _new_id()
    args[key] = node
    _conditions_tab.refresh()


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
    _conditions_tab.refresh()


# Render simple scalar fields (numbers) first, larger nested fields last.
_KIND_ORDER = {"int": 0, "float": 0, "operand": 1, "reference": 1, "condition": 2}


# --- operand + condition editors -------------------------------------------


def _operand_editor(parent: CommentedMap, key: str, label: str, *, force_reference=False) -> None:
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
                ui.select(["value", "reference"], value=op.get("type"), label="type",
                          on_change=lambda e, p=parent, k=key: _set_operand_type(p, k, e.value)) \
                    .props("dense").classes("min-w-[130px]")

        if op.get("type") == "value":
            ui.number(label="value", value=op.get("value"),
                      on_change=lambda e, o=op: o.__setitem__(
                          "value", float(e.value) if e.value is not None else None)) \
                .props("dense").classes("min-w-[140px]")
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


def _condition_editor(node: CommentedMap, depth: int, on_remove=None) -> None:
    cond_type = node.get("condition")
    is_combinator = _is_combinator(cond_type)
    type_opts = _with_current(vocabulary.condition_types(), cond_type)
    collapsed = id(node) in STATE.setdefault("collapsed", set())

    with ui.card().classes("w-full").style(f"margin-left:{depth * 16}px"):
        with ui.row().classes("items-center gap-2 w-full"):
            ui.button(icon="chevron_right" if collapsed else "expand_more",
                      on_click=lambda n=node: _toggle_collapsed(n)) \
                .props("flat dense").tooltip("Expand" if collapsed else "Collapse")
            ui.select(type_opts, value=cond_type, label="type",
                      on_change=lambda e, n=node: _reshape_condition(n, e.value)) \
                .props("dense").classes("min-w-[150px]")
            _text("id", node, "id").props("dense")
            ui.space()
            if is_combinator:
                ui.button(icon="add", on_click=lambda n=node: _add_child(n)) \
                    .props("flat dense").tooltip("Add child")
                _paste_button(lambda n=node: _paste_append(n.setdefault("args", CommentedSeq())),
                              "Paste as child")
            ui.button(icon="content_copy", on_click=lambda n=node: _copy_condition(n)) \
                .props("flat dense").tooltip("Copy")
            if on_remove is not None:
                ui.button(icon="content_cut",
                          on_click=lambda n=node, cb=on_remove: _cut_condition(n, cb)) \
                    .props("flat dense").tooltip("Cut")
                ui.button(icon="delete", on_click=lambda cb=on_remove: cb()) \
                    .props("flat dense color=negative")

        if collapsed:
            return

        if is_combinator:
            children = node.setdefault("args", CommentedSeq())
            if len(children) == 0:
                ui.label("(no children yet — add at least one)").classes(MUTED)
            for c_idx, child in enumerate(children):
                _condition_editor(child, depth + 1,
                                  on_remove=lambda cl=children, ci=c_idx: _remove_condition(cl, ci))
        else:
            args = node.setdefault("args", CommentedMap())
            for a in sorted(_specs_for(cond_type), key=lambda a: _KIND_ORDER.get(a["kind"], 1)):
                if a["kind"] in ("operand", "reference"):
                    _operand_editor(args, a["name"], a["name"],
                                    force_reference=(a["kind"] == "reference"))
                elif a["kind"] == "int":
                    _int_field(args, a["name"])
                elif a["kind"] == "float":
                    _float_field(args, a["name"])
                elif a["kind"] == "condition":
                    _nested_condition_editor(args, a["name"], depth)


@ui.refreshable
def _conditions_tab() -> None:
    conditions = DOCS["strategy"].setdefault("conditions", CommentedSeq())
    with ui.row().classes("items-center gap-2"):
        ui.label("Conditions").classes("font-medium")
        ui.button(icon="add", on_click=lambda: _add_top_condition(conditions)) \
            .props("flat dense").tooltip("Add top-level condition")
        _paste_button(lambda: _paste_append(conditions), "Paste condition")
    if len(conditions) == 0:
        ui.label("No conditions defined.").classes(MUTED)
    for idx, cond in enumerate(conditions):
        _condition_editor(cond, depth=0,
                          on_remove=lambda cl=conditions, i=idx: _remove_condition(cl, i))


# ---------------------------------------------------------------------------
# Save-on-switch controller + page
# ---------------------------------------------------------------------------

STATE = {"current": None, "reverting": False, "clipboard": None, "collapsed": set()}
_TAB_BUILDERS: dict[str, Any] = {
    "Settings · Display": _display_tab,
    "Settings · Historical": _historical_tab,
    "Strategy · General": _general_tab,
    "Strategy · Indicators": _indicators_tab,
    "Strategy · Conditions": _conditions_tab,
}


def _commit(tab_label: str) -> bool:
    """Validate and save the document backing ``tab_label``. Returns success."""
    doc_key = TAB_DOC[tab_label]
    doc = DOCS[doc_key]
    try:
        VALIDATORS[doc_key](doc)
    except ValidationError as e:
        first = str(e).splitlines()[1] if "\n" in str(e) else str(e)
        ui.notify(f"{doc_key} invalid — not saved: {first}", type="negative", timeout=6000)
        return False
    persistence.save_document(doc, PATHS[doc_key])
    ui.notify(f"Saved {PATHS[doc_key].name}", type="positive")
    return True


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
    _load_docs()
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

    with ui.tabs(on_change=on_tab_change).classes("w-full") as tabs:
        for label in TAB_DOC:
            ui.tab(label)

    with ui.tab_panels(tabs, value="Settings · Display").classes("w-full"):
        for label, builder in _TAB_BUILDERS.items():
            with ui.tab_panel(label):
                builder()


def main() -> None:
    ui.run(title="Config Editor", port=8080, reload=False, show=False)


if __name__ in {"__main__", "__mp_main__"}:
    main()
