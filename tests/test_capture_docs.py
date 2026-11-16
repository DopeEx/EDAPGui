"""Regression checks for complete, non-destructive documentation window captures."""

import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from tools.capture_docs import (
    BACKGROUND,
    MARGIN,
    ROOT,
    capture_window,
    check_capture_area,
    preserve_exception_hooks,
    read_defaults,
    run,
    window_bounds,
)


@contextmanager
def capture_backend(image):
    screen = Mock()
    screen.monitors = [{}, {"left": 0, "top": 0, "width": 1920, "height": 1080}]
    screen.grab.return_value = SimpleNamespace(size=image.size, bgra=image.tobytes("raw", "BGRX"))
    mss = Mock()
    mss.mss.return_value.__enter__ = Mock(return_value=screen)
    mss.mss.return_value.__exit__ = Mock(return_value=False)
    with (
        patch.dict(sys.modules, {"mss": mss}),
        patch("tools.capture_docs.window_bounds", return_value=(80, 60, 200, 140)) as bounds,
        patch("tools.capture_docs.check_capture_area") as check,
    ):
        yield screen, bounds, check


class DocumentationCaptureTests(unittest.TestCase):
    def test_preview_startup_provides_the_application_theme_style(self):
        import tkinter as tk

        import ttkbootstrap as ttk

        with patch.object(sys, "path", [str(ROOT / "src"), *sys.path]):
            import gui
            from gui.theme import Theme

            class PreviewInitializedError(Exception):
                pass

            initialized = Mock()
            style = Mock()
            backdrop = Mock()

            def initialize_gui(window, **_kwargs):
                Theme(window, SimpleNamespace(px=lambda value: value)).apply()
                initialized()
                raise PreviewInitializedError

            main_window = SimpleNamespace(
                window_state=SimpleNamespace(load=Mock(), save=Mock()),
                MousePoint=Mock(),
                datetime=None,
                enable_dpi_awareness=Mock(),
                APGui=initialize_gui,
            )

            def initialize_toplevel(window, *_args, **_kwargs):
                window.tk = SimpleNamespace()

            modules = {
                "gui.main_window": main_window,
                "core.commodities": SimpleNamespace(get_localization=Mock()),
                "gui.cv_view": SimpleNamespace(CvFrames=Mock()),
                "gui.fields": SimpleNamespace(DEFAULT_OVERLAY_LINE_ORDER=()),
                "gui.waypoint_model": SimpleNamespace(RouteDocument=Mock(), new_waypoint=Mock()),
            }
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.dict(sys.modules, modules),
                patch.object(gui, "main_window", main_window, create=True),
                patch.object(ttk, "Window", return_value=backdrop),
                patch.object(ttk.Style, "get_instance", return_value=style),
                patch.object(tk.Toplevel, "__init__", initialize_toplevel),
                patch.object(ttk.Toplevel, "__init__", initialize_toplevel),
                patch.object(tk.Toplevel, "withdraw"),
                patch.object(tk.Toplevel, "iconbitmap"),
                patch.object(tk.Toplevel, "configure"),
                patch("tools.capture_docs.logging.shutdown"),
            ):
                with self.assertRaises(PreviewInitializedError):
                    run(Path(directory) / "output", preview=False)
            initialized.assert_called_once()
            style.register_theme.assert_called_once()
            backdrop.destroy.assert_called_once()

    def test_capture_preserves_entire_frame_and_adds_outer_margin(self):
        image = Image.new("RGB", (120 + 2 * MARGIN, 80 + 2 * MARGIN), BACKGROUND)
        image.putpixel((MARGIN + 1, MARGIN), (255, 0, 0))
        image.putpixel((MARGIN + 118, MARGIN + 79), (0, 255, 0))
        with capture_backend(image) as (screen, _bounds, check):
            result = capture_window(123, 456)
        screen.grab.assert_called_once_with(
            {"left": 80 - MARGIN, "top": 60 - MARGIN, "width": image.width, "height": image.height}
        )
        self.assertEqual(check.call_count, 2)
        # Preserve compositor pixels, including rounded corners, without a synthetic crop or frame.
        self.assertEqual(result.tobytes(), image.tobytes())

    def test_incomplete_or_blank_capture_is_an_explicit_failure(self):
        for image in (Image.new("RGB", (50, 80)), Image.new("RGB", (120 + 2 * MARGIN, 80 + 2 * MARGIN))):
            with self.subTest(size=image.size):
                with capture_backend(image):
                    with self.assertRaises(RuntimeError):
                        capture_window(123, 456)

    def test_capture_rejects_offscreen_moving_or_newly_obscured_windows(self):
        image = Image.new("RGB", (120 + 2 * MARGIN, 80 + 2 * MARGIN))
        with capture_backend(image) as (screen, _bounds, _check):
            screen.monitors[1]["height"] = 100
            with self.assertRaisesRegex(RuntimeError, "fit on one monitor"):
                capture_window(123, 456)
            screen.grab.assert_not_called()
        with capture_backend(image) as (_screen, bounds, _check):
            bounds.side_effect = [(80, 60, 200, 140), (90, 60, 210, 140)]
            with self.assertRaisesRegex(RuntimeError, "moved during capture"):
                capture_window(123, 456)
        with capture_backend(image) as (_screen, _bounds, check):
            check.side_effect = [None, RuntimeError("Another window overlaps the capture area.")]
            with self.assertRaisesRegex(RuntimeError, "overlaps the capture area"):
                capture_window(123, 456)

    def test_capture_area_requires_unobscured_window_and_neutral_backdrop(self):
        gui = Mock()
        gui.IsIconic.return_value = False
        gui.IsWindowVisible.side_effect = lambda handle: handle != 40
        rectangles = {
            40: (80, 60, 200, 140),
            50: (400, 400, 500, 500),
            456: (68, 48, 212, 152),
            60: (80, 60, 200, 140),
        }
        gui.GetWindowRect.side_effect = rectangles.__getitem__
        for order, accepted in (
            ([40, 50, 123, 456, 60], True),
            ([60, 123, 456], False),
            ([123, 60, 456], False),
            ([456, 123], False),
            ([123], False),
        ):
            with self.subTest(order=order):

                def enumerate_windows(callback, data):
                    for handle in order:
                        callback(handle, data)

                gui.EnumWindows.side_effect = enumerate_windows
                with patch.dict(sys.modules, {"win32gui": gui}):
                    if accepted:
                        check_capture_area(123, 456, (68, 48, 212, 152))
                    else:
                        with self.assertRaises(RuntimeError):
                            check_capture_area(123, 456, (68, 48, 212, 152))

    def test_window_bounds_use_visible_dwm_frame_and_reject_api_failure(self):
        for result, rectangle in ((0, (80, 60, 200, 140)), (0, (0, 0, 0, 0)), (-1, (0, 0, 0, 0))):
            with self.subTest(result=result, rectangle=rectangle):

                def get_bounds(_hwnd, _attribute, pointer, _size):
                    pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = rectangle
                    return result

                get_attribute = Mock(side_effect=get_bounds)
                with (
                    patch(
                        "tools.capture_docs.ctypes.WinDLL",
                        return_value=SimpleNamespace(DwmGetWindowAttribute=get_attribute),
                    ),
                ):
                    if result == 0 and rectangle[2] > rectangle[0]:
                        self.assertEqual(window_bounds(123), rectangle)
                    else:
                        with self.assertRaises(RuntimeError):
                            window_bounds(123)

    def test_temporary_logger_hooks_are_restored_before_directory_cleanup(self):
        original_main, original_thread, original_unraisable = Mock(), Mock(), Mock()
        with (
            patch.object(sys, "excepthook", original_main),
            patch.object(threading, "excepthook", original_thread),
            patch.object(sys, "unraisablehook", original_unraisable),
        ):
            with tempfile.TemporaryDirectory() as directory:
                filename = Path(directory) / "autopilot.log"
                with preserve_exception_hooks():

                    def stale_hook(*_args):
                        filename.write_text("late exception")

                    sys.excepthook = stale_hook
                    threading.excepthook = stale_hook
                    sys.unraisablehook = stale_hook
            error = RuntimeError("capture failed")
            sys.excepthook(RuntimeError, error, None)
            threading.excepthook(error)
            sys.unraisablehook(error)
            original_main.assert_called_once_with(RuntimeError, error, None)
            original_thread.assert_called_once_with(error)
            original_unraisable.assert_called_once_with(error)
            self.assertFalse(filename.exists())

    def test_defaults_follow_source_without_starting_autopilot_or_executing_code(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "autopilot.py"
            source.write_text(
                "raise RuntimeError('must not execute')\n"
                "class Autopilot:\n"
                "    def __init__(self):\n"
                "        self.config = {'ActivateEliteEachKey': True, 'OverlayLineOrder': list(ORDER)}\n"
            )
            config = read_defaults(source, ("mode", "status"))
            self.assertTrue(config["ActivateEliteEachKey"])
            self.assertEqual(config["OverlayLineOrder"], ["mode", "status"])
