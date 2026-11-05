"""Download user-local market inputs. No dependency on the source quant repository.

BaoStock requests are sequential: its SDK shares a global socket. Each stock is
committed separately; a checkpoint is advanced only after bars AND factors pass
validation. Failed runs can be retried without discarding completed stocks.
"""
import contextlib
import hashlib
import json
import socket
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .sources import now_shanghai, valid_ohlc
from .store import dumps

DEFAULT_START = "2009-01-01"
PREFIXES = {"sh": ("600", "601", "603", "605", "688", "689"),
            "sz": ("000", "001", "002", "003", "300", "301")}
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus"


class ProviderError(ValueError):
    pass


def collect(result):
    """Avoid SDK get_data(): older versions use removed pandas.DataFrame.append."""
    if result is None or not hasattr(result, "error_code"):
        raise ProviderError("BaoStock未返回有效结果")
    if result.error_code != "0":
        raise ProviderError(f"BaoStock {result.error_code}: {result.error_msg}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    if result.error_code != "0":
        raise ProviderError(f"BaoStock page {result.error_code}: {result.error_msg}")
    return pd.DataFrame(rows, columns=result.fields)


class BaoStockSource:
    def __init__(self, timeout=20):
        import baostock
        self.bs = baostock
        self.timeout = timeout

    def __enter__(self):
        self.old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout)
        try:
            self.reconnect()
        except BaseException:
            socket.setdefaulttimeout(self.old_timeout)
            raise
        return self

    def __exit__(self, *args):
        try:
            with contextlib.redirect_stdout(sys.stderr):
                self.bs.logout()
        finally:
            socket.setdefaulttimeout(self.old_timeout)

    def reconnect(self):
        with contextlib.redirect_stdout(sys.stderr):
            self.bs.logout()
            result = self.bs.login()
        if result.error_code != "0":
            raise ProviderError(f"BaoStock login: {result.error_msg}")
        # SDK 0.9.x loops forever on socket EOF; turn an empty recv into an
        # exception, which its error path converts into a failed request.
        import baostock.common.context as context
        context.default_socket = _EOFSafeSocket(context.default_socket)

    def read(self, query):
        with contextlib.redirect_stdout(sys.stderr):
            return collect(query())

    def basic(self):
        return self.read(self.bs.query_stock_basic)

    def calendar(self, start, end):
        return self.read(lambda: self.bs.query_trade_dates(start_date=start, end_date=end))

    def bars(self, code, start, end):
        return self.read(lambda: self.bs.query_history_k_data_plus(code, FIELDS, start_date=start,
                                                        end_date=end, frequency="d", adjustflag="3"))

    def factors(self, code, end):
        # Full factor list is small. Include events before the requested price
        # range so the starting multiplier never borrows a future event.
        return self.read(lambda: self.bs.query_adjust_factor(code, start_date="1990-01-01", end_date=end))


class _EOFSafeSocket:
    def __init__(self, socket):
        self.socket = socket

    def __getattr__(self, name):
        return getattr(self.socket, name)

    def recv(self, count):
        data = self.socket.recv(count)
        if not data:
            raise ConnectionError("BaoStock server closed connection")
        return data


def retry(call, source, attempts):
    for attempt in range(attempts):
        try:
            return call()
        except (OSError, ValueError, EOFError) as exc:
            if attempt + 1 == attempts:
                raise ProviderError(str(exc)) from exc
            print(f"源请求失败，重试 {attempt + 2}/{attempts}: {str(exc)[:160]}", file=sys.stderr, flush=True)
            time.sleep(min(2 ** attempt, 4))
            source.reconnect()


