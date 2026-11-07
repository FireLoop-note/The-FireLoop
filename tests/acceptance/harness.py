"""Fire Loop 验收套件公共底座（被 run_m1a_acceptance.py 和各 checks_*.py 引用）。

这里放所有验收套件共用的东西：

- 隔离环境（唯一管道/互斥体/临时 FIRELOOP_HOME）与子进程台账；
- PASS/FAIL/SKIP 记录、汇总、机器可读 JSON；
- Win32 只读探测（管道是否存在、互斥体是否存在、Ctrl+Break 投递）；
- agent 的启动/优雅停止/强制停止、CLI 调用、日志读取；
- 成功/失败/内部异常统一清场。

具体验收项在 checks_m1a.py / checks_b1.py / checks_b2.py；
入口和套件编排在 run_m1a_acceptance.py。

红线：本模块**只读探测**生产管道/互斥体，不连生产管道、不杀非本套件拉起的进程；
桌面事件真实操作只有 checks_b2.py 在显式 --enable-desktop-events 时才做。
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

from fireloop import protocol  # noqa: E402
from fireloop.single_instance import DEFAULT_MUTEX_NAME  # noqa: E402

PYTHON = sys.executable
PIPE_NAMESPACE = "\\\\.\\pipe"
RESULT_DIR_NAME = "fireloop-acceptance"  # %TEMP% 下持久保存机器可读结果，不随清场删除
DEFAULT_JSON_PATH = Path(tempfile.gettempdir()) / RESULT_DIR_NAME / (
    f"result-{datetime.now():%Y%m%d-%H%M%S}-pid{os.getpid()}.json"
)

ALL_SUITES = ("m1a", "b1", "b2", "m2")

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
# 结果记录与断言
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

    def wait_line(
        self, proc: subprocess.Popen | None, needle: str, timeout: float = 10.0
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
        raise _Fail(f"未在 {timeout:.0f}s 内等到包含 {needle!r} 的输出，退出码={code}，输出={lines!r}")

    def wait_startup(
        self, proc: subprocess.Popen | None, needle: str = "已启动", timeout: float = 10.0
    ) -> str:
        return self.wait_line(proc, needle, timeout)

    def drain(self, timeout: float = 3.0) -> list[str]:
        """进程退出后把剩余输出收完。"""
        self._done.wait(timeout)
        time.sleep(0.05)
        return self.snapshot()


# ---------------------------------------------------------------------------
# 验收上下文：隔离名、临时目录、子进程台账、结果列表
# ---------------------------------------------------------------------------


class Context:
    def __init__(
        self,
        skip_graceful_exit: bool = False,
        enable_desktop_events: bool = False,
        enable_result_window: bool = False,
        suites: tuple[str, ...] = ALL_SUITES,
    ):
        self.skip_graceful_exit = skip_graceful_exit
        self.enable_desktop_events = enable_desktop_events
        self.enable_result_window = enable_result_window
        self.suites = suites
        self.pid = os.getpid()
        self.tag = f"{self.pid}-{uuid.uuid4().hex[:8]}"
        self.pipe_name = rf"{PIPE_NAMESPACE}\fireloop-accept-{self.tag}"
        self.mutex_name = rf"Local\FireLoopAccept-{self.tag}"
        self.second_pipe = self.pipe_name + "-second"
        # B2 事件阶段用的第二套隔离名（第一套在 M1a 阶段结束后已经释放）
        self.events_pipe = self.pipe_name + "-events"
        self.events_mutex = self.mutex_name + "-events"
        # M2 模型链路阶段用的第三套隔离名
        self.m2_pipe = self.pipe_name + "-m2"
        self.m2_mutex = self.mutex_name + "-m2"
        # B1 测试注册表前缀（唯一，绝不等于生产前缀 fireloop）
        self.registry_prefix = f"fireloop-test-{uuid.uuid4().hex[:8]}"

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
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.started_monotonic = time.monotonic()

    # ---------- 子进程 ----------

    def child_env(
        self,
        extra: dict[str, str] | None = None,
        remove: tuple[str, ...] = (),
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["FIRELOOP_HOME"] = self.home
        env["PYTHONPATH"] = str(SRC) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        for name in remove:
            env.pop(name, None)
        if extra:
            env.update(extra)
        return env

    def spawn(
        self,
        label: str,
        args: list[str],
        env_extra: dict[str, str] | None = None,
        env_remove: tuple[str, ...] = (),
        **kwargs,
    ) -> subprocess.Popen:
        proc = subprocess.Popen(
            [PYTHON, *args],
            cwd=str(ROOT),
            env=self.child_env(env_extra, env_remove),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        self.procs.append((label, proc))
        return proc

    def start_agent(
        self,
        pipe_name: str,
        mutex_name: str,
        label: str = "测试 agent",
        extra_args: tuple[str, ...] = (),
        timeout: float = 10.0,
        env_extra: dict[str, str] | None = None,
        env_remove: tuple[str, ...] = (),
    ) -> tuple[subprocess.Popen, LinePump, str]:
        """起一个隔离 agent 并等启动行；返回 (进程, 输出泵, 启动行)。"""
        proc = self.spawn(
            label,
            [
                "-m",
                "fireloop.agent",
                "--pipe-name",
                pipe_name,
                "--mutex-name",
                mutex_name,
                *extra_args,
            ],
            env_extra=env_extra,
            env_remove=env_remove,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        pump = LinePump(proc.stdout)
        line = pump.wait_startup(proc, timeout=timeout)
        return proc, pump, line

    def stop_agent_graceful(
        self, proc: subprocess.Popen, pump: LinePump | None, timeout: float = 10.0
    ) -> tuple[bool, int | None, list[str]]:
        """投递 Ctrl+Break 并等退出；返回 (是否成功投递, 退出码, 尾部输出)。"""
        delivered, error = send_ctrl_break(proc.pid)
        if not delivered:
            return False, None, pump.drain() if pump is not None else []
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                exit_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = proc.wait(timeout=5)
            return True, exit_code, pump.drain() if pump is not None else []
        return True, exit_code, pump.drain() if pump is not None else []

    def stop_agent_hard(self, proc: subprocess.Popen | None = None, pump: LinePump | None = None) -> None:
        """非优雅路径下强行停掉测试 agent（只动自己拉起的 pid）。"""
        proc = proc if proc is not None else self.agent
        pump = pump if pump is not None else self.pump
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc is not None:
            self.agent_exited = True
        if pump is not None:
            pump.drain()

    def run_cli(
        self,
        args: list[str],
        timeout: float = 30.0,
        pipe_name: str | None = None,
    ) -> tuple[subprocess.CompletedProcess, float]:
        """跑一次 CLI；默认带 M1a 隔离管道名，可显式指定其他隔离管道。"""
        command = [
            PYTHON,
            "-m",
            "fireloop.cli",
            *args,
            "--pipe-name",
            pipe_name or self.pipe_name,
        ]
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
# 记录 / 检查包装
# ---------------------------------------------------------------------------


def print_header(ctx: Context) -> None:
    real = "检测到真实 agent 常驻" if ctx.real_agent_detected else "未检测到真实 agent"
    print("Fire Loop 验收套件（隔离模式）")
    print("=" * 78)
    print(f"python              : {PYTHON}")
    print(f"项目根目录          : {ROOT}")
    print(f"验收套件            : {', '.join(ctx.suites)}")
    print(f"桌面事件真测        : {'开' if ctx.enable_desktop_events else '关（相关项 SKIP）'}")
    print(f"结果窗口真测        : {'开' if ctx.enable_result_window else '关（相关项 SKIP）'}")
    print(f"隔离管道            : {ctx.pipe_name}")
    print(f"隔离互斥体          : {ctx.mutex_name}")
    print(f"临时 FIRELOOP_HOME  : {ctx.home}")
    print(f"B1 测试注册表前缀   : {ctx.registry_prefix}")
    print(f"生产管道（不碰）    : {protocol.PIPE_NAME}（{real}）")
    print(f"生产互斥体（不碰）  : {DEFAULT_MUTEX_NAME}")
    print("-" * 78)


def record(ctx: Context, check_id: str, name: str, status: str, detail: str = "", suite: str = "m1a") -> None:
    ctx.results.append(
        {
            "id": check_id,
            "suite": suite,
            "name": name,
            "status": status,
            "detail": detail,
            "duration_s": None,
        }
    )
    print(f"[{status}] {check_id}. {name}" + (f" —— {detail}" if detail else ""))


def check(ctx: Context, check_id: str, name: str, func, suite: str = "m1a") -> None:
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
            "suite": suite,
            "name": name,
            "status": status,
            "detail": detail or "",
            "duration_s": round(elapsed, 3),
        }
    )
    suffix = f" —— {detail}" if detail else ""
    print(f"[{status}] {check_id}. {name}{suffix}  ({elapsed:.2f}s)")


# ---------------------------------------------------------------------------
# 公共小工具（子进程 / 日志 / 管道）
# ---------------------------------------------------------------------------


def ensure_agent(ctx: Context) -> subprocess.Popen:
    if ctx.agent is None:
        raise _Skip("测试 agent 未启动，本项跳过")
    if ctx.agent.poll() is not None:
        raise _Skip(f"测试 agent 已退出（exit={ctx.agent.returncode}），本项跳过")
    return ctx.agent


def write_token(ctx: Context, token: str, path: Path | None = None) -> None:
    (path or ctx.token_path).write_text(token + "\n", encoding="utf-8")


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
    stripped = (text or "").strip()
    return _short(stripped.splitlines()[0] if stripped else "", 160)


def load_logs(ctx: Context, name_glob: str = "agent-*.jsonl") -> tuple[list[Path], list[dict], str]:
    """读临时 HOME 下的 JSONL 日志；返回 (文件列表, 记录列表, 全文)。"""
    log_dir = Path(ctx.home) / "logs"
    files = sorted(log_dir.glob(name_glob)) if log_dir.is_dir() else []
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


def events_by_pipe(records: list[dict], pipe_name: str) -> list[dict]:
    """筛出属于指定隔离管道实例的日志（同一 HOME 下可能跑过两代 agent）。"""
    return [record for record in records if record.get("pipe") == pipe_name]


# ---------------------------------------------------------------------------
# 机器可读结果 + 入口
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_m1a_acceptance.py",
        description="Fire Loop 项目验收（隔离模式，可与真实常驻 agent 共存）",
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
    parser.add_argument(
        "--enable-desktop-events",
        action="store_true",
        help=(
            "真测剪贴板/前台窗口事件（会短暂改动当前桌面/剪贴板；"
            "默认跳过并标 SKIP，SKIP 不算通过）"
        ),
    )
    parser.add_argument(
        "--enable-result-window",
        action="store_true",
        help=(
            "真测结果展示窗口（会短暂弹窗打扰桌面；默认跳过并标 SKIP，SKIP 不算通过）"
        ),
    )
    parser.add_argument(
        "--suite",
        default="all",
        metavar="LIST",
        help=(
            "要跑的验收套件：all（默认）或逗号分隔的 m1a,b1,b2,m2，"
            "例如 --suite m1a 只跑 M1a 基线回归"
        ),
    )
    args = parser.parse_args(argv)
    selected: list[str] = []
    if args.suite.strip().lower() == "all":
        selected = list(ALL_SUITES)
    else:
        for part in args.suite.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in ALL_SUITES:
                parser.error(f"未知套件 {name!r}（可选：all,m1a,b1,b2,m2）")
            if name not in selected:
                selected.append(name)
        if not selected:
            parser.error("--suite 不能为空")
    # M1a 是基线：扩展套件的检查复用 M1a 起的测试 agent，所以 m1a 永远一起跑。
    if "m1a" not in selected:
        selected.insert(0, "m1a")
    args.suites = tuple(name for name in ALL_SUITES if name in selected)
    return args


def write_json(ctx: Context) -> tuple[bool, str, int]:
    """写机器可读结果；返回 (是否成功, 路径, 失败数)。"""
    total = len(ctx.results)
    passed = sum(1 for item in ctx.results if item["status"] == "PASS")
    failed = sum(1 for item in ctx.results if item["status"] == "FAIL")
    skipped = sum(1 for item in ctx.results if item["status"] == "SKIP")
    by_suite: dict[str, dict[str, int]] = {}
    for item in ctx.results:
        bucket = by_suite.setdefault(item.get("suite", "m1a"), {"total": 0, "passed": 0, "failed": 0, "skipped": 0})
        bucket["total"] += 1
        bucket["passed" if item["status"] == "PASS" else "failed" if item["status"] == "FAIL" else "skipped"] += 1
    payload = {
        "schema": "fireloop.acceptance.v1",
        "suites": list(ctx.suites),
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
            "events_pipe_name": ctx.events_pipe,
            "events_mutex_name": ctx.events_mutex,
            "m2_pipe_name": ctx.m2_pipe,
            "m2_mutex_name": ctx.m2_mutex,
            "fireloop_home": ctx.home,
            "default_pipe": protocol.PIPE_NAME,
            "default_mutex": DEFAULT_MUTEX_NAME,
            "real_agent_detected": ctx.real_agent_detected,
            "default_pipe_before": ctx.default_pipe_before,
            "default_pipe_after": pipe_exists(protocol.PIPE_NAME),
            "default_mutex_before": ctx.default_mutex_before,
            "default_mutex_after": mutex_exists(DEFAULT_MUTEX_NAME),
            "b1_test_registry_prefix": ctx.registry_prefix,
        },
        "options": {
            "skip_graceful_exit": ctx.skip_graceful_exit,
            "enable_desktop_events": ctx.enable_desktop_events,
            "enable_result_window": ctx.enable_result_window,
        },
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "by_suite": by_suite,
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


def run_main(argv: list[str] | None, pipeline) -> int:
    """通用入口：解析参数 → 建隔离上下文 → 跑 pipeline → 汇总/写 JSON。

    pipeline(ctx, args) 按顺序编排各套件；内部异常记 internal FAIL，
    finally 兜底清场，最后无论如何都写机器可读结果。
    """
    args = parse_args(argv)
    ctx = Context(
        skip_graceful_exit=args.skip_graceful_exit,
        enable_desktop_events=args.enable_desktop_events,
        enable_result_window=args.enable_result_window,
        suites=args.suites,
    )
    if args.json_path:
        ctx.json_path = Path(args.json_path)

    try:
        print_header(ctx)
        try:
            pipeline(ctx, args)
        except Exception:  # noqa: BLE001 —— 脚本自身 bug 也要给可读结果，不能裸崩
            detail = traceback.format_exc(limit=4)
            record(ctx, "internal", "验收脚本内部异常", "FAIL", _short(detail, 600), suite="all")
    finally:
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
    if skipped:
        print(f"验收通过（{skipped} 项 SKIP，见上；SKIP 不算通过，逐项看 detail）。")
    else:
        print("验收全部通过。")
    return 0
