import json
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from trade.qvix.config import Settings
from trade.qvix.download import (FIELDS, ProviderError, attach_factors, collect, download_market,
                                  eligible_universe, first_change, normalize_bars)
from trade.qvix.locking import writer_lock
from trade.qvix.market import adjusted_close


class Source:
    def __init__(self):
        self.dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06"])
        self.calls = []
        self.fail = set()
        self.missing_day = False
        self.multiplier = 2.

    def reconnect(self):
        pass

    def calendar(self, start, end):
        return pd.DataFrame({"calendar_date": self.dates.strftime("%Y-%m-%d"), "is_trading_day": "1"})

    def basic(self):
        return pd.DataFrame({"code": ["sh.600000", "sh.688001", "sz.300308", "bj.430017", "sh.000300", "sz.000001"],
                             "ipoDate": "2000-01-01", "outDate": ["", "", "", "", "", "2024-12-31"],
                             "type": ["1", "1", "1", "1", "2", "1"], "status": ["1"] * 5 + ["0"]})

    def bars(self, code, start, end):
        self.calls.append((code, start, end))
        if code in self.fail:
            raise ProviderError("offline")
        prices = [10., 5., 5.5]
        rows = [[str(date.date()), code, str(price), str(price), str(price), str(price), str(price), "100", "1000", "3", "1"]
                for date, price in zip(self.dates, prices) if start <= str(date.date()) <= end]
        if self.missing_day:
            rows = rows[1:]
        return pd.DataFrame(rows, columns=FIELDS.split(","))

    def factors(self, code, end):
        return pd.DataFrame({"code": [code], "dividOperateDate": ["2025-01-03"], "backAdjustFactor": [str(self.multiplier)]})


def run(tmp_path, source, **kwargs):
    return download_market(Settings(data_dir=tmp_path), source=source, attempts=1,
                           now="2025-01-07T10:00:00+08:00", **kwargs)


