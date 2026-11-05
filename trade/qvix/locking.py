"""One writer per data directory, including long bootstrap operations."""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone


@contextmanager
def writer_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".writer.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(f"数据目录已有写入锁 {path}；确认没有另一个更新进程后，才可手动移除此锁") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}, handle)
        yield
    finally:
        path.unlink(missing_ok=True)
