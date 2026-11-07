"""Fire Loop B1 验收项（安装/卸载脚本 + CLI file/uri）。

阶段约定（由 run_m1a_acceptance.py 调用）：

- ``run_agent_phase``：M1a 测试 agent 还在跑时执行 CLI file / uri 行为验收；
- ``run_offline_phase``：M1a agent 已退出后执行安装脚本 dry-run / 测试前缀 -Apply /
  uninstall -Apply 验收（不依赖 agent）。

红线：只调用 install/install.ps1 与 install/uninstall.ps1，生产前缀的 -Apply 绝对不跑；
测试前缀固定为 ``fireloop-test-<uuid8>``，结束前必须删干净。
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import winreg
from pathlib import Path

from fireloop import protocol

from harness import (
    ROOT,
    _Skip,
    check,
    ensure_agent,
    first_line,
    load_logs,
    record,
    require,
)

# 安装脚本/文档由 task-5 交付；就绪前离线阶段标 SKIP 并写清原因，不假装通过。
INSTALL_SCRIPT = ROOT / "install" / "install.ps1"
UNINSTALL_SCRIPT = ROOT / "install" / "uninstall.ps1"

HKCU = winreg.HKEY_CURRENT_USER
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_PYTHONW = Path(r"D:\Python\pythonw.exe")

# 生产键（验收全程只读探测，绝不允许被创建/修改）
PRODUCTION_TARGETS: dict[str, tuple[str, str | None]] = {
    "scheme": (r"Software\Classes\fireloop", None),
    "file_verb": (r"Software\Classes\*\shell\FireLoop", None),
    "dir_verb": (r"Software\Classes\Directory\shell\FireLoop", None),
    "run_value": (RUN_KEY, "FireLoop"),
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def task_requests(ctx) -> list[dict]:
    _, records, _ = load_logs(ctx)
    return [
        record
        for record in records
        if record.get("event") == "request" and record.get("type") == "task"
    ]


def find_task_text(ctx, expected: str) -> bool:
    """用日志里的 text_len + text_sha8 指纹核对 CLI 实际发出去的 task.text。

    日志不记正文（M1a 安全红线），所以验收只能按指纹比对：指纹对得上 = 文本正确。
    """
    wanted = protocol.text_fingerprint(expected)
    for record in task_requests(ctx):
        if record.get("text_len") == wanted["text_len"] and record.get("text_sha8") == wanted["text_sha8"]:
            return True
    return False


def _cli_ok(proc) -> None:
    require(proc.returncode == 0, f"exit={proc.returncode}（期望 0），stderr={first_line(proc.stderr)!r}")
    require(proc.stdout.strip() == "收到任务", f"stdout={proc.stdout.strip()!r}（期望 收到任务）")


# ---------------------------------------------------------------------------
# agent 在跑时：CLI file / uri
# ---------------------------------------------------------------------------


def run_agent_phase(ctx) -> None:
    def t_file_absolute() -> str:
        ensure_agent(ctx)
        target = Path(ctx.home) / "B1 验收 报表.txt"
        target.write_text("这段文件内容绝不能出现在日志里", encoding="utf-8")
        proc, elapsed = ctx.run_cli(["file", str(target)])
        _cli_ok(proc)
        expected = "file:" + os.path.abspath(str(target))
        require(
            find_task_text(ctx, expected),
            f"日志里没有 task.text 的预期指纹（text={expected!r}）",
        )
        return f"exit=0 耗时={elapsed:.2f}s，task.text 指纹匹配"

    check(ctx, "B1-1", "cli file <绝对路径>（含中文/空格）回显正常，task.text=file:<绝对路径>", t_file_absolute, suite="b1")

    def t_file_relative() -> str:
        ensure_agent(ctx)
        relative = os.path.join("docs", "验收.md")
        proc, elapsed = ctx.run_cli(["file", relative])
        _cli_ok(proc)
        expected = "file:" + os.path.normpath(os.path.join(str(ROOT), relative))
        require(
            find_task_text(ctx, expected),
            f"相对路径没有转成绝对路径（期望 text={expected!r}）",
        )
        return f"exit=0 耗时={elapsed:.2f}s，相对路径已转绝对"

    check(ctx, "B1-2", "cli file <相对路径> 转绝对路径后发送", t_file_relative, suite="b1")

    def t_uri_task() -> str:
        ensure_agent(ctx)
        raw_text = "B1 URI 任务 中文 + 符号"
        uri = "fireloop://task?text=" + urllib.parse.quote(raw_text, safe="")
        proc, elapsed = ctx.run_cli(["uri", uri])
        _cli_ok(proc)
        require(find_task_text(ctx, raw_text), f"uri task 解码结果不对（期望 text={raw_text!r}）")
        return f"exit=0 耗时={elapsed:.2f}s，URL 解码正确"

    check(ctx, "B1-3", "cli uri fireloop://task?text=... URL 解码后发送，回显正常", t_uri_task, suite="b1")

    def t_uri_file() -> str:
        ensure_agent(ctx)
        path = str(Path(ctx.home) / "B1 URI 文件 示例.txt")
        uri = "fireloop://file?path=" + urllib.parse.quote(path, safe="")
        proc, elapsed = ctx.run_cli(["uri", uri])
        _cli_ok(proc)
        require(find_task_text(ctx, "file:" + path), f"uri file 解码结果不对（期望 text={'file:' + path!r}）")
        return f"exit=0 耗时={elapsed:.2f}s，task.text=file:<路径>"

    check(ctx, "B1-4", "cli uri fireloop://file?path=... 生成 file:<路径> 并回显", t_uri_file, suite="b1")

    def t_cli_usage_errors() -> str:
        ensure_agent(ctx)
        cases = [
            ("file 空路径", ["file", ""], "用法错误"),
            ("uri 非 fireloop scheme", ["uri", "https://example.com/x"], "用法错误"),
            ("uri task 缺 text", ["uri", "fireloop://task"], "用法错误"),
            ("uri 未知 host", ["uri", "fireloop://bogus?x=1"], "用法错误"),
        ]
        failures: list[str] = []
        for label, args, needle in cases:
            proc, _ = ctx.run_cli(args)
            combined = proc.stdout + proc.stderr
            if proc.returncode != protocol.CLI_EXIT_INTERNAL:
                failures.append(f"{label}: exit={proc.returncode}（期望 4）")
            if needle not in combined:
                failures.append(f"{label}: 输出缺 {needle!r}")
            if "Traceback" in combined:
                failures.append(f"{label}: 出现 traceback")
        require(not failures, "；".join(failures))
        return f"{len(cases)} 个非法参数全部退出码 4、无 traceback"

    check(ctx, "B1-5", "cli file/uri 非法参数退出码 4，不抛裸 traceback", t_cli_usage_errors, suite="b1")


# ---------------------------------------------------------------------------
# agent 已退出后：安装/卸载脚本（HKCU 注册表）
# ---------------------------------------------------------------------------

_KNOWN_ANCESTORS = (
    r"Software\Classes",
    r"Software\Classes\*",
    r"Software\Classes\*\shell",
    r"Software\Classes\Directory",
    r"Software\Classes\Directory\shell",
    RUN_KEY,
)


def _key_exists(path: str) -> bool:
    try:
        with winreg.OpenKey(HKCU, path):
            return True
    except FileNotFoundError:
        return False


def _read_value(path: str, name: str = "") -> tuple[bool, object]:
    try:
        with winreg.OpenKey(HKCU, path) as key:
            try:
                return True, winreg.QueryValueEx(key, name)[0]
            except FileNotFoundError:
                return False, None
    except FileNotFoundError:
        return False, None


def _production_snapshot() -> dict[str, bool]:
    """生产键/值是否存在（只读；验收前后必须一致）。"""
    snapshot: dict[str, bool] = {}
    for label, (path, value_name) in PRODUCTION_TARGETS.items():
        if value_name is None:
            snapshot[label] = _key_exists(path)
        else:
            snapshot[label] = _read_value(path, value_name)[0]
    return snapshot


def _real_backup_entries() -> set[str]:
    """真实 %LOCALAPPDATA%\\FireLoop\\backup 下已有的 reg-* 目录（只读快照）。"""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return set()
    root = Path(local) / "FireLoop" / "backup"
    if not root.is_dir():
        return set()
    return {path.name for path in root.glob("reg-*") if path.is_dir()}


def _handler_name(prefix: str) -> str:
    suffix = prefix[len("fireloop-"):] if prefix.startswith("fireloop-") else prefix
    return f"FireLoop-{suffix}"


def _test_targets(prefix: str) -> dict[str, str]:
    """测试前缀对应的 4 类注册项路径（与 install/_common.ps1 的命名规则一致）。"""
    handler = _handler_name(prefix)
    return {
        "scheme": rf"Software\Classes\{prefix}",
        "scheme_command": rf"Software\Classes\{prefix}\shell\open\command",
        "file_verb": rf"Software\Classes\*\shell\{handler}",
        "file_command": rf"Software\Classes\*\shell\{handler}\command",
        "dir_verb": rf"Software\Classes\Directory\shell\{handler}",
        "dir_command": rf"Software\Classes\Directory\shell\{handler}\command",
        "run_value_name": handler,
    }


def _decode_ps_output(data: bytes) -> str:
    """PowerShell 5.1 重定向输出在本机是 UTF-8；兜底 GBK。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _run_ps(ctx, script: Path, args: list[str] | tuple[str, ...] = (), timeout: float = 240.0):
    """以隔离 LOCALAPPDATA 跑 install/uninstall 脚本，返回 (退出码, 合并输出)。"""
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *args,
    ]
    env = ctx.child_env({"LOCALAPPDATA": str(Path(ctx.home) / "localappdata")})
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"{script.name} 超过 {timeout:.0f}s 未退出") from exc
    return proc.returncode, _decode_ps_output(proc.stdout + proc.stderr)


