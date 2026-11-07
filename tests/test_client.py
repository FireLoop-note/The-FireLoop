"""T2 CLI 客户端测试（标准库 unittest，零第三方依赖）。

覆盖：
- 无 agent 监听：快速抛 AgentOfflineError（< 2 秒）、CLI 2 秒级返回退出码 1；
- token 缺失：AgentOfflineError / CLI 退出码 1，且 CLI 绝不自己生成 token；
- 真 Win32 命名管道端到端（测试内 ctypes 起 fake 服务端）：
  hello 鉴权 → ping/task → 响应解析 → 短连接；
- 服务端返回错误帧 / 坏 JSON / 中途断开：AgentProtocolError / AgentOfflineError；
- 超时：连上但故意不响应 → AgentTimeoutError，CLI 退出码 2；
- 参数错误：task 缺失/为空、未知命令、未知选项 → CLI 退出码 4；
- --pipe-name 生效；
- B1：file / uri 子命令（task.text 格式、URL 解码中文空格、非法链接、退出码、真子进程）；
- T1 的 PipeServer 已就绪时，另跑一组真服务端端到端（未就绪自动 skip）。

说明：默认服务端是测试内的 fake 服务端（worker-server 的 pipe_server.py 未就绪时先用它），
管道是真实的 Win32 命名管道；不是常驻 agent 进程。所有管道名带 pid + uuid，
不碰真实 `\\\\.\\pipe\\fireloop-implant`，也不污染真实 %LOCALAPPDATA%。
"""

import contextlib
import ctypes
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from fireloop import cli, client, paths, protocol  # noqa: E402

# T1 服务端已就绪时顺带跑真端到端；未就绪也不阻塞 T2 自己验收（自动 skip）
try:  # pragma: no cover - 取决于 T1 是否已交付
    from fireloop.pipe_server import PipeServer as _PipeServer
except Exception:  # noqa: BLE001 - T1 文件缺失或导入失败都当未就绪
    _PipeServer = None

# ---------------------------------------------------------------------------
# 测试进程级环境隔离：FIRELOOP_HOME 指向临时目录，不污染真实数据目录
# ---------------------------------------------------------------------------

_TMP_ROOT = None
_ORIGINAL_HOME = None


def setUpModule():
    global _TMP_ROOT, _ORIGINAL_HOME
    _ORIGINAL_HOME = os.environ.get("FIRELOOP_HOME")
    _TMP_ROOT = tempfile.TemporaryDirectory(prefix="fireloop-client-tests-")
    os.environ["FIRELOOP_HOME"] = _TMP_ROOT.name


def tearDownModule():
    global _TMP_ROOT
    if _ORIGINAL_HOME is None:
        os.environ.pop("FIRELOOP_HOME", None)
    else:
        os.environ["FIRELOOP_HOME"] = _ORIGINAL_HOME
    if _TMP_ROOT is not None:
        _TMP_ROOT.cleanup()
        _TMP_ROOT = None


# ---------------------------------------------------------------------------
# 测试用 Win32 命名管道服务端（ctypes；只实现测试需要的行为）
# ---------------------------------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255

ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_CreateNamedPipeW = kernel32.CreateNamedPipeW
_CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
]
_CreateNamedPipeW.restype = wintypes.HANDLE

_ConnectNamedPipe = kernel32.ConnectNamedPipe
_ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_ConnectNamedPipe.restype = wintypes.BOOL

_DisconnectNamedPipe = kernel32.DisconnectNamedPipe
_DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
_DisconnectNamedPipe.restype = wintypes.BOOL

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

_FlushFileBuffers = kernel32.FlushFileBuffers
_FlushFileBuffers.argtypes = [wintypes.HANDLE]
_FlushFileBuffers.restype = wintypes.BOOL

_SERVER_POLL_S = 0.005


def _peek(handle):
    """非阻塞查可读字节数；对端断开返回 None。"""
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
        raise OSError(err, "PeekNamedPipe")
    return avail.value


def _read_available(handle, limit):
    size = max(1, min(limit, protocol.MAX_LINE_BYTES))
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
        raise OSError(ctypes.get_last_error(), "ReadFile")
    return buf.raw[: read.value]


def _write_all(handle, data):
    if not data:
        return
    buf = ctypes.create_string_buffer(data, len(data))
    base = ctypes.addressof(buf)
    sent = 0
    while sent < len(data):
        written = wintypes.DWORD(0)
        ok = _WriteFile(
            handle,
            ctypes.c_void_p(base + sent),
            len(data) - sent,
            ctypes.byref(written),
            None,
        )
        if not ok or written.value == 0:
            raise OSError(ctypes.get_last_error(), "WriteFile")
        sent += written.value


