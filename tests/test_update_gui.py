"""Update opt-in, target selection and Info layout without a game or network."""

import sys
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

import ttkbootstrap as ttk

from core import updates
from gui.pages import build_info
from gui.theme import Metrics, Theme

try:
    from vision.ocr import OCR  # noqa: F401

    _PADDLEOCR_STUBBED = False
except ModuleNotFoundError as error:
    if error.name != "paddleocr":
        raise
    sys.modules["paddleocr"] = SimpleNamespace(PaddleOCR=None)
    _PADDLEOCR_STUBBED = True

try:
    from gui.main_window import APGui
finally:
    if _PADDLEOCR_STUBBED:
        del sys.modules["paddleocr"]
        sys.modules.pop("vision.ocr", None)


class UpdateGuiTestCase(unittest.TestCase):
    def setUp(self):
        self.window = APGui.__new__(APGui)
        self.window.root = mock.Mock()
        self.window.check_main = mock.Mock()
        self.window.check_main.get.return_value = False
        self.window.update_check_id = 0
        self.window.update_tag = None
        self.window.main_commit = None
        self.window.installed_build = None
        self.window.update_text = mock.Mock()
        self.window.main_update_text = mock.Mock()
        self.window.update_label = mock.Mock()
        self.window.download_button = mock.Mock()
        self.window.main_update_button = mock.Mock()
        self.window.main_updates_frame = mock.Mock()
        self.window.metrics = mock.Mock()
        self.window.notify_user = mock.Mock()

    def run_check(self):
        with (
            mock.patch("gui.main_window.threading.Thread") as thread,
            mock.patch.object(updates, "get_latest_release", return_value="v999.0.0") as release,
            mock.patch.object(
                updates, "get_main_commit", return_value=updates.MainCommit("a" * 40, "2026-11-17", "Test")
            ) as main,
        ):
            thread.side_effect = lambda *, target, daemon: SimpleNamespace(start=target)
            self.window.check_updates()
            callback = self.window.root.after.call_args.args[1]
            callback()
        return release, main

    def test_default_checks_only_releases_and_hides_main(self):
        release, main = self.run_check()
        release.assert_called_once_with()
        main.assert_not_called()
        self.window.main_updates_frame.pack_forget.assert_called()
        self.window.main_update_button.set_disabled.assert_called_with(True)
        self.assertEqual(self.window.update_tag, "v999.0.0")
        self.window.main_commit = updates.MainCommit("a" * 40, "2026-11-17", "Stale result")
        with (
            mock.patch("gui.main_window.messagebox.askokcancel") as confirm,
            mock.patch.object(updates, "launch_updater") as launch,
        ):
            self.window.install_update("main")
        confirm.assert_not_called()
        launch.assert_not_called()
        self.window.notify_user.assert_called_once()

    def test_opt_in_checks_both_channels_and_keeps_higher_releases_available(self):
        self.window.check_main.get.return_value = True
        self.window.installed_build = updates.InstalledBuild("main", "b" * 40, updates.__version__)
        release, main = self.run_check()
        release.assert_called_once()
        main.assert_called_once()
        self.assertEqual(self.window.update_tag, "v999.0.0")
        self.window.download_button.set_disabled.assert_called_with(False)
        self.window.main_update_button.set_disabled.assert_called_with(False)
        self.window.main_updates_frame.pack.assert_called()

    def test_development_footer_notice_is_hidden_when_opted_out(self):
        self.window.check_main.get.return_value = True
        self.window._main_update_result(updates.MainCommit("a" * 40, "2026-11-17", "Test"))
        self.window.update_label.configure.assert_called_with(text="Development update available")
        self.window.update_label.grid.assert_called()
        self.window.check_main.get.return_value = False
        self.window._show_update_notice()
        self.window.update_label.grid_remove.assert_called_once()

    def test_old_check_cannot_restore_hidden_main_details_after_opt_out(self):
        self.window.check_main.get.return_value = True
        with mock.patch("gui.main_window.threading.Thread"):
            self.window.check_updates()
        stale_callback = self.window.root.after.call_args.args[1]
        self.window.check_main.get.return_value = False
        self.window._save_window_state = mock.Mock()
        with mock.patch.object(self.window, "check_updates"):
            self.window.toggle_main_updates()
        self.window.update_check_id += 1
        stale_callback()
        self.assertIsNone(self.window.main_commit)
        self.window.main_updates_frame.pack_forget.assert_called()

    def test_install_launches_exact_displayed_target_without_closing_app(self):
        self.window.check_main.get.return_value = True
        self.window.main_commit = updates.MainCommit("a" * 40, "2026-11-17", "Test")
        self.window.close_window = mock.Mock()
        with (
            mock.patch("gui.main_window.messagebox.askokcancel", return_value=True),
            mock.patch.object(updates, "launch_updater") as launch,
        ):
            self.window.install_update("main")
        launch.assert_called_once_with("main", "a" * 40)
        self.window.close_window.assert_not_called()

    def test_cancelled_or_failed_launch_leaves_program_running_and_reports_failure(self):
        self.window.update_tag = "v999.0.0"
        with (
            mock.patch("gui.main_window.messagebox.askokcancel", return_value=False) as confirm,
            mock.patch("gui.main_window.messagebox.showerror") as showerror,
            mock.patch.object(updates, "launch_updater", side_effect=OSError("blocked")) as launch,
        ):
            self.window.install_update()
            launch.assert_not_called()
            confirm.return_value = True
            self.window.install_update()
        showerror.assert_called_once()
        self.window.notify_user.assert_called_once_with("Could not start updater: blocked")


