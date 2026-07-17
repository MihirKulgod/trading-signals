import mplfinance as mpf

import condition

def plot_candles(candles, filepath="chart.png", overlay_cols=None, panel_cols=None):
    addplots = []

    # Indicators that share price's scale (EMA, SMA, VWAP, BBANDS) go on the main chart
    if overlay_cols:
        for col in overlay_cols:
            addplots.append(mpf.make_addplot(candles[col], panel=0))

    # Indicators on a different scale (RSI, MACD) need their own subplot
    if panel_cols:
        for i, col in enumerate(panel_cols, start=2):  # panel 0=price, 1=volume
            addplots.append(mpf.make_addplot(candles[col], panel=i, ylabel=col))

    mpf.plot(
        candles,
        type="candle",
        style="yahoo",
        volume=True,
        columns=["open", "high", "low", "close", "volume"],
        addplot=addplots if addplots else None,
        savefig=filepath,
    )

def try_addplot(col_name, df, addplots, panel_no, ylabel=None, color="pink", type="line"):
    if not col_name in df.columns:
        raise condition.ColumnNotFoundError(col_name, df)
    
    col = df[col_name]
    if col.isna().all():
        return
    
    if ylabel:
        addplots.append(mpf.make_addplot(col, panel=panel_no, ylabel=ylabel, color=color, type=type, secondary_y = False, width=0.85))
    else:
        addplots.append(mpf.make_addplot(col, panel=panel_no, color=color, type=type, secondary_y = False, width=0.85))