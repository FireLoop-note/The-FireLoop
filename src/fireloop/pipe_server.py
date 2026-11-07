"""Fire Loop 命名管道服务端（ctypes 直调 Win32，零第三方依赖）。

线协议见 docs/协议-v1.md，M1a/M1b 约定：

- 短连接：一个连接先做调用方 SID 校验（M1b），再 hello token 鉴权，然后处理若干请求，
  最后断开，回到 accept；
- 调用方 SID 不是当前用户（M1b）→ 回 ``unauthorized`` 并断开；
- 首帧必须是合法 hello 且 token 与构造参数一致，否则回 ``bad_token`` 并断开；
- 首帧等待最多 ``protocol.HELLO_TIMEOUT_MS``，用 PeekNamedPipe 轮询 + deadline，
  绝不阻塞 ReadFile 干等；超时回错误帧并断开；
- 请求只认 ping / task；task 只回显，不执行、不落盘；
- 单行上限 ``protocol.MAX_LINE_BYTES``，超限回 ``too_large`` 并断开；
- 单个连接里的任何异常只断开该连接并记日志，serve_forever 循环绝不崩；
- 日志不出现任务原文，只记 ``protocol.text_fingerprint``；
- 日志事件含 ``unauthorized`` / ``hello_timeout``。

实现要点：内核对象全部用 ctypes 直调 kernel32 / advapi32；句柄由本类自己管理，
阻塞在 ``ConnectNamedPipe`` 时可通过 stop() 的"自我连接"唤醒（见 stop 注释）。
"""

from __future__ import annotations

import ctypes
import hmac
import os
import threading
import time
from ctypes import wintypes

from . import APP_NAME, __version__, log, protocol

# ---- Win32 常量 ----
PIPE_ACCESS_DUPLEX = 0x00000003

PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255

IN_BUFFER_BYTES = protocol.MAX_LINE_BYTES
OUT_BUFFER_BYTES = protocol.MAX_LINE_BYTES
DEFAULT_TIMEOUT_MS = 0

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_BUSY = 231
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535

# 这些错误码说明"连接已经没了"，按客户端正常关闭处理。
_EOF_WINERRORS = frozenset({0, ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED, ERROR_NO_DATA})

READ_CHUNK = 8192

# ---- M1b：调用方身份（SID）校验 + 首帧 hello 超时 ----
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x00000008
TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser
ERROR_INSUFFICIENT_BUFFER = 122
HELLO_POLL_INTERVAL = 0.01  # 首帧 PeekNamedPipe 轮询间隔（秒）

# 64 位下 c_void_p(-1) 是 0xFFFFFFFFFFFFFFFF；CreateNamedPipeW/CreateFileW 失败都返回它。
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CreateNamedPipeW = _kernel32.CreateNamedPipeW
_CreateNamedPipeW.argtypes = (
    wintypes.LPCWSTR,  # lpName
    wintypes.DWORD,  # dwOpenMode
    wintypes.DWORD,  # dwPipeMode
    wintypes.DWORD,  # nMaxInstances
    wintypes.DWORD,  # nOutBufferSize
    wintypes.DWORD,  # nInBufferSize
    wintypes.DWORD,  # nDefaultTimeOut
    wintypes.LPVOID,  # lpSecurityAttributes
)
_CreateNamedPipeW.restype = wintypes.HANDLE

_ConnectNamedPipe = _kernel32.ConnectNamedPipe
_ConnectNamedPipe.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
_ConnectNamedPipe.restype = wintypes.BOOL

_ReadFile = _kernel32.ReadFile
_ReadFile.argtypes = (
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPVOID,
)
_ReadFile.restype = wintypes.BOOL

_WriteFile = _kernel32.WriteFile
_WriteFile.argtypes = (
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPVOID,
)
_WriteFile.restype = wintypes.BOOL

_FlushFileBuffers = _kernel32.FlushFileBuffers
_FlushFileBuffers.argtypes = (wintypes.HANDLE,)
_FlushFileBuffers.restype = wintypes.BOOL

