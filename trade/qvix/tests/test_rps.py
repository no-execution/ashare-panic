import json
import os
import re

import numpy as np
import pandas as pd
import pytest

from trade.qvix.config import Settings, INDEX_METHOD
from trade.qvix.rps import RULE, OUTPUT, closed_date, load_counts, refresh_counts, high_ratios, risk_summary, RISK_THRESHOLD_PCT


def counts(path, dates, selected, **extra):
    pd.DataFrame({"日期": dates, "RPS候选数量": 10, "符合条件数量": selected,
                  "当日股票池数量": 100, "当日最高价缺失数量": 0, "RPS50条件": ">=90", "RPS120条件": ">93",
                  "RPS250条件": ">95", "高位比例阈值": .8, "价格笼子规则": RULE, **extra}).to_csv(path, index=False)


def settings_at(tmp_path):
    return Settings(data_dir=tmp_path, rps_counts=tmp_path / "source.csv", rps_history=tmp_path / "history",
                    local_daily=tmp_path / "daily")


def test_exact_dates_no_intraday_fill_and_source_updates(tmp_path):
    settings = settings_at(tmp_path)
    dates = ["2026-09-23", "2026-09-24", "2026-09-30"]
    counts(settings.rps_counts, dates, [4, 0, 8])
    frame, payload = load_counts(settings, now="2026-09-30T14:00:00+08:00")
    assert list(payload["days"]) == dates[:2]
    assert payload["latest"] == dates[1]
    assert frame.reindex(pd.to_datetime(["2026-09-29"])).selected.isna().all()
    assert frame.selected.iloc[-1] == 0  # Real zero is distinct from unpublished.
    counts(settings.rps_counts, dates, [4, 0, 9])
    frame, payload = load_counts(settings, now="2026-09-30T15:10:00+08:00")
    assert payload["latest"] == dates[-1] and frame.selected.iloc[-1] == 9
    assert closed_date("2026-09-30T06:00:00Z") == pd.Timestamp("2026-09-29")


def test_reject_mixed_rules_and_invalid_rows_override_older_values(tmp_path):
    settings = settings_at(tmp_path)
    counts(settings.rps_counts, ["2026-09-24"], [4], **{"RPS120条件": ">=93"})
    frame, payload = load_counts(settings, now="2026-10-01")
    assert frame.empty and "口径不一致" in payload["warnings"][0]
    counts(settings.rps_counts, ["2026-09-23", "2026-09-24"], [4, 4])
    counts(tmp_path / OUTPUT, ["2026-09-24"], [-1])
    os.utime(settings.rps_counts, (1, 1))
    frame, payload = load_counts(settings, now="2026-10-01")
    assert np.isnan(frame.selected.iloc[-1])
    assert payload["latest"] == "2026-09-23" and payload["warnings"]


def seed_history(settings, date, ranks=(90, 94, 96)):
    settings.rps_history.mkdir(exist_ok=True)
    pd.DataFrame({"股票代码": ["600000.SH"], "日期": [date], "RPS50": [ranks[0]],
                  "RPS120": [ranks[1]], "RPS250": [ranks[2]]}).to_csv(settings.rps_history / (date + ".csv"), index=False)


def test_refresh_uses_eod_high_not_close_and_picks_up_backfills(tmp_path):
    settings = settings_at(tmp_path)
    (settings.local_daily / "sh").mkdir(parents=True)
    bar_path = settings.local_daily / "sh/600000.SH.csv"
    bars = pd.DataFrame({"日期": ["2026-09-23", "2026-09-24", "2026-09-30"],
                         "最高": [10, 8, 20], "收盘": [9, 5, 19]})
    bars.to_csv(bar_path, index=False)
    for date in bars.日期:
        seed_history(settings, date)
    refresh_counts(settings, now="2026-09-30T14:00:00+08:00")
    frame, payload = load_counts(settings, now="2026-09-30T14:00:00+08:00")
    assert list(frame.selected) == [1, 1]  # RPS50/high-price equality, today's high ignored.
    assert payload["latest"] == "2026-09-24"
    seed_history(settings, "2026-09-24", (89, 93, 95))
    refresh_counts(settings, now="2026-09-30T16:00:00+08:00")
    frame, payload = load_counts(settings, now="2026-09-30T16:00:00+08:00")
    assert list(frame.selected) == [1, 0, 1]  # Corrections, true zero, newly published day.
    original = (tmp_path / OUTPUT).read_bytes()
    seed_history(settings, "2026-09-24", (90, 93, 95))
    bad = settings.rps_history / "2026-09-24.csv"
    bad.write_text("错误字段\n1\n")
    with pytest.raises(ValueError):
        refresh_counts(settings, now="2026-09-30T16:00:00+08:00")
    assert (tmp_path / OUTPUT).read_bytes() == original


