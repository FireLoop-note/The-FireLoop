"""DeepSeek 聊天补全客户端（标准库 ``urllib``，零第三方依赖）。

- 接口：OpenAI 兼容的 ``POST {base_url}/chat/completions``，``stream=false``；
- 思考：请求体固定带 ``thinking: {"type": "disabled"}``——文件摘要不需要深度思考，
  而且 DeepSeek 默认 thinking 会烧光 output tokens（实测 max_tokens=16 时 content 返回空串）；
- key：``api_key`` 参数优先，否则 ``secrets.load_api_key()``（环境变量 / secrets.json）；
- 安全：key 只放进 ``Authorization`` 请求头，**不进日志、不进异常信息、不进返回值**；
  本模块不写日志，调用方要记录也只能记 model / usage / 耗时，不记 messages 和响应原文；
- 错误：统一抛 ``LLMError``，``code`` 见 ``ERROR_CODES``，``message`` 是简体中文说明。

接口细节和错误码含义见 ``docs/模型接入.md``。
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

from . import secrets

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT_S = 90.0
DEFAULT_MAX_TOKENS = 800

CHAT_PATH = "/chat/completions"

#: 固定关闭 DeepSeek 深度思考。文件摘要不需要思考；而且默认 thinking 模式会把
#: output tokens 烧在 reasoning 上（Lead 实测：max_tokens=16 时 content 为空串，
#: 加 thinking.disabled 后同一请求 content="收到"、completion_tokens=1）。
#: 集中在这一处，请求体别再传顶层 reasoning_effort。
_THINKING_DISABLED = {"type": "disabled"}

#: LLMError.code 的全部合法取值。
ERROR_CODES = frozenset(
    {"no_key", "network", "timeout", "http_error", "api_error", "bad_response"}
)

#: 常见 HTTP 状态码的中文提示（http_error 用）。
_STATUS_HINTS = {
    400: "请求格式错误",
    401: "API key 无效或已失效",
    402: "账户余额不足",
    403: "没有权限（接口或 key 被限制）",
    404: "接口地址不对（检查 base_url）",
    422: "请求参数不合法",
    429: "请求太频繁或额度用尽，稍后再试",
    500: "DeepSeek 服务端错误",
    502: "DeepSeek 网关错误",
    503: "DeepSeek 服务暂时不可用",
    504: "DeepSeek 网关超时",
}

_DEFAULT_STATUS_HINT = "接口返回错误"

_MESSAGES_OK_TEXT = (
    "没有可用的 DeepSeek API key：请设置环境变量 DEEPSEEK_API_KEY，"
    "或在 %LOCALAPPDATA%\\FireLoop\\secrets.json 里配置 deepseek_api_key"
    "（见 docs/模型接入.md）"
)


class LLMError(Exception):
    """LLM 调用失败。

    ``code`` 是机器可读错误码，必须在 ``ERROR_CODES`` 里；
    ``message`` 是给用户看的简体中文说明，**永远不会包含 API key**。
    """

    def __init__(self, code: str, message: str = ""):
        if code not in ERROR_CODES:
            raise ValueError(f"未知的 LLMError code: {code!r}")
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _sanitize(text, *, secret: str = "", limit: int = 200) -> str:
    """把外部文本收拾成能安全塞进异常信息的短句：抹掉 key、压平空白、截断。"""
    out = str(text)
    if secret:
        out = out.replace(secret, "***")
    out = " ".join(out.split())
    if len(out) > limit:
        out = out[:limit] + "…"
    return out


def _safe_read(exc: urllib.error.HTTPError) -> bytes:
    """尽力读 HTTPError 的响应体；读不到就当空，绝不因为读错误体再抛异常。"""
    try:
        return exc.read() or b""
    except Exception:  # noqa: BLE001 - 错误响应体属于"能读就读"，读不到不重要
        return b""


def _endpoint(base_url: str) -> str:
    """拼出 chat/completions 地址，兼容 base_url 带不带尾斜杠。"""
    base = str(base_url).strip().rstrip("/")
    if not base:
        raise ValueError("base_url 不能为空")
    return base + CHAT_PATH


def _resolve_key(api_key) -> str:
    """确定这次请求用的 key：显式参数 > secrets.load_api_key()；都没有抛 no_key。"""
    if isinstance(api_key, str):
        key = api_key.strip()
        if key:
            return key
    key = secrets.load_api_key()
    if key:
        return key
    raise LLMError("no_key", _MESSAGES_OK_TEXT)


def _usage_int(value) -> int:
    """usage 字段容错：只认非负整数，其他（缺失/None/字符串/bool/负数）一律 0。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _parse_usage(raw) -> dict:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if isinstance(raw, dict):
        for name in usage:
            usage[name] = _usage_int(raw.get(name))
    return usage


