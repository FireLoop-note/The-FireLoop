"""paths/log 单测（标准库 unittest）。用 FIRELOOP_HOME 指向临时目录，不碰真实 LOCALAPPDATA。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fireloop import log, paths


class PathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("FIRELOOP_HOME")
        os.environ["FIRELOOP_HOME"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FIRELOOP_HOME", None)
        else:
            os.environ["FIRELOOP_HOME"] = self._old
        self._tmp.cleanup()

    def test_home_override(self):
        self.assertEqual(paths.home(), Path(self._tmp.name))
        self.assertEqual(paths.token_path(), Path(self._tmp.name) / "agent.token")

    def test_ensure_dirs(self):
        paths.ensure_dirs()
        self.assertTrue(paths.logs_dir().is_dir())
        self.assertTrue(paths.run_dir().is_dir())

    def test_ensure_token_stable(self):
        token1 = paths.ensure_token()
        self.assertEqual(len(token1), 64)
        self.assertEqual(paths.ensure_token(), token1)
        self.assertEqual(paths.read_token(), token1)

    def test_log_jsonl(self):
        log.log_event("test_event", level="debug", answer=42, note="中文")
        files = list(paths.logs_dir().glob("agent-*.jsonl"))
        self.assertEqual(len(files), 1)
        line = files[0].read_text(encoding="utf-8").strip()
        record = json.loads(line)
        self.assertEqual(record["event"], "test_event")
        self.assertEqual(record["level"], "debug")
        self.assertEqual(record["answer"], 42)
        self.assertIn("ts", record)


if __name__ == "__main__":
    unittest.main()