def test_price_window_uses_250_calendar_sessions_and_missing_stays_missing(tmp_path):
    dates = pd.bdate_range("2025-01-01", periods=252).strftime("%Y-%m-%d")
    (tmp_path / "sh").mkdir()
    values = [100.] + [10.] * 249 + [np.nan, 10.]
    pd.DataFrame({"日期": dates, "最高": values}).to_csv(tmp_path / "sh/600000.SH.csv", index=False)
    ratios, _ = high_ratios(tmp_path, {date: {"600000.SH"} for date in dates[-3:]})
    assert ratios[(dates[-3], "600000.SH")] == .1
    assert (dates[-2], "600000.SH") not in ratios
    assert ratios[(dates[-1], "600000.SH")] == 1


def test_all_candidate_prices_missing_is_not_zero(tmp_path):
    settings = settings_at(tmp_path)
    counts(settings.rps_counts, ["2026-09-24"], [0], **{"当日最高价缺失数量": 10})
    frame, payload = load_counts(settings, now="2026-10-01")
    assert frame.selected.isna().all() and payload["latest"] is None
    (settings.local_daily / "sh").mkdir(parents=True)
    pd.DataFrame({"日期": ["2026-09-24"], "最高": [np.nan]}).to_csv(settings.local_daily / "sh/600000.SH.csv", index=False)
    seed_history(settings, "2026-09-24")
    refresh_counts(settings, now="2026-10-01")
    frame, payload = load_counts(settings, now="2026-10-01")
    assert frame.selected.isna().all() and payload["warnings"]


def test_cli_refresh_then_report_reads_new_aggregate(tmp_path):
    from trade.qvix.__main__ import main
    from trade.qvix.store import Store
    settings = settings_at(tmp_path)
    date = "2020-01-02"
    (settings.local_daily / "sh").mkdir(parents=True)
    pd.DataFrame({"日期": [date], "最高": [10.]}).to_csv(settings.local_daily / "sh/600000.SH.csv", index=False)
    seed_history(settings, date)
    with Store(tmp_path) as store:
        store.put("index:000300.SH", pd.DataFrame({"open": [10.], "high": [10.], "low": [10.], "close": [10.]}, index=pd.to_datetime([date])), INDEX_METHOD, "seed")
    result = main(["--data-dir", str(tmp_path), "--local-daily", str(settings.local_daily),
                   "--rps-history", str(settings.rps_history), "--rps-counts", str(settings.rps_counts),
                   "report", "--refresh-rps"])
    assert result == 0
    exported = pd.read_csv(tmp_path / "rps-breadth.csv")
    assert exported.selected.tolist() == [1]


def test_report_six_panels_keeps_rps_missing_on_snapshot_day(tmp_path):
    from trade.qvix.store import Store
    from trade.qvix.report import build_report
    settings = settings_at(tmp_path)
    counts(settings.rps_counts, ["2026-09-23", "2026-09-24"], [40, 36], **{"RPS候选数量": 44})
    dates = pd.bdate_range("2026-09-01", "2026-09-30")
    bars = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100.}, index=dates)
    with Store(tmp_path) as store:
        store.put("index:000300.SH", bars, INDEX_METHOD, "seed")
        store.save_run("snapshot-seed", "snapshot", {"generated_at": "2026-10-08T14:00:00+08:00"})
    html = build_report(settings).read_text()
    payload = json.loads(re.search(r'<script type="application/json" id="review-data">(.*?)</script>', html).group(1))
    assert payload["rps"]["latest"] == "2026-09-24"
    assert "2026-10-08" not in payload["rps"]["days"]
    assert 'id="rps-tools"' in html and '"xaxis6"' in html
    assert "report --refresh-rps" in html
    assert (tmp_path / "rps-breadth.csv").exists()
    assert 'id="rps-risk"' in html and '高位风险提示 2.50%' in html
    assert payload["rps"]["risk"]["threshold_pct"] == 2.5
    assert (tmp_path / "rps-risk.json").exists()