_DisconnectNamedPipe = _kernel32.DisconnectNamedPipe
_DisconnectNamedPipe.argtypes = (wintypes.HANDLE,)
_DisconnectNamedPipe.restype = wintypes.BOOL

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL

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

# ---- M1b：SID 校验 / hello 超时用到的内核调用 ----

_GetCurrentProcess = _kernel32.GetCurrentProcess
_GetCurrentProcess.argtypes = ()
_GetCurrentProcess.restype = wintypes.HANDLE

_OpenProcess = _kernel32.OpenProcess
_OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_OpenProcess.restype = wintypes.HANDLE

_GetNamedPipeClientProcessId = _kernel32.GetNamedPipeClientProcessId
_GetNamedPipeClientProcessId.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
_GetNamedPipeClientProcessId.restype = wintypes.BOOL

_PeekNamedPipe = _kernel32.PeekNamedPipe
_PeekNamedPipe.argtypes = (
    wintypes.HANDLE,  # hNamedPipe
    wintypes.LPVOID,  # lpBuffer（传 NULL 只查可读字节数）
    wintypes.DWORD,  # nBufferSize
    wintypes.LPDWORD,  # lpBytesRead
    wintypes.LPDWORD,  # lpTotalBytesAvail
    wintypes.LPDWORD,  # lpBytesLeftThisMessage
)
_PeekNamedPipe.restype = wintypes.BOOL

_LocalFree = _kernel32.LocalFree
_LocalFree.argtypes = (ctypes.c_void_p,)
_LocalFree.restype = ctypes.c_void_p

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_OpenProcessToken = _advapi32.OpenProcessToken
_OpenProcessToken.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
)
_OpenProcessToken.restype = wintypes.BOOL

_GetTokenInformation = _advapi32.GetTokenInformation
_GetTokenInformation.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_GetTokenInformation.restype = wintypes.BOOL

_ConvertSidToStringSidW = _advapi32.ConvertSidToStringSidW
_ConvertSidToStringSidW.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
_ConvertSidToStringSidW.restype = wintypes.BOOL


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _ConnectionClosed(Exception):
    """连接已断开（客户端关闭 / 管道错误）。winerror=0 表示正常 EOF。"""

    def __init__(self, winerror: int = 0):
        super().__init__(f"connection closed (winerror={winerror})")
        self.winerror = winerror


class _LineTooLarge(Exception):
    """单行超过 protocol.MAX_LINE_BYTES。"""


class _HelloTimeout(Exception):
    """首帧 hello 在 protocol.HELLO_TIMEOUT_MS 内没到齐。"""


def _is_invalid_handle(handle) -> bool:
    return not handle or handle == INVALID_HANDLE_VALUE


