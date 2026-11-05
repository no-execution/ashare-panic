"""Offline end-to-end acceptance with synthetic data, never investment input.

Run from the repository root: python -m scripts.check_pipeline
Creates data/pipeline-check only. Exercises the real init/update CLI, downloader,
1000-stock breadth, historical percentiles, damage, SQLite and HTML generation.
"""
import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from trade.qvix.__main__ import main
from trade.qvix.config import Settings, PANIC_METHOD
from trade.qvix.download import FIELDS
from trade.qvix.metrics import score_history
from trade.qvix.store import Store


class SyntheticSource:
    dates = pd.bdate_range("2023-01-02", periods=420)
    codes = [f"sh.{600000 + i:06}" for i in range(1000)]

    def basic(self):
        return pd.DataFrame({"code": self.codes, "ipoDate": "2000-01-01", "outDate": "", "type": "1", "status": "1"})

    def calendar(self, start, end):
        return pd.DataFrame({"calendar_date": self.dates.strftime("%Y-%m-%d"), "is_trading_day": "1"})

    def bars(self, code, start, end):
        price = 10 * np.exp(np.sin(np.arange(len(self.dates)) / 15) * .1 + np.arange(len(self.dates)) / 2000)
        frame = pd.DataFrame({"date": self.dates.strftime("%Y-%m-%d"), "code": code,
            "open": price, "high": price + .1, "low": price - .1, "close": price,
            "preclose": np.r_[price[0], price[:-1]], "volume": 10000, "amount": 100000,
            "adjustflag": "3", "tradestatus": "1"})
        return frame.loc[frame.date.between(start, end), FIELDS.split(",")].astype(str)

    def factors(self, code, end):
        return pd.DataFrame(columns=["code", "dividOperateDate", "backAdjustFactor"])


def check():
    directory = Path("data/pipeline-check").resolve()
    source = SyntheticSource()
    end = str(source.dates[-1].date())
    bars = source.bars(source.codes[0], "2009-01-01", end).set_index("date")[["open", "high", "low", "close"]].astype(float)
    bars.index = pd.to_datetime(bars.index)
    with patch("trade.qvix.download.BaoStockSource", return_value=contextlib.nullcontext(source)), \
         patch("trade.qvix.sources.PublicSource.index_daily", return_value=bars), \
         patch("trade.qvix.sources.PublicSource.qvix_daily", return_value={}):
        init_exit = main(["--data-dir", str(directory), "init-data", "--end", end])
        update_exit = main(["--data-dir", str(directory), "update", "--end", end])
    with Store(directory) as store:
        scored = score_history(store.read("panic", PANIC_METHOD))
        latest = store.latest_run("sync")
    assert init_exit == update_exit == 2  # Optional QVIX deliberately absent.
    assert np.isfinite(scored.score.iloc[-1])
    assert latest["panic"]["count"] == 1000 and latest["damage"]["damage_valid"]
    assert latest["changed"]["panic"] == 0
    assert not any(k in latest["errors"] for k in ("panic", "damage"))
    assert (directory / "report.html").exists()
    assert "recompute_start" not in json.loads((directory / "market/checkpoint.json").read_text())
    result = {"synthetic": True, "stocks": 1000, "sessions": len(source.dates), "panic_score": scored.score.iloc[-1],
              "damage_valid": True, "idempotent_update": True, "optional_qvix_missing": True,
              "report_generated": True}
    (directory / "acceptance.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    check()