class UpdateInfoLayoutTestCase(unittest.TestCase):
    def test_main_details_are_opt_in_and_fit_at_supported_scalings(self):
        root = ttk.Window(themename="darkly")
        root.withdraw()
        self.addCleanup(root.destroy)
        app = APGui.__new__(APGui)
        app.root = root
        app.metrics = Metrics(root)
        app.theme = Theme(root, app.metrics)
        app.statistics_text = tk.StringVar(
            root,
            value=(
                "Current account\nJumps: 20 | Distance: 500 ly\nAssist time: 00:20:00\nSystem travel: 00:20:00\n\n"
                "Current ship (#1)\nJumps: 20 | Distance: 500 ly\nAssist time: 00:20:00\n"
                "System travel: 00:20:00\nAverage jump cycle: 60 s\nValid time measurements: 20"
            ),
        )
        app.update_text = tk.StringVar(root, value="A new release (v2.0.1) is available.")
        app.installed_build_text = tk.StringVar(
            root, value="Installed: aaaaaaaa (2026-11-17)\n" + "An example development commit message " * 3
        )
        app.main_update_text = tk.StringVar(
            root, value="Available: bbbbbbbb (2026-11-18)\n" + "Another development commit message " * 3
        )
        app.check_main = tk.BooleanVar(root, value=False)
        app.update_tag = "v2.0.1"
        app.main_commit = updates.MainCommit("b" * 40, "2026-11-18", "Test")
        app.installed_build = None
        app.toggle_main_updates = mock.Mock()
        for dpi in (96, 120, 144):
            with self.subTest(dpi=dpi):
                app.metrics.set_dpi(dpi)
                app.theme.apply()
                app.check_main.set(False)
                for child in root.winfo_children():
                    child.destroy()
                page = ttk.Frame(root)
                page.pack(fill="both", expand=True)
                build_info(app, page)
                root.update_idletasks()
                self.assertEqual(app.main_updates_frame.winfo_manager(), "")
                if dpi == 96:
                    check = next(
                        widget
                        for column in page.winfo_children()
                        for card in column.winfo_children()
                        for widget in card.winfo_children()
                        if isinstance(widget, ttk.Checkbutton)
                    )
                    root.deiconify()
                    root.update()
                    root.focus_force()
                    check.event_generate("<ButtonPress-1>", x=8, y=8)
                    check.event_generate("<ButtonRelease-1>", x=8, y=8)
                    root.update()
                    self.assertTrue(app.check_main.get())
                    self.assertIsNot(root.focus_get(), check)
                    root.event_generate("<Tab>")
                    root.update()
                    self.assertIs(root.focus_get(), check)
                    check.event_generate("<KeyPress-space>")
                    check.event_generate("<KeyRelease-space>")
                    root.update()
                    self.assertFalse(app.check_main.get())
                    self.assertIs(root.focus_get(), check)
                    self.assertEqual(app.toggle_main_updates.call_count, 2)
                app.check_main.set(True)
                app.refresh_update_button()
                root.update_idletasks()
                self.assertEqual(app.main_updates_frame.winfo_manager(), "pack")
                # The Info content gets the area beside the sidebar and below the tabs.
                self.assertLessEqual(page.winfo_reqheight(), app.metrics.px(550))
                app.check_main.set(False)
                app.refresh_update_button()
                root.update_idletasks()
                self.assertEqual(app.main_updates_frame.winfo_manager(), "")


if __name__ == "__main__":
    unittest.main()
