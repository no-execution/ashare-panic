from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trade.qvix.config import FACTORS, INDEX_METHOD, PANIC_METHOD, QVIX_METHOD, Settings
from trade.qvix.market import adjusted_close
from trade.qvix.metrics import breadth, market_features, percentile, rolling_percentile, score_snapshot
from trade.qvix.report import build_report, report_data
from trade.qvix.service import snapshot, sync
from trade.qvix.sources import parse_qvix_daily, parse_qvix_minute, parse_quotes, quote_is_fresh
from trade.qvix.store import Store

TZ = ZoneInfo("Asia/Shanghai")


def bars(dates, values):
    return pd.DataFrame({"open": values, "high": np.asarray(values) + 1,
                         "low": np.asarray(values) - 1, "close": values}, index=dates)


def test_percentile_excludes_today_and_counts_sessions_not_valid_rows():
    values = pd.Series([1., 2., np.nan, 3., 3., 100.])
    ranks = rolling_percentile(values, window=3, min_history=2)
    assert ranks.iloc[4] == 75  # [2, missing, 3]; half credit for ties
    assert ranks.iloc[5] == 100
    assert np.isnan(percentile(100, [1, 2, np.nan, np.nan], 3, 2)["percentile"])
    modified = values.copy()
    modified.iloc[-1] = -100
    pd.testing.assert_series_equal(ranks.iloc[:-1], rolling_percentile(modified, 3, 2).iloc[:-1])


def test_invalid_breadth_cannot_emit_score_even_with_extreme_factors():
    past = pd.DataFrame({name: [1., 2., 3.] for name in FACTORS})
    current = {name: 10. for name in FACTORS}
    current["data_valid"] = False
    result = score_snapshot(current, past, 3, 2)
    assert np.isnan(result["score"]) and not result["panic"]


def test_store_upsert_revisions_and_snapshot_separation(tmp_path):
    frame = pd.DataFrame({"close": [20.]}, index=pd.to_datetime(["2026-09-24"]))
    with Store(tmp_path) as store:
        assert store.put("qvix:50etf", frame, QVIX_METHOD, "a") == 1
        assert store.put("qvix:50etf", frame, QVIX_METHOD, "b") == 0
        frame.iloc[0, 0] = 21
        assert store.put("qvix:50etf", frame, QVIX_METHOD, "c") == 1
        store.save_run("d", "snapshot", {"score": np.nan})
        assert len(store.read("qvix:50etf", QVIX_METHOD)) == 1
        assert store.conn.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
        assert store.latest_run("snapshot")["score"] is None


def test_time_only_qvix_is_never_assigned_todays_date():
    now = datetime(2026, 9, 28, 10, 30, tzinfo=TZ)
    assert parse_qvix_minute(b"time,value\n10:29:00,20\n", now, 300)["status"] == "unverified_date"
    dated = b"time,value\n2026-09-28 10:29:00,20\n"
    assert parse_qvix_minute(dated, now, 300)["value"] == 20
    stale = b"time,value\n2026-09-25 10:29:00,20\n"
    assert parse_qvix_minute(stale, now, 300)["status"] == "stale"
    invalid_last = dated + b"2026-09-28 10:30:00,NaN\n"
    assert parse_qvix_minute(invalid_last, now, 300)["status"] == "invalid"


def test_freshness_lunch_close_future_and_wrong_date():
    assert quote_is_fresh(pd.Timestamp("2026-09-28 11:30", tz=TZ), datetime(2026, 9, 28, 12, tzinfo=TZ), 300)
    assert quote_is_fresh(pd.Timestamp("2026-09-28 15:00", tz=TZ), datetime(2026, 9, 28, 18, tzinfo=TZ), 300)
    assert not quote_is_fresh(pd.Timestamp("2026-09-28 14:00", tz=TZ), datetime(2026, 9, 28, 18, tzinfo=TZ), 300)
    assert not quote_is_fresh(pd.Timestamp("2026-09-28 11:00", tz=TZ), datetime(2026, 9, 28, 10, tzinfo=TZ), 300)


def test_qvix_mapping_invalid_prices_and_duplicates():
    columns = ["Unnamed: 0"] + [str(i) for i in range(2, 88)]
    raw = pd.DataFrame([["2026/9/24"] + [0] * 86], columns=columns)
    for first in (1, 9, 25, 67):
        raw.iloc[0, first:first + 4] = [20, 22, 19, 21]
    raw.iloc[0, 28] = 0  # upstream sometimes returns zero / Excel errors for unavailable series
    parsed = parse_qvix_daily(raw.to_csv(index=False).encode("gbk"))
    assert parsed["500etf"].close.iloc[0] == 21
    assert np.isnan(parsed["1000index"].close.iloc[0])
    duplicate = pd.concat([raw, raw]).to_csv(index=False).encode("gbk")
    with pytest.raises(ValueError, match="Duplicate"):
        parse_qvix_daily(duplicate)