def test_no_original_repository_paths_and_data_dir_controls_all_defaults(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.local_daily == tmp_path / "market/daily"
    assert settings.actions_db == tmp_path / "market/actions.sqlite3"
    assert settings.rps_counts == tmp_path / "rps-history-source.csv"


def test_universe_includes_delisted_during_interval_but_excludes_indices_and_bj():
    frame = Source().basic()
    assert list(eligible_universe(frame, "2024-01-01", "2025-01-06").code) == ["sh.600000", "sh.688001", "sz.000001", "sz.300308"]
    assert len(eligible_universe(frame, "2025-01-01", "2025-01-06")) == 3


def test_download_from_empty_is_resumable_with_explicit_start(tmp_path):
    source = Source()
    source.fail.add("sh.688001")
    first = run(tmp_path, source, start="2025-01-01")
    assert not first["complete"] and first["downloaded"] == 2
    assert "sh.688001" in first["errors"]
    state = json.loads((tmp_path / "market/checkpoint.json").read_text())
    assert "sh.688001" not in state["stocks"] and state["recompute_start"] == "2025-01-02"
    source.fail.clear()
    source.calls.clear()
    second = run(tmp_path, source, start="2025-01-01")
    assert second["complete"] and second["downloaded"] == 1 and second["skipped"] == 2
    assert source.calls == [("sh.688001", "2025-01-01", "2025-01-06")]
    assert second["recompute_start"] == "2025-01-02"
    assert not (tmp_path / "qvix.sqlite3").exists()


def test_hash_mismatch_redownloads_and_unknown_symbol_is_rejected(tmp_path):
    source = Source()
    run(tmp_path, source, start="2025-01-01")
    path = tmp_path / "market/daily/sh/600000.SH.csv"
    path.write_text("bad file")
    # Corrupt files are fetched again rather than trusted via their checkpoint.
    result = run(tmp_path, source)
    assert result["complete"] and result["downloaded"] == 1 and result["skipped"] == 2
    with pytest.raises(ValueError, match="不在源股票池"):
        run(tmp_path, source, symbols=["600001.SH"])


def test_partial_interval_never_commits_success_or_fabricates_days(tmp_path):
    source = Source()
    source.missing_day = True
    result = run(tmp_path, source, start="2025-01-01")
    assert not result["complete"] and result["downloaded"] == 0
    assert all("缺少1个交易日" in e for e in result["errors"].values())
    assert not list((tmp_path / "market/daily").glob("*/*.csv"))


def test_factors_use_effective_date_without_future_fill_and_quotes_are_not_double_adjusted():
    source = Source()
    raw = normalize_bars(source.bars("sh.600000", "2025-01-01", "2025-01-06"), "sh.600000", "2025-01-01", "2025-01-06")
    bars = attach_factors(raw, source.factors("sh.600000", "2025-01-06"), "sh.600000", "2025-01-06").set_index("date")
    bars.index = pd.to_datetime(bars.index)
    np.testing.assert_allclose(bars.back_adjust_factor, [1, 2, 2])
    np.testing.assert_allclose(adjusted_close(bars, pd.DataFrame()), [10, 10, 11])
    prefix = bars.iloc[:1]
    assert adjusted_close(prefix, pd.DataFrame()).iloc[0] == 10
    extra = pd.DataFrame({"close": [3.], "prevclose": [2.75]}, index=pd.to_datetime(["2025-01-07"]))
    np.testing.assert_allclose(adjusted_close(pd.concat([bars, extra]), pd.DataFrame()), [10, 10, 11, 12])
    quote_only = bars.copy()
    quote_only.loc[quote_only.index[1], "back_adjust_factor"] = np.nan
    quote_only["prevclose"] = [np.nan, 5., np.nan]
    np.testing.assert_allclose(adjusted_close(quote_only, pd.DataFrame()), [10, 10, 11])


@pytest.mark.parametrize("value", ["0", "-1", "nan"])
def test_bad_factor_never_defaults_to_one(value):
    source = Source()
    raw = normalize_bars(source.bars("sh.600000", "2025-01-01", "2025-01-06"), "sh.600000", "2025-01-01", "2025-01-06")
    factors = source.factors("sh.600000", "2025-01-06").assign(backAdjustFactor=value)
    with pytest.raises(ValueError, match="复权因子无效"):
        attach_factors(raw, factors, "sh.600000", "2025-01-06")


def test_factor_revision_marks_earliest_affected_bar_and_preserves_prior_history(tmp_path):
    source = Source()
    run(tmp_path, source, start="2025-01-01")
    state_path = tmp_path / "market/checkpoint.json"
    state = json.loads(state_path.read_text())
    state.pop("recompute_start")
    state_path.write_text(json.dumps(state))
    source.multiplier = 2.1
    result = run(tmp_path, source, start="2025-01-03", refresh=True)
    assert result["complete"] and result["recompute_start"] == "2025-01-03"
    bars = pd.read_csv(tmp_path / "market/daily/sh/600000.SH.csv")
    assert len(bars) == 3 and bars.date.iloc[0] == "2025-01-02"
    assert first_change(bars, bars) is None


def test_default_update_only_refetches_tail_not_entire_history(tmp_path):
    source = Source()
    run(tmp_path, source, start="2009-01-01")
    source.dates = source.dates.append(pd.DatetimeIndex(["2025-02-03"]))
    # Verify the calculated request range even though this intentionally sparse
    # mock will fail the new-day coverage check.
    result = download_market(Settings(data_dir=tmp_path), source=source, attempts=1,
                             now="2025-02-04T10:00:00+08:00")
    assert source.calls[-1][1] == "2024-12-17"
    assert not result["complete"]


def test_writer_lock_prevents_two_writers_and_releases_after_interrupt(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with writer_lock(tmp_path):
            with pytest.raises(ValueError, match="写入锁"):
                with writer_lock(tmp_path):
                    pass
            raise KeyboardInterrupt
    assert not (tmp_path / ".writer.lock").exists()


def test_cli_incomplete_download_does_not_sync_or_build_report(tmp_path, capsys):
    from trade.qvix.__main__ import main
    with patch("trade.qvix.__main__.download_market", return_value={"complete": False, "errors": {"x": "offline"}}), \
         patch("trade.qvix.__main__.sync", side_effect=AssertionError("must not compute")):
        assert main(["--data-dir", str(tmp_path), "init-data"]) == 2
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_empty_source_cannot_pretend_to_be_a_successful_market_download(tmp_path):
    source = Source()
    source.basic = lambda: source.basic_original.iloc[:0]
    source.basic_original = Source().basic()
    with pytest.raises(ValueError, match="为空"):
        run(tmp_path, source, start="2025-01-01")


def test_download_cutoff_waits_until_1830_in_shanghai(tmp_path):
    source = Source()
    before = download_market(Settings(data_dir=tmp_path / "before"), source=source, attempts=1,
                             start="2025-01-01", now="2025-01-06T08:00:00+00:00")
    after = download_market(Settings(data_dir=tmp_path / "after"), source=source, attempts=1,
                            start="2025-01-01", now="2025-01-06T11:00:00+00:00")
    assert before["target_end"] == "2025-01-03" and before["complete"]
    assert after["target_end"] == "2025-01-06" and after["complete"]


def test_paginated_source_failure_cannot_return_partial_success():
    class Result:
        error_code = "0"
        error_msg = "page lost"
        fields = ["code"]
        position = 0

        def next(self):
            self.position += 1
            if self.position == 1:
                return True
            self.error_code = "offline"
            return False

        def get_row_data(self):
            return ["sh.600000"]

    with pytest.raises(ProviderError, match="page lost"):
        collect(Result())


def test_sdk_socket_eof_is_a_failure_instead_of_an_infinite_loop():
    from trade.qvix.download import _EOFSafeSocket
    class Socket:
        def recv(self, count):
            return b""
    with pytest.raises(ConnectionError, match="closed connection"):
        _EOFSafeSocket(Socket()).recv(100)
