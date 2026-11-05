from unittest.mock import patch

import numpy as np
import pandas as pd

from trade.qvix.damage import DAMAGE_FEATURES, DAMAGE_SCALES, combine_pressure, damage_features, score_damage
from trade.qvix.damage_validation import validate_damage
from trade.qvix.config import INDEX_METHOD, PANIC_METHOD, Settings
from trade.qvix.store import Store


def test_fixed_scales_missing_and_no_short_dilution():
    frame = pd.DataFrame({key: [low, (low + high) / 2, high, high * 2, np.nan]
                          for key, (low, high) in DAMAGE_SCALES.items()})
    frame['damage_valid'] = True
    scored = score_damage(frame)
    np.testing.assert_allclose(scored.damage_score[:4], [0, 50, 100, 100])
    assert np.isnan(scored.damage_score.iloc[-1])
    short = pd.Series([95., 30., np.nan, 92., 99.])
    np.testing.assert_allclose(combine_pressure(short, scored.damage_score), [95, 50, np.nan, 100, np.nan], equal_nan=True)
    frame.loc[2, 'damage_valid'] = False
    assert np.isnan(score_damage(frame).damage_score.iloc[2])


def panels():
    dates = pd.bdate_range('2020-01-01', periods=650)
    prices = 100 * np.exp(-np.arange(650) / 800)
    closes = pd.DataFrame(np.repeat(prices[:, None], 1000, axis=1), index=dates)
    amounts = closes * 10
    ages = pd.DataFrame(np.repeat((np.arange(650) + 1)[:, None], 1000, axis=1), index=dates)
    return closes, amounts, ages


def test_causality_coverage_and_incremental_warmup():
    closes, amounts, ages = panels()
    full = damage_features(closes, amounts, ages)
    assert not full.damage_valid.iloc[:300].any()
    assert full.damage_valid.iloc[-1]
    prefix = damage_features(closes.iloc[:600], amounts.iloc[:600], ages.iloc[:600])
    pd.testing.assert_frame_equal(prefix, full.iloc[:600])
    tail = damage_features(closes.iloc[-330:], amounts.iloc[-330:], ages.iloc[-330:])
    pd.testing.assert_frame_equal(tail.iloc[-10:], full.iloc[-10:])
    amounts.iloc[-1, :151] = 0
    failed = damage_features(closes, amounts, ages)
    assert not failed.damage_valid.iloc[-1]
    assert np.isnan(score_damage(failed).damage_score.iloc[-1])


def test_exact_30pct_drawdown_and_listing_age():
    closes, amounts, ages = panels()
    closes[:] = 100.
    closes.iloc[-1] = 70.
    frame = damage_features(closes, amounts, ages)
    assert frame.deep_loss_share.iloc[-1] == 1
    assert np.isclose(frame.median_dd252.iloc[-1], .3)
    ages.iloc[-1] = 251
    assert damage_features(closes, amounts, ages).damage_count.iloc[-1] == 0


def test_backfill_separate_and_idempotent(tmp_path):
    from trade.qvix.service import backfill_damage
    dates = pd.bdate_range('2020-01-01', periods=5)
    settings = Settings(data_dir=tmp_path)
    original = pd.DataFrame({'below_ma20': .5}, index=dates)
    features = pd.DataFrame({name: 1. for name in DAMAGE_FEATURES}, index=dates)
    features['damage_valid'] = True
    with Store(tmp_path) as store:
        store.put('index:000300.SH', pd.DataFrame({'close': 100.}, index=dates), INDEX_METHOD, 'seed')
        store.put('panic', original, PANIC_METHOD, 'seed')
    with patch('trade.qvix.service.load_features', return_value=(features, [])):
        assert backfill_damage(settings)['changed'] == 5
        assert backfill_damage(settings)['changed'] == 0
    with Store(tmp_path) as store:
        expected = original.copy(); expected.index.name = 'date'
        pd.testing.assert_frame_equal(store.read('panic', PANIC_METHOD), expected, check_freq=False)
        assert not store.conn.execute("SELECT * FROM revisions WHERE series='panic'").fetchall()


def test_forward_diagnostic_uses_next_open_and_full_horizon():
    dates = pd.bdate_range('2020-01-01', periods=80)
    frame = pd.DataFrame({'score': 20., 'damage_score': 90., 'pressure_score': 90.}, index=dates)
    for key in DAMAGE_SCALES:
        frame[key] = .5
    bars = pd.DataFrame({'open': 100., 'close': 110., 'low': 80.}, index=dates)
    bars.iloc[0] = [1., 1., 1.]
    result = validate_damage(frame, {'000300.SH': bars})
    episode = result['long_episodes'][0]
    assert np.isclose(episode['000300.SH_forward60_return'], .1)
    assert np.isclose(episode['000300.SH_forward60_worst'], -.2)
    assert np.isnan(episode['000852.SH_forward60_return'])
    bars.iloc[20, 0] = np.nan
    result = validate_damage(frame, {'000300.SH': bars})
    assert np.isnan(result['long_episodes'][0]['000300.SH_forward60_return'])
