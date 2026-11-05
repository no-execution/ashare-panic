"""Audit actual paired closes before attempting a cross-method calibration.

Daily option OHLC cannot reconstruct historical bid/ask midpoints. This command
does not manufacture IV30 history or fit a mapping without observed overlap.
"""
import numpy as np
import pandas as pd

from .config import QVIX, QVIX_METHOD
from .options import method_id
from .service import finish, start_run
from .store import Store, dumps


def closed_values(frame, before):
    values = frame.close if "close" in frame else pd.Series(index=pd.DatetimeIndex([]), dtype=float)
    return values.loc[(values.index < before) & np.isfinite(values) & values.gt(0)]


def paired_closes(own, vendor, before):
    return pd.concat([closed_values(own, before).rename("iv30"),
                      closed_values(vendor, before).rename("qvix")], axis=1, sort=True).dropna().sort_index()


def history_summary(frame, before):
    values = closed_values(frame, before)
    return {"count": len(values), "first": values.index.min() if len(values) else None,
            "last": values.index.max() if len(values) else None}


def check_calibration(settings, *, offline=False):
    now, result, source = start_run(settings, "calibration-check")
    cutoff = pd.Timestamp(now.date())
    if (now.hour, now.minute) >= (15, 10):
        cutoff += pd.Timedelta(days=1)  # Allow today's closes only after the close buffer.
    result.update(calibrated=False, before=str(cutoff.date()), series={}, changed={})
    with Store(settings.data_dir) as store:
        if not offline:
            try:
                for name, frame in source.qvix_daily().items():
                    closed = frame.loc[frame.index < cutoff]
                    result["changed"][name] = store.put("qvix:" + name, closed, QVIX_METHOD, result["run_id"])
            except Exception as exc:
                result["errors"]["vendor_download"] = str(exc)
        for name in QVIX:
            own = store.read("iv30:" + name, method_id(settings.risk_free_rate))
            vendor = store.read("qvix:" + name, QVIX_METHOD)
            pairs = paired_closes(own, vendor, cutoff)
            result["series"][name] = {"method": method_id(settings.risk_free_rate),
                                      "own": history_summary(own, cutoff), "vendor": history_summary(vendor, cutoff),
                                      "paired_days": len(pairs),
                                      "status": "paired_data_available" if len(pairs) else "no_overlap"}
            # Preserve exact pairs for subsequent chronological validation, not model training by default.
            (source.raw_dir / f"{name}-pairs.json").write_text(
                dumps(pairs.reset_index(names="date").to_dict("records")), encoding="utf-8")
            if pairs.empty:
                result["errors"][name] = "没有同日自算IV30与供应商QVIX收盘配对，不能校准"
        result["warnings"].append("配对只使用独立收盘序列；盘中快照、单合约OHLC均不补入自算历史。")
        result["warnings"].append("有配对样本也不等于已校准，仍需按时间划分拟合/验证，并检查高波动区间覆盖。")
        result = finish(store, settings, result)
    (settings.data_dir / "calibration-check.json").write_text(dumps(result), encoding="utf-8")
    return result
