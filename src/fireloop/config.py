"""Fire Loop 统一配置读取（唯一真源）。

配置文件：``paths.config_path()``（默认 ``%LOCALAPPDATA%\\FireLoop\\config.json``，
测试可用 ``FIRELOOP_HOME`` 覆盖）。

结构：

```
{
  "events": {...},   # 规范化交给 events._normalize_events（默认全关）
  "model":  {...},   # 模型接入参数（默认关闭）
  "ui":     {...}    # 结果窗口等界面开关
}
```

安全默认：文件不存在/损坏时 events 全关、model 关闭，不会联网、不会监听。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from . import log, paths

DEFAULT_MODEL_CONFIG: dict = {
    "enabled": False,
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "timeout_s": 90.0,
    "max_file_chars": 20000,
    "max_output_tokens": 800,
    "max_calls_per_day": 100,
    "file_enabled": True,
}

DEFAULT_UI_CONFIG: dict = {
    "show_result_window": True,
}


def _events_defaults() -> dict:
    """延迟导入 events，避免与 events 的模块级依赖形成循环。"""
    from . import events

    return events.default_config()


def default_config() -> dict:
    """返回一份全新的默认配置（events 全关 + model 关闭）。"""
    config = _events_defaults()
    config["model"] = copy.deepcopy(DEFAULT_MODEL_CONFIG)
    config["ui"] = copy.deepcopy(DEFAULT_UI_CONFIG)
    return config


# ---------------------------------------------------------------------------
# 类型规范化：任何非法值一律回默认，不做猜测
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


def _normalize_model(raw) -> dict:
    config = copy.deepcopy(DEFAULT_MODEL_CONFIG)
    if not isinstance(raw, dict):
        return config
    config["enabled"] = _as_bool(raw.get("enabled"), config["enabled"])
    config["provider"] = _as_str(raw.get("provider"), config["provider"])
    config["base_url"] = _as_str(raw.get("base_url"), config["base_url"]).rstrip("/")
    config["model"] = _as_str(raw.get("model"), config["model"])
    config["timeout_s"] = _as_positive_number(raw.get("timeout_s"), config["timeout_s"])
    config["max_file_chars"] = _as_positive_int(
        raw.get("max_file_chars"), config["max_file_chars"]
    )
    config["max_output_tokens"] = _as_positive_int(
        raw.get("max_output_tokens"), config["max_output_tokens"]
    )
    config["max_calls_per_day"] = _as_positive_int(
        raw.get("max_calls_per_day"), config["max_calls_per_day"]
    )
    config["file_enabled"] = _as_bool(raw.get("file_enabled"), config["file_enabled"])
    return config


def _normalize_ui(raw) -> dict:
    config = copy.deepcopy(DEFAULT_UI_CONFIG)
    if not isinstance(raw, dict):
        return config
    config["show_result_window"] = _as_bool(
        raw.get("show_result_window"), config["show_result_window"]
    )
    return config


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def load_config(path: str | Path | None = None) -> dict:
    """读取并规范化完整配置。

    - 文件不存在：返回默认配置（events 全关、model 关闭）；
    - 文件损坏 / 不是 JSON 对象 / IO 错误：记 ``config_error`` 警告，回默认；
    - 返回值一定是可安全索引的完整 dict（events/model/ui 三段都在）。
    """
    from . import events

    target = Path(path) if path is not None else paths.config_path()
    try:
        # utf-8-sig：兼容带 BOM 的文件（PowerShell 5.1 的 Set-Content -Encoding UTF8
        # 会写 BOM；不带 BOM 也能正常读）。
        text = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return default_config()
    except OSError as exc:
        log.log_event(
            "config_error",
            level="warning",
            path=str(target),
            reason="read_failed",
            error=repr(exc),
        )
        return default_config()

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        log.log_event(
            "config_error",
            level="warning",
            path=str(target),
            reason="bad_json",
            error=str(exc),
        )
        return default_config()

    if not isinstance(raw, dict):
        log.log_event(
            "config_error",
            level="warning",
            path=str(target),
            reason="not_object",
        )
        return default_config()

    config = events._normalize_events(raw.get("events")) if hasattr(events, "_normalize_events") else events.default_config()
    config["model"] = _normalize_model(raw.get("model"))
    config["ui"] = _normalize_ui(raw.get("ui"))
    return config
