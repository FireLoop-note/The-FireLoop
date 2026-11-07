"""M1a 基线验收项（20 项）：隔离管道/互斥体 + 真进程 CLI + 日志 + 清场。

由 run_m1a_acceptance.py 按阶段调用：

- ``run_agent_phase``    agent 尚未启动 → 启动 → ping/task/连续 5 轮/单实例/token/垃圾输入
- ``run_shutdown_phase`` Ctrl+Break 优雅退出 + 退出后离线 + 日志检查
- ``run_final_phase``    清场 + 残留检查 + 红线/隔离自检（必须是最后一步）

后续里程碑（B1/B2）的项插在 agent phase 和 shutdown phase 之间，复用同一个测试 agent，
所以这里按阶段拆开，而不是一个 run() 一把梭。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from fireloop import client as fireloop_client
from fireloop import protocol

from harness import (
    DEFAULT_MUTEX_NAME,
    PIPE_NAMESPACE,
    PYTHON,
    ROOT,
    _Fail,
    _Skip,
    blast_raw,
    check,
    ensure_agent,
    first_line,
    load_logs,
    mutex_exists,
    pipe_exists,
    require,
    temp_accept_entries,
    write_token,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def run_agent_phase(ctx) -> None:
    """agent 启动前后的验收项（0 ~ 7b）。"""
    # 0. agent 未运行时 CLI 快速报错 --------------------------------------
    def t0_offline() -> str:
        proc, elapsed = ctx.run_cli(["ping"])
        require(
            proc.returncode == protocol.CLI_EXIT_OFFLINE,
            f"exit={proc.returncode}（期望 {protocol.CLI_EXIT_OFFLINE}），"
            f"stderr={first_line(proc.stderr)!r}",
        )
        require("没在运行" in proc.stderr, f"stderr 未明确报没在运行：{first_line(proc.stderr)!r}")
        require("Traceback" not in proc.stderr, f"stderr 出现裸 traceback：{first_line(proc.stderr)!r}")
        require(elapsed < 2.0, f"耗时 {elapsed:.2f}s，超过 2s 要求")
        return f"exit=1 耗时={elapsed:.2f}s stderr={first_line(proc.stderr)!r}"

    check(ctx, "0", "agent 未运行时 CLI 快速报错、退出码 1（<2s，不挂死）", t0_offline)

    # 1. 启动测试 agent（隔离管道 + 隔离互斥体） --------------------------
    def t1_startup() -> str:
        proc, pump, line = ctx.start_agent(ctx.pipe_name, ctx.mutex_name)
        ctx.agent, ctx.pump, ctx.startup_line = proc, pump, line
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
        require(proc.returncode == 0, f"exit={proc.returncode}（期望 0），stderr={first_line(proc.stderr)!r}")
        require(proc.stdout.strip() == "pong", f"stdout={proc.stdout.strip()!r}（期望 pong）")
        return f"exit=0 stdout=pong 耗时={elapsed:.2f}s"

    check(ctx, "2", "cli ping → pong，退出码 0", t2_ping)

    # 3. task → 回显 ------------------------------------------------------
    def t3_task() -> str:
        ensure_agent(ctx)
        proc, elapsed = ctx.run_cli(["task", "M1a 验收任务"])
        require(proc.returncode == 0, f"exit={proc.returncode}（期望 0），stderr={first_line(proc.stderr)!r}")
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
        require(proc.returncode == 1, f"exit={proc.returncode}（期望 1），输出={first_line(combined)!r}")
        require("已经在运行" in combined, f"未打印'已经在运行'：{first_line(combined)!r}")
        require("Traceback" not in combined, "第二实例打印了 traceback")
        require(pipe_exists(ctx.second_pipe) is False, "第二实例竟然创建了自己的管道")
        return f"exit=1 输出={first_line(combined)!r}"

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
            f"stderr={first_line(proc.stderr)!r}",
        )
        require(protocol.CODE_BAD_TOKEN in proc.stderr, f"stderr 未出现 bad_token：{first_line(proc.stderr)!r}")
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


def run_shutdown_phase(ctx) -> None:
    """Ctrl+Break 优雅退出 + 退出后离线 + 日志检查（8a/8b/9/9b/9c）。"""
    # 8a. Ctrl+Break 优雅退出 ---------------------------------------------
    def t8a_graceful() -> str:
        ensure_agent(ctx)
        if ctx.skip_graceful_exit:
            ctx.stop_agent_hard()
            raise _Skip("按 --skip-graceful-exit 跳过；测试 agent 已强制停止，agent_stop 日志项一并跳过")
        agent, pump = ctx.agent, ctx.pump
        delivered, exit_code, lines = ctx.stop_agent_graceful(agent, pump, timeout=10)
        if not delivered:
            ctx.stop_agent_hard()
            raise _Skip(
                "当前环境无法投递 Ctrl+Break（GenerateConsoleCtrlEvent 失败）；"
                "已强制停止测试 agent，可用 --skip-graceful-exit 明确跳过"
            )
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
        require("没在运行" in proc.stderr, f"stderr 未明确报没在运行：{first_line(proc.stderr)!r}")
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
        return (
            f"任务只记 {task_events[0].get('text_len')} 字节 / "
            f"sha8={task_events[0].get('text_sha8')}，无原文与 token"
        )

    check(ctx, "9c", "日志不含任务原文与 token（只记长度/sha256 前 8 位）", t9c_log_no_sensitive)


def run_final_phase(ctx) -> None:
    """清场 + 残留检查 + 红线/隔离自检（必须是整套验收的最后一步）。"""
    # 清场：杀残留、删临时目录（后续检查依赖清场结果）
    problems = ctx.cleanup()
    if problems:
        print(f"  清场时发现问题：{problems}")

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
        require(
            pipe_exists(ctx.events_pipe) is False,
            f"事件阶段管道仍然存在：{ctx.events_pipe}",
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

    # 12. 无残留临时目录 --------------------------------------------------
    def t12_no_leftover_temp() -> str:
        require(not Path(ctx.home).exists(), f"临时数据目录未删除：{ctx.home}")
        new_entries = temp_accept_entries() - ctx.temp_before
        require(not new_entries, f"%TEMP% 下出现新的 fireloop-accept-* 残留：{sorted(new_entries)}")
        require(not ctx.cleanup_problems, f"清场问题：{ctx.cleanup_problems}")
        return "临时目录已删除，%TEMP% 无新增残留"

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
