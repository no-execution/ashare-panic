"""Optional RPS CSV aggregation, extracted from the original project.

No RPS rank downloader is included. These helpers keep the existing dashboard
CSV semantics without importing the author's infra repository or matplotlib.
"""
import csv
from dataclasses import dataclass
from pathlib import Path
from collections import deque

LOOKBACK_DAYS = 250
DEFAULT_START_DATE = "2020-01-01"


@dataclass
class RPSCondition:
    col: str
    op: str
    threshold: float

    def match(self, value):
        return value > self.threshold if self.op == "gt" else value >= self.threshold

    def label(self):
        return f"{self.col}{'>' if self.op == 'gt' else '>='}{self.threshold:g}"


def read_csv_dicts(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def load_rps_records(files, conditions):
    records, codes = {}, set()
    for path in files:
        rows = []
        for row in read_csv_dicts(path):
            code = str(row.get("股票代码", "")).strip()
            if not code:
                continue
            try:
                values = {key: float(row[key]) for key in ("RPS50", "RPS120", "RPS250")}
            except (KeyError, ValueError, TypeError):
                continue
            rows.append((code, values))
            if all(c.match(values[c.col]) for c in conditions):
                codes.add(code)
        records[path.stem] = rows
    return records, codes


def build_trade_calendar(directory, max_date, target_dates):
    rows = read_csv_dicts(Path(directory) / "sh/600000.SH.csv")
    return sorted({row.get("日期", row.get("date")) for row in rows
                   if row.get("日期", row.get("date", "")) <= max_date}.union(target_dates))


def compute_intraday_high_ratios(codes, daily_dir, calendar, target_dates, max_date, lookback_days):
    ratios, stats = {}, {"missing_file": 0, "failed_file": 0}
    for code in sorted(codes):
        path = Path(daily_dir) / code[-2:].lower() / f"{code}.csv"
        if not path.exists():
            stats["missing_file"] += 1
            continue
        try:
            high_by_date = {}
            for row in read_csv_dicts(path):
                date = row.get("日期", row.get("date", ""))
                if date > max_date:
                    continue
                try:
                    high_by_date[date] = float(row.get("最高", row.get("high", "")))
                except (ValueError, TypeError):
                    pass
            queue = deque()
            for i, date in enumerate(calendar):
                value = high_by_date.get(date)
                if value is not None:
                    while queue and queue[-1][1] <= value:
                        queue.pop()
                    queue.append((i, value))
                while queue and queue[0][0] < i - lookback_days + 1:
                    queue.popleft()
                if date in target_dates and value is not None and queue and queue[0][1] > 0:
                    ratios[(date, code)] = value / queue[0][1]
        except (ValueError, OSError):
            stats["failed_file"] += 1
    return ratios, stats


def count_by_date(records, conditions, high_ratios, high_threshold, price_filter_enabled):
    rows = []
    for date, items in sorted(records.items()):
        candidates = [(c, v) for c, v in items if all(k.match(v[k.col]) for k in conditions)]
        missing = sum((date, c) not in high_ratios for c, _ in candidates) if price_filter_enabled else 0
        selected = sum(high_ratios.get((date, c), float("-inf")) >= high_threshold for c, _ in candidates) if price_filter_enabled else len(candidates)
        rows.append({"日期": date, "当日股票池数量": len(items), "RPS候选数量": len(candidates),
                     "RPS候选占比": len(candidates) / len(items) if items else 0., "符合条件数量": selected,
                     "符合条件占比": selected / len(items) if items else 0.,
                     "高位过滤剔除数量": len(candidates) - selected, "当日最高价缺失数量": missing})
    return rows


def write_counts_csv(path, rows, conditions, high_threshold, price_filter_enabled, start):
    rules = {c.col: c.label().replace(c.col, "") for c in conditions}
    extra = {f"{key}条件": rules.get(key, "") for key in ("RPS50", "RPS120", "RPS250")}
    extra.update({"高位比例阈值": high_threshold if price_filter_enabled else "",
                  "价格笼子规则": "当日最高价 >= 近250交易日最高价 * 阈值" if price_filter_enabled else "无", "起始日期": start})
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) + list(extra))
        writer.writeheader()
        writer.writerows({**row, **extra} for row in rows)


def rolling_mean(values, window):
    return [sum(values[i - window + 1:i + 1]) / window if i + 1 >= window else None for i in range(len(values))]
