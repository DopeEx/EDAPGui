"""Verify cockpit screenshot capture without game input or OCR."""

import json
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from core import paths
from core.constants import FlagsInMainShip, GuiFocusExternalPanel, GuiFocusNoFocus, GuiFocusRolePanel
from i18n.localization import LocalizationManager
from vision.cockpit_capture import PANELS, capture_cockpit


class CockpitCaptureTestCase(unittest.TestCase):
    def setUp(self):
        self.focus = GuiFocusNoFocus
        self.foreground = True
        self.left_tab_count = 5
        self.left_step = 0
        self.ap = Mock(terminate=False)
        self.ap.assist_is_enabled.return_value = False
        self.ap.status.get_cleaned_data.side_effect = lambda **_: {"Flags": FlagsInMainShip, "GuiFocus": self.focus}
        self.ap.jn.ship_state.return_value = {"type": "type10", "language": "German/DE"}
        self.ap.locale = LocalizationManager(str(paths.LOCALES_DIR), "en")
        self.ap.scr.get_screen_full.return_value = np.full((24, 32, 3), (10, 40, 200), dtype=np.uint8)
        self.ap.scr.get_screen_full.side_effect = self.screenshot
        self.ap.keys.send.side_effect = self.send
        for patcher in (
            patch("vision.cockpit_capture._game_is_foreground", side_effect=lambda: self.foreground),
            patch("vision.cockpit_capture.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def send(self, key, **_):
        directions = {direction: focus for _, direction, focus, _ in PANELS}
        if key in directions:
            self.focus = directions[key]
        elif key == "UI_Back":
            self.focus = GuiFocusNoFocus
        elif key == "CycleNextPanel" and self.focus == GuiFocusExternalPanel:
            self.left_step += 1

    def screenshot(self):
        image = self.ap.scr.get_screen_full.return_value.copy()
        if self.focus == GuiFocusExternalPanel:
            image[5:7, 3:22] = 50 * (self.left_step % self.left_tab_count)
            image[14:, :] = self.left_step * 40  # Simulate changing HUD content below the tab bar.
        return image

    def test_records_all_panels_with_journal_language_and_png_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, report = capture_cockpit(self.ap, Path(tmp), {"display_mode": None, "hud_brightness_pct": 70})
            frames = report["frames"]
            self.assertEqual(len(frames), 18)
            self.assertEqual([frame["step"] for frame in frames if frame["view"] == "left"], [1, 2, 3, 4, 5])
            self.assertEqual(report["duplicates"], [])
            self.assertEqual(frames[0]["file"], "RENAME-BEFORE-FIXTURE__00_forward_step-00_type10_32x24_de.png")
            self.assertEqual([frame for frame in frames if frame["gui_focus"] == GuiFocusNoFocus], [frames[0]])
            self.assertEqual(
                [frame["step"] for frame in frames if frame["gui_focus"] == GuiFocusRolePanel], [1, 2, 3, 4]
            )
            self.assertEqual(report["game_language"], "de")
            self.assertEqual(report["resolution"], "32x24")
            self.assertIsNone(report["stopped"])
            self.assertEqual(json.loads((folder / "manifest.json").read_text(encoding="utf-8")), report)
            np.testing.assert_array_equal(
                cv2.imread(str(folder / frames[0]["file"])), self.ap.scr.get_screen_full.return_value
            )
        self.ap.ocr.assert_not_called()
        self.assertEqual(self.focus, GuiFocusNoFocus)

    def test_left_panel_drops_return_to_first_tab_even_when_rest_of_frame_changes(self):
        self.left_tab_count = 4
        with tempfile.TemporaryDirectory() as tmp:
            folder, report = capture_cockpit(self.ap, Path(tmp), {})
            self.assertEqual(len(list(folder.glob("*.png"))), 17)
        self.assertEqual([frame["step"] for frame in report["frames"] if frame["view"] == "left"], [1, 2, 3, 4])
        self.assertEqual(report["duplicates"], [{"view": "left", "step": 5, "same_as_step": 1}])
        self.assertIsNone(report["stopped"])

    def test_role_panel_starts_at_top_when_crew_was_previously_selected(self):
        selected = 3

        def send(key, **options):
            nonlocal selected
            if self.focus == GuiFocusRolePanel:
                if key == "UI_Up":
                    selected = 0 if options.get("hold", 0) >= 1 else max(0, selected - 1)
                elif key == "CycleNextPanel":
                    selected = min(3, selected + 1)
            self.send(key, **options)

        self.ap.keys.send.side_effect = send
        self.ap.scr.get_screen_full.side_effect = lambda: np.full((24, 32, 3), selected, dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            folder, report = capture_cockpit(self.ap, Path(tmp), {})
            captured = [
                int(cv2.imread(str(folder / frame["file"]))[0, 0, 0])
                for frame in report["frames"]
                if frame["gui_focus"] == GuiFocusRolePanel
            ]
        self.assertEqual(captured, [0, 1, 2, 3])
        self.assertIsNone(report["stopped"])

    def test_stops_without_saving_or_sending_more_input_after_focus_loss(self):
        def capture():
            self.foreground = False
            return self.ap.scr.get_screen_full.return_value

        self.ap.scr.get_screen_full.side_effect = capture
        with tempfile.TemporaryDirectory() as tmp:
            _, report = capture_cockpit(self.ap, Path(tmp), {})
        self.assertEqual(report["frames"], [])
        self.assertIn("foreground", report["stopped"])
        self.ap.keys.send.assert_called_once_with("HeadLookReset")

    def test_waits_for_manual_focus_if_activation_does_not_take(self):
        self.foreground = False

        def switch_to_game(delay):
            if delay == 0.1:
                self.ap.keys.send.assert_not_called()
                self.foreground = True

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("vision.cockpit_capture.sleep", side_effect=switch_to_game),
        ):
            _, report = capture_cockpit(self.ap, Path(tmp), {})
        self.ap.keys.activate_elite_for_input.assert_called_once_with()
        self.assertIsNone(report["stopped"])
        self.assertEqual(len(report["frames"]), 18)

    def test_never_sends_a_key_if_activation_and_manual_focus_fail(self):
        self.foreground = False
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("vision.cockpit_capture.monotonic", side_effect=count()),
        ):
            _, report = capture_cockpit(self.ap, Path(tmp), {})
        self.ap.keys.activate_elite_for_input.assert_called_once_with()
        self.ap.keys.send.assert_not_called()
        self.assertEqual(report["frames"], [])
        self.assertIn("foreground", report["stopped"])
