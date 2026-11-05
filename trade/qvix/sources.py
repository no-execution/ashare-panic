"""Public HTTP adapters. Archive responses; reject ambiguous dates and bad prices."""
import hashlib
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from .config import QVIX
from .store import dumps

SHANGHAI = ZoneInfo("Asia/Shanghai")
OPTBBS = "http://1.optbbs.com/d/csv/d/"
OHLC = ["open", "high", "low", "close"]


def now_shanghai():
    return datetime.now(SHANGHAI)


def valid_ohlc(frame):
    p = frame[OHLC].apply(pd.to_numeric, errors="coerce")
    valid = (np.isfinite(p).all(axis=1) & p.gt(0).all(axis=1)
             & p.high.ge(p[["open", "close", "low"]].max(axis=1))
             & p.low.le(p[["open", "close", "high"]].min(axis=1)))
    return valid


def parse_qvix_daily(content):
    raw = pd.read_csv(io.BytesIO(content), encoding="gbk")
    if len(raw.columns) < 71 or str(raw.columns[1]) != "2" or str(raw.columns[70]) != "71":
        raise ValueError("OptBBS CSV schema changed; inspect the archived response")
    dates = pd.to_datetime(raw.iloc[:, 0], errors="coerce")
    result = {}
    for name, (_, first, _) in QVIX.items():
        frame = raw.iloc[:, first:first + 4].copy()
        frame.columns = OHLC
        frame = frame.apply(pd.to_numeric, errors="coerce")
        frame.index = pd.DatetimeIndex(dates, name="date")
        frame = frame.loc[frame.index.notna()]
        if frame.index.has_duplicates:
            raise ValueError(f"Duplicate QVIX dates: {name}")
        frame.loc[~valid_ohlc(frame), OHLC] = np.nan
        result[name] = frame.sort_index()
    return result


def parse_qvix_minute(content, now, max_age):
    """A bare HH:MM CSV cannot establish its trading date. Never infer it from HTTP Date."""
    raw = pd.read_csv(io.BytesIO(content))
    if raw.shape[1] < 2:
        raise ValueError("QVIX minute CSV requires timestamp and value")
    times = raw.iloc[:, 0].astype(str)
    if not times.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]").all():
        return {"status": "unverified_date", "value": None,
                "reason": "源仅提供时分秒，无法确认交易日期"}
    stamps = pd.to_datetime(times, errors="coerce")
    if stamps.isna().any():
        raise ValueError("Invalid QVIX timestamp")
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize(SHANGHAI)
    row = pd.DataFrame({"timestamp": stamps, "value": pd.to_numeric(raw.iloc[:, 1], errors="coerce")})
    last = row.sort_values("timestamp").iloc[-1]
    stamp, value = last.timestamp, last.value
    if not quote_is_fresh(stamp, now, max_age):
        return {"status": "stale", "value": None, "quote_time": stamp.isoformat()}
    if not np.isfinite(value) or value <= 0:
        return {"status": "invalid", "value": None, "quote_time": stamp.isoformat()}
    return {"status": "ok", "value": float(value), "quote_time": stamp.isoformat()}


def quote_is_fresh(stamp, now, max_age):
    stamp, now = pd.Timestamp(stamp), pd.Timestamp(now)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("now must have timezone")
    if stamp.date() != now.date() or stamp > now + pd.Timedelta(seconds=30):
        return False
    # Lunch and after-close quotes are evaluated against the last trading minute.
    clock = now.hour * 60 + now.minute
    if clock < 9 * 60 + 30:
        return False
    reference = now
    if 11 * 60 + 30 <= clock < 13 * 60:
        reference = now.normalize() + pd.Timedelta(hours=11, minutes=30)
    elif clock >= 15 * 60:
        reference = now.normalize() + pd.Timedelta(hours=15)
    return stamp >= reference - pd.Timedelta(seconds=max_age)


