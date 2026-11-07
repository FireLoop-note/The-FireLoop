"""config.py 单测（标准库 unittest，FIRELOOP_HOME 临时目录隔离）。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fireloop import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("FIRELOOP_HOME")
        os.environ["FIRELOOP_HOME"] = self._tmp.name
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FIRELOOP_HOME", None)
        else:
            os.environ["FIRELOOP_HOME"] = self._old
        self._tmp.cleanup()

    def write(self, obj):
        self.path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def test_missing_file_defaults_safe(self):
        cfg = config.load_config()
        self.assertEqual(cfg["events"]["enabled"], False)
        self.assertEqual(cfg["model"]["enabled"], False)
        self.assertEqual(cfg["model"]["model"], "deepseek-flash")
        self.assertEqual(cfg["ui"]["show_result_window"], True)

    def test_full_config(self):
        self.write(
            {
                "events": {"enabled": True, "file": {"watch_dirs": ["D:\\watch"]}},
                "model": {
                    "enabled": True,
                    "base_url": "http://127.0.0.1:9999/",
                    "model": "deepseek-flash",
                    "max_file_chars": 500,
                    "max_calls_per_day": 3,
                },
                "ui": {"show_result_window": False},
            }
        )
        cfg = config.load_config()
        self.assertTrue(cfg["events"]["enabled"])
        self.assertEqual(cfg["events"]["file"]["watch_dirs"], ["D:\\watch"])
        self.assertTrue(cfg["model"]["enabled"])
        self.assertEqual(cfg["model"]["base_url"], "http://127.0.0.1:9999")
        self.assertEqual(cfg["model"]["max_file_chars"], 500)
        self.assertEqual(cfg["model"]["max_calls_per_day"], 3)
        self.assertFalse(cfg["ui"]["show_result_window"])

    def test_bom_config_read_ok(self):
        """PowerShell 5.1 写出的 UTF-8 BOM 文件必须能读（真实踩过的坑）。"""
        payload = {"model": {"enabled": True, "model": "deepseek-flash"}}
        self.path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        cfg = config.load_config()
        self.assertTrue(cfg["model"]["enabled"])
        self.assertEqual(cfg["model"]["model"], "deepseek-flash")

    def test_bad_json_defaults(self):
        self.path.write_text("{not json", encoding="utf-8")
        cfg = config.load_config()
        self.assertEqual(cfg["events"]["enabled"], False)
        self.assertEqual(cfg["model"]["enabled"], False)

    def test_wrong_types_fall_back(self):
        self.write(
            {
                "model": {
                    "enabled": "yes",
                    "timeout_s": -1,
                    "max_file_chars": 0,
                    "max_calls_per_day": True,
                    "base_url": "",
                }
            }
        )
        cfg = config.load_config()
        model = cfg["model"]
        self.assertEqual(model["enabled"], False)
        self.assertEqual(model["timeout_s"], 90.0)
        self.assertEqual(model["max_file_chars"], 20000)
        self.assertEqual(model["max_calls_per_day"], 100)
        self.assertEqual(model["base_url"], "https://api.deepseek.com")

    def test_not_object_defaults(self):
        self.write([1, 2, 3])
        cfg = config.load_config()
        self.assertEqual(cfg["model"]["enabled"], False)

    def test_unknown_fields_ignored(self):
        self.write({"model": {"enabled": True, "nonsense": 1}, "extra": "x"})
        cfg = config.load_config()
        self.assertTrue(cfg["model"]["enabled"])
        self.assertNotIn("nonsense", cfg["model"])
        self.assertNotIn("extra", cfg)


if __name__ == "__main__":
    unittest.main()