def _token_equal(given: str, expected: str) -> bool:
    """常数时间比较 token；客户端可能发来任意 unicode，先转 bytes 避免 TypeError。"""
    try:
        return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
    except (UnicodeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 低层管道 IO（模块级函数，方便单测时单独验证行为）
# ---------------------------------------------------------------------------


def _create_pipe_instance(pipe_name: str):
    """创建一个未连接的命名管道实例；失败返回 None。"""
    handle = _CreateNamedPipeW(
        pipe_name,
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        PIPE_UNLIMITED_INSTANCES,
        OUT_BUFFER_BYTES,
        IN_BUFFER_BYTES,
        DEFAULT_TIMEOUT_MS,
        None,
    )
    if _is_invalid_handle(handle):
        return None
    return handle


def _wait_for_client(handle) -> bool:
    """阻塞等客户端连上来。返回 False 表示这次 accept 失败、应重建实例重试。"""
    if _ConnectNamedPipe(handle, None):
        return True
    winerror = ctypes.get_last_error()
    if winerror in (ERROR_PIPE_CONNECTED, ERROR_NO_DATA):
        # 客户端在我调用 ConnectNamedPipe 之前就连上了（或连上又立刻退了），
        # 按"已连接"处理，后面的 ReadFile 会读到 EOF。
        return True
    log.log_event("error", level="warning", stage="connect", winerror=winerror)
    return False


def _disconnect_and_close(handle) -> None:
    if _is_invalid_handle(handle):
        return
    # 客户端可能已经先关了，DisconnectNamedPipe 失败不用管。
    _DisconnectNamedPipe(handle)
    _CloseHandle(handle)


def _read_line(handle, buffer: bytearray) -> bytes:
    """从管道读一行（不含 \\n）。超长抛 _LineTooLarge，断开抛 _ConnectionClosed。"""
    while True:
        newline_at = buffer.find(b"\n")
        if newline_at >= 0:
            # 含换行符的整行字节数必须 <= 上限（与 protocol.encode 的判定一致）。
            if newline_at + 1 > protocol.MAX_LINE_BYTES:
                raise _LineTooLarge()
            line = bytes(buffer[:newline_at])
            del buffer[: newline_at + 1]
            return line
        if len(buffer) > protocol.MAX_LINE_BYTES:
            raise _LineTooLarge()

        chunk = ctypes.create_string_buffer(READ_CHUNK)
        nread = wintypes.DWORD(0)
        if not _ReadFile(handle, chunk, READ_CHUNK, ctypes.byref(nread), None):
            raise _ConnectionClosed(ctypes.get_last_error())
        if nread.value == 0:
            raise _ConnectionClosed(0)
        buffer += chunk.raw[: nread.value]


def _peek_available(handle) -> int | None:
    """PeekNamedPipe 查管道里可读字节数；None 表示连接已断开。"""
    avail = wintypes.DWORD(0)
    if not _PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None):
        return None
    return int(avail.value)


def _read_chunk(handle, size: int) -> bytes:
    """读走当前已就绪的 size 字节（调用前已 Peek 过，不会阻塞）。"""
    size = max(1, int(size))
    chunk = ctypes.create_string_buffer(size)
    nread = wintypes.DWORD(0)
    if not _ReadFile(handle, chunk, size, ctypes.byref(nread), None):
        raise _ConnectionClosed(ctypes.get_last_error())
    if nread.value == 0:
        raise _ConnectionClosed(0)
    return chunk.raw[: nread.value]


def _read_line_with_timeout(handle, buffer: bytearray, timeout_ms: int) -> bytes:
    """带 deadline 地读一行（首帧 hello 专用）。

    用 PeekNamedPipe 轮询"有没有数据"，有数据才 ReadFile，绝不在没数据时阻塞 ReadFile
    干等。超时抛 _HelloTimeout，超长抛 _LineTooLarge，断开抛 _ConnectionClosed。
    """
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
    while True:
        newline_at = buffer.find(b"\n")
        if newline_at >= 0:
            if newline_at + 1 > protocol.MAX_LINE_BYTES:
                raise _LineTooLarge()
            line = bytes(buffer[:newline_at])
            del buffer[: newline_at + 1]
            return line
        if len(buffer) > protocol.MAX_LINE_BYTES:
            raise _LineTooLarge()

        avail = _peek_available(handle)
        if avail is None:
            raise _ConnectionClosed(ctypes.get_last_error())
        if avail == 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _HelloTimeout()
            time.sleep(min(HELLO_POLL_INTERVAL, remaining))
            continue
        buffer += _read_chunk(handle, avail)


def _send_frame(handle, msg: dict, flush: bool = False) -> None:
    """编码并写出一帧。

    flush=True 只用于"写完就断开"的错误帧：FlushFileBuffers 会阻塞到客户端把数据
    读走，不刷的话 DisconnectNamedPipe/CloseHandle 可能丢掉错误帧。正常响应不 flush，
    因为服务端要等客户端自己关连接，数据一定在缓冲区里等着被读。
    """
    data = protocol.encode(msg)
    buf = ctypes.create_string_buffer(data, len(data))
    written = wintypes.DWORD(0)
    if not _WriteFile(handle, buf, len(data), ctypes.byref(written), None):
        raise _ConnectionClosed(ctypes.get_last_error())
    if written.value != len(data):
        raise _ConnectionClosed(ERROR_PIPE_NOT_CONNECTED)
    if flush:
        # 客户端已经关闭时这里会失败，忽略即可。
        _FlushFileBuffers(handle)


