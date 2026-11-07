"""M2 任务处理器单测（标准库 unittest）。

纪律：
- **绝不联网**：``fireloop.llm.chat`` 在 setUp 里就被替换成"一调就 AssertionError"
  的 mock，每个用例再显式给返回值/异常；不会读真实 key、不会发 HTTP。
- **绝不真弹窗**：``task_processor._launch_result_window`` 整体 mock。
- **绝不碰真实数据目录**：每个用例临时 ``FIRELOOP_HOME``，结果文件/日志都写在临时目录。
- 每个用例有硬超时看门狗（30s），挂死就 dump 线程栈并退出进程。

运行：
    cd D:\\ds植入式
    $env:PYTHONPATH="$PWD\\src"
    & D:\\Python\\python.exe -m unittest discover -s tests -p "test_task_processor.py" -v
"""

from __future__ import annotations

import faulthandler
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fireloop import (  # noqa: E402
    agent as agent_entry,
    config as config_module,
    llm,
    paths,
    protocol,
    task_processor as tp,
)
from fireloop.agent import AgentPipeServer, build_file_task_reply  # noqa: E402

_FAKE_CHAT_CONTENT = "这是火环给出的文件分析结果。"


def _model_cfg(**overrides) -> dict:
    """一份 model 段配置（默认参考 config.DEFAULT_MODEL_CONFIG）。"""
    cfg = dict(config_module.DEFAULT_MODEL_CONFIG)
    cfg.update(overrides)
    return cfg


def _full_config(*, model: dict | None = None, ui: dict | None = None) -> dict:
    cfg = config_module.default_config()
    if model:
        cfg["model"].update(model)
    if ui:
        cfg["ui"].update(ui)
    return cfg


def _chat_result(content: str = _FAKE_CHAT_CONTENT) -> dict:
    return {
        "content": content,
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        "model": "deepseek-flash",
    }


class _HardTimeoutMixin:
    """每个用例硬超时：超时 dump 所有线程栈后直接退出进程。"""

    TEST_TIMEOUT_SECONDS = 30.0

    def _start_watchdog(self) -> None:
        test_id = self.id()

        def _bark() -> None:
            print(f"\n[watchdog] 用例 {test_id} 超过 {self.TEST_TIMEOUT_SECONDS}s", flush=True)
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
    """每个用例独立 FIRELOOP_HOME，不碰真实 %LOCALAPPDATA%\\FireLoop。"""

    def _setup_temp_home(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="fireloop-tp-test-")
        self._old_home = os.environ.get("FIRELOOP_HOME")
        os.environ["FIRELOOP_HOME"] = self._tmp_dir.name
        paths.ensure_dirs()

    def _teardown_temp_home(self) -> None:
        if self._old_home is None:
            os.environ.pop("FIRELOOP_HOME", None)
        else:
            os.environ["FIRELOOP_HOME"] = self._old_home
        self._tmp_dir.cleanup()

    def home(self) -> Path:
        return Path(os.environ["FIRELOOP_HOME"])

    def make_input_file(self, name: str, content: str | bytes) -> Path:
        directory = self.home() / "inputs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def read_logs(self) -> tuple[list[dict], str]:
        records: list[dict] = []
        chunks: list[str] = []
        logs_dir = paths.logs_dir()
        if logs_dir.exists():
            for path in sorted(logs_dir.glob("*.jsonl")):
                text = path.read_text(encoding="utf-8")
                chunks.append(text)
                for line in text.splitlines():
                    if line.strip():
                        records.append(json.loads(line))
        return records, "".join(chunks)


