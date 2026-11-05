import numpy as np
import pandas as pd
import pytest

from trade.qvix.config import INDEX_METHOD, QVIX_METHOD, Settings
from trade.qvix.metrics import qvix_context
from trade.qvix.options import method_id
from trade.qvix.report import build_report, report_data
from trade.qvix.store import Store


def test_windows_distinguish_recent_regime_and_long_history():
    dates = pd.bdate_range("2022-01-01", periods=756)
    past = pd.Series([40.] * 504 + [20.] * 252, index=dates)
    result = qvix_context(30., past)
    assert result["windows"]["252"]["percentile"] == 100
    assert result["windows"]["756"]["percentile"] == pytest.approx(100 / 3)
    assert result["change_5d"] == 10.
    assert result["change_5d_date"] == str(dates[-5].date())


def test_windows_require_coverage_without_reaching_past_window():
    past = pd.Series(20., index=pd.bdate_range("2022-01-01", periods=900))
    past.iloc[-51:] = np.nan  # 201/252 is below the declared 202 minimum.
    result = qvix_context(30., past)
    assert result["windows"]["252"]["sample_count"] == 201
    assert np.isnan(result["windows"]["252"]["percentile"])
    assert result["windows"]["756"]["percentile"] == 100
    assert np.isnan(result["change_5d"])  # Do not substitute the last available close.


def test_no_cross_method_change_or_same_method_rank_from_reference():
    reference = pd.Series(20., index=pd.bdate_range("2022-01-01", periods=756))
    result = qvix_context(30., reference * np.nan, reference=reference)
    assert np.isnan(result["change_5d"])
    assert np.isnan(result["windows"]["252"]["percentile"])
    assert result["reference_windows"]["252"]["percentile"] == 100


def test_report_old_snapshot_excludes_future_history_and_preserves_store(tmp_path):
    settings = Settings(data_dir=tmp_path)
    dates = pd.bdate_range("2022-01-01", periods=800)
    quote_date = dates[756]
    quote_time = str(quote_date.date()) + "T13:00:00+08:00"
    values = pd.Series([20.] * 756 + [40.] * 44, index=dates)
    item = {"value": 30., "status": "estimated", "quote_time": quote_time, "method": method_id(.015)}
    snapshot = {"qvix": {"50etf": item}, "generated_at": quote_time}
    with Store(tmp_path) as store:
        store.put("index:000300.SH", pd.DataFrame({"close": 100., "open": 100., "high": 101., "low": 99.}, index=dates), INDEX_METHOD, "seed")
        store.put("qvix:50etf", values.to_frame("close"), QVIX_METHOD, "seed")
        store.put("iv30:50etf", values.to_frame("close"), method_id(.015), "seed")
        store.save_run("sample", "snapshot", snapshot)
        _, daily = report_data(store, settings)
        assert daily.loc[quote_date, "50etf_percentile_252"] == 100
        assert daily.loc[quote_date, "50etf_change_5d"] == 20
        before = store.conn.total_changes
        html = build_report(settings, start=str(quote_date.date())).read_text()
        live = html[html.index("盘中采样 ·"):html.index("最近日级数据 ·")]
        assert "1年分位 100.00 · n=252" in live
        assert "3年分位 100.00 · n=756" in live
        assert "较5交易日前 +10.00 个百分点" in live
        assert store.latest_run("snapshot") == snapshot
        assert store.conn.total_changes == before
        assert len(store.read("iv30:50etf", method_id(.015))) == 800
    assert "linked-cursor" in html and "plotly_afterplot" in html
    assert '"hovermode":false' in html
    assert "{plot_id}" not in html
