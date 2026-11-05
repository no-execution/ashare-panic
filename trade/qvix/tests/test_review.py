from unittest.mock import patch

import numpy as np
import pandas as pd

from trade.qvix.config import FACTORS, QVIX, Settings
from trade.qvix.market import adjusted_close, load_features, read_bars
from trade.qvix.metrics import breadth, market_features, percentile, rolling_percentile
from trade.qvix.review import review_data


def test_vector_ranks_match_reference_including_missing_and_ties():
    rng = np.random.default_rng(23)
    values = pd.Series(rng.integers(0, 12, 1200).astype(float), name="factor")
    values.iloc[::7] = np.nan
    for window, minimum in [(10, 3), (252, 202), (756, 605)]:
        expected = pd.Series([percentile(value, values.iloc[max(0, i - window):i], window, minimum)["percentile"]
                              for i, value in enumerate(values)], name="factor")
        pd.testing.assert_series_equal(rolling_percentile(values, window, minimum), expected)
    assert rolling_percentile(pd.Series([], dtype=float)).empty


def test_episode_groups_use_sessions_and_missing_breaks_events():
    dates = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"])
    daily = pd.DataFrame({"score": [86., 91., np.nan, 89.], "panic": [True, True, False, True],
                          "drop5_share": .2, "basket_loss5": .1}, index=dates)
    for name in QVIX:
        daily[name] = [np.nan, 20., np.nan, 21.]
    result = review_data(daily)
    assert [e["days"] for e in result["events"]] == [1, 2]
    assert result["events"][1]["date"] == "2020-01-03"
    assert result["years"][0]["valid"] == 3
    assert result["years"][0]["panic"] == 3
    assert result["days"]["2020-01-06"]["score"] is None
    assert result["coverage"][0]["count"] == 2


def test_cached_live_panels_match_causal_append_and_invalidate(tmp_path):
    settings = Settings(data_dir=tmp_path / "out", local_daily=tmp_path / "bars", actions_db=tmp_path / "missing.db")
    (settings.local_daily / "sh").mkdir(parents=True)
    dates = pd.bdate_range("2025-01-01", periods=85)
    paths = []
    # Validate the actual breadth inputs, including differing listing ages,
    # an ex-rights prevclose, and a missing prior session.
    for j in range(3):
        frame = pd.DataFrame({"date": dates[:-1], "close": np.linspace(10., 11., 84), "amount": 100.})
        if j == 1:
            frame = frame.iloc[:-1]
        if j == 2:
            frame = frame.iloc[-30:]
        path = settings.local_daily / "sh" / f"60000{j}.SH.csv"
        frame.to_csv(path, index=False)
        paths.append(path)
    quote = pd.DataFrame({"close": [5., 10.5, 10.], "prevclose": [5.5, 11., 11.], "amount": 500.},
                         index=pd.Index([p.stem for p in paths], name="code"))
    indices = {code: pd.DataFrame({"close": np.arange(len(dates)) + 100}, index=dates)
               for code in ("000300.SH", "000852.SH")}
    calendar = dates[-60:]
    closes, amounts, ages = {}, {}, {}
    for path in paths:
        bars = pd.concat([read_bars(path), pd.DataFrame([quote.loc[path.stem]], index=[dates[-1]])])
        closes[path.stem] = adjusted_close(bars, pd.DataFrame()).reindex(calendar)
        amounts[path.stem] = bars.amount.reindex(calendar)
        ages[path.stem] = pd.Series(np.arange(len(bars)) + 1, index=bars.index).reindex(calendar)
    expected = market_features(breadth(pd.DataFrame(closes), pd.DataFrame(amounts), pd.DataFrame(ages)), indices)
    with patch("trade.qvix.market.breadth", wraps=breadth) as calculate:
        actual, _ = load_features(settings, calendar, indices, quote)
        for received, part in zip(calculate.call_args.args, (closes, amounts, ages)):
            pd.testing.assert_frame_equal(received, pd.DataFrame(part), check_dtype=False)
    pd.testing.assert_frame_equal(actual, expected)
    with patch("trade.qvix.market.read_bars", side_effect=AssertionError("cache should avoid CSV reads")):
        cached, _ = load_features(settings, calendar, indices, quote)
    pd.testing.assert_frame_equal(cached, actual, check_exact=True)
    changed = read_bars(paths[0]).reset_index()
    changed.loc[len(changed) - 1, "close"] = 20.
    changed.to_csv(paths[0], index=False)
    with patch("trade.qvix.market.read_bars", wraps=read_bars) as reader:
        load_features(settings, calendar, indices, quote)
        assert reader.call_count == 3


def test_review_html_has_full_history_without_changing_rank_warmup(tmp_path):
    from trade.qvix.report import build_report
    from trade.qvix.store import Store
    from trade.qvix.config import INDEX_METHOD, PANIC_METHOD
    dates = pd.bdate_range("2016-01-01", "2020-01-15")
    bars = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100.}, index=dates)
    features = pd.DataFrame({name: np.arange(len(dates)) for name in FACTORS}, index=dates)
    features["data_valid"] = True
    with Store(tmp_path) as store:
        store.put("index:000300.SH", bars, INDEX_METHOD, "seed")
        store.put("panic", features, PANIC_METHOD, "seed")
    html = build_report(Settings(data_dir=tmp_path)).read_text()
    assert '"2020-01-01"' in html
    assert '"score": 100.0' in html
    assert 'id="date-start"' in html and 'id="event-list"' in html
    assert '"showticklabels":true' in html
    assert "{plot_id}" not in html
    assert 'id="pressure-modes"' in html
    assert '"pressure":"combined"' in html
    assert '综合默认不叠画' in html
    assert '低压参考 15 · 非卖出信号' in html
    assert '高压参考 85' in html
    assert 'id="heat-tools"' in html
    assert '"heat_index":"000300.SH"' in html
    assert '"xaxis5"' in html
    assert '"rangebreaks"' in html
    assert '"paper_bgcolor":"#ffffff"' in html
    assert '"plot_bgcolor":"#ffffff"' in html
    assert 'color-scheme:light' in html
    assert 'color-scheme:dark' not in html
    assert '红色阳线 / 绿色阴线' in html
    assert '金黄阳线' not in html
    assert (tmp_path / 'index-heat.csv').exists()


def test_panic_only_skips_option_requests(tmp_path):
    from trade.qvix.service import snapshot
    settings = Settings(data_dir=tmp_path, local_daily=tmp_path / "missing")
    with patch("trade.qvix.service.PublicSource.quotes", return_value=(pd.DataFrame(), [])), \
         patch("trade.qvix.service.PublicSource.qvix_snapshot", side_effect=AssertionError("must skip options")):
        result = snapshot(settings, panic_only=True)
    assert result["panic_only"] is True
    assert result["qvix"] == {}
