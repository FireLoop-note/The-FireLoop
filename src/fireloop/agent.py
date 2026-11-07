"""Fire Loop 常驻 agent 入口：``python -m fireloop.agent``。

启动流程（见 docs/fire-loop-方案.md）：

1. ``paths.ensure_dirs()`` 建数据目录；
2. ``paths.ensure_token()`` 读/生成 token；
3. ``SingleInstance`` 抢 ``Local\\FireLoopAgent`` 互斥体，抢不到说明已有实例，退出码 1；
4. 起 ``PipeServer`` 无限 accept。

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

from . import log, paths, protocol
from .pipe_server import PipeServer
from .single_instance import SingleInstance

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
        description="Fire Loop 常驻 agent（M1a：命名管道 + JSON Lines，不接模型）",
    )
    parser.add_argument(
        "--pipe-name",
        default=protocol.PIPE_NAME,
        help=f"命名管道全名，默认 {protocol.PIPE_NAME}",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    paths.ensure_dirs()
    token = paths.ensure_token()

    instance = SingleInstance()
    if not instance.acquire():
        print("Fire Loop agent 已经在运行", flush=True)
        return 1

    server = None
    try:
        server = PipeServer(pipe_name=args.pipe_name, token=token)
        try:
            _install_console_handler(server)
            print(f"Fire Loop agent 已启动，管道 {args.pipe_name}", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            # 控制台处理器兜底失败时（比如没有控制台），这里是最后一道清理。
            server.stop()
            log.log_event("agent_stop_requested", reason="keyboard_interrupt")
    finally:
        instance.release()

    print("Fire Loop agent 已退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
