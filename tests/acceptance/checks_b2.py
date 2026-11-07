"""Fire Loop B2 验收项（SID 校验 / hello 超时 / 事件子系统）。

阶段约定（由 run_m1a_acceptance.py 调用）：

- ``run_agent_phase``：M1a 测试 agent 还在跑时执行 hello 超时、当前用户 SID 不误伤；
- ``run_default_config_phase``：M1a agent 退出后，核对默认配置 = 事件全关（M1a 行为不变）；
- ``run_events_phase``：用临时 config.json 起第二个隔离 agent，验收文件事件；
  剪贴板/前台窗口只在 ``--enable-desktop-events`` 时真测，否则 SKIP（SKIP 不算通过）；
- ``run_cross_user_phase``：跨用户 SID 拒绝路径。没有 fl_test 账号/密码时 SKIP，
  并明确写清"SKIP 不是 PASS"；账号由 Lead 的 task-9 创建。

跨用户拒绝口径（Lead 2026-11-07 确认）：标准账户 fl_test 很可能在 CreateFile 阶段就被
Windows 命名管道默认 DACL 拒绝（ERROR_ACCESS_DENIED），根本收不到 unauthorized 帧。
所以验收只要求"连不上/被拒"，Windows DACL 层拒绝或协议层 unauthorized 都算通过，
报告里写清是哪一层；**不允许为了拿 unauthorized 帧去改管道 ACL**。
"""

from __future__ import annotations

import ctypes
import inspect
import json
import os
import subprocess
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from fireloop import client as fireloop_client
from fireloop import events
from fireloop import protocol
from fireloop.pipe_server import PipeServer

from harness import (
    PYTHON,
    _Skip,
    check,
    ensure_agent,
    events_by_pipe,
    first_line,
    load_logs,
    record,
    require,
)

CROSS_USER_SCRIPT = Path(__file__).resolve().parent / "run_cross_user_sid_check.py"
CROSS_USER_DEFAULT_USER = "fl_test"

# ---------------------------------------------------------------------------
# 剪贴板 / 前台窗口 ctypes 小工具（只在 --enable-desktop-events 时用）
# ---------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_SHOW = 5

_user32.OpenClipboard.argtypes = (wintypes.HWND,)
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = ()
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = ()
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = (wintypes.UINT,)
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.SetClipboardData.argtypes = (wintypes.UINT, ctypes.c_void_p)
_user32.SetClipboardData.restype = ctypes.c_void_p

_kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
_kernel32.GlobalAlloc.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
_kernel32.GlobalFree.restype = ctypes.c_void_p
_kernel32.GetCurrentThreadId.argtypes = ()
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_user32.GetForegroundWindow.argtypes = ()
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = (wintypes.HWND,)
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.ShowWindow.restype = wintypes.BOOL
_user32.UpdateWindow.argtypes = (wintypes.HWND,)
_user32.UpdateWindow.restype = wintypes.BOOL
_user32.DestroyWindow.argtypes = (wintypes.HWND,)
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.RegisterClassW.argtypes = (ctypes.c_void_p,)
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.UnregisterClassW.argtypes = (wintypes.LPCWSTR, wintypes.HINSTANCE)
_user32.UnregisterClassW.restype = wintypes.BOOL
_user32.CreateWindowExW.argtypes = (
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
)
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_user32.DefWindowProcW.restype = ctypes.c_ssize_t

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _open_clipboard_with_retry(retries: int = 20):
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def _read_clipboard_text() -> str | None:
    if not _open_clipboard_with_retry():
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def _write_clipboard_text(text: str) -> bool:
    if not _open_clipboard_with_retry():
        return False
    try:
        if not _user32.EmptyClipboard():
            return False
        buffer = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buffer)
        handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            _kernel32.GlobalFree(handle)
            return False
        try:
            ctypes.memmove(pointer, buffer, size)
        finally:
            _kernel32.GlobalUnlock(handle)
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            return False
        return True  # 所有权已交给系统
    finally:
        _user32.CloseClipboard()


def _clear_clipboard() -> None:
    if _open_clipboard_with_retry():
        try:
            _user32.EmptyClipboard()
        finally:
            _user32.CloseClipboard()


