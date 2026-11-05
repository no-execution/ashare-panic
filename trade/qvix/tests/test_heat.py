import numpy as np
import pandas as pd

from trade.qvix.heat import HEAT_FACTORS, index_heat
from trade.qvix.metrics import percentile


def test_heat_causal_ranks_and_two_equal_dimensions():
    dates = pd.bdate_range('2020-01-01', periods=1000)
    close = pd.Series(100 * np.exp(np.random.default_rng(31).normal(.0005, .01, len(dates)).cumsum()), index=dates)
    result = index_heat(close)
    pd.testing.assert_frame_equal(index_heat(close.iloc[:700]), result.iloc[:700])
    for key in HEAT_FACTORS:
        assert result['p_' + key].iloc[-1] == percentile(result[key].iloc[-1], result[key].iloc[:-1])["percentile"]
    expected = (result.extension + result.momentum) / 2
    np.testing.assert_allclose(result.heat_score, expected, equal_nan=True)
    assert result.heat_score.dropna().between(0, 100).all()


def test_heat_flat_declining_missing_and_insufficient_are_not_hot():
    dates = pd.bdate_range('2020-01-01', periods=500)
    close = pd.Series(100., index=dates)
    flat = index_heat(close)
    assert flat.heat_score.iloc[:311].isna().all()
    assert flat.heat_score.iloc[311:].eq(0).all()
    assert index_heat(pd.Series(np.linspace(200., 100., 500), index=dates)).heat_score.iloc[-1] == 0
    close.iloc[-1] = np.nan
    assert np.isnan(index_heat(close).heat_score.iloc[-1])
    close.iloc[-1] = 200
    close.iloc[-10] = np.nan
    assert np.isnan(index_heat(close).heat_score.iloc[-1])  # No missing MA closes filled.


def test_heat_positive_extreme_and_snapshot_dont_change_past():
    dates = pd.bdate_range('2020-01-01', periods=500)
    close = pd.Series(100., index=dates)
    before = index_heat(close.iloc[:-1])
    close.iloc[-1] = 150
    current = index_heat(close)
    pd.testing.assert_frame_equal(before, current.iloc[:-1])
    assert current.heat_score.iloc[-1] == 100
    assert current.bias20.iloc[-1] > 0


def test_report_heat_preserves_warmup_and_separates_snapshot(tmp_path):
    import json
    import re
    from trade.qvix.config import Settings, INDEX_METHOD
    from trade.qvix.report import build_report
    from trade.qvix.store import Store
    dates = pd.bdate_range('2020-01-01', periods=500)
    bars = pd.DataFrame({'open': 100., 'high': 100., 'low': 100., 'close': 100.}, index=dates)
    current = dates[-1] + pd.offsets.BDay()
    with Store(tmp_path) as store:
        store.put('index:000300.SH', bars, INDEX_METHOD, 'seed')
        store.save_run('snapshot-seed', 'snapshot', {'generated_at': current.isoformat(),
            'indices': {'000300.SH': {'timestamp': current.isoformat(), 'open': 100., 'high': 150., 'low': 100., 'close': 150.}}})
    html = build_report(Settings(data_dir=tmp_path), start=str(dates[-10].date())).read_text()
    payload = json.loads(re.search(r'<script type="application/json" id="review-data">(.*?)</script>', html).group(1))
    heat = payload['heat']['000300.SH']
    assert heat[str(dates[-1].date())]['heat_score'] == 0
    assert heat[str(current.date())]['heat_score'] == 100
    assert heat[str(current.date())]['snapshot'] is True
    exported = pd.read_csv(tmp_path / 'index-heat.csv')
    assert len(exported) == 500  # No snapshot or display-range truncation in daily export.