def test_quote_parser_preserves_date_and_previous_reference():
    fields = [""] * 36
    for index, value in {3: "9", 4: "10", 5: "9.5", 6: "100", 30: "20260928103000",
                         33: "10", 34: "8.9", 35: "a/b/123"}.items():
        fields[index] = value
    payload = ('v_sh600000="' + "~".join(fields) + '";v_sz000001="";').encode()
    quote = parse_quotes(payload).loc["600000.SH"]
    assert quote.prevclose == 10 and quote.amount == 123
    assert str(quote.timestamp) == "2026-09-28 10:30:00+08:00"


def test_adjustment_is_causal_and_quote_reference_not_double_counted():
    dates = pd.date_range("2026-01-01", periods=3)
    frame = pd.DataFrame({"close": [10., 5., 5.5], "prevclose": [np.nan, 5., 5.]}, index=dates)
    actions = pd.DataFrame([dict(date=dates[1], countForSell=0, countAsGift=100000,
                                 countOfIncreasement=0, suogu=0, bonus=0, priceForSell=0)])
    assert adjusted_close(frame, actions).tolist() == [10., 10., 11.]
    frame = frame.drop(columns="prevclose")
    assert adjusted_close(frame, actions).tolist() == [10., 10., 11.]


def test_breadth_invariant_to_adjustment_scale_at_exact_threshold():
    dates = pd.bdate_range("2026-01-01", periods=30)
    closes = pd.DataFrame(np.full((30, 1000), 10.), index=dates)
    closes.iloc[-1] = 9.5
    amounts = closes * 100
    ages = closes * 100
    original = breadth(closes, amounts, ages)
    scaled = breadth(closes * 1.23456789, amounts, ages)
    assert original.drop5_share.iloc[-1] == scaled.drop5_share.iloc[-1] == 1
    pd.testing.assert_series_equal(original.new_low20, scaled.new_low20)


def test_full_and_incremental_market_features_agree():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2020-01-01", periods=1500)
    indices = {code: pd.DataFrame({"close": 100 * (1 + rng.normal(0, .02, 1500)).cumprod()}, index=dates)
               for code in ("000300.SH", "000852.SH")}
    full = market_features(pd.DataFrame(index=dates), indices)
    short = market_features(pd.DataFrame(index=dates[-60:]), indices)
    pd.testing.assert_frame_equal(full.iloc[-10:], short.iloc[-10:], check_exact=True)


def seed(store):
    dates = pd.bdate_range("2026-01-01", periods=30)
    for code in ("000001.SH", "000300.SH", "000852.SH"):
        store.put("index:" + code, bars(dates, np.arange(30) + 100), INDEX_METHOD, "seed")
    store.put("qvix:50etf", bars(dates, np.arange(30) + 10), QVIX_METHOD, "seed")
    features = pd.DataFrame({name: np.arange(30) / 100 for name in FACTORS}, index=dates)
    features["data_valid"] = True
    store.put("panic", features, PANIC_METHOD, "seed")
    return dates


def test_report_date_filter_does_not_shrink_percentile_history(tmp_path):
    settings = Settings(data_dir=tmp_path, window=10, min_history=5)
    with Store(tmp_path) as store:
        dates = seed(store)
        _, full = report_data(store, settings)
        assert full["50etf_percentile"].iloc[-1] == 100
    path = build_report(settings, start=str(dates[-2].date()))
    assert path.exists() and "plotly.js" in path.read_text()
    daily = pd.read_csv(tmp_path / "daily.csv")
    assert len(daily) == 30 and daily["50etf_percentile"].iloc[-1] == 100


