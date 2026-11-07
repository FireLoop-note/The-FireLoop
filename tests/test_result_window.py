"""result_window 的非 GUI 逻辑单测（标准库 unittest）。

只测：参数解析、文本文件读取/编码、--text 与 --text-file 的优先级、窗口尺寸估算、
退出码（2/3/4/0）、pythonw 下 stdout/stderr 为 None 时的健壮性。
**不弹任何窗口、不碰真实剪贴板**：成功路径一律用 mock 替换 show()。
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fireloop import result_window as rw  # noqa: E402

try:  # TclError 用例需要 tkinter；没有就跳过
    import tkinter as _tk

    _HAS_TK = True
except Exception:  # noqa: BLE001
    _tk = None
    _HAS_TK = False


class ParseArgsTest(unittest.TestCase):
    def test_title_and_text(self):
        self.assertEqual(
            rw.parse_args(["--title", "标题", "--text", "正文"]),
            ("标题", None, "正文"),
        )

    def test_text_file_default_title(self):
        self.assertEqual(
            rw.parse_args(["--text-file", r"D:\结果.txt"]),
            (rw.DEFAULT_TITLE, r"D:\结果.txt", None),
        )

    def test_equals_forms(self):
        self.assertEqual(
            rw.parse_args(["--title=标题", "--text=正文"]),
            ("标题", None, "正文"),
        )

    def test_both_keys_kept_for_resolve(self):
        self.assertEqual(
            rw.parse_args(["--text", "命令行", "--text-file", "a.txt"]),
            (rw.DEFAULT_TITLE, "a.txt", "命令行"),
        )

    def test_option_order_independent(self):
        self.assertEqual(
            rw.parse_args(["--text", "正文", "--title", "标题"]),
            ("标题", None, "正文"),
        )

    def test_default_title(self):
        self.assertEqual(rw.parse_args(["--text", "x"])[0], rw.DEFAULT_TITLE)

    def test_text_value_may_start_with_dash(self):
        self.assertEqual(rw.parse_args(["--text", "-abc"])[2], "-abc")

    def test_empty_equals_values_allowed(self):
        self.assertEqual(rw.parse_args(["--title=", "--text="]), ("", None, ""))

    def test_missing_value_is_usage_error(self):
        for argv in (["--title"], ["--text"], ["--text-file"]):
            with self.assertRaises(rw.UsageError, msg=repr(argv)):
                rw.parse_args(argv)

    def test_unknown_arg_is_usage_error(self):
        for argv in (["--bogus"], ["--bogus=1"], ["位置参数"], ["-x"]):
            with self.assertRaises(rw.UsageError, msg=repr(argv)):
                rw.parse_args(argv)

    def test_help(self):
        for argv in (["-h"], ["--help"], ["--text", "x", "--help"]):
            with self.assertRaises(rw.HelpRequested, msg=repr(argv)):
                rw.parse_args(argv)


class ReadTextFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fireloop-rw-read-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_utf8_chinese(self):
        path = self.tmp / "结果.txt"
        path.write_text("第一行\n第二行 中文\n", encoding="utf-8")
        self.assertEqual(rw.read_text_file(str(path)), "第一行\n第二行 中文\n")

    def test_utf8_with_bom(self):
        path = self.tmp / "bom.txt"
        path.write_text("带 BOM 的中文", encoding="utf-8-sig")
        self.assertEqual(rw.read_text_file(str(path)), "带 BOM 的中文")

    def test_crlf_normalized(self):
        path = self.tmp / "crlf.txt"
        path.write_bytes(b"a\r\nb\r\n")
        self.assertEqual(rw.read_text_file(str(path)), "a\nb\n")

    def test_empty_file(self):
        path = self.tmp / "empty.txt"
        path.write_bytes(b"")
        self.assertEqual(rw.read_text_file(str(path)), "")

    def test_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            rw.read_text_file(str(self.tmp / "不存在.txt"))

    def test_non_utf8_raises_unicode_error(self):
        path = self.tmp / "gbk.txt"
        path.write_bytes("中文".encode("gbk"))
        with self.assertRaises(UnicodeDecodeError):
            rw.read_text_file(str(path))


class ResolveTextTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fireloop-rw-resolve-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_text_only(self):
        self.assertEqual(rw.resolve_text(None, "正文"), "正文")

    def test_text_file_only(self):
        path = self.tmp / "r.txt"
        path.write_text("来自文件", encoding="utf-8")
        self.assertEqual(rw.resolve_text(str(path), None), "来自文件")

    def test_text_file_preferred_over_text(self):
        path = self.tmp / "r.txt"
        path.write_text("来自文件", encoding="utf-8")
        self.assertEqual(rw.resolve_text(str(path), "来自命令行"), "来自文件")

    def test_neither_raises_usage_error(self):
        with self.assertRaises(rw.UsageError):
            rw.resolve_text(None, None)

    def test_empty_text_file_path_raises_usage_error(self):
        with self.assertRaises(rw.UsageError):
            rw.resolve_text("   ", "正文")

    def test_empty_text_is_allowed(self):
        self.assertEqual(rw.resolve_text(None, ""), "")


class EstimateSizeTest(unittest.TestCase):
    def test_short_text_uses_min_size(self):
        self.assertEqual(rw._estimate_size("hi"), (rw.MIN_WIDTH, rw.MIN_HEIGHT))

    def test_long_line_capped_to_max_width(self):
        width, _height = rw._estimate_size("x" * 500)
        self.assertEqual(width, rw.MAX_WIDTH)

    def test_many_lines_capped_to_max_height(self):
        _width, height = rw._estimate_size("\n" * 200)
        self.assertEqual(height, rw.MAX_HEIGHT)

    def test_grows_with_content(self):
        width, _ = rw._estimate_size("x" * 40)  # 320 + 440 = 760
        self.assertGreater(width, rw.MIN_WIDTH)
        _, height = rw._estimate_size("\n".join(["行"] * 20))  # 160 + 480 = 640
        self.assertGreater(height, rw.MIN_HEIGHT)

    def test_always_within_bounds(self):
        for text in ("", "短", "x" * 10000, "\n" * 200, "中文" * 100):
            width, height = rw._estimate_size(text)
            self.assertGreaterEqual(width, rw.MIN_WIDTH, repr(text[:20]))
            self.assertLessEqual(width, rw.MAX_WIDTH, repr(text[:20]))
            self.assertGreaterEqual(height, rw.MIN_HEIGHT, repr(text[:20]))
            self.assertLessEqual(height, rw.MAX_HEIGHT, repr(text[:20]))


class EmitTest(unittest.TestCase):
    def test_emit_survives_none_streams(self):
        """pythonw 下 sys.stdout / sys.stderr 是 None，输出不能炸。"""
        with mock.patch.object(rw.sys, "stdout", None), mock.patch.object(
            rw.sys, "stderr", None
        ):
            rw._emit("一句话")
            rw._emit("另一句", "stdout")

    def test_main_usage_error_survives_none_streams(self):
        with mock.patch.object(rw.sys, "stdout", None), mock.patch.object(
            rw.sys, "stderr", None
        ):
            self.assertEqual(rw.main([]), rw.EXIT_USAGE)


class MainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="fireloop-rw-main-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    # --- 工具 ---

    def run_main(self, argv, show_side_effect=None):
        """跑 main()，show 全程被 mock 掉（绝不弹真窗口）。"""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rw, "show") as show_mock:
            if show_side_effect is not None:
                show_mock.side_effect = show_side_effect
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = rw.main(argv)
        return code, out.getvalue(), err.getvalue(), show_mock

    def write_text(self, name, content, encoding="utf-8"):
        path = self.tmp / name
        path.write_text(content, encoding=encoding)
        return str(path)

    # --- 退出码 0 ---

    def test_help_exit0(self):
        code, out, err, show_mock = self.run_main(["--help"])
        self.assertEqual(code, rw.EXIT_OK)
        self.assertIn("用法", out)
        self.assertIn("--text-file", out)
        self.assertEqual(err, "")
        show_mock.assert_not_called()

    def test_success_with_text_option(self):
        calls = []

        def fake_show(title, text):
            calls.append((title, text))

        code, out, err, show_mock = self.run_main(
            ["--title", "标题", "--text", "正文"], fake_show
        )
        self.assertEqual(code, rw.EXIT_OK)
        self.assertEqual(calls, [("标题", "正文")])
        self.assertEqual(out, "")
        self.assertEqual(err, "")
        show_mock.assert_called_once_with("标题", "正文")

    def test_success_with_text_file(self):
        path = self.write_text("结果.txt", "第一行\n第二行 中文\n")
        calls = []

        code, _out, err, show_mock = self.run_main(
            ["--text-file", path], lambda title, text: calls.append((title, text))
        )
        self.assertEqual(code, rw.EXIT_OK)
        self.assertEqual(calls, [(rw.DEFAULT_TITLE, "第一行\n第二行 中文\n")])
        self.assertEqual(err, "")

    def test_both_prefers_text_file(self):
        path = self.write_text("r.txt", "来自文件")
        calls = []
        code, _out, _err, _show = self.run_main(
            ["--title", "t", "--text", "来自命令行", "--text-file", path],
            lambda title, text: calls.append((title, text)),
        )
        self.assertEqual(code, rw.EXIT_OK)
        self.assertEqual(calls, [("t", "来自文件")])

    # --- 退出码 2：参数 / 输入错误 ---

    def test_no_args_exit2(self):
        code, out, err, show_mock = self.run_main([])
        self.assertEqual(code, rw.EXIT_USAGE)
        self.assertEqual(out, "")
        self.assertIn("用法错误", err)
        show_mock.assert_not_called()

    def test_unknown_arg_exit2(self):
        code, _out, err, show_mock = self.run_main(["--bogus"])
        self.assertEqual(code, rw.EXIT_USAGE)
        self.assertIn("用法错误", err)
        show_mock.assert_not_called()

    def test_missing_option_value_exit2(self):
        for argv in (["--title"], ["--text"], ["--text-file"]):
            code, _out, err, show_mock = self.run_main(argv)
            self.assertEqual(code, rw.EXIT_USAGE, repr(argv))
            self.assertIn("用法错误", err)
            show_mock.assert_not_called()

    def test_missing_text_source_exit2(self):
        code, _out, err, show_mock = self.run_main(["--title", "只有标题"])
        self.assertEqual(code, rw.EXIT_USAGE)
        self.assertIn("用法错误", err)
        self.assertIn("--text", err)
        show_mock.assert_not_called()

    def test_missing_file_exit2(self):
        code, _out, err, show_mock = self.run_main(
            ["--text-file", str(self.tmp / "不存在.txt")]
        )
        self.assertEqual(code, rw.EXIT_USAGE)
        self.assertIn("读取文本失败", err)
        show_mock.assert_not_called()

    def test_non_utf8_file_exit2(self):
        path = self.tmp / "gbk.txt"
        path.write_bytes("中文".encode("gbk"))
        code, _out, err, show_mock = self.run_main(["--text-file", str(path)])
        self.assertEqual(code, rw.EXIT_USAGE)
        self.assertIn("UTF-8", err)
        show_mock.assert_not_called()

    # --- 退出码 3：没有 GUI ---

    @unittest.skipUnless(_HAS_TK, "tkinter 不可用")
    def test_tcl_error_exit3(self):
        code, _out, err, show_mock = self.run_main(
            ["--text", "正文"], _tk.TclError("no display name and no $DISPLAY")
        )
        self.assertEqual(code, rw.EXIT_NO_GUI)
        self.assertIn("图形环境", err)
        show_mock.assert_called_once()

    def test_import_error_exit3(self):
        code, _out, err, _show = self.run_main(
            ["--text", "正文"], ImportError("No module named 'tkinter'")
        )
        self.assertEqual(code, rw.EXIT_NO_GUI)
        self.assertIn("图形环境", err)

    # --- 退出码 4：其他内部错误 ---

    def test_unexpected_error_exit4(self):
        code, _out, err, _show = self.run_main(
            ["--text", "正文"], RuntimeError("窗口炸了")
        )
        self.assertEqual(code, rw.EXIT_INTERNAL)
        self.assertIn("显示窗口失败", err)
        self.assertIn("RuntimeError", err)

    # --- 其他约定 ---

    def test_main_returns_int(self):
        code, _out, _err, _show = self.run_main(["--help"])
        self.assertIsInstance(code, int)

    def test_usage_text_mentions_both_modes(self):
        self.assertIn("--text", rw.USAGE)
        self.assertIn("--text-file", rw.USAGE)


if __name__ == "__main__":
    unittest.main()
