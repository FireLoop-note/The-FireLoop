"""剪贴板事件 watcher：ctypes 直调 ``AddClipboardFormatListener`` + 隐藏消息窗口。

实现方式（不修改剪贴板、不读剪贴板正文）：

1. 在独立 daemon 线程里注册窗口类、创建 **message-only** 隐藏窗口
   （``HWND_MESSAGE``，任务栏/桌面不可见）；
2. ``AddClipboardFormatListener(hwnd)`` 订阅 ``WM_CLIPBOARDUPDATE``；
3. 收到通知后 ``OpenClipboard`` → ``EnumClipboardFormats`` 拿格式类型，
   文本格式再用 ``GetClipboardData`` + ``GlobalSize`` 取字节长度；
4. 立刻 ``CloseClipboard``，把 ``{format_type, formats, is_text, length}`` 交给
   EventBus —— **绝不读取/记录/修改剪贴板内容**。

``stop()`` 通过 ``PostThreadMessageW(WM_QUIT)`` 让消息循环退出，随后移除监听、
销毁窗口、注销窗口类；不会挂死。

附带的 ``format_name`` / ``build_event`` 是纯逻辑，方便单测（不碰真实剪贴板）。
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
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_CLIPBOARDUPDATE = 0x031D
PM_NOREMOVE = 0x0000

HWND_MESSAGE = wintypes.HWND(-3)  # message-only window 的父窗口

CF_TEXT = 1
CF_OEMTEXT = 7
CF_UNICODETEXT = 13
TEXT_FORMATS = frozenset({CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT})

MAX_FORMATS = 64  # 一次剪贴板更新最多枚举这么多种格式，防异常数据

DEFAULT_STOP_TIMEOUT = 2.0

_STANDARD_FORMAT_NAMES = {
    1: "CF_TEXT",
    2: "CF_BITMAP",
    3: "CF_METAFILEPICT",
    4: "CF_SYLK",
    5: "CF_DIF",
    6: "CF_TIFF",
    7: "CF_OEMTEXT",
    8: "CF_DIB",
    9: "CF_PALETTE",
    10: "CF_PENDATA",
    11: "CF_RIFF",
    12: "CF_WAVE",
    13: "CF_UNICODETEXT",
    14: "CF_ENHMETAFILE",
    15: "CF_HDROP",
    16: "CF_LOCALE",
    17: "CF_DIBV5",
    0x0080: "CF_OWNERDISPLAY",
    0x0081: "CF_DSPTEXT",
    0x0082: "CF_DSPBITMAP",
    0x0083: "CF_DSPMETAFILEPICT",
    0x008E: "CF_DSPENHMETAFILE",
}

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    )


_RegisterClassW = _user32.RegisterClassW
_RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
_RegisterClassW.restype = ctypes.c_ushort

_UnregisterClassW = _user32.UnregisterClassW
_UnregisterClassW.argtypes = (wintypes.LPCWSTR, wintypes.HANDLE)
_UnregisterClassW.restype = wintypes.BOOL

_CreateWindowExW = _user32.CreateWindowExW
_CreateWindowExW.argtypes = (
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HANDLE,
    wintypes.HANDLE,
    wintypes.LPVOID,
)
_CreateWindowExW.restype = wintypes.HWND

_DestroyWindow = _user32.DestroyWindow
_DestroyWindow.argtypes = (wintypes.HWND,)
_DestroyWindow.restype = wintypes.BOOL

_DefWindowProcW = _user32.DefWindowProcW
_DefWindowProcW.argtypes = (
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
_DefWindowProcW.restype = ctypes.c_ssize_t

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

_PostQuitMessage = _user32.PostQuitMessage
_PostQuitMessage.argtypes = (ctypes.c_int,)
_PostQuitMessage.restype = None

_GetModuleHandleW = _kernel32.GetModuleHandleW
_GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
_GetModuleHandleW.restype = wintypes.HANDLE

_GlobalSize = _kernel32.GlobalSize
_GlobalSize.argtypes = (wintypes.HANDLE,)
_GlobalSize.restype = ctypes.c_size_t

_AddClipboardFormatListener = _user32.AddClipboardFormatListener
_AddClipboardFormatListener.argtypes = (wintypes.HWND,)
_AddClipboardFormatListener.restype = wintypes.BOOL

_RemoveClipboardFormatListener = _user32.RemoveClipboardFormatListener
_RemoveClipboardFormatListener.argtypes = (wintypes.HWND,)
_RemoveClipboardFormatListener.restype = wintypes.BOOL

_OpenClipboard = _user32.OpenClipboard
_OpenClipboard.argtypes = (wintypes.HWND,)
_OpenClipboard.restype = wintypes.BOOL

_CloseClipboard = _user32.CloseClipboard
_CloseClipboard.argtypes = ()
_CloseClipboard.restype = wintypes.BOOL

_EnumClipboardFormats = _user32.EnumClipboardFormats
_EnumClipboardFormats.argtypes = (wintypes.UINT,)
_EnumClipboardFormats.restype = wintypes.UINT

_GetClipboardData = _user32.GetClipboardData
_GetClipboardData.argtypes = (wintypes.UINT,)
_GetClipboardData.restype = wintypes.HANDLE

_GetClipboardFormatNameW = _user32.GetClipboardFormatNameW
_GetClipboardFormatNameW.argtypes = (wintypes.UINT, wintypes.LPWSTR, ctypes.c_int)
_GetClipboardFormatNameW.restype = ctypes.c_int


# ---------------------------------------------------------------------------
# 纯逻辑（可单测，不碰剪贴板）
# ---------------------------------------------------------------------------


def format_name(fmt: int) -> str:
    """格式号 → 可读格式名。自定义格式查系统注册名；查不到给 unknown_xxx。

    注意：GetClipboardFormatNameW 只查"格式名字"，不打开剪贴板、不读内容。
    """
    try:
        fmt = int(fmt)
    except (TypeError, ValueError):
        return "unknown"
    standard = _STANDARD_FORMAT_NAMES.get(fmt)
    if standard:
        return standard
    if fmt >= 0xC000:  # 注册格式从 0xC000 起
        buffer = ctypes.create_unicode_buffer(256)
        length = _GetClipboardFormatNameW(fmt, buffer, len(buffer))
        if length > 0:
            return buffer.value
        return f"registered_{fmt}"
    return f"unknown_{fmt}"


def build_event(*, formats, format_type: str, is_text: bool, length) -> dict:
    """构造剪贴板事件。只有格式/长度/是否文本，没有正文。"""
    normalized = [int(f) for f in formats]
    safe_length = (
        int(length)
        if isinstance(length, int) and not isinstance(length, bool) and length >= 0
        else None
    )
    signature = ",".join(str(f) for f in normalized)
    return {
        "source": "clipboard",
        "kind": "clipboard_update",
        # 主格式（EnumClipboardFormats 的第一个，通常是应用首选格式）
        "format_type": format_type,
        # 所有格式号，只用于判断类型，不含内容
        "formats": normalized,
        "is_text": bool(is_text),
        # 文本格式的 GlobalSize 字节数（含结尾 NUL）；非文本格式为 None
        "length": safe_length,
        # 同格式签名 + 长度在 60s 内算重复，避免连续复制同一份东西刷屏
        "fingerprint": f"clipboard:{format_type}:{signature}:{safe_length}",
    }


def _collect_event() -> dict | None:
    """**调用方必须先 OpenClipboard**。只枚举格式、取文本长度，不读内容。"""
    formats: list[int] = []
    fmt = 0
    while len(formats) < MAX_FORMATS:
        fmt = int(_EnumClipboardFormats(fmt))
        if fmt == 0:
            break
        formats.append(fmt)
    if not formats:
        return None

    primary = formats[0]
    is_text = any(f in TEXT_FORMATS for f in formats)
    length = None
    if is_text:
        for candidate in formats:
            if candidate not in TEXT_FORMATS:
                continue
            handle = _GetClipboardData(candidate)
            if handle:
                size = int(_GlobalSize(handle))
                if size > 0:
                    length = size
            break

    return build_event(
        formats=formats,
        format_type=format_name(primary),
        is_text=is_text,
        length=length,
    )


# ---------------------------------------------------------------------------
# watcher
# ---------------------------------------------------------------------------


class ClipboardWatcher:
    """剪贴板事件 watcher。``start()`` / ``stop()`` 均可重复调用。"""

    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hwnd = None
        self._wndproc = None  # 必须挂实例上，防止回调被 GC
        self._class_name = f"FireLoopClipboardWatcher_{os.getpid()}_{id(self)}"
        self._class_registered = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="fireloop-clipboard-watch",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(3.0):
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="clipboard",
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
                watcher="clipboard",
                stage="stop_timeout",
            )

    # ---------- 线程内 ----------

    def _run(self) -> None:
        try:
            self._thread_id = int(_GetCurrentThreadId())
            hinstance = _GetModuleHandleW(None)
            self._wndproc = _WNDPROC(self._on_message)
            window_class = WNDCLASSW()
            window_class.style = 0
            window_class.lpfnWndProc = self._wndproc
            window_class.cbClsExtra = 0
            window_class.cbWndExtra = 0
            window_class.hInstance = hinstance
            window_class.hIcon = None
            window_class.hCursor = None
            window_class.hbrBackground = None
            window_class.lpszMenuName = None
            window_class.lpszClassName = self._class_name

            if not _RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._class_registered = True

            hwnd = _CreateWindowExW(
                0,
                self._class_name,
                "FireLoopClipboardWatcher",
                0,
                0,
                0,
                0,
                0,
                HWND_MESSAGE,
                None,
                hinstance,
                None,
            )
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hwnd = hwnd

            # 强制建出线程消息队列，保证 stop() 的 PostThreadMessageW 能生效。
            msg = wintypes.MSG()
            _PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

            if not _AddClipboardFormatListener(hwnd):
                raise ctypes.WinError(ctypes.get_last_error())
            log.log_event("watcher_started", watcher="clipboard")
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
                watcher="clipboard",
                stage="run",
                error=repr(exc),
            )
        finally:
            self._ready.set()
            self._cleanup()

    def _cleanup(self) -> None:
        hwnd, self._hwnd = self._hwnd, None
        if hwnd:
            _RemoveClipboardFormatListener(hwnd)
            _DestroyWindow(hwnd)
        if self._class_registered:
            _UnregisterClassW(self._class_name, _GetModuleHandleW(None))
            self._class_registered = False
        log.log_event("watcher_stopped", watcher="clipboard")

    def _on_message(self, hwnd, message, wparam, lparam) -> int:
        try:
            if message == WM_CLIPBOARDUPDATE:
                self._handle_update()
                return 0
            if message == WM_DESTROY:
                _PostQuitMessage(0)
                return 0
        except Exception as exc:  # 窗口过程绝不能把异常抛回 C
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="clipboard",
                stage="wndproc",
                error=repr(exc),
            )
            return 0
        return int(_DefWindowProcW(hwnd, message, wparam, lparam))

    def _handle_update(self) -> None:
        opened = False
        for _ in range(5):
            if _OpenClipboard(self._hwnd):
                opened = True
                break
            time.sleep(0.01)  # 别的程序可能正占着剪贴板，短暂重试
        if not opened:
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="clipboard",
                stage="open_clipboard",
            )
            return
        try:
            event = _collect_event()
        finally:
            _CloseClipboard()
        if event is not None:
            self.bus.publish(event)
