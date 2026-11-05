"""Application workflows. Providers fail independently; snapshots never write daily rows."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from .config import FACTORS, INDICES, QVIX, INDEX_METHOD, PANIC_METHOD, QVIX_METHOD
from .damage import DAMAGE_METHOD, DAMAGE_FEATURES, DAMAGE_WARMUP, score_damage
from .market import load_features, local_index, save_closing_quotes, stock_files
from .metrics import percentile, qvix_context, score_history, score_snapshot
from .options import OptionSource, method_id
from .sources import PublicSource, now_shanghai, parse_qvix_daily, quote_is_fresh, valid_ohlc
from .store import Store, clean, dumps


def merge(old, new):
    if new.empty:
        return old
    combined = pd.concat([old, new]) if not old.empty else new.copy()
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def start_run(settings, kind):
    now = now_shanghai()
    run_id = now.strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:6]
    result = {"run_id": run_id, "kind": kind, "started_at": now.isoformat(), "generated_at": now.isoformat(),
              "window": settings.window, "min_history": settings.min_history,
              "errors": {}, "warnings": []}
    source = PublicSource(settings.data_dir / "raw" / run_id, settings.timeout)
    return now, result, source


def finish(store, settings, result):
    result["generated_at"] = now_shanghai().isoformat()
    result = clean(result)
    store.save_run(result["run_id"], result["kind"], result)
    path = settings.data_dir / "raw" / result["run_id"] / "result.json"
    path.write_text(dumps(result), encoding="utf-8")
    return result


def index_history(store):
    return {code: store.read("index:" + code, INDEX_METHOD) for code in INDICES}


def calendar_of(indices):
    # A-share index observations define actual sessions; never synthesize weekdays.
    dates = pd.DatetimeIndex([])
    for code in ("000001.SH", "000300.SH"):
        dates = dates.union(indices[code].index)
    return dates.sort_values()


def sync(settings, *, start=None, end=None, offline=False, qvix_csv=None):
    now, result, source = start_run(settings, "sync")
    today = pd.Timestamp(now.date())
    last_closed = today if (now.hour, now.minute) >= (15, 10) else today - pd.Timedelta(days=1)
    end = min(pd.Timestamp(end), last_closed) if end else last_closed
    start = pd.Timestamp(start) if start else None
    if start is not None and start > end:
        raise ValueError("start is after the last closed date")
    result["requested_end"] = str(end.date())
    result["changed"] = {}
    with Store(settings.data_dir) as store:
        indices = index_history(store)
        for code in INDICES:
            try:
                previous = indices[code]
                local = local_index(settings.local_daily, code).loc[:end]
                # Previously fetched remote history takes precedence over older local files.
                bars = merge(local, previous).loc[:end]
                if not offline:
                    left = start if start is not None else (
                        bars.index[max(0, len(bars) - 10)] if len(bars) else pd.Timestamp("2009-01-01"))
                    try:
                        remote = source.index_daily(code, left, end)
                        if remote.empty:
                            result["errors"][code] = "接口未返回日线，保留已有历史"
                        bars = merge(bars, remote)
                    except Exception as exc:
                        result["errors"][code] = str(exc)
                if bars.empty:
                    raise ValueError("No index history")
                bars = bars.loc["2009-01-01":end]
                if not valid_ohlc(bars).all():
                    raise ValueError("Invalid index OHLC")
                result["changed"][code] = store.put("index:" + code, bars, INDEX_METHOD, result["run_id"])
                indices[code] = bars
            except Exception as exc:
                result["errors"][code] = str(exc)
        calendar = calendar_of(indices)
        calendar = calendar[calendar <= end]

        try:
            if qvix_csv:
                content = Path(qvix_csv).read_bytes()
                (source.raw_dir / "qvix-import.csv").write_bytes(content)
                qvix = parse_qvix_daily(content)
            elif not offline:
                qvix = source.qvix_daily()
            else:
                qvix = {}
            for name, frame in qvix.items():
                frame = frame.loc[:end]
                result["changed"]["qvix:" + name] = store.put(
                    "qvix:" + name, frame, QVIX_METHOD, result["run_id"])
        except Exception as exc:
            result["errors"]["qvix"] = str(exc)

        # Only independent post-close quotes may become daily stock input.
        if not offline and end == today and today in calendar:
            codes = [path.stem for path in stock_files(settings.local_daily)]
            quotes, errors = source.quotes(codes)
            if errors:
                result["errors"]["stock_quotes"] = errors
            if not quotes.empty:
                final = quotes.timestamp.map(lambda t: t.date() == today.date() and
                                             t.hour >= 15 and t <= pd.Timestamp(now_shanghai()))
                quotes = quotes.loc[final & valid_ohlc(quotes) & quotes.prevclose.gt(0)]
                if not quotes.empty:
                    save_closing_quotes(settings.data_dir, quotes, today)
            # Build a separate same-method close history; never promote an earlier snapshot.
            result["iv30_close"] = OptionSource(source, settings.risk_free_rate, settings.max_age_seconds).snapshots(QVIX)
            for name, item in result["iv30_close"].items():
                if item["status"] == "estimated" and pd.Timestamp(item["quote_time_min"]).hour >= 15:
                    row = pd.DataFrame([{"close": item["value"], "quote_time": item["quote_time"],
                                         "tenor_mode": item["tenor_mode"]}], index=[today])
                    result["changed"]["iv30:" + name] = store.put("iv30:" + name, row, item["method"], result["run_id"])
                else:
                    result["errors"]["iv30:" + name] = item.get("reason", "期权未提供收盘后的有效报价")

        try:
            if not len(calendar) or any(indices[c].empty for c in ("000300.SH", "000852.SH")):
                raise ValueError("沪深300/中证1000日线不足，不能计算恐慌评分")
            old = store.read("panic", PANIC_METHOD)
            left = start if start is not None else (
                old.index[max(0, len(old) - 10)] if len(old) else pd.Timestamp("2009-01-01"))
            # Long damage needs 252-session peaks plus the persistence warmup.
            padded = calendar[max(0, calendar.searchsorted(left) - DAMAGE_WARMUP):]
            features, warnings = load_features(settings, padded, indices, include_damage=True)
            result["warnings"].extend(warnings)
            features = features.loc[left:]
            if set(DAMAGE_FEATURES).issubset(features.columns):
                damage = features[list(DAMAGE_FEATURES)]
                result["changed"]["damage"] = store.put("damage", damage, DAMAGE_METHOD, result["run_id"])
                result["damage"] = {"date": str(damage.index[-1].date()), **score_damage(damage).iloc[-1].to_dict()}
                if not result["damage"]["damage_valid"]:
                    result["errors"]["damage"] = "长期损伤历史或当日覆盖不足"
                features = features.drop(columns=list(DAMAGE_FEATURES))
            combined = merge(old, features).reindex(calendar)
            combined["data_valid"] = combined.data_valid.fillna(False).astype(bool)
            scored = score_history(combined, settings.window, settings.min_history)
            # Store inputs only; scores are recomputed from the selected window at read time.
            columns = [c for c in features.columns if not c.startswith("p_") and c not in ("score", "panic", "shock")]
            result["changed"]["panic"] = store.put("panic", scored[columns], PANIC_METHOD, result["run_id"])
            result["panic"] = {"date": str(scored.index[-1].date()), **scored.iloc[-1].to_dict()}
            if not np.isfinite(scored.score.iloc[-1]):
                result["errors"]["panic"] = "最新日期覆盖不足或历史不足，未产生有效评分"
        except Exception as exc:
            result["errors"]["panic"] = str(exc)

        result["latest"] = {}
        for name in QVIX:
            history = store.read("qvix:" + name, QVIX_METHOD)
            valid = history.dropna(subset=["close"]) if "close" in history else history
            result["latest"][name] = str(valid.index[-1].date()) if len(valid) else None
            if len(calendar) and (valid.empty or valid.index[-1] < calendar[-1]):
                result["errors"]["qvix:" + name] = "缺少最新交易日数据；旧值仅作历史参考"
        result["index_latest"] = {c: str(d.index[-1].date()) if len(d) else None for c, d in indices.items()}
        return finish(store, settings, result)


def snapshot(settings, *, panic_only=False):
    now, result, source = start_run(settings, "snapshot")
    today = pd.Timestamp(now.date())
    codes = [path.stem for path in stock_files(settings.local_daily)] + list(INDICES)
    with ThreadPoolExecutor(max_workers=2) as pool:
        quote_task = pool.submit(source.quotes, codes)
        qvix_task = (pool.submit(source.qvix_snapshot, now, settings.max_age_seconds, settings.risk_free_rate)
                     if not panic_only else None)
        quotes, errors = quote_task.result()
        qvix = qvix_task.result() if qvix_task else {}
    result["panic_only"] = panic_only
    if panic_only:
        result["warnings"].append("本次为恐慌评分快速采样，未请求期权链；QVIX历史保留不变")
    result["generated_at"] = now_shanghai().isoformat()
    if errors:
        result["errors"]["quotes"] = errors
    with Store(settings.data_dir) as store:
        indices = {code: frame.loc[:today - pd.Timedelta(days=1)] for code, frame in index_history(store).items()}
        calendar = calendar_of(indices)
        result["qvix"] = {}
        for name, item in qvix.items():
            history = store.read("qvix:" + name, QVIX_METHOD)
            values = history.close.reindex(calendar) if "close" in history else pd.Series(index=calendar, dtype=float)
            if item["status"] == "ok":
                item.update(percentile(item["value"], values, settings.window, settings.min_history))
                item.update(qvix_context(item["value"], values))
                item["percentile_basis"] = "vendor_daily_close"
                if not np.isfinite(item["percentile"]):
                    item["status"] = "insufficient_history"
            elif item["status"] == "estimated":
                own = store.read("iv30:" + name, method_id(settings.risk_free_rate))
                own_values = own.close.reindex(calendar) if "close" in own else pd.Series(index=calendar, dtype=float)
                item.update(percentile(item["value"], own_values, settings.window, settings.min_history))
                item.update(qvix_context(item["value"], own_values, reference=values))
                item["percentile_basis"] = "same_method_daily_close"
                reference = percentile(item["value"], values, settings.window, settings.min_history)
                item["reference_percentile"] = reference["percentile"]
                item["reference_sample_count"] = reference["sample_count"]
                item["reference_basis"] = "跨口径：自算IV30相对供应商QVIX历史；未校准，不是同口径分位"
                result["warnings"].append(f"{name}: 自算IV30；跨口径参考分位未校准，同口径历史仅{item['sample_count']}条")
            if item["status"] not in ("ok", "estimated"):
                result["errors"]["qvix:" + name] = item.get("reason", item["status"])
            valid = values.dropna()
            item["previous_close"] = ({"date": str(valid.index[-1].date()), "value": float(valid.iloc[-1])}
                                      if len(valid) else None)
            if item["status"] == "estimated" and len(valid) and valid.index[-1] < calendar[-1]:
                item["warnings"].append("QVIX历史有近期缺口，参考分位的历史样本不完整")
            result["qvix"][name] = item
        try:
            if quotes.empty or not len(calendar):
                raise ValueError("无有效行情或历史，请先运行 sync")
            fresh = quotes.timestamp.map(lambda t: quote_is_fresh(t, now_shanghai(), settings.max_age_seconds))
            quotes = quotes.loc[fresh & valid_ohlc(quotes) & quotes.prevclose.gt(0)]
            result["quote_count"] = len(quotes)
            result["quote_time_min"] = quotes.timestamp.min()
            result["quote_time_max"] = quotes.timestamp.max()
            result["indices"] = {}
            for code, frame in indices.items():
                if code in quotes.index:
                    result["indices"][code] = quotes.loc[code].to_dict()
                if code not in ("000300.SH", "000852.SH"):
                    continue
                if frame.empty or code not in quotes.index or frame.index[-1] != calendar[-1]:
                    raise ValueError(f"{code} 当前行情或前一交易日日线缺失")
                quote = quotes.loc[code]
                if abs(quote.prevclose / frame.close.iloc[-1] - 1) > .0001:
                    raise ValueError(f"{code} 昨收与本地历史不匹配，请先补齐日线")
                indices[code] = merge(frame, pd.DataFrame([quote.to_dict()], index=[today]))
            history = store.read("panic", PANIC_METHOD).reindex(calendar)
            if not set(FACTORS).issubset(history.columns) or history.iloc[-1][list(FACTORS)].isna().any():
                raise ValueError("前一交易日恐慌历史缺失，请先更新本地股票日线并 sync")
            current_calendar = calendar[-DAMAGE_WARMUP:].append(pd.DatetimeIndex([today]))
            features, warnings = load_features(settings, current_calendar, indices, quotes, include_damage=True)
            result["warnings"].extend(warnings)
            if set(DAMAGE_FEATURES).issubset(features.columns):
                result["damage"] = score_damage(features[list(DAMAGE_FEATURES)]).iloc[-1].to_dict()
                result["damage"]["status"] = "approximate" if result["damage"]["damage_valid"] else "unavailable"
                if not result["damage"]["damage_valid"]:
                    result["errors"]["damage"] = "长期损伤历史或当日覆盖不足"
                features = features.drop(columns=list(DAMAGE_FEATURES))
            result["panic"] = score_snapshot(features.iloc[-1].to_dict(), history, settings.window, settings.min_history)
            result["panic"]["status"] = "approximate" if np.isfinite(result["panic"]["score"]) else "unavailable"
            if result["panic"]["status"] == "unavailable":
                result["errors"]["panic"] = "股票覆盖或历史样本不足"
            short_score, long_score = result["panic"]["score"], result.get("damage", {}).get("damage_score", np.nan)
            result["pressure_score"] = max(short_score, long_score) if np.isfinite(short_score) and np.isfinite(long_score) else None
        except Exception as exc:
            result["panic"] = {"status": "unavailable", "score": None, "reason": str(exc)}
            result["errors"]["panic"] = str(exc)
        result["warnings"].append("盘中值对比历史日收盘分布；不是同一时刻的历史分位，也不是触底概率")
        return finish(store, settings, result)


def backfill_damage(settings, *, start=None, end=None):
    """Read-only upstream backfill into a separate versioned damage series."""
    now, result, source = start_run(settings, "damage-backfill")
    last_closed = pd.Timestamp(now.date()) - (pd.Timedelta(days=1) if (now.hour, now.minute) < (15, 10) else pd.Timedelta(0))
    right = min(pd.Timestamp(end), last_closed) if end else last_closed
    with Store(settings.data_dir) as store:
        indices = index_history(store)
        calendar = calendar_of(indices)
        calendar = calendar[calendar <= right]
        if not len(calendar):
            raise ValueError("没有历史指数交易日，请先 sync")
        left = pd.Timestamp(start) if start else calendar[0]
        padded = calendar[max(0, calendar.searchsorted(left) - DAMAGE_WARMUP):]
        if left > calendar[-1]:
            raise ValueError("所选区间没有已收盘交易日")
        features, warnings = load_features(settings, padded, indices, include_damage=True)
        damage = features.loc[left:, list(DAMAGE_FEATURES)]
        result["warnings"] = warnings
        result["changed"] = store.put("damage", damage, DAMAGE_METHOD, result["run_id"])
        scores = score_damage(damage)
        result["valid_days"] = int(scores.damage_score.notna().sum())
        result["first_valid"] = scores.damage_score.first_valid_index()
        result["damage"] = {"date": str(scores.index[-1].date()), **scores.iloc[-1].to_dict()}
        return finish(store, settings, result)