def _wait_idle(processor: tp.TaskProcessor, timeout: float = 5.0) -> None:
    """等 worker 把队列里的任务全部处理完（unfinished_tasks 归零）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if processor._queue.unfinished_tasks == 0:
            time.sleep(0.01)  # 让 task_done 之后可能残留的日志/弹窗 mock 收尾
            return
        time.sleep(0.01)
    raise AssertionError(f"{timeout}s 内任务没有处理完")


def _wait_for(predicate, timeout: float = 5.0, message: str = "条件未满足") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{timeout}s 内{message}")


class _TaskProcessorTestBase(_TempHomeMixin, _HardTimeoutMixin, unittest.TestCase):
    """公共夹具：临时 home + 禁网 mock + 禁弹窗 mock + processor 清理。"""

    def setUp(self) -> None:
        self._start_watchdog()
        self._setup_temp_home()
        self._processors: list[tp.TaskProcessor] = []

        # 禁网：任何测试真的调到 llm.chat 都立刻失败（说明测试写错了）。
        llm_patcher = mock.patch("fireloop.llm.chat")
        self.llm_mock = llm_patcher.start()
        self.llm_mock.side_effect = AssertionError("测试禁止真调 llm.chat（会联网/读 key）")
        self.addCleanup(llm_patcher.stop)

        # 禁弹窗：结果窗口启动整体 mock。
        launch_patcher = mock.patch.object(tp, "_launch_result_window")
        self.launch_mock = launch_patcher.start()
        self.addCleanup(launch_patcher.stop)

    def tearDown(self) -> None:
        try:
            for processor in self._processors:
                try:
                    processor.stop(timeout=2.0)
                except Exception:
                    pass
            self._teardown_temp_home()
        finally:
            self._stop_watchdog()

    # ---- 夹具辅助 ----

    def make_processor(
        self, *, model: dict | None = None, ui: dict | None = None, start: bool = True
    ) -> tp.TaskProcessor:
        processor = tp.TaskProcessor(_full_config(model=model, ui=ui))
        self._processors.append(processor)
        if start:
            processor.start()
        return processor

    def allow_chat(self, content: str = _FAKE_CHAT_CONTENT) -> mock.Mock:
        self.llm_mock.side_effect = None
        self.llm_mock.return_value = _chat_result(content)
        return self.llm_mock

    def results(self, prefix: str = "result") -> list[Path]:
        results_dir = self.home() / "results"
        if not results_dir.exists():
            return []
        return sorted(results_dir.glob(f"{prefix}-*.md"))

    def log_events(self) -> list[str]:
        records, _ = self.read_logs()
        return [record.get("event") for record in records]

    def event_records(self, event: str) -> list[dict]:
        records, _ = self.read_logs()
        return [record for record in records if record.get("event") == event]


# ---------------------------------------------------------------------------
# 正常处理流程
# ---------------------------------------------------------------------------


class FileTaskSuccessTests(_TaskProcessorTestBase):
    def test_success_writes_result_file_and_launches_window(self) -> None:
        input_file = self.make_input_file("note.txt", "第一行\n机密正文-XYZ\n")
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        chat = self.allow_chat("这是火环给出的文件分析结果。")

        self.assertTrue(processor.submit(f"file:{input_file}", "req-ok"))
        _wait_idle(processor)

        # 1) llm.chat 收到正确的 messages
        self.assertEqual(chat.call_count, 1)
        messages = chat.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], tp.SYSTEM_PROMPT)
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn(str(input_file), messages[1]["content"])
        self.assertIn("机密正文-XYZ", messages[1]["content"])
        self.assertNotIn("已截断", messages[1]["content"])

        # 2) 结果文件生成
        results = self.results("result")
        self.assertEqual(len(results), 1, results)
        result_text = results[0].read_text(encoding="utf-8")
        self.assertIn("火环 · 文件分析", result_text)
        self.assertIn("这是火环给出的文件分析结果。", result_text)
        self.assertIn(str(input_file), result_text)
        self.assertIn("- 截断：否", result_text)

        # 3) 结果窗口被拉起（mock），指向结果文件
        self.launch_mock.assert_called_once()
        kwargs = self.launch_mock.call_args.kwargs
        self.assertEqual(kwargs["title"], "火环 · 文件分析")
        self.assertEqual(Path(kwargs["text_file"]), results[0])

        # 4) 日志事件齐全，且不含正文/key
        events = self.log_events()
        for expected in ("task_enqueued", "model_request", "model_response", "task_processed"):
            self.assertIn(expected, events)
        _, all_logs = self.read_logs()
        self.assertNotIn("机密正文-XYZ", all_logs, "日志里不允许出现文件内容原文")
        self.assertNotRegex(all_logs, r"sk-[A-Za-z0-9]{8,}", "日志里不允许出现 key")
        model_request = self.event_records("model_request")[-1]
        self.assertEqual(model_request["text_len"], len("第一行\n机密正文-XYZ\n"))
        self.assertFalse(model_request["truncated"])
        task_processed = self.event_records("task_processed")[-1]
        self.assertTrue(task_processed["ok"])
        self.assertEqual(Path(task_processed["result_file"]), results[0])

    def test_llm_receives_config_params(self) -> None:
        input_file = self.make_input_file("a.txt", "hello")
        processor = self.make_processor(
            model={
                "enabled": True,
                "file_enabled": True,
                "model": "my-model",
                "base_url": "http://127.0.0.1:9/v1/",
                "timeout_s": 12.5,
                "max_output_tokens": 321,
            }
        )
        chat = self.allow_chat()

        self.assertTrue(processor.submit(f"file:{input_file}", "req-params"))
        _wait_idle(processor)

        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["model"], "my-model")
        self.assertEqual(kwargs["base_url"], "http://127.0.0.1:9/v1")  # rstrip("/")
        self.assertEqual(kwargs["timeout_s"], 12.5)
        self.assertEqual(kwargs["max_tokens"], 321)

    def test_ui_show_result_window_false_skips_launch(self) -> None:
        input_file = self.make_input_file("a.txt", "hello")
        processor = self.make_processor(
            model={"enabled": True, "file_enabled": True},
            ui={"show_result_window": False},
        )
        self.allow_chat()

        self.assertTrue(processor.submit(f"file:{input_file}", "req-no-window"))
        _wait_idle(processor)

        self.assertEqual(len(self.results("result")), 1)
        self.launch_mock.assert_not_called()

    def test_large_file_is_truncated_in_prompt_and_marked(self) -> None:
        input_file = self.make_input_file("big.txt", "A" * 200)
        processor = self.make_processor(
            model={"enabled": True, "file_enabled": True, "max_file_chars": 20}
        )
        chat = self.allow_chat()

        self.assertTrue(processor.submit(f"file:{input_file}", "req-big"))
        _wait_idle(processor)

        user_content = chat.call_args.args[0][1]["content"]
        self.assertIn("已截断", user_content)
        sent_content = user_content.split("文件内容：\n", 1)[1].split("\n\n[", 1)[0]
        self.assertEqual(sent_content, "A" * 20)

        request = self.event_records("model_request")[-1]
        self.assertTrue(request["truncated"])
        self.assertEqual(request["max_file_chars"], 20)
        self.assertEqual(request["text_len"], 20)

        result_text = self.results("result")[0].read_text(encoding="utf-8")
        self.assertIn("- 截断：是", result_text)

    def test_binary_like_file_reads_with_replacement(self) -> None:
        input_file = self.make_input_file("weird.bin", b"\xff\xfe\x00abc\x80")
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.allow_chat()

        self.assertTrue(processor.submit(f"file:{input_file}", "req-bin"))
        _wait_idle(processor)

        self.assertEqual(len(self.results("result")), 1)
        self.assertTrue(self.event_records("task_processed")[-1]["ok"])


# ---------------------------------------------------------------------------
# 文件校验失败
# ---------------------------------------------------------------------------


class FileTaskValidationTests(_TaskProcessorTestBase):
    def test_missing_file_shows_error_and_no_result_file(self) -> None:
        missing = self.home() / "inputs" / "nope.txt"
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})

        self.assertTrue(processor.submit(f"file:{missing}", "req-missing"))
        _wait_idle(processor)

        self.assertEqual(self.results("result"), [])
        errors = self.results("error")
        self.assertEqual(len(errors), 1)
        error_text = errors[0].read_text(encoding="utf-8")
        self.assertIn("文件不存在", error_text)
        self.assertIn("file_not_found", error_text)
        self.launch_mock.assert_called_once()
        self.assertEqual(self.launch_mock.call_args.kwargs["title"], "火环 · 处理失败")

        processed = self.event_records("task_processed")[-1]
        self.assertFalse(processed["ok"])
        self.assertEqual(processed["reason"], "file_not_found")
        self.llm_mock.assert_not_called()

    def test_directory_path_rejected(self) -> None:
        directory = self.home() / "inputs"
        directory.mkdir(parents=True, exist_ok=True)
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})

        self.assertTrue(processor.submit(f"file:{directory}", "req-dir"))
        _wait_idle(processor)

        self.assertEqual(self.results("result"), [])
        processed = self.event_records("task_processed")[-1]
        self.assertFalse(processed["ok"])
        self.assertEqual(processed["reason"], "not_a_file")
        self.llm_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 队列 / 去重 / 配额 / 启停
# ---------------------------------------------------------------------------


class QueuePolicyTests(_TaskProcessorTestBase):
    def test_dedupe_same_task_within_window(self) -> None:
        input_file = self.make_input_file("a.txt", "hello")
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.allow_chat()
        task_text = f"file:{input_file}"

        self.assertTrue(processor.submit(task_text, "req-1"))
        ok, reason = processor.submit_with_reason(task_text, "req-2")
        self.assertFalse(ok)
        self.assertEqual(reason, tp.REASON_DEDUPE)
        self.assertFalse(processor.submit(task_text, "req-3"))

        _wait_idle(processor)
        self.assertEqual(self.llm_mock.call_count, 1, "去重命中不应重复调模型")
        self.assertTrue(self.event_records("task_deduped"), "缺少 task_deduped 日志")

    def test_different_task_text_not_deduped(self) -> None:
        file_a = self.make_input_file("a.txt", "A")
        file_b = self.make_input_file("b.txt", "B")
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.allow_chat()

        self.assertTrue(processor.submit(f"file:{file_a}", "req-a"))
        self.assertTrue(processor.submit(f"file:{file_b}", "req-b"))
        _wait_idle(processor)
        self.assertEqual(self.llm_mock.call_count, 2)

    def test_quota_drop_rejects_after_daily_limit(self) -> None:
        file_a = self.make_input_file("a.txt", "A")
        file_b = self.make_input_file("b.txt", "B")
        processor = self.make_processor(
            model={"enabled": True, "file_enabled": True, "max_calls_per_day": 1}
        )
        self.allow_chat()

        self.assertTrue(processor.submit(f"file:{file_a}", "req-first"))
        ok, reason = processor.submit_with_reason(f"file:{file_b}", "req-second")
        self.assertFalse(ok)
        self.assertEqual(reason, tp.REASON_QUOTA)

        _wait_idle(processor)
        self.assertEqual(self.llm_mock.call_count, 1)
        quota_records = self.event_records("quota_drop")
        self.assertEqual(len(quota_records), 1)
        self.assertEqual(quota_records[0]["limit"], 1)

    def test_queue_full_rejects_when_worker_not_started(self) -> None:
        processor = self.make_processor(
            model={"enabled": True, "file_enabled": True}, start=False
        )
        try:
            accepted = 0
            for index in range(tp.QUEUE_MAXSIZE):
                if processor.submit(f"file:C:\\tmp\\f{index}.txt", f"req-q{index}"):
                    accepted += 1
            self.assertEqual(accepted, tp.QUEUE_MAXSIZE)
            ok, reason = processor.submit_with_reason("file:C:\\tmp\\extra.txt", "req-extra")
            self.assertFalse(ok)
            self.assertEqual(reason, tp.REASON_QUEUE_FULL)
            self.assertTrue(self.event_records("task_queue_full"))
        finally:
            processor.stop(timeout=1.0)

    def test_stop_terminates_worker_and_rejects_new_tasks(self) -> None:
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.assertTrue(processor.is_worker_alive())
        processor.stop(timeout=3.0)
        self.assertFalse(processor.is_worker_alive())
        ok, reason = processor.submit_with_reason("file:C:\\tmp\\x.txt", "req-after-stop")
        self.assertFalse(ok)
        self.assertEqual(reason, tp.REASON_STOPPED)

    def test_bad_submit_arguments_rejected(self) -> None:
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.assertFalse(processor.submit("hello", "req-1"))
        self.assertFalse(processor.submit("file:", "req-2"))
        self.assertFalse(processor.submit("file:C:\\x.txt", ""))
        ok, reason = processor.submit_with_reason("hello", "req-3")
        self.assertFalse(ok)
        self.assertIsNone(reason)


# ---------------------------------------------------------------------------
# 模型错误
# ---------------------------------------------------------------------------


class ModelErrorTests(_TaskProcessorTestBase):
    def test_llm_error_shows_error_window_without_result(self) -> None:
        input_file = self.make_input_file("a.txt", "hello")
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.llm_mock.side_effect = llm.LLMError(
            "no_key", "没有可用的 DeepSeek API key，请先配置"
        )

        self.assertTrue(processor.submit(f"file:{input_file}", "req-no-key"))
        _wait_idle(processor)

        self.assertEqual(self.results("result"), [])
        errors = self.results("error")
        self.assertEqual(len(errors), 1)
        self.assertIn("no_key", errors[0].read_text(encoding="utf-8"))
        self.assertEqual(self.launch_mock.call_args.kwargs["title"], "火环 · 处理失败")

        model_errors = self.event_records("model_error")
        self.assertEqual(len(model_errors), 1)
        self.assertEqual(model_errors[0]["code"], "no_key")
        processed = self.event_records("task_processed")[-1]
        self.assertFalse(processed["ok"])
        self.assertEqual(processed["reason"], "no_key")

    def test_unexpected_exception_treated_as_internal_and_worker_survives(self) -> None:
        file_a = self.make_input_file("a.txt", "A")
        file_b = self.make_input_file("b.txt", "B")
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.llm_mock.side_effect = [RuntimeError("boom-secret-detail"), _chat_result()]

        self.assertTrue(processor.submit(f"file:{file_a}", "req-bad"))
        _wait_idle(processor)
        self.assertTrue(processor.is_worker_alive(), "单任务异常不能拖垮 worker")

        self.assertTrue(processor.submit(f"file:{file_b}", "req-good"))
        _wait_idle(processor, timeout=5.0)
        self.assertEqual(len(self.results("result")), 1)

        model_errors = self.event_records("model_error")
        self.assertEqual(model_errors[0]["code"], "internal")
        _, all_logs = self.read_logs()
        self.assertNotIn("boom-secret-detail", all_logs, "日志不记异常原文")

    def test_result_window_launch_failure_does_not_fail_task(self) -> None:
        input_file = self.make_input_file("a.txt", "hello")
        processor = self.make_processor(model={"enabled": True, "file_enabled": True})
        self.allow_chat()
        self.launch_mock.side_effect = OSError("show.py 起不来")

        self.assertTrue(processor.submit(f"file:{input_file}", "req-launch-fail"))
        _wait_idle(processor)

        self.assertEqual(len(self.results("result")), 1)
        processed = self.event_records("task_processed")[-1]
        self.assertTrue(processed["ok"])
        self.assertTrue(self.event_records("result_window_error"))


# ---------------------------------------------------------------------------
# 结果窗口命令 / 项目根
# ---------------------------------------------------------------------------


class ResultWindowCommandTests(_TempHomeMixin, _HardTimeoutMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_watchdog()
        self._setup_temp_home()

    def tearDown(self) -> None:
        try:
            self._teardown_temp_home()
        finally:
            self._stop_watchdog()

    def test_build_command_uses_show_py_and_expected_args(self) -> None:
        text_file = self.home() / "results" / "result-x.md"
        command = tp.build_result_window_command("标题", text_file)

        self.assertIsInstance(command, list)
        self.assertEqual(Path(command[0]).name.lower() in {"pythonw.exe", "python.exe"}, True)
        show_py = Path(command[1])
        self.assertEqual(show_py.name, "show.py")
        self.assertEqual(show_py.parent.name, "install")
        self.assertEqual(show_py, tp.project_root() / "install" / "show.py")
        self.assertIn("--title", command)
        self.assertEqual(command[command.index("--title") + 1], "标题")
        self.assertIn("--text-file", command)
        self.assertEqual(command[command.index("--text-file") + 1], str(text_file))

    def test_project_root_points_at_repo(self) -> None:
        root = tp.project_root()
        self.assertTrue((root / "src" / "fireloop" / "task_processor.py").is_file())
        self.assertTrue((root / "install" / "show.py").is_file())


# ---------------------------------------------------------------------------
# agent 分派逻辑
# ---------------------------------------------------------------------------


class AgentDispatchTests(_TempHomeMixin, _HardTimeoutMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_watchdog()
        self._setup_temp_home()

    def tearDown(self) -> None:
        try:
            self._teardown_temp_home()
        finally:
            self._stop_watchdog()

    def _fake_processor(self, *, submit_result: tuple[bool, str | None] = (True, None)):
        processor = mock.Mock()
        processor.submit_with_reason.return_value = submit_result
        return processor

    def test_file_task_queued_gets_immediate_ack(self) -> None:
        processor = self._fake_processor()
        reply = build_file_task_reply(
            protocol.make_task("id-1", "file:C:\\tmp\\a.txt"),
            processor,
            _model_cfg(enabled=True, file_enabled=True),
        )
        self.assertEqual(
            reply,
            {
                "v": protocol.PROTOCOL_VERSION,
                "type": protocol.RESULT,
                "ok": True,
                "id": "id-1",
                "text": "收到任务，正在处理…",
                "queued": True,
            },
        )
        processor.submit_with_reason.assert_called_once_with(
            "file:C:\\tmp\\a.txt", "id-1"
        )

    def test_non_file_task_falls_back_to_original_echo(self) -> None:
        reply = build_file_task_reply(
            protocol.make_task("id-2", "普通任务"),
            self._fake_processor(),
            _model_cfg(enabled=True, file_enabled=True),
        )
        self.assertIsNone(reply)

    def test_ping_not_intercepted(self) -> None:
        reply = build_file_task_reply(
            protocol.make_ping("id-3"),
            self._fake_processor(),
            _model_cfg(enabled=True, file_enabled=True),
        )
        self.assertIsNone(reply)

    def test_model_disabled_falls_back(self) -> None:
        reply = build_file_task_reply(
            protocol.make_task("id-4", "file:C:\\tmp\\a.txt"),
            self._fake_processor(),
            _model_cfg(enabled=False, file_enabled=True),
        )
        self.assertIsNone(reply)

    def test_file_enabled_false_falls_back(self) -> None:
        reply = build_file_task_reply(
            protocol.make_task("id-5", "file:C:\\tmp\\a.txt"),
            self._fake_processor(),
            _model_cfg(enabled=True, file_enabled=False),
        )
        self.assertIsNone(reply)

    def test_missing_id_or_empty_path_falls_back(self) -> None:
        processor = self._fake_processor()
        model_cfg = _model_cfg(enabled=True, file_enabled=True)
        self.assertIsNone(
            build_file_task_reply(
                {"v": 1, "type": protocol.TASK, "text": "file:C:\\tmp\\a.txt"},
                processor,
                model_cfg,
            )
        )
        self.assertIsNone(
            build_file_task_reply(protocol.make_task("id-6", "file:"), processor, model_cfg)
        )
        processor.submit_with_reason.assert_not_called()

    def test_rejected_submit_returns_queued_false_with_reason(self) -> None:
        cases = (
            (tp.REASON_DEDUPE, "重复任务"),
            (tp.REASON_QUOTA, "配额已用完"),
            (tp.REASON_QUEUE_FULL, "队列已满"),
            (tp.REASON_STOPPED, "正在退出"),
        )
        for reason, text_hint in cases:
            processor = self._fake_processor(submit_result=(False, reason))
            reply = build_file_task_reply(
                protocol.make_task(f"id-{reason}", "file:C:\\tmp\\a.txt"),
                processor,
                _model_cfg(enabled=True, file_enabled=True),
            )
            self.assertIsNotNone(reply)
            self.assertTrue(reply["ok"])
            self.assertFalse(reply["queued"])
            self.assertEqual(reply["reason"], reason)
            self.assertTrue(reply["text"].startswith("任务未提交："))
            self.assertIn(text_hint, reply["text"])

    def test_agent_pipe_server_dispatch_override(self) -> None:
        processor = self._fake_processor()
        model_cfg = _model_cfg(enabled=True, file_enabled=True)
        # 用 patch 基类 __init__ 避免真的创建命名管道实例
        with mock.patch.object(agent_entry.PipeServer, "__init__", return_value=None):
            server = AgentPipeServer(
                pipe_name=protocol.PIPE_NAME,
                token="fake-token",
                processor=processor,
                model_cfg=model_cfg,
            )

        file_reply = server._dispatch(protocol.make_task("id-7", "file:C:\\tmp\\a.txt"))
        self.assertTrue(file_reply["queued"])

        echo_reply = server._dispatch(protocol.make_task("id-8", "普通任务"))
        self.assertTrue(echo_reply["ok"])
        self.assertEqual(echo_reply["text"], "收到任务")

        ping_reply = server._dispatch(protocol.make_ping("id-9"))
        self.assertEqual(ping_reply["text"], "pong")


class AgentLifecycleTests(_TempHomeMixin, _HardTimeoutMixin, unittest.TestCase):
    """进程内验证 agent.main：processor 先 start、退出 finally 里 stop。"""

    def setUp(self) -> None:
        self._start_watchdog()
        self._setup_temp_home()

    def tearDown(self) -> None:
        try:
            self._teardown_temp_home()
        finally:
            self._stop_watchdog()

    def test_agent_main_starts_and_stops_task_processor(self) -> None:
        recorded: dict = {}

        class FakeProcessor:
            def __init__(self, cfg):
                recorded["processor_config"] = cfg
                recorded["processor"] = self

            def start(self) -> None:
                recorded["processor_started"] = True

            def stop(self, timeout: float = 5.0) -> None:
                recorded["processor_stopped"] = timeout

        class FakeServer:
            def __init__(self, pipe_name, token, processor=None, model_cfg=None, validate_sid=True):
                recorded["server"] = {
                    "pipe_name": pipe_name,
                    "token": token,
                    "processor": processor,
                    "model_cfg": model_cfg,
                    "validate_sid": validate_sid,
                }

            def serve_forever(self) -> None:
                recorded["served"] = True

            def stop(self) -> None:
                pass

        class FakeSingleInstance:
            def __init__(self, name):
                recorded["mutex_name"] = name

            def acquire(self) -> bool:
                return True

            def release(self) -> None:
                recorded["released"] = True

        output = io.StringIO()
        model_section = dict(config_module.DEFAULT_MODEL_CONFIG)
        model_section.update({"enabled": True, "file_enabled": True})
        fake_config = config_module.default_config()
        fake_config["model"] = model_section

        with (
            mock.patch.object(agent_entry, "SingleInstance", FakeSingleInstance),
            mock.patch.object(agent_entry, "TaskProcessor", FakeProcessor),
            mock.patch.object(agent_entry, "AgentPipeServer", FakeServer),
            mock.patch.object(agent_entry, "_install_console_handler", return_value=False),
            mock.patch.object(agent_entry, "load_config", return_value=fake_config),
            mock.patch.object(agent_entry, "start_event_watchers", return_value=(None, [])),
            mock.patch.object(agent_entry, "stop_event_watchers", return_value=None),
            redirect_stdout(output),
        ):
            rc = agent_entry.main(["--pipe-name", r"\\.\pipe\fireloop-test-lifecycle"])

        self.assertEqual(rc, 0)
        self.assertTrue(recorded["processor_started"])
        self.assertEqual(recorded["processor_stopped"], 5.0, "退出时必须以 timeout=5.0 停 processor")
        self.assertTrue(recorded["served"])
        self.assertTrue(recorded["released"])
        self.assertIsInstance(recorded["server"]["processor"], FakeProcessor)
        self.assertTrue(recorded["server"]["model_cfg"]["enabled"])
        self.assertIs(recorded["server"]["validate_sid"], True, "SID 校验必须保持默认开启")
        self.assertIn("Fire Loop agent 已启动", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