def _activate_window(hwnd) -> bool:
    """把窗口切到前台（AttachThreadInput 绕过前台锁，失败返回 False）。"""
    if not hwnd:
        return False
    current_tid = _kernel32.GetCurrentThreadId()
    foreground = _user32.GetForegroundWindow()
    foreground_tid = _user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    attached = False
    try:
        if foreground_tid and foreground_tid != current_tid:
            attached = bool(_user32.AttachThreadInput(current_tid, foreground_tid, True))
        _user32.ShowWindow(hwnd, SW_SHOW)
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        return _user32.GetForegroundWindow() == hwnd
    finally:
        if attached:
            _user32.AttachThreadInput(current_tid, foreground_tid, False)


def _create_test_window(title: str):
    """创建一个带唯一标题的可见窗口，返回 (hwnd, class_name, wndproc_ref)。"""
    class_name = f"FireLoopAcceptTestWindow-{uuid.uuid4().hex[:8]}"
    hinstance = _kernel32.GetModuleHandleW(None)
    results = {}

    def _wndproc(hwnd, message, wparam, lparam):
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    wndproc = _WNDPROC(_wndproc)
    window_class = _WNDCLASSW()
    window_class.style = 0
    window_class.lpfnWndProc = ctypes.cast(wndproc, ctypes.c_void_p)
    window_class.hInstance = hinstance
    window_class.hbrBackground = 6  # COLOR_WINDOW + 1
    window_class.lpszClassName = class_name
    atom = _user32.RegisterClassW(ctypes.byref(window_class))
    if not atom:
        raise AssertionError(f"RegisterClassW 失败（WinError {ctypes.get_last_error()}）")
    results["wndproc"] = wndproc
    hwnd = _user32.CreateWindowExW(
        0,
        class_name,
        title,
        WS_OVERLAPPEDWINDOW | WS_VISIBLE,
        120,
        120,
        420,
        200,
        None,
        None,
        hinstance,
        None,
    )
    if not hwnd:
        _user32.UnregisterClassW(class_name, hinstance)
        raise AssertionError(f"CreateWindowExW 失败（WinError {ctypes.get_last_error()}）")
    _user32.ShowWindow(hwnd, SW_SHOW)
    _user32.UpdateWindow(hwnd)
    return hwnd, class_name, results["wndproc"]


def _destroy_test_window(hwnd, class_name: str) -> None:
    hinstance = _kernel32.GetModuleHandleW(None)
    if hwnd:
        _user32.DestroyWindow(hwnd)
    try:
        _user32.UnregisterClassW(class_name, hinstance)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _assert_event_fields(event: dict, allowed: set[str], label: str) -> None:
    """event_dispatch 记录允许的字段（含日志信封 ts/level/event），不允许正文类字段。"""
    envelope = {"ts", "level", "event"}
    extra = set(event.keys()) - allowed - envelope
    require(not extra, f"{label}事件里出现多余字段（可能有正文）：{sorted(extra)}")
    forbidden = {"text", "content", "data", "html", "rtf", "preview", "image", "clipboard_text"}
    hits = set(event.keys()) & forbidden
    require(not hits, f"{label}事件里出现正文类字段：{sorted(hits)}")


def _same_path(left, right) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )
    except (OSError, ValueError):
        return False


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.1, what: str = "条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# ---------------------------------------------------------------------------
# agent 在跑时：hello 超时 + 当前用户 SID 不误伤
# ---------------------------------------------------------------------------


