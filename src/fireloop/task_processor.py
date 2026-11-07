"""Fire Loop M2 任务处理器：右键文件任务串行处理。

流程（详见 ``docs/fire-loop-方案.md`` M2 部分 / task-14 契约）：

1. ``submit("file:<路径>", req_id)`` 入队：单 worker 线程 + ``queue.Queue(maxsize=16)``
   串行处理，避免并发调模型；60s 内同任务文本指纹去重；每日配额超限拒绝并记
   ``quota_drop``。
2. worker 取到任务后：解析路径 → 存在性/是否文件校验 → 读文本（UTF-8
   ``errors=replace``，超过 ``config.model.max_file_chars`` 截断并标注）→ 构造
   messages（system 提示词 + 文件路径与内容）→ ``llm.chat()``。
3. 成功：把结果写成 ``<FireLoop home>\\results\\result-<时间戳>.md``，并用
   ``pythonw <项目根>\\install\\show.py --title ... --text-file ...`` 拉起结果窗口。
4. 失败：把可读错误（绝不含 API key）写成 ``error-<时间戳>.md`` 并用同一个窗口展示。

安全口径：日志只记长度/截断/usage/耗时/错误码/结果文件路径，**绝不记 API key、
绝不记文件内容原文**；本模块不直接碰 key（``llm.chat`` 自己解析），也不联网写死地址。
"""

from __future__ import annotations

import hashlib
import queue
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

from . import config as config_module
from . import llm, log, paths

# ---- 对外常量 ----
FILE_TASK_PREFIX = "file:"

#: 队列上限：串行处理，排队太多没意义，满了直接拒。
QUEUE_MAXSIZE = 16
#: 同一任务文本的指纹去重窗口（秒）。
DEDUPE_WINDOW_S = 60.0

#: submit 失败原因码（agent 回给客户端响应帧里的 ``reason`` 字段）。
REASON_DEDUPE = "dedupe"
REASON_QUOTA = "quota"
REASON_QUEUE_FULL = "queue_full"
REASON_STOPPED = "stopped"

#: 失败原因码 → 给用户看的中文短句（用于 "任务未提交：<短句>"）。
REASON_TEXT = {
    REASON_DEDUPE: "重复任务，60 秒内已提交过",
    REASON_QUOTA: "今日模型调用配额已用完",
    REASON_QUEUE_FULL: "任务队列已满",
    REASON_STOPPED: "agent 正在退出",
}

#: M2 system 提示词（任务书给定口径：一句话说明 + 5 条以内要点 + 问题/风险，简洁不客套）。
SYSTEM_PROMPT = (
    "你是火环，用户右键了一个文件交给你。请用简体中文输出："
    "先用一句话说明这个文件是什么；再用 5 条以内要点讲清楚它讲了什么；"
    "最后指出明显的问题或风险。简洁，不客套，不要复述整个文件。"
)

_RESULTS_DIR_NAME = "results"
_RESULT_TITLE_OK = "火环 · 文件分析"
_RESULT_TITLE_ERROR = "火环 · 处理失败"
_PYTHONW_NAME = "pythonw.exe"
_SHOW_RELATIVE = Path("install") / "show.py"

_TRUNCATE_MARKER = "\n\n[文件内容超过 {max_chars} 字符，已截断，只分析了前面部分]"

