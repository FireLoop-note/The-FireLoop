"""文件事件 watcher：ctypes 直调 ``ReadDirectoryChangesW``（叠加 I/O + 事件等待）。

只监听配置白名单里的目录（``events.file.watch_dirs``），**不递归子目录**，
不读文件内容，只把"路径 + 变化类型"变成事件交给 EventBus：

- created / modified / deleted / renamed

线程模型：每个目录一个 daemon 线程。``ReadDirectoryChangesW`` 用 overlapped 方式
发起，线程阻塞在 ``WaitForMultipleObjects`` 上同时等 I/O 完成和停止事件；
``stop()`` 置停止事件后 ``CancelIoEx`` 取消挂起 I/O，因此不会挂死。

目录不存在：记一条 ``watcher_error``（level=warning）后跳过，不影响其它目录。
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import log

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---- Win32 常量 ----
FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OVERLAPPED = 0x40000000

FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010

NOTIFY_FILTER = (
    FILE_NOTIFY_CHANGE_FILE_NAME
    | FILE_NOTIFY_CHANGE_DIR_NAME
    | FILE_NOTIFY_CHANGE_SIZE
    | FILE_NOTIFY_CHANGE_LAST_WRITE
)

FILE_ACTION_ADDED = 1
FILE_ACTION_REMOVED = 2
FILE_ACTION_MODIFIED = 3
FILE_ACTION_RENAMED_OLD_NAME = 4
FILE_ACTION_RENAMED_NEW_NAME = 5

ACTION_TO_KIND = {
    FILE_ACTION_ADDED: "created",
    FILE_ACTION_REMOVED: "deleted",
    FILE_ACTION_MODIFIED: "modified",
    FILE_ACTION_RENAMED_OLD_NAME: "renamed",
    FILE_ACTION_RENAMED_NEW_NAME: "renamed",
}

# ReadDirectoryChangesW 缓冲区：64KB 够放一大批通知；溢出（返回 0 字节）会记日志。
BUFFER_SIZE = 64 * 1024
DEFAULT_STOP_TIMEOUT = 3.0

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
ERROR_OPERATION_ABORTED = 995


class OVERLAPPED(ctypes.Structure):
    """Win32 OVERLAPPED（只用到 hEvent；64 位下前两个字段是 c_void_p）。"""

    _fields_ = (
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    )


_POVERLAPPED = ctypes.POINTER(OVERLAPPED)

_CreateFileW = _kernel32.CreateFileW
_CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
_CreateFileW.restype = wintypes.HANDLE

_ReadDirectoryChangesW = _kernel32.ReadDirectoryChangesW
_ReadDirectoryChangesW.argtypes = (
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
    wintypes.LPDWORD,
    _POVERLAPPED,
    wintypes.LPVOID,
)
_ReadDirectoryChangesW.restype = wintypes.BOOL

_GetOverlappedResult = _kernel32.GetOverlappedResult
_GetOverlappedResult.argtypes = (
    wintypes.HANDLE,
    _POVERLAPPED,
    wintypes.LPDWORD,
    wintypes.BOOL,
)
_GetOverlappedResult.restype = wintypes.BOOL

_CancelIoEx = _kernel32.CancelIoEx
_CancelIoEx.argtypes = (wintypes.HANDLE, _POVERLAPPED)
_CancelIoEx.restype = wintypes.BOOL

_CreateEventW = _kernel32.CreateEventW
_CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_CreateEventW.restype = wintypes.HANDLE

_SetEvent = _kernel32.SetEvent
_SetEvent.argtypes = (wintypes.HANDLE,)
_SetEvent.restype = wintypes.BOOL

_ResetEvent = _kernel32.ResetEvent
_ResetEvent.argtypes = (wintypes.HANDLE,)
_ResetEvent.restype = wintypes.BOOL

_WaitForSingleObject = _kernel32.WaitForSingleObject
_WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_WaitForSingleObject.restype = wintypes.DWORD

_WaitForMultipleObjects = _kernel32.WaitForMultipleObjects
_WaitForMultipleObjects.argtypes = (
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.BOOL,
    wintypes.DWORD,
)
_WaitForMultipleObjects.restype = wintypes.DWORD

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL

# FILE_NOTIFY_INFORMATION 头：NextEntryOffset / Action / FileNameLength（都是 DWORD）
_NOTIFY_HEADER = struct.Struct("<III")
_NOTIFY_HEADER_BYTES = _NOTIFY_HEADER.size  # 12
_NAME_OFFSET = _NOTIFY_HEADER_BYTES


def _event(kind: str, path: str, **extra) -> dict:
    """构造一条文件事件；fingerprint = 路径 + 变化类型，供总线去重。"""
    event = {
        "source": "file",
        "kind": kind,
        "path": path,
        "fingerprint": f"file:{path}:{kind}",
    }
    event.update(extra)
    return event


def parse_notifications(buffer: bytes, watch_dir: str) -> list[dict]:
    """解析 ``ReadDirectoryChangesW`` 返回的 FILE_NOTIFY_INFORMATION 列表。

    纯函数（不碰 Win32、不做 IO），便于单测。返回事件 dict 列表：

    - created / deleted / modified：``{"path": 完整路径}``
    - renamed：``{"path": 新路径, "old_path": 旧路径}``（同一次通知里能配上对时）；
      配对不上的记录也会退化成单条 renamed 事件，一个通知都不丢。
    """
    events: list[dict] = []
    total = len(buffer)
    offset = 0
    pending_old: str | None = None

    def flush_pending() -> None:
        nonlocal pending_old
        if pending_old is not None:
            events.append(_event("renamed", pending_old))
            pending_old = None

    while offset + _NOTIFY_HEADER_BYTES <= total:
        next_offset, action, name_len = _NOTIFY_HEADER.unpack_from(buffer, offset)
        name_start = offset + _NAME_OFFSET
        if name_len <= 0 or name_start + name_len > total:
            break
        name = buffer[name_start : name_start + name_len].decode(
            "utf-16-le", errors="replace"
        )
        full_path = os.path.join(watch_dir, name)

        if action == FILE_ACTION_RENAMED_OLD_NAME:
            flush_pending()
            pending_old = full_path
        elif action == FILE_ACTION_RENAMED_NEW_NAME:
            if pending_old is not None:
                events.append(_event("renamed", full_path, old_path=pending_old))
                pending_old = None
            else:
                events.append(_event("renamed", full_path))
        elif action in ACTION_TO_KIND:
            flush_pending()
            events.append(_event(ACTION_TO_KIND[action], full_path))
        # 未知 action：跳过，不猜
        if next_offset == 0:
            break
        offset += next_offset

    flush_pending()
    return events


def _resolve_dir(raw) -> str | None:
    """展开环境变量/相对路径并确认目录存在；不存在记 warning 返回 None。"""
    try:
        text = os.path.expandvars(os.path.expanduser(str(raw)))
        path = Path(text)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
    except (OSError, ValueError) as exc:
        log.log_event(
            "watcher_error",
            level="warning",
            watcher="file",
            stage="resolve_dir",
            dir=str(raw),
            error=repr(exc),
        )
        return None
    if not path.is_dir():
        log.log_event(
            "watcher_error",
            level="warning",
            watcher="file",
            stage="watch_dir",
            dir=str(path),
            error="目录不存在，跳过",
        )
        return None
    return str(path)


def _open_directory(directory: str):
    handle = _CreateFileW(
        directory,
        FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED,
        None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        log.log_event(
            "watcher_error",
            level="warning",
            watcher="file",
            stage="open_dir",
            dir=directory,
            winerror=ctypes.get_last_error(),
        )
        return None
    return handle


class FileWatcher:
    """文件事件 watcher。``start()`` / ``stop()`` 均可重复调用。"""

    def __init__(self, watch_dirs, bus):
        self._raw_dirs = [d for d in (watch_dirs or [])]
        self.bus = bus
        self._lock = threading.Lock()
        self._started = False
        self._stop_handle = None
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._threads = []
            self._stop_handle = _CreateEventW(None, True, False, None)
            if not self._stop_handle:
                self._started = False
                log.log_event(
                    "watcher_error",
                    level="warning",
                    watcher="file",
                    stage="create_stop_event",
                    winerror=ctypes.get_last_error(),
                )
                return

            directories: list[str] = []
            for raw in self._raw_dirs:
                resolved = _resolve_dir(raw)
                if resolved is not None and resolved not in directories:
                    directories.append(resolved)

            if not directories:
                log.log_event(
                    "watcher_idle",
                    watcher="file",
                    reason="no_watch_dirs",
                    configured=len(self._raw_dirs),
                )
                self._started = False
                _CloseHandle(self._stop_handle)
                self._stop_handle = None
                return

            for index, directory in enumerate(directories):
                thread = threading.Thread(
                    target=self._watch_loop,
                    args=(directory, self._stop_handle),
                    name=f"fireloop-file-watch-{index}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

        log.log_event(
            "watcher_started",
            watcher="file",
            dirs=directories,
            threads=len(directories),
        )

    def stop(self, timeout: float = DEFAULT_STOP_TIMEOUT) -> None:
        with self._lock:
            threads = list(self._threads)
            self._threads = []
            stop_handle = self._stop_handle
            self._stop_handle = None
            self._started = False
            if stop_handle:
                _SetEvent(stop_handle)

        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(remaining)
        alive = [t.name for t in threads if t.is_alive()]

        if alive:
            # 线程还挂在 WaitForMultipleObjects 上，此时不能关 stop_handle，
            # 否则会把等待它的线程搞坏。句柄留着当兜底（daemon 线程不阻塞进程退出）。
            log.log_event(
                "watcher_error",
                level="warning",
                watcher="file",
                stage="stop_timeout",
                threads=alive,
            )
        elif stop_handle:
            _CloseHandle(stop_handle)

        if threads and not alive:
            log.log_event("watcher_stopped", watcher="file", threads=len(threads))

    # ---------- 每个目录一条线程 ----------

    def _watch_loop(self, directory: str, stop_handle) -> None:
        handle = _open_directory(directory)
        if handle is None:
            return
        io_event = None
        overlapped = OVERLAPPED()
        try:
            io_event = _CreateEventW(None, True, False, None)
            if not io_event:
                log.log_event(
                    "watcher_error",
                    level="warning",
                    watcher="file",
                    stage="create_io_event",
                    dir=directory,
                    winerror=ctypes.get_last_error(),
                )
                return
            ctypes.memset(ctypes.byref(overlapped), 0, ctypes.sizeof(overlapped))
            overlapped.hEvent = io_event
            buffer = ctypes.create_string_buffer(BUFFER_SIZE)
            handles = (wintypes.HANDLE * 2)(stop_handle, io_event)
            overlapped_ptr = ctypes.byref(overlapped)

            while True:
                # 先看停止请求，避免 stop() 之后又发起一次新的 I/O。
                if _WaitForSingleObject(stop_handle, 0) == WAIT_OBJECT_0:
                    break
                _ResetEvent(io_event)
                if not _ReadDirectoryChangesW(
                    handle,
                    buffer,
                    BUFFER_SIZE,
                    False,
                    NOTIFY_FILTER,
                    None,
                    overlapped_ptr,
                    None,
                ):
                    winerror = ctypes.get_last_error()
                    if winerror != ERROR_OPERATION_ABORTED:
                        log.log_event(
                            "watcher_error",
                            level="warning",
                            watcher="file",
                            stage="read",
                            dir=directory,
                            winerror=winerror,
                        )
                    break

                result = _WaitForMultipleObjects(2, handles, False, INFINITE)
                if result == WAIT_OBJECT_0:
                    # 停止信号：取消挂起的 I/O，等它落定再关句柄。
                    _CancelIoEx(handle, overlapped_ptr)
                    nbytes = wintypes.DWORD(0)
                    _GetOverlappedResult(
                        handle, overlapped_ptr, ctypes.byref(nbytes), True
                    )
                    break
                if result == WAIT_OBJECT_0 + 1:
                    nbytes = wintypes.DWORD(0)
                    if not _GetOverlappedResult(
                        handle, overlapped_ptr, ctypes.byref(nbytes), False
                    ):
                        winerror = ctypes.get_last_error()
                        if winerror != ERROR_OPERATION_ABORTED:
                            log.log_event(
                                "watcher_error",
                                level="warning",
                                watcher="file",
                                stage="overlapped_result",
                                dir=directory,
                                winerror=winerror,
                            )
                        break
                    if nbytes.value <= 0:
                        # 返回 0 字节 = 变更缓冲区溢出（事件太密集），只能记日志继续。
                        log.log_event(
                            "watcher_error",
                            level="warning",
                            watcher="file",
                            stage="buffer_overflow",
                            dir=directory,
                        )
                        continue
                    for event in parse_notifications(
                        buffer.raw[: nbytes.value], directory
                    ):
                        self.bus.publish(event)
                else:
                    log.log_event(
                        "watcher_error",
                        level="warning",
                        watcher="file",
                        stage="wait",
                        dir=directory,
                        wait_result=int(result),
                    )
                    break
        except Exception as exc:  # 单目录线程异常不能拖垮整个 watcher
            log.log_event(
                "watcher_error",
                level="error",
                watcher="file",
                stage="loop",
                dir=directory,
                error=repr(exc),
            )
        finally:
            if io_event:
                _CloseHandle(io_event)
            _CloseHandle(handle)