def _try_connect_once(pipe_name: str) -> bool:
    """以客户端身份连一下自己的管道（用于唤醒阻塞在 ConnectNamedPipe 的 serve 线程）。"""
    handle = _CreateFileW(
        pipe_name,
        GENERIC_READ | GENERIC_WRITE,
        0,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if _is_invalid_handle(handle):
        return False
    _CloseHandle(handle)
    return True


# ---------------------------------------------------------------------------
# M1b：调用方 SID 校验
# ---------------------------------------------------------------------------


def _token_user_sid(token_handle) -> str | None:
    """从已打开的进程令牌取用户 SID 字符串；任何失败返回 None（不抛异常）。"""
    size = wintypes.DWORD(0)
    # 第一次调用只为拿 SIZE；预期返回失败 + ERROR_INSUFFICIENT_BUFFER。
    _GetTokenInformation(token_handle, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
    if not size.value:
        return None

    buf = ctypes.create_string_buffer(size.value)
    if not _GetTokenInformation(
        token_handle, TOKEN_USER_CLASS, buf, size.value, ctypes.byref(size)
    ):
        return None

    token_user = ctypes.cast(buf, ctypes.POINTER(_TOKEN_USER)).contents
    sid = token_user.User.Sid
    if not sid:
        return None

    sid_text = wintypes.LPWSTR()
    if not _ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
        return None
    try:
        return sid_text.value
    finally:
        _LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))


def _current_user_sid() -> str | None:
    """当前进程（agent）用户的 SID 字符串；拿不到返回 None。"""
    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        return _token_user_sid(token)
    finally:
        if token.value:
            _CloseHandle(token)


def _process_user_sid(pid: int) -> str | None:
    """指定进程用户的 SID 字符串；进程打不开（权限/已退出）返回 None。"""
    pid = int(pid or 0)
    if pid <= 0:
        return None
    process = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if _is_invalid_handle(process):
        return None
    token = wintypes.HANDLE()
    try:
        if not _OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return None
        return _token_user_sid(token)
    finally:
        if token.value:
            _CloseHandle(token)
        _CloseHandle(process)


def _client_user_sid(pipe_handle) -> str | None:
    """管道调用方（客户端进程）的用户 SID；拿不到返回 None（调用方按未授权处理）。"""
    client_pid = wintypes.DWORD(0)
    if not _GetNamedPipeClientProcessId(pipe_handle, ctypes.byref(client_pid)):
        return None
    return _process_user_sid(int(client_pid.value))


def _sid_equal(left: str | None, right: str | None) -> bool:
    """比较两个 SID 字符串（None 一律视为不相等）。"""
    if not left or not right:
        return False
    return left.strip().upper() == right.strip().upper()


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------


