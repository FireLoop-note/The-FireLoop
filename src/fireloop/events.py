"""Fire Loop 事件总线与事件配置（M1b / B2）。

事件统一形状（普通 dict，可直接 JSON 序列化）::

    {"ts": "<ISO8601 本地时间>",
     "source": "file" | "clipboard" | "foreground",
     "kind": "...",
     ...只带元数据的附加字段...}

隐私口径（硬约束）：事件只描述"发生了什么"，绝不含正文——

- 文件：只带路径 + 变化类型；
- 剪贴板：只带格式类型 / 长度 / 是否文本；
- 前台窗口：只带进程名 + 窗口标题。

EventBus 只做调度，不读文件内容、不读剪贴板内容、不碰窗口句柄。

三道闸（顺序固定：去重 → 去抖 → 每小时配额）：

1. 去重：同 fingerprint 在 ``dedupe_window_s``（默认 60s）内只发一次；
2. 去抖：同 ``(source, kind)`` 在 ``debounce_ms``（默认 1000ms）内合并，只发第一条；
   被合并掉的条数记到下一次同键事件的 ``merged_count`` 字段里；
3. 配额：滚动 1 小时内最多 dispatch ``quota_per_hour`` 条（默认 120），
   超出丢弃，并写一条 ``quota_drop`` 日志。

配置见 ``load_config`` / ``docs/事件.md``；默认 ``events.enabled = false``，全关。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import log, paths

# ---- 默认值（接口约定，Lead 定死；不要改语义） ----
DEFAULT_QUOTA_PER_HOUR = 120
DEFAULT_DEBOUNCE_MS = 1000
DEFAULT_DEDUPE_WINDOW_S = 60
QUOTA_WINDOW_S = 3600.0  # 配额窗口：滚动 1 小时

DEFAULT_CONFIG: dict = {
    "events": {
        "enabled": False,
        "file": {"enabled": True, "watch_dirs": []},
        "clipboard": {"enabled": False},
        "foreground": {"enabled": False},
        "quota_per_hour": DEFAULT_QUOTA_PER_HOUR,
        "debounce_ms": DEFAULT_DEBOUNCE_MS,
        "dedupe_window_s": DEFAULT_DEDUPE_WINDOW_S,
    }
}

WATCHER_NAMES = ("file", "clipboard", "foreground")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def default_config() -> dict:
    """返回一份全新的默认配置（调用方随便改，不污染全局）。"""
    return copy.deepcopy(DEFAULT_CONFIG)


def config_path() -> Path:
    """配置文件路径（.. paths.config_path() 的转发，方便测试替换）。"""
    return paths.config_path()


def _as_bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_positive_int(value, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _as_non_negative_int(value, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _as_non_negative_number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return default
    return value


def _as_watch_dirs(value) -> list[str]:
    """白名单目录：只接受字符串列表，空串/非字符串条目静默丢掉。"""
    if not isinstance(value, list):
        return []
    dirs: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            dirs.append(item.strip())
    return dirs


def _normalize_events(raw) -> dict:
    """把用户写的 events 段合并进默认配置，类型不对的一律回默认值。"""
    config = default_config()
    target = config["events"]
    if not isinstance(raw, dict):
        return config

    target["enabled"] = _as_bool(raw.get("enabled"), target["enabled"])

    file_raw = raw.get("file")
    if isinstance(file_raw, dict):
        target["file"]["enabled"] = _as_bool(
            file_raw.get("enabled"), target["file"]["enabled"]
        )
        target["file"]["watch_dirs"] = _as_watch_dirs(file_raw.get("watch_dirs"))

    for name in ("clipboard", "foreground"):
        section = raw.get(name)
        if isinstance(section, dict):
            target[name]["enabled"] = _as_bool(
                section.get("enabled"), target[name]["enabled"]
            )

    target["quota_per_hour"] = _as_positive_int(
        raw.get("quota_per_hour"), DEFAULT_QUOTA_PER_HOUR
    )
    target["debounce_ms"] = _as_non_negative_int(
        raw.get("debounce_ms"), DEFAULT_DEBOUNCE_MS
    )
    target["dedupe_window_s"] = _as_non_negative_number(
        raw.get("dedupe_window_s"), DEFAULT_DEDUPE_WINDOW_S
    )
    return config


def load_config(path: str | Path | None = None) -> dict:
    """读取事件配置。

    - ``path`` 省略时读 ``paths.config_path()``（%LOCALAPPDATA%\\FireLoop\\config.json，
      FIRELOOP_HOME 可覆盖）；
    - 文件不存在：返回默认配置（events.enabled=false，全关）；
    - 文件损坏 / 不是 JSON 对象 / IO 错误：记一条 ``config_error`` 警告日志，回默认配置；
    - 已知字段逐个校验，类型不对回默认值；未知字段忽略。

    返回值一定是完整的、可安全索引的配置 dict。
    """
    target = Path(path) if path is not None else config_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_config()
    except OSError as exc:
        log.log_event(
            "config_error",
            level="warning",
            path=str(target),
            reason="read_failed",
            error=repr(exc),
        )
        return default_config()

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        log.log_event(
            "config_error",
            level="warning",
            path=str(target),
            reason="bad_json",
            error=str(exc),
        )
        return default_config()

    if not isinstance(raw, dict):
        log.log_event(
            "config_error",
            level="warning",
            path=str(target),
            reason="not_object",
        )
        return default_config()

    return _normalize_events(raw.get("events"))


def enabled_watchers(config: dict | None) -> dict[str, bool]:
    """按配置算出每个 watcher 该不该启动。

    ``events.enabled`` 为 false 时一律全 false；它的优先级高于各分项开关。
    任何字段缺失/类型不对都按"关"处理（安全默认）。
    """
    flags = {name: False for name in WATCHER_NAMES}
    if not isinstance(config, dict):
        return flags
    events_cfg = config.get("events")
    if not isinstance(events_cfg, dict) or events_cfg.get("enabled") is not True:
        return flags
    for name in WATCHER_NAMES:
        section = events_cfg.get(name)
        flags[name] = isinstance(section, dict) and section.get("enabled") is True
    return flags


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------

# 这些关键字暗示"正文"，即使调用方手滑塞进来也绝不写日志。
_CONTENT_KEY_BLOCKLIST = frozenset(
    {"text", "content", "data", "html", "rtf", "preview", "image", "clipboard_text"}
)


def now_iso() -> str:
    """当前本地时间 ISO 字符串（毫秒精度，带时区）。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _auto_fingerprint(event: dict) -> str:
    """没带 fingerprint 的事件：按去掉 ts/merged_count 后的内容算稳定指纹。"""
    payload = {
        key: value
        for key, value in event.items()
        if key not in ("ts", "merged_count", "fingerprint")
    }
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        raw = repr(payload)
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
    return f"auto:{digest}"


