"""Retrospective coverage diagnostics, not an optimized or out-of-sample backtest."""
import numpy as np
import pandas as pd

from .damage import DAMAGE_SCALES

CASES = {
    "2013-06-25": "流动性冲击观察", "2015-08-26": "急跌观察", "2016-01-28": "急跌观察",
    "2018-10-18": "长期熊市观察", "2019-01-03": "长期熊市观察", "2020-02-03": "急跌观察",
    "2020-03-23": "急跌观察", "2022-04-25": "急跌与长期压力", "2022-10-31": "长期低迷观察",
    "2024-02-05": "急跌观察", "2024-06-24": "持续下跌中段", "2024-08-28": "9·24前低迷",
    "2024-09-23": "9·24前一日", "2025-04-07": "急跌观察",
}


def validate_damage(daily, indices):
    paired = daily.dropna(subset=["score", "damage_score"])
    candidates = {
        "short_original": paired.score,
        "blend_75_25": .75 * paired.score + .25 * paired.damage_score,
        "blend_50_50": .5 * paired.score + .5 * paired.damage_score,
        "union_max": paired[["score", "damage_score"]].max(axis=1),
    }
    high_short = paired.score.ge(90)
    comparison = []
    for name, values in candidates.items():
        item = {"method": name, "paired_days": len(paired), "days_ge85": int(values.ge(85).sum()),
                "original_short_ge90": int(high_short.sum()),
                "retained_short_ge90": int((high_short & values.ge(90)).sum()),
                "new_days_ge85": int((values.ge(85) & paired.score.lt(85)).sum())}
        comparison.append(item)
    cases = []
    for date, label in CASES.items():
        if pd.Timestamp(date) not in daily.index:
            continue
        row = daily.loc[date]
        cases.append({"date": date, "label": label, "short": row.score, "long": row.damage_score,
                      "combined": row.pressure_score, "median_dd252": row.get("median_dd252"),
                      "deep_loss_share": row.get("deep_loss_share"), "weakness60": row.get("weakness60")})
    years = []
    for year, group in paired.groupby(paired.index.year):
        years.append({"year": int(year), "valid": len(group), "short_ge85": int(group.score.ge(85).sum()),
                      "long_ge85": int(group.damage_score.ge(85).sum()),
                      "combined_ge85": int(group.pressure_score.ge(85).sum()),
                      "additional": int((group.damage_score.ge(85) & group.score.lt(85)).sum())})
    # Group long alerts across actual index sessions; unavailable rows break runs.
    mask = daily.damage_score.ge(85)
    groups = mask.ne(mask.shift()).cumsum()
    episodes = []
    for _, group in daily.loc[mask].groupby(groups[mask]):
        date = group.index[0]
        item = {"start": str(date.date()), "end": str(group.index[-1].date()), "sessions": len(group),
                "peak": group.damage_score.max(), "short_at_start": group.score.iloc[0]}
        # Diagnostic only: enter NEXT open, then inspect 60 actual sessions.
        # Full horizons required; missing OHLC never gets compressed away.
        for code in ("000300.SH", "000852.SH"):
            bars = indices.get(code, pd.DataFrame()).reindex(index=daily.index, columns=["open", "low", "close"])
            offset = daily.index.get_loc(date) + 1
            future = bars.iloc[offset:offset + 60]
            valid = len(future) == 60 and future[["open", "low", "close"]].notna().all().all()
            item[code + "_forward60_return"] = future.close.iloc[-1] / future.open.iloc[0] - 1 if valid else np.nan
            item[code + "_forward60_worst"] = future.low.min() / future.open.iloc[0] - 1 if valid else np.nan
        episodes.append(item)
    sensitivity = []
    for date in CASES:
        if pd.Timestamp(date) not in paired.index:
            continue
        row = paired.loc[date]
        for multiplier in (.9, 1., 1.1):
            parts = [np.clip((row[key] - low) / (high * multiplier - low) * 100, 0, 100)
                     for key, (low, high) in DAMAGE_SCALES.items()]
            sensitivity.append({"date": date, "upper_anchor_multiplier": multiplier, "long_score": float(np.mean(parts))})
    return {"method": "damage-v1", "chosen_fusion": "union_max", "comparison": comparison,
            "cases": cases, "years": years, "long_episodes": episodes, "sensitivity": sensitivity,
            "limitations": ["案例为事后指定的诊断样本，没有独立底部标签，不是样本外验证", "阈值为固定研究刻度，未做收益寻优",
                            "高压力可持续很久，并可继续下跌；新增覆盖不等于底部预测准确率",
                            "前瞻观察从下一交易日开盘起算，未计成本；不足60日或价格缺失留空",
                            "源历史存在复权、退市覆盖及修订限制；不能当作严格当时可知回测"]}