def run_agent_phase(ctx) -> None:
    def t_hello_timeout() -> str:
        ensure_agent(ctx)
        # 直连管道但不发 hello：服务端应在协议 HELLO_TIMEOUT_MS（默认 2000ms）后
        # 回错误帧并断开，绝不能一直阻塞。
        handle = fireloop_client._connect(ctx.pipe_name, protocol.CONNECT_TIMEOUT_MS)
        started = time.monotonic()
        data = bytearray()
        first_data_at: float | None = None
        closed_at: float | None = None
        try:
            deadline = started + 6.0
            while time.monotonic() < deadline:
                available = fireloop_client._peek_available(handle)
                if available is None:
                    closed_at = time.monotonic()
                    break
                if available > 0:
                    data.extend(fireloop_client._read_chunk(handle, available))
                    if first_data_at is None:
                        first_data_at = time.monotonic()
                    continue
                time.sleep(0.02)
        finally:
            fireloop_client._close(handle)

        require(first_data_at is not None, "连上后 6s 内没收到任何错误帧（首帧超时没生效，服务端可能一直阻塞）")
        require(closed_at is not None, "服务端回了错误帧但没有断开连接")
        first_frame_after = first_data_at - started
        closed_after = closed_at - started
        require(
            1.2 <= first_frame_after <= 4.0,
            f"错误帧在 {first_frame_after:.2f}s 才到，不在 HELLO_TIMEOUT_MS=2000ms 附近",
        )
        require(closed_after <= 5.0, f"断开太慢：{closed_after:.2f}s")
        lines = [line for line in bytes(data).split(b"\n") if line.strip()]
        require(bool(lines), "错误帧内容为空")
        try:
            frame = json.loads(lines[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssertionError(f"错误帧不是合法 JSON：{bytes(data)[:120]!r}") from exc
        require(
            frame.get("ok") is False and frame.get("code") in (protocol.CODE_BAD_TOKEN, protocol.CODE_TIMEOUT),
            f"错误帧 code={frame.get('code')!r}，期望 bad_token 或 timeout",
        )
        require(ctx.agent.poll() is None, "hello 超时后 agent 进程崩了")
        proc, _ = ctx.run_cli(["ping"])
        require(proc.returncode == 0 and proc.stdout.strip() == "pong", f"超时后 ping exit={proc.returncode}")
        _, records, _ = load_logs(ctx)
        timeouts = [
            record_item
            for record_item in events_by_pipe(records, ctx.pipe_name)
            if record_item.get("event") == "hello_timeout"
        ]
        require(bool(timeouts), "日志缺少 hello_timeout 事件")
        return f"约 {first_frame_after:.2f}s 回 {frame.get('code')} 并断开，agent 存活且 ping 正常"

    check(
        ctx,
        "B2-1",
        "首帧 hello 超时：约 2s 内回错误帧并断开，agent 不崩",
        t_hello_timeout,
        suite="b2",
    )

    def t_sid_current_user_ok() -> str:
        ensure_agent(ctx)
        signature = inspect.signature(PipeServer.__init__)
        require("validate_sid" in signature.parameters, "PipeServer 没有 validate_sid 参数")
        require(
            signature.parameters["validate_sid"].default is True,
            f"validate_sid 默认值不是 True：{signature.parameters['validate_sid'].default!r}",
        )
        # 当前用户反复连接不应该被 SID 校验误伤。
        for _ in range(3):
            proc, _ = ctx.run_cli(["ping"])
            require(
                proc.returncode == 0 and proc.stdout.strip() == "pong",
                f"当前用户连接失败 exit={proc.returncode} stderr={first_line(proc.stderr)!r}",
            )
        _, records, _ = load_logs(ctx)
        unauthorized = [
            record_item
            for record_item in events_by_pipe(records, ctx.pipe_name)
            if record_item.get("event") == "unauthorized"
        ]
        require(not unauthorized, f"当前用户被误判 unauthorized：{unauthorized!r}")
        return "validate_sid 默认开启，当前用户 3 次连接全部正常，日志无 unauthorized"

    check(
        ctx,
        "B2-2",
        "SID 校验默认开启且不误伤当前用户（日志无 unauthorized）",
        t_sid_current_user_ok,
        suite="b2",
    )


# ---------------------------------------------------------------------------
# agent 退出后：默认配置必须全关（M1a 行为不变）
# ---------------------------------------------------------------------------


def run_default_config_phase(ctx) -> None:
    def t_default_off() -> str:
        config = events.default_config()
        events_cfg = config.get("events", {})
        require(events_cfg.get("enabled") is False, f"默认 events.enabled 不是 False：{events_cfg.get('enabled')!r}")
        require(
            events_cfg.get("file", {}).get("watch_dirs") == [],
            f"默认 file.watch_dirs 不是 []：{events_cfg.get('file', {}).get('watch_dirs')!r}",
        )
        require(
            events_cfg.get("clipboard", {}).get("enabled") is False,
            "默认 clipboard.enabled 不是 False",
        )
        require(
            events_cfg.get("foreground", {}).get("enabled") is False,
            "默认 foreground.enabled 不是 False",
        )
        flags = events.enabled_watchers(config)
        require(
            not any(flags.values()),
            f"默认配置下居然有 watcher 会被启动：{flags}",
        )

        # 不存在的 config.json → 默认配置（不是报错、不是全开）
        loaded = events.load_config(Path(ctx.home) / "config-does-not-exist.json")
        require(
            events.enabled_watchers(loaded) == flags,
            f"load_config(不存在文件) 没回默认全关：{events.enabled_watchers(loaded)}",
        )

        # M1a 阶段日志不能出现 event_dispatch / events_started（默认不开事件，行为与 M1a 一致）
        _, records, _ = load_logs(ctx)
        dispatch = [record_item for record_item in records if record_item.get("event") == "event_dispatch"]
        require(not dispatch, f"默认关闭时仍产生了事件分发：{dispatch!r}")
        started = [record_item for record_item in records if record_item.get("event") == "events_started"]
        require(not started, f"默认关闭时仍启动了事件子系统：{started!r}")

        # M1a 基线全量回归
        m1a_failures = [
            item for item in ctx.results if item.get("suite") == "m1a" and item["status"] == "FAIL"
        ]
        require(not m1a_failures, f"M1a 基线有失败项：{[item['id'] for item in m1a_failures]}")

        return "events.enabled=False、三个 watcher 全关、默认配置无 event_dispatch，M1a 基线全 PASS"

    check(
        ctx,
        "B2-3",
        "默认配置事件全关，M1a 行为不变（全量回归）",
        t_default_off,
        suite="b2",
    )


# ---------------------------------------------------------------------------
# 事件阶段：起第二个隔离 agent（临时 config.json），验收事件
# ---------------------------------------------------------------------------


def run_events_phase(ctx) -> None:
    watch_dir = Path(ctx.home) / "watch"
    outside_dir = Path(ctx.home) / "outside"
    watch_dir.mkdir(parents=True, exist_ok=True)
    outside_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "events": {
            "enabled": True,
            "file": {"enabled": True, "watch_dirs": [str(watch_dir)]},
            "clipboard": {"enabled": bool(ctx.enable_desktop_events)},
            "foreground": {"enabled": bool(ctx.enable_desktop_events)},
            # 验收要逐条核对 created/modified，临时关掉去抖/去重（产品默认值已另项核对）
            "debounce_ms": 0,
            "dedupe_window_s": 0,
        }
    }
    config_path = Path(ctx.home) / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    desktop = bool(ctx.enable_desktop_events)
    agent = None
    pump = None
    graceful = False

    # 事件子系统的日志（events_started / watcher_started / event_dispatch …）不带 pipe 字段，
    # 且和 M1a 阶段共用同一个 JSONL 文件，所以用"启动事件 agent 前的日志条数"做切片，
    # 只看这一阶段新增的记录。
    _, before_records, _ = load_logs(ctx)
    log_baseline = len(before_records)

    def _records() -> tuple[list[dict], str]:
        _, records, raw = load_logs(ctx)
        return records[log_baseline:], raw

    def _dispatches(source: str | None = None) -> list[dict]:
        records, _ = _records()
        return [
            record_item
            for record_item in records
            if record_item.get("event") == "event_dispatch"
            and (source is None or record_item.get("source") == source)
        ]

    try:
        agent, pump, startup_line = ctx.start_agent(
            ctx.events_pipe, ctx.events_mutex, label="事件测试 agent"
        )
        require(ctx.events_pipe in startup_line, f"事件 agent 启动行不含隔离管道：{startup_line!r}")
        require(agent.poll() is None, "事件 agent 启动后立即退出")

        def t_watchers_started() -> str:
            records, _ = _records()
            events_started = [
                record_item for record_item in records if record_item.get("event") == "events_started"
            ]
            require(bool(events_started), "日志缺少 events_started（config.json 没生效或事件没启动）")
            watchers = events_started[0].get("watchers") or []
            require(
                any("FileWatcher" in str(name) for name in watchers),
                f"events_started 里没有 FileWatcher：{watchers}",
            )
            file_started = [
                record_item
                for record_item in records
                if record_item.get("event") == "watcher_started"
                and record_item.get("watcher") == "file"
            ]
            require(bool(file_started), "日志缺少 file watcher_started")
            dirs = file_started[0].get("dirs") or []
            require(
                any(_same_path(item, watch_dir) for item in dirs),
                f"file watcher 没监听白名单目录 {watch_dir}：{dirs}",
            )
            clipboard_started = [
                record_item
                for record_item in records
                if record_item.get("event") == "watcher_started"
                and record_item.get("watcher") == "clipboard"
            ]
            foreground_started = [
                record_item
                for record_item in records
                if record_item.get("event") == "watcher_started"
                and record_item.get("watcher") == "foreground"
            ]
            if desktop:
                require(bool(clipboard_started), "开了 --enable-desktop-events 但 clipboard watcher 没启动")
                require(bool(foreground_started), "开了 --enable-desktop-events 但 foreground watcher 没启动")
            else:
                require(not clipboard_started, "默认没开桌面事件却启动了 clipboard watcher")
                require(not foreground_started, "默认没开桌面事件却启动了 foreground watcher")
            return (
                f"events_started watchers={watchers}；file 监听 {dirs}；"
                f"桌面 watcher={'开' if desktop else '关'}"
            )

        check(
            ctx,
            "B2-4",
            "事件配置生效：按 config 启动 file watcher，桌面 watcher 只在开关打开时启动",
            t_watchers_started,
            suite="b2",
        )

        def t_file_events() -> str:
            secret = f"B2-FILE-SECRET-{uuid.uuid4().hex}"
            target = watch_dir / "B2 事件 文件.txt"
            target.write_text(secret, encoding="utf-8")
            created = _wait_for(
                lambda: [
                    record_item
                    for record_item in _dispatches("file")
                    if record_item.get("kind") == "created" and _same_path(record_item.get("path"), target)
                ],
                timeout=10.0,
                what="file created 事件",
            )
            require(bool(created), f"10s 内没等到 file created 的 event_dispatch（监听目录={watch_dir}）")
            with target.open("a", encoding="utf-8") as handle:
                handle.write("\n追加内容 " + secret)
            modified = _wait_for(
                lambda: [
                    record_item
                    for record_item in _dispatches("file")
                    if record_item.get("kind") == "modified" and _same_path(record_item.get("path"), target)
                ],
                timeout=10.0,
                what="file modified 事件",
            )
            require(bool(modified), "10s 内没等到 file modified 的 event_dispatch")
            _, raw = _records()
            require(secret not in raw, "日志里出现了文件内容（隐私红线）")
            file_allowed = {"source", "kind", "path", "fingerprint", "old_path", "merged_count"}
            for event in created + modified:
                _assert_event_fields(event, file_allowed, "文件")
            require(
                _same_path(created[0].get("path"), target),
                f"created 事件 path 不对：{created[0].get('path')!r}",
            )
            return (
                f"created+modified 各 1 条 event_dispatch，path 正确，无正文；"
                f"文件内容 secret 未进日志"
            )

        check(
            ctx,
            "B2-5",
            "文件事件：白名单目录创建/修改文件 → event_dispatch，且不含文件内容",
            t_file_events,
            suite="b2",
        )

        def t_outside_no_event() -> str:
            outside = outside_dir / "B2 非白名单 文件.txt"
            outside.write_text("白名单外不该被监听", encoding="utf-8")
            time.sleep(1.2)
            hits = [
                record_item
                for record_item in _dispatches("file")
                if "B2 非白名单" in str(record_item.get("path", ""))
            ]
            require(not hits, f"非白名单目录产生了事件：{hits}")
            return "非白名单目录写文件未产生事件"

        check(
            ctx,
            "B2-6",
            "文件事件只监听白名单目录（非白名单目录不产生事件）",
            t_outside_no_event,
            suite="b2",
        )

        def t_clipboard_event() -> str:
            if not desktop:
                raise _Skip(
                    "默认跳过剪贴板真实操作；加 --enable-desktop-events 才真测（SKIP 不算通过）"
                )
            original = _read_clipboard_text()
            secret = f"B2-CLIP-SECRET-{uuid.uuid4().hex}"
            try:
                require(_write_clipboard_text(secret), "写测试文本到剪贴板失败")
                hit = _wait_for(
                    lambda: _dispatches("clipboard"),
                    timeout=10.0,
                    what="clipboard 事件",
                )
                require(bool(hit), "10s 内没等到 clipboard 的 event_dispatch")
                event = hit[0]
                require(
                    event.get("kind") == "clipboard_update",
                    f"clipboard kind 不对：{event.get('kind')!r}",
                )
                require(event.get("is_text") is True, f"is_text 不是 True：{event.get('is_text')!r}")
                require(
                    isinstance(event.get("length"), int) and event["length"] > 0,
                    f"length 不对：{event.get('length')!r}",
                )
                _, raw = _records()
                require(secret not in raw, "日志里出现了剪贴板正文（隐私红线）")
                clipboard_allowed = {
                    "source",
                    "kind",
                    "format_type",
                    "formats",
                    "is_text",
                    "length",
                    "fingerprint",
                    "merged_count",
                }
                _assert_event_fields(event, clipboard_allowed, "剪贴板")
                return (
                    f"clipboard_update is_text=True length={event.get('length')} "
                    f"format_type={event.get('format_type')!r}，无正文"
                )
            finally:
                if original is not None:
                    _write_clipboard_text(original)
                else:
                    _clear_clipboard()

        check(
            ctx,
            "B2-7",
            "剪贴板事件：真实写入剪贴板触发 event_dispatch（仅格式/长度，无正文）",
            t_clipboard_event,
            suite="b2",
        )

        def t_foreground_event() -> str:
            if not desktop:
                raise _Skip(
                    "默认跳过前台窗口真实操作；加 --enable-desktop-events 才真测（SKIP 不算通过）"
                )
            previous = _user32.GetForegroundWindow()
            title = f"FireLoop 验收窗口 {uuid.uuid4().hex[:8]}"
            hwnd, class_name, _wndproc_ref = _create_test_window(title)
            try:
                require(_activate_window(hwnd), "SetForegroundWindow 失败，无法真实切换前台窗口（系统前台锁）")
                hit = _wait_for(
                    lambda: [
                        record_item
                        for record_item in _dispatches("foreground")
                        if title in str(record_item.get("title", ""))
                    ],
                    timeout=10.0,
                    what="foreground 事件",
                )
                require(bool(hit), "10s 内没等到自己窗口的 foreground event_dispatch")
                event = hit[0]
                require(
                    event.get("kind") == "foreground_changed",
                    f"foreground kind 不对：{event.get('kind')!r}",
                )
                require(bool(event.get("process")), f"foreground 事件缺 process：{event!r}")
                foreground_allowed = {
                    "source",
                    "kind",
                    "process",
                    "title",
                    "fingerprint",
                    "merged_count",
                }
                _assert_event_fields(event, foreground_allowed, "前台窗口")
                return (
                    f"foreground_changed process={event.get('process')!r}，"
                    f"title 匹配测试窗口，无窗口内容"
                )
            finally:
                _destroy_test_window(hwnd, class_name)
                if previous:
                    _activate_window(previous)

        check(
            ctx,
            "B2-8",
            "前台窗口事件：真实切换前台窗口触发 event_dispatch（仅进程名/标题）",
            t_foreground_event,
            suite="b2",
        )

        def t_events_shutdown() -> str:
            nonlocal graceful
            if ctx.skip_graceful_exit:
                ctx.stop_agent_hard(agent, pump)
                raise _Skip("按 --skip-graceful-exit 跳过优雅退出；事件 agent 已强制停止（SKIP 不算通过）")
            delivered, exit_code, lines = ctx.stop_agent_graceful(agent, pump, timeout=10)
            if not delivered:
                ctx.stop_agent_hard(agent, pump)
                raise _Skip("当前环境无法投递 Ctrl+Break；事件 agent 已强制停止（SKIP 不算通过）")
            require(exit_code == 0, f"事件 agent 退出码 {exit_code}（期望 0），尾部={lines[-3:]!r}")
            require(any("已退出" in line for line in lines), f"事件 agent 未打印已退出：{lines[-3:]!r}")
            graceful = True
            records, _ = _records()
            require(
                any(record_item.get("event") == "events_stopped" for record_item in records),
                "日志缺少 events_stopped",
            )
            expected = {"file"}
            if desktop:
                expected |= {"clipboard", "foreground"}
            stopped = {
                record_item.get("watcher")
                for record_item in records
                if record_item.get("event") == "watcher_stopped"
            }
            missing = expected - stopped
            require(not missing, f"缺少 watcher_stopped：{sorted(missing)}（实际 {sorted(stopped)}）")
            stop_errors = [
                record_item
                for record_item in records
                if record_item.get("event") == "watcher_error"
                and record_item.get("stage") == "stop_timeout"
            ]
            require(not stop_errors, f"watcher 停止超时：{stop_errors!r}")
            return f"exit=0，watcher_stopped={sorted(stopped)}，events_stopped 存在，无 stop_timeout"

        check(
            ctx,
            "B2-9",
            "事件 agent 优雅退出：3 类 watcher 都 stopped，无 stop_timeout",
            t_events_shutdown,
            suite="b2",
        )
    finally:
        if agent is not None and agent.poll() is None:
            ctx.stop_agent_hard(agent, pump)
        # 失败路径也清掉临时 config，避免影响后续检查
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# 跨用户 SID 拒绝：没有 fl_test 账号/密码就 SKIP，明确不是 PASS
# ---------------------------------------------------------------------------


def run_cross_user_phase(ctx) -> None:
    def t_cross_user() -> str:
        user = CROSS_USER_DEFAULT_USER
        user_exists = subprocess.run(
            ["net.exe", "user", user],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).returncode == 0
        if not user_exists:
            raise _Skip(
                f"标准账户 {user} 不存在（Lead task-9 未完成）；建好后重跑本项或"
                f"单独跑 tests/acceptance/run_cross_user_sid_check.py。SKIP 不算通过。"
            )
        password = os.environ.get("FL_TEST_PASSWORD", "")
        if not password:
            raise _Skip(
                f"{user} 账号已存在，但验收进程没有 FL_TEST_PASSWORD；"
                f"请交互式跑 tests/acceptance/run_cross_user_sid_check.py --user {user}。"
                "SKIP 不算通过。"
            )
        json_path = Path(ctx.home) / "cross-user-result.json"
        try:
            proc = subprocess.run(
                [
                    PYTHON,
                    str(CROSS_USER_SCRIPT),
                    "--user",
                    user,
                    "--json",
                    str(json_path),
                ],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=ctx.child_env({"FL_TEST_PASSWORD": password}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("跨用户 SID 验收脚本 300s 未结束") from exc
        result: dict = {}
        if json_path.is_file():
            try:
                result = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                result = {}
        verdict = result.get("verdict")
        detail = result.get("detail", "")
        if proc.returncode == 2:
            raise _Skip(f"跨用户验收环境不具备：{detail or first_line(proc.stdout)}。SKIP 不算通过。")
        require(
            proc.returncode == 0,
            f"跨用户拒绝路径不符合预期：verdict={verdict!r} detail={detail!r} "
            f"输出={first_line(proc.stdout)}",
        )
        require(
            verdict in ("sid_denied", "denied_by_windows_dacl"),
            f"跨用户结果 verdict 不认识：{verdict!r}（{detail!r}）",
        )
        return f"{verdict}：{detail}"

    check(
        ctx,
        "B2-10",
        "跨用户 SID 拒绝：fl_test 连当前用户 agent 被拒（Windows DACL 或协议 unauthorized）",
        t_cross_user,
        suite="b2",
    )
