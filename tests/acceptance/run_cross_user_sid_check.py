"""B2 跨用户 SID 拒绝路径验收（独立脚本，需要 fl_test 标准账户 + 密码）。

背景
----
agent 的命名管道默认 ``validate_sid=True``：连接后先取调用方 SID，与 agent 自己
的 SID 比较，不一致回 ``unauthorized`` 错误帧并断开。本脚本验证这条拒绝路径：

1. 用隔离管道/互斥体/临时 FIRELOOP_HOME 起一个测试 agent（当前用户）；
2. 把 token 和启动脚本放到 ``C:\\Users\\Public\\FireLoopCrossUser-<pid>-<rand>``
   （标准用户进程也能读；结果文件由标准用户写回该目录）；
3. 用 Windows 计划任务（``schtasks /RU fl_test``）以标准用户身份跑
   ``python -m fireloop.cli ping --pipe-name <隔离管道>``；
4. 预期：CLI 报 unauthorized（退出码 3），agent 不崩，当前用户 ping 仍正常；
5. 全部清场：删计划任务、删共享目录、停测试 agent。

**密码处理**：优先读环境变量 ``FL_TEST_PASSWORD``；没有则 getpass 交互输入。
密码不写任何文件、不打印；但 ``schtasks /Create /RP`` 会让它短暂出现在本机进程
命令行里（个人自用机器，可接受；介意的话跑完重启一次任务计划服务即可）。

用法::

    cd D:\\ds植入式
    $env:PYTHONPATH = "$PWD\\src"
    python tests\\acceptance\\run_cross_user_sid_check.py
    # 或指定用户名/环境变量
    python tests\\acceptance\\run_cross_user_sid_check.py --user fl_test --password-env FL_TEST_PASSWORD

先自检管线（当前用户跑同一个脚本，应得到 pong，不需要 fl_test 账号）::

    python tests\\acceptance\\run_cross_user_sid_check.py --selftest

退出码：0 = 拒绝路径符合预期；1 = 安全问题/脚本失败；2 = 环境不具备
（账号不存在、计划任务建不了等），需要 Lead 处理后再跑。
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
from harness import ROOT, _short  # noqa: E402

PYTHON = sys.executable
PUBLIC_DIR = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))


# ---------------------------------------------------------------------------
# 共享目录里的启动脚本（PowerShell，UTF-8 BOM，能正确处理中文项目路径）
# ---------------------------------------------------------------------------

RUN_PS1_TEMPLATE = """$ErrorActionPreference = 'Continue'
$env:FIRELOOP_HOME = '{home}'
$env:PYTHONPATH = '{pythonpath}'
$out = & '{python}' -m fireloop.cli ping --pipe-name '{pipe}' 2>&1 | Out-String
Set-Content -Path '{out_file}' -Value $out -Encoding UTF8
Set-Content -Path '{rc_file}' -Value $LASTEXITCODE -Encoding Ascii
"""


def _write_ps1(path: Path, *, home: str, pythonpath: str, python: str, pipe: str, out_file: Path, rc_file: Path) -> None:
    text = RUN_PS1_TEMPLATE.format(
        home=home,
        pythonpath=pythonpath,
        python=python,
        pipe=pipe,
        out_file=out_file,
        rc_file=rc_file,
    )
    path.write_text(text, encoding="utf-8-sig")


def _run_script_locally(script: Path, timeout: float) -> subprocess.CompletedProcess:
    """--selftest 用：当前用户直接跑同一个启动脚本，验证管线通不通。"""
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _run_as_scheduled_task(
    *, task_name: str, script: Path, user: str, password: str, rc_file: Path, timeout: float
) -> subprocess.CompletedProcess:
    """建一个一次性计划任务，以 user 身份跑 script，等结果文件出现。"""
    create = subprocess.run(
        [
            "schtasks.exe",
            "/Create",
            "/TN",
            task_name,
            "/TR",
            f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{script}"',
            "/SC",
            "ONCE",
            "/ST",
            "00:00",
            "/RU",
            user,
            "/RP",
            password,
            "/RL",
            "LIMITED",
            "/F",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if create.returncode != 0:
        return create
    try:
        run = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if run.returncode != 0:
            return run
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if rc_file.is_file():
                # 等文件写完整（计划任务进程退出前可能还在 flush）
                first = rc_file.read_text(encoding="ascii", errors="replace").strip()
                time.sleep(0.2)
                second = rc_file.read_text(encoding="ascii", errors="replace").strip()
                if first and first == second:
                    break
            time.sleep(0.2)
        return run
    finally:
        subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    for encoding in ("utf-8-sig", "utf-16", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeError, OSError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _cleanup_task(task_name: str) -> None:
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _check_agent_alive_and_current_user_ok(ctx) -> tuple[bool, str]:
    alive = ctx.agent is not None and ctx.agent.poll() is None
    proc, _ = ctx.run_cli(["ping"])
    current_ok = proc.returncode == 0 and proc.stdout.strip() == "pong"
    return alive and current_ok, f"agent_alive={alive} current_user_ping={'pong' if current_ok else proc.returncode}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_cross_user_sid_check.py",
        description="B2 跨用户 SID 拒绝路径验收（隔离环境，默认用 fl_test 标准账户）",
    )
    parser.add_argument("--user", default="fl_test", help="标准用户名，默认 fl_test")
    parser.add_argument(
        "--password-env",
        default="FL_TEST_PASSWORD",
        help="从这个环境变量读密码；没有则交互输入（密码不落盘）",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="只自检管线：以当前用户跑同一个启动脚本（预期 pong，正常退出码 0）",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="把结果写成 JSON")
    parser.add_argument("--timeout", type=float, default=40.0, help="等计划任务出结果的最长秒数")
    args = parser.parse_args(argv)

    ctx = harness.Context(suites=())
    task_name = f"FireLoopCrossUser-{ctx.tag}"
    shared_dir = PUBLIC_DIR / f"FireLoopCrossUser-{ctx.tag}"
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    result: dict = {
        "schema": "fireloop.cross_user_sid.v1",
        "time": started_at,
        "mode": "selftest" if args.selftest else "cross-user",
        "user": None if args.selftest else args.user,
        "verdict": "unknown",
        "detail": "",
    }
    exit_code = 2

    try:
        # 1) 隔离 agent（当前用户），SID 校验默认开启
        proc, pump, line = ctx.start_agent(ctx.pipe_name, ctx.mutex_name)
        ctx.agent, ctx.pump = proc, pump
        ctx.token = ctx.token_path.read_text(encoding="utf-8").strip()
        print(f"[准备] 测试 agent 启动：{line}")
        print(f"[准备] 隔离管道：{ctx.pipe_name}")
        print(f"[准备] 临时 FIRELOOP_HOME：{ctx.home}")

        # 2) 共享目录（标准用户进程可读 token / 写结果）
        shared_dir.mkdir(parents=True, exist_ok=False)
        token_file = shared_dir / "agent.token"
        token_file.write_text(ctx.token + "\n", encoding="utf-8")
        out_file = shared_dir / "out.txt"
        rc_file = shared_dir / "rc.txt"
        script = shared_dir / "run.ps1"
        _write_ps1(
            script,
            home=str(shared_dir),
            pythonpath=str(ROOT / "src"),
            python=PYTHON,
            pipe=ctx.pipe_name,
            out_file=out_file,
            rc_file=rc_file,
        )
        print(f"[准备] 跨用户启动脚本：{script}")

        if args.selftest:
            run_result = _run_script_locally(script, args.timeout)
            print(f"[自检] 当前用户执行启动脚本 exit={run_result.returncode}")
        else:
            # 账号存在性检查
            user_check = subprocess.run(
                ["net.exe", "user", args.user],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if user_check.returncode != 0:
                result["verdict"] = "environment_not_ready"
                result["detail"] = (
                    f"标准账户 {args.user} 不存在（task-9 由 Lead 创建）；"
                    "建好后重跑本脚本"
                )
                print(f"[环境] {result['detail']}")
                return 2

            password = os.environ.get(args.password_env, "")
            if not password:
                password = getpass.getpass(f"请输入 {args.user} 的密码（不落盘、不打印）：")
            if not password:
                result["verdict"] = "environment_not_ready"
                result["detail"] = "没有提供密码"
                print(f"[环境] {result['detail']}")
                return 2
            print(f"[跨用户] 建计划任务以 {args.user} 身份跑 CLI（任务名 {task_name}）")
            try:
                _run_as_scheduled_task(
                    task_name=task_name,
                    script=script,
                    user=args.user,
                    password=password,
                    rc_file=rc_file,
                    timeout=args.timeout,
                )
            finally:
                password = ""
            _cleanup_task(task_name)

        # 3) 收结果
        out_text = _read_text(out_file)
        rc_text = _read_text(rc_file).strip()
        result["cli_exit_code"] = int(rc_text) if rc_text.lstrip("-").isdigit() else None
        result["cli_output"] = _short(out_text, 600)
        print(f"[结果] CLI exit={result['cli_exit_code']}")
        print(f"[结果] CLI 输出：{_short(out_text, 300)!r}")

        # 4) agent 不能崩、当前用户不能受影响
        still_ok, detail = _check_agent_alive_and_current_user_ok(ctx)
        result["agent_still_ok"] = detail
        print(f"[结果] {detail}")
        if not still_ok:
            result["verdict"] = "agent_broken"
            result["detail"] = f"跨用户尝试后 agent 或当前用户连接异常：{detail}"
            exit_code = 1
            return exit_code

        # 5) 服务端日志：应出现 unauthorized
        _, records, _ = harness.load_logs(ctx)
        pipe_records = harness.events_by_pipe(records, ctx.pipe_name)
        unauthorized = [r for r in pipe_records if r.get("event") == "unauthorized"]
        hello_ok = [r for r in pipe_records if r.get("event") == "hello_ok"]
        result["server_unauthorized_events"] = len(unauthorized)
        print(f"[结果] agent 日志 unauthorized 事件数 = {len(unauthorized)}")

        cli_exit = result["cli_exit_code"]
        output = out_text or ""
        lowered = output.lower()

        if args.selftest:
            if cli_exit == 0 and "pong" in output:
                result["verdict"] = "selftest_ok"
                result["detail"] = "当前用户同一脚本跑通（pong），跨用户管线可用"
                exit_code = 0
            else:
                result["verdict"] = "selftest_failed"
                result["detail"] = f"自检没拿到 pong：exit={cli_exit} out={_short(output, 200)!r}"
                exit_code = 1
            print(f"[自检] {result['verdict']}：{result['detail']}")
            return exit_code

        # 验收口径（Lead 确认）：跨用户"连不上/被拒"即可，不要求一定拿到 unauthorized 帧。
        # 两层的区别要写清楚：Windows 命名管道 DACL 先拒绝，还是协议层 SID 校验拒绝。
        access_denied_markers = (
            "windows 错误 5",
            "拒绝访问",
            "access is denied",
            "access denied",
            "错误 5",
        )
        reached_protocol = bool(unauthorized)

        if cli_exit == 0:
            result["verdict"] = "security_fail_cross_user_allowed"
            result["layer"] = "none"
            result["detail"] = "跨用户连接居然成功（pong）——SID 校验失效，安全问题"
            exit_code = 1
        elif reached_protocol:
            # 服务端确实回了 unauthorized（无论 CLI 把它映射成哪个退出码）
            result["verdict"] = "sid_denied"
            result["layer"] = "protocol"
            result["detail"] = (
                f"协议层拒绝：agent 日志有 unauthorized，CLI exit={cli_exit}；"
                "agent 不崩、当前用户正常"
            )
            exit_code = 0
        elif cli_exit == 1 and any(marker in lowered for marker in access_denied_markers):
            # 标准用户连 CreateFile 就被 Windows DACL 拒绝，请求根本没到 agent。
            result["verdict"] = "denied_by_windows_dacl"
            result["layer"] = "windows_dacl"
            result["detail"] = (
                "Windows 层拒绝：标准用户在 CreateFile 连命名管道时被系统 DACL 拒绝"
                "（agent 侧没收到请求），agent 不崩、当前用户正常"
            )
            exit_code = 0
        elif cli_exit == 1:
            result["verdict"] = "needs_review"
            result["layer"] = "unknown"
            result["detail"] = (
                "CLI 退出码 1，但不是明确的 ACCESS_DENIED，也不是协议层 unauthorized；"
                f"输出={_short(output, 200)!r}。需要人工判断（可能是 token/管道名/命令行问题）"
            )
            exit_code = 2
        else:
            result["verdict"] = "unexpected"
            result["layer"] = "unknown"
            result["detail"] = f"CLI exit={cli_exit} 输出={_short(output, 200)!r}"
            exit_code = 1

        print(f"[结论] {result['verdict']}（layer={result.get('layer')}）：{result['detail']}")
        return exit_code
    finally:
        _cleanup_task(task_name)
        if shared_dir.exists():
            for _ in range(5):
                try:
                    shutil.rmtree(shared_dir)
                    break
                except OSError:
                    time.sleep(0.2)
        ctx.cleanup()
        result["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if "verdict" not in result:
            result["verdict"] = "error"
        if args.json_path:
            path = Path(args.json_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[结果] JSON：{path}")
        print(f"[清场] 计划任务已删、共享目录已删={not shared_dir.exists()}、测试 agent 已停")


if __name__ == "__main__":
    sys.exit(main())
