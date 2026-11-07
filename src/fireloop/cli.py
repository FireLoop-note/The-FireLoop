"""Fire Loop CLI：从命令行调一次常驻 agent，然后退出。

用法：
    python -m fireloop.cli ping
    python -m fireloop.cli task "任务文本"
    python -m fireloop.cli file "D:\\资料\\示例.txt"
    python -m fireloop.cli uri "fireloop://task?text=hello"
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

import os
import sys
import urllib.parse
import uuid

from . import client, protocol

EXIT_USAGE = protocol.CLI_EXIT_INTERNAL  # 用法错误用 4（协议里 4 = 内部错误）

# fireloop:// 协议（M1b 注册；这里只负责 CLI 侧解析）
URI_SCHEME = "fireloop"
URI_HOST_TASK = "task"
URI_HOST_FILE = "file"

USAGE = """Fire Loop CLI（火环）

用法:
  python -m fireloop.cli ping
  python -m fireloop.cli task "任务文本"
  python -m fireloop.cli file "D:\\资料\\示例.txt"
  python -m fireloop.cli uri "fireloop://task?text=hello"
  python -m fireloop.cli <命令> --pipe-name "\\\\.\\pipe\\fireloop-implant"

命令:
  ping              探测 agent 是否在运行，成功打印 pong
  task "文本"        把任务文本发给 agent，成功打印服务端回的 text
  file "路径"        把路径作为任务发送：task.text = "file:<绝对路径>"（相对路径转绝对，不检查存在）
  uri "链接"         解析 fireloop:// 链接后发任务（参数值需 URL 编码）：
                       fireloop://task?text=文本   → task.text = 文本（URL 解码）
                       fireloop://file?path=路径   → task.text = "file:路径"（URL 解码）

选项:
  --pipe-name <名>   覆盖默认管道名
  -h, --help         显示本帮助
  --                 后续参数一律当位置参数（参数以 - 开头时用）

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


def _require_single_arg(command: str, positionals: list[str], example: str) -> str:
    """命令后面必须跟且只跟一个参数。"""
    if len(positionals) < 2:
        raise UsageError(f"{command} 缺少参数，例如：{example}")
    if len(positionals) > 2:
        raise UsageError(f"{command} 只接受一个参数；参数含空格时请用引号括起来")
    return positionals[1]


def _decode_uri_value(value: str) -> str:
    """URL 百分号解码。

    只做 %XX 解码，不把 + 当空格（form 编码那套）：Windows 路径里 + 是合法字符，
    真实链接里空格应编码成 %20。install.ps1 生成链接时请用 [uri]::EscapeDataString。
    """
    return urllib.parse.unquote(value, encoding="utf-8", errors="replace")


def _query_params(query: str) -> dict[str, list[str]]:
    """解析 query 字面量（值做百分号解码，+ 保留原样）。重复 key 返回列表。"""
    params: dict[str, list[str]] = {}
    for chunk in query.split("&"):
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        params.setdefault(_decode_uri_value(key), []).append(
            _decode_uri_value(value if sep else "")
        )
    return params


def _first_query_param(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    return values[0] if values else None


def _text_from_uri(uri: str) -> str:
    """fireloop://task?text=xxx / fireloop://file?path=xxx → 要发送的 task.text。

    其他 scheme、其他 host、参数缺失或为空 → UsageError（CLI 退出码 4）。
    """
    if not uri.strip():
        raise UsageError("uri 参数不能为空")

    parts = urllib.parse.urlsplit(uri)
    if parts.scheme.lower() != URI_SCHEME:
        raise UsageError(f"不是 fireloop:// 链接：{uri}")

    host = parts.netloc.lower()
    if host not in (URI_HOST_TASK, URI_HOST_FILE):
        raise UsageError(
            f"不支持的 fireloop:// 目标：{host or '(空)'}（只支持 task / file）"
        )

    params = _query_params(parts.query)
    if host == URI_HOST_TASK:
        text = _first_query_param(params, "text")
        if text is None:
            raise UsageError("fireloop://task 缺少 text 参数")
        if not text.strip():
            raise UsageError("fireloop://task 的 text 不能为空")
        return text

    path = _first_query_param(params, "path")
    if path is None:
        raise UsageError("fireloop://file 缺少 path 参数")
    if not path.strip():
        raise UsageError("fireloop://file 的 path 不能为空")
    return "file:" + path


def _build_message(argv: list[str]) -> tuple[dict, str]:
    """把命令行转成请求帧；返回 (消息, 管道名)。参数错抛 UsageError。"""
    pipe_name, positionals = _parse_args(argv)
    if not positionals:
        raise UsageError("缺少命令（ping / task / file / uri）")

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

    if command == "file":
        raw = _require_single_arg(
            command,
            positionals,
            'python -m fireloop.cli file "D:\\资料\\示例.txt"',
        )
        if not raw.strip():
            raise UsageError("file 路径不能为空")
        return protocol.make_task(uuid.uuid4().hex, "file:" + os.path.abspath(raw)), pipe_name

    if command == "uri":
        raw = _require_single_arg(
            command,
            positionals,
            'python -m fireloop.cli uri "fireloop://task?text=hello"',
        )
        return protocol.make_task(uuid.uuid4().hex, _text_from_uri(raw)), pipe_name

    raise UsageError(f"不认识的命令：{command}（只支持 ping / task / file / uri）")


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