def eligible_universe(frame, start, end):
    required = {"code", "ipoDate", "outDate", "type", "status"}
    if not required.issubset(frame):
        raise ProviderError("证券列表字段缺失")
    if frame.empty:
        raise ProviderError("证券列表为空")
    frame = frame.copy().fillna("")
    selected = frame.code.map(lambda c: any(c.startswith(m + "." + p) for m, ps in PREFIXES.items() for p in ps))
    selected &= frame.type.eq("1") & frame.ipoDate.ne("") & frame.ipoDate.le(end)
    selected &= frame.outDate.eq("") | frame.outDate.ge(start)
    result = frame.loc[selected].sort_values("code")
    if result.empty or result.code.duplicated().any():
        raise ProviderError("证券列表为空或代码重复")
    return result


def normalize_bars(frame, code, start, end):
    if not set(FIELDS.split(",")).issubset(frame):
        raise ProviderError(f"{code}: 日线字段缺失")
    if frame.empty:
        return pd.DataFrame(columns=["date", "code", "open", "high", "low", "close", "source_preclose", "volume", "amount", "tradestatus"])
    frame = frame.copy()
    if not frame.code.eq(code).all() or not frame.adjustflag.eq("3").all():
        raise ProviderError(f"{code}: 源代码/复权类型不匹配")
    dates = pd.to_datetime(frame.date, errors="coerce")
    if dates.isna().any() or dates.duplicated().any() or not dates.between(pd.Timestamp(start), pd.Timestamp(end)).all():
        raise ProviderError(f"{code}: 日期无效、重复或越界")
    for name in ("open", "high", "low", "close", "preclose", "volume", "amount"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    normal = frame.tradestatus.eq("1")
    if not frame.tradestatus.isin(["0", "1"]).all() or not valid_ohlc(frame.loc[normal]).all():
        raise ProviderError(f"{code}: 交易状态或OHLC无效")
    if not np.isfinite(frame[["close", "volume", "amount"]]).all().all() or (frame[["volume", "amount"]] < 0).any().any():
        raise ProviderError(f"{code}: 成交量/额无效")
    # Suspended observations are retained but must never count as traded stocks.
    frame.loc[~normal, "amount"] = 0.
    frame["date"] = dates.dt.strftime("%Y-%m-%d")
    frame["code"] = code[3:] + "." + code[:2].upper()
    return frame.drop(columns="adjustflag").rename(columns={"preclose": "source_preclose"}).sort_values("date")


def attach_factors(bars, factors, code, end):
    required = {"code", "dividOperateDate", "backAdjustFactor"}
    if not required.issubset(factors):
        raise ProviderError(f"{code}: 复权因子字段缺失")
    factors = factors.copy()
    factors["dividOperateDate"] = pd.to_datetime(factors.dividOperateDate, errors="coerce")
    factors["backAdjustFactor"] = pd.to_numeric(factors.backAdjustFactor, errors="coerce")
    if (not factors.code.eq(code).all() or factors.dividOperateDate.isna().any()
            or factors.dividOperateDate.duplicated().any() or factors.dividOperateDate.gt(pd.Timestamp(end)).any()
            or not np.isfinite(factors.backAdjustFactor).all() or not factors.backAdjustFactor.gt(0).all()):
        raise ProviderError(f"{code}: 复权因子无效或日期越界")
    result = bars.drop(columns="back_adjust_factor", errors="ignore").sort_values("date").copy()
    result["date"] = pd.to_datetime(result.date)
    if len(factors) and len(result):
        result = pd.merge_asof(result, factors[["dividOperateDate", "backAdjustFactor"]].sort_values("dividOperateDate"),
                               left_on="date", right_on="dividOperateDate", direction="backward")
        result["back_adjust_factor"] = result.pop("backAdjustFactor").fillna(1.)
        result = result.drop(columns="dividOperateDate")
    else:
        result["back_adjust_factor"] = 1.
    result["date"] = result.date.dt.strftime("%Y-%m-%d")
    return result


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dumps(value), encoding="utf-8")
    temporary.replace(path)


