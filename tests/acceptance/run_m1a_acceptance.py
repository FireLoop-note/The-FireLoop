"""Fire Loop M1a 集成验收：真起 agent 进程 + 真跑 CLI，逐条核对 docs/fire-loop-方案.md 第 5 节验收清单。

由 Lead 在 T1/T2 交付后执行，不是子模块单测的替代。
用临时 FIRELOOP_HOME，不污染真实 %LOCALAPPDATA%；默认管道名 + 真实单实例互斥体（机器上不能有其他 agent 在跑）。

运行：
    cd D:\\ds植入式
    $env:PYTHONPATH = "$PWD\\src"
    python tests\\acceptance\\run_m1a_acceptance.py
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from fireloop import protocol  # noqa: E402

PYTHON = sys.executable
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def cli_env(home: str) -> dict:
    env = os.environ.copy()
    env["FIRELOOP_HOME"] = home
    env["PYTHONPATH"] = str(SRC) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return env


def run_cli(home: str, args: list[str], timeout: float = 20.0) -> tuple[subprocess.CompletedProcess, float]:
    started = time.monotonic()
    proc = subprocess.run(
        [PYTHON, "-m", "fireloop.cli", *args],
        cwd=str(ROOT),
        env=cli_env(home),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc, time.monotonic() - started


def wait_agent_ready(proc: subprocess.Popen, timeout: float = 10.0) -> str:
    """等 agent 打印启动行；返回已读到的输出（启动行会包含管道名）。"""
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.02)
            continue
        lines.append(line.strip())
        if "已启动" in line:
            return line.strip()
    raise AssertionError(f"agent 未在规定时间内启动，输出：{lines!r}，退出码：{proc.poll()}")


def main() -> int:
    home_dir = tempfile.TemporaryDirectory(prefix="fireloop-accept-")
    home = home_dir.name
    agent: subprocess.Popen | None = None
    original_token = None
    token_file = Path(home) / "agent.token"
    try:
        print(f"FIRELOOP_HOME = {home}")
        print(f"管道 = {protocol.PIPE_NAME}")
        print("=" * 72)

        # 0. 环境检查：机器上不能有别的 agent（默认管道 + 全局互斥体）
        proc, elapsed = run_cli(home, ["ping"])
        record(
            "0. agent 未运行时 CLI 明确报没在跑且快速返回",
            proc.returncode == protocol.CLI_EXIT_OFFLINE
            and "没在运行" in proc.stderr
            and "Traceback" not in proc.stderr
            and elapsed < protocol.CONNECT_TIMEOUT_MS / 1000.0 + 2.0,
            f"exit={proc.returncode} 耗时={elapsed:.2f}s stderr={proc.stderr.strip()!r}",
        )

        # 1. 启动 agent
        agent = subprocess.Popen(
            [PYTHON, "-m", "fireloop.agent"],
            cwd=str(ROOT),
            env=cli_env(home),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        startup_line = wait_agent_ready(agent)
        record("1. agent 启动并打印启动行", "已启动" in startup_line, startup_line)
        record(
            "1b. agent 启动后自动生成 token（64 位 hex）",
            token_file.exists() and len(token_file.read_text(encoding="utf-8").strip()) == 64,
            f"token 文件存在={token_file.exists()}",
        )
        original_token = token_file.read_text(encoding="utf-8").strip()

        # 2. ping → pong
        proc, elapsed = run_cli(home, ["ping"])
        record(
            "2. cli ping 返回 pong，退出码 0",
            proc.returncode == 0 and proc.stdout.strip() == "pong",
            f"exit={proc.returncode} stdout={proc.stdout.strip()!r} 耗时={elapsed:.2f}s",
        )

        # 3. task → 回显
        proc, elapsed = run_cli(home, ["task", "M1a 验收任务"])
        record(
            "3. cli task 把任务送到 agent 并回显，退出码 0",
            proc.returncode == 0 and proc.stdout.strip() == "收到任务",
            f"exit={proc.returncode} stdout={proc.stdout.strip()!r} 耗时={elapsed:.2f}s",
        )

        # 4. 连续 5 次 ping + 5 次 task（回归"管道名消失空窗"问题）
        seq_ok = True
        seq_detail = []
        for i in range(5):
            p_ping, _ = run_cli(home, ["ping"])
            p_task, _ = run_cli(home, ["task", f"连续任务 {i}"])
            ok = (
                p_ping.returncode == 0
                and p_ping.stdout.strip() == "pong"
                and p_task.returncode == 0
                and p_task.stdout.strip() == "收到任务"
            )
            seq_ok = seq_ok and ok
            seq_detail.append(f"#{i}:{'ok' if ok else 'FAIL'}")
        record("4. ping/task 连续跑 5 轮不出现偶发 agent_offline", seq_ok, " ".join(seq_detail))

        # 5. 第二个 agent 实例被单实例挡住
        second = subprocess.run(
            [PYTHON, "-m", "fireloop.agent"],
            cwd=str(ROOT),
            env=cli_env(home),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        record(
            "5. 第二个 agent 实例被单实例锁挡住，退出码 1",
            second.returncode == 1 and "已经在运行" in (second.stdout + second.stderr),
            f"exit={second.returncode} out={(second.stdout + second.stderr).strip()!r}",
        )

        # 6. token 错误 → 协议错误退出码 3，agent 不崩
        token_file.write_text("0" * 64 + "\n", encoding="utf-8")
        proc, elapsed = run_cli(home, ["ping"])
        record(
            "6. token 不对时 CLI 明确报错，退出码 3",
            proc.returncode == protocol.CLI_EXIT_PROTOCOL
            and protocol.CODE_BAD_TOKEN in proc.stderr
            and "Traceback" not in proc.stderr,
            f"exit={proc.returncode} stderr={proc.stderr.strip()!r}",
        )
        token_file.write_text(original_token + "\n", encoding="utf-8")
        proc, _ = run_cli(home, ["ping"])
        record("6b. 恢复 token 后 CLI 恢复可用", proc.returncode == 0 and proc.stdout.strip() == "pong", f"exit={proc.returncode}")

        # 7. 非法 JSON + 超长行直灌管道，agent 不崩
        def blast_garbage(payload: bytes) -> None:
            handle = open(protocol.PIPE_NAME, "r+b", buffering=0)
            try:
                handle.write(payload)
            finally:
                handle.close()

        blast_garbage(b"this is not json\n")
        time.sleep(0.2)
        blast_garbage(b'{"v":1,"type":"task","text":"' + b"x" * (protocol.MAX_LINE_BYTES * 2) + b'"}\n')
        time.sleep(0.3)
        proc, elapsed = run_cli(home, ["ping"])
        record(
            "7. 非法 JSON / 超长行之后 agent 仍存活且能服务",
            agent.poll() is None and proc.returncode == 0 and proc.stdout.strip() == "pong",
            f"agent_alive={agent.poll() is None} 后续 ping exit={proc.returncode}",
        )

        # 8. Ctrl+Break 优雅退出（真实用户行为），再验 CLI 报没在跑
        graceful = False
        exit_code = None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if kernel32.GenerateConsoleCtrlEvent(1, agent.pid):  # 1 = CTRL_BREAK_EVENT
                try:
                    exit_code = agent.wait(timeout=10)
                    graceful = True
                except subprocess.TimeoutExpired:
                    pass
        except Exception as exc:
            print(f"  优雅退出调用异常：{exc!r}")

        if not graceful:
            agent.terminate()
            try:
                exit_code = agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
                exit_code = agent.wait(timeout=5)
        rest = agent.stdout.read() if agent.stdout else ""
        agent = None
        record(
            "8a. agent 收到 Ctrl+Break 优雅退出：退出码 0，打印已退出",
            graceful and exit_code == 0 and "已退出" in rest,
            f"graceful={graceful} exit={exit_code} 尾部={rest.strip()[-80:]!r}",
        )

        proc, elapsed = run_cli(home, ["ping"])
        record(
            "8b. agent 退出后 CLI 明确报没在跑，退出码 1，不挂死",
            proc.returncode == protocol.CLI_EXIT_OFFLINE
            and "没在运行" in proc.stderr
            and elapsed < 5.0,
            f"exit={proc.returncode} 耗时={elapsed:.2f}s",
        )

        # 9. 日志检查：JSONL 存在、有启动/停止事件、不含任务原文、不含 token
        log_dir = Path(home) / "logs"
        log_files = sorted(log_dir.glob("agent-*.jsonl")) if log_dir.is_dir() else []
        all_lines = "\n".join(f.read_text(encoding="utf-8") for f in log_files)
        events = [
            json.loads(line)["event"]
            for line in all_lines.splitlines()
            if line.strip()
        ]
        record(
            "9. 日志是 JSONL，含 agent_start/agent_stop",
            bool(log_files) and "agent_start" in events and "agent_stop" in events,
            f"事件={sorted(set(events))}",
        )
        record(
            "9b. 日志不含任务原文与 token",
            "M1a 验收任务" not in all_lines and (original_token or "\x00") not in all_lines,
            "已按指纹记录 text_len/text_sha8",
        )

        # 10. 残留与红线检查
        leftovers = [p for p in Path(tempfile.gettempdir()).glob("fireloop-*") if p.name.startswith("fireloop-test-")]
        record("10. 无残留测试临时目录", not leftovers, f"残留={leftovers}")

        py_files = list((SRC / "fireloop").glob("*.py"))
        forbidden = ("requests", "urllib.request", "httpx", "openai", "winreg")
        hits = []
        for f in py_files:
            text = f.read_text(encoding="utf-8")
            for word in forbidden:
                if word in text:
                    hits.append(f"{f.name}:{word}")
        record("11. 无模型/网络/注册表调用（0 模型调用、不写注册表）", not hits, f"命中={hits}")

    finally:
        if agent is not None and agent.poll() is None:
            agent.kill()
            agent.wait(timeout=5)
        home_dir.cleanup()

    print("=" * 72)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"验收结果：{passed}/{len(RESULTS)} 通过")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("M1a 验收全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
