"""Small live source check; downloads no bulk data and does not touch monitor storage."""
import json
import socket
import sys
import contextlib

import baostock as bs
import pandas as pd
import numpy as np

from trade.qvix.download import attach_factors
from trade.qvix.market import adjusted_close


def frame(result):
    if result.error_code != "0":
        raise RuntimeError(f"{result.error_code}: {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        raise RuntimeError(result.error_msg)
    return pd.DataFrame(rows, columns=result.fields)


def main():
    socket.setdefaulttimeout(20)
    with contextlib.redirect_stdout(sys.stderr):
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(login.error_msg)
        try:
            basic = frame(bs.query_stock_basic())
            summary = {"basic_rows": len(basic), "star_rows": int(basic.code.str.startswith("sh.688").sum()),
                       "delisted_rows": int(basic.status.eq("0").sum()), "samples": {}}
            for code in ("sh.600000", "sh.601899", "sh.688001", "sz.300308"):
                raw = frame(bs.query_history_k_data_plus(code, "date,code,open,high,low,close,preclose,volume,amount,pctChg,tradestatus", start_date="2025-01-01", end_date="2025-12-31", frequency="d", adjustflag="3"))
                factors = frame(bs.query_adjust_factor(code, start_date="1990-01-01", end_date="2025-12-31"))
                adjusted = frame(bs.query_history_k_data_plus(code, "date,close", start_date="2025-01-01", end_date="2025-12-31", frequency="d", adjustflag="1"))
                normalized = raw.copy()
                normalized["close"] = pd.to_numeric(normalized.close)
                normalized = normalized.rename(columns={"preclose": "source_preclose"})
                matched = attach_factors(normalized, factors, code, "2025-12-31").set_index("date")
                matched.index = pd.to_datetime(matched.index)
                direct = adjusted.set_index("date").close.astype(float)
                direct.index = pd.to_datetime(direct.index)
                reconstructed = adjusted_close(matched, pd.DataFrame()) * matched.back_adjust_factor.iloc[0]
                direct = direct.reindex(reconstructed.index)
                assert direct.notna().all() and len(direct) > 200
                error = float((reconstructed / direct - 1).abs().max())
                assert error < 1e-6, (code, error)
                summary["samples"][code] = {"raw_rows": len(raw), "factor_rows": len(factors),
                                             "last_date": raw.date.max(), "max_adjusted_price_relative_error": error}
        finally:
            bs.logout()
    from pathlib import Path
    destination = Path("data/provider-check.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
