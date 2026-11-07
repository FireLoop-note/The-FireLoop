"""Fire Loop M2 验收项（模型链路 / 结果窗口）。【骨架：依赖 task-13/14 完成后填充真实检查】

阶段约定（由 run_m1a_acceptance.py 调用）：

- ``run_m2_phase``：M1a/B1/B2 主 agent 生命周期全部结束、清场之前运行。
  用临时 FIRELOOP_HOME 里的 ``config.json`` / ``secrets.json`` / ``results/``，
  起本地 mock LLM 服务（127.0.0.1，标准库 http.server），**绝不调用真实 DeepSeek API**、
  **绝不读取或触碰真实 %LOCALAPPDATA%\\FireLoop\\secrets.json**；
- 结果窗口默认 SKIP；只有 ``--enable-result-window`` 才做一次起停冒烟（会短暂弹窗）。

隔离红线：

- 假 key 固定 ``sk-test-<uuid>``，只写进临时 HOME 的 secrets.json / 子进程环境变量；
- config 里 ``model.base_url`` 指向本地 mock 服务；
- 验收进程自己的环境里如果恰好有 DEEPSEEK_API_KEY，起无 key 场景时要显式移除；
- 结果文件、config、secrets 全部在临时 HOME，跑完随 HOME 删除。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from harness import (
    PYTHON,
    ROOT,
    _Skip,
    check,
    first_line,
    load_logs,
    record,
    require,
)

SRC = Path(__file__).resolve().parents[2] / "src"
SHOW_SCRIPT = ROOT / "install" / "show.py"
PYTHONW = Path(r"D:\Python\pythonw.exe")
FAKE_KEY_PREFIX = "sk-test-"


# ---------------------------------------------------------------------------
# 本地 mock LLM 服务（标准库 http.server，OpenAI 兼容）
# ---------------------------------------------------------------------------


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """客户端超时先断开时，handler 写 socket 会 BrokenPipe；不要刷 traceback。"""

    def handle_error(self, request, client_address):  # noqa: D401
        return


class MockLLMServer:
    """127.0.0.1 上的假 /chat/completions 服务。

    - 记录每个请求的 path / headers / body（验收断言用）；
    - 按 ``mode`` 返回：normal（OpenAI 兼容 200）、http_500、http_401、http_429、
      bad_json、bad_response（缺 choices）；
    - ``slow`` 模式会先 sleep ``delay_s`` 再回 200，用于超时验收。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.requests: list[dict] = []
        self.mode = "normal"
        self.delay_s = 0.0
        self.content = "这是 mock 模型的固定回答：一句话说明 + 3 条要点。"
        self.response_model = "mock-model"
        self.usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> "MockLLMServer":
        handler = self._make_handler()
        self._httpd = _QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="mock-llm", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise AssertionError("mock LLM 服务还没启动")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    # ---------- 请求记录 ----------

    def reset_requests(self) -> None:
        with self._lock:
            self.requests.clear()

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self.requests)

    def wait_requests(self, count: int, timeout: float = 20.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.request_count() >= count:
                return self.snapshot()
            time.sleep(0.05)
        return self.snapshot()

    # ---------- HTTP handler ----------

    def _send_json(self, handler: http.server.BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _make_handler(self):
        mock = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D401 —— 静默，别刷验收输出
                return

            def do_POST(self):  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length else b""
                body = None
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = None
                entry = {
                    "path": self.path,
                    "headers": {key: value for key, value in self.headers.items()},
                    "raw_body": raw,
                    "body": body,
                    "received_at": time.monotonic(),
                }
                with mock._lock:
                    mock.requests.append(entry)

                if self.path.split("?")[0].rstrip("/") != "/chat/completions":
                    mock._send_json(self, 404, {"error": {"message": "mock: not found"}})
                    return

                mode = mock.mode
                if mode == "slow":
                    time.sleep(mock.delay_s)
                if mode in ("http_500", "http_401", "http_429"):
                    status = int(mode.split("_")[1])
                    mock._send_json(self, status, {"error": {"message": f"mock {status}"}})
                    return
                if mode == "bad_json":
                    data = b"this is not json"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if mode == "bad_response":
                    mock._send_json(self, 200, {"id": "chatcmpl-mock", "choices": []})
                    return
                mock._send_json(
                    self,
                    200,
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": mock.response_model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": mock.content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": dict(mock.usage),
                    },
                )

        return _Handler


# ---------------------------------------------------------------------------
# 临时 HOME / 配置 / 结果 / 日志小工具
# ---------------------------------------------------------------------------


def _home(ctx) -> Path:
    return Path(ctx.home)


def _write_config(
    ctx,
    model_overrides: dict,
    events_enabled: bool = False,
    ui_overrides: dict | None = None,
) -> Path:
    """写临时 HOME 的 config.json（结构与 src/fireloop/config.py 对齐）。

    验收默认把 ui.show_result_window 关掉，避免 agent 处理任务时真弹窗打扰用户；
    结果窗口本身由 M2-W（--enable-result-window）用 install/show.py 单独冒烟。
    """
    model = {
        "enabled": True,
        "provider": "deepseek",
        "base_url": "",
        "model": "mock-model",
        "timeout_s": 5.0,
        "max_file_chars": 20000,
        "max_output_tokens": 200,
        "max_calls_per_day": 100,
        "file_enabled": True,
    }
    model.update(model_overrides)
    ui = {"show_result_window": False}
    if ui_overrides:
        ui.update(ui_overrides)
    config = {"events": {"enabled": events_enabled}, "model": model, "ui": ui}
    path = _home(ctx) / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _write_secrets(ctx, api_key: str) -> Path:
    path = _home(ctx) / "secrets.json"
    path.write_text(json.dumps({"deepseek_api_key": api_key}, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _clear_secrets(ctx) -> None:
    path = _home(ctx) / "secrets.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _results_dir(ctx) -> Path:
    return _home(ctx) / "results"


def _result_files(ctx) -> list[Path]:
    """results/ 下的成功结果文件（result-*.md）。"""
    directory = _results_dir(ctx)
    return sorted(directory.glob("result-*.md")) if directory.is_dir() else []


def _error_files(ctx) -> list[Path]:
    """results/ 下的失败结果文件（error-*.md，可读错误会写在这里）。"""
    directory = _results_dir(ctx)
    return sorted(directory.glob("error-*.md")) if directory.is_dir() else []


def _wait_error_document(ctx, before: set[Path], timeout: float = 15.0) -> Path | None:
    """等本次场景新增的 error-*.md（可读错误文档）。"""

    def probe():
        new_files = [path for path in _error_files(ctx) if path not in before]
        return new_files or None

    hit = _wait_for(probe, timeout=timeout)
    return hit[0] if hit else None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _log_count(ctx) -> int:
    _, records, _ = load_logs(ctx)
    return len(records)


def _records_since(ctx, baseline: int) -> tuple[list[dict], str]:
    _, records, raw = load_logs(ctx)
    return records[baseline:], raw


def _wait_for(predicate, timeout: float = 20.0, interval: float = 0.1, what: str = "条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _start_m2_agent(
    ctx,
    *,
    api_key_env: str | None = None,
    label: str = "M2 测试 agent",
):
    """起 M2 阶段隔离 agent；返回 (proc, pump)。"""
    env_extra = None
    if api_key_env is not None:
        env_extra = {"DEEPSEEK_API_KEY": api_key_env}
    # 无 key 场景：确保子进程环境里没有 DEEPSEEK_API_KEY（验收机器环境里可能有）
    proc, pump, line = ctx.start_agent(
        ctx.m2_pipe,
        ctx.m2_mutex,
        label=label,
        env_extra=env_extra,
        env_remove=() if api_key_env is not None else ("DEEPSEEK_API_KEY",),
    )
    require(ctx.m2_pipe in line, f"M2 agent 启动行不含隔离管道：{line!r}")
    return proc, pump


def _stop_m2_agent(ctx, proc, pump) -> None:
    if proc is not None and proc.poll() is None:
        if not ctx.skip_graceful_exit:
            delivered, exit_code, _lines = ctx.stop_agent_graceful(proc, pump, timeout=10)
            if delivered:
                return
        ctx.stop_agent_hard(proc, pump)


def _sweep_result_windows(ctx) -> list[str]:
    """清掉 agent/冒烟留下的 pythonw show.py 进程（只认命令里带本次临时 HOME 的）。

    agent 每处理完一个文件任务都会 pythonw 起结果窗口；验收默认不验证窗口，
    但也不能让窗口弹出来打扰用户或残留进程。这里用 CIM 查命令行，按临时 HOME 过滤，
    只杀本套件相关的 show.py 进程。
    """
    home_text = str(ctx.home).replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -and "
        "$_.CommandLine.Contains('show.py') -and $_.CommandLine.Contains('__HOME__') } | "
        "Select-Object -ExpandProperty ProcessId"
    ).replace("__HOME__", home_text)
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    killed: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        subprocess.run(
            ["taskkill.exe", "/PID", line, "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        killed.append(line)
    return killed


# ---------------------------------------------------------------------------
# 结果窗口：默认 SKIP；--enable-result-window 才做一次起停冒烟
# ---------------------------------------------------------------------------


def _run_result_window_check(ctx) -> None:
    def t_result_window() -> str:
        if not ctx.enable_result_window:
            raise _Skip(
                "结果窗口默认 SKIP（真弹窗会打扰桌面）；加 --enable-result-window 才真测。SKIP 不算通过。"
            )
        require(SHOW_SCRIPT.is_file(), f"结果展示器入口不存在（task-16 未完成）：{SHOW_SCRIPT}")
        require(PYTHONW.is_file(), f"找不到 pythonw.exe：{PYTHONW}")
        text_file = _home(ctx) / "result-window-smoke.txt"
        text_file.write_text("M2 验收：结果窗口冒烟（2 秒后由验收关闭）", encoding="utf-8")
        proc = subprocess.Popen(
            [
                str(PYTHONW),
                str(SHOW_SCRIPT),
                "--title",
                "Fire Loop M2 验收冒烟",
                "--text-file",
                str(text_file),
            ],
            cwd=str(ROOT),
            env=ctx.child_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ctx.procs.append(("结果窗口冒烟", proc))
        try:
            time.sleep(2.0)
            require(proc.poll() is None, "结果窗口进程 2 秒内就退出了（可能没弹出来或直接报错）")
            return "pythonw show.py 弹出结果窗口并存活 2 秒（已关闭）"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            _sweep_result_windows(ctx)

    check(
        ctx,
        "M2-W",
        "结果窗口：pythonw show.py 起停冒烟（--enable-result-window 才真测）",
        t_result_window,
        suite="m2",
    )


# ---------------------------------------------------------------------------
# M2 模型链路（mock LLM 全链路 / 无 key / 错误 / 配额 / 截断 / 隐私）
# ---------------------------------------------------------------------------


def _new_fake_key() -> str:
    return f"{FAKE_KEY_PREFIX}{uuid.uuid4().hex}"


def _register_fake_key(ctx, key: str) -> None:
    keys = getattr(ctx, "_m2_fake_keys", None)
    if keys is None:
        keys = []
        ctx._m2_fake_keys = keys
    keys.append(key)


def _write_input_file(ctx, name: str, content: str) -> Path:
    """在临时 HOME 写验收输入文件，并登记内容标记供隐私扫描用。"""
    path = _home(ctx) / name
    path.write_text(content, encoding="utf-8")
    markers = getattr(ctx, "_m2_content_markers", None)
    if markers is None:
        markers = []
        ctx._m2_content_markers = markers
    markers.append(content)
    return path


def _model_deps_ready() -> list[str]:
    missing = [
        name
        for name in ("llm.py", "task_processor.py", "secrets.py")
        if not (SRC / "fireloop" / name).is_file()
    ]
    return missing


def _real_sensitive_snapshot() -> dict:
    """真实 %LOCALAPPDATA%\\FireLoop 的 secrets.json / config.json 只读快照。

    验收全程只读这个快照做前后比对，绝不读取/打印 key 内容、绝不修改文件。
    """
    local = os.environ.get("LOCALAPPDATA", "").strip()
    snapshot: dict = {"root_exists": False, "secrets.json": None, "config.json": None}
    if not local:
        return snapshot
    root = Path(local) / "FireLoop"
    if not root.is_dir():
        return snapshot
    snapshot["root_exists"] = True
    for name in ("secrets.json", "config.json"):
        try:
            stat = (root / name).stat()
            snapshot[name] = (stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            pass
    return snapshot


def _events(records: list[dict], name: str) -> list[dict]:
    return [record_item for record_item in records if record_item.get("event") == name]


def _first_event(records: list[dict], name: str) -> dict | None:
    hits = _events(records, name)
    return hits[0] if hits else None


def _messages_text(request: dict) -> list[str]:
    body = request.get("body") or {}
    messages = body.get("messages") or []
    texts: list[str] = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            texts.append(message["content"])
    return texts


def _is_alive(proc) -> bool:
    return proc is not None and proc.poll() is None


def _ping_m2_ok(ctx) -> bool:
    try:
        cli, _ = ctx.run_cli(["ping"], pipe_name=ctx.m2_pipe)
    except Exception:  # noqa: BLE001
        return False
    return cli.returncode == 0 and cli.stdout.strip() == "pong"


@contextlib.contextmanager
def _m2_agent(ctx, mock: MockLLMServer, *, model_overrides: dict | None = None, secrets_key: str | None = None):
    """起一个 M2 隔离 agent；退出时优雅停掉并清理结果窗口进程。"""
    overrides = dict(model_overrides or {})
    overrides["base_url"] = mock.base_url
    _write_config(ctx, overrides)
    if secrets_key is None:
        _clear_secrets(ctx)
    else:
        _write_secrets(ctx, secrets_key)
    mock.reset_requests()
    baseline = _log_count(ctx)
    proc = pump = None
    try:
        proc, pump = _start_m2_agent(ctx)
        yield baseline, proc, pump
    finally:
        _stop_m2_agent(ctx, proc, pump)
        _sweep_result_windows(ctx)


def _wait_event(ctx, baseline: int, name: str, timeout: float = 20.0, predicate=None) -> dict | None:
    def probe():
        records, _ = _records_since(ctx, baseline)
        hits = [
            record_item
            for record_item in records
            if record_item.get("event") == name
            and (predicate is None or predicate(record_item))
        ]
        return hits or None

    result = _wait_for(probe, timeout=timeout)
    return result[0] if result else None


def _run_model_checks(ctx, mock: MockLLMServer, real_sensitive_before: dict) -> None:
    # ---- M2-1 全链路：cli file → agent 入队 → mock 模型 → results/*.md ----
    def t_full_chain() -> str:
        fake_key = _new_fake_key()
        _register_fake_key(ctx, fake_key)
        marker = f"M2-FILE-CONTENT-{uuid.uuid4().hex}"
        input_file = _write_input_file(
            ctx, "M2 全链路 输入.txt", f"{marker}\n这是验收用的临时文件内容：一句话总结。"
        )
        with _m2_agent(ctx, mock, secrets_key=fake_key) as (baseline, proc, _pump):
            results_before = set(_result_files(ctx))
            cli, elapsed = ctx.run_cli(["file", str(input_file)], pipe_name=ctx.m2_pipe, timeout=30)
            require(
                cli.returncode == 0,
                f"cli file exit={cli.returncode} stdout={cli.stdout!r} stderr={first_line(cli.stderr)!r}",
            )
            require(
                "收到任务" in cli.stdout,
                f"cli file 没有回 queued 回显，stdout={cli.stdout!r}",
            )
            new_results = _wait_for(
                lambda: [path for path in _result_files(ctx) if path not in results_before],
                timeout=30,
                what="结果文件",
            )
            require(
                bool(new_results),
                f"30s 内没等到结果文件 {_results_dir(ctx)}/result-*.md",
            )
            result_text = _read_text(new_results[0])
            require(
                mock.content in result_text,
                f"结果文件内容不是 mock 返回（前缀={result_text[:100]!r}）",
            )
            require(
                f"total {mock.usage['total_tokens']}" in result_text,
                f"结果文件里没有正确的 usage（期望 total {mock.usage['total_tokens']}）",
            )
            requests = mock.wait_requests(1, timeout=15)
            require(bool(requests), "mock LLM 服务没收到任何请求（模型链路没通）")
            request = requests[0]
            require(
                request.get("path", "").split("?")[0].rstrip("/") == "/chat/completions",
                f"mock 收到非预期路径：{request.get('path')!r}",
            )
            auth = (request.get("headers") or {}).get("Authorization", "")
            require(auth == f"Bearer {fake_key}", "mock 收到的 Authorization 不是本次假 key")
            body = request.get("body") or {}
            require(body.get("model") == "mock-model", f"请求 model 不对：{body.get('model')!r}")
            require(body.get("stream") is False, "请求 stream 不是 false")
            require(isinstance(body.get("max_tokens"), int), "请求缺 max_tokens")
            # task-18：DeepSeek 默认 thinking 会吃光 output tokens 导致 content 为空。
            # 只断言"如果传了 thinking 就必须是 disabled"，不强制要求字段存在（兼容 task-18 前后）。
            thinking = body.get("thinking")
            if thinking is not None:
                require(
                    thinking == {"type": "disabled"},
                    f"请求体 thinking 不是 disabled：{thinking!r}",
                )
                thinking_note = "thinking=disabled"
            else:
                thinking_note = "请求体无 thinking 字段"
            require("reasoning_effort" not in body, "请求体不应传 reasoning_effort")
            require(
                any(marker in text for text in _messages_text(request)),
                "prompt 里没有输入文件内容标记",
            )
            records, raw = _records_since(ctx, baseline)
            events = {record_item.get("event") for record_item in records}
            for needed in ("task_enqueued", "task_processed", "model_request", "model_response"):
                require(needed in events, f"日志缺少 {needed}；实际事件={sorted(events)}")
            request_event = _first_event(records, "model_request") or {}
            require(request_event.get("model") == "mock-model", "model_request 没记 model")
            require(
                isinstance(request_event.get("text_len"), int) and request_event["text_len"] > 0,
                "model_request 没记 text_len",
            )
            response_event = _first_event(records, "model_response") or {}
            usage = response_event.get("usage")
            require(isinstance(usage, dict), f"model_response 没记 usage：{response_event!r}")
            require(
                int(usage.get("total_tokens", -1)) == mock.usage["total_tokens"],
                f"usage.total_tokens 不对：{usage!r}，期望 {mock.usage['total_tokens']}",
            )
            processed_event = _first_event(records, "task_processed") or {}
            require(processed_event.get("ok") is True, f"task_processed ok != True：{processed_event!r}")
            require(
                processed_event.get("result_file") == str(new_results[0]),
                "task_processed 记的 result_file 和实际结果文件不一致",
            )
            require(fake_key not in raw, "日志里出现了假 key（隐私红线）")
            require(marker not in raw, "日志里出现了文件内容原文（隐私红线）")
            return (
                f"exit=0 queued 回显；results/{new_results[0].name} 内容=mock 返回；"
                f"日志 task_enqueued/task_processed/model_request/model_response 齐全、"
                f"usage.total={usage.get('total_tokens')}；{thinking_note}；"
                f"假 key/正文未进日志（cli 耗时 {elapsed:.2f}s）"
            )

    check(ctx, "M2-1", "mock LLM 全链路：cli file → agent 入队 → mock 调用 → results/*.md", t_full_chain, suite="m2")

    # ---- M2-2 普通 task 仍回显（M1 行为回归，不调模型） ----
    def t_plain_task() -> str:
        with _m2_agent(ctx, mock, secrets_key=_new_fake_key()):
            cli, elapsed = ctx.run_cli(["task", "M2 普通任务仍然回显"], pipe_name=ctx.m2_pipe)
            require(cli.returncode == 0, f"exit={cli.returncode} stderr={first_line(cli.stderr)!r}")
            require(
                cli.stdout.strip() == "收到任务",
                f"普通 task 回显变了：stdout={cli.stdout!r}（期望 收到任务）",
            )
            require(
                mock.request_count() == 0,
                f"普通 task 居然调用了模型（mock 收到 {mock.request_count()} 次请求）",
            )
            return f"普通 task 仍回显 收到任务，mock 0 次调用（{elapsed:.2f}s）"

    check(ctx, "M2-2", "普通 task 仍走 M1 回显，不触发模型调用（回归）", t_plain_task, suite="m2")

    # ---- M2-3 大文件截断：mock 收到的 prompt 被 max_file_chars 截断 ----
    def t_truncation() -> str:
        max_chars = 300
        head_marker = f"M2-TRUNC-HEAD-{uuid.uuid4().hex}"
        tail_marker = f"M2-TRUNC-TAIL-{uuid.uuid4().hex}"
        input_file = _write_input_file(
            ctx, "M2 大文件 输入.txt", head_marker + "A" * 2000 + tail_marker
        )
        with _m2_agent(
            ctx,
            mock,
            model_overrides={"max_file_chars": max_chars},
            secrets_key=_new_fake_key(),
        ) as (baseline, _proc, _pump):
            cli, _elapsed = ctx.run_cli(["file", str(input_file)], pipe_name=ctx.m2_pipe, timeout=30)
            require(cli.returncode == 0, f"cli file exit={cli.returncode} stdout={cli.stdout!r}")
            requests = mock.wait_requests(1, timeout=25)
            require(bool(requests), "mock LLM 没收到大文件任务的请求")
            texts = _messages_text(requests[0])
            combined = "\n".join(texts)
            require(head_marker in combined, "prompt 里没有文件开头标记（可能文件没读进去）")
            require(
                tail_marker not in combined,
                f"大文件没被截断：尾部标记仍出现在 prompt（max_file_chars={max_chars}）",
            )
            require(
                "已截断" in combined or "[文件内容超过" in combined,
                "prompt 里没有截断标注（截断标记没加）",
            )
            containing = [text for text in texts if head_marker in text] or texts
            longest = max(len(text) for text in containing)
            require(
                longest <= max_chars + 800,
                f"送进 prompt 的文件段 {longest} 字符，超出 max_file_chars={max_chars} 太多",
            )
            request_event = _wait_event(ctx, baseline, "model_request", timeout=10)
            require(request_event is not None, "日志缺少 model_request（截断场景）")
            require(request_event.get("truncated") is True, f"model_request.truncated 不是 True：{request_event!r}")
            require(
                int(request_event.get("max_file_chars", -1)) == max_chars,
                f"model_request.max_file_chars 不对：{request_event.get('max_file_chars')!r}",
            )
            return (
                f"max_file_chars={max_chars}：开头标记在、尾部标记被截掉，"
                f"prompt 最长文件段 {longest} 字符，日志 truncated=True"
            )

    check(ctx, "M2-3", "大文件截断：超过 max_file_chars 的正文不会完整送进模型", t_truncation, suite="m2")

    # ---- M2-4 每日配额：第二次 file 被拒 + quota_drop ----
    def t_quota() -> str:
        first_file = _write_input_file(ctx, "M2 配额 第一次.txt", f"配额测试第一次 {uuid.uuid4().hex}")
        second_file = _write_input_file(ctx, "M2 配额 第二次.txt", f"配额测试第二次 {uuid.uuid4().hex}")
        with _m2_agent(
            ctx,
            mock,
            model_overrides={"max_calls_per_day": 1},
            secrets_key=_new_fake_key(),
        ) as (baseline, proc, _pump):
            results_before = set(_result_files(ctx))
            cli1, _ = ctx.run_cli(["file", str(first_file)], pipe_name=ctx.m2_pipe, timeout=30)
            require(cli1.returncode == 0, f"第一次 cli file exit={cli1.returncode} stdout={cli1.stdout!r}")
            first_done = _wait_for(
                lambda: [path for path in _result_files(ctx) if path not in results_before],
                timeout=30,
                what="第一次结果文件",
            )
            require(bool(first_done), "第一次 file 任务没产生结果文件")
            cli2, _ = ctx.run_cli(["file", str(second_file)], pipe_name=ctx.m2_pipe, timeout=30)
            require(cli2.returncode == 0, f"第二次 cli file exit={cli2.returncode} stdout={cli2.stdout!r}")
            require(
                "任务未提交" in cli2.stdout,
                f"配额拒绝没有给用户可读提示，stdout={cli2.stdout!r}",
            )
            quota_event = _wait_event(ctx, baseline, "quota_drop", timeout=15)
            require(quota_event is not None, "第二次 file 后日志没有 quota_drop（配额没生效）")
            require(
                mock.request_count() == 1,
                f"配额=1 但模型被调了 {mock.request_count()} 次",
            )
            new_results = [path for path in _result_files(ctx) if path not in results_before]
            require(len(new_results) == 1, f"配额=1 但产生了 {len(new_results)} 个结果文件")
            require(_is_alive(proc), "配额拒绝后 agent 崩了")
            require(_ping_m2_ok(ctx), "配额拒绝后 agent 不再响应 ping")
            return "max_calls_per_day=1：第一次成功、第二次 quota_drop，模型只被调 1 次，agent 存活"

    check(ctx, "M2-4", "每日配额：max_calls_per_day=1 时第二次 file 被拒并记 quota_drop", t_quota, suite="m2")

    # ---- M2-5 无 key：no_key 可读错误、agent 不崩、不联网 ----
    def t_no_key() -> str:
        input_file = _write_input_file(ctx, "M2 无 key 输入.txt", f"无 key 场景 {uuid.uuid4().hex}")
        with _m2_agent(ctx, mock, secrets_key=None) as (baseline, proc, pump):
            results_before = set(_result_files(ctx))
            errors_before = set(_error_files(ctx))
            cli, _ = ctx.run_cli(["file", str(input_file)], pipe_name=ctx.m2_pipe, timeout=30)
            require(cli.returncode == 0, f"cli file exit={cli.returncode} stdout={cli.stdout!r}")
            error_event = _wait_event(ctx, baseline, "model_error", timeout=20)
            require(error_event is not None, f"无 key 时日志没有 model_error；事件={sorted({r.get('event') for r in _records_since(ctx, baseline)[0]})}")
            code = error_event.get("code")
            require(code == "no_key", f"无 key 错误码={code!r}（期望 no_key）")
            require(mock.request_count() == 0, "无 key 却发出了模型网络请求")
            require(_is_alive(proc), "无 key 错误后 agent 崩了")
            require(_ping_m2_ok(ctx), "无 key 错误后 agent 不再响应 ping")
            records, raw = _records_since(ctx, baseline)
            require("sk-" not in raw, "无 key 场景日志出现 sk- 前缀")
            require(
                not [path for path in _result_files(ctx) if path not in results_before],
                "无 key 场景不该产生成功结果文件",
            )
            # 错误文件可能在 model_error 之后才写；先等它出现再读
            error_document = _wait_error_document(ctx, errors_before, timeout=10)
            require(error_document is not None, "无 key 时没有写 error-*.md（可读错误没落地）")
            error_text = _read_text(error_document)
            require("处理失败" in error_text, f"错误文档不是可读格式：{error_text[:80]!r}")
            require("no_key" in error_text, f"错误文档没有 no_key：{error_text[:200]!r}")
            require("错误码" in error_text, f"错误文档缺少可读提示（错误码）：{error_text[:200]!r}")
            require("sk-" not in error_text, "错误文档里出现了 sk- 前缀")
            output = "\n".join(pump.snapshot()) if pump is not None else ""
            require("Traceback" not in output, "agent 输出出现裸 traceback（无 key 错误没被可读处理）")
            return "model_error code=no_key，agent 存活且 ping 正常；mock 0 请求、日志无 sk-、无 traceback"

    check(ctx, "M2-5", "无 key：file 任务给出可读 no_key 错误，agent 不崩且不联网", t_no_key, suite="m2")

    # ---- M2-6 mock 返回 500：可读错误、agent 不崩 ----
    def t_http_500() -> str:
        input_file = _write_input_file(ctx, "M2 500 输入.txt", f"500 场景 {uuid.uuid4().hex}")
        with _m2_agent(ctx, mock, secrets_key=_new_fake_key()) as (baseline, proc, pump):
            errors_before = set(_error_files(ctx))
            mock.mode = "http_500"
            cli, _ = ctx.run_cli(["file", str(input_file)], pipe_name=ctx.m2_pipe, timeout=30)
            require(cli.returncode == 0, f"cli file exit={cli.returncode} stdout={cli.stdout!r}")
            error_event = _wait_event(ctx, baseline, "model_error", timeout=20)
            require(error_event is not None, "500 场景日志没有 model_error")
            require(
                error_event.get("code") == "http_error",
                f"500 错误码={error_event.get('code')!r}（期望 http_error）",
            )
            require(_is_alive(proc), "500 错误后 agent 崩了")
            require(_ping_m2_ok(ctx), "500 错误后 agent 不再响应 ping")
            error_document = _wait_error_document(ctx, errors_before, timeout=10)
            require(error_document is not None, "500 错误没有写 error-*.md（可读错误没落地）")
            error_text = _read_text(error_document)
            require("处理失败" in error_text, f"错误文档不是可读格式：{error_text[:120]!r}")
            require("http_error" in error_text, f"错误文档没有 http_error 原因：{error_text[:200]!r}")
            output = "\n".join(pump.snapshot()) if pump is not None else ""
            require("Traceback" not in output, "agent 输出出现裸 traceback（500 错误没被可读处理）")
            return "mock 500 → model_error code=http_error + error-*.md 可读错误，agent 存活且 ping 正常"

    check(ctx, "M2-6", "mock 返回 500：可读错误、agent 不崩", t_http_500, suite="m2")

    # ---- M2-7 mock 超时：可读错误、agent 不崩 ----
    def t_timeout() -> str:
        input_file = _write_input_file(ctx, "M2 超时 输入.txt", f"超时场景 {uuid.uuid4().hex}")
        with _m2_agent(
            ctx,
            mock,
            model_overrides={"timeout_s": 1.0},
            secrets_key=_new_fake_key(),
        ) as (baseline, proc, pump):
            errors_before = set(_error_files(ctx))
            mock.mode = "slow"
            mock.delay_s = 3.0
            started = time.monotonic()
            cli, _ = ctx.run_cli(["file", str(input_file)], pipe_name=ctx.m2_pipe, timeout=30)
            require(cli.returncode == 0, f"cli file exit={cli.returncode} stdout={cli.stdout!r}")
            error_event = _wait_event(ctx, baseline, "model_error", timeout=20)
            require(error_event is not None, "超时场景日志没有 model_error")
            require(
                error_event.get("code") == "timeout",
                f"超时错误码={error_event.get('code')!r}（期望 timeout）",
            )
            require(time.monotonic() - started <= 15.0, "超时没有在预期时间内返回")
            require(_is_alive(proc), "超时错误后 agent 崩了")
            require(_ping_m2_ok(ctx), "超时错误后 agent 不再响应 ping")
            error_document = _wait_error_document(ctx, errors_before, timeout=10)
            require(error_document is not None, "超时错误没有写 error-*.md（可读错误没落地）")
            error_text = _read_text(error_document)
            require("处理失败" in error_text, f"错误文档不是可读格式：{error_text[:120]!r}")
            require("timeout" in error_text, f"错误文档没有 timeout 原因：{error_text[:200]!r}")
            return f"mock 慢响应 + timeout_s=1 → model_error code=timeout + error-*.md，agent 存活且 ping 正常"

    check(ctx, "M2-7", "mock 超时：可读错误、agent 不崩", t_timeout, suite="m2")

    # ---- M2-8 日志/隐私扫描 + 真实 secrets 未被碰 ----
    def t_privacy() -> str:
        fake_keys = getattr(ctx, "_m2_fake_keys", [])
        markers = getattr(ctx, "_m2_content_markers", [])
        require(fake_keys, "验收没有记录假 key，隐私扫描不成立")
        require(markers, "验收没有记录输入文件内容，隐私扫描不成立")
        _files, _records, raw = load_logs(ctx)
        for fake_key in fake_keys:
            require(fake_key not in raw, "日志里出现假 key（隐私红线）")
        require(
            re.search(r"sk-[A-Za-z0-9]{4,}", raw) is None,
            "日志里出现 sk- 开头的疑似真实 key（隐私红线）",
        )
        for marker in markers:
            require(marker not in raw, f"日志里出现输入文件内容原文：{marker[:40]!r}…（隐私红线）")
        after = _real_sensitive_snapshot()
        require(
            after == real_sensitive_before,
            f"真实 %LOCALAPPDATA%\\FireLoop 的 secrets/config 被改动了：{real_sensitive_before} → {after}",
        )
        produced = [path for path in _result_files(ctx)]
        return (
            f"日志无 sk- key、无 {len(markers)} 个输入内容原文；"
            f"真实 secrets/config 前后一致（只读快照）；本次结果文件 {len(produced)} 个"
        )

    check(ctx, "M2-8", "日志隐私扫描：无 key（sk-）、无文件内容原文；真实 secrets 未被触碰", t_privacy, suite="m2")


def run_m2_phase(ctx, args=None) -> None:
    """M2 验收阶段：mock LLM 服务 + 临时 HOME 隔离 + 结果窗口开关。"""
    missing = _model_deps_ready()
    mock = MockLLMServer()
    real_sensitive_before = _real_sensitive_snapshot()
    try:
        if missing:
            def pending() -> str:
                raise _Skip(
                    f"M2 模型链路验收待依赖就绪（缺少 src/fireloop/{missing}）；"
                    "mock 服务/隔离 HOME/结果窗口开关已就绪，依赖完成后再真跑"
                )

            check(ctx, "M2-1", "M2 模型链路（依赖未就绪）", pending, suite="m2")
        else:
            mock.start()
            try:
                _run_model_checks(ctx, mock, real_sensitive_before)
            finally:
                _sweep_result_windows(ctx)
    finally:
        mock.stop()
        _sweep_result_windows(ctx)
    _run_result_window_check(ctx)