class _Conn:
    """fake 服务端侧的连接：按行读、发帧。全部非阻塞轮询 + 短超时，便于停服。"""

    def __init__(self, server, handle):
        self.server = server
        self.handle = handle
        self._buf = bytearray()

    def read_line(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[: newline + 1]
                return line
            if self.server.stopping:
                return None
            try:
                avail = _peek(self.handle)
                if avail is None:
                    return None
                if avail > 0:
                    self._buf.extend(_read_available(self.handle, avail))
                    continue
            except OSError:
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(_SERVER_POLL_S)

    def read_msg(self, timeout=5.0):
        raw = self.read_line(timeout)
        if raw is None:
            return None
        try:
            return protocol.decode_line(raw)
        except protocol.ProtocolError:
            return None

    def send(self, msg):
        _write_all(self.handle, protocol.encode(msg))

    def send_raw(self, raw):
        _write_all(self.handle, raw)

    def idle(self, timeout=20.0):
        """故意不响应：只等测试收场或对端断开。"""
        deadline = time.monotonic() + timeout
        while not self.server.stopping and time.monotonic() < deadline:
            try:
                if _peek(self.handle) is None:
                    return
            except OSError:
                return
            time.sleep(_SERVER_POLL_S)


def _handle_hello(conn, server):
    """读首帧 hello 并按 token 回 hello_ok / 错误帧。成功返回 True。"""
    hello = conn.read_msg()
    if hello is None:
        return False
    if hello.get("type") != protocol.HELLO:
        conn.send(protocol.make_error(None, protocol.CODE_BAD_MSG, "首帧必须是 hello"))
        return False
    if server.token is not None and hello.get("token") != server.token:
        conn.send(protocol.make_error(None, protocol.CODE_BAD_TOKEN, "token 不对"))
        return False
    conn.send(protocol.make_hello_ok("Fire Loop", "0.1.0"))
    return True


def _handler_ok(server, handle):
    """正常服务端：hello 鉴权 → ping 回 pong / task 回「收到任务」。记录收到的请求。"""
    conn = _Conn(server, handle)
    if not _handle_hello(conn, server):
        return
    req = conn.read_msg()
    if req is None:
        return
    server.requests.append(req)
    mtype = req.get("type")
    if mtype == protocol.PING:
        conn.send(protocol.make_ok(req.get("id"), text="pong"))
    elif mtype == protocol.TASK:
        text = req.get("text") or ""
        conn.send(protocol.make_ok(req.get("id"), text="收到任务", text_len=len(text)))
    else:
        conn.send(protocol.make_error(req.get("id"), protocol.CODE_UNKNOWN_TYPE, str(mtype)))


def _handler_request_error(server, handle):
    """hello 通过，但请求回 ok=false（验证 CLI 退出码 3）。"""
    conn = _Conn(server, handle)
    if not _handle_hello(conn, server):
        return
    req = conn.read_msg()
    if req is None:
        return
    conn.send(protocol.make_error(req.get("id"), protocol.CODE_INTERNAL, "测试内部错误"))


def _handler_garbage(server, handle):
    """hello 通过，然后回一行坏 JSON（验证 bad_json 处理）。"""
    conn = _Conn(server, handle)
    if not _handle_hello(conn, server):
        return
    if conn.read_msg() is None:
        return
    conn.send_raw(b"this is not json\n")


def _handler_close_after_hello(server, handle):
    """hello 通过后直接断开（验证客户端中途断开处理）。"""
    conn = _Conn(server, handle)
    if not _handle_hello(conn, server):
        return
    # 直接返回 = 服务端关连接


def _handler_never_respond(server, handle):
    """连上但一个字节都不读不回：验证 hello 超时。"""
    conn = _Conn(server, handle)
    conn.idle()


def _handler_hello_ok_then_hang(server, handle):
    """hello 正常，请求不回：验证单请求响应超时。"""
    conn = _Conn(server, handle)
    if not _handle_hello(conn, server):
        return
    if conn.read_msg() is None:
        return
    conn.idle()


class FakePipeServer:
    """测试用极简命名管道服务端（ctypes CreateNamedPipeW + 线程）。

    只服务一个连接、处理完再建下一个实例；daemon 线程，stop() 保证能收场。
    """

    def __init__(self, pipe_name, handler, token=None, max_instances=PIPE_UNLIMITED_INSTANCES):
        self.pipe_name = pipe_name
        self.handler = handler
        self.token = token
        self.max_instances = max_instances
        self.error = None
        self.connections = 0
        self.requests = []  # _handler_ok 收到的请求帧，供断言 CLI 实际发了什么
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"fake-pipe-{uuid.uuid4().hex[:6]}", daemon=True
        )

    @property
    def stopping(self):
        return self._stop.is_set()

    def start(self):
        self._thread.start()
        if not self._ready.wait(5.0):
            raise AssertionError("fake 服务端启动超时")
        if self.error is not None:
            raise AssertionError(f"fake 服务端启动失败：{self.error!r}")
        return self

    def stop(self):
        self._stop.set()
        self._poke()
        self._thread.join(2.0)
        if self._thread.is_alive():
            self._poke()
            self._thread.join(3.0)

    def _poke(self):
        """连一下自己的管道，把阻塞在 ConnectNamedPipe 的服务端线程叫醒。"""
        try:
            handle = _CreateFileW(
                self.pipe_name,
                GENERIC_READ | GENERIC_WRITE,
                0,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
        except OSError:
            return
        if handle and handle != INVALID_HANDLE_VALUE:
            _CloseHandle(handle)

    def _accept(self, handle):
        ok = _ConnectNamedPipe(handle, None)
        if ok:
            return True
        err = ctypes.get_last_error()
        if err == ERROR_PIPE_CONNECTED:
            return True
        if err in (ERROR_NO_DATA, ERROR_PIPE_NOT_CONNECTED):
            return False  # 连上又断（通常是 stop 的 poke）
        raise OSError(err, "ConnectNamedPipe")

    def _run(self):
        try:
            while not self._stop.is_set():
                handle = _CreateNamedPipeW(
                    self.pipe_name,
                    PIPE_ACCESS_DUPLEX,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                    self.max_instances,
                    65536,
                    65536,
                    0,
                    None,
                )
                if not handle or handle == INVALID_HANDLE_VALUE:
                    raise OSError(ctypes.get_last_error(), "CreateNamedPipeW")
                self._ready.set()
                if self._stop.is_set():
                    _CloseHandle(handle)
                    return
                try:
                    connected = self._accept(handle)
                    if self._stop.is_set():
                        return
                    if not connected:
                        continue
                    self.connections += 1
                    try:
                        self.handler(self, handle)
                    except OSError:
                        pass  # 客户端提前断开属于正常
                    finally:
                        # 必须先 flush 再断开：DisconnectNamedPipe 会丢掉客户端还没读走的
                        # 最后一条响应（Win32 文档明确要求先 FlushFileBuffers）
                        _FlushFileBuffers(handle)
                        try:
                            _DisconnectNamedPipe(handle)
                        except OSError:
                            pass
                finally:
                    _CloseHandle(handle)
        except Exception as exc:  # 记下来，teardown 时让测试失败得更清楚
            self.error = exc
        finally:
            self._ready.set()


# ---------------------------------------------------------------------------
# 测试基类：每个用例独立 FIRELOOP_HOME + 唯一管道名
# ---------------------------------------------------------------------------


class ClientTestBase(unittest.TestCase):
    def setUp(self):
        self._home_dir = tempfile.TemporaryDirectory(prefix="fireloop-case-")
        self.home = Path(self._home_dir.name)
        self._prev_home = os.environ.get("FIRELOOP_HOME")
        os.environ["FIRELOOP_HOME"] = str(self.home)
        self._servers = []
        self.addCleanup(self._teardown_case)

    def _teardown_case(self):
        for server in reversed(self._servers):
            server.stop()
        server_errors = [server.error for server in self._servers if server.error is not None]
        if self._prev_home is None:
            os.environ.pop("FIRELOOP_HOME", None)
        else:
            os.environ["FIRELOOP_HOME"] = self._prev_home
        self._home_dir.cleanup()
        # 清理干净之后再报 fake 服务端线程异常，避免异常把临时目录/env 留在现场
        if server_errors:
            raise AssertionError(f"fake 服务端线程异常：{server_errors!r}")

    # --- 工具 ---

    def write_token(self, token=None):
        token = token or (uuid.uuid4().hex + uuid.uuid4().hex)
        paths.write_token(token)
        return token

    def unique_pipe(self):
        return f"\\\\.\\pipe\\fireloop-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def start_server(self, handler, *, token=None, max_instances=PIPE_UNLIMITED_INSTANCES):
        server = FakePipeServer(
            self.unique_pipe(), handler, token=token, max_instances=max_instances
        ).start()
        self._servers.append(server)
        return server

    def run_cli(self, args, pipe_name=None, timeout=20.0):
        """起一个真子进程跑 `python -m fireloop.cli ...`。"""
        env = os.environ.copy()
        env["FIRELOOP_HOME"] = str(self.home)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
        command = [sys.executable, "-m", "fireloop.cli", *args]
        if pipe_name is not None:
            command += ["--pipe-name", pipe_name]
        started = time.monotonic()
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        proc.elapsed = time.monotonic() - started
        return proc


# ---------------------------------------------------------------------------
# 1. 无 agent / token 缺失：快速失败
# ---------------------------------------------------------------------------


class OfflineTest(ClientTestBase):
    def test_no_server_raises_offline_fast(self):
        self.write_token()
        pipe = self.unique_pipe()
        started = time.monotonic()
        with self.assertRaises(client.AgentOfflineError) as ctx:
            client.call(protocol.make_ping("p1"), pipe_name=pipe)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, f"无 agent 时应立即失败，实际 {elapsed:.2f}s")
        self.assertIn("管道", str(ctx.exception))

    def test_missing_token_raises_offline(self):
        pipe = self.unique_pipe()
        with self.assertRaises(client.AgentOfflineError) as ctx:
            client.call(protocol.make_ping("p1"), pipe_name=pipe)
        self.assertIn("token", str(ctx.exception))
        self.assertFalse(paths.token_path().exists(), "client 不应生成 token")

    def test_invalid_pipe_name(self):
        self.write_token()
        with self.assertRaises(client.AgentOfflineError):
            client.call(protocol.make_ping("p1"), pipe_name="")

    def test_non_dict_message_rejected(self):
        self.write_token()
        with self.assertRaises(client.AgentProtocolError) as ctx:
            client.call("not-a-dict")
        self.assertEqual(ctx.exception.code, protocol.CODE_BAD_MSG)

    def test_connect_timeout_when_pipe_busy(self):
        """管道实例被占满 → CreateFileW 返回 ERROR_PIPE_BUSY → WaitNamedPipe 等到超时。"""
        server = self.start_server(_handler_never_respond, max_instances=1)
        self.write_token()
        # 先自己开一条原始连接占住唯一的管道实例
        raw = _CreateFileW(
            server.pipe_name,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        self.assertTrue(raw and raw != INVALID_HANDLE_VALUE, "原始连接应能连上 fake 服务端")
        try:
            deadline = time.monotonic() + 5.0
            while server.connections < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(server.connections, 1, "服务端应已接住原始连接")

            started = time.monotonic()
            with self.assertRaises(client.AgentTimeoutError) as ctx:
                client.call(
                    protocol.make_ping("busy-1"),
                    pipe_name=server.pipe_name,
                    connect_timeout_ms=300,
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0)
            self.assertIn("超时", str(ctx.exception))
        finally:
            _CloseHandle(raw)


# ---------------------------------------------------------------------------
# 2. 命令行参数解析 / 用法错误（不发网络请求）
# ---------------------------------------------------------------------------


class ArgvTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_ping_message(self):
        msg, pipe_name = cli._build_message(["ping"])
        self.assertEqual(msg["type"], protocol.PING)
        self.assertEqual(msg["v"], protocol.PROTOCOL_VERSION)
        self.assertTrue(msg["id"])
        self.assertEqual(pipe_name, protocol.PIPE_NAME)

    def test_task_message(self):
        msg, _ = cli._build_message(["task", "帮我看看这个"])
        self.assertEqual(msg["type"], protocol.TASK)
        self.assertEqual(msg["text"], "帮我看看这个")

    def test_pipe_name_option(self):
        msg, pipe_name = cli._build_message(["ping", "--pipe-name", r"\\.\pipe\custom"])
        self.assertEqual(msg["type"], protocol.PING)
        self.assertEqual(pipe_name, r"\\.\pipe\custom")

    def test_pipe_name_equals_form(self):
        _, pipe_name = cli._build_message(["ping", r"--pipe-name=\\.\pipe\x2"])
        self.assertEqual(pipe_name, r"\\.\pipe\x2")

    def test_task_text_after_double_dash(self):
        msg, _ = cli._build_message(["task", "--", "-abc-def"])
        self.assertEqual(msg["text"], "-abc-def")

    def test_missing_command_is_usage_error(self):
        code, out, err = self.run_main([])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn("用法错误", err)

    def test_task_missing_text_is_usage_error(self):
        code, _, err = self.run_main(["task"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("用法错误", err)

    def test_task_empty_text_is_usage_error(self):
        for text in ("", "   ", "\t"):
            code, _, err = self.run_main(["task", text])
            self.assertEqual(code, cli.EXIT_USAGE, f"text={text!r}")
            self.assertIn("用法错误", err)

    def test_unknown_command_is_usage_error(self):
        code, _, err = self.run_main(["invoke"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("用法错误", err)

    def test_unknown_option_is_usage_error(self):
        code, _, err = self.run_main(["ping", "--bogus"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("用法错误", err)

    def test_pipe_name_missing_value_is_usage_error(self):
        code, _, err = self.run_main(["ping", "--pipe-name"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("用法错误", err)

    def test_extra_positional_is_usage_error(self):
        self.assertEqual(self.run_main(["ping", "extra"])[0], cli.EXIT_USAGE)
        self.assertEqual(self.run_main(["task", "a", "b"])[0], cli.EXIT_USAGE)

    def test_task_too_long_is_usage_error(self):
        code, _, err = self.run_main(["task", "x" * (protocol.MAX_TASK_TEXT + 1)])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("协议", err)

    def test_help(self):
        code, out, err = self.run_main(["--help"])
        self.assertEqual(code, protocol.CLI_EXIT_OK)
        self.assertIn("用法", out)
        self.assertEqual(err, "")
        self.assertIn("file", out)
        self.assertIn("uri", out)

    # --- B1：file 子命令 ---

    def test_file_message_absolute(self):
        path = r"C:\资料\a b.txt"
        msg, _ = cli._build_message(["file", path])
        self.assertEqual(msg["type"], protocol.TASK)
        self.assertEqual(msg["text"], "file:" + path)

    def test_file_message_relative_becomes_absolute(self):
        relative = r"sub\相对 路径.txt"
        msg, _ = cli._build_message(["file", relative])
        expected = "file:" + os.path.abspath(relative)
        self.assertEqual(msg["text"], expected)
        self.assertTrue(os.path.isabs(msg["text"][len("file:"):]))
        self.assertNotEqual(msg["text"], "file:" + relative)  # 已变成绝对路径

    def test_file_does_not_check_existence(self):
        missing = str(Path(tempfile.gettempdir()) / "fireloop-绝对不存在-12345.txt")
        msg, _ = cli._build_message(["file", missing])
        self.assertEqual(msg["text"], "file:" + missing)

    def test_file_empty_path_is_usage_error(self):
        for args in (["file"], ["file", ""], ["file", "   "]):
            code, out, err = self.run_main(args)
            self.assertEqual(code, cli.EXIT_USAGE, f"args={args!r}")
            self.assertEqual(out, "")
            self.assertIn("用法错误", err)

    def test_file_extra_arg_is_usage_error(self):
        self.assertEqual(self.run_main(["file", "a.txt", "b.txt"])[0], cli.EXIT_USAGE)

    # --- B1：uri 子命令 ---

    def test_uri_task_text_encoded(self):
        uri = "fireloop://task?text=%E4%BD%A0%E5%A5%BD%20world"
        msg, _ = cli._build_message(["uri", uri])
        self.assertEqual(msg["type"], protocol.TASK)
        self.assertEqual(msg["text"], "你好 world")

    def test_uri_task_raw_chinese_and_space(self):
        msg, _ = cli._build_message(["uri", "fireloop://task?text=打开 记事本"])
        self.assertEqual(msg["text"], "打开 记事本")

    def test_uri_file_path_encoded(self):
        uri = "fireloop://file?path=C%3A%5C%E8%B5%84%E6%96%99%5Ca%20b.txt"
        msg, _ = cli._build_message(["uri", uri])
        self.assertEqual(msg["text"], r"file:C:\资料\a b.txt")

    def test_uri_file_path_raw_backslashes(self):
        msg, _ = cli._build_message(["uri", r"fireloop://file?path=C:\资料\a b.txt"])
        self.assertEqual(msg["text"], r"file:C:\资料\a b.txt")

    def test_uri_plus_is_kept_literal(self):
        # 路径里的 + 不能被当成空格（百分号编码才是空格），install.ps1 用 EscapeDataString
        msg, _ = cli._build_message(["uri", r"fireloop://file?path=C:\a+b.txt"])
        self.assertEqual(msg["text"], r"file:C:\a+b.txt")

    def test_uri_percent_and_hash_decoded(self):
        msg, _ = cli._build_message(["uri", "fireloop://task?text=100%25%23done"])
        self.assertEqual(msg["text"], "100%#done")

    def test_uri_scheme_host_case_insensitive(self):
        msg, _ = cli._build_message(["uri", "FIRELOOP://TASK?text=ABC"])
        self.assertEqual(msg["text"], "ABC")

    def test_uri_invalid_scheme_is_usage_error(self):
        for uri in ("http://example.com/x", "file:///C:/x.txt", "not-a-uri", ""):
            code, _, err = self.run_main(["uri", uri])
            self.assertEqual(code, cli.EXIT_USAGE, f"uri={uri!r}")
            self.assertIn("用法错误", err)

    def test_uri_unknown_host_is_usage_error(self):
        for uri in ("fireloop://nope?text=x", "fireloop://", "fireloop:task?text=x"):
            code, _, err = self.run_main(["uri", uri])
            self.assertEqual(code, cli.EXIT_USAGE, f"uri={uri!r}")
            self.assertIn("用法错误", err)

    def test_uri_missing_or_empty_param_is_usage_error(self):
        for uri in (
            "fireloop://task",
            "fireloop://task?text=",
            "fireloop://task?text=%20",
            "fireloop://file",
            "fireloop://file?path=",
            "fireloop://file?path=%20",
        ):
            code, _, err = self.run_main(["uri", uri])
            self.assertEqual(code, cli.EXIT_USAGE, f"uri={uri!r}")
            self.assertIn("用法错误", err)

    def test_uri_extra_arg_is_usage_error(self):
        self.assertEqual(self.run_main(["uri", "fireloop://task?text=a", "b"])[0], cli.EXIT_USAGE)

    def test_uri_extra_params_and_fragment_ignored(self):
        msg, _ = cli._build_message(["uri", "fireloop://task?text=hi&extra=1#frag"])
        self.assertEqual(msg["text"], "hi")
        msg, _ = cli._build_message(["uri", "fireloop://task?text=hi#frag"])
        self.assertEqual(msg["text"], "hi")

    def test_uri_text_too_long_is_usage_error(self):
        uri = "fireloop://task?text=" + ("x" * (protocol.MAX_TASK_TEXT + 1))
        code, _, err = self.run_main(["uri", uri])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("协议", err)


# ---------------------------------------------------------------------------
# 3. fake 服务端上的客户端行为（真 Win32 管道）
# ---------------------------------------------------------------------------


class ClientAgainstFakeServerTest(ClientTestBase):
    def test_ping_ok(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        resp = client.call(protocol.make_ping("p1"), pipe_name=server.pipe_name)
        self.assertEqual(resp["type"], protocol.RESULT)
        self.assertIs(resp["ok"], True)
        self.assertEqual(resp["text"], "pong")
        self.assertEqual(resp["id"], "p1")

    def test_task_ok(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        resp = client.call(
            protocol.make_task("t1", "测试任务"), pipe_name=server.pipe_name
        )
        self.assertIs(resp["ok"], True)
        self.assertEqual(resp["text"], "收到任务")
        self.assertEqual(resp["text_len"], 4)

    def test_short_connection_two_calls(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        for i in range(2):
            resp = client.call(protocol.make_ping(f"p{i}"), pipe_name=server.pipe_name)
            self.assertEqual(resp["text"], "pong")
        self.assertEqual(server.connections, 2, "每次 call 应新建一条短连接")

    def test_explicit_token_overrides_file(self):
        # 文件里是 a，但显式传 b：应以显式 token 为准，鉴权成功
        self.write_token("a" * 64)
        server = self.start_server(_handler_ok, token="b" * 64)
        resp = client.call(
            protocol.make_ping("p1"), pipe_name=server.pipe_name, token="b" * 64
        )
        self.assertEqual(resp["text"], "pong")

    def test_bad_token_protocol_error(self):
        self.write_token("a" * 64)
        server = self.start_server(_handler_ok, token="b" * 64)
        with self.assertRaises(client.AgentProtocolError) as ctx:
            client.call(protocol.make_ping("p1"), pipe_name=server.pipe_name)
        self.assertEqual(ctx.exception.code, protocol.CODE_BAD_TOKEN)
        self.assertIn("token", ctx.exception.message)

    def test_server_error_frame_protocol_error(self):
        token = self.write_token()
        server = self.start_server(_handler_request_error, token=token)
        with self.assertRaises(client.AgentProtocolError) as ctx:
            client.call(protocol.make_ping("p1"), pipe_name=server.pipe_name)
        self.assertEqual(ctx.exception.code, protocol.CODE_INTERNAL)
        self.assertEqual(ctx.exception.message, "测试内部错误")

    def test_garbage_response_protocol_error(self):
        token = self.write_token()
        server = self.start_server(_handler_garbage, token=token)
        with self.assertRaises(client.AgentProtocolError) as ctx:
            client.call(protocol.make_ping("p1"), pipe_name=server.pipe_name)
        self.assertEqual(ctx.exception.code, protocol.CODE_BAD_JSON)

    def test_server_closes_after_hello(self):
        token = self.write_token()
        server = self.start_server(_handler_close_after_hello, token=token)
        with self.assertRaises(client.AgentOfflineError):
            client.call(protocol.make_ping("p1"), pipe_name=server.pipe_name)

    def test_hello_timeout(self):
        self.write_token()
        server = self.start_server(_handler_never_respond)
        started = time.monotonic()
        with self.assertRaises(client.AgentTimeoutError) as ctx:
            client.call(protocol.make_ping("p1"), pipe_name=server.pipe_name)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, protocol.HELLO_TIMEOUT_MS / 1000.0 * 0.8)
        self.assertLess(elapsed, 4.0, f"hello 超时不应拖太久，实际 {elapsed:.2f}s")
        self.assertIn("超时", str(ctx.exception))

    def test_response_timeout(self):
        token = self.write_token()
        server = self.start_server(_handler_hello_ok_then_hang, token=token)
        started = time.monotonic()
        with self.assertRaises(client.AgentTimeoutError):
            client.call(
                protocol.make_ping("p1"),
                pipe_name=server.pipe_name,
                response_timeout_ms=300,
            )
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 3.0, f"响应超时不应挂死，实际 {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# 4. CLI 端到端（真子进程 + 真管道 + fake 服务端）
# ---------------------------------------------------------------------------


class CliEndToEndTest(ClientTestBase):
    def test_ping_exit0_prints_pong(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        proc = self.run_cli(["ping"], pipe_name=server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "pong")
        self.assertEqual(proc.stderr, "")

    def test_task_exit0_prints_server_text(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        proc = self.run_cli(["task", "测试任务"], pipe_name=server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "收到任务")

    def test_offline_exit1_fast(self):
        self.write_token()
        proc = self.run_cli(["ping"], pipe_name=self.unique_pipe())
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OFFLINE, proc.stderr)
        self.assertIn("没在运行", proc.stderr)
        self.assertIn(protocol.CODE_AGENT_OFFLINE, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertLess(proc.elapsed, 3.0, f"CLI 无 agent 应快速返回，实际 {proc.elapsed:.2f}s")

    def test_offline_missing_token_exit1_and_no_token_generated(self):
        proc = self.run_cli(["ping"], pipe_name=self.unique_pipe())
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OFFLINE, proc.stderr)
        self.assertIn("没在运行", proc.stderr)
        self.assertIn("token", proc.stderr)
        self.assertFalse(paths.token_path().exists(), "CLI 绝不能自己生成 token")

    def test_timeout_exit2(self):
        self.write_token()
        server = self.start_server(_handler_never_respond)
        proc = self.run_cli(["ping"], pipe_name=server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_TIMEOUT, proc.stderr)
        self.assertIn(protocol.CODE_TIMEOUT, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertLess(proc.elapsed, 8.0, f"CLI 超时应明确退出，实际 {proc.elapsed:.2f}s")

    def test_bad_token_exit3(self):
        self.write_token("a" * 64)
        server = self.start_server(_handler_ok, token="b" * 64)
        proc = self.run_cli(["ping"], pipe_name=server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_PROTOCOL, proc.stderr)
        self.assertIn(protocol.CODE_BAD_TOKEN, proc.stderr)

    def test_server_error_exit3(self):
        token = self.write_token()
        server = self.start_server(_handler_request_error, token=token)
        proc = self.run_cli(["ping"], pipe_name=server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_PROTOCOL, proc.stderr)
        self.assertIn(protocol.CODE_INTERNAL, proc.stderr)
        self.assertIn("测试内部错误", proc.stderr)

    def test_empty_task_exit4(self):
        for args in (["task"], ["task", ""], ["task", "   "]):
            proc = self.run_cli(args)
            self.assertEqual(proc.returncode, cli.EXIT_USAGE, f"args={args!r} {proc.stderr}")
            self.assertIn("用法错误", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertEqual(proc.stdout, "")

    def test_unknown_command_exit4(self):
        proc = self.run_cli(["whatever"])
        self.assertEqual(proc.returncode, cli.EXIT_USAGE, proc.stderr)
        self.assertIn("用法错误", proc.stderr)

    # --- B1：file / uri 子命令端到端（真子进程 + 真管道 + fake 服务端） ---

    def test_file_sends_absolute_path(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        path = str(self.home / "资料" / "a b.txt")  # 不存在也行，file 不检查存在性
        proc = self.run_cli(["file", path], pipe_name=server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "收到任务")
        self.assertEqual(server.requests[-1]["type"], protocol.TASK)
        self.assertEqual(server.requests[-1]["text"], "file:" + path)

    def test_uri_task_sends_decoded_text(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        uri = "fireloop://task?text=%E6%89%93%E5%BC%80%20%E8%AE%B0%E4%BA%8B%E6%9C%AC"
        proc = self.run_cli(["uri", uri], pipe_name=server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(server.requests[-1]["text"], "打开 记事本")

    def test_uri_file_sends_file_text(self):
        token = self.write_token()
        server = self.start_server(_handler_ok, token=token)
        path = str(self.home / "资料" / "a b.txt")
        encoded = urllib.parse.quote(path, safe="")  # 等价于 .NET Uri.EscapeDataString
        proc = self.run_cli(
            ["uri", "fireloop://file?path=" + encoded], pipe_name=server.pipe_name
        )
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(server.requests[-1]["text"], "file:" + path)

    def test_uri_invalid_exit4(self):
        proc = self.run_cli(["uri", "https://example.com/x"])
        self.assertEqual(proc.returncode, cli.EXIT_USAGE, proc.stderr)
        self.assertIn("用法错误", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_file_empty_exit4(self):
        proc = self.run_cli(["file", ""])
        self.assertEqual(proc.returncode, cli.EXIT_USAGE, proc.stderr)
        self.assertIn("用法错误", proc.stderr)

    def test_new_commands_without_agent_exit1(self):
        self.write_token()
        for args in (["file", r"C:\nonexistent\a.txt"], ["uri", "fireloop://task?text=hello"]):
            proc = self.run_cli(args, pipe_name=self.unique_pipe())
            self.assertEqual(proc.returncode, protocol.CLI_EXIT_OFFLINE, proc.stderr)
            self.assertIn("没在运行", proc.stderr)


# ---------------------------------------------------------------------------
# 5. 真服务端端到端：直接 import T1 的 PipeServer（只读使用，不改它的文件）
# ---------------------------------------------------------------------------


@unittest.skipUnless(_PipeServer is not None, "T1 pipe_server.py 尚未就绪，跳过真服务端端到端")
class RealPipeServerEndToEndTest(ClientTestBase):
    """用 worker-server 写的真实 PipeServer 起服务端，验证 T2 客户端半边端到端。"""

    def start_real_server(self, token):
        server = _PipeServer(pipe_name=self.unique_pipe(), token=token)
        thread = threading.Thread(
            target=server.serve_forever, name="real-pipe-server", daemon=True
        )
        thread.start()
        self.addCleanup(self._stop_real_server, server, thread)
        self._wait_pipe_ready(server.pipe_name)
        return server

    def _wait_pipe_ready(self, pipe_name, timeout=5.0):
        """探测管道实例已经能连上（探测连接会被服务端正常拒绝并断开）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            handle = _CreateFileW(
                pipe_name,
                GENERIC_READ | GENERIC_WRITE,
                0,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if handle and handle != INVALID_HANDLE_VALUE:
                _CloseHandle(handle)
                time.sleep(0.05)  # 让服务端处理完探测连接、重新回到 accept
                return
            time.sleep(0.01)
        self.fail("PipeServer 5 秒内没起来")

    def _stop_real_server(self, server, thread):
        server.stop(timeout=3.0)
        thread.join(5.0)
        self.assertFalse(thread.is_alive(), "PipeServer 线程没退出")

    def call_with_retry(self, msg, pipe_name, timeout=3.0, **kwargs):
        """刚起的服务端可能在收尾上一次探测连接，短暂 offline 时重试。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return client.call(msg, pipe_name=pipe_name, **kwargs)
            except client.AgentOfflineError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def run_cli_until_ready(self, args, pipe_name, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            proc = self.run_cli(args, pipe_name=pipe_name)
            if proc.returncode != protocol.CLI_EXIT_OFFLINE or time.monotonic() >= deadline:
                return proc
            time.sleep(0.05)

    def test_ping_against_real_pipe_server(self):
        token = self.write_token()
        server = self.start_real_server(token)
        resp = self.call_with_retry(protocol.make_ping("real-p1"), server.pipe_name)
        self.assertIs(resp["ok"], True)
        self.assertEqual(resp["text"], "pong")
        self.assertEqual(resp["id"], "real-p1")

    def test_task_against_real_pipe_server(self):
        token = self.write_token()
        server = self.start_real_server(token)
        resp = self.call_with_retry(
            protocol.make_task("real-t1", "真服务端任务"), server.pipe_name
        )
        self.assertIs(resp["ok"], True)
        self.assertEqual(resp["text"], "收到任务")
        self.assertEqual(resp["text_len"], 6)

    def test_bad_token_against_real_pipe_server(self):
        self.write_token("a" * 64)
        server = self.start_real_server("b" * 64)
        with self.assertRaises(client.AgentProtocolError) as ctx:
            self.call_with_retry(protocol.make_ping("real-p2"), server.pipe_name)
        self.assertEqual(ctx.exception.code, protocol.CODE_BAD_TOKEN)

    def test_cli_ping_against_real_pipe_server(self):
        token = self.write_token()
        server = self.start_real_server(token)
        proc = self.run_cli_until_ready(["ping"], server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "pong")

    def test_cli_task_against_real_pipe_server(self):
        token = self.write_token()
        server = self.start_real_server(token)
        proc = self.run_cli_until_ready(["task", "真服务端回显"], server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "收到任务")

    def test_cli_bad_token_against_real_pipe_server(self):
        # agent 的 token 与家庭目录里的不一致 → 真服务端回 bad_token → CLI 退出码 3
        self.write_token("a" * 64)
        server = self.start_real_server("b" * 64)
        proc = self.run_cli_until_ready(["ping"], server.pipe_name)
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_PROTOCOL, proc.stderr)
        self.assertIn(protocol.CODE_BAD_TOKEN, proc.stderr)

    def test_cli_uri_against_real_pipe_server(self):
        token = self.write_token()
        server = self.start_real_server(token)
        proc = self.run_cli_until_ready(
            ["uri", "fireloop://task?text=%E4%BD%A0%E5%A5%BD"], server.pipe_name
        )
        self.assertEqual(proc.returncode, protocol.CLI_EXIT_OK, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "收到任务")


if __name__ == "__main__":
    unittest.main()