def atomic_csv(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def first_change(existing, combined):
    """Earliest changed input, including historical factor revisions."""
    if existing.empty:
        return combined.date.min()
    columns = [c for c in ("open", "high", "low", "close", "amount", "back_adjust_factor") if c in combined]
    left = existing.set_index("date").reindex(index=combined.date, columns=columns).apply(pd.to_numeric, errors="coerce")
    right = combined.set_index("date")[columns].apply(pd.to_numeric, errors="coerce")
    same = np.isclose(left.to_numpy(), right.to_numpy(), rtol=1e-12, atol=1e-12, equal_nan=True).all(axis=1)
    return combined.loc[~same, "date"].min() if (~same).any() else None


def download_market(settings, *, start=None, end=None, symbols=None, attempts=3, refresh=False, source=None, now=None):
    if attempts < 1 or attempts > 10:
        raise ValueError("retries 必须在1至10之间")
    stamp = pd.Timestamp(now if now is not None else now_shanghai())
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("Asia/Shanghai").tz_localize(None)
    # BaoStock normally publishes after market close; never request a forming bar.
    cutoff = stamp.normalize() - (pd.Timedelta(days=1) if (stamp.hour, stamp.minute) < (18, 30) else pd.Timedelta(0))
    end = min(pd.Timestamp(end), cutoff) if end else cutoff
    market_dir = settings.data_dir / "market"
    checkpoint_path = market_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {"provider": "baostock", "stocks": {}}
    beginning = str(pd.Timestamp(start or checkpoint.get("history_start", DEFAULT_START)).date())
    if beginning > str(end.date()):
        raise ValueError("start 晚于可请求的已收盘日期")
    checkpoint["history_start"] = min(beginning, checkpoint.get("history_start", beginning))
    result = {"kind": "download", "provider": "baostock", "started_at": now_shanghai().isoformat(),
              "history_start": beginning, "requested_end": str(end.date()), "sample_mode": symbols is not None,
              "downloaded": 0, "skipped": 0, "errors": {}, "warnings": ["证券列表和复权因子可能被上游修订；退市历史完整性未独立验证"]}
    if checkpoint.get("recompute_start"):
        result["recompute_start"] = checkpoint["recompute_start"]
    with (contextlib.nullcontext(source) if source is not None else BaoStockSource(settings.timeout)) as provider:
        calendar = retry(lambda: provider.calendar(beginning, str(end.date())), provider, attempts)
        if not {"calendar_date", "is_trading_day"}.issubset(calendar):
            raise ProviderError("交易日历字段缺失")
        sessions = pd.to_datetime(calendar.loc[calendar.is_trading_day.eq("1"), "calendar_date"], errors="raise")
        if sessions.isna().any() or sessions.duplicated().any():
            raise ProviderError("交易日历包含空日期或重复日期")
        sessions = sessions[(sessions <= end) & (sessions >= pd.Timestamp(beginning))].sort_values()
        if sessions.empty:
            raise ProviderError("源未返回近期交易日历")
        target = str(sessions.max().date())
        atomic_csv(market_dir / "calendar.csv", calendar)
        result["target_end"] = target
        universe = eligible_universe(retry(provider.basic, provider, attempts), beginning, target)
        # Always preserve the complete discovered universe, even in a small smoke run.
        atomic_csv(market_dir / "universe.csv", universe)
        if symbols is not None:
            wanted = {s.lower() if "." in s and s[:2].lower() in PREFIXES else s[-2:].lower() + "." + s[:6] for s in symbols}
            unknown = wanted - set(universe.code)
            if unknown:
                raise ValueError(f"不在源股票池中：{sorted(unknown)}")
            universe = universe.loc[universe.code.isin(wanted)]
        result["stock_count"] = len(universe)
        failures = 0
        for number, row in enumerate(universe.itertuples(index=False), 1):
            code = row.code
            local_code = code[3:] + "." + code[:2].upper()
            path = settings.local_daily / code[:2] / f"{local_code}.csv"
            factor_path = market_dir / "factors" / f"{local_code}.csv"
            old = checkpoint["stocks"].get(code, {})
            expected = min(target, row.outDate) if row.outDate else target
            try:
                if (not refresh and old.get("checked_end", "") >= target and old.get("start", "9999") <= beginning
                        and path.exists() and factor_path.exists()
                        and old.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest()):
                    result["skipped"] += 1
                    continue
                existing = pd.DataFrame()
                if path.exists():
                    try:
                        existing = pd.read_csv(path, dtype={"date": str, "code": str})
                        if not {"date", "code", "close", "amount", "back_adjust_factor"}.issubset(existing) or existing.date.duplicated().any():
                            existing = pd.DataFrame()
                    except (ValueError, OSError):
                        pass  # Re-download corrupt caches; never skip them.
                if len(existing) and existing.date.max() > target:
                    raise ProviderError("本目录已有更晚数据；历史日期下载请使用独立 --data-dir")
                # With no matching checkpoint, redownload rather than trust an unrelated file.
                left = beginning
                if len(existing) and old.get("start", "9999") <= beginning and (not refresh or start is None):
                    left = max(beginning, str((pd.Timestamp(existing.date.max()) - pd.Timedelta(days=20)).date()))
                fresh = normalize_bars(retry(lambda: provider.bars(code, left, target), provider, attempts), code, left, target)
                expected_sessions = sessions[(sessions >= pd.Timestamp(max(left, row.ipoDate)))
                                             & (sessions <= pd.Timestamp(expected))]
                missing = set(expected_sessions.dt.strftime("%Y-%m-%d")) - set(fresh.date)
                if missing:
                    raise ProviderError(f"{code}: 请求区间缺少{len(missing)}个交易日，例如{sorted(missing)[:3]}")
                combined = pd.concat([existing, fresh], ignore_index=True) if len(existing) else fresh
                combined = combined.drop_duplicates("date", keep="last").sort_values("date")
                if combined.empty or (not row.outDate and combined.date.max() < target):
                    raise ProviderError(f"{code}: 日线未覆盖目标交易日 {expected}")
                factors = retry(lambda: provider.factors(code, target), provider, attempts)
                combined = attach_factors(combined, factors, code, target)
                changed = first_change(existing, combined)
                if changed is not None:
                    result["recompute_start"] = min(changed, result.get("recompute_start", changed))
                    checkpoint["recompute_start"] = result["recompute_start"]
                # Factors first; a checkpoint is committed only after the matching bars.
                atomic_csv(factor_path, factors)
                atomic_csv(path, combined)
                checkpoint["stocks"][code] = {"start": min(beginning, old.get("start", beginning)), "checked_end": target,
                    "last_bar": combined.date.max(), "rows": len(combined), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "updated_at": now_shanghai().isoformat()}
                atomic_json(checkpoint_path, checkpoint)
                result["downloaded"] += 1
                failures = 0
            except (OSError, ValueError) as exc:
                result["errors"][code] = str(exc)
                failures += 1
                print(f"{code}: {str(exc)[:180]}", file=sys.stderr, flush=True)
                if failures >= 5:
                    result["errors"]["aborted"] = "连续5只股票失败，停止请求；重新运行可续传"
                    break
            finally:
                if number % 25 == 0 or number == len(universe):
                    print(f"股票进度 {number}/{len(universe)} 下载={result['downloaded']} 跳过={result['skipped']} 失败={len(result['errors'])}", file=sys.stderr, flush=True)
        result["generated_at"] = now_shanghai().isoformat()
        result["complete"] = not result["errors"] and result["downloaded"] + result["skipped"] == len(universe)
        if symbols is not None:
            result["warnings"].append("指定股票仅用于网络诊断；不能代表全市场，也不会自动计算恐慌评分")
        atomic_json(market_dir / "download-latest.json", result)
    return result
