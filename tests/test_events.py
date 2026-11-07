"""事件子系统单测（标准库 unittest）。

覆盖范围：

- ``EventBus``：去抖合并 / fingerprint 去重 / 每小时配额 / 订阅回调异常隔离 /
  ``event_dispatch`` 与 ``quota_drop`` 日志（假时钟，纯逻辑）；
- ``load_config``：默认全关、用户配置合并、坏 JSON / 坏类型回退、
  ``FIRELOOP_HOME`` 隔离（不碰真实 %LOCALAPPDATA%）；
- ``FileWatcher``：``parse_notifications`` 纯逻辑（created/modified/deleted/renamed、
  改名配对、坏缓冲）+ 真实临时目录冒烟（文件创建/修改/改名/删除，带超时）；
- ``ClipboardWatcher`` / ``ForegroundWatcher``：**单测不操作真实剪贴板和桌面**，
  只测格式名 / 事件构造 / 过滤逻辑。真实剪贴板和前台窗口冒烟步骤写在
  docs/事件.md，交给验收脚本跑。

跑法::

    cd D:\\ds植入式
    $env:PYTHONPATH="$PWD\\src"
    D:\\Python\\python.exe -m unittest discover -s tests -p "test_events.py" -v
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fireloop import events, paths
from fireloop.clipboard_watcher import build_event as build_clipboard_event
from fireloop.clipboard_watcher import format_name
from fireloop.events import (
    DEFAULT_CONFIG,
    EventBus,
    enabled_watchers,
    load_config,
)
from fireloop.file_watcher import FileWatcher, parse_notifications
from fireloop.foreground_watcher import build_event as build_foreground_event

# 文件通知记录头：NextEntryOffset / Action / FileNameLength
_HEADER = 12


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


class FakeClock:
    """可手动拨动的假单调时钟。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _same_path(left, right) -> bool:
    if left is None:
        return False
    return _norm(left) == _norm(right)


def _notify_buffer(items) -> bytes:
    """按 FILE_NOTIFY_INFORMATION 拼一段 ReadDirectoryChangesW 缓冲区。

    items: [(action, 相对文件名), ...]
    """
    records: list[bytes] = []
    for index, (action, name) in enumerate(items):
        raw_name = name.encode("utf-16-le")
        if index == len(items) - 1:
            next_offset = 0
        else:
            next_offset = _HEADER + len(raw_name)
        records.append(
            struct.pack("<III", next_offset, action, len(raw_name)) + raw_name
        )
    return b"".join(records)


