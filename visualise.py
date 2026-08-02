import os

import mplfinance as mpf
import pandas as pd

import condition


def try_addplot(col_name, df, addplots, panel_no, ylabel=None, color="pink", type="line") -> bool:
    if not col_name in df.columns:
        raise condition.ColumnNotFoundError(col_name, df)
    
    col = df[col_name]
    if col.isna().all():
        return False
    
    if ylabel:
        addplots.append(mpf.make_addplot(col, panel=panel_no, ylabel=ylabel, color=color, type=type, secondary_y = False, width=0.85))
    else:
        addplots.append(mpf.make_addplot(col, panel=panel_no, color=color, type=type, secondary_y = False, width=0.85))
    return True

def examine_condition(examination_window: int, condition_id: str, timestamp: pd.Timestamp, df: pd.DataFrame, display_panels: list[dict], signal_aggregates):
    output_path = f"output/{condition_id}/{timestamp.date()}/{timestamp.time().strftime('%H:%M')}.png"

    pos = df.index.get_indexer([timestamp], method="bfill")[0]
    if pos == -1:
        print(f"Couldn't find a suitable timestamp to plot {condition_id} onset at {timestamp}!")
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