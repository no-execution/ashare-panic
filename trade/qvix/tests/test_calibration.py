from unittest.mock import patch

import numpy as np
import pandas as pd

from trade.qvix.calibration import check_calibration, paired_closes
from trade.qvix.config import Settings, QVIX_METHOD
from trade.qvix.options import method_id
from trade.qvix.store import Store


def test_pairs_require_exact_date_positive_values_and_exclude_current_day():
    dates = pd.date_range("2026-09-21", periods=5)
    own = pd.DataFrame({"close": [20., np.nan, 25., 30., 40.]}, index=dates)
    vendor = pd.DataFrame({"close": [21., 23., 0., 31., 41.]}, index=dates)
    pairs = paired_closes(own, vendor, dates[-1])
    assert list(pairs.index) == [dates[0], dates[3]]
    assert pairs.iv30.tolist() == [20., 30.]


def test_check_downloads_closed_vendor_history_but_never_promotes_snapshots(tmp_path):
    settings = Settings(data_dir=tmp_path)
    now = pd.Timestamp("2026-09-28 14:30", tz="Asia/Shanghai")
    frame = pd.DataFrame({"close": [20., 22.]}, index=pd.to_datetime(["2026-09-24", "2026-09-28"]))
    with Store(tmp_path) as store:
        store.save_run("old", "snapshot", {"qvix": {"50etf": {"value": 25., "status": "estimated"}}})
    with patch("trade.qvix.service.now_shanghai", return_value=now), \
         patch("trade.qvix.sources.PublicSource.qvix_daily", return_value={"50etf": frame}):
        result = check_calibration(settings)
    assert result["calibrated"] is False
    assert result["series"]["50etf"]["paired_days"] == 0
    assert result["series"]["50etf"]["own"]["count"] == 0
    assert result["series"]["50etf"]["vendor"]["count"] == 1
    assert "50etf" in result["errors"]
    with Store(tmp_path) as store:
        assert len(store.read("qvix:50etf", QVIX_METHOD)) == 1
        assert store.conn.execute("SELECT COUNT(*) FROM daily WHERE series LIKE 'iv30:%'").fetchone()[0] == 0


def test_after_close_allows_today_but_does_not_claim_one_pair_calibrates(tmp_path):
    settings = Settings(data_dir=tmp_path)
    now = pd.Timestamp("2026-09-28 18:00", tz="Asia/Shanghai")
    frame = pd.DataFrame({"close": [20.]}, index=pd.to_datetime(["2026-09-28"]))
    with Store(tmp_path) as store:
        store.put("iv30:50etf", frame, method_id(settings.risk_free_rate), "close")
    with patch("trade.qvix.service.now_shanghai", return_value=now), \
         patch("trade.qvix.sources.PublicSource.qvix_daily", return_value={"50etf": frame}):
        result = check_calibration(settings)
    assert result["series"]["50etf"]["paired_days"] == 1
    assert result["series"]["50etf"]["status"] == "paired_data_available"
    assert result["calibrated"] is False
