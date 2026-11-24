"""公开快照的离线回归测试，只使用仓库随附的配置模板。"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from PIL import Image

from livedeck.stream_ws import serve
from scripts import build_runtime, preflight, run_bot


class SourceSmokeTests(unittest.TestCase):
    def test_runtime_dependencies(self):
        report = preflight.Report(root=str(preflight.ROOT))
        preflight.check_dependencies(report)
        self.assertEqual(report.failures, [])
        # 验证实际使用的 Pillow 原生扩展和直播流 WebSocket 入口。
        with Image.new("RGB", (1, 1), "white") as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertTrue(callable(serve))

    def test_subjects_template_loads(self):
        subjects, poll = run_bot.load_subjects(run_bot.ROOT / "config/subjects.json.example")
        self.assertTrue(subjects)
        self.assertTrue(all(subject.uid > 0 for subject in subjects))
        self.assertIn(poll["mode"], ("auto", "feed", "per_user"))

    def test_run_bot_help_uses_utf8_when_redirected(self):
        result = subprocess.run(
            [sys.executable, "scripts/run_bot.py", "--help"],
            cwd=run_bot.ROOT,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        )
        self.assertIn("动态取数方式", result.stdout)

    def test_runtime_check_does_not_build(self):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            patch.object(build_runtime, "cmd_build") as build,
            patch.object(build_runtime, "check", return_value=False) as check,
        ):
            self.assertEqual(build_runtime.main(["--check"]), 1)
            self.assertEqual(build_runtime.main(["check"]), 1)
        self.assertEqual(check.call_count, 2)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
