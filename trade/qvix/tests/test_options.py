"""Math and integration tests for the independent live-options fallback."""
from math import erf, exp, log, sqrt
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import requests

from trade.qvix.config import INDEX_METHOD, QVIX_METHOD, Settings
from trade.qvix.options import OptionSource, method_id, observe_book_updates, parse_options, term_variance, variance30
from trade.qvix.report import build_report
from trade.qvix.service import snapshot
from trade.qvix.sources import PublicSource
from trade.qvix.store import Store

NOW = pd.Timestamp("2026-09-28 13:30:00", tz="Asia/Shanghai")


def synthetic_chain(days=(20, 50), volatility=.2):
    """Black forward-option prices on a dense strike grid give a known variance."""
    rows = []
    cdf = lambda x: .5 * (1 + erf(x / sqrt(2)))
    for day in days:
        t, forward, rate = day / 365, 100., .015
        for k in np.arange(50., 150.1, .5):
            d1 = (log(forward / k) + .5 * volatility ** 2 * t) / (volatility * sqrt(t))
            d2 = d1 - volatility * sqrt(t)
            call = exp(-rate * t) * (forward * cdf(d1) - k * cdf(d2))
            put = call - exp(-rate * t) * (forward - k)
            for kind, price in [("C", call), ("P", put)]:
                price = max(0., price)
                if price < 1e-10:
                    price = 0.
                rows.append(dict(code=f"{day}-{kind}-{k}", kind=kind, strike=k, bid=price, ask=price,
                                 timestamp=NOW, expiry=NOW + pd.Timedelta(days=day)))
    return pd.DataFrame(rows)


def test_constant_volatility_is_recovered_and_has_auditable_terms():
    result = variance30(synthetic_chain(), NOW)
    assert result["value"] == pytest.approx(20, abs=.05)
    assert result["tenor_mode"] == "interpolated"
    assert result["terms"][0]["forward"] == pytest.approx(100)
    assert result["quote_time"] == NOW.isoformat()
    assert result["terms"][0]["used_codes"]


def test_monthly_expiry_requires_explicit_bounded_extrapolation():
    result = variance30(synthetic_chain(days=(30.05, 58.05)), NOW)
    assert result["tenor_mode"] == "extrapolated"
    assert result["value"] == pytest.approx(20, abs=.05)
    assert any("外推" in w for w in result["warnings"])
    with pytest.raises(ValueError, match="到期期限"):
        variance30(synthetic_chain(days=(38, 60)), NOW)


def test_after_close_does_not_decay_maturity_with_frozen_quotes():
    chain = synthetic_chain()
    close = NOW.normalize() + pd.Timedelta(hours=15)
    chain["timestamp"] = close
    at_close = variance30(chain, close)
    at_night = variance30(chain, close + pd.Timedelta(hours=5))
    assert at_close["value"] == at_night["value"]


def test_stale_unused_itm_quote_does_not_disable_fresh_otm_estimate():
    chain = synthetic_chain()
    chain.loc[(chain.kind == "C") & (chain.strike == 60), "timestamp"] = NOW - pd.Timedelta(days=1)
    assert variance30(chain, NOW)["value"] == pytest.approx(20, abs=.05)


def test_stale_atm_is_rejected_and_stale_otm_is_disclosed():
    chain = synthetic_chain()
    chain.loc[(chain.kind == "C") & (chain.strike == 100), "timestamp"] = NOW - pd.Timedelta(days=1)
    with pytest.raises(ValueError, match="K0"):
        variance30(chain, NOW)
    chain = synthetic_chain()
    chain.loc[(chain.kind == "C") & (chain.strike == 110), "timestamp"] = NOW - pd.Timedelta(days=1)
    result = variance30(chain, NOW)
    assert sum(t["stale_otm_excluded"] for t in result["terms"]) == 2
    assert all("-C-110.0" not in code for t in result["terms"] for code in t["used_codes"])
    assert any("过期" in w for w in result["warnings"])


def test_two_zero_bid_strikes_stop_each_tail():
    chain = synthetic_chain(days=(20,))
    mask = (chain.kind == "C") & chain.strike.isin([110., 110.5])
    chain.loc[mask, "bid"] = 0.
    result = term_variance(chain, NOW, .015)
    assert result["max_strike"] == 109.5


def test_bad_quote_duplicates_and_missing_pairs_are_rejected():
    chain = synthetic_chain(days=(20,))
    with pytest.raises(ValueError, match="重复"):
        term_variance(pd.concat([chain, chain.iloc[:1]]), NOW, .015)
    with pytest.raises(ValueError, match="不匹配"):
        term_variance(chain.iloc[1:], NOW, .015)
    chain.loc[(chain.kind == "C") & (chain.strike == 100), "ask"] = 0
    with pytest.raises(ValueError, match="倒挂"):
        term_variance(chain, NOW, .015)


