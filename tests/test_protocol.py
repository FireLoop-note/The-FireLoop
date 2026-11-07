"""协议层单测（标准库 unittest，不依赖 pytest）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fireloop import protocol as p


class EncodeDecodeTest(unittest.TestCase):
    def test_ping_roundtrip(self):
        msg = p.make_ping("a1")
        self.assertEqual(p.decode_line(p.encode(msg)), msg)
        self.assertTrue(p.encode(msg).endswith(b"\n"))

    def test_task_ok(self):
        msg = p.make_task("a2", "打开记事本")
        raw = p.encode(msg)
        self.assertEqual(p.decode_line(raw), msg)
        self.assertLessEqual(len(raw), p.MAX_LINE_BYTES)

    def test_task_empty_rejected(self):
        with self.assertRaises(p.ProtocolError) as ctx:
            p.make_task("a3", "   ")
        self.assertEqual(ctx.exception.code, p.CODE_BAD_MSG)

    def test_task_too_long(self):
        with self.assertRaises(p.ProtocolError) as ctx:
            p.make_task("a4", "x" * (p.MAX_TASK_TEXT + 1))
        self.assertEqual(ctx.exception.code, p.CODE_TOO_LARGE)

    def test_bad_json(self):
        for raw in (b"not json\n", b"{}\n", b"[]\n"):
            with self.assertRaises(p.ProtocolError) as ctx:
                p.decode_line(raw)
            self.assertIn(ctx.exception.code, (p.CODE_BAD_JSON, p.CODE_BAD_MSG))

    def test_missing_version(self):
        with self.assertRaises(p.ProtocolError) as ctx:
            p.decode_line(b'{"type":"ping"}\n')
        self.assertEqual(ctx.exception.code, p.CODE_BAD_MSG)

    def test_wrong_version(self):
        with self.assertRaises(p.ProtocolError) as ctx:
            p.decode_line(b'{"v":2,"type":"ping"}\n')
        self.assertEqual(ctx.exception.code, p.CODE_BAD_MSG)

    def test_line_too_large(self):
        big = b'{"v":1,"type":"task","text":"' + b"x" * p.MAX_LINE_BYTES + b'"}'
        with self.assertRaises(p.ProtocolError) as ctx:
            p.decode_line(big)
        self.assertEqual(ctx.exception.code, p.CODE_TOO_LARGE)

    def test_empty_line(self):
        with self.assertRaises(p.ProtocolError) as ctx:
            p.decode_line(b"  \n")
        self.assertEqual(ctx.exception.code, p.CODE_BAD_JSON)

    def test_error_frame(self):
        msg = p.make_error("a5", p.CODE_BAD_TOKEN, "token 不对")
        self.assertEqual(msg["ok"], False)
        self.assertEqual(p.decode_line(p.encode(msg)), msg)

    def test_ok_frame(self):
        msg = p.make_ok("a6", text="pong")
        self.assertEqual(msg["ok"], True)
        self.assertEqual(p.decode_line(p.encode(msg)), msg)

    def test_text_fingerprint_does_not_leak(self):
        fp = p.text_fingerprint("这是任务原文")
        self.assertEqual(fp["text_len"], 6)
        self.assertNotIn("原文", str(fp))
        self.assertEqual(len(fp["text_sha8"]), 8)


if __name__ == "__main__":
    unittest.main()
