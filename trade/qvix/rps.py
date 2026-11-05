"""Read EOD RPS breadth; opt-in rebuild never changes the upstream RPS files."""
from pathlib import Path

import numpy as np
import pandas as pd

from .sources import now_shanghai


RULE = "当日最高价 >= 近250交易日最高价 * 阈值"
METHOD = "plot_rps_history.py：RPS50≥90 / RPS120>93 / RPS250>95；当日最高价≥近250交易日最高价×80%；占比分母为当日RPS股票池"
COUNTS = {"RPS候选数量": "candidates", "符合条件数量": "selected",
          "当日最高价缺失数量": "missing_price", "当日股票池数量": "universe"}
OUTPUT = "rps-history-source.csv"
# Chosen from the distribution, not optimized against subsequent returns.
# Fixed after selection: future refreshes must not silently move the line.
RISK_THRESHOLD_PCT = 2.5
RISK_CALIBRATION_END = "2026-10-08"


def risk_summary(frame):
    """Descriptive calibration audit, not a timing-performance backtest."""
    calibration = frame.loc[:RISK_CALIBRATION_END]
    sample = calibration["selected_pct"]
    valid = sample.dropna()
    comparisons = []
    for threshold in (2.4, 2.5, 2.6, 2.8, 3.0):
        high = sample.ge(threshold)
        groups = high.ne(high.shift()).cumsum()
        runs = high[high].groupby(groups[high]).size()
        comparisons.append({"threshold_pct": threshold, "high_records": int(high.sum()),
                            "share_pct": float(high.sum() / len(valid) * 100) if len(valid) else None,
                            "episodes": len(runs), "longest_records": int(runs.max()) if len(runs) else 0})
    # Sensitivity bound: every candidate with a missing high could qualify.
    # This does not fill the chart or pretend those unknown prices were observed.
    sensitivity = {}
    if {"selected", "missing_price", "universe"}.issubset(calibration.columns):
        priced = calibration.loc[sample.notna()]
        upper = 100 * (priced.selected + priced.missing_price) / priced.universe
        sensitivity = {"partial_price_records": int(priced.missing_price.gt(0).sum()),
                       "p95_upper_bound_pct": float(upper.quantile(.95)) if len(upper) else None,
                       "possible_high_records": int(upper.ge(RISK_THRESHOLD_PCT).sum()),
                       "ambiguous_high_records": int((priced.selected_pct.lt(RISK_THRESHOLD_PCT) & upper.ge(RISK_THRESHOLD_PCT)).sum())}
    return {"threshold_pct": RISK_THRESHOLD_PCT, "selected_on": RISK_CALIBRATION_END,
            "sample_start": str(valid.index[0].date()) if len(valid) else None,
            "sample_end": str(valid.index[-1].date()) if len(valid) else None,
            "records": len(valid), "p95_pct": float(valid.quantile(.95)) if len(valid) else None,
            "comparisons": comparisons, **sensitivity,
            "note": "全样本分布选定的固定参考线；历史触发是事后复盘，未经样本外择时验证，不是卖出信号。"}


def closed_date(now=None):
    stamp = pd.Timestamp(now if now is not None else now_shanghai())
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    # A conservative end-of-day boundary; having a file is not proof of a
    # completed day's RPS before this time. Missing holidays are not synthesized.
    return stamp.normalize() - (pd.Timedelta(days=1) if stamp.hour * 60 + stamp.minute < 15 * 60 + 10 else pd.Timedelta(0))


def read_counts(path, cutoff):
    raw = pd.read_csv(path, dtype=str)
    required = {"日期", *COUNTS, "RPS50条件", "RPS120条件", "RPS250条件", "高位比例阈值", "价格笼子规则"}
    if not required.issubset(raw.columns):
        raise ValueError("缺少日期、家数或口径字段")
    same = (raw.RPS50条件.eq(">=90") & raw.RPS120条件.eq(">93") & raw.RPS250条件.eq(">95")
            & pd.to_numeric(raw.高位比例阈值, errors="coerce").eq(.8) & raw.价格笼子规则.eq(RULE))
    if not same.all():
        raise ValueError("口径不一致，拒绝拼接（要求≥90 / >93 / >95及最高价80%过滤）")
    dates = pd.to_datetime(raw["日期"], errors="coerce")
    values = raw[list(COUNTS)].apply(pd.to_numeric, errors="coerce")
    valid = dates.notna() & dates.le(cutoff) & np.isfinite(values).all(axis=1)
    valid &= values.ge(0).all(axis=1) & values.mod(1).eq(0).all(axis=1)
    valid &= (values["符合条件数量"] + values["当日最高价缺失数量"]).le(values["RPS候选数量"])
    valid &= ~(values["RPS候选数量"].gt(0) & values["当日最高价缺失数量"].eq(values["RPS候选数量"]))
    valid &= values["当日股票池数量"].gt(0) & values["RPS候选数量"].le(values["当日股票池数量"])
    # Invalid source rows cannot become zeros; duplicates are not well-defined.
    if dates.dropna().duplicated().any():
        raise ValueError("日期重复")
    bad = int((~valid & (dates.isna() | dates.le(cutoff))).sum())
    usable_date = dates.notna() & dates.le(cutoff)
    result = values.loc[usable_date].rename(columns=COUNTS)
    result.loc[~valid] = np.nan
    result.index = pd.DatetimeIndex(dates.loc[usable_date], name="date")
    return result.sort_index(), bad