#: 配额按自然日重置。
_TODAY_FACTORY = date.today


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def _as_bool(value, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_positive_int(value, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _as_positive_number(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def _as_str(value, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return value.strip()


def model_config(config: dict | None) -> dict:
    """从完整配置里取出规范化后的 model 段。

    ``config.load_config()`` 在生产路径已经规范化过；这里再兜一层，保证
    ``TaskProcessor`` 和 agent 分派逻辑拿到的字段一定齐全、类型安全。
    缺失/非法一律回 ``config.DEFAULT_MODEL_CONFIG`` 的安全默认（全关）。
    """
    defaults = config_module.DEFAULT_MODEL_CONFIG
    raw = config.get("model") if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "enabled": _as_bool(raw.get("enabled"), defaults["enabled"]),
        "provider": _as_str(raw.get("provider"), defaults["provider"]),
        "base_url": _as_str(raw.get("base_url"), defaults["base_url"]).rstrip("/"),
        "model": _as_str(raw.get("model"), defaults["model"]),
        "timeout_s": _as_positive_number(raw.get("timeout_s"), defaults["timeout_s"]),
        "max_file_chars": _as_positive_int(
            raw.get("max_file_chars"), defaults["max_file_chars"]
        ),
        "max_output_tokens": _as_positive_int(
            raw.get("max_output_tokens"), defaults["max_output_tokens"]
        ),
        "max_calls_per_day": _as_positive_int(
            raw.get("max_calls_per_day"), defaults["max_calls_per_day"]
        ),
        "file_enabled": _as_bool(raw.get("file_enabled"), defaults["file_enabled"]),
    }


def ui_config(config: dict | None) -> dict:
    """从完整配置里取 UI 段（当前只关心 show_result_window）。"""
    defaults = config_module.DEFAULT_UI_CONFIG
    raw = config.get("ui") if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "show_result_window": _as_bool(
            raw.get("show_result_window"), defaults["show_result_window"]
        ),
    }


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def fingerprint(text: str) -> str:
    """任务文本指纹（用于去重，不用于日志原文）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def parse_file_task(task_text: str) -> Path:
    """解析 ``file:<路径>``；格式不对抛 ValueError（不含文件内容）。"""
    if not isinstance(task_text, str) or not task_text.startswith(FILE_TASK_PREFIX):
        raise ValueError("不是 file: 任务")
    raw = task_text[len(FILE_TASK_PREFIX):].strip()
    if raw[:1] == raw[-1:] and raw[:1] in {"'", '"'} and len(raw) >= 2:
        raw = raw[1:-1].strip()
    if not raw:
        raise ValueError("文件路径为空")
    return Path(raw)


def read_text_limited(path: Path, max_chars: int) -> tuple[str, bool]:
    """读 UTF-8 文本（errors=replace），最多读 ``max_chars + 1`` 字符。

    返回 ``(文本, 是否被截断)``。只读上限+1 个字符，避免超大文件把内存吃穿。
    文件不是文本时 errors=replace 保证不抛 UnicodeDecodeError。
    """
    max_chars = max(1, int(max_chars))
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read(max_chars + 1)
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]
    return content, truncated


def build_messages(path: Path, content: str, truncated: bool, max_chars: int) -> list[dict]:
    """构造给模型的 messages：system 提示词 + 文件路径与内容。"""
    user_text = f"文件路径：{path}\n\n文件内容：\n{content}"
    if truncated:
        user_text += _TRUNCATE_MARKER.format(max_chars=max_chars)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]


def _results_dir() -> Path:
    directory = paths.home() / _RESULTS_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write_result_file(text: str, prefix: str) -> Path:
    """写结果文件，保证文件名不撞车（同毫秒也加序号）。"""
    directory = _results_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]  # 毫秒精度
    name = f"{prefix}-{stamp}.md"
    path = directory / name
    index = 1
    while path.exists():
        path = directory / f"{prefix}-{stamp}-{index}.md"
        index += 1
    path.write_text(text, encoding="utf-8")
    return path


def render_result_markdown(
    *,
    path: Path,
    model: str,
    truncated: bool,
    content: str,
    usage: dict | None,
    latency_ms: int,
) -> str:
    """成功结果的 markdown。"""
    usage = usage if isinstance(usage, dict) else {}
    lines = [
        "# 火环 · 文件分析",
        "",
        f"- 文件：`{path}`",
        f"- 模型：{model}",
        f"- 截断：{'是' if truncated else '否'}",
        f"- 耗时：{latency_ms} ms",
        (
            "- token："
            f"prompt {int(usage.get('prompt_tokens') or 0)} / "
            f"completion {int(usage.get('completion_tokens') or 0)} / "
            f"total {int(usage.get('total_tokens') or 0)}"
        ),
        "",
        "---",
        "",
        content.strip() or "（模型没有返回内容）",
        "",
    ]
    return "\n".join(lines)


def render_error_markdown(*, path: Path | None, message: str, reason: str) -> str:
    """失败结果的 markdown（调用方保证 message 不含 API key）。"""
    lines = [
        "# 火环 · 处理失败",
        "",
        f"- 原因：{reason}",
        f"- 文件：{path if path is not None else '（未知）'}",
        f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
        "",
        "---",
        "",
        message.strip() or "未提供错误详情",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 结果窗口启动（测试里 mock 掉，不真弹窗）
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """项目根：``src/fireloop/task_processor.py`` 往上两级。"""
    return Path(__file__).resolve().parents[2]


def build_result_window_command(title: str, text_file: Path) -> list[str]:
    """拼出 ``pythonw <root>\\install\\show.py --title ... --text-file ...``。

    pythonw 与当前解释器同目录优先；没有就退回 sys.executable（会短暂出控制台，
    但功能不丢）。
    """
    root = project_root()
    launcher = Path(sys.executable)
    pythonw = launcher.with_name(_PYTHONW_NAME)
    if pythonw.exists():
        launcher = pythonw
    return [
        str(launcher),
        str(root / _SHOW_RELATIVE),
        "--title",
        str(title),
        "--text-file",
        str(text_file),
    ]


def _launch_result_window(title: str, text_file: Path) -> object:
    """独立进程拉起结果窗口；不等待、不弹控制台。测试里整体 mock。"""
    command = build_result_window_command(title, text_file)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        command,
        cwd=str(project_root()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


# ---------------------------------------------------------------------------
# 任务处理器
# ---------------------------------------------------------------------------


class _TaskItem:
    """队列里的一条 file 任务。"""

    __slots__ = ("req_id", "text", "path", "enqueued_monotonic")

    def __init__(self, req_id: str, text: str, path: Path, enqueued_monotonic: float):
        self.req_id = req_id
        self.text = text
        self.path = path
        self.enqueued_monotonic = enqueued_monotonic


class TaskProcessor:
    """单 worker 线程的任务处理器（file task），串行调用模型。

    Args:
        config: ``config.load_config()`` 返回的完整配置 dict（取 ``model`` / ``ui`` 段）。

    接口（task-14 契约）：
        ``start()``：启动 worker 线程（幂等）；
        ``stop(timeout=5.0)``：请求停止并 join，最多等 ``timeout`` 秒；
        ``submit(task_text, req_id) -> bool``：入队成功返回 True。
    """

    def __init__(self, config: dict):
        self.config = config if isinstance(config, dict) else {}
        self.model = model_config(self.config)
        self.ui = ui_config(self.config)
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # 去重：task 指纹 -> 最近一次入队时刻（monotonic）
        self._recent: dict[str, float] = {}
        # 每日配额（自然日重置）
        self._quota_day: date = _TODAY_FACTORY()
        self._quota_used = 0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="fireloop-task-worker",
                daemon=True,
            )
            self._worker.start()

    def stop(self, timeout: float = 5.0) -> None:
        """请求停止：worker 处理完当前任务后退出；最多等 ``timeout`` 秒。"""
        self._stop_event.set()
        worker = self._worker
        if worker is None:
            return
        worker.join(timeout=max(0.0, float(timeout)))
        if worker.is_alive():
            log.log_event(
                "task_worker_stop_timeout",
                level="warning",
                timeout_s=timeout,
                pending=self._queue.qsize(),
            )
        self._worker = None

    def is_worker_alive(self) -> bool:
        worker = self._worker
        return worker is not None and worker.is_alive()

    # ---------- 提交 ----------

    def submit(self, task_text: str, req_id: str) -> bool:
        """入队成功返回 True（task-14 契约接口）。"""
        ok, _reason = self.submit_with_reason(task_text, req_id)
        return ok

    def submit_with_reason(self, task_text: str, req_id: str) -> tuple[bool, str | None]:
        """入队并返回 ``(是否入队, 失败原因码)``；成功时原因码为 None。

        失败原因码（agent 会原样回给客户端响应的 ``reason`` 字段，并配
        ``queued=False``）：``dedupe`` / ``quota`` / ``queue_full`` / ``stopped``。
        参数本身不合法（不是 file task、req_id 非法、file: 后路径为空）时返回
        ``(False, None)``，由调用方按原有协议逻辑处理（不回"任务未提交"帧）。
        """
        if not isinstance(task_text, str) or not task_text.startswith(FILE_TASK_PREFIX):
            log.log_event(
                "task_rejected",
                level="warning",
                reason="not_file_task",
                req_id=req_id if isinstance(req_id, str) else None,
            )
            return False, None
        if not isinstance(req_id, str) or not req_id:
            log.log_event(
                "task_rejected", level="warning", reason="bad_req_id", req_id=None
            )
            return False, None
        if self._stop_event.is_set():
            log.log_event(
                "task_rejected", level="warning", reason="stopping", req_id=req_id
            )
            return False, REASON_STOPPED

        try:
            path = parse_file_task(task_text)
        except ValueError as exc:
            log.log_event(
                "task_rejected",
                level="warning",
                reason="bad_file_path",
                req_id=req_id,
                detail=str(exc),
            )
            return False, None

        now = time.monotonic()
        task_fp = fingerprint(task_text)
        with self._lock:
            self._prune_recent_locked(now)
            last = self._recent.get(task_fp)
            if last is not None and (now - last) < DEDUPE_WINDOW_S:
                log.log_event(
                    "task_deduped",
                    level="warning",
                    req_id=req_id,
                    dedupe_window_s=DEDUPE_WINDOW_S,
                    age_s=round(now - last, 2),
                )
                return False, REASON_DEDUPE
            if not self._quota_available_locked():
                log.log_event(
                    "quota_drop",
                    level="warning",
                    req_id=req_id,
                    limit=self.model["max_calls_per_day"],
                    used=self._quota_used,
                )
                return False, REASON_QUOTA
            # 先预占配额，队列满再回滚
            self._quota_used += 1

        item = _TaskItem(req_id=req_id, text=task_text, path=path, enqueued_monotonic=now)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._lock:
                self._quota_used -= 1
            log.log_event(
                "task_queue_full",
                level="warning",
                req_id=req_id,
                maxsize=QUEUE_MAXSIZE,
            )
            return False, REASON_QUEUE_FULL

        with self._lock:
            self._recent[task_fp] = now
        log.log_event(
            "task_enqueued",
            req_id=req_id,
            task_kind="file",
            file_path=str(path),
            text_len=len(task_text),
            queue_size=self._queue.qsize(),
        )
        return True, None

    # ---------- worker ----------

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(item)
            except Exception as exc:  # 兜底：单任务异常不能拖垮 worker
                log.log_event(
                    "task_error",
                    level="error",
                    stage="worker",
                    req_id=getattr(item, "req_id", None),
                    error=repr(exc),
                )
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    # ---------- 处理 ----------

    def _process(self, item: _TaskItem) -> None:
        started = time.monotonic()
        path = item.path
        log_context = {"req_id": item.req_id, "file_path": str(path)}

        # 1) 存在性 / 是否文件（提交时不再校验，文件可能在这期间被删）
        if not path.exists():
            self._finish_error(
                item,
                reason="file_not_found",
                message=f"文件不存在：{path}",
                started=started,
            )
            return
        if not path.is_file():
            self._finish_error(
                item,
                reason="not_a_file",
                message=f"路径不是文件（可能是目录）：{path}",
                started=started,
            )
            return

        # 2) 读文本（有界读取 + errors=replace + 截断标注）
        max_chars = int(self.model["max_file_chars"])
        try:
            content, truncated = read_text_limited(path, max_chars)
        except OSError as exc:
            self._finish_error(
                item,
                reason="read_error",
                message=f"读取文件失败：{exc}",
                started=started,
            )
            return

        messages = build_messages(path, content, truncated, max_chars)

        # 3) 调模型（日志只记长度/模型/耗时，不记内容、不记 key）
        log.log_event(
            "model_request",
            req_id=item.req_id,
            model=self.model["model"],
            text_len=len(content),
            truncated=truncated,
            max_file_chars=max_chars,
        )
        call_started = time.perf_counter()
        try:
            result = llm.chat(
                messages,
                model=self.model["model"],
                base_url=self.model["base_url"],
                timeout_s=self.model["timeout_s"],
                max_tokens=self.model["max_output_tokens"],
            )
        except llm.LLMError as exc:
            latency_ms = int((time.perf_counter() - call_started) * 1000)
            # LLMError.message 契约保证不含 key；这里仍只记 code，不记 message
            log.log_event(
                "model_error",
                level="warning",
                req_id=item.req_id,
                code=exc.code,
                latency_ms=latency_ms,
                model=self.model["model"],
            )
            self._finish_error(
                item,
                reason=exc.code,
                message=f"{exc.message or '模型调用失败'}（错误码 {exc.code}）",
                started=started,
            )
            return
        except Exception as exc:  # 未预期异常：不把 traceback 文本/内容写进日志
            latency_ms = int((time.perf_counter() - call_started) * 1000)
            log.log_event(
                "model_error",
                level="error",
                req_id=item.req_id,
                code="internal",
                latency_ms=latency_ms,
                error_type=type(exc).__name__,
                model=self.model["model"],
            )
            self._finish_error(
                item,
                reason="internal",
                message="模型调用出现内部错误，请稍后再试",
                started=started,
            )
            return

        latency_ms = int((time.perf_counter() - call_started) * 1000)
        usage = result.get("usage") if isinstance(result, dict) else None
        returned_model = result.get("model") if isinstance(result, dict) else None
        content_out = result.get("content") if isinstance(result, dict) else ""
        content_out = content_out if isinstance(content_out, str) else ""
        log.log_event(
            "model_response",
            req_id=item.req_id,
            model=returned_model or self.model["model"],
            usage=usage if isinstance(usage, dict) else {},
            latency_ms=latency_ms,
        )

        # 4) 写结果文件 + 展示
        result_text = render_result_markdown(
            path=path,
            model=returned_model or self.model["model"],
            truncated=truncated,
            content=content_out,
            usage=usage if isinstance(usage, dict) else {},
            latency_ms=latency_ms,
        )
        result_file = _write_result_file(result_text, prefix="result")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.log_event(
            "task_processed",
            req_id=item.req_id,
            ok=True,
            reason="ok",
            file_path=str(path),
            result_file=str(result_file),
            elapsed_ms=elapsed_ms,
        )
        self._show(result_file, title=_RESULT_TITLE_OK)

    def _finish_error(self, item: _TaskItem, *, reason: str, message: str, started: float) -> None:
        """失败统一出口：写 error md + 展示 + 记 task_processed。"""
        text = render_error_markdown(path=item.path, message=message, reason=reason)
        try:
            error_file: Path | None = _write_result_file(text, prefix="error")
            error_file_text = str(error_file)
        except OSError as exc:  # 结果目录都写不了就只记日志
            error_file = None
            error_file_text = ""
            log.log_event(
                "error",
                level="error",
                stage="write_error_result",
                req_id=item.req_id,
                error=repr(exc),
            )
        log.log_event(
            "task_processed",
            req_id=item.req_id,
            ok=False,
            reason=reason,
            file_path=str(item.path),
            result_file=error_file_text,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        if error_file is not None:
            self._show(error_file, title=_RESULT_TITLE_ERROR)

    def _show(self, text_file: Path, *, title: str) -> None:
        """按 ui 配置决定是否弹结果窗口；弹窗失败只记日志，不影响主流程。"""
        if not self.ui.get("show_result_window", True):
            return
        try:
            _launch_result_window(title=title, text_file=text_file)
        except Exception as exc:
            log.log_event(
                "result_window_error",
                level="warning",
                stage="launch",
                error_type=type(exc).__name__,
            )

    # ---------- 内部：去重 / 配额 ----------

    def _prune_recent_locked(self, now: float) -> None:
        expired = [
            key
            for key, last in self._recent.items()
            if (now - last) >= DEDUPE_WINDOW_S
        ]
        for key in expired:
            self._recent.pop(key, None)

    def _quota_available_locked(self) -> bool:
        today = _TODAY_FACTORY()
        if today != self._quota_day:
            self._quota_day = today
            self._quota_used = 0
            log.log_event("quota_reset", date=str(today))
        return self._quota_used < int(self.model["max_calls_per_day"])
