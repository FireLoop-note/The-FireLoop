"""Fire Loop 集成验收套件（M1a 起，后续里程碑复用）。

真起 agent 进程 + 真跑 CLI，逐条核对 docs/fire-loop-方案.md 第 5 节验收清单。
不是模块单测的替代：单测验逻辑，本套件验真实进程行为。

隔离设计（重点）
----------------
每次运行都会生成一份完全独立的验收环境：

- 唯一命名管道：``\\\\.\\pipe\\fireloop-accept-<pid>-<uuid8>``
- 唯一单实例互斥体：``Local\\FireLoopAccept-<pid>-<uuid8>``
- 唯一临时 FIRELOOP_HOME（token / 日志都写在里面）

测试 agent 用 ``python -m fireloop.agent --pipe-name ... --mutex-name ...`` 启动，
CLI 每次都显式带 ``--pipe-name``。所以：

- 全程**不碰**生产管道 ``\\\\.\\pipe\\fireloop-implant``；
- 全程**不碰**生产互斥体 ``Local\\FireLoopAgent``；
- 用户电脑上真实 agent 常驻时，验收照样能跑，且**绝不杀**真实 agent
  （套件只管理自己 subprocess 拉起来的 pid）。

运行（docs/验收.md 有完整说明）：

    cd D:\\ds植入式
    $env:PYTHONPATH = "$PWD\\src"
    python tests\\acceptance\\run_m1a_acceptance.py

可选参数：

    --json <path>          把机器可读结果写到指定路径（默认写 %TEMP%\\fireloop-acceptance\\）
    --skip-graceful-exit   跳过 Ctrl+Break 优雅退出项并标 SKIP（无控制台环境用；默认真测）

退出码：0 = 全部通过（SKIP 不算失败）；1 = 有失败项或脚本内部错误。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from fireloop import client as fireloop_client  # noqa: E402
from fireloop import protocol  # noqa: E402
from fireloop.single_instance import DEFAULT_MUTEX_NAME  # noqa: E402

PYTHON = sys.executable
PIPE_NAMESPACE = "\\\\.\\pipe"
RESULT_DIR_NAME = "fireloop-acceptance"  # %TEMP% 下持久保存机器可读结果，不随清场删除
DEFAULT_JSON_PATH = Path(tempfile.gettempdir()) / RESULT_DIR_NAME / (
    f"result-{datetime.now():%Y%m%d-%H%M%S}-pid{os.getpid()}.json"
)

_CTRL_BREAK_EVENT = 1

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_GenerateConsoleCtrlEvent = _kernel32.GenerateConsoleCtrlEvent
_GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
_GenerateConsoleCtrlEvent.restype = wintypes.BOOL

_SYNCHRONIZE = 0x00100000
_ERROR_FILE_NOT_FOUND = 2

_OpenMutexW = _kernel32.OpenMutexW
_OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_OpenMutexW.restype = wintypes.HANDLE

_CloseHandle = _kernel32.CloseHandle
_CloseHandle.argtypes = (wintypes.HANDLE,)
_CloseHandle.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------


class _Fail(Exception):
    """某一条验收项失败（记 FAIL）。"""


class _Skip(Exception):
    """某一条验收项不适用（记 SKIP）。"""


def require(condition: object, message: str = "") -> None:
    """验收断言：失败抛 _Fail，由 check() 记成 FAIL 而不是让脚本崩掉。"""
    if not condition:
        raise _Fail(message or "断言失败")


def _short(text: str | None, limit: int = 200) -> str:
    """把多行输出压成一行，方便人类看。"""
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


# ---------------------------------------------------------------------------
# Win32 / 环境探测小工具（只读探测，不产生副作用）
# ---------------------------------------------------------------------------


def pipe_exists(pipe_name: str) -> bool | None:
    """只读检查命名管道是否存在；列目录失败返回 None（未知）。"""
    short = pipe_name[len(PIPE_NAMESPACE):].lstrip("\\")
    try:
        return short in os.listdir(PIPE_NAMESPACE)
    except OSError:
        return None


def mutex_exists(mutex_name: str) -> bool | None:
    """只读检查命名互斥体是否存在（OpenMutexW + CloseHandle，无副作用）。"""
    ctypes.set_last_error(0)
    handle = _OpenMutexW(_SYNCHRONIZE, False, mutex_name)
    if handle:
        _CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == _ERROR_FILE_NOT_FOUND:
        return False
    return None  # 拒绝访问等：状态未知，不猜


def send_ctrl_break(pid: int) -> tuple[bool, int]:
    """给指定进程组投递 CTRL_BREAK；返回 (是否投递成功, GetLastError)。"""
    ctypes.set_last_error(0)
    ok = _GenerateConsoleCtrlEvent(_CTRL_BREAK_EVENT, pid)
    return bool(ok), ctypes.get_last_error()


def temp_accept_entries() -> set[str]:
    """%TEMP% 下 fireloop-accept-* 条目（不含持久结果目录）。"""
    root = Path(tempfile.gettempdir())
    try:
        return {
            p.name
            for p in root.glob("fireloop-accept-*")
            if p.name != RESULT_DIR_NAME
        }
    except OSError:
        return set()


# ---------------------------------------------------------------------------
# 子进程输出读取：后台线程收行，主线程带 deadline 等待，任何情况不挂死
# ---------------------------------------------------------------------------


class LinePump:
    """把子进程 stdout 按行收进内存，供等待启动行 / 收尾取尾部输出。"""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="accept-stdout", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            for line in self._stream:
                with self._lock:
                    self._lines.append(line.rstrip("\r\n"))
        except Exception:  # noqa: BLE001 —— 读线程异常不能影响验收主流程
            pass
        finally:
            self._done.set()

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def wait_startup(
        self, proc: subprocess.Popen | None, needle: str = "已启动", timeout: float = 10.0
    ) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.snapshot():
                if needle in line:
                    return line
            if proc is not None and proc.poll() is not None:
                break
            if self._done.is_set():
                break
            time.sleep(0.02)
        lines = self.snapshot()
        code = proc.poll() if proc is not None else "?"
        raise _Fail(f"agent 未在 {timeout:.0f}s 内打印启动行，退出码={code}，输出={lines!r}")

    def drain(self, timeout: float = 3.0) -> list[str]:
        """进程退出后把剩余输出收完。"""
        self._done.wait(timeout)
        time.sleep(0.05)
        return self.snapshot()


# ---------------------------------------------------------------------------
# 验收上下文：隔离名、临时目录、子进程台账、结果列表
# ---------------------------------------------------------------------------


class Context:
    def __init__(self, skip_graceful_exit: bool):
        self.skip_graceful_exit = skip_graceful_exit
        self.pid = os.getpid()
        self.tag = f"{self.pid}-{uuid.uuid4().hex[:8]}"
        self.pipe_name = rf"{PIPE_NAMESPACE}\fireloop-accept-{self.tag}"
        self.mutex_name = rf"Local\FireLoopAccept-{self.tag}"
        self.second_pipe = self.pipe_name + "-second"

        # 必须在创建临时目录之前快照，才能判断本次运行有没有留下新东西。
        self.temp_before = temp_accept_entries()
        self.default_pipe_before = pipe_exists(protocol.PIPE_NAME)
        self.default_mutex_before = mutex_exists(DEFAULT_MUTEX_NAME)
        self.real_agent_detected = bool(self.default_pipe_before) or bool(
            self.default_mutex_before
        )

        self.home = tempfile.mkdtemp(prefix=f"fireloop-accept-home-{self.tag}-")
        self.json_path = Path(DEFAULT_JSON_PATH)

        self.results: list[dict] = []
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.agent: subprocess.Popen | None = None
        self.pump: LinePump | None = None
        self.startup_line = ""
        self.token = ""
        self.token_path = Path(self.home) / "agent.token"
        self.graceful_ok = False
        self.agent_exited = False
        self.log_records: list[dict] = []
        self.cleanup_problems: list[str] = []

    # ---------- 子进程 ----------

    def child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["FIRELOOP_HOME"] = self.home
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        return env

    def spawn(self, label: str, args: list[str], **kwargs) -> subprocess.Popen:
        proc = subprocess.Popen(
            [PYTHON, *args],
            cwd=str(ROOT),
            env=self.child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        self.procs.append((label, proc))
        return proc

    def run_cli(
        self, args: list[str], timeout: float = 30.0
    ) -> tuple[subprocess.CompletedProcess, float]:
        """跑一次 CLI；永远显式带隔离管道名，绝不碰生产管道。"""
        command = [PYTHON, "-m", "fireloop.cli", *args, "--pipe-name", self.pipe_name]
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                cwd=str(ROOT),
                env=self.child_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise _Fail(f"CLI 超过 {timeout:.0f}s 未退出：args={args!r}") from exc
        return proc, time.monotonic() - started

    def stop_agent_hard(self) -> None:
        """非优雅路径下强行停掉测试 agent（只动自己拉起的 pid）。"""
        agent = self.agent
        if agent is not None and agent.poll() is None:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait(timeout=5)
        self.agent_exited = True
        if self.pump is not None:
            self.pump.drain()
        return None

    # ---------- 清理 ----------

    def cleanup(self) -> list[str]:
        """成功、失败、异常路径统一走这里：杀测试进程 + 删临时目录。幂等。"""
        problems: list[str] = []
        for label, proc in self.procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    problems.append(f"发现并终止残留进程：{label} pid={proc.pid}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"终止进程失败：{label} pid={proc.pid}：{exc!r}")
        if self.pump is not None:
            self.pump.drain()

        home = Path(self.home)
        if home.exists():
            for _ in range(10):
                try:
                    shutil.rmtree(home)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.2)
            if home.exists():
                problems.append(f"临时目录删不掉：{home}")
        self.cleanup_problems = problems
        return problems


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def print_header(ctx: Context) -> None:
    real = "检测到真实 agent 常驻" if ctx.real_agent_detected else "未检测到真实 agent"
    print("Fire Loop 验收套件（M1a，隔离模式）")
    print("=" * 78)
    print(f"python              : {PYTHON}")
    print(f"项目根目录          : {ROOT}")
    print(f"隔离管道            : {ctx.pipe_name}")
    print(f"隔离互斥体          : {ctx.mutex_name}")
    print(f"临时 FIRELOOP_HOME  : {ctx.home}")
    print(f"生产管道（不碰）    : {protocol.PIPE_NAME}（{real}）")
    print(f"生产互斥体（不碰）  : {DEFAULT_MUTEX_NAME}")
    print("-" * 78)


def record(ctx: Context, check_id: str, name: str, status: str, detail: str = "") -> None:
    ctx.results.append(
        {
            "id": check_id,
            "name": name,
            "status": status,
            "detail": detail,
            "duration_s": None,
        }
    )
    print(f"[{status}] {check_id}. {name}" + (f" —— {detail}" if detail else ""))


def check(ctx: Context, check_id: str, name: str, func) -> None:
    """执行一条验收项：PASS/FAIL/SKIP 都记下来，绝不因为单项异常中断整套。"""
    started = time.monotonic()
    try:
        detail = func()
        status = "PASS"
    except _Skip as exc:
        status, detail = "SKIP", str(exc)
    except _Fail as exc:
        status, detail = "FAIL", str(exc)
    except Exception as exc:  # noqa: BLE001
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    ctx.results.append(
        {
            "id": check_id,
            "name": name,
            "status": status,
            "detail": detail or "",
            "duration_s": round(elapsed, 3),
        }
    )
    suffix = f" —— {detail}" if detail else ""
    print(f"[{status}] {check_id}. {name}{suffix}  ({elapsed:.2f}s)")


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------


def ensure_agent(ctx: Context) -> subprocess.Popen:
    if ctx.agent is None:
        raise _Skip("测试 agent 未启动，本项跳过")
    if ctx.agent.poll() is not None:
        raise _Skip(f"测试 agent 已退出（exit={ctx.agent.returncode}），本项跳过")
    return ctx.agent


def write_token(ctx: Context, token: str) -> None:
    ctx.token_path.write_text(token + "\n", encoding="utf-8")


def blast_raw(pipe_name: str, payload: bytes) -> str:
    """绕过 CLI 直灌原始字节；服务端拒绝并断开属于预期，写失败不算失败。"""
    try:
        handle = open(pipe_name, "r+b", buffering=0)
    except OSError as exc:
        raise _Fail(f"连不上隔离管道 {pipe_name}：{exc}") from exc
    try:
        try:
            handle.write(payload)
            outcome = "写入完成"
        except OSError as exc:
            outcome = f"写入被服务端断开（预期内）：WinError {getattr(exc, 'winerror', '?')}"
    finally:
        try:
            handle.close()
        except OSError:
            pass
    return outcome


def first_line(text: str | None) -> str:
    return _short((text or "").strip().splitlines()[0] if (text or "").strip() else "", 160)


def load_logs(ctx: Context) -> tuple[list[Path], list[dict], str]:
    """读临时 HOME 下的 JSONL 日志；返回 (文件列表, 记录列表, 全文)。"""
    log_dir = Path(ctx.home) / "logs"
    files = sorted(log_dir.glob("agent-*.jsonl")) if log_dir.is_dir() else []
    records: list[dict] = []
    raw_parts: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        raw_parts.append(text)
        for index, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise _Fail(f"{path.name} 第 {index} 行不是合法 JSON：{line[:120]!r}") from exc
    return files, records, "\n".join(raw_parts)


# ---------------------------------------------------------------------------
# 验收项
# ---------------------------------------------------------------------------


def run_checks(ctx: Context) -> None:
    # 0. agent 未运行时 CLI 快速报错 --------------------------------------
    def t0_offline() -> str:
        proc, elapsed = ctx.run_cli(["ping"])
        require(
            proc.returncode == protocol.CLI_EXIT_OFFLINE,
            f"exit={proc.returncode}（期望 {protocol.CLI_EXIT_OFFLINE}），"
            f"stderr={_short(proc.stderr)!r}",
        )
        require("没在运行" in proc.stderr, f"stderr 未明确报没在运行：{_short(proc.stderr)!r}")
        require("Traceback" not in proc.stderr, f"stderr 出现裸 traceback：{_short(proc.stderr)!r}")
        require(elapsed < 2.0, f"耗时 {elapsed:.2f}s，超过 2s 要求")
        return f"exit=1 耗时={elapsed:.2f}s stderr={first_line(proc.stderr)!r}"

    check(ctx, "0", "agent 未运行时 CLI 快速报错、退出码 1（<2s，不挂死）", t0_offline)

    # 1. 启动测试 agent（隔离管道 + 隔离互斥体） --------------------------
    def t1_startup() -> str:
        proc = ctx.spawn(
            "测试 agent",
            [
                "-m",
                "fireloop.agent",
                "--pipe-name",
                ctx.pipe_name,
                "--mutex-name",
                ctx.mutex_name,
            ],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        ctx.agent = proc
        ctx.pump = LinePump(proc.stdout)
        line = ctx.pump.wait_startup(proc)
        ctx.startup_line = line
        require("已启动" in line, f"启动行不含'已启动'：{line!r}")
        require(ctx.pipe_name in line, f"启动行不含隔离管道名：{line!r}")
        require(proc.poll() is None, f"agent 打印启动行后立即退出，exit={proc.returncode}")
        require(pipe_exists(ctx.pipe_name) is True, "隔离管道未出现在命名管道空间")
        return f"启动行={line!r}"

    check(ctx, "1", "测试 agent 用隔离管道/互斥体启动并打印启动行", t1_startup)

    # 1b. token 自动生成 ---------------------------------------------------
    def t1b_token() -> str:
        require(ctx.token_path.is_file(), f"token 文件不存在：{ctx.token_path}")
        token = ctx.token_path.read_text(encoding="utf-8").strip()
        require(
            re.fullmatch(r"[0-9a-f]{64}", token) is not None,
            f"token 不是 64 位小写 hex（长度 {len(token)}）",
        )
        ctx.token = token
        return f"token 文件自动生成，64 位 hex（{ctx.token_path}）"

    check(ctx, "1b", "agent 启动后自动生成 token（64 位 hex）", t1b_token)

    # 2. ping → pong ------------------------------------------------------
    def t2_ping() -> str:
        ensure_agent(ctx)
        proc, elapsed = ctx.run_cli(["ping"])
        require(proc.returncode == 0, f"exit={proc.returncode}（期望 0），stderr={_short(proc.stderr)!r}")
        require(proc.stdout.strip() == "pong", f"stdout={proc.stdout.strip()!r}（期望 pong）")
        return f"exit=0 stdout=pong 耗时={elapsed:.2f}s"

    check(ctx, "2", "cli ping → pong，退出码 0", t2_ping)

    # 3. task → 回显 ------------------------------------------------------
    def t3_task() -> str:
        ensure_agent(ctx)
        proc, elapsed = ctx.run_cli(["task", "M1a 验收任务"])
        require(proc.returncode == 0, f"exit={proc.returncode}（期望 0），stderr={_short(proc.stderr)!r}")
        require(proc.stdout.strip() == "收到任务", f"stdout={proc.stdout.strip()!r}（期望 收到任务）")
        return f"exit=0 stdout=收到任务 耗时={elapsed:.2f}s"

    check(ctx, "3", "cli task 把任务送到 agent 并回显，退出码 0", t3_task)

    # 4. 连续 5 轮 ping + task（回归“管道空窗”） --------------------------
    def t4_rounds() -> str:
        ensure_agent(ctx)
        failures: list[str] = []
        timings: list[float] = []
        for index in range(5):
            ping_proc, ping_elapsed = ctx.run_cli(["ping"])
            task_proc, task_elapsed = ctx.run_cli(["task", f"连续任务 {index}"])
            timings.extend([ping_elapsed, task_elapsed])
            if ping_proc.returncode != 0 or ping_proc.stdout.strip() != "pong":
                failures.append(
                    f"#{index} ping exit={ping_proc.returncode} stdout={ping_proc.stdout.strip()!r}"
                )
            if task_proc.returncode != 0 or task_proc.stdout.strip() != "收到任务":
                failures.append(
                    f"#{index} task exit={task_proc.returncode} stdout={task_proc.stdout.strip()!r}"
                )
            if "没在运行" in (ping_proc.stderr + task_proc.stderr):
                failures.append(f"#{index} 出现 agent_offline")
        require(not failures, "；".join(failures))
        return f"5 轮 ping+task 全 OK，单次平均 {(sum(timings) / len(timings)) * 1000:.0f}ms"

    check(ctx, "4", "ping/task 连续 5 轮不出现偶发 agent_offline（管道空窗回归）", t4_rounds)

    # 5. 同一互斥体的第二个实例被挡 ---------------------------------------
    def t5_second_instance() -> str:
        ensure_agent(ctx)
        try:
            proc = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "fireloop.agent",
                    "--pipe-name",
                    ctx.second_pipe,
                    "--mutex-name",
                    ctx.mutex_name,
                ],
                cwd=str(ROOT),
                env=ctx.child_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise _Fail("第二实例 30s 未退出（应当立即被单实例锁挡住）") from exc
        combined = proc.stdout + proc.stderr
        require(proc.returncode == 1, f"exit={proc.returncode}（期望 1），输出={_short(combined)!r}")
        require("已经在运行" in combined, f"未打印'已经在运行'：{_short(combined)!r}")
        require("Traceback" not in combined, "第二实例打印了 traceback")
        require(pipe_exists(ctx.second_pipe) is False, "第二实例竟然创建了自己的管道")
        return f"exit=1 输出={_short(combined)!r}"

    check(
        ctx,
        "5",
        "同一隔离互斥体的第二个 agent 实例被挡，退出码 1 并打印'已经在运行'",
        t5_second_instance,
    )

    # 6. 错 token → 退出码 3 ----------------------------------------------
    def t6_bad_token() -> str:
        ensure_agent(ctx)
        original = ctx.token
        write_token(ctx, "0" * 64)
        try:
            proc, elapsed = ctx.run_cli(["ping"])
        finally:
            write_token(ctx, original)
        require(
            proc.returncode == protocol.CLI_EXIT_PROTOCOL,
            f"exit={proc.returncode}（期望 {protocol.CLI_EXIT_PROTOCOL}），"
            f"stderr={_short(proc.stderr)!r}",
        )
        require(protocol.CODE_BAD_TOKEN in proc.stderr, f"stderr 未出现 bad_token：{_short(proc.stderr)!r}")
        require("Traceback" not in proc.stderr, "stderr 出现裸 traceback")
        return f"exit=3 stderr={first_line(proc.stderr)!r} 耗时={elapsed:.2f}s"

    check(ctx, "6", "token 错误时 CLI 明确报错，退出码 3 且打印 bad_token", t6_bad_token)

    def t6b_token_restore() -> str:
        ensure_agent(ctx)
        proc, _ = ctx.run_cli(["ping"])
        require(proc.returncode == 0 and proc.stdout.strip() == "pong", f"exit={proc.returncode}")
        return "恢复 token 后 ping 正常"

    check(ctx, "6b", "恢复正确 token 后 CLI 恢复可用", t6b_token_restore)

    # 7. 非法 JSON + 超长行直灌管道，agent 不崩 ---------------------------
    def t7_garbage() -> str:
        ensure_agent(ctx)
        invalid = blast_raw(ctx.pipe_name, b"this is not json\n")
        time.sleep(0.2)
        oversize = blast_raw(
            ctx.pipe_name,
            b'{"v":1,"type":"task","text":"'
            + b"x" * (protocol.MAX_LINE_BYTES * 2)
            + b'"}\n',
        )
        time.sleep(0.3)
        require(ctx.agent.poll() is None, "agent 收到非法输入后崩溃")
        proc, elapsed = ctx.run_cli(["ping"])
        require(proc.returncode == 0 and proc.stdout.strip() == "pong", f"后续 ping exit={proc.returncode}")
        return f"非法 JSON：{invalid}；超长行：{oversize}；agent 存活，后续 ping 耗时={elapsed:.2f}s"

    check(
        ctx,
        "7",
        "非法 JSON / 超长行直灌管道后 agent 存活且后续 ping 正常",
        t7_garbage,
    )

    # 7b. 鉴权后的协议错误不崩：缺字段 / 未知 type / 超长 task ------------
    def t7b_protocol_errors() -> str:
        ensure_agent(ctx)
        cases = [
            (protocol.CODE_BAD_MSG, {"v": 1, "id": "acc-bad-msg", "type": "task"}),
            (protocol.CODE_UNKNOWN_TYPE, {"v": 1, "id": "acc-unknown", "type": "bogus"}),
            (
                protocol.CODE_TOO_LARGE,
                {"v": 1, "id": "acc-too-large", "type": "task", "text": "y" * (protocol.MAX_TASK_TEXT + 100)},
            ),
        ]
        seen: list[str] = []
        for expected, message in cases:
            try:
                fireloop_client.call(message, pipe_name=ctx.pipe_name, token=ctx.token)
            except fireloop_client.AgentProtocolError as exc:
                require(
                    exc.code == expected,
                    f"期望错误码 {expected}，实际 {exc.code}（{exc.message}）",
                )
                seen.append(exc.code)
            else:
                raise _Fail(f"期望错误码 {expected}，服务端却返回成功")
        require(ctx.agent.poll() is None, "协议错误后 agent 崩溃")
        proc, _ = ctx.run_cli(["ping"])
        require(proc.returncode == 0 and proc.stdout.strip() == "pong", f"后续 ping exit={proc.returncode}")
        return f"错误码 {seen}，agent 存活且 ping 正常"

    check(
        ctx,
        "7b",
        "缺字段 / 未知 type / 超长 task 返回协议错误且 agent 不崩",
        t7b_protocol_errors,
    )

    # 8a. Ctrl+Break 优雅退出 ---------------------------------------------
    def t8a_graceful() -> str:
        ensure_agent(ctx)
        if ctx.skip_graceful_exit:
            ctx.stop_agent_hard()
            raise _Skip("按 --skip-graceful-exit 跳过；测试 agent 已强制停止，agent_stop 日志项一并跳过")
        agent = ctx.agent
        delivered, error = send_ctrl_break(agent.pid)
        if not delivered:
            ctx.stop_agent_hard()
            raise _Skip(
                f"当前环境无法投递 Ctrl+Break（GenerateConsoleCtrlEvent 失败，Windows 错误 {error}）；"
                "已强制停止测试 agent，可用 --skip-graceful-exit 明确跳过"
            )
        try:
            exit_code = agent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            agent.terminate()
            try:
                exit_code = agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
                exit_code = agent.wait(timeout=5)
            raise _Fail(f"agent 收到 Ctrl+Break 后 10s 内未退出（已强制终止，exit={exit_code}）")
        lines = ctx.pump.drain() if ctx.pump is not None else []
        ctx.graceful_ok = True
        ctx.agent_exited = True
        tail = lines[-3:]
        require(exit_code == 0, f"退出码 {exit_code}（期望 0），尾部输出={tail!r}")
        require(any("已退出" in line for line in lines), f"未打印'已退出'，尾部输出={tail!r}")
        return f"exit=0，尾部输出={tail[-1] if tail else ''!r}"

    check(
        ctx,
        "8a",
        "agent 收到 Ctrl+Break 优雅退出：退出码 0、打印'已退出'",
        t8a_graceful,
    )

    # 8b. 退出后 CLI 离线 -------------------------------------------------
    def t8b_offline_after_exit() -> str:
        require(ctx.agent_exited or (ctx.agent is not None and ctx.agent.poll() is not None), "agent 居然还在运行")
        proc, elapsed = ctx.run_cli(["ping"])
        require(proc.returncode == protocol.CLI_EXIT_OFFLINE, f"exit={proc.returncode}（期望 1）")
        require("没在运行" in proc.stderr, f"stderr 未明确报没在运行：{_short(proc.stderr)!r}")
        require(elapsed < 2.0, f"耗时 {elapsed:.2f}s，超过 2s")
        return f"exit=1 耗时={elapsed:.2f}s"

    check(ctx, "8b", "agent 退出后 CLI 明确报没在运行，退出码 1 且不挂死", t8b_offline_after_exit)

    # 9. 日志 JSONL -------------------------------------------------------
    def t9_log_start() -> str:
        files, records, _ = load_logs(ctx)
        ctx.log_records = records
        require(bool(files), f"日志目录没有 agent-*.jsonl：{Path(ctx.home) / 'logs'}")
        events = [record.get("event") for record in records]
        require("agent_start" in events, f"日志缺少 agent_start；事件={sorted(set(events))}")
        starts = [record for record in records if record.get("event") == "agent_start"]
        require(
            all(record.get("pipe") == ctx.pipe_name for record in starts),
            f"agent_start 的 pipe 不是隔离管道：{[record.get('pipe') for record in starts]!r}",
        )
        return f"文件={[file.name for file in files]}，事件={sorted(set(events))}"

    check(ctx, "9", "日志是 JSONL，含 agent_start 且 pipe 为本次隔离管道", t9_log_start)

    def t9b_log_stop() -> str:
        if not ctx.graceful_ok:
            raise _Skip("未执行 Ctrl+Break 优雅退出，本项跳过（只记 agent_start）")
        require(bool(ctx.log_records), "日志记录为空，无法核对 agent_stop")
        events = [record.get("event") for record in ctx.log_records]
        require("agent_stop" in events, f"日志缺少 agent_stop；事件={sorted(set(events))}")
        stops = [record for record in ctx.log_records if record.get("event") == "agent_stop"]
        reasons = [record.get("reason") for record in stops]
        require(
            all(record.get("pipe") == ctx.pipe_name for record in stops),
            f"agent_stop 的 pipe 不是隔离管道：{[record.get('pipe') for record in stops]!r}",
        )
        return f"agent_stop reason={reasons}"

    check(ctx, "9b", "日志 JSONL 含 agent_stop（优雅退出路径）", t9b_log_stop)

    def t9c_log_no_sensitive() -> str:
        files, records, raw = load_logs(ctx)
        require(bool(files), "没有日志文件，无法核对敏感信息")
        require("M1a 验收任务" not in raw, "日志出现任务原文'M1a 验收任务'")
        require("连续任务" not in raw, "日志出现任务原文'连续任务'")
        require(ctx.token not in raw, "日志出现 token")
        require('"text":' not in raw, '日志出现 "text" 字段（应只记 text_len/text_sha8）')
        task_events = [
            record for record in records if record.get("event") == "request" and record.get("type") == "task"
        ]
        require(
            bool(task_events) and all("text_len" in record and "text_sha8" in record for record in task_events),
            "task 请求日志缺少 text_len/text_sha8 指纹",
        )
        return f"任务只记 {task_events[0].get('text_len')} 字节 / sha8={task_events[0].get('text_sha8')}，无原文与 token"

    check(ctx, "9c", "日志不含任务原文与 token（只记长度/sha256 前 8 位）", t9c_log_no_sensitive)

    # 10. 无残留进程 ------------------------------------------------------
    def t10_no_leftover_procs() -> str:
        alive = [label for label, proc in ctx.procs if proc.poll() is None]
        require(not alive, f"仍有测试进程存活：{alive}")
        require(
            pipe_exists(ctx.pipe_name) is False,
            f"隔离管道仍然存在（agent 可能还在跑）：{ctx.pipe_name}",
        )
        require(
            pipe_exists(ctx.second_pipe) is False,
            f"第二实例管道仍然存在：{ctx.second_pipe}",
        )
        return f"已跟踪 {len(ctx.procs)} 个测试进程全部退出，隔离管道已消失"

    check(ctx, "10", "无残留测试进程（隔离管道已消失）", t10_no_leftover_procs)

    # 11. 零模型 / 注册表调用（静态红线） --------------------------------
    def t11_no_model_registry() -> str:
        forbidden = ("requests", "urllib.request", "httpx", "openai", "winreg")
        hits: list[str] = []
        for path in sorted((SRC / "fireloop").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for word in forbidden:
                if word in text:
                    hits.append(f"{path.name}:{word}")
        require(not hits, f"命中禁止引用：{hits}")
        return f"已扫描 {len(list((SRC / 'fireloop').glob('*.py')))} 个生产模块，无模型/网络/注册表调用"

    check(ctx, "11", "生产代码无模型 / 网络 / 注册表调用（M1a 红线）", t11_no_model_registry)

    # 清场：杀残留、删临时目录（后续 12/13 依赖清场结果）
    problems = ctx.cleanup()
    if problems:
        print(f"  清场时发现问题：{problems}")

    # 12. 无残留临时目录 --------------------------------------------------
    def t12_no_leftover_temp() -> str:
        require(not Path(ctx.home).exists(), f"临时数据目录未删除：{ctx.home}")
        new_entries = temp_accept_entries() - ctx.temp_before
        require(not new_entries, f"%TEMP% 下出现新的 fireloop-accept-* 残留：{sorted(new_entries)}")
        require(not ctx.cleanup_problems, f"清场问题：{ctx.cleanup_problems}")
        return f"临时目录已删除，%TEMP% 无新增残留"

    check(ctx, "12", "清场：临时数据目录已删除、%TEMP% 无新增残留", t12_no_leftover_temp)

    # 13. 隔离自检：全程没碰生产管道 / 互斥体 ---------------------------
    def t13_isolation() -> str:
        require(ctx.pipe_name != protocol.PIPE_NAME, "隔离管道名等于生产管道名")
        require(ctx.mutex_name != DEFAULT_MUTEX_NAME, "隔离互斥体名等于生产互斥体名")
        require(ctx.pipe_name.startswith(PIPE_NAMESPACE + "\\"), "隔离管道名前缀不对")
        after_pipe = pipe_exists(protocol.PIPE_NAME)
        after_mutex = mutex_exists(DEFAULT_MUTEX_NAME)
        require(
            after_pipe == ctx.default_pipe_before,
            f"生产管道状态被改变：{ctx.default_pipe_before} → {after_pipe}",
        )
        require(
            after_mutex == ctx.default_mutex_before,
            f"生产互斥体状态被改变：{ctx.default_mutex_before} → {after_mutex}",
        )
        acquired = [
            record.get("name")
            for record in ctx.log_records
            if record.get("event") == "single_instance_acquired"
        ]
        if not acquired:
            raise _Skip("日志不可用，无法核对 agent 实际使用的互斥体名")
        require(
            ctx.mutex_name in acquired,
            f"日志记录的互斥体名不是隔离名：{acquired!r}",
        )
        real = "检测到真实 agent 常驻" if ctx.real_agent_detected else "无真实 agent 常驻"
        return (
            f"生产管道 {ctx.default_pipe_before} → {after_pipe}，"
            f"生产互斥体 {ctx.default_mutex_before} → {after_mutex}；{real}；"
            f"agent 实际互斥体={ctx.mutex_name}"
        )

    check(ctx, "13", "隔离自检：全程未触碰生产管道/互斥体，真实 agent 状态不变", t13_isolation)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_m1a_acceptance.py",
        description="Fire Loop M1a 集成验收（隔离模式，可与真实常驻 agent 共存）",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        metavar="PATH",
        help=(
            "机器可读结果的输出路径；默认写 "
            f"{DEFAULT_JSON_PATH.parent}\\result-<时间>-pid<pid>.json"
        ),
    )
    parser.add_argument(
        "--skip-graceful-exit",
        action="store_true",
        help="跳过 Ctrl+Break 优雅退出项（无控制台环境用，该项标记 SKIP）",
    )
    return parser.parse_args(argv)


def write_json(ctx: Context) -> tuple[bool, str, int]:
    """写机器可读结果；返回 (是否成功, 路径, 失败数)。"""
    total = len(ctx.results)
    passed = sum(1 for item in ctx.results if item["status"] == "PASS")
    failed = sum(1 for item in ctx.results if item["status"] == "FAIL")
    skipped = sum(1 for item in ctx.results if item["status"] == "SKIP")
    payload = {
        "schema": "fireloop.acceptance.v1",
        "suite": "M1a 集成验收",
        "started_at": ctx.started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_s": round(time.monotonic() - ctx.started_monotonic, 3),
        "python": {
            "executable": PYTHON,
            "version": sys.version.split()[0],
        },
        "isolation": {
            "pipe_name": ctx.pipe_name,
            "mutex_name": ctx.mutex_name,
            "fireloop_home": ctx.home,
            "default_pipe": protocol.PIPE_NAME,
            "default_mutex": DEFAULT_MUTEX_NAME,
            "real_agent_detected": ctx.real_agent_detected,
            "default_pipe_before": ctx.default_pipe_before,
            "default_pipe_after": pipe_exists(protocol.PIPE_NAME),
            "default_mutex_before": ctx.default_mutex_before,
            "default_mutex_after": mutex_exists(DEFAULT_MUTEX_NAME),
        },
        "options": {"skip_graceful_exit": ctx.skip_graceful_exit},
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
        "exit_code": 0 if failed == 0 else 1,
        "results": ctx.results,
    }
    path = Path(ctx.json_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(f"写机器可读结果失败：{exc!r}")
        return False, str(path), failed
    return True, str(path), failed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ctx = Context(skip_graceful_exit=args.skip_graceful_exit)
    ctx.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    ctx.started_monotonic = time.monotonic()
    if args.json_path:
        ctx.json_path = Path(args.json_path)

    try:
        print_header(ctx)
        try:
            run_checks(ctx)
        except Exception:  # noqa: BLE001 —— 脚本自身 bug 也要给可读结果，不能裸崩
            detail = traceback.format_exc(limit=3)
            record(ctx, "internal", "验收脚本内部异常", "FAIL", _short(detail, 500))
    finally:
        # 兜底清场：无论上面走到哪一步，测试进程和临时目录都不能留。
        leftover = ctx.cleanup()
        if leftover:
            print(f"兜底清场发现问题：{leftover}")

    json_ok, json_path, failed = write_json(ctx)
    passed = sum(1 for item in ctx.results if item["status"] == "PASS")
    skipped = sum(1 for item in ctx.results if item["status"] == "SKIP")
    total = len(ctx.results)
    elapsed = time.monotonic() - ctx.started_monotonic

    print("=" * 78)
    print(f"验收结果：{total} 项，PASS {passed} / FAIL {failed} / SKIP {skipped}，耗时 {elapsed:.2f}s")
    if failed:
        print("失败项：")
        for item in ctx.results:
            if item["status"] == "FAIL":
                print(f"  - {item['id']}. {item['name']}：{item['detail']}")
    if not json_ok:
        print("机器可读结果写入失败，按失败处理。")
        return 1
    print(f"机器可读结果：{json_path}")
    if failed:
        return 1
    print("M1a 验收全部通过。" if skipped == 0 else f"M1a 验收通过（{skipped} 项 SKIP，见上）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
