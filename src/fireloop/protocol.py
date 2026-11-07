"""Fire Loop 管道协议 v1 的消息定义与编解码。

线格式见 docs/协议-v1.md。
纯逻辑模块：不碰 Win32、不做 IO，任何进程可 import 并单测。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

PROTOCOL_VERSION = 1

PIPE_NAME = r"\\.\pipe\fireloop-implant"
MAX_LINE_BYTES = 64 * 1024
MAX_TASK_TEXT = 4096

CONNECT_TIMEOUT_MS = 2000
HELLO_TIMEOUT_MS = 2000
REQUEST_TIMEOUT_MS = 5000

CLI_EXIT_OK = 0
CLI_EXIT_OFFLINE = 1
CLI_EXIT_TIMEOUT = 2
CLI_EXIT_PROTOCOL = 3
CLI_EXIT_INTERNAL = 4

# 消息 type
HELLO = "hello"
HELLO_OK = "hello_ok"
PING = "ping"
TASK = "task"
RESULT = "result"

# 错误码（docs/协议-v1.md 第 5 节）
CODE_AGENT_OFFLINE = "agent_offline"
CODE_BAD_TOKEN = "bad_token"
CODE_BAD_JSON = "bad_json"
CODE_BAD_MSG = "bad_msg"
CODE_TOO_LARGE = "too_large"
CODE_UNKNOWN_TYPE = "unknown_type"
CODE_TIMEOUT = "timeout"
CODE_INTERNAL = "internal"
CODE_UNAUTHORIZED = "unauthorized"

ALLOWED_TYPES = frozenset({HELLO, HELLO_OK, PING, TASK, RESULT})


class ProtocolError(Exception):
    """协议层错误。code 必须来自 docs/协议-v1.md 的错误码表。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def _check_message(msg: Any) -> dict:
    """校验通用字段：必须是 dict，v 必须匹配，type 必须是非空字符串。"""
    if not isinstance(msg, dict):
        raise ProtocolError(CODE_BAD_MSG, "消息必须是 JSON 对象")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError(CODE_BAD_MSG, f"不支持的协议版本: {msg.get('v')!r}")
    mtype = msg.get("type")
    if not isinstance(mtype, str) or not mtype:
        raise ProtocolError(CODE_BAD_MSG, "缺少 type")
    return msg


def encode(msg: dict) -> bytes:
    """dict → 一行 UTF-8 bytes（末尾带 \\n）。超限抛 too_large。"""
    raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_LINE_BYTES:
        raise ProtocolError(CODE_TOO_LARGE, f"单行 {len(raw)} 字节，超过上限 {MAX_LINE_BYTES}")
    return raw


def decode_line(line: bytes | str) -> dict:
    """一行 → dict。非法 JSON 抛 bad_json，结构不对抛 bad_msg，超长抛 too_large。"""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ProtocolError(CODE_TOO_LARGE, f"单行 {len(line)} 字节，超过上限 {MAX_LINE_BYTES}")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(CODE_BAD_JSON, "不是合法 UTF-8") from exc
    else:
        text = line
    text = text.strip()
    if not text:
        raise ProtocolError(CODE_BAD_JSON, "空消息")
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(CODE_BAD_JSON, str(exc)) from exc
    return _check_message(msg)


def make_hello(token: str) -> dict:
    if not isinstance(token, str) or not token:
        raise ProtocolError(CODE_BAD_TOKEN, "token 为空")
    return {"v": PROTOCOL_VERSION, "type": HELLO, "token": token}


def make_hello_ok(agent: str, version: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": HELLO_OK, "agent": agent, "version": version}


def make_ping(req_id: str) -> dict:
    return {"v": PROTOCOL_VERSION, "id": req_id, "type": PING}


def make_task(req_id: str, text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError(CODE_BAD_MSG, "task.text 不能为空")
    if len(text) > MAX_TASK_TEXT:
        raise ProtocolError(CODE_TOO_LARGE, f"task.text 超过 {MAX_TASK_TEXT} 字符")
    return {"v": PROTOCOL_VERSION, "id": req_id, "type": TASK, "text": text}


def make_ok(req_id: str | None, **fields) -> dict:
    msg: dict = {"v": PROTOCOL_VERSION, "type": RESULT, "ok": True}
    if req_id is not None:
        msg["id"] = req_id
    msg.update(fields)
    return msg


def make_error(req_id: str | None, code: str, message: str = "") -> dict:
    msg: dict = {"v": PROTOCOL_VERSION, "type": RESULT, "ok": False, "code": code}
    if req_id is not None:
        msg["id"] = req_id
    if message:
        msg["message"] = message
    return msg


def text_fingerprint(text: str) -> dict:
    """日志用指纹：不记原文，只记长度和 sha256 前 8 位。"""
    return {
        "text_len": len(text),
        "text_sha8": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
    }