def parse_quotes(content):
    rows = []
    for symbol, value in re.findall(r'v_([a-z]{2}\d{6})="([^"]*)";', content.decode("gb18030", errors="replace")):
        fields = value.split("~")
        if len(fields) < 36:
            continue
        try:
            stamp = pd.to_datetime(fields[30], format="%Y%m%d%H%M%S").tz_localize(SHANGHAI)
            rows.append({
                "code": symbol[2:] + "." + symbol[:2].upper(), "timestamp": stamp,
                "open": float(fields[5]), "close": float(fields[3]), "prevclose": float(fields[4]),
                "high": float(fields[33]), "low": float(fields[34]), "volume": float(fields[6]),
                # The compound field uses yuan (unlike fields 37/57, which use 万元).
                "amount": float(fields[35].split("/")[2]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return pd.DataFrame(rows).set_index("code") if rows else pd.DataFrame()


class PublicSource:
    def __init__(self, raw_dir, timeout=12):
        self.raw_dir = raw_dir
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def get(self, name, url, params=None):
        metadata = {"url": url, "params": params, "retrieved_at": now_shanghai().isoformat()}
        try:
            referer = "https://stock.finance.sina.com.cn/" if ".sina" in url else "https://gu.qq.com/"
            response = requests.get(url, params=params, timeout=self.timeout,
                                    headers={"User-Agent": "Mozilla/5.0", "Referer": referer, "Cache-Control": "no-cache"})
            metadata["status_code"] = response.status_code
            metadata["sha256"] = hashlib.sha256(response.content).hexdigest()
            (self.raw_dir / name).write_bytes(response.content)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            metadata["error"] = str(exc)
            raise
        finally:
            (self.raw_dir / (name + ".json")).write_text(dumps(metadata), encoding="utf-8")

    def qvix_daily(self):
        return parse_qvix_daily(self.get("qvix.csv", OPTBBS + "k.csv"))

    def qvix_snapshot(self, now, max_age, rate=.015):
        def fetch(item):
            name, (_, _, filename) = item
            try:
                result = parse_qvix_minute(self.get(filename, OPTBBS + filename), now_shanghai(), max_age)
            except (requests.RequestException, ValueError, pd.errors.ParserError) as exc:
                result = {"status": "unavailable", "value": None, "reason": str(exc)}
            return name, result
        with ThreadPoolExecutor(max_workers=4) as pool:
            result = dict(pool.map(fetch, QVIX.items()))
        missing = [name for name, item in result.items() if item["status"] != "ok"]
        if missing:
            from .options import OptionSource
            for name, item in OptionSource(self, rate, max_age).snapshots(missing).items():
                item["vendor_status"] = result[name]
                result[name] = item
        return result

    def quotes(self, codes):
        codes = sorted(set(codes))
        batches = [codes[i:i + 80] for i in range(0, len(codes), 80)]

        def fetch(item):
            number, batch = item
            symbols = [code[-2:].lower() + code[:6] for code in batch]
            try:
                content = self.get(f"quotes-{number:03}.txt", "https://qt.gtimg.cn/q=" + ",".join(symbols))
                return parse_quotes(content), None
            except (requests.RequestException, ValueError) as exc:
                return pd.DataFrame(), str(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            parts = list(pool.map(fetch, enumerate(batches)))
        frames = [frame for frame, _ in parts if not frame.empty]
        quotes = pd.concat(frames) if frames else pd.DataFrame()
        return quotes, [error for _, error in parts if error]

    def index_daily(self, code, start, end):
        # Tencent limits one response. Page by year so long gaps cannot be silently truncated.
        frames = []
        symbol = code[-2:].lower() + code[:6]
        for year in range(start.year, end.year + 1):
            left = max(start, pd.Timestamp(year, 1, 1))
            right = min(end, pd.Timestamp(year, 12, 31))
            content = self.get(f"index-{code}-{year}.json",
                               "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                               {"param": f"{symbol},day,{left:%Y-%m-%d},{right:%Y-%m-%d},400,bfq"})
            rows = json.loads(content).get("data", {}).get(symbol, {}).get("day", [])
            if rows:
                frame = pd.DataFrame([row[:6] for row in rows],
                                     columns=["date", "open", "close", "high", "low", "volume"])
                frame.index = pd.to_datetime(frame.pop("date"))
                frame = frame.apply(pd.to_numeric, errors="coerce")
                if not valid_ohlc(frame).all():
                    raise ValueError(f"Invalid index OHLC: {code}")
                frames.append(frame.loc[left:right])
        return pd.concat(frames).sort_index() if frames else pd.DataFrame()
