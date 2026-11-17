"""Tests for the update check (no network access)."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from core import updates
from core.install_lock import installation_lock


class VersionCompareTestCase(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(updates.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updates.parse_version("V0.10.0"), (0, 10, 0))
        self.assertEqual(updates.parse_version("1.7.0"), (1, 7, 0))

    def test_parse_version_rejects_other_text(self):
        for text in ("", "latest", "v1.2", "1.2.3-beta", "v1.2.3.4"):
            with self.subTest(text=text):
                self.assertIsNone(updates.parse_version(text))

    def test_is_newer(self):
        self.assertTrue(updates.is_newer("v1.7.1", "1.7.0"))
        self.assertTrue(updates.is_newer("v1.10.0", "1.9.0"))
        self.assertFalse(updates.is_newer("v1.7.0", "1.7.0"))
        self.assertFalse(updates.is_newer("v1.6.9", "1.7.0"))

    def test_unparsable_versions_are_never_newer(self):
        self.assertFalse(updates.is_newer("nightly", "1.7.0"))
        self.assertFalse(updates.is_newer("v2.0.0", "unknown"))


class LatestReleaseTestCase(unittest.TestCase):
    @staticmethod
    def response(status, payload=None):
        response = mock.Mock(status_code=status)
        response.json.return_value = payload
        response.raise_for_status.side_effect = requests.HTTPError() if status >= 400 else None
        return response

    def test_returns_the_tag(self):
        with mock.patch.object(
            updates.requests, "get", return_value=self.response(200, {"tag_name": "v1.8.0"})
        ) as get_release:
            self.assertEqual(updates.get_latest_release(), "v1.8.0")
        get_release.assert_called_once_with(
            "https://api.github.com/repos/DopeEx/EDFlightControl/releases/latest",
            timeout=5.0,
            headers={"Accept": "application/vnd.github+json"},
        )

    def test_no_release_published(self):
        with mock.patch.object(updates.requests, "get", return_value=self.response(404)):
            self.assertIsNone(updates.get_latest_release())

    def test_server_error(self):
        with mock.patch.object(updates.requests, "get", return_value=self.response(500)):
            self.assertIsNone(updates.get_latest_release())

    def test_network_error(self):
        with mock.patch.object(updates.requests, "get", side_effect=requests.ConnectionError()):
            self.assertIsNone(updates.get_latest_release())

    def test_invalid_release_response_is_reported_as_failed_check(self):
        for payload in ([], {}, {"tag_name": "not a version"}):
            with (
                self.subTest(payload=payload),
                mock.patch.object(updates.requests, "get", return_value=self.response(200, payload)),
                mock.patch.object(updates.logger, "warning") as warning,
            ):
                self.assertIsNone(updates.get_latest_release())
                warning.assert_called_once()


class MainUpdatesTestCase(unittest.TestCase):
    sha = "a" * 40

    def test_reads_main_commit_without_exposing_multiline_message(self):
        response = LatestReleaseTestCase.response(
            200,
            {
                "sha": self.sha,
                "commit": {"committer": {"date": "2026-11-17T10:00:00Z"}, "message": "Test build\n\nDetails"},
            },
        )
        with mock.patch.object(updates.requests, "get", return_value=response) as get:
            commit = updates.get_main_commit()
        self.assertEqual(commit, updates.MainCommit(self.sha, "2026-11-17T10:00:00Z", "Test build"))
        self.assertIn("aaaaaaaa (2026-11-17)", commit.describe())
        self.assertEqual(get.call_args.args, (updates.MAIN_COMMIT_API,))

    def test_failed_or_malformed_main_check_does_not_claim_up_to_date(self):
        for payload in ({}, [], {"sha": "not a commit"}, {"sha": self.sha, "commit": None}):
            with self.subTest(payload=payload):
                with (
                    mock.patch.object(
                        updates.requests, "get", return_value=LatestReleaseTestCase.response(200, payload)
                    ),
                    mock.patch.object(updates.logger, "warning") as warning,
                ):
                    self.assertIsNone(updates.get_main_commit())
                    warning.assert_called_once()
        with mock.patch.object(updates.requests, "get", side_effect=requests.Timeout()):
            self.assertIsNone(updates.get_main_commit())

    def test_main_comparison_uses_revision_not_release_version(self):
        commit = updates.MainCommit(self.sha, "2026-11-17", "Test")
        installed = updates.InstalledBuild("main", self.sha, updates.__version__)
        self.assertFalse(updates.main_update_available(commit, installed))
        self.assertTrue(
            updates.main_update_available(commit, updates.InstalledBuild("main", "b" * 40, updates.__version__))
        )
        self.assertTrue(updates.main_update_available(commit, None))
        self.assertFalse(updates.main_update_available(None, installed))

    def test_installed_metadata_accepts_powershell_bom_and_rejects_stale_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertIsNone(updates.get_installed_build(root))
            (root / ".update").mkdir()
            state = root / ".update" / "installed.json"
            payload = {
                "channel": "main",
                "revision": self.sha,
                "version": updates.__version__,
                "commitDate": "2026-11-17T10:00:00Z",
                "commitMessage": "Test build",
            }
            state.write_text(json.dumps(payload), encoding="utf-8-sig")
            installed = updates.get_installed_build(root)
            self.assertEqual(installed.revision, self.sha)
            self.assertIn("Test build", installed.describe())
            for invalid in (
                dict(payload, version="0.0.1"),
                dict(payload, revision="not-a-commit"),
                dict(payload, commitDate=[]),
                {"channel": "main"},
                [],
            ):
                with self.subTest(invalid=invalid), mock.patch.object(updates.logger, "warning") as warning:
                    state.write_text(json.dumps(invalid), encoding="utf-8")
                    self.assertIsNone(updates.get_installed_build(root))
                    warning.assert_called_once()

    def test_updater_passes_exact_channel_target_and_rejects_git_checkout(self):
        with tempfile.TemporaryDirectory(prefix="edfc update ") as folder:
            root = Path(folder)
            (root / "update_edfc.bat").touch()
            with mock.patch.object(updates.subprocess, "Popen") as launch:
                updates.launch_updater("main", self.sha, root)
                self.assertEqual(launch.call_args.args[0][-3:], ["update_edfc.bat", "main", self.sha])
                self.assertEqual(launch.call_args.kwargs["cwd"], root)
                for channel, revision in (("other", self.sha), ("main", "main"), ("release", "v2.0.0 & echo x")):
                    with self.assertRaises(ValueError):
                        updates.launch_updater(channel, revision, root)
                (root / ".git").write_text("gitdir: elsewhere")
                with self.assertRaisesRegex(RuntimeError, "Git working copy"):
                    updates.launch_updater("release", "v2.0.1", root)
                self.assertEqual(launch.call_count, 1)


@unittest.skipUnless(os.name == "nt", "Windows installation lock")
class InstallationLockTestCase(unittest.TestCase):
    def test_restart_releases_installation_lock_before_relaunch(self):
        held = False

        @contextmanager
        def lock(_root):
            nonlocal held
            held = True
            try:
                yield
            finally:
                held = False

        def restart(*_args):
            self.assertFalse(held)

        entry = Path(updates.__file__).resolve().parents[1] / "main.py"
        with (
            mock.patch("core.install_lock.installation_lock", side_effect=lock),
            mock.patch.dict(sys.modules, {"gui.main_window": SimpleNamespace(main=lambda: True)}),
            mock.patch("os.execv", side_effect=restart) as relaunch,
            mock.patch.object(sys, "argv", [str(entry)]),
        ):
            runpy.run_path(str(entry), run_name="__main__")
        relaunch.assert_called_once_with(sys.executable, [sys.executable, str(entry)])

    def test_app_and_powershell_updater_share_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lock_path = str(root / ".update" / "installation.lock").replace("'", "''")
            command = (
                f"try {{ $f = [IO.File]::Open('{lock_path}', 'OpenOrCreate', 'ReadWrite', 'None'); "
                "$f.Dispose(); exit 0 } catch [IO.IOException] { exit 7 }"
            )
            with installation_lock(root):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with installation_lock(root):
                        self.fail("A second application must not acquire the update lock.")
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                    capture_output=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 7, result.stderr)
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_interrupted_exchange_requires_recovery_before_app_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".update").mkdir()
            (root / ".update" / "pending.json").write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "update_edfc.bat"):
                with installation_lock(root):
                    self.fail("An incomplete update must not start.")
            (root / ".update" / "pending.json").unlink()
            with installation_lock(root):
                pass


if __name__ == "__main__":
    unittest.main()