def test_refresh_matches_original_parser_filter_counts_and_denominator(tmp_path):
    from trade.qvix import rps_helpers as original
    settings = settings_at(tmp_path)
    (settings.local_daily / "sh").mkdir(parents=True)
    settings.rps_history.mkdir()
    date = "2020-01-02"
    records = pd.DataFrame({"日期": [date] * 6, "股票代码": [f"60000{i}.SH" for i in range(6)],
                            "RPS50": [90, 90, 90, 89, 99, 10], "RPS120": [94, 93, 94, 99, 99, 10],
                            "RPS250": [96, 96, 95, 99, 99, 10]})
    path = settings.rps_history / (date + ".csv")
    records.to_csv(path, index=False)
    for code in records.股票代码:
        pd.DataFrame({"日期": ["2019-12-31", date], "最高": [10., 8. if code != "600004.SH" else 7.],
                      "收盘": [9., 5.]}).to_csv(settings.local_daily / "sh" / (code + ".csv"), index=False)
    conditions = [original.RPSCondition("RPS50", "ge", 90), original.RPSCondition("RPS120", "gt", 93), original.RPSCondition("RPS250", "gt", 95)]
    parsed, codes = original.load_rps_records([path], conditions)
    calendar = original.build_trade_calendar(settings.local_daily, date, {date})
    ratios, _ = original.compute_intraday_high_ratios(codes, settings.local_daily, calendar, {date}, date, 250)
    expected = original.count_by_date(parsed, conditions, ratios, .8, True)[0]
    refresh_counts(settings, now="2020-01-03")
    actual = pd.read_csv(tmp_path / OUTPUT).iloc[0]
    for key, value in expected.items():
        if isinstance(value, float):
            assert actual[key] == pytest.approx(value)
        else:
            assert actual[key] == value
    assert actual["RPS候选数量"] == 2 and actual["符合条件数量"] == 1 and actual["当日股票池数量"] == 6


def test_percent_and_ma_match_original_and_have_no_future_leak(tmp_path):
    from trade.qvix.rps_helpers import rolling_mean
    settings = settings_at(tmp_path)
    dates = pd.bdate_range("2020-01-02", periods=25).strftime("%Y-%m-%d")
    counts(settings.rps_counts, dates, list(range(25)), **{"RPS候选数量": 30, "当日股票池数量": 200})
    frame, payload = load_counts(settings, now="2020-03-01")
    assert frame.selected_pct.iloc[-1] == 12
    assert frame.candidate_pct.eq(15).all()
    for period in (5, 20):
        np.testing.assert_allclose(frame[f"ma{period}"], np.array(rolling_mean(list(frame.selected_pct), period), dtype=float), equal_nan=True)
    before, _ = load_counts(settings, now=dates[19] + "T16:00:00+08:00")
    pd.testing.assert_frame_equal(before, frame.iloc[:20])
    assert payload["mean_pct"] == 6


def test_fixed_risk_threshold_equality_missing_and_partial_lower_bounds(tmp_path):
    settings = settings_at(tmp_path)
    dates = pd.bdate_range("2020-01-02", periods=5).strftime("%Y-%m-%d")
    counts(settings.rps_counts, dates, [4, 5, 6, 0, np.nan],
           **{"当日股票池数量": 200, "当日最高价缺失数量": [1, 0, 1, 0, 10]})
    frame, payload = load_counts(settings, now="2020-02-01")
    assert RISK_THRESHOLD_PCT == 2.5
    assert list(frame.risk_status) == ["incomplete", "high", "high", "below", "missing"]
    assert frame.selected_pct.iloc[1] == 2.5
    assert payload["risk"]["threshold_pct"] == 2.5
    assert frame.candidate_pct.iloc[3] > frame.selected_pct.iloc[3]  # Candidate line cannot trigger alert.
    assert payload["risk"]["partial_price_records"] == 2
    assert payload["risk"]["ambiguous_high_records"] == 1
    assert payload["risk"]["possible_high_records"] == 3


def test_risk_audit_counts_missing_as_episode_break_and_freezes_calibration_cutoff():
    dates = pd.bdate_range("2026-09-01", periods=6)
    frame = pd.DataFrame({"selected_pct": [2.4, 2.5, 2.8, np.nan, 3., 2.]}, index=dates)
    audit = risk_summary(frame)
    chosen = next(row for row in audit["comparisons"] if row["threshold_pct"] == 2.5)
    assert audit["records"] == 5
    assert chosen == {"threshold_pct": 2.5, "high_records": 3, "share_pct": 60., "episodes": 2, "longest_records": 2}
    frame.loc[pd.Timestamp("2027-01-01"), "selected_pct"] = 4.9
    assert risk_summary(frame) == audit  # New future data cannot retune the line or calibration sample.
