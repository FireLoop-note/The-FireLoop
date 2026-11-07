"""Fire Loop 客户端：ctypes 直调 Win32 命名管道，短连接完成一次请求/响应。

线协议见 docs/协议-v1.md。M1a 设计要点：

- 短连接：每次 call() 都走「连接 → hello 鉴权 → 发一条请求 → 读一条响应 → 关句柄」，
  不常驻、不重试、不推事件。
- 不生成 token：token 只由 agent 生成并落盘，客户端只读；读不到就当作 agent 没启动。
- 超时不挂死：所有等待都用 PeekNamedPipe 非阻塞轮询 + 单调时钟 deadline，
  不发起会无限阻塞的读调用；到点就抛 AgentTimeoutError 交给 CLI 退出。
  （不用「线程里阻塞读 + join」是为了不留下卡在 ReadFile 里的线程。）
- 句柄关闭放 finally，任何异常路径都不漏。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from . import paths, protocol

__all__ = [
    "AgentError",
    "AgentOfflineError",
    "AgentTimeoutError",
    "AgentProtocolError",
    "call",
]

# --- Win32 常量 -------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

ERROR_FILE_NOT_FOUND = 2
ERROR_BROKEN_PIPE = 109
ERROR_SEM_TIMEOUT = 121
ERROR_PIPE_BUSY = 231
ERROR_PIPE_NOT_CONNECTED = 233

# c_void_p 的 -1（64 位下 0xFFFF_FFFF_FFFF_FFFF）
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 轮询间隔：10ms 对 CLI 这种短命进程足够快，又不会空转烧 CPU
_POLL_INTERVAL_S = 0.01
# 单次 ReadFile 最多读这么多，防止服务端灌数据时一次分配过大
_MAX_READ_CHUNK = protocol.MAX_LINE_BYTES

# --- 内核调用原型（只声明用到的） -------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CreateFileW = kernel32.CreateFileW
_CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_CreateFileW.restype = wintypes.HANDLE

_WaitNamedPipeW = kernel32.WaitNamedPipeW
_WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_WaitNamedPipeW.restype = wintypes.BOOL

_ReadFile = kernel32.ReadFile
_ReadFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    ctypes.c_void_p,
]
_ReadFile.restype = wintypes.BOOL

_WriteFile = kernel32.WriteFile
_WriteFile.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    ctypes.c_void_p,
]
_WriteFile.restype = wintypes.BOOL

_PeekNamedPipe = kernel32.PeekNamedPipe
_PeekNamedPipe.argtypes = [
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
    wintypes.LPDWORD,
]
_PeekNamedPipe.restype = wintypes.BOOL

_CloseHandle = kernel32.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL


# --- 异常 -------------------------------------------------------------------


class AgentError(Exception):
    """客户端错误基类（CLI 按子类映射退出码）。"""


class AgentOfflineError(AgentError):
    """连不上 agent / agent 没启动过（CLI 退出码 1）。"""


class AgentTimeoutError(AgentError):
    """连接或等待响应超时（CLI 退出码 2）。"""


class AgentProtocolError(AgentError):
    """协议错误：服务端返回 ok=false 或帧结构不对（CLI 退出码 3）。

    code 取自 docs/协议-v1.md 第 5 节的错误码表。
    """

    def __init__(self, code: str, message: str = ""):
        self.code = str(code) if code is not None else protocol.CODE_INTERNAL
        self.message = message or ""
        super().__init__(f"{self.code}: {self.message}" if self.message else self.code)


# --- 内部小工具 -------------------------------------------------------------


def _close(handle: int | None) -> None:
    if handle and handle != INVALID_HANDLE_VALUE:
        _CloseHandle(handle)


def _write_all(handle: int, data: bytes, what: str) -> None:
    """把 data 全部写进管道。对端断开/写失败都按 agent 离线处理。"""
    total = len(data)
    if not total:
        return
    buf = ctypes.create_string_buffer(data, total)
    base = ctypes.addressof(buf)
    sent = 0
    while sent < total:
        written = wintypes.DWORD(0)
        ok = _WriteFile(
            handle,
            ctypes.c_void_p(base + sent),
            total - sent,
            ctypes.byref(written),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            raise AgentOfflineError(f"{what}失败，agent 可能已断开（Windows 错误 {err}）")
        if written.value == 0:
            raise AgentOfflineError(f"{what}失败：管道写入 0 字节")
        sent += written.value


def _peek_available(handle: int) -> int | None:
    """非阻塞查管道里可读字节数；对端已断开返回 None。

    传 1 字节的假 buffer 只为兼容部分实现（PeekNamedPipe 不消费数据）。
    """
    dummy = ctypes.create_string_buffer(1)
    avail = wintypes.DWORD(0)
    ok = _PeekNamedPipe(
        handle,
        ctypes.c_void_p(ctypes.addressof(dummy)),
        1,
        None,
        ctypes.byref(avail),
        None,
    )
    if not ok:
        err = ctypes.get_last_error()
        if err in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED):
            return None
        raise AgentOfflineError(f"读取管道状态失败（Windows 错误 {err}）")
    return avail.value


def _read_chunk(handle: int, size: int) -> bytes:
    """读走当前可读的数据（同步管道上最多读 size 字节，不会等满）。"""
    size = max(1, min(size, _MAX_READ_CHUNK))
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD(0)
    ok = _ReadFile(
        handle,
        ctypes.c_void_p(ctypes.addressof(buf)),
        size,
        ctypes.byref(read),
        None,
    )
    if not ok:
        err = ctypes.get_last_error()
        if err in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED):
            raise AgentOfflineError("agent 提前断开连接")
        raise AgentOfflineError(f"读取管道失败（Windows 错误 {err}）")
    return buf.raw[: read.value]


def _decode(raw: bytes, what: str) -> dict:
    try:
        return protocol.decode_line(raw)
    except protocol.ProtocolError as exc:
        raise AgentProtocolError(exc.code, exc.message) from exc


def _read_line(handle: int, timeout_ms: int, what: str) -> dict:
    """读一行 JSON（以 \\n 结尾），按 deadline 轮询；超时抛 AgentTimeoutError。

    不用阻塞 ReadFile，所以进程不可能卡死在这里。
    """
    timeout_ms = max(0, int(timeout_ms))
    deadline = time.monotonic() + timeout_ms / 1000.0
    buf = bytearray()
    while True:
        newline = buf.find(b"\n")
        if newline >= 0:
            if newline > protocol.MAX_LINE_BYTES:
                raise AgentProtocolError(
                    protocol.CODE_TOO_LARGE,
                    f"{what}单行超过 {protocol.MAX_LINE_BYTES} 字节",
                )
            return _decode(bytes(buf[:newline]), what)
        if len(buf) > protocol.MAX_LINE_BYTES:
            raise AgentProtocolError(
                protocol.CODE_TOO_LARGE,
                f"{what}单行超过 {protocol.MAX_LINE_BYTES} 字节",
            )

        avail = _peek_available(handle)
        if avail is None:
            raise AgentOfflineError(f"agent 在{what}前断开连接")
        if avail > 0:
            buf.extend(_read_chunk(handle, avail))
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentTimeoutError(f"等待{what}超时（{timeout_ms}ms）")
        time.sleep(min(_POLL_INTERVAL_S, remaining))


def _check_hello(frame: dict) -> None:
    mtype = frame.get("type")
    if mtype == protocol.HELLO_OK:
        return
    if mtype == protocol.RESULT and frame.get("ok") is False:
        raise AgentProtocolError(
            frame.get("code") or protocol.CODE_INTERNAL,
            str(frame.get("message") or "agent 拒绝首帧鉴权"),
        )
    raise AgentProtocolError(
        protocol.CODE_BAD_MSG,
        f"期望 hello_ok，收到 type={mtype!r}",
    )


def _check_result(frame: dict, req_id: object) -> dict:
    if frame.get("type") != protocol.RESULT:
        raise AgentProtocolError(
            protocol.CODE_BAD_MSG,
            f"期望 result，收到 type={frame.get('type')!r}",
        )
    if req_id is not None:
        rid = frame.get("id")
        if rid is not None and rid != req_id:
            raise AgentProtocolError(
                protocol.CODE_BAD_MSG,
                f"响应 id 不匹配：期望 {req_id!r}，收到 {rid!r}",
            )
    ok = frame.get("ok")
    if ok is True:
        return frame
    if ok is False:
        raise AgentProtocolError(
            frame.get("code") or protocol.CODE_INTERNAL,
            str(frame.get("message") or ""),
        )
    raise AgentProtocolError(protocol.CODE_BAD_MSG, "响应缺少 ok 字段")


# --- 连接 -------------------------------------------------------------------


def _connect(pipe_name: str, connect_timeout_ms: int) -> int:
    """打开命名管道客户端句柄。

    - 管道不存在（ERROR_FILE_NOT_FOUND）→ AgentOfflineError，立即返回；
    - 实例全忙（ERROR_PIPE_BUSY）→ WaitNamedPipeW 等剩余时间，再重试；
    - 等不到 → AgentTimeoutError；其他错误 → AgentOfflineError（带错误码）。
    """
    if not isinstance(pipe_name, str) or not pipe_name:
        raise AgentOfflineError("管道名无效")

    timeout_ms = max(0, int(connect_timeout_ms))
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        handle = _CreateFileW(
            pipe_name,
            GENERIC_READ | GENERIC_WRITE,
            0,  # 命名管道客户端不共享
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle and handle != INVALID_HANDLE_VALUE:
            return handle

        err = ctypes.get_last_error()
        if err == ERROR_FILE_NOT_FOUND:
            raise AgentOfflineError("管道不存在（Windows 错误 2）")
        if err != ERROR_PIPE_BUSY:
            raise AgentOfflineError(f"连接管道失败（Windows 错误 {err}）")

        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise AgentTimeoutError(f"连接管道超时（{timeout_ms}ms，管道一直忙）")
        if not _WaitNamedPipeW(pipe_name, remaining_ms):
            wait_err = ctypes.get_last_error()
            if wait_err == ERROR_SEM_TIMEOUT:
                raise AgentTimeoutError(f"连接管道超时（{timeout_ms}ms）")
            if wait_err == ERROR_FILE_NOT_FOUND:
                raise AgentOfflineError("管道不存在（等待时消失，Windows 错误 2）")
            raise AgentOfflineError(f"等待管道失败（Windows 错误 {wait_err}）")
        # 等到了空闲实例，回到循环重新 CreateFileW（可能被别人抢走）


# --- 对外接口 ---------------------------------------------------------------


def call(
    msg: dict,
    pipe_name: str = protocol.PIPE_NAME,
    token: str | None = None,
    connect_timeout_ms: int = protocol.CONNECT_TIMEOUT_MS,
    response_timeout_ms: int = protocol.REQUEST_TIMEOUT_MS,
) -> dict:
    """连上 agent，完成一次「hello 鉴权 + 一条请求」，返回服务端的 ok=true 响应 dict。

    参数：
        msg：请求帧，用 protocol.make_ping(...) / protocol.make_task(...) 构造。
        pipe_name：管道名，默认 protocol.PIPE_NAME。
        token：鉴权 token，缺省读 paths.read_token()；读不到 → AgentOfflineError。
        connect_timeout_ms：连接超时（含等待忙碌管道）。
        response_timeout_ms：单请求响应超时（hello 响应固定用 protocol.HELLO_TIMEOUT_MS）。

    异常：
        AgentOfflineError：连不上 / agent 没启动过 / 中途断开（CLI 退出码 1）。
        AgentTimeoutError：连接或等响应超时（CLI 退出码 2）。
        AgentProtocolError：服务端返回 ok=false 或帧不合法（CLI 退出码 3）。
    """
    if not isinstance(msg, dict):
        raise AgentProtocolError(protocol.CODE_BAD_MSG, "msg 必须是 dict")
    if token is None:
        token = paths.read_token()
    if not token:
        raise AgentOfflineError("token 不存在，agent 没启动过")

    try:
        hello_raw = protocol.encode(protocol.make_hello(token))
        request_raw = protocol.encode(msg)
    except protocol.ProtocolError as exc:
        raise AgentProtocolError(exc.code, exc.message) from exc
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError(protocol.CODE_BAD_MSG, f"消息无法编码：{exc}") from exc

    handle = _connect(pipe_name, connect_timeout_ms)
    try:
        _write_all(handle, hello_raw, "发送 hello")
        _check_hello(_read_line(handle, protocol.HELLO_TIMEOUT_MS, "hello 响应"))

        _write_all(handle, request_raw, "发送请求")
        return _check_result(
            _read_line(handle, response_timeout_ms, "响应"),
            msg.get("id"),
        )
    finally:
        _close(handle)
