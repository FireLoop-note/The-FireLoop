"""Fire Loop 常驻 agent 入口：``python -m fireloop.agent``。

启动流程（见 docs/fire-loop-方案.md）：

1. ``paths.ensure_dirs()`` 建数据目录；
2. ``paths.ensure_token()`` 读/生成 token；
3. ``SingleInstance`` 抢互斥体 ``Local\\FireLoopAgent``（可用 ``--mutex-name`` 覆盖，
   验收/测试环境借此与真实常驻 agent 完全隔离），抢不到说明已有实例，退出码 1；
4. 起 ``PipeServer`` 无限 accept；
5. 读 ``config.json``（``paths.config_path()``，由 ``config.load_config()`` 统一规范化
   成 events/model/ui 三段）：``events.enabled = true`` 时按 file/clipboard/foreground
   各自开关启动事件 watcher（**默认全关**）；``model.enabled = true`` 时把 "file:"
   开头的 task 交给 ``TaskProcessor`` 异步处理（单线程串行调模型），**默认关闭**，
   关闭时 file task 也只是回显，M1a 行为不变；退出时在 ``finally`` 里停止 processor
   和所有 watcher。

Ctrl+C：Windows 控制台里主线程正阻塞在 ``ConnectNamedPipe``，Python 的
KeyboardInterrupt 打不断 C 调用，所以额外用 ``SetConsoleCtrlHandler`` 注册
控制台处理器：收到 CTRL_C / CTRL_BREAK 时在后台线程里调 ``server.stop()``
（自我连接唤醒 accept 循环），随后正常清理并以退出码 0 退出。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
from ctypes import wintypes

from . import log, paths, protocol, task_processor
from .clipboard_watcher import ClipboardWatcher
from .config import load_config
from .events import EventBus, enabled_watchers
from .file_watcher import FileWatcher
from .foreground_watcher import ForegroundWatcher
from .pipe_server import PipeServer
from .single_instance import DEFAULT_MUTEX_NAME, SingleInstance
from .task_processor import TaskProcessor, model_config

_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

_SetConsoleCtrlHandler = _kernel32.SetConsoleCtrlHandler
_SetConsoleCtrlHandler.argtypes = (_HANDLER_ROUTINE, wintypes.BOOL)
_SetConsoleCtrlHandler.restype = wintypes.BOOL

_console_handler = None  # 全局持有回调引用，防止被 GC 回收


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fireloop.agent",
        description="Fire Loop 常驻 agent（命名管道 + JSON Lines；M2 支持右键文件分析）",
    )
    parser.add_argument(
        "--pipe-name",
        default=protocol.PIPE_NAME,
        help=f"命名管道全名，默认 {protocol.PIPE_NAME}",
    )
    parser.add_argument(
        "--mutex-name",
        default=None,
        help=(
            "单实例互斥体名，默认 "
            f"{DEFAULT_MUTEX_NAME}；验收/测试时传唯一名字即可与真实常驻 agent 隔离"
        ),
    )
    return parser


def _install_console_handler(server: PipeServer) -> bool:
    """注册控制台 Ctrl+C / Ctrl+Break 处理器。返回是否注册成功。"""

    def _on_console_ctrl(event: int) -> bool:
        try:
            if event in (_CTRL_C_EVENT, _CTRL_BREAK_EVENT):
                threading.Thread(
                    target=server.stop,
                    name="fireloop-stop",
                    daemon=True,
                ).start()
                return True
        except Exception:
            pass
        return False

    global _console_handler
    handler = _HANDLER_ROUTINE(_on_console_ctrl)
    try:
        if _SetConsoleCtrlHandler(handler, True):
            _console_handler = handler
            return True
    except Exception:
        pass
    return False


def build_event_watchers(config: dict, bus: EventBus) -> list:
    """按配置构造事件 watcher（只构造，不 start；便于测试/联调单独验证 gating）。"""
    flags = enabled_watchers(config)
    events_cfg = config.get("events", {}) if isinstance(config, dict) else {}
    watchers: list = []
    if flags["file"]:
        file_cfg = events_cfg.get("file", {})
        watch_dirs = file_cfg.get("watch_dirs", []) if isinstance(file_cfg, dict) else []
        watchers.append(FileWatcher(watch_dirs, bus))
    if flags["clipboard"]:
        watchers.append(ClipboardWatcher(bus))
    if flags["foreground"]:
        watchers.append(ForegroundWatcher(bus))
    return watchers


def start_event_watchers(
    config: dict | None = None,
) -> tuple[EventBus | None, list]:
    """按配置启动事件 watcher，返回 ``(bus, watchers)``。

    默认配置（``events.enabled = false``）直接返回 ``(None, [])``：不建总线、
    不起线程，保证 M1a 行为完全不变。单个 watcher 启动失败只记日志，不拖垮 agent。
    """
    if config is None:
        config = load_config()
    flags = enabled_watchers(config)
    if not any(flags.values()):
        return None, []

    bus = EventBus.from_config(config)
    watchers = build_event_watchers(config, bus)
    started: list = []
    for watcher in watchers:
        try:
            watcher.start()
        except Exception as exc:
            log.log_event(
                "watcher_error",
                level="warning",
                watcher=type(watcher).__name__,
                stage="start",
                error=repr(exc),
            )
            continue
        started.append(watcher)

    if started:
        log.log_event(
            "events_started",
            watchers=[type(watcher).__name__ for watcher in started],
            quota_per_hour=bus.quota_per_hour,
            debounce_ms=bus.debounce_ms,
            dedupe_window_s=bus.dedupe_window_s,
        )
    return bus, started


def stop_event_watchers(watchers: list) -> None:
    """停止所有事件 watcher；单个 watcher 报错不影响其它 watcher 和 agent 退出。"""
    if not watchers:
        return
    for watcher in watchers:
        try:
            watcher.stop()
        except Exception as exc:
            log.log_event(
                "watcher_error",
                level="warning",
                watcher=type(watcher).__name__,
                stage="stop",
                error=repr(exc),
            )
    log.log_event("events_stopped", count=len(watchers))


def build_file_task_reply(
    msg: dict,
    processor: TaskProcessor | None,
    model_cfg: dict | None,
) -> dict | None:
    """M2：判断一条请求是不是"要交给任务处理器"的 file task，并尝试入队。

    返回要回给客户端的响应 dict；返回 ``None`` 表示"不归任务处理器管"，
    交给 ``PipeServer._dispatch`` 的原逻辑（ping/task 回显、错误帧等）。

    归任务处理器管的条件（task-14 契约）：
    - type == task；text 以 ``file:`` 开头；
    - 配置里 ``model.enabled == True`` 且 ``model.file_enabled == True``；
    - 有非空 id、file: 后面有路径。
    入队失败（去重/配额/队列满/正在退出）回 ``ok=true, queued=false, reason=<码>``，
    让 CLI 能明确显示"任务未提交"及原因（协议 v1 没有专门的配额错误码）。
    """
    if not isinstance(msg, dict) or msg.get("type") != protocol.TASK:
        return None
    if processor is None or not isinstance(model_cfg, dict):
        return None
    if not (
        model_cfg.get("enabled") is True and model_cfg.get("file_enabled") is True
    ):
        return None

    text = msg.get("text")
    req_id = msg.get("id")
    if not isinstance(text, str) or not text.startswith(task_processor.FILE_TASK_PREFIX):
        return None
    if not isinstance(req_id, str) or not req_id:
        return None  # 缺 id：交给父类回 bad_msg
    try:
        task_processor.parse_file_task(text)
    except ValueError:
        return None  # file: 后路径为空等非法值：交给父类原逻辑

    queued, reason = processor.submit_with_reason(text, req_id)
    if queued:
        return protocol.make_ok(req_id, text="收到任务，正在处理…", queued=True)
    if reason not in task_processor.REASON_TEXT:
        return None
    return protocol.make_ok(
        req_id,
        text=f"任务未提交：{task_processor.REASON_TEXT[reason]}",
        queued=False,
        reason=reason,
    )


class AgentPipeServer(PipeServer):
    """agent 专用管道服务端：在父类协议分派前拦一层 M2 file task。

    ``pipe_server.py`` 保持通用（不 import llm / task_processor）；M2 的业务钩子
    放在 agent 层，通过覆写 ``_dispatch`` 实现，管道实现本身不用改。
    """

    def __init__(
        self,
        pipe_name: str = protocol.PIPE_NAME,
        token: str | None = None,
        processor: TaskProcessor | None = None,
        model_cfg: dict | None = None,
        validate_sid: bool = True,
    ):
        super().__init__(pipe_name=pipe_name, token=token, validate_sid=validate_sid)
        self._processor = processor
        self._model_cfg = dict(model_cfg) if isinstance(model_cfg, dict) else {}

    def _dispatch(self, msg: dict) -> dict:
        reply = build_file_task_reply(msg, self._processor, self._model_cfg)
        if reply is not None:
            return reply
        return super()._dispatch(msg)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    paths.ensure_dirs()
    token = paths.ensure_token()

    # None（没传 --mutex-name）时保持原来的默认行为，完全不受影响。
    mutex_name = args.mutex_name or DEFAULT_MUTEX_NAME

    instance = SingleInstance(mutex_name)
    if not instance.acquire():
        print("Fire Loop agent 已经在运行", flush=True)
        return 1

    server = None
    watchers: list = []  # 事件 watcher；默认配置（events.enabled=false）保持为空
    processor: TaskProcessor | None = None  # M2 任务处理器；见下方 start/stop
    try:
        # 统一配置（events/model/ui 三段）：文件不存在/坏 JSON 都安全回落到默认。
        config = load_config()
        model_cfg = model_config(config)

        # M2：任务处理器默认配置（model.enabled=false）下只是空转线程，不联网；
        # 只有 file task + 模型启用时才真正工作。
        processor = TaskProcessor(config)
        processor.start()

        if model_cfg.get("enabled"):
            server = AgentPipeServer(
                pipe_name=args.pipe_name,
                token=token,
                processor=processor,
                model_cfg=model_cfg,
            )
        else:
            # 模型没启用时不需要 M2 分派层：用原 PipeServer，M1 行为完全一致
            # （也保住了 tests/test_pipe_server.py 里对 PipeServer 构造的 mock）。
            server = PipeServer(pipe_name=args.pipe_name, token=token)
        try:
            _install_console_handler(server)
            # 默认配置下这里返回 (None, [])，不创建任何监听线程，M1a 行为不变。
            _event_bus, watchers = start_event_watchers(config)
            print(f"Fire Loop agent 已启动，管道 {args.pipe_name}", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            # 控制台处理器兜底失败时（比如没有控制台），这里是最后一道清理。
            server.stop()
            log.log_event("agent_stop_requested", reason="keyboard_interrupt")
    finally:
        stop_event_watchers(watchers)
        if processor is not None:
            try:
                processor.stop(timeout=5.0)
            except Exception as exc:  # 停止失败也不能卡住 agent 退出
                log.log_event(
                    "task_processor_error",
                    level="warning",
                    stage="stop",
                    error=type(exc).__name__,
                )
        instance.release()

    print("Fire Loop agent 已退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