@pytest.mark.parametrize("closed_valid", [True, False])
def test_report_close_replaces_morning_pressure_only_when_valid(tmp_path, closed_valid):
    import json
    import re
    from trade.qvix.damage import DAMAGE_METHOD

    settings = Settings(data_dir=tmp_path, window=10, min_history=5)
    with Store(tmp_path) as store:
        dates = seed(store)
        features = store.read("panic", PANIC_METHOD)
        features["count"] = 1500
        features.loc[dates[-1], "data_valid"] = closed_valid
        store.put("panic", features, PANIC_METHOD, "close")
        damage = pd.DataFrame({"median_dd252": .20, "deep_loss_share": .40,
                               "weakness60": .60, "damage_valid": True}, index=dates)
        store.put("damage", damage, DAMAGE_METHOD, "close")
        stamp = str(dates[-1].date()) + "T11:44:00+08:00"
        morning = {"generated_at": stamp, "quote_time_max": stamp,
                   "panic": {"score": 75.46, "features": {"count": 1500}},
                   "damage": {"damage_score": 80.46}, "pressure_score": 80.46}
        store.save_run("morning", "snapshot", morning)
    html = build_report(settings).read_text()
    payload = json.loads(re.search(r'<script type="application/json" id="review-data">(.*?)</script>', html).group(1))
    overview = html[html.index('<section class="overview">'):html.index('<section class="workspace"')]
    assert payload["snapshot"]["pressure_superseded"] is closed_valid
    if closed_valid:
        assert "日级评分" in overview and "100.00" in overview and "75.46" not in overview
        assert '"name":"短期恐慌采样"' not in html
        assert '"name":"长期损伤采样"' not in html
        assert '"name":"综合压力采样"' not in html
    else:
        assert "盘中估算" in overview and "75.46" in overview
        assert '"name":"短期恐慌采样"' in html
    with Store(tmp_path) as store:
        assert store.latest_run("snapshot") == morning


def test_sync_offline_is_idempotent_and_does_not_promote_snapshot(tmp_path):
    settings = Settings(data_dir=tmp_path, local_daily=tmp_path / "missing", window=10, min_history=5)
    with Store(tmp_path) as store:
        dates = seed(store)
        original = store.read("panic", PANIC_METHOD)
    frozen = datetime(2026, 2, 13, 10, tzinfo=TZ)
    features = original.assign(count=1500, coverage_ratio=1.0, basket_return=.01, adv_share=.5)
    with patch("trade.qvix.service.now_shanghai", return_value=frozen), \
         patch("trade.qvix.service.load_features", return_value=(features, [])), \
         patch("trade.qvix.service.PublicSource.quotes", side_effect=AssertionError("offline network")):
        sync(settings, offline=True)
        second = sync(settings, offline=True)
    assert second["changed"]["panic"] == 0
    with Store(tmp_path) as store:
        assert store.read("panic", PANIC_METHOD).index.max() == dates[-1]


def test_snapshot_provider_failure_keeps_history_and_records_partial_result(tmp_path):
    settings = Settings(data_dir=tmp_path, local_daily=tmp_path / "missing", window=10, min_history=5)
    with Store(tmp_path) as store:
        seed(store)
        before = store.conn.execute("SELECT * FROM daily ORDER BY series,date").fetchall()
    unavailable = {name: {"status": "unavailable", "value": None} for name in ("50etf", "300etf", "500etf", "1000index")}
    with patch("trade.qvix.service.PublicSource.quotes", return_value=(pd.DataFrame(), ["timeout"])), \
         patch("trade.qvix.service.PublicSource.qvix_snapshot", return_value=unavailable):
        result = snapshot(settings)
    assert result["panic"]["status"] == "unavailable"
    assert result["qvix"]["50etf"]["previous_close"]["value"] == 39
    with Store(tmp_path) as store:
        after = store.conn.execute("SELECT * FROM daily ORDER BY series,date").fetchall()
    assert before == after


@pytest.mark.parametrize("hour,quote_clock,should_save", [(14, "14:00", False), (16, "14:59", False), (16, "15:00", True)])
def test_only_independent_post_close_quotes_can_enter_daily_input(tmp_path, hour, quote_clock, should_save):
    settings = Settings(data_dir=tmp_path, local_daily=tmp_path / "missing", window=10, min_history=5)
    with Store(tmp_path) as store:
        dates = seed(store)
        features = store.read("panic", PANIC_METHOD)
    today = dates[-1] + pd.offsets.BDay()
    frozen = datetime(today.year, today.month, today.day, hour, tzinfo=TZ)
    remote = bars(pd.DatetimeIndex([today]), [130.])
    quotes = bars(pd.Index(["600000.SH"], name="code"), [10.])
    quotes["timestamp"] = pd.Timestamp(f"{today:%Y-%m-%d} {quote_clock}", tz=TZ)
    quotes["prevclose"] = 10.
    quotes["amount"] = 100.
    with patch("trade.qvix.service.now_shanghai", return_value=frozen), \
         patch("trade.qvix.service.PublicSource.index_daily", return_value=remote), \
         patch("trade.qvix.service.PublicSource.qvix_daily", return_value={}), \
         patch("trade.qvix.service.PublicSource.quotes", return_value=(quotes, [])), \
         patch("trade.qvix.service.OptionSource.snapshots", return_value={}), \
         patch("trade.qvix.service.load_features", return_value=(features, [])):
        sync(settings)
    assert (tmp_path / "closes" / f"{today:%Y-%m-%d}.csv.gz").exists() == should_save
    if hour == 14:
        with Store(tmp_path) as store:
            assert store.read("index:000300.SH", INDEX_METHOD).index.max() < today
