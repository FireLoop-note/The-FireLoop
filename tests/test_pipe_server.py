"""T1 管道服务端行为测试（标准库 unittest，真起命名管道，不 mock Win32）。

运行：
    cd D:\\ds植入式
    $env:PYTHONPATH="$PWD\\src"
    & D:\\Python\\python.exe -m unittest discover -s tests -p "test_pipe_server.py" -v

测试纪律：
- 每个用例把 FIRELOOP_HOME 指到独立的 tempfile.TemporaryDirectory()，绝不写真实
  %LOCALAPPDATA%\\FireLoop；
- 管道名 / 互斥体名都带 pid + 随机串，避免和 worker-cli 并行测试、真实 agent 冲突；
- 测试客户端直接用内置 open(pipe_name, "r+b", buffering=0) 读写管道，不依赖 T2 的
  client.py（测试 T1 时它可能还没写完）；
- 每个用例有硬超时看门狗：真挂死就 dump 线程栈并 os._exit(99)，绝不让命令行挂住。

关于客户端实现（踩过的坑，写清楚防止后来人再踩）：
Windows 同步（非 OVERLAPPED）命名管道句柄上的 I/O 是按"文件对象"串行化的。
如果客户端在后台线程里挂一个阻塞 ReadFile，主线程再 WriteFile 同一个句柄，
两个调用会互相卡死；服务端此时也等不到数据，形成三方死锁。
所以这里不用"读线程"，而是 PeekNamedPipe 轮询有没有数据、有数据才读一次，
所有等待都有超时上限。T2 的 client.py 也必须避开同一个坑（用 overlapped I/O
或同样的 Peek 轮询）。
"""

from __future__ import annotations

import ctypes
import faulthandler
import json
import msvcrt
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
import uuid
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fireloop import APP_NAME, __version__, paths, protocol  # noqa: E402
from fireloop.pipe_server import PipeServer  # noqa: E402
from fireloop.single_instance import SingleInstance  # noqa: E402

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PeekNamedPipe = _kernel32.PeekNamedPipe
_PeekNamedPipe.argtypes = (
    wintypes.HANDLE,  # hNamedPipe
    wintypes.LPVOID,  # 只看可读字节数，不真读，lpBuffer 传 NULL
    wintypes.DWORD,  # nBufferSize
    wintypes.LPDWORD,  # lpBytesRead
    wintypes.LPDWORD,  # lpTotalBytesAvail
    wintypes.LPDWORD,  # lpBytesLeftThisMessage
)
_PeekNamedPipe.restype = wintypes.BOOL

_EOF_WINERRORS = frozenset({0, 109, 232, 233})  # broken pipe / not connected 等

POLL_INTERVAL = 0.005


