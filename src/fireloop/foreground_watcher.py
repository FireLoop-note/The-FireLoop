"""前台窗口事件 watcher：ctypes 直调 ``SetWinEventHook`` + 消息循环。

监听 ``EVENT_SYSTEM_FOREGROUND``（=3，前台窗口切换），``WINEVENT_OUTOFCONTEXT``
（=0，回调走消息队列，不需要 DLL 注入）。收到事件后只取：

- 前台窗口所属进程名（``QueryFullProcessImageNameW`` 取完整路径后取 basename）；
- 窗口标题（``GetWindowTextW``，截断到 512 字符）。

**不读窗口内容、不截图、不模拟操作。** 用 ``WINEVENT_SKIPOWNPROCESS`` 跳过
Fire Loop 自己进程的窗口事件；窗口标题可能带文件名/聊天标题，这是验收时确认过
的口径（日志/事件只允许进程名 + 标题）。

``stop()`` 用 ``PostThreadMessageW(WM_QUIT)`` 结束消息循环，然后 Unhook；
``build_event`` 是纯逻辑，方便单测（不碰真实桌面）。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

from . import log

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- Win32 常量 ----
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
WINEVENT_FLAGS = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS

WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_TITLE_CHARS = 512
MAX_PATH_CHARS = 1024

DEFAULT_STOP_TIMEOUT = 2.0

_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,  # HWINEVENTHOOK
    wintypes.DWORD,  # event
    wintypes.HWND,  # hwnd
    wintypes.LONG,  # idObject
    wintypes.LONG,  # idChild
    wintypes.DWORD,  # idEventThread
    wintypes.DWORD,  # dwmsEventTime
)

_SetWinEventHook = _user32.SetWinEventHook
_SetWinEventHook.argtypes = (
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
    _WINEVENTPROC,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
)
_SetWinEventHook.restype = wintypes.HANDLE

_UnhookWinEvent = _user32.UnhookWinEvent
_UnhookWinEvent.argtypes = (wintypes.HANDLE,)
_UnhookWinEvent.restype = wintypes.BOOL

_GetMessageW = _user32.GetMessageW
_GetMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
)
_GetMessageW.restype = wintypes.BOOL

_PeekMessageW = _user32.PeekMessageW
_PeekMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
)
_PeekMessageW.restype = wintypes.BOOL

_TranslateMessage = _user32.TranslateMessage
_TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
_TranslateMessage.restype = wintypes.BOOL

_DispatchMessageW = _user32.DispatchMessageW
_DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
_DispatchMessageW.restype = ctypes.c_ssize_t

_PostThreadMessageW = _user32.PostThreadMessageW
_PostThreadMessageW.argtypes = (
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
_PostThreadMessageW.restype = wintypes.BOOL

_GetCurrentThreadId = _kernel32.GetCurrentThreadId
_GetCurrentThreadId.argtypes = ()
_GetCurrentThreadId.restype = wintypes.DWORD

_GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
_GetWindowThreadProcessId.argtypes = (wintypes.HWND, wintypes.LPDWORD)
_GetWindowThreadProcessId.restype = wintypes.DWORD

_GetWindowTextLengthW = _user32.GetWindowTextLengthW
_GetWindowTextLengthW.argtypes = (wintypes.HWND,)
_GetWindowTextLengthW.restype = ctypes.c_int

_GetWindowTextW = _user32.GetWindowTextW
_GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_GetWindowTextW.restype = ctypes.c_int

_OpenProcess = _kernel32.OpenProcess
_OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_OpenProcess.restype = wintypes.HANDLE

_QueryFullProcessImageNameW = _kernel32.QueryFullProcessImageNameW
_QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    wintypes.LPDWORD,
)
_QueryFullProcessImageNameW.restype = wintypes.BOOL

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# 纯逻辑（可单测，不碰真实桌面）
# ---------------------------------------------------------------------------


def build_event(process: str, title: str, hwnd: int | None = None) -> dict:
    """构造前台窗口事件。只允许进程名 + 标题两个业务字段。"""
    process_name = process if isinstance(process, str) and process else "unknown"
    safe_title = title if isinstance(title, str) else ""
    if len(safe_title) > MAX_TITLE_CHARS:
        safe_title = safe_title[:MAX_TITLE_CHARS]
    return {
        "source": "foreground",
        "kind": "foreground_changed",
        "process": process_name,
        "title": safe_title,
        # 同一个窗口 + 同一标题在 60s 内算重复（避免反复点同一窗口刷屏）
        "fingerprint": f"foreground:{process_name}:{hwnd if hwnd is not None else ''}:{safe_title}",
    }


def _window_title(hwnd) -> str:
    length = int(_GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    capacity = min(length + 1, MAX_TITLE_CHARS + 1)
    buffer = ctypes.create_unicode_buffer(capacity)
    _GetWindowTextW(hwnd, buffer, capacity)
    return buffer.value[:MAX_TITLE_CHARS]


def _process_name(pid: int) -> str:
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "unknown"
    try:
        buffer = ctypes.create_unicode_buffer(MAX_PATH_CHARS)
        size = wintypes.DWORD(len(buffer))
        if _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            name = os.path.basename(buffer.value)
            return name or "unknown"
        return "unknown"
    finally:
        _CloseHandle(handle)


# ---------------------------------------------------------------------------
# watcher
# ---------------------------------------------------------------------------


class ForegroundWatcher:
    """前台窗口 watcher。``start()`` / ``stop()`` 均可重复调用。"""

    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hook = None
        self._callback = None  # 必须挂实例上，防止回调被 GC

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="fireloop-foreground-watch",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(3.0):
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="foreground",
                stage="start_timeout",
            )

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while thread.is_alive() and time.monotonic() < deadline:
            if self._thread_id:
                _PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            thread.join(0.05)
        if thread.is_alive():
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="foreground",
                stage="stop_timeout",
            )

    # ---------- 线程内 ----------

    def _run(self) -> None:
        try:
            self._thread_id = int(_GetCurrentThreadId())
            msg = wintypes.MSG()
            # 先建出线程消息队列：SetWinEventHook(OUTOFCONTEXT) 依赖线程消息循环，
            # stop() 也要靠 PostThreadMessageW(WM_QUIT) 唤醒。
            _PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

            self._callback = _WINEVENTPROC(self._on_foreground)
            hook = _SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_FOREGROUND,
                None,
                self._callback,
                0,
                0,
                WINEVENT_FLAGS,
            )
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hook = hook

            log.log_event("watcher_started", watcher="foreground")
            self._ready.set()

            while True:
                result = _GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                _TranslateMessage(ctypes.byref(msg))
                _DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="foreground",
                stage="run",
                error=repr(exc),
            )
        finally:
            self._ready.set()
            hook, self._hook = self._hook, None
            if hook:
                _UnhookWinEvent(hook)
            log.log_event("watcher_stopped", watcher="foreground")

    def _on_foreground(
        self, hook, event, hwnd, id_object, id_child, id_thread, event_time
    ) -> None:
        """WinEvent 回调（运行在本 watcher 线程的消息循环里）。"""
        try:
            if event != EVENT_SYSTEM_FOREGROUND or not hwnd:
                return
            pid = wintypes.DWORD(0)
            _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or pid.value == os.getpid():
                return
            process_name = _process_name(pid.value)
            title = _window_title(hwnd)
            self.bus.publish(
                build_event(process_name, title, int(hwnd) if hwnd else None)
            )
        except Exception as exc:  # 回调绝不能把异常抛回 C
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="foreground",
                stage="callback",
                error=repr(exc),
            )