def quote_payload(index=False, adjusted=False):
    f = [""] * 51
    for i, value in {1: "0.019", 3: "0.0192", 7: "3", 8: "0.03", 32: "2026-09-28 13:30:00",
                     37: "测试合约", 43: "A" if adjusted else "M", 45: "C",
                     46: "2026-07-20" if index else "2026-10-28", 47: "18" if index else "30"}.items():
        f[i] = value
    symbol = "P_OP_mo2610C7400" if index else "CON_OP_10012313"
    return (f'var hq_str_{symbol}="' + ",".join(f) + '";').encode("gb18030")


def test_dated_parser_and_different_expiry_semantics():
    etf = parse_options(quote_payload()).iloc[0]
    assert etf.timestamp == NOW and etf.expiry == pd.Timestamp("2026-10-28 15:00", tz=NOW.tz)
    mo = parse_options(quote_payload(index=True)).iloc[0]
    assert mo.expiry == pd.Timestamp("2026-10-16 15:00", tz=NOW.tz)
    assert parse_options(quote_payload(adjusted=True)).empty


def test_book_change_proves_update_but_download_time_alone_does_not():
    previous = parse_options(quote_payload())
    previous["timestamp"] = NOW - pd.Timedelta(hours=1)
    previous["observed_at"] = NOW
    current = previous.copy()
    current["observed_at"] = NOW + pd.Timedelta(seconds=2)
    unchanged = observe_book_updates(previous, current, NOW + pd.Timedelta(seconds=3))
    assert unchanged.effective_timestamp.iloc[0] == previous.timestamp.iloc[0]
    current["bid"] += .0001
    changed = observe_book_updates(previous, current, NOW + pd.Timedelta(seconds=3))
    assert changed.effective_timestamp.iloc[0] == NOW
    assert changed.timestamp.iloc[0] == previous.timestamp.iloc[0]  # Original source clock retained.
    assert changed.time_basis.iloc[0] == "observed_book_change"
    later = changed.copy()
    later["observed_at"] += pd.Timedelta(seconds=2)
    assert observe_book_updates(changed, later, NOW + pd.Timedelta(seconds=5)).effective_timestamp.iloc[0] == NOW
    current["timestamp"] -= pd.Timedelta(days=1)
    assert observe_book_updates(previous, current, NOW + pd.Timedelta(seconds=3)).time_basis.iloc[0] == "source_timestamp"


def test_book_change_cannot_validate_future_or_different_contract():
    previous = parse_options(quote_payload()).assign(observed_at=NOW)
    current = previous.copy()
    current["bid"] += .0001
    current["timestamp"] = NOW + pd.Timedelta(hours=1)
    result = observe_book_updates(previous, current, NOW + pd.Timedelta(seconds=2))
    assert result.time_basis.iloc[0] == "source_timestamp"
    current["strike"] += 1
    with pytest.raises(ValueError, match="身份"):
        observe_book_updates(previous, current, NOW + pd.Timedelta(seconds=2))


def test_vendor_failure_automatically_uses_dated_option_chain(tmp_path):
    source = PublicSource(tmp_path)
    expected = {"50etf": {"status": "estimated", "value": 20.}}
    with patch.object(source, "get", side_effect=requests.Timeout("offline")), \
         patch.object(OptionSource, "snapshots", return_value=expected):
        result = source.qvix_snapshot(NOW, 300)
    assert result["50etf"]["value"] == 20
    assert result["50etf"]["vendor_status"]["status"] == "unavailable"


def test_reference_rank_is_separate_and_snapshot_does_not_write_own_history(tmp_path):
    settings = Settings(data_dir=tmp_path, local_daily=tmp_path / "missing", window=10, min_history=5)
    dates = pd.bdate_range("2026-08-03", periods=30)
    with Store(tmp_path) as store:
        index = pd.DataFrame({"close": np.arange(30) + 100., "open": 100., "high": 150., "low": 99.}, index=dates)
        store.put("index:000300.SH", index, INDEX_METHOD, "seed")
        store.put("qvix:50etf", pd.DataFrame({"close": np.arange(30) + 10.}, index=dates), QVIX_METHOD, "seed")
    current = {"50etf": {"status": "estimated", "value": 35., "quote_time": NOW.isoformat(),
                          "method": method_id(.015), "warnings": []}}
    with patch("trade.qvix.service.PublicSource.quotes", return_value=(pd.DataFrame(), [])), \
         patch("trade.qvix.service.PublicSource.qvix_snapshot", return_value=current):
        result = snapshot(settings)
    item = result["qvix"]["50etf"]
    assert item["percentile"] is None and item["sample_count"] == 0
    assert item["reference_percentile"] == 55.
    with Store(tmp_path) as store:
        assert store.read("iv30:50etf", method_id(.015)).empty
        assert store.read("qvix:50etf", QVIX_METHOD).close.iloc[-1] == 39
    html = build_report(settings).read_text()
    assert "跨口径参考（未校准）" in html and "自算IV30" in html
    assert "1年分位 缺失 · n=30" in html  # No short custom window masquerading as a year.
    assert html.index("盘中采样 ·") < html.index("最近日级数据 ·")