def _read_log(home: Path) -> list[dict]:
    records: list[dict] = []
    log_dir = home / "logs"
    if not log_dir.is_dir():
        return records
    for path in sorted(log_dir.glob("agent-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


class _IsolatedHomeTest(unittest.TestCase):
    """把 FIRELOOP_HOME 指到临时目录，测试绝不写真实 %LOCALAPPDATA%。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fireloop-events-test-")
        self._old_home = os.environ.get("FIRELOOP_HOME")
        os.environ["FIRELOOP_HOME"] = self._tmp.name

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("FIRELOOP_HOME", None)
        else:
            os.environ["FIRELOOP_HOME"] = self._old_home
        self._tmp.cleanup()

    @property
    def home(self) -> Path:
        return Path(self._tmp.name)


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBusTests(_IsolatedHomeTest):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.seen: list[dict] = []

    def _bus(self, **kwargs) -> EventBus:
        kwargs.setdefault("clock", self.clock)
        bus = EventBus(**kwargs)
        bus.subscribe(self.seen.append)
        return bus

    def _publish(self, bus, kind: str, source: str = "file", **extra) -> bool:
        payload = {"source": source, "kind": kind}
        payload.update(extra)
        return bus.publish(payload)

    def test_publish_and_subscribe(self):
        bus = self._bus()
        self.assertTrue(self._publish(bus, "created", path=r"C:\tmp\a.txt"))
        self.assertEqual(len(self.seen), 1)
        event = self.seen[0]
        self.assertEqual(event["source"], "file")
        self.assertEqual(event["kind"], "created")
        self.assertEqual(event["path"], r"C:\tmp\a.txt")
        self.assertIn("ts", event)
        # ts 是可解析的带时区 ISO 时间
        parsed = datetime.fromisoformat(event["ts"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_debounce_merges_same_source_kind(self):
        bus = self._bus(debounce_ms=1000, dedupe_window_s=0)
        self.assertTrue(self._publish(bus, "created", path="A"))
        self.clock.advance(0.5)
        self.assertFalse(self._publish(bus, "created", path="B"))
        self.clock.advance(0.6)  # 距上次 publish 1.1s
        self.assertTrue(self._publish(bus, "created", path="C"))

        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.seen[0]["path"], "A")
        self.assertEqual(self.seen[1]["path"], "C")
        self.assertEqual(self.seen[1]["merged_count"], 1)
        self.assertNotIn("merged_count", self.seen[0])
        self.assertEqual(bus.merged_total, 1)

    def test_debounce_different_kind_not_merged(self):
        bus = self._bus(debounce_ms=1000, dedupe_window_s=0)
        self.assertTrue(self._publish(bus, "created", path="A"))
        self.clock.advance(0.2)
        self.assertTrue(self._publish(bus, "modified", path="A"))
        self.assertEqual(len(self.seen), 2)

    def test_dedupe_same_fingerprint_within_window(self):
        bus = self._bus(debounce_ms=0, dedupe_window_s=60)
        payload = {"source": "file", "kind": "created", "fingerprint": "fp-1"}
        self.assertTrue(bus.publish(dict(payload)))
        self.clock.advance(2.0)
        self.assertFalse(bus.publish(dict(payload)))  # 60s 内同指纹
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(bus.dropped_dedupe, 1)

        self.clock.advance(59.0)  # 距上次 dispatch 61s
        self.assertTrue(bus.publish(dict(payload)))
        self.assertEqual(len(self.seen), 2)

    def test_auto_fingerprint_dedupes_identical_events(self):
        bus = self._bus(debounce_ms=0, dedupe_window_s=60)
        payload = {"source": "file", "kind": "created", "path": "X"}
        self.assertTrue(bus.publish(dict(payload)))
        self.clock.advance(2.0)
        self.assertFalse(bus.publish(dict(payload)))
        self.assertEqual(len(self.seen), 1)
        self.clock.advance(59.0)
        self.assertTrue(bus.publish(dict(payload)))
        self.assertEqual(len(self.seen), 2)

    def test_quota_per_hour_and_quota_drop_log(self):
        bus = self._bus(quota_per_hour=3, debounce_ms=0, dedupe_window_s=0)
        for index in range(3):
            self.assertTrue(self._publish(bus, f"kind{index}", path=f"p{index}"))
        self.assertFalse(self._publish(bus, "kind3", path="p3"))
        self.assertEqual(len(self.seen), 3)
        self.assertEqual(bus.dropped_quota, 1)

        records = _read_log(self.home)
        drops = [record for record in records if record["event"] == "quota_drop"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["source"], "file")
        self.assertEqual(drops[0]["kind"], "kind3")
        self.assertEqual(drops[0]["quota_per_hour"], 3)

        # 滚动窗口：1 小时后配额恢复
        self.clock.advance(3601)
        self.assertTrue(self._publish(bus, "kind4", path="p4"))

    def test_event_dispatch_log_has_metadata_no_content(self):
        bus = self._bus(debounce_ms=0, dedupe_window_s=0)
        self.assertTrue(
            self._publish(bus, "created", path=r"C:\tmp\a.txt", size=3)
        )
        records = _read_log(self.home)
        dispatches = [
            record for record in records if record["event"] == "event_dispatch"
        ]
        self.assertEqual(len(dispatches), 1)
        dispatch = dispatches[0]
        self.assertEqual(dispatch["source"], "file")
        self.assertEqual(dispatch["kind"], "created")
        self.assertEqual(dispatch["path"], r"C:\tmp\a.txt")
        for forbidden in ("text", "content", "data", "html", "rtf"):
            self.assertNotIn(forbidden, dispatch)

    def test_subscriber_exception_is_isolated(self):
        bus = self._bus(debounce_ms=0, dedupe_window_s=0)
        received: list[dict] = []

        def bad_callback(event):
            raise RuntimeError("boom")

        bus.subscribe(bad_callback)
        bus.subscribe(received.append)
        self.assertTrue(self._publish(bus, "created", path="X"))
        self.assertEqual(len(received), 1)
        records = _read_log(self.home)
        self.assertTrue(
            any(record["event"] == "event_callback_error" for record in records)
        )

    def test_unsubscribe(self):
        bus = self._bus(debounce_ms=0, dedupe_window_s=0)
        received: list[dict] = []
        bus.subscribe(received.append)
        bus.unsubscribe(received.append)  # 注册的是同一个 bound method 对象时可移除
        received.clear()
        self.assertTrue(self._publish(bus, "created", path="X"))
        self.assertEqual(received, [])

    def test_invalid_event_raises_value_error(self):
        bus = self._bus()
        for bad in ("not-a-dict", {}, {"source": "", "kind": "x"}, {"source": "file"}):
            with self.assertRaises(ValueError):
                bus.publish(bad)

    def test_from_config_carries_limits(self):
        config = events.default_config()
        config["events"]["quota_per_hour"] = 5
        config["events"]["debounce_ms"] = 250
        config["events"]["dedupe_window_s"] = 7
        bus = EventBus.from_config(config)
        self.assertEqual(bus.quota_per_hour, 5)
        self.assertEqual(bus.debounce_ms, 250)
        self.assertEqual(bus.dedupe_window_s, 7)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class LoadConfigTests(_IsolatedHomeTest):
    def _write_config(self, data) -> Path:
        path = paths.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_file_returns_defaults(self):
        config = load_config()
        self.assertEqual(config, DEFAULT_CONFIG)
        events_cfg = config["events"]
        self.assertIs(events_cfg["enabled"], False)
        self.assertEqual(events_cfg["file"], {"enabled": True, "watch_dirs": []})
        self.assertEqual(events_cfg["clipboard"], {"enabled": False})
        self.assertEqual(events_cfg["foreground"], {"enabled": False})
        self.assertEqual(events_cfg["quota_per_hour"], 120)
        self.assertEqual(events_cfg["debounce_ms"], 1000)
        self.assertEqual(events_cfg["dedupe_window_s"], 60)

    def test_default_config_is_a_fresh_copy(self):
        first = load_config()
        first["events"]["file"]["watch_dirs"].append(r"C:\intruder")
        first["events"]["enabled"] = True
        second = load_config()
        self.assertEqual(second, DEFAULT_CONFIG)
        self.assertNotIn(r"C:\intruder", second["events"]["file"]["watch_dirs"])

    def test_custom_config_merged(self):
        watch = self.home / "watch"
        watch.mkdir()
        self._write_config(
            {
                "events": {
                    "enabled": True,
                    "file": {"enabled": False, "watch_dirs": [str(watch)]},
                    "clipboard": {"enabled": True},
                    "foreground": {"enabled": True},
                    "quota_per_hour": 7,
                    "debounce_ms": 250,
                    "dedupe_window_s": 5,
                }
            }
        )
        config = load_config()
        events_cfg = config["events"]
        self.assertIs(events_cfg["enabled"], True)
        self.assertEqual(
            events_cfg["file"], {"enabled": False, "watch_dirs": [str(watch)]}
        )
        self.assertIs(events_cfg["clipboard"]["enabled"], True)
        self.assertIs(events_cfg["foreground"]["enabled"], True)
        self.assertEqual(events_cfg["quota_per_hour"], 7)
        self.assertEqual(events_cfg["debounce_ms"], 250)
        self.assertEqual(events_cfg["dedupe_window_s"], 5)

    def test_bad_json_falls_back_to_defaults_and_logs(self):
        path = paths.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        config = load_config()
        self.assertEqual(config, DEFAULT_CONFIG)
        records = _read_log(self.home)
        self.assertTrue(
            any(
                record["event"] == "config_error"
                and record.get("reason") == "bad_json"
                for record in records
            )
        )

    def test_bad_types_normalized(self):
        self._write_config(
            {
                "events": {
                    "enabled": "yes",
                    "quota_per_hour": -5,
                    "debounce_ms": "fast",
                    "dedupe_window_s": -1,
                    "file": {"enabled": "true", "watch_dirs": ["  ", 7, r"C:\ok"]},
                    "clipboard": "on",
                }
            }
        )
        config = load_config()["events"]
        self.assertIs(config["enabled"], False)
        self.assertEqual(config["quota_per_hour"], 120)
        self.assertEqual(config["debounce_ms"], 1000)
        self.assertEqual(config["dedupe_window_s"], 60)
        self.assertEqual(config["file"]["watch_dirs"], [r"C:\ok"])
        self.assertIs(config["clipboard"]["enabled"], False)

    def test_partial_section_uses_defaults(self):
        self._write_config({"events": {"enabled": True}})
        events_cfg = load_config()["events"]
        self.assertIs(events_cfg["enabled"], True)
        self.assertIs(events_cfg["file"]["enabled"], True)  # 默认 file 分项是开的
        self.assertEqual(events_cfg["file"]["watch_dirs"], [])

    def test_config_path_under_fireloop_home(self):
        self.assertEqual(paths.config_path(), self.home / "config.json")


class EnabledWatchersTests(unittest.TestCase):
    def test_default_all_off(self):
        self.assertEqual(
            enabled_watchers(DEFAULT_CONFIG),
            {"file": False, "clipboard": False, "foreground": False},
        )

    def test_global_switch_wins(self):
        config = events.default_config()
        config["events"]["file"]["enabled"] = True
        config["events"]["clipboard"]["enabled"] = True
        config["events"]["foreground"]["enabled"] = True
        self.assertEqual(
            enabled_watchers(config),
            {"file": False, "clipboard": False, "foreground": False},
        )

    def test_per_watcher_switches(self):
        config = events.default_config()
        config["events"]["enabled"] = True
        config["events"]["file"]["enabled"] = False
        config["events"]["clipboard"]["enabled"] = True
        config["events"]["foreground"]["enabled"] = False
        self.assertEqual(
            enabled_watchers(config),
            {"file": False, "clipboard": True, "foreground": False},
        )

    def test_bad_config_object_is_all_off(self):
        self.assertEqual(
            enabled_watchers(None),
            {"file": False, "clipboard": False, "foreground": False},
        )
        self.assertEqual(
            enabled_watchers({"events": {"enabled": True, "file": "yes"}}),
            {"file": False, "clipboard": False, "foreground": False},
        )


# ---------------------------------------------------------------------------
# FileWatcher：纯解析逻辑
# ---------------------------------------------------------------------------

_ADDED = 1
_REMOVED = 2
_MODIFIED = 3
_RENAMED_OLD = 4
_RENAMED_NEW = 5


class FileNotifyParseTests(unittest.TestCase):
    watch_dir = r"C:\watch"

    def test_created_modified_deleted(self):
        buffer = _notify_buffer(
            [(_ADDED, "a.txt"), (_MODIFIED, "a.txt"), (_REMOVED, "b.txt")]
        )
        events_list = parse_notifications(buffer, self.watch_dir)
        self.assertEqual([event["kind"] for event in events_list], [
            "created",
            "modified",
            "deleted",
        ])
        self.assertEqual(
            events_list[0]["path"], os.path.join(self.watch_dir, "a.txt")
        )
        self.assertEqual(
            events_list[2]["path"], os.path.join(self.watch_dir, "b.txt")
        )
        for event in events_list:
            self.assertEqual(event["source"], "file")
            self.assertIn("fingerprint", event)
            self.assertNotIn("content", event)
            self.assertNotIn("text", event)

    def test_renamed_pair(self):
        buffer = _notify_buffer([(_RENAMED_OLD, "old.txt"), (_RENAMED_NEW, "new.txt")])
        events_list = parse_notifications(buffer, self.watch_dir)
        self.assertEqual(len(events_list), 1)
        self.assertEqual(events_list[0]["kind"], "renamed")
        self.assertEqual(
            events_list[0]["path"], os.path.join(self.watch_dir, "new.txt")
        )
        self.assertEqual(
            events_list[0]["old_path"], os.path.join(self.watch_dir, "old.txt")
        )

    def test_unpaired_old_name_still_reported(self):
        buffer = _notify_buffer([(_RENAMED_OLD, "old.txt")])
        events_list = parse_notifications(buffer, self.watch_dir)
        self.assertEqual(len(events_list), 1)
        self.assertEqual(events_list[0]["kind"], "renamed")
        self.assertIn("old.txt", events_list[0]["path"])

    def test_empty_and_truncated_buffers(self):
        self.assertEqual(parse_notifications(b"", self.watch_dir), [])
        self.assertEqual(parse_notifications(b"\x00" * 8, self.watch_dir), [])
        # 头部声明的名字长度超出缓冲区：不能崩，直接停
        broken = struct.pack("<III", 0, _ADDED, 100) + b"\x00" * 4
        self.assertEqual(parse_notifications(broken, self.watch_dir), [])


# ---------------------------------------------------------------------------
# FileWatcher：真实临时目录冒烟
# ---------------------------------------------------------------------------


class FileWatcherSmokeTests(_IsolatedHomeTest):
    def test_start_skips_missing_dir_and_logs_warning(self):
        bus = EventBus(clock=FakeClock())
        missing = self.home / "no-such-dir"
        watcher = FileWatcher([str(missing)], bus)
        watcher.start()
        try:
            self.assertEqual(watcher._threads, [])
            records = _read_log(self.home)
            self.assertTrue(
                any(
                    record["event"] == "watcher_error"
                    and record.get("stage") == "watch_dir"
                    for record in records
                )
            )
        finally:
            watcher.stop()

    def test_stop_without_start_is_safe(self):
        watcher = FileWatcher([], EventBus(clock=FakeClock()))
        watcher.stop()
        watcher.stop()
        watcher.start()  # 没有目录：记 idle 日志，不起线程
        self.assertEqual(watcher._threads, [])
        watcher.stop()
        watcher.stop()

    def test_real_file_events(self):
        watch_dir = self.home / "watch"
        watch_dir.mkdir()
        bus = EventBus(debounce_ms=0, dedupe_window_s=0, quota_per_hour=1000)
        seen: list[dict] = []
        bus.subscribe(seen.append)
        watcher = FileWatcher([str(watch_dir)], bus)
        watcher.start()
        try:
            target = watch_dir / "note-1.txt"
            target.write_text("hello", encoding="utf-8")
            self.assertTrue(
                _wait_until(
                    lambda: any(
                        event["kind"] == "created"
                        and _same_path(event["path"], target)
                        for event in seen
                    )
                ),
                f"等 created 事件超时，已收到: {seen!r}",
            )

            with open(target, "a", encoding="utf-8") as handle:
                handle.write(" more")
            self.assertTrue(
                _wait_until(
                    lambda: any(
                        event["kind"] == "modified"
                        and _same_path(event["path"], target)
                        for event in seen
                    )
                ),
                f"等 modified 事件超时，已收到: {seen!r}",
            )

            renamed = watch_dir / "note-1-renamed.txt"
            os.replace(target, renamed)
            self.assertTrue(
                _wait_until(
                    lambda: any(
                        event["kind"] == "renamed"
                        and _same_path(event["path"], renamed)
                        for event in seen
                    )
                ),
                f"等 renamed 事件超时，已收到: {seen!r}",
            )

            renamed.unlink()
            self.assertTrue(
                _wait_until(
                    lambda: any(
                        event["kind"] == "deleted"
                        and _same_path(event["path"], renamed)
                        for event in seen
                    )
                ),
                f"等 deleted 事件超时，已收到: {seen!r}",
            )
        finally:
            started = time.monotonic()
            watcher.stop()
            self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(watcher._threads, [])

        # 事件只带路径 + 变化类型，绝不含文件内容
        self.assertTrue(seen)
        for event in seen:
            self.assertEqual(event["source"], "file")
            self.assertIn("ts", event)
            for forbidden in ("text", "content", "data", "html", "rtf"):
                self.assertNotIn(forbidden, event)

        # event_dispatch 日志同样只有元数据
        dispatches = [
            record
            for record in _read_log(self.home)
            if record["event"] == "event_dispatch"
        ]
        self.assertTrue(dispatches)
        for record in dispatches:
            self.assertEqual(record["source"], "file")
            for forbidden in ("text", "content", "data", "html", "rtf"):
                self.assertNotIn(forbidden, record)


# ---------------------------------------------------------------------------
# ClipboardWatcher：只测纯逻辑（不操作真实剪贴板）
# ---------------------------------------------------------------------------


class ClipboardLogicTests(unittest.TestCase):
    def test_standard_format_names(self):
        self.assertEqual(format_name(1), "CF_TEXT")
        self.assertEqual(format_name(13), "CF_UNICODETEXT")
        self.assertEqual(format_name(15), "CF_HDROP")
        self.assertEqual(format_name(999), "unknown_999")

    def test_registered_format_name_never_raises(self):
        name = format_name(0xFFFF)
        self.assertIsInstance(name, str)
        self.assertTrue(name)

    def test_build_event_shape(self):
        event = build_clipboard_event(
            formats=[13, 1],
            format_type="CF_UNICODETEXT",
            is_text=True,
            length=42,
        )
        self.assertEqual(
            set(event),
            {"source", "kind", "format_type", "formats", "is_text", "length", "fingerprint"},
        )
        self.assertEqual(event["source"], "clipboard")
        self.assertEqual(event["kind"], "clipboard_update")
        self.assertEqual(event["formats"], [13, 1])
        self.assertIs(event["is_text"], True)
        self.assertEqual(event["length"], 42)
        for forbidden in ("text", "content", "data", "html", "rtf", "preview"):
            self.assertNotIn(forbidden, event)

    def test_build_event_non_text_and_bad_length(self):
        event = build_clipboard_event(
            formats=[2], format_type="CF_BITMAP", is_text=False, length=None
        )
        self.assertIsNone(event["length"])
        self.assertIs(event["is_text"], False)
        event = build_clipboard_event(
            formats=[2], format_type="CF_BITMAP", is_text=False, length=-9
        )
        self.assertIsNone(event["length"])

    def test_fingerprint_tracks_format_and_length(self):
        first = build_clipboard_event(
            formats=[13], format_type="CF_UNICODETEXT", is_text=True, length=10
        )
        second = build_clipboard_event(
            formats=[13], format_type="CF_UNICODETEXT", is_text=True, length=20
        )
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])


# ---------------------------------------------------------------------------
# ForegroundWatcher：只测纯逻辑（不操作真实桌面）
# ---------------------------------------------------------------------------


class ForegroundLogicTests(unittest.TestCase):
    def test_build_event_shape(self):
        event = build_foreground_event("explorer.exe", "桌面", 12345)
        self.assertEqual(
            set(event), {"source", "kind", "process", "title", "fingerprint"}
        )
        self.assertEqual(event["source"], "foreground")
        self.assertEqual(event["kind"], "foreground_changed")
        self.assertEqual(event["process"], "explorer.exe")
        self.assertEqual(event["title"], "桌面")
        for forbidden in ("content", "text", "data", "image", "screenshot"):
            self.assertNotIn(forbidden, event)

    def test_title_truncated_and_empty_process_default(self):
        event = build_foreground_event("", "x" * 900, None)
        self.assertEqual(event["process"], "unknown")
        self.assertEqual(len(event["title"]), 512)

    def test_fingerprint_distinguishes_windows(self):
        first = build_foreground_event("a.exe", "标题", 1)
        second = build_foreground_event("a.exe", "标题", 2)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])


# ---------------------------------------------------------------------------
# agent 集成：开关 gating（默认全关时绝不启动任何 watcher）
# ---------------------------------------------------------------------------


class AgentIntegrationTests(_IsolatedHomeTest):
    def test_default_config_starts_nothing(self):
        from fireloop import agent

        config = load_config(path=self.home / "missing.json")
        bus, watchers = agent.start_event_watchers(config)
        self.assertIsNone(bus)
        self.assertEqual(watchers, [])

    def test_enabled_but_all_sections_off_starts_nothing(self):
        from fireloop import agent

        config = events.default_config()
        config["events"]["enabled"] = True
        config["events"]["file"]["enabled"] = False
        config["events"]["clipboard"]["enabled"] = False
        config["events"]["foreground"]["enabled"] = False
        bus, watchers = agent.start_event_watchers(config)
        self.assertIsNone(bus)
        self.assertEqual(watchers, [])

    def test_build_watchers_respects_each_switch(self):
        from fireloop import agent

        config = events.default_config()
        config["events"]["enabled"] = True
        config["events"]["file"]["enabled"] = False
        config["events"]["clipboard"]["enabled"] = True
        config["events"]["foreground"]["enabled"] = True
        watchers = agent.build_event_watchers(config, object())
        self.assertEqual(
            [type(watcher).__name__ for watcher in watchers],
            ["ClipboardWatcher", "ForegroundWatcher"],
        )

    def test_build_file_watcher_keeps_watch_dirs(self):
        from fireloop import agent

        watch_dir = self.home / "watch"
        watch_dir.mkdir()
        config = events.default_config()
        config["events"]["enabled"] = True
        config["events"]["clipboard"]["enabled"] = False
        config["events"]["foreground"]["enabled"] = False
        config["events"]["file"]["watch_dirs"] = [str(watch_dir)]
        watchers = agent.build_event_watchers(config, object())
        self.assertEqual(len(watchers), 1)
        self.assertIsInstance(watchers[0], FileWatcher)
        self.assertEqual(watchers[0]._raw_dirs, [str(watch_dir)])


if __name__ == "__main__":
    unittest.main()
