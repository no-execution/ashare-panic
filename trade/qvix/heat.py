"""Experimental index heat, not a sell signal or a top probability."""
import pandas as pd

from .metrics import rolling_percentile

HEAT_METHOD = "index-heat-v1"
HEAT_FACTORS = ("bias20", "bias60", "return5", "return20")


def index_heat(close):
    """Actual-session input; prior 756 sessions, >=252 observations per rank.

    Positive deviation/momentum only: non-positive values score zero, but
    insufficient history stays missing. Two equally weighted dimensions,
    each averaging its short and medium horizons. No forward observations.
    """
    close = close.where(close.gt(0))
    out = pd.DataFrame(index=close.index)
    for window in (20, 60):
        out[f"bias{window}"] = close / close.rolling(window, min_periods=window).mean() - 1
    for window in (5, 20):
        out[f"return{window}"] = close.pct_change(window, fill_method=None)
    for name in HEAT_FACTORS:
        raw = out[name]
        rank = rolling_percentile(raw, 756, 252)
        out["p_" + name] = rank
        out["h_" + name] = rank.mask(raw.le(0) & rank.notna(), 0.)
    out["extension"] = out[["h_bias20", "h_bias60"]].mean(axis=1, skipna=False)
    out["momentum"] = out[["h_return5", "h_return20"]].mean(axis=1, skipna=False)
    out["heat_score"] = out[["extension", "momentum"]].mean(axis=1, skipna=False)
    return out
