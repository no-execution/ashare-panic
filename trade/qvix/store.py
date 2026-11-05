"""Small SQLite store: idempotent daily rows, revision log, separate run journal."""
import json
import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def dumps(value):
    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(directory / "qvix.sqlite3")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily (
                series TEXT, date TEXT, method TEXT, payload TEXT, run_id TEXT,
                PRIMARY KEY(series,date,method));
            CREATE TABLE IF NOT EXISTS revisions (
                series TEXT, date TEXT, method TEXT, payload TEXT, run_id TEXT,
                replaced_by TEXT);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, kind TEXT, payload TEXT);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.conn.close()

    def put(self, series, frame, method, run_id):
        if frame.index.has_duplicates:
            raise ValueError(f"Duplicate dates: {series}")
        changed = 0
        # One lookup per series instead of one query per historical day.
        existing = dict((date, (payload, old_run)) for date, payload, old_run in self.conn.execute(
            "SELECT date,payload,run_id FROM daily WHERE series=? AND method=?", (series, method)))
        with self.conn:
            for date, row in frame.sort_index().iterrows():
                key = (series, pd.Timestamp(date).strftime("%Y-%m-%d"), method)
                payload = dumps(row.to_dict())
                old = existing.get(key[1])
                if old and old[0] == payload:
                    continue
                if old:
                    self.conn.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?)", (*key, *old, run_id))
                self.conn.execute("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?)", (*key, payload, run_id))
                changed += 1
        return changed

    def read(self, series, method):
        rows = self.conn.execute(
            "SELECT date,payload FROM daily WHERE series=? AND method=? ORDER BY date", (series, method)
        ).fetchall()
        return pd.DataFrame([json.loads(row[1]) for row in rows],
                            index=pd.DatetimeIndex([row[0] for row in rows], name="date"))

    def save_run(self, run_id, kind, result):
        with self.conn:
            self.conn.execute("INSERT INTO runs VALUES (?,?,?)", (run_id, kind, dumps(result)))

    def latest_run(self, kind):
        row = self.conn.execute("SELECT payload FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return json.loads(row[0]) if row else None