class PipeServer:
    """命名管道服务端：串行 accept 的短连接模型。

    Args:
        pipe_name: 管道全名，默认 protocol.PIPE_NAME。
        token: hello 首帧必须携带的 token（由 paths.ensure_token() 提供）。
        validate_sid: 是否校验管道调用方 SID（默认 True，M1b 安全项）。
            测试可显式传 False 来隔离验证其它行为。
    """

    def __init__(
        self,
        pipe_name: str = protocol.PIPE_NAME,
        token: str | None = None,
        validate_sid: bool = True,
    ):
        if not isinstance(pipe_name, str) or not pipe_name:
            raise ValueError("pipe_name 不能为空")
        if not isinstance(token, str) or not token:
            raise ValueError("token 不能为空")
        self.pipe_name = pipe_name
        self.token = token
        self.validate_sid = bool(validate_sid)
        self._current_sid: str | None = None  # 懒加载；进程生命周期内不变
        self._stop_event = threading.Event()
        self._serving = threading.Event()
        # 先在构造时就把第一个管道实例建出来：这样 agent 打印"已启动"之前，
        # 管道名已经可被客户端 CreateFile 连上，避免启动行与 accept 之间的竞态
        # （客户端连早了会拿到 ERROR_FILE_NOT_FOUND，CLI 就误报 agent 没在跑）。
        first_handle = _create_pipe_instance(self.pipe_name)
        if first_handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        self._first_handle = first_handle

    # ---------- 生命周期 ----------

    def serve_forever(self) -> None:
        """无限 accept 循环。KeyboardInterrupt 时清理句柄后正常 return。

        同一个管道实例在多次连接之间复用：ConnectNamedPipe → 处理 → DisconnectNamedPipe
        → 回到 ConnectNamedPipe。不复用、每连接重建实例的话，两个连接之间管道名会短暂
        消失，客户端（CLI）恰好在这时 CreateFileW 就会拿到 ERROR_FILE_NOT_FOUND，
        误报"agent 没在跑"。复用后管道名在整个服务期间一直存在。
        """
        log.log_event(
            "agent_start",
            pipe=self.pipe_name,
            pid=os.getpid(),
            version=__version__,
        )
        self._serving.set()
        handle, self._first_handle = self._first_handle, None
        reason = "stopped"
        try:
            while not self._stop_event.is_set():
                if handle is None:
                    handle = _create_pipe_instance(self.pipe_name)
                    if handle is None:
                        log.log_event(
                            "error",
                            level="error",
                            stage="create_pipe",
                            pipe=self.pipe_name,
                            winerror=ctypes.get_last_error(),
                        )
                        # 管道都建不出来属于致命错误（比如 pipe_name 非法），交给上层退出。
                        raise RuntimeError(f"CreateNamedPipeW 失败: pipe={self.pipe_name}")

                if not _wait_for_client(handle):
                    # 这个实例状态不对，关掉重建（正常短连接不会走到这里）。
                    if self._stop_event.is_set():
                        break
                    _CloseHandle(handle)
                    handle = None
                    time.sleep(0.05)
                    continue
                if self._stop_event.is_set():
                    break
                try:
                    self._handle_client(handle)
                except Exception as exc:  # 兜底：单连接异常绝不允许崩掉 accept 循环
                    log.log_event(
                        "error",
                        level="error",
                        stage="serve_connection",
                        error=repr(exc),
                    )

                # 断开但保留实例，下一轮在同一个句柄上重新 ConnectNamedPipe。
                if not _DisconnectNamedPipe(handle):
                    # 极少见：实例坏了就换一个（此时客户端会短暂看到管道名消失）。
                    log.log_event(
                        "error",
                        level="warning",
                        stage="disconnect",
                        winerror=ctypes.get_last_error(),
                    )
                    _CloseHandle(handle)
                    handle = None
                    if self._stop_event.is_set():
                        break
                    time.sleep(0.05)
        except KeyboardInterrupt:
            reason = "keyboard_interrupt"
        finally:
            if handle is not None:
                _disconnect_and_close(handle)
            pending, self._first_handle = self._first_handle, None
            if pending is not None:
                _disconnect_and_close(pending)
            self._serving.clear()
            self._stop_event.set()
            log.log_event("agent_stop", reason=reason, pipe=self.pipe_name)

    def stop(self, timeout: float = 2.0) -> None:
        """请求 serve_forever 退出。

        问题：serve_forever 通常阻塞在 ConnectNamedPipe，Python 层无法直接打断。
        做法：先置停止标志，再以客户端身份连接一下自己（``_try_connect_once``），
        ConnectNamedPipe 立刻返回，serve 线程看到标志后清理句柄退出。

        如果连接正被某个客户端占着（阻塞在 ReadFile），这里只能等它断开；
        M1a 是短连接，正常客户端很快会关。超时只记日志，不抛异常。
        """
        self._stop_event.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._serving.is_set():
                return
            if _try_connect_once(self.pipe_name):
                return
            time.sleep(0.01)
        log.log_event(
            "error",
            level="warning",
            stage="stop",
            message="stop() 超时：serve_forever 可能仍在处理已连接的客户端",
            pipe=self.pipe_name,
        )

    # ---------- 单连接处理 ----------

    def _handle_client(self, handle) -> None:
        """处理一个已连接的客户端；本函数内异常全部消化，绝不上抛。"""
        log.log_event("client_connected", pipe=self.pipe_name)
        buffer = bytearray()
        disconnect_reason = "client_closed"
        try:
            # M1b：先确认调用方与 agent 同用户，再读首帧 hello。
            if not self._check_client_sid(handle):
                disconnect_reason = "unauthorized"
                return
            if not self._authenticate(handle, buffer):
                disconnect_reason = "hello_rejected"
                return
            while True:
                try:
                    line = _read_line(handle, buffer)
                except _LineTooLarge:
                    self._reject(handle, None, protocol.CODE_TOO_LARGE, "单行超过上限")
                    log.log_event(
                        "error",
                        level="warning",
                        stage="read_line",
                        code=protocol.CODE_TOO_LARGE,
                        limit=protocol.MAX_LINE_BYTES,
                    )
                    disconnect_reason = "too_large"
                    return
                except _ConnectionClosed as exc:
                    if exc.winerror in _EOF_WINERRORS:
                        disconnect_reason = "client_closed"
                    else:
                        disconnect_reason = "io_error"
                        log.log_event(
                            "error",
                            level="warning",
                            stage="read",
                            winerror=exc.winerror,
                        )
                    return

                try:
                    msg = protocol.decode_line(line)
                except protocol.ProtocolError as exc:
                    self._reject(handle, None, exc.code, exc.message)
                    log.log_event(
                        "error",
                        level="warning",
                        stage="decode",
                        code=exc.code,
                    )
                    disconnect_reason = "protocol_error"
                    return

                try:
                    response = self._dispatch(msg)
                except protocol.ProtocolError as exc:
                    req_id = msg.get("id") if isinstance(msg.get("id"), str) else None
                    self._reject(handle, req_id, exc.code, exc.message)
                    log.log_event(
                        "error",
                        level="warning",
                        stage="dispatch",
                        code=exc.code,
                        msg_type=msg.get("type"),
                    )
                    disconnect_reason = "protocol_error"
                    return

                _send_frame(handle, response)
        except _ConnectionClosed as exc:
            disconnect_reason = "io_error"
            log.log_event("error", level="warning", stage="send", winerror=exc.winerror)
        except Exception as exc:  # 最后兜底，日志里带上类型
            disconnect_reason = "internal_error"
            log.log_event("error", level="error", stage="handle_client", error=repr(exc))
        finally:
            log.log_event(
                "client_disconnected",
                reason=disconnect_reason,
                pipe=self.pipe_name,
            )

    def _check_client_sid(self, handle) -> bool:
        """M1b：确认管道调用方与 agent 是同一个用户。

        取不到调用方 SID（OpenProcess/令牌被拒等）也按不通过处理（fail closed），
        回 ``unauthorized`` 后断开，并记 ``unauthorized`` 日志。
        """
        if not self.validate_sid:
            return True

        if self._current_sid is None:
            self._current_sid = _current_user_sid()
        if not self._current_sid:
            log.log_event(
                "unauthorized",
                level="warning",
                reason="current_sid_unavailable",
                pipe=self.pipe_name,
            )
            self._reject_unauthorized(handle)
            return False

        client_sid = _client_user_sid(handle)
        if not _sid_equal(client_sid, self._current_sid):
            log.log_event(
                "unauthorized",
                level="warning",
                reason="sid_mismatch",
                client_sid=client_sid,
                agent_sid=self._current_sid,
                pipe=self.pipe_name,
            )
            self._reject_unauthorized(handle)
            return False
        return True

    def _reject_unauthorized(self, handle) -> None:
        try:
            _send_frame(
                handle,
                protocol.make_error(
                    None, protocol.CODE_UNAUTHORIZED, "调用方不是当前用户，拒绝服务"
                ),
                flush=True,
            )
        except _ConnectionClosed:
            pass

    def _authenticate(self, handle, buffer: bytearray) -> bool:
        """校验首帧 hello。失败时已回错误帧，返回 False。"""
        try:
            line = _read_line_with_timeout(handle, buffer, protocol.HELLO_TIMEOUT_MS)
        except _HelloTimeout:
            log.log_event(
                "hello_timeout",
                level="warning",
                timeout_ms=protocol.HELLO_TIMEOUT_MS,
                pipe=self.pipe_name,
            )
            try:
                self._reject(handle, None, protocol.CODE_BAD_TOKEN, "首帧 hello 超时")
            except _ConnectionClosed:
                pass
            return False
        except _LineTooLarge:
            log.log_event("hello_rejected", reason="first_frame_too_large", pipe=self.pipe_name)
            try:
                self._reject(handle, None, protocol.CODE_BAD_TOKEN, "首帧必须是 hello")
            except _ConnectionClosed:
                pass
            return False
        except _ConnectionClosed as exc:
            if exc.winerror in _EOF_WINERRORS:
                log.log_event("hello_rejected", reason="eof_before_hello", pipe=self.pipe_name)
            else:
                log.log_event(
                    "error",
                    level="warning",
                    stage="read_hello",
                    winerror=exc.winerror,
                )
            return False

        try:
            msg = protocol.decode_line(line)
        except protocol.ProtocolError as exc:
            # 契约：首帧必须是合法 hello，否则一律 bad_token 并断开。
            log.log_event(
                "hello_rejected",
                reason="invalid_first_frame",
                detail=exc.code,
                pipe=self.pipe_name,
            )
            try:
                self._reject(handle, None, protocol.CODE_BAD_TOKEN, "首帧必须是合法 hello")
            except _ConnectionClosed:
                pass
            return False

        if msg.get("type") != protocol.HELLO:
            log.log_event("hello_rejected", reason="not_hello", pipe=self.pipe_name)
            try:
                self._reject(handle, None, protocol.CODE_BAD_TOKEN, "首帧必须是 hello")
            except _ConnectionClosed:
                pass
            return False

        token = msg.get("token")
        if not isinstance(token, str) or not _token_equal(token, self.token):
            log.log_event("hello_rejected", reason="bad_token", pipe=self.pipe_name)
            try:
                self._reject(handle, None, protocol.CODE_BAD_TOKEN, "token 校验失败")
            except _ConnectionClosed:
                pass
            return False

        _send_frame(handle, protocol.make_hello_ok(APP_NAME, __version__))
        log.log_event("hello_ok", pipe=self.pipe_name)
        return True

    def _dispatch(self, msg: dict) -> dict:
        """把一条已解码的请求变成响应；协议问题抛 protocol.ProtocolError。"""
        mtype = msg.get("type")
        if mtype not in (protocol.PING, protocol.TASK):
            raise protocol.ProtocolError(
                protocol.CODE_UNKNOWN_TYPE, f"未知 type: {mtype!r}"
            )

        req_id = msg.get("id")
        if not isinstance(req_id, str) or not req_id:
            raise protocol.ProtocolError(
                protocol.CODE_BAD_MSG, "缺少 id（必须是非空字符串）"
            )

        if mtype == protocol.PING:
            log.log_event("request", type=protocol.PING, id=req_id)
            return protocol.make_ok(req_id, text="pong")

        text = msg.get("text")
        if not isinstance(text, str):
            raise protocol.ProtocolError(
                protocol.CODE_BAD_MSG, "task 缺 text，或 text 不是字符串"
            )
        if not text.strip():
            raise protocol.ProtocolError(protocol.CODE_BAD_MSG, "task.text 不能为空")
        if len(text) > protocol.MAX_TASK_TEXT:
            raise protocol.ProtocolError(
                protocol.CODE_TOO_LARGE,
                f"task.text 超过 {protocol.MAX_TASK_TEXT} 字符",
            )

        # 日志只记长度和 sha256 前 8 位，绝不出现任务原文。
        log.log_event(
            "request",
            type=protocol.TASK,
            id=req_id,
            **protocol.text_fingerprint(text),
        )
        return protocol.make_ok(req_id, text="收到任务", text_len=len(text))

    def _reject(self, handle, req_id, code: str, message: str = "") -> None:
        """回错误帧（写完就 flush，因为调用方随后会断开连接）。"""
        _send_frame(handle, protocol.make_error(req_id, code, message), flush=True)