def _extract_error_detail(body: bytes) -> str:
    """从错误响应体里抠出可读的 error.message / error.code；抠不到返回空串。

    只认 JSON 结构里的错误对象，不把原始 HTML/文本整个倒进异常信息。
    """
    if not body:
        return ""
    try:
        payload = json.loads(body[:4096].decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        for field in ("message", "code"):
            value = error.get(field)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    if isinstance(error, str) and error.strip():
        return error
    return ""


def _http_error_message(status: int, body: bytes, key: str) -> str:
    hint = _STATUS_HINTS.get(status, _DEFAULT_STATUS_HINT)
    message = f"请求失败：HTTP {status}（{hint}）"
    detail = _sanitize(_extract_error_detail(body), secret=key)
    if detail:
        message += f"；接口返回：{detail}"
    return message


def _parse_response(body: bytes, fallback_model: str) -> dict:
    """解析 200 响应；结构不对抛 bad_response。content 允许空串，usage 缺省为 0。"""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LLMError("bad_response", "响应不是合法 UTF-8，无法解析") from exc

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise LLMError(
            "bad_response",
            f"响应不是合法 JSON（{_sanitize(str(exc), limit=120)}）",
        ) from exc

    if not isinstance(payload, dict):
        raise LLMError("bad_response", "响应 JSON 不是对象")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("bad_response", "响应缺少非空的 choices 数组")

    first = choices[0]
    if not isinstance(first, dict):
        raise LLMError("bad_response", "响应 choices[0] 不是对象")

    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMError("bad_response", "响应缺少 choices[0].message 对象")

    if "content" not in message:
        raise LLMError("bad_response", "响应缺少 choices[0].message.content 字段")
    content = message["content"]
    if content is None:  # 个别情况下 content 为 null（如 max_tokens 被推理耗尽）
        content = ""
    if not isinstance(content, str):
        raise LLMError("bad_response", "响应 choices[0].message.content 不是字符串")

    returned_model = payload.get("model")
    if not isinstance(returned_model, str) or not returned_model.strip():
        returned_model = fallback_model

    return {
        "content": content,
        "usage": _parse_usage(payload.get("usage")),
        "model": returned_model.strip(),
    }


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------


def _http_post(
    url: str, headers: dict, body_bytes: bytes, timeout_s: float
) -> tuple[int, bytes]:
    """发一次 HTTP POST，返回 ``(status, body_bytes)``。

    - 4xx/5xx 也按 ``(status, body)`` 返回，不在这里抛 ``HTTPError``，上层统一映射；
    - 连不上 / 超时 / HTTP 协议异常的异常原样往上抛，由 ``chat()`` 翻译成 ``LLMError``。

    这个函数是单测的 mock 接缝（见 tests/test_llm.py），签名不要随便改。
    """
    request = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _safe_read(exc)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def chat(
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    timeout_s: float = 90.0,
    max_tokens: int = 800,
) -> dict:
    """调用 DeepSeek 聊天补全，返回解析后的结果。

    Args:
        messages: OpenAI 兼容的消息列表，原样放进请求体，不做改写。
        model: 模型名，默认 ``DEFAULT_MODEL``。
        base_url: 接口根地址，默认 ``DEFAULT_BASE_URL``；尾斜杠可有可无。
        api_key: 显式 key；None/空串时走 ``secrets.load_api_key()``。
        timeout_s: 单次 HTTP 超时（秒），默认 90，避免挂死。
        max_tokens: 最多生成多少 token，默认 800（成本控制参数之一）。

    Returns:
        ``{"content": str, "usage": {"prompt_tokens": int, "completion_tokens": int,
        "total_tokens": int}, "model": str}``。content 允许空串；usage 缺字段为 0。
        请求体固定带 ``thinking: {"type": "disabled"}``，关闭深度思考（见模块常量
        ``_THINKING_DISABLED``）。

    Raises:
        LLMError: code 见 ``ERROR_CODES``；错误信息里不含 key。
    """
    key = _resolve_key(api_key)
    url = _endpoint(base_url)
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "thinking": dict(_THINKING_DISABLED),
    }
    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }

    try:
        status, body = _http_post(url, headers, body_bytes, timeout_s)
    except urllib.error.HTTPError as exc:
        # 正常走 _http_post 不会到这里；防 _http_post 被替换成直接抛 HTTPError 的实现。
        raise LLMError(
            "http_error", _http_error_message(exc.code, _safe_read(exc), key)
        ) from exc
    except TimeoutError as exc:  # socket.timeout 在 3.10+ 就是 TimeoutError
        raise LLMError(
            "timeout", f"请求超时（{timeout_s:g} 秒），DeepSeek 接口没有按时返回"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LLMError(
                "timeout", f"请求超时（{timeout_s:g} 秒），DeepSeek 接口没有按时返回"
            ) from exc
        raise LLMError(
            "network", f"网络错误，连不上 DeepSeek 接口：{_sanitize(exc.reason, secret=key)}"
        ) from exc
    except http.client.HTTPException as exc:
        raise LLMError(
            "network", f"HTTP 协议错误：{_sanitize(type(exc).__name__, secret=key)}"
        ) from exc
    except OSError as exc:
        raise LLMError(
            "network", f"网络错误：{_sanitize(exc, secret=key)}"
        ) from exc

    if status != 200:
        raise LLMError("http_error", _http_error_message(status, body, key))

    return _parse_response(body, fallback_model=model)