def _unique_pipe_name() -> str:
    return rf"\\.\pipe\fireloop-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _unique_mutex_name() -> str:
    return rf"Local\FireLoopTest-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class _PipeClient:
    """测试用最小客户端：open 命名管道，PeekNamedPipe 轮询 + 顺序读写，全部带超时。"""

    def __init__(self, pipe_name: str):
        self._pipe = open(pipe_name, "r+b", buffering=0)
        self._handle = msvcrt.get_osfhandle(self._pipe.fileno())
        self._buf = bytearray()

    # ---- 发送 ----
    def send_raw(self, data: bytes) -> None:
        self._pipe.write(data)

    def send_msg(self, msg: dict) -> None:
        self.send_raw(protocol.encode(msg))

    # ---- 接收 ----
    def _available(self):
        """可读字节数；None 表示连接已断开。"""
        avail = wintypes.DWORD(0)
        if not _PeekNamedPipe(self._handle, None, 0, None, ctypes.byref(avail), None):
            winerror = ctypes.get_last_error()
            if winerror in _EOF_WINERRORS:
                return None
            raise AssertionError(f"PeekNamedPipe 失败，winerror={winerror}")
        return avail.value

    def _read_line(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            newline_at = self._buf.find(b"\n")
            if newline_at >= 0:
                line = bytes(self._buf[:newline_at])
                del self._buf[: newline_at + 1]
                return line

            avail = self._available()
            if avail is None:
                raise AssertionError("连接被服务端断开，没等到完整响应")
            if avail == 0:
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{timeout}s 内没等到服务端响应")
                time.sleep(POLL_INTERVAL)
                continue
            chunk = self._pipe.read(avail)
            if not chunk:
                raise AssertionError("连接被服务端断开，没等到完整响应")
            self._buf += chunk

    def recv_msg(self, timeout: float = 5.0) -> dict:
        return json.loads(self._read_line(timeout).decode("utf-8"))

    def wait_eof(self, timeout: float = 5.0) -> None:
        """等连接被服务端断开；断开前不允许再出现完整响应行。"""
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in self._buf:
                raise AssertionError(f"预期断开，却收到了数据: {self._read_line(0.1)!r}")
            avail = self._available()
            if avail is None:
                return
            if avail:
                chunk = self._pipe.read(avail)
                if not chunk:
                    return
                self._buf += chunk
                continue
            if time.monotonic() >= deadline:
                raise AssertionError(f"{timeout}s 内服务端没有断开连接")
            time.sleep(POLL_INTERVAL)

    def close(self) -> None:
        try:
            self._pipe.close()
        except OSError:
            pass


class _HardTimeoutMixin:
    """每个用例硬超时：超时 dump 所有线程栈后直接退出进程，保证测试不挂死。"""

    TEST_TIMEOUT_SECONDS = 30.0

    def _start_watchdog(self) -> None:
        test_id = self.id()

        def _bark() -> None:
            print(
                f"\n[watchdog] 用例 {test_id} 超过 {self.TEST_TIMEOUT_SECONDS}s 未结束，"
                "dump 线程栈后退出进程",
                flush=True,
            )
            faulthandler.dump_traceback()
            os._exit(99)

        self._watchdog = threading.Timer(self.TEST_TIMEOUT_SECONDS, _bark)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _stop_watchdog(self) -> None:
        timer = getattr(self, "_watchdog", None)
        if timer is not None:
            timer.cancel()


class _TempHomeMixin:
    """每个用例独立的 FIRELOOP_HOME，用完还原环境变量。"""

    def _setup_temp_home(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="fireloop-test-")
        self._old_home = os.environ.get("FIRELOOP_HOME")
        os.environ["FIRELOOP_HOME"] = self._tmp_dir.name
        paths.ensure_dirs()

    def _teardown_temp_home(self) -> None:
        if self._old_home is None:
            os.environ.pop("FIRELOOP_HOME", None)
        else:
            os.environ["FIRELOOP_HOME"] = self._old_home
        self._tmp_dir.cleanup()


class SingleInstanceTest(_TempHomeMixin, _HardTimeoutMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_watchdog()
        self._setup_temp_home()
        self._name = _unique_mutex_name()
        self._instances: list[SingleInstance] = []

    def tearDown(self) -> None:
        try:
            for instance in self._instances:
                instance.release()
            self._teardown_temp_home()
        finally:
            self._stop_watchdog()

    def _make(self) -> SingleInstance:
        instance = SingleInstance(self._name)
        self._instances.append(instance)
        return instance

    def test_second_acquire_false_then_release_allows_reacquire(self) -> None:
        first = self._make()
        second = self._make()
        self.assertTrue(first.acquire(), "第一个实例应该拿到互斥体")
        self.assertFalse(second.acquire(), "第二个实例应被单实例锁挡住")
        first.release()
        self.assertTrue(second.acquire(), "第一个释放后第二个应该能拿到")
        second.release()
        self.assertFalse(second.held)

    def test_acquire_twice_same_instance_is_idempotent(self) -> None:
        instance = self._make()
        self.assertTrue(instance.acquire())
        self.assertTrue(instance.acquire())
        self.assertTrue(instance.held)

    def test_release_without_acquire_is_noop(self) -> None:
        instance = self._make()
        instance.release()
        self.assertFalse(instance.held)
        self.assertTrue(instance.acquire())
        instance.release()
        instance.release()  # 重复 release 不应报错
        self.assertFalse(instance.held)


class PipeServerTest(_TempHomeMixin, _HardTimeoutMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_watchdog()
        self._setup_temp_home()
        self.assertEqual(paths.home(), Path(os.environ["FIRELOOP_HOME"]))
        self.token = paths.ensure_token()
        self.pipe_name = _unique_pipe_name()
        self._clients: list[_PipeClient] = []
        self.server = PipeServer(pipe_name=self.pipe_name, token=self.token)
        self._thread = threading.Thread(
            target=self.server.serve_forever,
            name="fireloop-pipe-server-under-test",
            daemon=True,
        )
        self._thread.start()

    def tearDown(self) -> None:
        try:
            for client in self._clients:
                client.close()
            self.server.stop()
            self._thread.join(timeout=5)
            self.assertFalse(self._thread.is_alive(), "serve_forever 线程没有退出（线程泄漏）")
            self._teardown_temp_home()
        finally:
            self._stop_watchdog()

    # ---------- 工具 ----------

    def connect(self, timeout: float = 5.0) -> _PipeClient:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not self._thread.is_alive():
                self.fail("serve_forever 线程提前退出，无法连接")
            try:
                client = _PipeClient(self.pipe_name)
            except OSError as exc:
                last_error = exc
                time.sleep(0.01)
                continue
            self._clients.append(client)
            return client
        self.fail(f"{timeout}s 内连不上管道 {self.pipe_name}: {last_error}")

    def handshake(self) -> _PipeClient:
        client = self.connect()
        client.send_msg(protocol.make_hello(self.token))
        reply = client.recv_msg()
        self.assertEqual(reply["type"], protocol.HELLO_OK)
        return client

    def connect_like_cli(self, timeout: float = 2.0) -> _PipeClient:
        """模拟 T2 客户端：只有 ERROR_PIPE_BUSY 才等，ERROR_FILE_NOT_FOUND 直接失败。

        服务端如果在两次连接之间重建管道实例，就会出现管道名短暂消失的空窗，
        这时真实 CLI 会误报"agent 没在跑"。这个 helper 用来锁死"没有空窗"。
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                client = _PipeClient(self.pipe_name)
            except OSError as exc:
                # 管道实例被上一个连接占着时：CreateFileW 报 ERROR_PIPE_BUSY(231)，
                # 但 Python 的 open() 走 CRT，会把它映射成 EINVAL(errno=22)。
                # 这属于正常的"等实例空出来"，真实 CLI 会 WaitNamedPipe 等。
                busy = exc.winerror == 231 or exc.errno == 22
                if not busy:
                    raise AssertionError(
                        "管道不可用"
                        f"（errno={exc.errno}, winerror={exc.winerror}），"
                        f"真实 CLI 会误报 agent 没在跑: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{timeout}s 内管道一直忙") from exc
                time.sleep(0.002)
                continue
            self._clients.append(client)
            return client

    def expect_error(self, client: _PipeClient, code: str) -> dict:
        reply = client.recv_msg()
        self.assertEqual(reply["type"], protocol.RESULT)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["code"], code)
        return reply

    def assert_server_serves_new_connection(self) -> None:
        """错误 / 断开之后服务端还能继续服务新连接。"""
        client = self.handshake()
        client.send_msg(protocol.make_ping("after-error"))
        reply = client.recv_msg()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["id"], "after-error")
        self.assertEqual(reply["text"], "pong")
        client.close()

    def read_log_records(self) -> list[dict]:
        records: list[dict] = []
        logs_dir = paths.logs_dir()
        if not logs_dir.exists():
            return records
        for path in sorted(logs_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return records

    # ---------- 正常流程 ----------

    def test_hello_ok_fields(self) -> None:
        client = self.connect()
        client.send_msg(protocol.make_hello(self.token))
        reply = client.recv_msg()
        self.assertEqual(reply["v"], protocol.PROTOCOL_VERSION)
        self.assertEqual(reply["type"], protocol.HELLO_OK)
        self.assertEqual(reply["agent"], APP_NAME)
        self.assertEqual(reply["version"], __version__)
        self.assertNotIn("id", reply)

    def test_ping_pong(self) -> None:
        client = self.handshake()
        client.send_msg(protocol.make_ping("ping-1"))
        reply = client.recv_msg()
        self.assertEqual(
            reply,
            {
                "v": protocol.PROTOCOL_VERSION,
                "id": "ping-1",
                "type": protocol.RESULT,
                "ok": True,
                "text": "pong",
            },
        )

    def test_task_echo(self) -> None:
        client = self.handshake()
        text = "测试任务：打开记事本"
        client.send_msg(protocol.make_task("task-1", text))
        reply = client.recv_msg()
        self.assertEqual(reply["type"], protocol.RESULT)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["id"], "task-1")
        self.assertEqual(reply["text"], "收到任务")
        self.assertEqual(reply["text_len"], len(text))

    def test_multiple_requests_in_one_connection(self) -> None:
        client = self.handshake()
        for i in range(3):
            client.send_msg(protocol.make_ping(f"p{i}"))
            self.assertEqual(client.recv_msg()["id"], f"p{i}")
        text = "hello 火环"
        client.send_msg(protocol.make_task("t9", text))
        reply = client.recv_msg()
        self.assertEqual(reply["id"], "t9")
        self.assertEqual(reply["text"], "收到任务")
        self.assertEqual(reply["text_len"], len(text))

    def test_back_to_back_short_connections_no_gap(self) -> None:
        """连续短连接：服务端必须复用管道实例，两次连接之间管道名不能消失。"""
        for i in range(5):
            client = self.connect_like_cli()
            client.send_msg(protocol.make_hello(self.token))
            self.assertEqual(client.recv_msg()["type"], protocol.HELLO_OK)
            client.send_msg(protocol.make_ping(f"burst-{i}"))
            reply = client.recv_msg()
            self.assertEqual(reply["id"], f"burst-{i}")
            self.assertEqual(reply["text"], "pong")
            client.close()

    # ---------- 鉴权与错误 ----------

    def test_bad_token_rejected(self) -> None:
        client = self.connect()
        client.send_msg(protocol.make_hello("0" * 64))
        self.expect_error(client, protocol.CODE_BAD_TOKEN)
        client.wait_eof()
        self.assert_server_serves_new_connection()

    def test_missing_token_rejected(self) -> None:
        client = self.connect()
        client.send_msg({"v": protocol.PROTOCOL_VERSION, "type": protocol.HELLO})
        self.expect_error(client, protocol.CODE_BAD_TOKEN)
        client.wait_eof()

    def test_request_before_hello_rejected(self) -> None:
        client = self.connect()
        client.send_msg(protocol.make_ping("early"))
        self.expect_error(client, protocol.CODE_BAD_TOKEN)
        client.wait_eof()
        self.assert_server_serves_new_connection()

    def test_first_frame_not_json_rejected_as_bad_token(self) -> None:
        client = self.connect()
        client.send_raw(b"this is not json\n")
        self.expect_error(client, protocol.CODE_BAD_TOKEN)
        client.wait_eof()

    def test_bad_json_after_hello(self) -> None:
        client = self.handshake()
        client.send_raw(b'{"v":1,"id":"x" oops\n')
        self.expect_error(client, protocol.CODE_BAD_JSON)
        client.wait_eof()
        self.assert_server_serves_new_connection()

    def test_task_missing_text_is_bad_msg(self) -> None:
        client = self.handshake()
        client.send_msg({"v": protocol.PROTOCOL_VERSION, "id": "t1", "type": protocol.TASK})
        self.expect_error(client, protocol.CODE_BAD_MSG)
        client.wait_eof()
        self.assert_server_serves_new_connection()

    def test_task_text_wrong_type_is_bad_msg(self) -> None:
        client = self.handshake()
        client.send_msg(
            {"v": protocol.PROTOCOL_VERSION, "id": "t2", "type": protocol.TASK, "text": 123}
        )
        self.expect_error(client, protocol.CODE_BAD_MSG)
        client.wait_eof()

    def test_ping_missing_id_is_bad_msg(self) -> None:
        client = self.handshake()
        client.send_msg({"v": protocol.PROTOCOL_VERSION, "type": protocol.PING})
        self.expect_error(client, protocol.CODE_BAD_MSG)
        client.wait_eof()

    def test_unknown_type(self) -> None:
        client = self.handshake()
        client.send_msg({"v": protocol.PROTOCOL_VERSION, "id": "u1", "type": "shutdown"})
        reply = self.expect_error(client, protocol.CODE_UNKNOWN_TYPE)
        self.assertEqual(reply["id"], "u1")
        client.wait_eof()
        self.assert_server_serves_new_connection()

    def test_line_too_large_disconnects_and_server_survives(self) -> None:
        client = self.handshake()
        client.send_raw(b"x" * (protocol.MAX_LINE_BYTES + 1) + b"\n")
        self.expect_error(client, protocol.CODE_TOO_LARGE)
        client.wait_eof()
        self.assert_server_serves_new_connection()

    def test_task_text_too_long(self) -> None:
        client = self.handshake()
        # 单行远没到 65536 字节，但 task.text 超过协议上限 4096 字符。
        client.send_msg(
            {
                "v": protocol.PROTOCOL_VERSION,
                "id": "big",
                "type": protocol.TASK,
                "text": "字" * (protocol.MAX_TASK_TEXT + 1),
            }
        )
        self.expect_error(client, protocol.CODE_TOO_LARGE)
        client.wait_eof()

    def test_server_survives_error_burst(self) -> None:
        for _ in range(2):
            bad_token = self.connect()
            bad_token.send_msg(protocol.make_hello("bad"))
            self.expect_error(bad_token, protocol.CODE_BAD_TOKEN)
            bad_token.wait_eof()

            bad_json = self.handshake()
            bad_json.send_raw(b"!!!\n")
            self.expect_error(bad_json, protocol.CODE_BAD_JSON)
            bad_json.wait_eof()

            too_large = self.handshake()
            too_large.send_raw(b"z" * (protocol.MAX_LINE_BYTES + 1) + b"\n")
            self.expect_error(too_large, protocol.CODE_TOO_LARGE)
            too_large.wait_eof()
        self.assert_server_serves_new_connection()

    # ---------- 日志纪律 ----------

    def test_logs_contain_only_fingerprint_not_task_text(self) -> None:
        text = "绝密任务-火环-abc123"
        client = self.handshake()
        client.send_msg(protocol.make_task("t-log", text))
        reply = client.recv_msg()
        self.assertEqual(reply["text_len"], len(text))

        records = self.read_log_records()
        events = [r["event"] for r in records]
        for expected in ("agent_start", "client_connected", "hello_ok", "request"):
            self.assertIn(expected, events)

        fingerprint = protocol.text_fingerprint(text)
        task_records = [
            r
            for r in records
            if r.get("event") == "request" and r.get("type") == protocol.TASK
        ]
        self.assertEqual(len(task_records), 1)
        self.assertEqual(task_records[0]["text_len"], fingerprint["text_len"])
        self.assertEqual(task_records[0]["text_sha8"], fingerprint["text_sha8"])

        logs_text = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted(paths.logs_dir().glob("*.jsonl"))
        )
        self.assertNotIn(text, logs_text, "日志里不允许出现任务原文")
        self.assertNotIn(self.token, logs_text, "日志里不允许出现 token")

    def test_error_and_disconnect_are_logged(self) -> None:
        client = self.handshake()
        client.send_raw(b"not json\n")
        self.expect_error(client, protocol.CODE_BAD_JSON)
        client.wait_eof()

        records = self.read_log_records()
        self.assertTrue(
            any(
                r.get("event") == "error" and r.get("code") == protocol.CODE_BAD_JSON
                for r in records
            ),
            f"没有找到 bad_json 的 error 日志: {records}",
        )
        self.assertTrue(any(r.get("event") == "client_disconnected" for r in records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
