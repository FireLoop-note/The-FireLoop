"""Fire Loop JSONL 日志。

一行一条 JSON，写 %LOCALAPPDATA%\\FireLoop\\logs\\agent-YYYYMMDD.jsonl。
日志失败不能搞崩主流程，异常一律吞掉（写 stderr 也不需要，M1a 阶段静默）。
不记任务原文，只记长度和哈希前缀（由调用方用 protocol.text_fingerprint 生成）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from . import paths

_lock = threading.Lock()


def log_event(event: str, level: str = "info", **fields) -> None:
    """写一条事件日志。event 是事件名，其余字段原样进 JSON。"""
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
    }
    record.update(fields)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with _lock:
        try:
            paths.logs_dir().mkdir(parents=True, exist_ok=True)
            name = f"agent-{datetime.now():%Y%m%d}.jsonl"
            with open(paths.logs_dir() / name, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
