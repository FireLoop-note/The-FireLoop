"""Fire Loop 路径管理。

所有磁盘路径集中从这里取，禁止在别处硬编码。
测试用 FIRELOOP_HOME 覆盖数据根目录，避免污染真实 LOCALAPPDATA 或多实例互踩。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

TOKEN_BYTES = 32  # secrets.token_hex(32) 生成 64 位 hex


def home() -> Path:
    """数据根目录：%LOCALAPPDATA%\\FireLoop，测试时可用 FIRELOOP_HOME 覆盖。"""
    override = os.environ.get("FIRELOOP_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        raise RuntimeError("LOCALAPPDATA 不存在，无法确定 Fire Loop 数据目录")
    return Path(local) / "FireLoop"


def logs_dir() -> Path:
    return home() / "logs"


def run_dir() -> Path:
    return home() / "run"


def token_path() -> Path:
    return home() / "agent.token"


def config_path() -> Path:
    """配置文件路径：%LOCALAPPDATA%\\FireLoop\\config.json。不存在即用默认配置。"""
    return home() / "config.json"


def ensure_dirs() -> None:
    """创建数据目录。幂等。"""
    for d in (home(), logs_dir(), run_dir()):
        d.mkdir(parents=True, exist_ok=True)


def read_token() -> str | None:
    """读取 token；文件不存在或为空返回 None。其他 IO 错误原样抛出。"""
    try:
        text = token_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text or None


def write_token(token: str) -> None:
    ensure_dirs()
    token_path().write_text(token + "\n", encoding="utf-8")


def ensure_token() -> str:
    """读 token；不存在则生成并落盘。agent 和 CLI 都走这个函数。"""
    token = read_token()
    if token:
        return token
    token = secrets.token_hex(TOKEN_BYTES)
    write_token(token)
    return token
