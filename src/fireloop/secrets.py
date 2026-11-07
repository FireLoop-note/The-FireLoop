"""Fire Loop 的 DeepSeek API key 读取。

安全红线（改这个文件前先读）：

- **绝不**把 key 打印到终端、写进日志、写进异常信息、写进文档或测试；
- 本模块只把 key 作为返回值交给调用方，自身不产生任何含 key 的输出；
- 读失败一律返回 None，不抛异常；warning 日志只记原因、路径和错误类型，
  不记文件内容，也不记 key 的任何片段。

key 来源优先级（高 → 低）：

1. 环境变量 ``DEEPSEEK_API_KEY``；
2. ``%LOCALAPPDATA%\\FireLoop\\secrets.json`` 的 ``deepseek_api_key`` 字段
   （测试可用 ``FIRELOOP_HOME`` 覆盖数据目录，见 ``paths.home()``）。

文件不存在 / 没有该字段 / 值为空串，都视为"没有 key"，返回 None。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import log, paths

ENV_VAR = "DEEPSEEK_API_KEY"
SECRETS_FILENAME = "secrets.json"
KEY_FIELD = "deepseek_api_key"


def secrets_path() -> Path:
    """secrets.json 路径：跟着 paths.home() 走，不写死绝对路径。"""
    return paths.home() / SECRETS_FILENAME


def _clean_key(value) -> str | None:
    """把候选值收拾成非空 key；非字符串 / 空串 / 纯空白一律 None。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _warn(reason: str, **fields) -> None:
    """记一条不含 key 的 warning；日志自身出错也不能影响读 key。"""
    try:
        log.log_event("secrets_error", level="warning", reason=reason, **fields)
    except Exception:  # noqa: BLE001 - 日志失败必须静默，不能把主流程带崩
        pass


def load_api_key() -> str | None:
    """读取 DeepSeek API key；没有就返回 None（任何读取失败都不抛异常）。

    环境变量优先；环境变量是空串 / 纯空白视为没设置，继续看文件。
    返回的字符串已 strip，调用方不要再把这个值写进任何日志或错误信息。
    """
    from_env = _clean_key(os.environ.get(ENV_VAR, ""))
    if from_env is not None:
        return from_env

    try:
        target = secrets_path()
    except Exception:  # noqa: BLE001 - 极端环境（如 LOCALAPPDATA 缺失）也按没有 key 处理
        return None

    try:
        # utf-8-sig：兼容 Windows PowerShell 5.1 写出的 UTF-8 BOM 文件，
        # 没 BOM 的普通 UTF-8 也照常读（本机真实 secrets.json 就是带 BOM 的）。
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        _warn("decode_failed", path=str(target))
        return None
    except OSError as exc:
        _warn("read_failed", path=str(target), error=type(exc).__name__)
        return None

    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError 的文本只有行列号，不含原文；这里只额外记行列号。
        _warn(
            "bad_json",
            path=str(target),
            line=getattr(exc, "lineno", None),
            col=getattr(exc, "colno", None),
        )
        return None

    if not isinstance(raw, dict):
        _warn("not_object", path=str(target))
        return None

    return _clean_key(raw.get(KEY_FIELD))