def _delete_key_tree(path: str) -> None:
    try:
        key = winreg.OpenKey(HKCU, path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
    except FileNotFoundError:
        return
    subkeys: list[str] = []
    with key:
        index = 0
        while True:
            try:
                subkeys.append(winreg.EnumKey(key, index))
            except OSError:
                break
            index += 1
    for subkey in subkeys:
        _delete_key_tree(path + "\\" + subkey)
    try:
        winreg.DeleteKey(HKCU, path)
    except FileNotFoundError:
        pass


def _emergency_cleanup(ctx, prefix: str) -> list[str]:
    """uninstall 失败/中途异常时的兜底清场：只删本套件唯一测试前缀的项。

    安全闸：prefix 必须以 ``fireloop-test-`` 开头，绝不碰生产键。
    """
    if not prefix.startswith("fireloop-test-"):
        return [f"拒绝兜底清场：prefix 不是测试前缀 {prefix!r}"]
    targets = _test_targets(prefix)
    # 1) 删除 3 个测试键树
    for path in (targets["scheme"], targets["file_verb"], targets["dir_verb"]):
        _delete_key_tree(path)
    # 2) 删除测试 Run 值
    try:
        with winreg.OpenKey(HKCU, RUN_KEY, 0, winreg.KEY_SET_VALUE) as run_key:
            try:
                winreg.DeleteValue(run_key, targets["run_value_name"])
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    # 3) 清掉安装时创建、现在已空的父键（只限已知祖先，且与 install-result 对照）
    backup_root = Path(ctx.home) / "localappdata" / "FireLoop" / "backup"
    absent_before: list[str] = []
    for result_path in sorted(backup_root.glob("reg-*/install-result.json")) if backup_root.is_dir() else []:
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("key_prefix") == prefix:
            absent_before = [str(item) for item in data.get("ancestors_absent_before") or []]
            break
    candidates = sorted(
        (item for item in absent_before if item in _KNOWN_ANCESTORS),
        key=len,
        reverse=True,
    )
    for relative in candidates:
        path = relative
        try:
            with winreg.OpenKey(HKCU, path) as key:
                has_subkeys = False
                try:
                    winreg.EnumKey(key, 0)
                    has_subkeys = True
                except OSError:
                    pass
                value_count = winreg.QueryInfoKey(key)[1]
        except FileNotFoundError:
            continue
        if not has_subkeys and value_count == 0:
            _delete_key_tree(path)
    # 4) 返回仍残留的项
    leftovers: list[str] = []
    for path in (targets["scheme"], targets["file_verb"], targets["dir_verb"]):
        if _key_exists(path):
            leftovers.append(path)
    if _read_value(RUN_KEY, targets["run_value_name"])[0]:
        leftovers.append(RUN_KEY + " :: " + targets["run_value_name"])
    return leftovers


def run_offline_phase(ctx) -> None:
    missing = [str(path.relative_to(ROOT)) for path in (INSTALL_SCRIPT, UNINSTALL_SCRIPT) if not path.is_file()]
    if missing:

        def pending() -> str:
            raise _Skip(f"B1 安装脚本验收待依赖就绪（缺少：{missing}）")

        check(ctx, "B1-6", "B1 安装/卸载脚本验收（依赖未就绪）", pending, suite="b1")
        return

    prefix = ctx.registry_prefix
    handler = _handler_name(prefix)
    backup_root = Path(ctx.home) / "localappdata" / "FireLoop" / "backup"
    prod_before = _production_snapshot()
    real_backup_before = _real_backup_entries()
    pythonw = DEFAULT_PYTHONW
    state = {"apply_done": False}

    def t_dry_run() -> str:
        require(pythonw.is_file(), f"默认 Python 解释器不存在：{pythonw}（安装脚本应报错退出，属环境问题）")
        rc, output = _run_ps(ctx, INSTALL_SCRIPT)
        require(rc == 0, f"exit={rc}（期望 0），输出={first_line(output)!r}")
        require("DRY-RUN" in output, "输出没有 DRY-RUN 标记")
        require("未写入任何注册表项" in output, "输出没有'未写入任何注册表项'声明")
        required = {
            "协议 key": r"HKCU\Software\Classes\fireloop",
            "文件右键": r"HKCU\Software\Classes\*\shell\FireLoop",
            "文件夹右键": r"HKCU\Software\Classes\Directory\shell\FireLoop",
            "自启 Run": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        }
        missing_tokens = [label for label, token in required.items() if token not in output]
        require(not missing_tokens, f"dry-run 输出缺少注册项：{missing_tokens}")
        require("4 类注册项" in output, "dry-run 输出没有'4 类注册项'计划段")
        require("FireLoop =" in output, "dry-run 输出没有列出 Run\\FireLoop 的值")
        launch_script = ROOT / "install" / "launch.py"
        require(launch_script.is_file(), f"launch.py 启动器不存在：{launch_script}")
        require(str(launch_script) in output, "dry-run 输出没有列出 launch.py 启动器路径")
        require("[预检]" in output and "启动器自检" in output, "dry-run 输出没有启动器自检段")
        require("[通过]" in output, "启动器自检没有通过（注册表命令会跑不起来）")
        # dry-run 前后生产键不变（本机应全部不存在）
        require(_production_snapshot() == prod_before, "dry-run 改变了生产注册表状态")
        # dry-run 不应该创建备份目录（我们已把 LOCALAPPDATA 指到临时目录）
        created_backups = sorted(backup_root.glob("reg-*")) if backup_root.is_dir() else []
        require(not created_backups, f"dry-run 竟然创建了备份目录：{[p.name for p in created_backups]}")
        return (
            f"exit=0，4 类注册项齐全，未写注册表、未建备份；"
            f"生产键状态={prod_before}"
        )

    check(
        ctx,
        "B1-6",
        "install.ps1 生产前缀 dry-run：列出 4 类注册项且不写注册表/不建备份",
        t_dry_run,
        suite="b1",
    )

    def t_apply() -> str:
        rc, output = _run_ps(ctx, INSTALL_SCRIPT, ["-Apply", "-KeyPrefix", prefix])
        require(rc == 0, f"exit={rc}（期望 0），输出={first_line(output)!r}")
        require("[验证通过]" in output, "安装脚本没有报验证通过")
        require("4 类注册项均已写入且读回一致" in output, "安装脚本验证段缺失")
        quoted_python = f'"{pythonw}"'
        launch_script = ROOT / "install" / "launch.py"
        require(launch_script.is_file(), f"launch.py 启动器不存在：{launch_script}")
        quoted_launch = f'"{launch_script}"'
        expected = [
            ("scheme 默认值", _test_targets(prefix)["scheme"], "", ""),
            ("scheme URL Protocol", _test_targets(prefix)["scheme"], "URL Protocol", ""),
            (
                "scheme command",
                _test_targets(prefix)["scheme_command"],
                "",
                quoted_python + " " + quoted_launch + ' cli uri "%1"',
            ),
            ("file verb 显示名", _test_targets(prefix)["file_verb"], "", "交给火环"),
            (
                "file command",
                _test_targets(prefix)["file_command"],
                "",
                quoted_python + " " + quoted_launch + ' cli file "%1"',
            ),
            ("dir verb 显示名", _test_targets(prefix)["dir_verb"], "", "交给火环"),
            (
                "dir command",
                _test_targets(prefix)["dir_command"],
                "",
                quoted_python + " " + quoted_launch + ' cli file "%1"',
            ),
        ]
        for label, path, value_name, want in expected:
            found, actual = _read_value(path, value_name)
            require(found, f"{label} 不存在：{path} :: {value_name or '(默认值)'}")
            require(
                str(actual) == want,
                f"{label} 值不对：{actual!r}，期望 {want!r}",
            )
        found, run_data = _read_value(RUN_KEY, _test_targets(prefix)["run_value_name"])
        require(found, f"Run 值不存在：{RUN_KEY} :: {_test_targets(prefix)['run_value_name']}")
        want_agent = quoted_python + " " + quoted_launch + " agent"
        require(str(run_data) == want_agent, f"Run 自启命令不对：{run_data!r}，期望 {want_agent!r}")

        # 备份必须落在隔离出来的 LOCALAPPDATA（不碰用户真实备份目录）
        backup_dirs = sorted(backup_root.glob("reg-*")) if backup_root.is_dir() else []
        require(bool(backup_dirs), f"隔离备份根下没有 reg-* 备份目录：{backup_root}")
        result_file = backup_dirs[-1] / "install-result.json"
        before_file = backup_dirs[-1] / "before.json"
        require(result_file.is_file(), f"安装结果清单不存在：{result_file}")
        require(before_file.is_file(), f"安装前快照不存在：{before_file}")
        result_data = json.loads(result_file.read_text(encoding="utf-8"))
        require(result_data.get("key_prefix") == prefix, f"备份清单 key_prefix 不对：{result_data.get('key_prefix')!r}")
        require(result_data.get("handler_name") == handler, f"备份清单 handler_name 不对：{result_data.get('handler_name')!r}")
        require(
            result_data.get("python_exe") == str(pythonw),
            f"备份清单 python_exe 不对：{result_data.get('python_exe')!r}",
        )
        require(_production_snapshot() == prod_before, "测试前缀 -Apply 动了生产键")
        require(
            _real_backup_entries() == real_backup_before,
            "测试前缀 -Apply 往真实 LOCALAPPDATA 备份目录写了东西",
        )
        state["apply_done"] = True
        return (
            f"exit=0，4 类注册项逐值核对通过（pythonw={pythonw.name}），"
            f"备份在临时 LOCALAPPDATA：{backup_dirs[-1].name}"
        )

    check(
        ctx,
        "B1-7",
        "install.ps1 -Apply -KeyPrefix <唯一测试前缀>：4 类注册项值正确且不碰生产键",
        t_apply,
        suite="b1",
    )

    def t_uninstall() -> str:
        targets = _test_targets(prefix)
        rc, output = _run_ps(ctx, UNINSTALL_SCRIPT, ["-Apply", "-KeyPrefix", prefix])
        require(rc == 0, f"exit={rc}（期望 0），输出={first_line(output)!r}")
        require("[完成]" in output, "卸载脚本没有报完成")
        still = [label for label, path in {
            "scheme": targets["scheme"],
            "file_verb": targets["file_verb"],
            "dir_verb": targets["dir_verb"],
        }.items() if _key_exists(path)]
        require(not still, f"卸载后注册表键仍存在：{still}")
        require(
            not _read_value(RUN_KEY, targets["run_value_name"])[0],
            "卸载后 Run 自启值仍存在",
        )
        # 幂等：再跑一次仍应成功、不动别的东西
        rc2, output2 = _run_ps(ctx, UNINSTALL_SCRIPT, ["-Apply", "-KeyPrefix", prefix])
        require(rc2 == 0, f"第二次 uninstall exit={rc2}（期望 0）")
        require(
            ("没有需要删除的内容" in output2) or ("跳过" in output2) or ("幂等" in output2),
            f"第二次 uninstall 没有体现幂等：{first_line(output2)!r}",
        )
        require(not _key_exists(targets["scheme"]), "第二次 uninstall 后 scheme 键又出现了")
        require(_production_snapshot() == prod_before, "uninstall 动了生产键")
        return "exit=0，4 类注册项全部删除并验证不存在；再跑一次仍 exit=0（幂等）"

    check(
        ctx,
        "B1-8",
        "uninstall.ps1 -Apply -KeyPrefix <唯一测试前缀>：4 类注册项删干净且幂等",
        t_uninstall,
        suite="b1",
    )

    def t_production_untouched() -> str:
        after = _production_snapshot()
        require(after == prod_before, f"全程生产注册表状态被改变：{prod_before} → {after}")
        require(
            _real_backup_entries() == real_backup_before,
            "全程往真实 LOCALAPPDATA 备份目录写了东西",
        )
        targets = _test_targets(prefix)
        leftovers = [
            path
            for path in (targets["scheme"], targets["file_verb"], targets["dir_verb"])
            if _key_exists(path)
        ]
        if _read_value(RUN_KEY, targets["run_value_name"])[0]:
            leftovers.append(RUN_KEY + " :: " + targets["run_value_name"])
        require(not leftovers, f"测试前缀注册表残留：{leftovers}")
        production_absent = all(not value for value in prod_before.values())
        state_text = "原本不存在，全程未被创建" if production_absent else "原本已存在（已安装），全程未被修改"
        return (
            f"生产键 {sorted(prod_before)}：{state_text}；"
            f"真实备份目录无新增，测试前缀 {prefix} 已彻底删除"
        )

    check(
        ctx,
        "B1-9",
        "全程生产注册表键未被创建/修改，真实 LOCALAPPDATA 备份目录无新增",
        t_production_untouched,
        suite="b1",
    )

    # 兜底清场：无论上面哪一项失败/异常，测试前缀的注册表项都不能留。
    try:
        targets = _test_targets(prefix)
        leftover = [
            path
            for path in (targets["scheme"], targets["file_verb"], targets["dir_verb"])
            if _key_exists(path)
        ]
        if _read_value(RUN_KEY, targets["run_value_name"])[0]:
            leftover.append(RUN_KEY + " :: " + targets["run_value_name"])
        if leftover:
            try:
                _run_ps(ctx, UNINSTALL_SCRIPT, ["-Apply", "-KeyPrefix", prefix], timeout=120)
            except AssertionError:
                pass
            remaining = _emergency_cleanup(ctx, prefix)
            if remaining:
                record(
                    ctx,
                    "B1-cleanup",
                    "B1 兜底清场失败：测试注册表项仍残留",
                    "FAIL",
                    f"残留={remaining}",
                    suite="b1",
                )
            else:
                record(
                    ctx,
                    "B1-cleanup",
                    "B1 兜底清场：uninstall 异常时已用注册表 API 删掉测试前缀项",
                    "PASS",
                    f"测试前缀={prefix}（只删测试命名项）",
                    suite="b1",
                )
    finally:
        state.clear()
