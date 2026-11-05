"""Read existing local bars + this tool's finalized quotes; never modify infra data."""
import hashlib
import json
import sqlite3
from zipfile import BadZipFile
import numpy as np
import pandas as pd

from .metrics import breadth, market_features

RENAME = {"日期": "date", "股票代码": "code", "开盘": "open", "收盘": "close",
          "最高": "high", "最低": "low", "成交额": "amount", "成交量": "volume"}


def stock_files(directory):
    prefixes = {"sh": ("600", "601", "603", "605", "688", "689"),
                "sz": ("000", "001", "002", "003", "300", "301")}
    return sorted(path for market, starts in prefixes.items()
                  for path in (directory / market).glob("*.csv") if path.stem.startswith(starts))


def read_bars(path):
    frame = pd.read_csv(path).rename(columns=RENAME)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("date")), name="date")
    if frame.index.has_duplicates:
        raise ValueError(f"Duplicate bar dates: {path}")
    return frame.sort_index()


def read_actions(path):
    if not path.exists():
        return {}, ["缺少权息数据库：MA/新低指标为未复权近似"]
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        frame = pd.read_sql_query("""
            SELECT s.code||'.'||m.market AS code,w.* FROM stkWeight w
            JOIN Stock s ON s.stockid=w.stockid JOIN Market m ON m.marketid=s.marketid
        """, conn)
    frame["date"] = pd.to_datetime(frame.date.astype(str), format="%Y%m%d")
    frame = frame[(frame.countAsGift != 0) | (frame.countForSell != 0) |
                  (frame.bonus != 0) | (frame.countOfIncreasement != 0) | (frame.suogu > 0)]
    last = str(frame.date.max().date()) if not frame.empty else "无记录"
    return {key: group for key, group in frame.groupby("code")}, [
        f"权息库最后事件日期 {last}；不代表数据已更新至今日，近期复权完整性未验证",
        "历史退市股票覆盖未验证；低覆盖日期不出评分（也可能包含真实大规模停牌）",
    ]


def adjusted_close(bars, actions):
    """Causal adjustment: only ex-date and later prices move. Official prevclose wins."""
    price = bars.close.to_numpy(dtype=float)
    events = np.ones(len(bars))
    references = bars.get("prevclose", pd.Series(np.nan, index=bars.index))
    if "back_adjust_factor" in bars:
        factors = pd.to_numeric(bars.back_adjust_factor, errors="coerce")
        known = factors.notna()
        if not np.isfinite(factors[known]).all() or not factors[known].gt(0).all():
            raise ValueError("Invalid BaoStock adjustment factor")
        filled = factors.ffill().fillna(1.)
        events = (filled / filled.shift(1).fillna(filled.iloc[0])).to_numpy(dtype=float, copy=True)
    for row in actions.itertuples():
        i = bars.index.searchsorted(row.date)
        if i == 0 or i >= len(bars) or pd.notna(references.iloc[i]):
            continue
        rights = row.countForSell / 100000
        shares = (1 + (row.countAsGift + row.countOfIncreasement) / 100000 + rights)
        shares *= row.suogu if row.suogu > 0 else 1
        reference = (price[i - 1] - row.bonus / 10000 + rights * row.priceForSell / 1000) / shares
        if reference > 0 and price[i - 1] > 0:
            events[i] *= price[i - 1] / reference
    for i in np.flatnonzero(references.notna().to_numpy()):
        if i > 0 and references.iloc[i] > 0 and price[i - 1] > 0:
            events[i] = price[i - 1] / references.iloc[i]
    if "back_adjust_factor" in bars:
        cumulative = 1.
        base = filled.iloc[0]
        for i in range(len(bars)):
            if known.iloc[i] and pd.isna(references.iloc[i]):
                # A quote-only row may have applied an event while its provider
                # factor was absent. Reconcile the next known cumulative factor
                # rather than applying that same event a second time.
                events[i] = factors.iloc[i] / (base * cumulative)
            cumulative *= events[i]
    return pd.Series(price * np.cumprod(events), index=bars.index)


def closing_quotes(data_dir, end):
    frames = []
    for path in sorted((data_dir / "closes").glob("*.csv.gz")):
        if path.name[:10] <= end.strftime("%Y-%m-%d"):
            frames.append(pd.read_csv(path, parse_dates=["date"]))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def save_closing_quotes(data_dir, quotes, date):
    directory = data_dir / "closes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{date:%Y-%m-%d}.csv.gz"
    frame = quotes.reset_index().assign(date=date.strftime("%Y-%m-%d"))
    if path.exists():
        frame = pd.concat([pd.read_csv(path), frame]).drop_duplicates("code", keep="last")
    temp = path.with_suffix(".tmp")
    frame.to_csv(temp, index=False, compression="gzip")
    temp.replace(path)


