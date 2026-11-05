"""Pure calculations. Windows count market sessions, not non-null observations."""
import numpy as np
import pandas as pd

from .config import FACTORS, RANK_WINDOWS


def percentile(value, past, window=756, min_history=252):
    sample = pd.Series(past, dtype=float).iloc[-window:]
    sample = sample[np.isfinite(sample)]
    n = len(sample)
    rank = np.nan
    if np.isfinite(value) and n >= min_history:
        rank = 100 * ((sample < value).sum() + .5 * (sample == value).sum()) / n
    return {"percentile": float(rank), "sample_count": n}


def rolling_percentile(values, window=756, min_history=252):
    # Bounded vectorized blocks avoid thousands of pandas objects per series.
    # Each window contains exactly the prior market sessions, never today's value.
    raw = values.to_numpy(dtype=float)
    if not len(raw):
        return pd.Series(index=values.index, dtype=float, name=values.name)
    padded = np.pad(raw, (window, 0), constant_values=np.nan)
    windows = np.lib.stride_tricks.sliding_window_view(padded, window + 1)
    result = np.full(len(raw), np.nan)
    for offset in range(0, len(raw), 512):
        block = windows[offset:offset + 512]
        past, current = block[:, :-1], block[:, -1:]
        valid = np.isfinite(past)
        count = valid.sum(axis=1)
        ranks = ((valid & (past < current)).sum(axis=1)
                 + .5 * (valid & (past == current)).sum(axis=1))
        good = (count >= min_history) & np.isfinite(current[:, 0])
        result[offset:offset + len(block)][good] = 100 * ranks[good] / count[good]
    return pd.Series(result, index=values.index, name=values.name)


def qvix_context(value, past, reference=None):
    """Past is indexed by market sessions strictly before the observation date."""
    def ranks(values):
        return {str(window): {**percentile(value, values, window, minimum), "min_history": minimum}
                for window, minimum in RANK_WINDOWS.items()}

    # No backfill and no cross-method subtraction; t-5 is the fifth prior session.
    baseline = float(past.iloc[-5]) if len(past) >= 5 else np.nan
    return {"windows": ranks(past),
            "reference_windows": ranks(reference) if reference is not None else {},
            "change_5d": float(value - baseline),
            "change_5d_date": str(past.index[-5].date()) if len(past) >= 5 else None}


def breadth(closes, amounts, ages):
    # Adjustment scaling must not move an exact -5% return across the threshold.
    returns = closes.pct_change(fill_method=None).round(10)
    valid = (closes.gt(0) & closes.shift().gt(0) & amounts.gt(0)
             & ages.ge(60) & returns.abs().le(.31))
    ma = closes.rolling(20, min_periods=16).mean()
    low = closes.shift().rolling(20, min_periods=16).min()

    def share(condition, eligible):
        return (condition & eligible).sum(axis=1) / eligible.sum(axis=1).replace(0, np.nan)

    out = pd.DataFrame({
        "count": valid.sum(axis=1),
        "below_ma20": share(closes < ma * (1 - 1e-10), valid & ma.notna()),
        "new_low20": share(closes < low * (1 - 1e-10), valid & low.notna()),
        "drop5_share": share(returns <= -.05, valid),
        "adv_share": share(returns > 0, valid),
    })
    prior_count = out["count"].shift().rolling(20, min_periods=5).median()
    out["coverage_ratio"] = out["count"] / prior_count.replace(0, np.nan)
    out["data_valid"] = (out["count"] >= 1000) & (out.coverage_ratio >= .85)
    out.loc[~out.data_valid, ["below_ma20", "new_low20", "drop5_share", "adv_share"]] = np.nan
    return out


def market_features(breadth_frame, indices):
    out = breadth_frame.copy()
    returns = pd.concat([
        indices[code].close.reindex(out.index).pct_change(fill_method=None)
        for code in ("000300.SH", "000852.SH")
    ], axis=1).mean(axis=1, skipna=False)
    out["basket_return"] = returns
    out["basket_loss5"] = 1 - (1 + returns).rolling(5).apply(np.prod, raw=True)
    out["downside_vol20"] = np.sqrt(252 * returns.clip(upper=0).pow(2).rolling(20).mean())
    # Rolling sums can differ at machine precision between full and incremental runs.
    columns = ["basket_return", "basket_loss5", "downside_vol20"]
    out[columns] = out[columns].round(12)
    return out


def score_history(features, window=756, min_history=252):
    out = features.copy()
    for name in FACTORS:
        out["p_" + name] = rolling_percentile(out[name], window, min_history)
    out["score"] = out[["p_" + name for name in FACTORS]].mean(axis=1, skipna=False)
    out.loc[~out.data_valid, "score"] = np.nan
    out["shock"] = (out.drop5_share >= .15) | (out.basket_loss5 >= .08)
    out["panic"] = out.data_valid & out.shock & (out.score >= 85)
    return out


def score_snapshot(features, history, window=756, min_history=252):
    ranks = {name: percentile(features.get(name, np.nan), history[name], window, min_history)
             for name in FACTORS}
    values = [item["percentile"] for item in ranks.values()]
    score = float(np.mean(values)) if features.get("data_valid", False) else np.nan
    shock = features.get("drop5_share", 0) >= .15 or features.get("basket_loss5", 0) >= .08
    return {"score": score, "factors": ranks, "features": features,
            "panic": bool(np.isfinite(score) and score >= 85 and shock)}
