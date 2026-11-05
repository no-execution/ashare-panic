"""Causal long-term market damage, kept separate from panic-v1.

Version 1 uses fixed, interpretable research scales, not fitted bottom labels:
median 252-session drawdown 5%..35%, share down >=30% 10%..70%,
60-session mean share below MA60 35%..85%. Equal-weight mean, 85 alert.
These are severity scores, not historical percentiles or bottom probabilities.
"""
import numpy as np
import pandas as pd

DAMAGE_METHOD = "damage-v1"
DAMAGE_FEATURES = ("median_dd252", "deep_loss_share", "below_ma60_share", "weakness60",
                   "damage_count", "damage_coverage", "damage_valid")
DAMAGE_SCALES = {
    "median_dd252": (.05, .35),
    "deep_loss_share": (.10, .70),
    "weakness60": (.35, .85),
}
# 252-session peaks, and a further 59 sessions for persistence/coverage warmup.
DAMAGE_WARMUP = 320


def damage_features(closes, amounts, ages):
    """Do not fill prices, gaps or coverage failures; price peaks include today.

    Each stock needs age >=252 bars and >=202 valid closes in the last 252
    actual market sessions. Persistence needs >=48 valid dates in 60 sessions.
    """
    returns = closes.pct_change(fill_method=None).round(10)
    peak = closes.rolling(252, min_periods=202).max()
    ma60 = closes.rolling(60, min_periods=48).mean()
    eligible = (closes.gt(0) & closes.shift().gt(0) & amounts.gt(0) & ages.ge(252)
                & returns.abs().le(.31) & peak.notna() & ma60.notna())
    count = eligible.sum(axis=1)
    reference = count.shift().rolling(20, min_periods=5).median().replace(0, np.nan)
    coverage = count / reference
    valid = count.ge(1000) & coverage.ge(.85)
    loss = (1 - closes / peak).clip(lower=0).where(eligible)
    frame = pd.DataFrame({
        "median_dd252": loss.median(axis=1),
        "deep_loss_share": (loss.ge(.30 - 1e-10) & eligible).sum(axis=1) / count.replace(0, np.nan),
        "below_ma60_share": ((closes < ma60 * (1 - 1e-10)) & eligible).sum(axis=1) / count.replace(0, np.nan),
        "damage_count": count, "damage_coverage": coverage,
    }, index=closes.index)
    frame.loc[~valid, ["median_dd252", "deep_loss_share", "below_ma60_share"]] = np.nan
    frame["weakness60"] = frame.below_ma60_share.rolling(60, min_periods=48).mean()
    frame["damage_valid"] = valid & frame[list(DAMAGE_SCALES)].notna().all(axis=1)
    return frame


def score_damage(features):
    out = features.copy()
    for name, (low, high) in DAMAGE_SCALES.items():
        out["d_" + name] = ((out[name] - low) / (high - low) * 100).clip(0, 100)
    out["damage_score"] = out[["d_" + name for name in DAMAGE_SCALES]].mean(axis=1, skipna=False)
    out.loc[~out.damage_valid.fillna(False).astype(bool), "damage_score"] = np.nan
    out["damage_alert"] = out.damage_score.ge(85)
    return out


def combine_pressure(short, long):
    """Union severity: retain either type of stress, never dilute an acute spike.

    Both inputs are required. A missing long score is not silently replaced by 0.
    Original short panic-v1 thresholds/flags remain unchanged.
    """
    return pd.concat([short, long], axis=1).max(axis=1, skipna=False)
