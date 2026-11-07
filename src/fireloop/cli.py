"""Fire Loop CLI：从命令行调一次常驻 agent，然后退出。

用法：
    python -m fireloop.cli ping
    python -m fireloop.cli task "任务文本"
    python -m fireloop.cli ping --pipe-name "\\\\.\\pipe\\fireloop-implant"

退出码（docs/协议-v1.md 第 6 节）：
    0  成功
    1  agent 没在运行（agent_offline）
    2  连接 / 响应超时
    3  协议错误（bad_token / bad_json / bad_msg / 服务端返回 ok=false）
    4  用法错误或内部错误

CLI 是短命进程：绝不自己生成 token、绝不常驻、任何输入都不抛裸 traceback。
"""

from __future__ import annotations

import sys
import uuid

from . import client, protocol

EXIT_USAGE = protocol.CLI_EXIT_INTERNAL  # 用法错误用 4（协议里 4 = 内部错误）

USAGE = """Fire Loop CLI（火环）

用法:
  python -m fireloop.cli ping
  python -m fireloop.cli task "任务文本"
  python -m fireloop.cli <命令> --pipe-name "\\\\.\\pipe\\fireloop-implant"

命令:
  ping              探测 agent 是否在运行，成功打印 pong
  task "文本"        把任务文本发给 agent，成功打印服务端回的 text

选项:
  --pipe-name <名>   覆盖默认管道名
  -h, --help         显示本帮助
  --                 后续参数一律当位置参数（任务文本以 - 开头时用）

退出码:
  0 成功 / 1 agent 没在运行 / 2 超时 / 3 协议错误 / 4 用法或内部错误
"""


class UsageError(Exception):
    """命令行参数不合法。"""


class HelpRequested(Exception):
    """请求打印帮助。"""


def _parse_args(argv: list[str]) -> tuple[str, list[str]]:
    """拆出管道名和位置参数。位置参数 = [命令, 参数...]。"""
    pipe_name = protocol.PIPE_NAME
    positionals: list[str] = []
    end_of_options = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not end_of_options and arg == "--":
            end_of_options = True
        elif not end_of_options and arg == "--pipe-name":
            i += 1
            if i >= len(argv) or not argv[i]:
                raise UsageError("--pipe-name 后面缺少管道名")
            pipe_name = argv[i]
        elif not end_of_options and arg.startswith("--pipe-name="):
            value = arg.split("=", 1)[1]
            if not value:
                raise UsageError("--pipe-name= 后面缺少管道名")
            pipe_name = value
        elif not end_of_options and arg in ("-h", "--help"):
            raise HelpRequested()
        elif not end_of_options and arg.startswith("-") and arg != "-":
            raise UsageError(f"不认识的选项：{arg}（放在 -- 后面可当普通文本）")
        else:
            positionals.append(arg)
        i += 1
    return pipe_name, positionals


def _build_message(argv: list[str]) -> tuple[dict, str]:
    """把命令行转成请求帧；返回 (消息, 管道名)。参数错抛 UsageError。"""
    pipe_name, positionals = _parse_args(argv)
    if not positionals:
        raise UsageError("缺少命令（ping 或 task）")

    command = positionals[0]
    if command == "ping":
        if len(positionals) > 1:
            raise UsageError("ping 不接受额外参数")
        return protocol.make_ping(uuid.uuid4().hex), pipe_name

    if command == "task":
        if len(positionals) < 2:
            raise UsageError('task 缺少任务文本，例如：python -m fireloop.cli task "打开记事本"')
        if len(positionals) > 2:
            raise UsageError('任务文本含空格时请用引号括起来，例如：task "帮我看看这个"')
        text = positionals[1]
        if not text.strip():
            raise UsageError("task 文本不能为空")
        return protocol.make_task(uuid.uuid4().hex, text), pipe_name

    raise UsageError(f"不认识的命令：{command}（只支持 ping / task）")


def _print_error(message: str) -> None:
    print(message, file=sys.stderr)


def _print_usage() -> None:
    print(USAGE, file=sys.stderr)


def _print_response(resp: dict) -> None:
    text = resp.get("text")
    if isinstance(text, str) and text:
        print(text)
    else:
        print("ok")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        msg, pipe_name = _build_message(argv)
    except HelpRequested:
        print(USAGE)
        return protocol.CLI_EXIT_OK
    except UsageError as exc:
        _print_error(f"用法错误：{exc}")
        _print_usage()
        return EXIT_USAGE
    except protocol.ProtocolError as exc:
        _print_error(f"参数不符合协议（{exc.code}）：{exc.message}")
        _print_usage()
        return EXIT_USAGE

    try:
        resp = client.call(msg, pipe_name=pipe_name)
    except client.AgentOfflineError as exc:
        _print_error(f"Fire Loop 没在运行（{protocol.CODE_AGENT_OFFLINE}）：{exc}")
        return protocol.CLI_EXIT_OFFLINE
    except client.AgentTimeoutError as exc:
        _print_error(f"等待 Fire Loop 超时（{protocol.CODE_TIMEOUT}）：{exc}")
        return protocol.CLI_EXIT_TIMEOUT
    except client.AgentProtocolError as exc:
        detail = exc.message or "无说明"
        _print_error(f"Fire Loop 返回协议错误（{exc.code}）：{detail}")
        return protocol.CLI_EXIT_PROTOCOL
    except OSError as exc:
        _print_error(f"内部错误：{exc}")
        return protocol.CLI_EXIT_INTERNAL
    except Exception as exc:  # 兜底：任何意外都只打一行，不吓用户
        _print_error(f"内部错误：{type(exc).__name__}: {exc}")
        return protocol.CLI_EXIT_INTERNAL

    _print_response(resp)
    return protocol.CLI_EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
