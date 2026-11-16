"""Tests for application logging setup."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LoggerTestCase(unittest.TestCase):
    def test_file_logging_works_with_existing_root_handlers(self):
        project_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as log_dir, tempfile.TemporaryDirectory() as debug_dir:
            env = os.environ.copy()
            env["EDFC_LOG_DIR"] = log_dir
            env["EDFC_DEBUG_DIR"] = debug_dir
            (Path(log_dir) / "autopilot.log").write_text("previous normal", encoding="utf-8")
            (Path(debug_dir) / "debug.log").write_text("previous debug", encoding="utf-8")
            script = (
                "import logging, sys\n"
                f"sys.path.insert(0, {str(project_root / 'src')!r})\n"
                "logging.getLogger().addHandler(logging.NullHandler())\n"
                "from core.logger import logger, set_log_level\n"
                "set_log_level(logging.DEBUG)\n"
                "logger.debug('file logger debug regression marker')\n"
                "logger.warning('file logger warning regression marker')\n"
                "set_log_level(logging.INFO)\n"
                "logger.info('after debug marker')\n"
                "for handler in logging.getLogger().handlers + logger.handlers:\n"
                "    handler.flush()\n"
            )
            subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
            )

            log_contents = (Path(log_dir) / "autopilot.log").read_text(encoding="utf-8")
            debug_contents = (Path(debug_dir) / "debug.log").read_text(encoding="utf-8")
            self.assertEqual(len(list(Path(log_dir).glob("autopilot *.log"))), 1)
            self.assertEqual(len(list(Path(debug_dir).glob("debug *.log"))), 1)
            self.assertFalse(list(Path(log_dir).glob("debug*.log")))

        self.assertNotIn("file logger debug regression marker", log_contents)
        self.assertIn("file logger warning regression marker", log_contents)
        self.assertIn("file logger debug regression marker", debug_contents)
        self.assertIn("file logger warning regression marker", debug_contents)
        self.assertNotIn("after debug marker", debug_contents)
        self.assertIn("after debug marker", log_contents)

    def test_debug_file_is_not_created_until_debug_is_enabled(self):
        project_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as log_dir, tempfile.TemporaryDirectory() as debug_dir:
            env = os.environ.copy()
            env["EDFC_LOG_DIR"] = log_dir
            env["EDFC_DEBUG_DIR"] = debug_dir
            script = (
                "import logging, sys\n"
                f"sys.path.insert(0, {str(project_root / 'src')!r})\n"
                "from core.logger import logger, set_log_level\n"
                "set_log_level(logging.INFO, 'at startup')\n"
                "logger.info('normal marker')\n"
            )
            subprocess.run([sys.executable, "-c", script], check=True, cwd=project_root, env=env)
            self.assertFalse((Path(debug_dir) / "debug.log").exists())

    def test_legacy_log_overrides_are_ignored(self):
        project_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            paths_file = root / "src" / "core" / "paths.py"
            paths_file.parent.mkdir(parents=True)
            shutil.copyfile(project_root / "src" / "core" / "paths.py", paths_file)
            env = os.environ.copy()
            env.pop("EDFC_LOG_DIR", None)
            env.pop("EDFC_DEBUG_DIR", None)
            env["EDAP_LOG_DIR"] = str(root / "legacy-logs")
            env["EDAP_DEBUG_DIR"] = str(root / "legacy-debug")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import runpy, sys; paths = runpy.run_path(sys.argv[1]); "
                    "print(paths['LOG_DIR']); print(paths['DEBUG_DIR'])",
                    str(paths_file),
                ],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                [Path(line) for line in result.stdout.splitlines()],
                [root / "user_data" / "logs", root / "user_data" / "debug"],
            )
            self.assertFalse((root / "legacy-logs").exists())
            self.assertFalse((root / "legacy-debug").exists())


if __name__ == "__main__":
    unittest.main()
