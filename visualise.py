import os

import mplfinance as mpf
import pandas as pd

import app_paths
import condition


def try_addplot(col_name, df, addplots, panel_no, ylabel=None, color="pink", type="line", label=None) -> bool:
    if not col_name in df.columns:
        raise condition.ColumnNotFoundError(col_name, df)

    col = df[col_name]
    if col.isna().all():
        return False

    kwargs = dict(panel=panel_no, color=color, type=type, secondary_y=False, width=0.85)
    if ylabel:
        kwargs["ylabel"] = ylabel
    if label:
        kwargs["label"] = label
    addplots.append(mpf.make_addplot(col, **kwargs))
    return True

# Colors auto-assigned to a composite condition's children, in order; cycles if
# there are more children than colors.
CHILD_COLORS = [
    "tab:blue", "tab:orange", "tab:green", "tab:red",
    "tab:purple", "tab:brown", "tab:pink", "tab:olive",
]

def examine_condition(examination_window: int, condition_id: str, timestamp: pd.Timestamp, df: pd.DataFrame, display_panels: list[dict], signal_aggregates, children: list[str] = None, name: str = None):
    # name carries the session's outcome so the file listing answers "which days
    # did this fire, and what blocked the rest" without opening anything.
    stem = name or f"{timestamp.date()} {timestamp.time().strftime('%H-%M')}"
    output_path = str(app_paths.output_dir() / str(condition_id) / f"{stem}.png")

    # ffill: the display bar containing the timestamp. bfill would pick the next
    # bar instead, running past the session end into the following day.
    pos = df.index.get_indexer([timestamp], method="ffill")[0]
    if pos == -1:
        print(f"\nCouldn't find a suitable timestamp to plot {condition_id} at {timestamp}!")
        return

    window = df.iloc[max(0, pos - (examination_window - 1)):pos + 1]

    addplots = []

    VOLUME_PANEL = 1

    i = 0
    for panel in display_panels:
        if i == VOLUME_PANEL:
            i += 1
        hadSuccess = False
        for item in panel.items():
            id, color = item
            added = try_addplot(id, window, addplots, i, None, color)
            hadSuccess = hadSuccess or added
        if hadSuccess:
            i += 1

    if children:
        # Draw children on the same panel as the condition's own line below
        # (shared panel i), so they overlay the parent signal rather than sitting
        # in a separate panel.
        representative_agg = signal_aggregates[0]
        for idx, child_id in enumerate(children):
            col = f"{child_id}_{representative_agg}"
            color = CHILD_COLORS[idx % len(CHILD_COLORS)]
            try_addplot(col, window, addplots, i, None, color, label=child_id)

    for agg in signal_aggregates:
        col = f"{condition_id}_{agg}"
        try_addplot(col, window, addplots, i, condition_id, "black")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    mpf.plot(
        window,
        type="candle",
        style="yahoo",
        volume=True,
        columns=["open", "high", "low", "close", "volume"],
        addplot=addplots,
        figsize=(20, 8),
        savefig=dict(fname=output_path, dpi=300),
    )