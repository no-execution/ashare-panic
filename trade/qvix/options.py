"""Dated Sina option chains and a separately versioned 30-day variance estimate.

This is not the vendor QVIX algorithm. No vendor history is written under this method.
Formula reference: https://cdn.cboe.com/resources/vix/VIX_Methodology.pdf
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from .sources import SHANGHAI, now_shanghai, quote_is_fresh

ETF = {"50etf": "510050", "300etf": "510300", "500etf": "510500"}
API = "https://stock.finance.sina.com.cn/futures/api/openapi.php/"


def method_id(rate):
    return f"variance30-v1:r={rate:.8g}"


def valuation_time(asof):
    clock = asof.hour * 60 + asof.minute
    if 690 <= clock < 780:
        return asof.normalize() + pd.Timedelta(hours=11, minutes=30)
    if clock >= 900:
        return asof.normalize() + pd.Timedelta(hours=15)
    return asof


def parse_options(content):
    rows = []
    text = content.decode("gb18030", errors="replace")
    for symbol, value in re.findall(r'hq_str_((?:CON_OP_|P_OP_)[\w]+)="([^"]+)"', text):
        f = value.split(",")
        if len(f) < 48:
            raise ValueError(f"Incomplete option quote: {symbol}")
        stamp = pd.Timestamp(f[32]).tz_localize(SHANGHAI)
        is_index = symbol.startswith("P_OP_")
        if not is_index and f[43] != "M":
            continue  # Adjusted ETF contracts cannot be mixed with standard contracts.
        if f[45] not in ("C", "P"):
            raise ValueError(f"Unknown option type: {symbol}")
        expiry = (stamp.normalize() + pd.Timedelta(days=int(f[47])) if is_index
                  else pd.Timestamp(f[46]).tz_localize(SHANGHAI))
        rows.append({"code": symbol, "kind": f[45], "strike": float(f[7]),
                     "bid": float(f[1]), "ask": float(f[3]), "previous_close": float(f[8]),
                     "timestamp": stamp, "expiry": expiry + pd.Timedelta(hours=15), "name": f[37]})
    return pd.DataFrame(rows)


def observe_book_updates(previous, current, now):
    """Bound a book change between two captures without relabelling the source clock.

    Sina's timestamp can remain fixed while bid/ask changes. A download time alone
    is insufficient; require a changed book and today's dated quotes in both captures.
    """
    current = current.copy()
    prior = previous.set_index("code")
    current["effective_timestamp"] = current.timestamp
    current["time_basis"] = "source_timestamp"
    for i, row in current.iterrows():
        if row.code not in prior.index:
            continue
        old = prior.loc[row.code]
        if old.strike != row.strike or old.kind != row.kind or old.expiry != row.expiry:
            raise ValueError("两次采样的合约身份发生变化")
        changed = row.bid != old.bid or row.ask != old.ask
        valid_dates = (row.timestamp.date() == old.timestamp.date() == now.date()
                       and old.timestamp <= row.timestamp <= now)
        if changed and valid_dates and old.observed_at <= row.observed_at <= now:
            current.loc[i, "effective_timestamp"] = max(row.timestamp, old.observed_at)
            current.loc[i, "time_basis"] = "observed_book_change"
        elif (valid_dates and "effective_timestamp" in previous and old.effective_timestamp > row.timestamp
              and old.effective_timestamp <= old.observed_at <= row.observed_at <= now):
            # Carry an already proved lower bound through the next identical capture.
            current.loc[i, "effective_timestamp"] = old.effective_timestamp
            current.loc[i, "time_basis"] = old.time_basis
    return current


def term_variance(chain, asof, rate, tick=.0001, max_age=300):
    """OTM mid-quotes, put-call parity forward, K0 and strike-spacing integration."""
    if chain.empty or chain.expiry.nunique() != 1:
        raise ValueError("期权到期日缺失或不一致")
    if chain.duplicated(["kind", "strike"]).any():
        raise ValueError("同一到期日存在重复行权价/类型")
    effective = chain.get("effective_timestamp", chain.timestamp)
    chain = chain.assign(effective_timestamp=effective,
                         fresh=effective.map(lambda t: quote_is_fresh(t, asof, max_age)))
    pricing_time = valuation_time(asof)
    days = (chain.expiry.iloc[0] - pricing_time).total_seconds() / 86400
    if days <= 0:
        raise ValueError("期权已到期")
    t = days / 365
    values = chain[["strike", "bid", "ask"]]
    if not np.isfinite(values).all(axis=None) or (chain.strike <= 0).any() or (chain.bid < 0).any() or (chain.ask < chain.bid).any():
        raise ValueError("期权价格无效或买卖价倒挂")
    quotes = chain.assign(mid=(chain.bid + chain.ask) / 2).pivot(index="strike", columns="kind", values=["mid", "bid", "ask"])
    fresh = chain.pivot(index="strike", columns="kind", values="fresh")
    if ("mid", "C") not in quotes or ("mid", "P") not in quotes or quotes.isna().any(axis=None):
        raise ValueError("认购/认沽行权价不匹配")
    paired = quotes[(quotes["bid"]["C"] > 0) & (quotes["bid"]["P"] > 0) & fresh.all(axis=1)]
    if paired.empty:
        raise ValueError("无可用平值认购/认沽报价")
    k = (paired["mid"]["C"] - paired["mid"]["P"]).abs().idxmin()
    forward = float(k + np.exp(rate * t) * (paired.loc[k, ("mid", "C")] - paired.loc[k, ("mid", "P")]))
    below = quotes.index[quotes.index <= forward]
    if len(below) == 0 or forward >= quotes.index.max():
        raise ValueError("期权链未覆盖隐含远期价格")
    k0 = float(below[-1])
    if k0 not in paired.index:
        raise ValueError("K0认购/认沽报价不可用")
    used = {(k, "C"), (k, "P"), (k0, "C"), (k0, "P")}
    for kind in ("C", "P"):
        for strike in (k, k0):
            spread = quotes.loc[strike, ("ask", kind)] - quotes.loc[strike, ("bid", kind)]
            if spread > max(3 * tick, .5 * quotes.loc[strike, ("mid", kind)]):
                raise ValueError("远期/K0报价价差过大")
    selected = {k0: float(quotes.loc[k0, "mid"].mean())}
    counts, stale = {}, 0
    for kind, strikes in [("P", quotes.index[quotes.index < k0][::-1]), ("C", quotes.index[quotes.index > k0])]:
        zeros = count = 0
        for strike in strikes:
            if not fresh.loc[strike, kind]:
                stale += 1
                zeros = 0
                continue  # Exclude stale OTM quotes; do not treat them as zero bids.
            bid, ask = quotes.loc[strike, ("bid", kind)], quotes.loc[strike, ("ask", kind)]
            if bid == 0:
                zeros += 1
                if zeros == 2:
                    break
                continue
            zeros = 0
            mid = (bid + ask) / 2
            if ask - bid > max(3 * tick, .5 * mid):
                raise ValueError(f"价差过大: {kind} {strike}")
            selected[float(strike)] = float(mid)
            used.add((strike, kind))
            count += 1
        counts[kind] = count
    if min(counts.values()) < 2:
        raise ValueError("远期价格两侧均需至少两个有效虚值行权价")
    if stale / (len(selected) + stale) > .2:
        raise ValueError("虚值行权价中超过20%的报价过期，拒绝不完整估计")
    used_rows = chain[[ (row.strike, row.kind) in used for row in chain.itertuples() ]]
    if (used_rows.effective_timestamp.max() - used_rows.effective_timestamp.min()).total_seconds() > max_age:
        raise ValueError("实际参与计算的报价时间跨度过大")
    strikes = np.array(sorted(selected))
    delta = np.empty(len(strikes))
    delta[0], delta[-1] = strikes[1] - strikes[0], strikes[-1] - strikes[-2]
    delta[1:-1] = (strikes[2:] - strikes[:-2]) / 2
    prices = np.array([selected[k] for k in strikes])
    variance = 2 * np.exp(rate * t) / t * np.sum(delta * prices / strikes ** 2) - (forward / k0 - 1) ** 2 / t
    if not np.isfinite(variance) or variance <= 0:
        raise ValueError("计算方差非正或非有限值")
    return {"variance": float(variance), "days": days, "expiry": chain.expiry.iloc[0].isoformat(),
            "valuation_time": pricing_time.isoformat(),
            "forward": forward, "k0": k0, "strikes": len(strikes),
            "min_strike": float(strikes.min()), "max_strike": float(strikes.max()),
            "stale_otm_excluded": stale, "used_codes": used_rows.code.tolist(),
            "source_timestamp_min": used_rows.timestamp.min().isoformat(),
            "observed_book_updates": int(used_rows.get("time_basis", pd.Series(dtype=str)).eq("observed_book_change").sum()),
            "quote_time_min": used_rows.effective_timestamp.min().isoformat(),
            "quote_time": used_rows.effective_timestamp.max().isoformat()}


def variance30(chain, asof, rate=.015, tick=.0001, max_age=300):
    terms = [term_variance(part, asof, rate, tick, max_age) for _, part in chain.groupby("expiry", sort=True)]
    if len(terms) != 2:
        raise ValueError("需要两个不同到期月份")
    near, far = terms
    d1, d2 = near["days"], far["days"]
    if d1 < 7 or d2 <= 30 or d1 > 37:
        raise ValueError("到期期限无法用于30天估计：需要近月>=7天且<=37天，次近月>30天")
    w1 = (d2 - 30) / (d2 - d1)
    w2 = 1 - w1
    variance = (near["variance"] * d1 * w1 + far["variance"] * d2 * w2) / 30
    if not np.isfinite(variance) or variance <= 0:
        raise ValueError("30天方差无效")
    warnings = ["自算IV30，非供应商QVIX；固定利率假设，有限挂牌行权价覆盖"]
    if d1 > 30:
        warnings.append(f"最近到期尚有{d1:.3f}天，30天总方差采用有限外推")
    excluded = sum(term["stale_otm_excluded"] for term in terms)
    if excluded:
        warnings.append(f"剔除{excluded}个过期虚值报价；剩余行权价积分，结果为近似")
    if any(term["observed_book_updates"] for term in terms):
        warnings.append("部分报价时效由两次采样间买卖盘变化确认；源时间字段保持原样存档")
    return {"status": "estimated", "method": method_id(rate), "source": "sina_option_chain",
            "value": float(100 * np.sqrt(variance)), "quote_time": max(term["quote_time"] for term in terms),
            "quote_time_min": min(term["quote_time_min"] for term in terms), "calculated_at": asof.isoformat(),
            "rate": rate, "tenor_mode": "extrapolated" if d1 > 30 else "interpolated",
            "terms": terms, "contract_count": len(chain), "warnings": warnings}


class OptionSource:
    def __init__(self, http, rate=.015, max_age=300):
        self.http, self.rate, self.max_age = http, rate, max_age

    def api(self, name, endpoint, params):
        result = json.loads(self.http.get(name, API + endpoint, params))["result"]
        if result["status"]["code"] != 0:
            raise ValueError(f"Sina API error: {result['status']}")
        return result["data"]

    def contract_codes(self, series, now):
        if series in ETF:
            cate = series[:-3].upper() + "ETF"
            meta = self.api(f"options-{series}-months.json", "StockOptionService.getStockName", {"exchange": "null", "cate": cate})
            if meta["stockId"] != ETF[series]:
                raise ValueError("期权标的代码与请求不一致")
            months = sorted(set(meta["contractMonth"]))
            selected = []
            for month in months:
                info = self.api(f"options-{series}-{month}-expiry.json", "StockOptionService.getRemainderDay",
                                {"exchange": "null", "cate": cate, "date": month})
                expiry = pd.Timestamp(info["expireDay"], tz=SHANGHAI) + pd.Timedelta(hours=15)
                if (expiry - valuation_time(now)).total_seconds() / 86400 >= 7:
                    selected.append((month, expiry))
                if len(selected) == 2:
                    break
            codes = []
            for month, expiry in selected:
                suffix = ETF[series] + month.replace("-", "")[2:]
                content = self.http.get(f"options-{series}-{month}-codes.txt",
                                        f"https://hq.sinajs.cn/list=OP_UP_{suffix},OP_DOWN_{suffix}")
                codes += re.findall(r"CON_OP_\d+", content.decode())
            if len(selected) != 2:
                raise ValueError("不足两个未临近到期的月份")
            return sorted(set(codes)), {month.replace("-", ""): expiry for month, expiry in selected}
        page = self.http.get("options-mo-months.html", "https://stock.finance.sina.com.cn/futures/view/optionsCffexDP.php/mo/cffex")
        months = sorted(set(re.findall(r"\bmo\d{4}\b", page.decode("utf-8", errors="replace"))))
        codes = []
        # Exact expiry is taken from the dated contract's remaining-calendar-days field.
        # Fetch three nearest months so a <7-day contract can be excluded after parsing.
        for month in months[:3]:
            data = self.api(f"options-{month}-chain.json", "OptionService.getOptionData",
                            {"type": "futures", "product": "mo", "exchange": "cffex", "pinzhong": month})
            codes += ["P_OP_" + row[-1] for side in ("up", "down") for row in data[side]]
        return sorted(set(codes)), {}

    def quote_batch(self, series, codes, attempt=0):
        frames, returned = [], set()
        for i in range(0, len(codes), 80):
            content = self.http.get(f"options-{series}-quotes-{attempt}-{i // 80}.txt",
                                    "https://hq.sinajs.cn/list=" + ",".join(codes[i:i + 80]))
            returned.update(re.findall(r'hq_str_((?:CON_OP_|P_OP_)[\w]+)="[^"]+', content.decode("gb18030")))
            frame = parse_options(content)
            frame["observed_at"] = pd.Timestamp(now_shanghai())
            frames.append(frame)
        if set(codes) - returned:
            raise ValueError("期权报价返回不完整")
        return pd.concat(frames, ignore_index=True)

    def fetch(self, series):
        codes, expiries = self.contract_codes(series, pd.Timestamp(now_shanghai()))
        if not codes:
            raise ValueError("期权合约列表为空")
        chain = self.quote_batch(series, codes)
        if chain.empty:
            raise ValueError("无标准期权合约")
        if expiries and not chain.expiry.isin(expiries.values()).all():
            raise ValueError("合约报价到期日与月份元数据不一致")
        if series == "1000index":
            for row in chain.itertuples():
                month = re.search(r"mo(\d{4})", row.code).group(1)
                if row.expiry.strftime("%y%m") != month:
                    raise ValueError("股指期权剩余天数与合约月份不一致")
        now = pd.Timestamp(now_shanghai())
        chain = chain[(chain.expiry - valuation_time(now)).dt.total_seconds() >= 7 * 86400]
        dates = sorted(chain.expiry.unique())[:2]
        chain = chain[chain.expiry.isin(dates)]
        # At most two short refreshes, only when the complete estimate fails quality checks.
        for attempt in range(3):
            chain.to_csv(self.http.raw_dir / f"options-{series}-normalized.csv", index=False)
            try:
                return variance30(chain, pd.Timestamp(now_shanghai()), self.rate,
                                  .2 if series == "1000index" else .0001, self.max_age)
            except ValueError:
                if attempt == 2:
                    raise
            time.sleep(2)
            current = self.quote_batch(series, chain.code.tolist(), attempt + 1)
            if not current.expiry.isin(dates).all():
                raise ValueError("复取报价的到期日发生变化")
            chain = observe_book_updates(chain, current, pd.Timestamp(now_shanghai()))

    def snapshots(self, series):
        def fetch(name):
            try:
                return name, self.fetch(name)
            except Exception as exc:
                return name, {"status": "unavailable", "value": None, "reason": str(exc), "method": method_id(self.rate)}
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(pool.map(fetch, series))