def load_counts(settings, *, now=None):
    sources = [settings.rps_counts, settings.data_dir / OUTPUT]
    frames, warnings, loaded = [], [], []
    # If either source is corrected later, its overlapping dates take priority.
    sources = sorted(set(Path(p) for p in sources if p is not None),
                     key=lambda p: p.stat().st_mtime_ns if p.exists() else -1)
    for path in sources:
        if not path.exists():
            continue
        try:
            frame, bad = read_counts(path, closed_date(now))
        except (ValueError, OSError) as exc:
            warnings.append(f"{path.name}: {exc}")
            continue
        frames.append(frame)
        loaded.append(str(path))
        if bad:
            warnings.append(f"{path.name}: {bad} 行无效，已留空")
    frame = pd.concat(frames) if frames else pd.DataFrame(columns=COUNTS.values(), index=pd.DatetimeIndex([], name="date"))
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    # Same percentage units and complete-window rolling mean as the plot.
    # Compute before viewport slicing, over the available RPS observation dates.
    frame["selected_pct"] = 100 * frame.selected / frame.universe
    frame["candidate_pct"] = 100 * frame.candidates / frame.universe
    for period in (5, 20):
        frame[f"ma{period}"] = frame.selected_pct.rolling(period, min_periods=period).mean()
    # Partial-price counts are lower bounds: they can confirm a breach, but
    # cannot prove that the true percentage is below the threshold.
    frame["risk_status"] = "below"
    frame.loc[frame.missing_price.gt(0), "risk_status"] = "incomplete"
    frame.loc[frame.selected_pct.ge(RISK_THRESHOLD_PCT), "risk_status"] = "high"
    frame.loc[frame.selected_pct.isna(), "risk_status"] = "missing"
    usable = frame.selected.dropna()
    latest = str(usable.index[-1].date()) if len(usable) else None
    return frame, {"latest": latest, "method": METHOD, "sources": loaded, "warnings": warnings,
                   "mean_pct": frame.selected_pct.mean(),
                   "risk": risk_summary(frame),
                   "days": {str(date.date()): row.to_dict() for date, row in frame.iterrows()}}


def high_ratios(daily_dir, candidates):
    """Delegate the high-price filter and trading calendar to the original plot."""
    from . import rps_helpers as original
    dates = set(candidates)
    calendar = original.build_trade_calendar(daily_dir, max(dates), dates)
    code_dates = {}
    for date, codes in candidates.items():
        for code in codes:
            code_dates.setdefault(code, set()).add(date)
    result, stats = {}, {"missing_file": 0, "failed_file": 0}
    for code, targets in sorted(code_dates.items()):
        ratios, status = original.compute_intraday_high_ratios({code}, daily_dir, calendar,
                                                               targets, max(dates), original.LOOKBACK_DAYS)
        result.update(ratios)
        for key in stats:
            stats[key] += status[key]
    return result, stats


def refresh_counts(settings, *, now=None):
    """Explicit historical aggregation, using the existing chart's price logic.

    Only qualifying codes are retained, rather than all stocks for every date.
    A full rebuild intentionally picks up corrections/backfills, not just appends.
    Ordinary report/snapshot runs do not call this relatively expensive path.
    """
    cutoff = str(closed_date(now).date())
    from . import rps_helpers as original
    conditions = [original.RPSCondition("RPS50", "ge", 90), original.RPSCondition("RPS120", "gt", 93),
                  original.RPSCondition("RPS250", "gt", 95)]
    files = sorted(p for p in settings.rps_history.glob("????-??-??.csv") if "2020-01-01" <= p.stem <= cutoff)
    if not files:
        raise ValueError(f"没有已收盘的 RPS 历史文件：{settings.rps_history}")
    candidates, pool, candidate_records = {}, {}, {}
    for path in files:
        # Validate metadata, then reuse the original parser (including its
        # exact denominator) one file at a time to avoid retaining millions of rows.
        raw = pd.read_csv(path, nrows=1, dtype={"股票代码": str})
        required = {"股票代码", "日期", "RPS50", "RPS120", "RPS250"}
        if not required.issubset(raw.columns) or not raw["日期"].eq(path.stem).all():
            raise ValueError(f"RPS 日期/字段不一致：{path}")
        records, codes = original.load_rps_records([path], conditions)
        pool[path.stem] = len(records[path.stem])
        candidates[path.stem] = codes
        candidate_records[path.stem] = [(code, values) for code, values in records[path.stem]
                                        if all(condition.match(values[condition.col]) for condition in conditions)]
    if not any(pool.values()):
        raise ValueError("RPS 历史无有效股票，保留原统计文件")
    dates = set(candidates)
    ratios, stats = high_ratios(settings.local_daily, candidates)
    rows = original.count_by_date(candidate_records, conditions, ratios, .8, True)
    for row in rows:
        # Non-candidates were discarded only for memory use, not the denominator.
        universe = pool[row["日期"]]
        row["当日股票池数量"] = universe
        row["RPS候选占比"] = row["RPS候选数量"] / universe if universe else 0.
        row["符合条件占比"] = row["符合条件数量"] / universe if universe else 0.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    target = settings.data_dir / OUTPUT
    staging = target.with_suffix(".tmp")
    original.write_counts_csv(staging, rows, conditions, .8, True, original.DEFAULT_START_DATE)
    staging.replace(target)
    return {"path": str(target), "dates": len(rows), "latest": max(dates), **stats}