class EventBus:
    """事件总线：publish / subscribe + 去抖 / 去重 / 配额。

    Args:
        quota_per_hour: 滚动 1 小时内最多 dispatch 的条数，默认 120。
        debounce_ms: 同 (source, kind) 合并窗口（毫秒），默认 1000；0 表示不合并。
        dedupe_window_s: 同 fingerprint 丢弃窗口（秒），默认 60；0 表示不去重。
        clock: 单调时钟函数（测试注入假时钟用），默认 ``time.monotonic``。

    线程安全：publish 可被多个 watcher 线程并发调用；订阅回调在发布线程里执行，
    **回调里的异常会被吞掉并记日志，不影响其它订阅者和 watcher**。
    """

    def __init__(
        self,
        quota_per_hour: int = DEFAULT_QUOTA_PER_HOUR,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        dedupe_window_s: float = DEFAULT_DEDUPE_WINDOW_S,
        clock=None,
    ):
        if isinstance(quota_per_hour, bool) or not isinstance(quota_per_hour, int):
            raise ValueError("quota_per_hour 必须是正整数")
        if quota_per_hour < 1:
            raise ValueError("quota_per_hour 必须 >= 1")
        debounce_ms = _as_non_negative_int(debounce_ms, -1)
        if debounce_ms < 0:
            raise ValueError("debounce_ms 必须是非负整数")
        dedupe_window_s = _as_non_negative_number(dedupe_window_s, -1)
        if dedupe_window_s < 0:
            raise ValueError("dedupe_window_s 必须是非负数字")

        self.quota_per_hour = quota_per_hour
        self.debounce_ms = debounce_ms
        self.dedupe_window_s = dedupe_window_s
        self._clock = clock or time.monotonic

        self._lock = threading.Lock()
        self._subscribers: list = []
        self._last_publish: dict[tuple[str, str], float] = {}
        self._pending_merged: dict[tuple[str, str], int] = {}
        self._fingerprint_last: dict[str, float] = {}
        self._dispatch_times: list[float] = []
        self._dropped_quota = 0
        self._dropped_dedupe = 0
        self._merged_total = 0

    # ---------- 构造 / 统计 ----------

    @classmethod
    def from_config(cls, config: dict | None) -> "EventBus":
        """用 ``load_config()`` 的返回值构造总线。"""
        events_cfg: dict = {}
        if isinstance(config, dict) and isinstance(config.get("events"), dict):
            events_cfg = config["events"]
        return cls(
            quota_per_hour=events_cfg.get("quota_per_hour", DEFAULT_QUOTA_PER_HOUR),
            debounce_ms=events_cfg.get("debounce_ms", DEFAULT_DEBOUNCE_MS),
            dedupe_window_s=events_cfg.get(
                "dedupe_window_s", DEFAULT_DEDUPE_WINDOW_S
            ),
        )

    @property
    def dropped_quota(self) -> int:
        with self._lock:
            return self._dropped_quota

    @property
    def dropped_dedupe(self) -> int:
        with self._lock:
            return self._dropped_dedupe

    @property
    def merged_total(self) -> int:
        with self._lock:
            return self._merged_total

    # ---------- 订阅 ----------

    def subscribe(self, callback) -> None:
        """注册回调 ``callback(event: dict)``。同一个回调可重复注册。"""
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        """移除回调；没注册过就什么都不做。"""
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    # ---------- 发布 ----------

    def publish(self, event: dict) -> bool:
        """发布一条事件。

        返回 True 表示真的 dispatch 了；False 表示被去重/去抖/配额丢弃。
        ``event`` 不会被原地修改（内部复制）；缺少 ``ts`` 时由总线补。
        缺少 ``fingerprint`` 时按事件内容算稳定指纹参与去重。
        """
        if not isinstance(event, dict):
            raise ValueError("event 必须是 dict")
        ev = dict(event)
        source = ev.get("source")
        kind = ev.get("kind")
        if not isinstance(source, str) or not source:
            raise ValueError("event.source 必须是非空字符串")
        if not isinstance(kind, str) or not kind:
            raise ValueError("event.kind 必须是非空字符串")

        fingerprint = ev.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            fingerprint = _auto_fingerprint(ev)

        now = self._clock()
        key = (source, kind)
        dispatch = False
        dropped_quota = False
        subscribers: tuple = ()
        merged_count = 0

        with self._lock:
            # 1) 去重
            last_seen = self._fingerprint_last.get(fingerprint)
            if last_seen is not None and now - last_seen < self.dedupe_window_s:
                self._dropped_dedupe += 1
                return False

            # 2) 去抖（合并）
            last_pub = self._last_publish.get(key)
            if last_pub is not None and (now - last_pub) * 1000.0 < self.debounce_ms:
                self._merged_total += 1
                self._pending_merged[key] = self._pending_merged.get(key, 0) + 1
                return False

            # 3) 配额
            self._prune_quota(now)
            if len(self._dispatch_times) >= self.quota_per_hour:
                self._dropped_quota += 1
                dropped_quota = True
            else:
                ev["ts"] = now_iso()
                merged_count = self._pending_merged.pop(key, 0)
                if merged_count:
                    ev["merged_count"] = merged_count
                self._fingerprint_last[fingerprint] = now
                self._last_publish[key] = now
                self._dispatch_times.append(now)
                subscribers = tuple(self._subscribers)
                dispatch = True

        if dropped_quota:
            log.log_event(
                "quota_drop",
                level="warning",
                source=source,
                kind=kind,
                quota_per_hour=self.quota_per_hour,
                window_s=int(QUOTA_WINDOW_S),
            )
            return False

        if not dispatch:  # 理论上到不了这里，防御式兜底
            return False

        self._log_dispatch(ev)
        for callback in subscribers:
            try:
                callback(ev)
            except Exception as exc:  # 订阅者自己炸了不能拖死事件线程
                log.log_event(
                    "event_callback_error",
                    level="warning",
                    source=source,
                    kind=kind,
                    error=repr(exc),
                )
        return True

    def _log_dispatch(self, event: dict) -> None:
        """统一落 ``event_dispatch`` 日志；再不济也要保证不因日志失败而断流。"""
        fields = {
            key: value
            for key, value in event.items()
            if key != "ts" and key not in _CONTENT_KEY_BLOCKLIST
        }
        try:
            log.log_event("event_dispatch", **fields)
        except Exception:
            # 日志里出现不可序列化的值也不能影响事件分发。
            pass

    def _prune_quota(self, now: float) -> None:
        cutoff = now - QUOTA_WINDOW_S
        while self._dispatch_times and self._dispatch_times[0] <= cutoff:
            self._dispatch_times.pop(0)
