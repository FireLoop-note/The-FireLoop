"""DeepSeek 客户端 + key 读取单测（标准库 unittest）。

覆盖范围：

- ``secrets.load_api_key``：环境变量优先、临时 ``FIRELOOP_HOME`` 下的
  secrets.json、文件缺失 / 字段缺失 / 空串 / 坏 JSON / 坏 UTF-8 / 读失败，
  以及 warning 日志里不出现 key；
- ``llm.chat``：正常响应解析、请求体与请求头、HTTP 401/429/500、
  超时 / 网络错误、坏 JSON / 缺 choices / 缺 content、无 key、usage 容错、
  base_url 尾斜杠、消息列表原样透传；
- 所有错误信息的 message / repr 都不含假 key 字面量。

安全约定（硬性）：

- 全部用假 key（``sk-test-...``），**绝不联网、绝不读真实 key**：
  ``LLMTest.setUp`` 默认把 ``fireloop.llm._http_post`` 换成"一调就报错"的替身，
  要发请求的用例再显式换成假实现；
- ``DEEPSEEK_API_KEY`` 在 setUp 里摘掉、tearDown 还原；
- ``FIRELOOP_HOME`` 指向临时目录，不碰真实 %LOCALAPPDATA%。

跑法::

    cd D:\\ds植入式
    $env:PYTHONPATH="$PWD\\src"
    D:\\Python\\python.exe -m unittest discover -s tests -p "test_llm.py" -v
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fireloop import llm, paths
from fireloop import secrets as fireloop_secrets

# 假 key：只用于断言"不会泄露"，与真实 key 无关。
FAKE_KEY = "sk-test-0000000000000000000000000000000000000000"
FAKE_KEY_ENV = "sk-test-env-111111111111111111111111111111111111"

_STATUS_OK = 200


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _ok_body(content: str = "你好", model: str = "deepseek-flash") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _no_network(*args, **kwargs):
    """默认网络替身：任何没显式 mock 的 chat 调用都会在这里炸掉。"""
    raise AssertionError("测试禁止真实网络调用：必须先 mock fireloop.llm._http_post")


class FakePost:
    """``_http_post`` 的假实现：记录调用参数，按预置结果返回或抛异常。"""

    def __init__(self, status: int = _STATUS_OK, body: bytes = b"", exc=None):
        self.status = status
        self.body = body
        self.exc = exc
        self.calls: list[dict] = []

    def __call__(self, url, headers, body_bytes, timeout_s):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body_bytes": body_bytes,
                "timeout_s": timeout_s,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.status, self.body

    def request_json(self, index: int = 0) -> dict:
        return json.loads(self.calls[index]["body_bytes"].decode("utf-8"))


class EnvIsolatedTest(unittest.TestCase):
    """临时 FIRELOOP_HOME + 摘掉真实环境变量，保证不碰真实 key / 真实数据目录。"""

    _ENV_NAMES = ("FIRELOOP_HOME", "DEEPSEEK_API_KEY")

    def setUp(self):
        self._saved_env = {name: os.environ.get(name) for name in self._ENV_NAMES}
        os.environ.pop("DEEPSEEK_API_KEY", None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["FIRELOOP_HOME"] = self._tmp.name
        self._patches: list = []

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _patch(self, target, attribute: str, **kwargs):
        patcher = mock.patch.object(target, attribute, **kwargs)
        started = patcher.start()
        self._patches.append(patcher)
        return started


# ---------------------------------------------------------------------------
# LLMError 本身
# ---------------------------------------------------------------------------


class LLMErrorTest(unittest.TestCase):
    def test_error_codes_exact(self):
        self.assertEqual(
            llm.ERROR_CODES,
            {"no_key", "network", "timeout", "http_error", "api_error", "bad_response"},
        )

    def test_error_attributes(self):
        exc = llm.LLMError("api_error", "接口返回业务错误")
        self.assertEqual(exc.code, "api_error")
        self.assertEqual(exc.message, "接口返回业务错误")
        self.assertIn("api_error", str(exc))
        self.assertIn("接口返回业务错误", str(exc))

    def test_unknown_code_rejected(self):
        with self.assertRaises(ValueError):
            llm.LLMError("not_a_code", "x")


# ---------------------------------------------------------------------------
# chat 正常路径
# ---------------------------------------------------------------------------


class ChatSuccessTest(EnvIsolatedTest):
    def setUp(self):
        super().setUp()
        self._net_guard = mock.patch.object(llm, "_http_post", _no_network)
        self._net_guard.start()
        self._patches.append(self._net_guard)

    def _install_post(self, status: int = _STATUS_OK, body: bytes = b"", exc=None) -> FakePost:
        fake = FakePost(status=status, body=body, exc=exc)
        patcher = mock.patch.object(llm, "_http_post", new=fake)
        patcher.start()
        self._patches.append(patcher)
        return fake

    def _assert_no_key(self, exc: BaseException) -> None:
        text = f"{exc}\n{exc!r}\n{getattr(exc, 'message', '')}"
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(FAKE_KEY_ENV, text)

    # -- 正常响应 ---------------------------------------------------------

    def test_normal_response_parsed(self):
        fake = self._install_post(body=_json_bytes(_ok_body()))
        result = llm.chat(
            [{"role": "user", "content": "你好"}], api_key=FAKE_KEY
        )
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(result["content"], "你好")
        self.assertEqual(result["model"], "deepseek-flash")
        self.assertEqual(
            result["usage"],
            {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        )

    def test_empty_content_allowed(self):
        self._install_post(body=_json_bytes(_ok_body(content="")))
        result = llm.chat([{"role": "user", "content": "你好"}], api_key=FAKE_KEY)
        self.assertEqual(result["content"], "")

    def test_null_content_tolerated_as_empty(self):
        payload = _ok_body()
        payload["choices"][0]["message"]["content"] = None
        self._install_post(body=_json_bytes(payload))
        result = llm.chat([{"role": "user", "content": "你好"}], api_key=FAKE_KEY)
        self.assertEqual(result["content"], "")

    def test_model_falls_back_to_request_model(self):
        payload = _ok_body()
        del payload["model"]
        self._install_post(body=_json_bytes(payload))
        result = llm.chat(
            [{"role": "user", "content": "hi"}],
            model="deepseek-chat",
            api_key=FAKE_KEY,
        )
        self.assertEqual(result["model"], "deepseek-chat")

    def test_usage_missing_tolerated(self):
        payload = _ok_body()
        del payload["usage"]
        self._install_post(body=_json_bytes(payload))
        result = llm.chat([{"role": "user", "content": "hi"}], api_key=FAKE_KEY)
        self.assertEqual(
            result["usage"],
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    def test_usage_partial_and_invalid_tolerated(self):
        payload = _ok_body()
        payload["usage"] = {
            "prompt_tokens": 5,
            "completion_tokens": "bad",
            "total_tokens": -3,
        }
        self._install_post(body=_json_bytes(payload))
        result = llm.chat([{"role": "user", "content": "hi"}], api_key=FAKE_KEY)
        self.assertEqual(
            result["usage"],
            {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 0},
        )

    def test_usage_not_dict_tolerated(self):
        payload = _ok_body()
        payload["usage"] = "nope"
        self._install_post(body=_json_bytes(payload))
        result = llm.chat([{"role": "user", "content": "hi"}], api_key=FAKE_KEY)
        self.assertEqual(result["usage"]["total_tokens"], 0)

    # -- 请求本身 ---------------------------------------------------------

    def test_request_body_and_headers(self):
        fake = self._install_post(body=_json_bytes(_ok_body()))
        messages = [
            {"role": "system", "content": "你是火环"},
            {"role": "user", "content": "把这份文件总结一下"},
        ]
        llm.chat(messages, api_key=FAKE_KEY)

        call = fake.calls[0]
        self.assertEqual(call["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(call["timeout_s"], 90.0)
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {FAKE_KEY}")

        body = fake.request_json()
        self.assertEqual(
            set(body), {"model", "messages", "max_tokens", "stream", "thinking"}
        )
        self.assertEqual(body["model"], llm.DEFAULT_MODEL)
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["max_tokens"], 800)
        self.assertIs(body["stream"], False)
        # 固定关闭深度思考：默认 thinking 会把 output tokens 烧在 reasoning 上，
        # 实测 max_tokens=16 时 content 返回空串；顶层也不传 reasoning_effort。
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("reasoning", body)
        # key 只在 Authorization 头里，不进请求体
        self.assertNotIn(FAKE_KEY.encode("utf-8"), call["body_bytes"])

    def test_thinking_always_disabled(self):
        # 无论模型/输出长度怎么换，请求体都必须带 thinking.disabled。
        for kwargs in (
            {},
            {"model": "deepseek-chat", "max_tokens": 16},
            {"base_url": "https://example.test/v1", "timeout_s": 5.0},
        ):
            with self.subTest(kwargs=kwargs):
                fake = self._install_post(body=_json_bytes(_ok_body()))
                llm.chat(
                    [{"role": "user", "content": "hi"}],
                    api_key=FAKE_KEY,
                    **kwargs,
                )
                body = fake.request_json()
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertNotIn("reasoning_effort", body)

    def test_custom_model_base_url_timeout_max_tokens(self):
        fake = self._install_post(body=_json_bytes(_ok_body()))
        llm.chat(
            [{"role": "user", "content": "hi"}],
            model="deepseek-chat",
            base_url="https://example.test/v1",
            api_key=FAKE_KEY,
            timeout_s=1.5,
            max_tokens=42,
        )
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(call["timeout_s"], 1.5)
        body = fake.request_json()
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["max_tokens"], 42)

    def test_base_url_trailing_slash(self):
        for base_url in (
            "https://api.deepseek.com/",
            "https://api.deepseek.com//",
        ):
            with self.subTest(base_url=base_url):
                fake = self._install_post(body=_json_bytes(_ok_body()))
                llm.chat(
                    [{"role": "user", "content": "hi"}],
                    base_url=base_url,
                    api_key=FAKE_KEY,
                )
                self.assertEqual(
                    fake.calls[0]["url"],
                    "https://api.deepseek.com/chat/completions",
                )

    # -- key 解析 ---------------------------------------------------------

    def test_explicit_key_does_not_touch_secrets(self):
        fake = self._install_post(body=_json_bytes(_ok_body()))
        loader = self._patch(fireloop_secrets, "load_api_key", return_value=FAKE_KEY_ENV)
        llm.chat([{"role": "user", "content": "hi"}], api_key=FAKE_KEY)
        loader.assert_not_called()
        self.assertEqual(
            fake.calls[0]["headers"]["Authorization"], f"Bearer {FAKE_KEY}"
        )

    def test_blank_key_falls_back_to_secrets(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                fake = self._install_post(body=_json_bytes(_ok_body()))
                loader = self._patch(
                    fireloop_secrets, "load_api_key", return_value=FAKE_KEY_ENV
                )
                llm.chat([{"role": "user", "content": "hi"}], api_key=blank)
                loader.assert_called_once_with()
                self.assertEqual(
                    fake.calls[0]["headers"]["Authorization"],
                    f"Bearer {FAKE_KEY_ENV}",
                )

    def test_missing_key_raises_no_key(self):
        self._patch(fireloop_secrets, "load_api_key", return_value=None)
        with self.assertRaises(llm.LLMError) as ctx:
            llm.chat([{"role": "user", "content": "hi"}])
        exc = ctx.exception
        self.assertEqual(exc.code, "no_key")
        self.assertIn("DEEPSEEK_API_KEY", exc.message)
        self.assertIn("secrets.json", exc.message)
        self._assert_no_key(exc)

    def test_missing_key_does_not_send_request(self):
        # 默认 _http_post 替身一调就 AssertionError；no_key 必须在发请求之前抛。
        self._patch(fireloop_secrets, "load_api_key", return_value=None)
        with self.assertRaises(llm.LLMError):
            llm.chat([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# chat 错误路径
# ---------------------------------------------------------------------------


class ChatErrorTest(EnvIsolatedTest):
    def setUp(self):
        super().setUp()
        self._net_guard = mock.patch.object(llm, "_http_post", _no_network)
        self._net_guard.start()
        self._patches.append(self._net_guard)

    def _install_post(self, status: int = _STATUS_OK, body: bytes = b"", exc=None) -> FakePost:
        fake = FakePost(status=status, body=body, exc=exc)
        patcher = mock.patch.object(llm, "_http_post", new=fake)
        patcher.start()
        self._patches.append(patcher)
        return fake

    def _assert_no_key(self, exc: BaseException) -> None:
        text = f"{exc}\n{exc!r}\n{getattr(exc, 'message', '')}"
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(FAKE_KEY_ENV, text)

    def _expect_error(self, expected_code: str, *, status=_STATUS_OK, body=b"", exc=None):
        self._install_post(status=status, body=body, exc=exc)
        with self.assertRaises(llm.LLMError) as ctx:
            llm.chat([{"role": "user", "content": "hi"}], api_key=FAKE_KEY)
        self.assertEqual(ctx.exception.code, expected_code)
        self.assertTrue(ctx.exception.message)
        self._assert_no_key(ctx.exception)
        return ctx.exception

    # -- HTTP 错误 --------------------------------------------------------

    def test_http_401(self):
        body = _json_bytes(
            {"error": {"message": "Authentication Fails", "type": "auth"}}
        )
        exc = self._expect_error("http_error", status=401, body=body)
        self.assertIn("401", exc.message)
        self.assertIn("API key", exc.message)
        self.assertIn("Authentication Fails", exc.message)

    def test_http_429_and_500(self):
        for status, hint in ((429, "请求太频繁"), (500, "服务端错误")):
            with self.subTest(status=status):
                exc = self._expect_error(
                    "http_error",
                    status=status,
                    body=_json_bytes({"error": {"message": "server said no"}}),
                )
                self.assertIn(str(status), exc.message)
                self.assertIn(hint, exc.message)

    def test_http_error_detail_redacts_key(self):
        # 万一接口把 key 回显进错误信息，也必须在进 LLMError 前抹掉。
        body = _json_bytes({"error": {"message": f"bad key {FAKE_KEY} rejected"}})
        exc = self._expect_error("http_error", status=400, body=body)
        self.assertIn("***", exc.message)
        self.assertNotIn(FAKE_KEY, exc.message)

    def test_http_error_non_json_body_does_not_crash(self):
        exc = self._expect_error(
            "http_error", status=502, body=b"<html>Bad Gateway</html>"
        )
        self.assertIn("502", exc.message)

    def test_http_error_raised_directly_is_mapped(self):
        # 防 _http_post 被换成直接抛 HTTPError 的实现。
        err = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(_json_bytes({"error": {"message": "busy"}})),
        )
        exc = self._expect_error("http_error", exc=err)
        self.assertIn("503", exc.message)
        self.assertIn("busy", exc.message)

    # -- 网络 / 超时 ------------------------------------------------------

    def test_timeout_socket(self):
        exc = self._expect_error("timeout", exc=socket.timeout("timed out"))
        self.assertIn("超时", exc.message)

    def test_timeout_wrapped_in_urlerror(self):
        exc = self._expect_error(
            "timeout", exc=urllib.error.URLError(socket.timeout("timed out"))
        )
        self.assertIn("超时", exc.message)

    def test_network_urlerror(self):
        exc = self._expect_error(
            "network", exc=urllib.error.URLError("connection refused")
        )
        self.assertIn("网络", exc.message)

    def test_network_oserror(self):
        exc = self._expect_error("network", exc=ConnectionResetError("reset"))
        self.assertIn("网络", exc.message)

    # -- 响应结构错误 -----------------------------------------------------

    def test_bad_json(self):
        exc = self._expect_error("bad_response", body=b"not json at all")
        self.assertIn("JSON", exc.message)

    def test_non_utf8_response(self):
        self._expect_error("bad_response", body=b"\xff\xfe{}")

    def test_response_not_object(self):
        self._expect_error("bad_response", body=b"[1, 2, 3]")

    def test_missing_choices(self):
        for payload in ({}, {"choices": []}, {"choices": "x"}):
            with self.subTest(payload=payload):
                self._expect_error("bad_response", body=_json_bytes(payload))

    def test_missing_message_or_content(self):
        payloads = (
            {"choices": [{"index": 0}]},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": 123}}]},
            {"choices": [{"message": {"content": ["x"]}}]},
            "not-a-dict",
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self._expect_error("bad_response", body=_json_bytes(payload))


# ---------------------------------------------------------------------------
# secrets.load_api_key（临时 FIRELOOP_HOME，不碰真实 secrets.json）
# ---------------------------------------------------------------------------


class SecretsLoadTest(EnvIsolatedTest):
    def _write_secrets(self, payload) -> Path:
        path = fireloop_secrets.secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _log_records(self) -> list[dict]:
        logs_dir = paths.logs_dir()
        if not logs_dir.is_dir():
            return []
        records: list[dict] = []
        for path in sorted(logs_dir.glob("agent-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        return records

    def _log_text(self) -> str:
        logs_dir = paths.logs_dir()
        if not logs_dir.is_dir():
            return ""
        return "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(logs_dir.glob("agent-*.jsonl"))
        )

    def _assert_no_key_anywhere(self) -> None:
        text = self._log_text()
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(FAKE_KEY_ENV, text)
        for record in self._log_records():
            dumped = json.dumps(record, ensure_ascii=False)
            self.assertNotIn(FAKE_KEY, dumped)
            self.assertNotIn(FAKE_KEY_ENV, dumped)

    # -- 路径与来源优先级 -------------------------------------------------

    def test_path_follows_fireloop_home(self):
        self.assertEqual(
            fireloop_secrets.secrets_path(),
            Path(self._tmp.name) / "secrets.json",
        )

    def test_missing_file_returns_none(self):
        self.assertIsNone(fireloop_secrets.load_api_key())
        self.assertEqual(self._log_records(), [])

    def test_env_var_wins_over_file(self):
        self._write_secrets({"deepseek_api_key": FAKE_KEY})
        os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY_ENV
        self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY_ENV)

    def test_env_var_short_circuits_file_read(self):
        os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY_ENV
        self._patch(
            Path,
            "read_text",
            new=mock.MagicMock(side_effect=AssertionError("环境变量存在时不应读文件")),
        )
        self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY_ENV)

    def test_env_var_stripped(self):
        os.environ["DEEPSEEK_API_KEY"] = f"  {FAKE_KEY_ENV}  "
        self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY_ENV)

    def test_blank_env_var_falls_back_to_file(self):
        self._write_secrets({"deepseek_api_key": FAKE_KEY})
        for blank in ("", "   "):
            with self.subTest(blank=blank):
                os.environ["DEEPSEEK_API_KEY"] = blank
                self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY)

    def test_file_key_used(self):
        self._write_secrets({"deepseek_api_key": FAKE_KEY})
        self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY)

    def test_file_key_stripped(self):
        self._write_secrets({"deepseek_api_key": f"  {FAKE_KEY}  "})
        self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY)

    def test_utf8_bom_file_supported(self):
        # Windows PowerShell 5.1 的 Set-Content/Out-File 会给 UTF-8 文件加 BOM；
        # 本机真实 secrets.json 就是带 BOM 的，必须能读。
        path = fireloop_secrets.secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"deepseek_api_key": FAKE_KEY}, ensure_ascii=False).encode("utf-8")
        path.write_bytes(b"\xef\xbb\xbf" + payload + b"\n")
        self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY)

    # -- 没有 key / 坏内容 ------------------------------------------------

    def test_missing_field_returns_none(self):
        self._write_secrets({"other_field": FAKE_KEY})
        self.assertIsNone(fireloop_secrets.load_api_key())

    def test_empty_field_returns_none(self):
        for value in ("", "   ", None, 123, [], {}):
            with self.subTest(value=value):
                self._write_secrets({"deepseek_api_key": value})
                self.assertIsNone(fireloop_secrets.load_api_key())

    def test_bad_json_returns_none_and_does_not_leak_key(self):
        path = fireloop_secrets.secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 故意写坏 JSON，让文件里带着假 key 的原文
        path.write_text(
            '{"deepseek_api_key": "' + FAKE_KEY + '", oops}',
            encoding="utf-8",
        )
        self.assertIsNone(fireloop_secrets.load_api_key())
        records = self._log_records()
        self.assertTrue(records)
        self.assertEqual(records[-1]["event"], "secrets_error")
        self.assertEqual(records[-1]["reason"], "bad_json")
        self._assert_no_key_anywhere()

    def test_not_object_returns_none(self):
        path = fireloop_secrets.secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(fireloop_secrets.load_api_key())
        records = self._log_records()
        self.assertEqual(records[-1]["reason"], "not_object")

    def test_bad_utf8_returns_none(self):
        path = fireloop_secrets.secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"deepseek_api_key": "\xff\xfe"}')
        self.assertIsNone(fireloop_secrets.load_api_key())
        records = self._log_records()
        self.assertEqual(records[-1]["reason"], "decode_failed")

    def test_read_failure_returns_none(self):
        self._write_secrets({"deepseek_api_key": FAKE_KEY})
        broken = mock.MagicMock(side_effect=OSError("denied"))
        with mock.patch.object(Path, "read_text", new=broken):
            self.assertIsNone(fireloop_secrets.load_api_key())
        records = self._log_records()
        self.assertEqual(records[-1]["reason"], "read_failed")
        self.assertEqual(records[-1]["error"], "OSError")
        self._assert_no_key_anywhere()

    def test_success_logs_nothing(self):
        self._write_secrets({"deepseek_api_key": FAKE_KEY})
        self.assertEqual(fireloop_secrets.load_api_key(), FAKE_KEY)
        self.assertEqual(self._log_records(), [])


if __name__ == "__main__":
    unittest.main()
