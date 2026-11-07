"""Fire Loop 命名管道服务端（ctypes 直调 Win32，零第三方依赖）。

线协议见 docs/协议-v1.md，M1a 约定：

- 短连接：一个连接先做 hello token 鉴权，再处理若干请求，然后断开，回到 accept；
- 首帧必须是合法 hello 且 token 与构造参数一致，否则回 ``bad_token`` 并断开；
- 请求只认 ping / task；task 只回显，不执行、不落盘；
- 单行上限 ``protocol.MAX_LINE_BYTES``，超限回 ``too_large`` 并断开；
- 单个连接里的任何异常只断开该连接并记日志，serve_forever 循环绝不崩；
- 日志不出现任务原文，只记 ``protocol.text_fingerprint``。

实现要点：内核对象全部用 ctypes 直调 kernel32；句柄由本类自己管理，
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


class _ConnectionClosed(Exception):
    """连接已断开（客户端关闭 / 管道错误）。winerror=0 表示正常 EOF。"""

    def __init__(self, winerror: int = 0):
        super().__init__(f"connection closed (winerror={winerror})")
        self.winerror = winerror


class _LineTooLarge(Exception):
    """单行超过 protocol.MAX_LINE_BYTES。"""


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
# 服务端
# ---------------------------------------------------------------------------


class PipeServer:
    """命名管道服务端：串行 accept 的短连接模型。

    Args:
        pipe_name: 管道全名，默认 protocol.PIPE_NAME。
        token: hello 首帧必须携带的 token（由 paths.ensure_token() 提供）。
    """

    def __init__(self, pipe_name: str = protocol.PIPE_NAME, token: str | None = None):
        if not isinstance(pipe_name, str) or not pipe_name:
            raise ValueError("pipe_name 不能为空")
        if not isinstance(token, str) or not token:
            raise ValueError("token 不能为空")
        self.pipe_name = pipe_name
        self.token = token
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

    def _authenticate(self, handle, buffer: bytearray) -> bool:
        """校验首帧 hello。失败时已回错误帧，返回 False。"""
        try:
            line = _read_line(handle, buffer)
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