def _panel_signature(settings, paths, calendar):
    sources = paths + sorted((settings.data_dir / "closes").glob("*.csv.gz"))
    sources += [settings.actions_db, settings.actions_db.with_name(settings.actions_db.name + "-wal")]
    fingerprints = []
    for path in sources:
        stat = path.stat() if path.exists() else None
        fingerprints.append((str(path.resolve()), stat.st_mtime_ns if stat else None,
                             stat.st_size if stat else None))
    content = json.dumps(["panels-v1", fingerprints, calendar.astype(str).tolist()])
    return hashlib.sha256(content.encode()).hexdigest()


def _history_panels(settings, calendar, *, slot="daily"):
    """Cache only historical inputs. Live quotes never enter this cache."""
    paths = stock_files(settings.local_daily)
    if not paths:
        raise ValueError(f"No stock bars under {settings.local_daily}")
    cache = settings.data_dir / "cache" / f"panels-{slot}.npz"
    signature = _panel_signature(settings, paths, calendar) if len(calendar) <= 400 else None
    if signature and cache.exists():
        try:
            with np.load(cache, allow_pickle=False) as data:
                if str(data["signature"]) == signature:
                    columns = data["columns"].tolist()
                    panels = [pd.DataFrame(data[key], index=calendar, columns=columns)
                              for key in ("closes", "amounts", "ages")]
                    tail = pd.DataFrame(data["tail"], index=columns, columns=["close", "age"])
                    return *panels, tail, json.loads(str(data["warnings"]))
        except (OSError, ValueError, KeyError, EOFError, BadZipFile):
            pass  # A missing/corrupt cache is rebuilt from source inputs.
    extra = closing_quotes(settings.data_dir, calendar.max())
    extras = {code: group.set_index("date").drop(columns="code")
              for code, group in extra.groupby("code")} if not extra.empty else {}
    standalone = (settings.data_dir / "market/checkpoint.json").exists()
    if standalone:
        actions, warnings = {}, ["BaoStock后复权因子，按生效日使用；与原Hikyuu权息库口径可能存在差异",
                                 "历史退市股票覆盖未独立验证；源修订后可指定起点重算"]
    else:
        actions, warnings = read_actions(settings.actions_db)
    closes, amounts, ages, tails = {}, {}, {}, {}
    for path in paths:
        bars = read_bars(path).loc[:calendar.max()]
        addition = extras.get(path.stem)
        if addition is not None:
            bars = addition.combine_first(bars).sort_index()
        if bars.empty:
            continue
        if standalone and "back_adjust_factor" not in bars:
            raise ValueError(f"Missing standalone adjustment factors: {path.name}")
        # Count age before slicing the calculation window.
        ages[path.stem] = pd.Series(np.arange(len(bars)) + 1, index=bars.index).reindex(calendar)
        adjusted = adjusted_close(bars, actions.get(path.stem, pd.DataFrame()))
        closes[path.stem] = adjusted.reindex(calendar)
        amounts[path.stem] = bars.amount.reindex(calendar)
        tails[path.stem] = {"close": adjusted.iloc[-1], "age": len(bars)}
    panels = [pd.DataFrame(part, index=calendar) for part in (closes, amounts, ages)]
    tail = pd.DataFrame.from_dict(tails, orient="index").reindex(index=panels[0].columns, columns=["close", "age"])
    if signature:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".tmp.npz")
        np.savez(temporary, signature=signature, columns=np.asarray(panels[0].columns, dtype=str),
                 closes=panels[0].to_numpy(dtype=float), amounts=panels[1].to_numpy(dtype=float),
                 ages=panels[2].to_numpy(dtype=float), tail=tail[["close", "age"]].to_numpy(dtype=float),
                 warnings=json.dumps(warnings, ensure_ascii=False))
        temporary.replace(cache)
    return *panels, tail, warnings


def load_features(settings, calendar, indices, quotes=None, *, include_damage=False):
    """Preserve historical sessions and compute the fresh row using official prevclose."""
    live = quotes is not None and not quotes.empty
    history_calendar = calendar[:-1] if live else calendar
    closes, amounts, ages, tail, warnings = _history_panels(
        settings, history_calendar, slot="live" if live else "daily")
    if live:
        codes = closes.columns.union(quotes.index.intersection(
            [path.stem for path in stock_files(settings.local_daily)]))
        closes, amounts, ages = [frame.reindex(index=calendar, columns=codes)
                                for frame in (closes, amounts, ages)]
        current = quotes.reindex(codes)
        prior = tail.reindex(codes)
        # Equivalent to causal adjustment of an appended bar: the accumulated
        # historic scale is retained and today's official ex-rights reference wins.
        prices = prior.close * current.close / current.prevclose
        prices = prices.where(prior.age.notna(), current.close)
        closes.loc[calendar[-1]] = prices
        amounts.loc[calendar[-1]] = current.amount
        ages.loc[calendar[-1]] = (prior.age.fillna(0) + 1).where(current.close.notna())
    b = breadth(closes, amounts, ages)
    features = market_features(b, indices)
    if include_damage:
        from .damage import damage_features
        features = features.join(damage_features(closes, amounts, ages))
    return features, warnings


def local_index(directory, code):
    path = directory / code[-2:].lower() / f"{code}.csv"
    return read_bars(path).drop(columns=["code"], errors="ignore") if path.exists() else pd.DataFrame()
