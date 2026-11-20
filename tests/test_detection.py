"""Tests for template matching and the screen detection on real screenshot crops.

The crops in tests/fixtures were captured at the 3440x1440 reference resolution, so the templates are
loaded unscaled. A fake screen returns the crop for every region, which exercises the real region
filters, templates, thresholds and offset calculations without the game or a monitor.
"""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from core import paths
from core.constants import FlagsBeingInterdicted, FlagsFsdJump, FlagsSupercruise
from core.types import EdapInterruptError
from gui.cv_view import CvFrames
from routines.flight import FlightMixin
from tests.helpers import FIXTURES, FakeScreen, ScreenshotScreen, load_fixture_image, load_screenshot, pad_to_region
from vision.detection import DetectionMixin
from vision.regions import ScreenRegions
from vision.templates import ImageTemplates


class Detector(DetectionMixin):
    def __init__(self):
        self.cv_frames = CvFrames()


class InterdictionTestCase(unittest.TestCase):
    def detector(self):
        detector = Detector()
        detector.status = Mock()
        detector.status.get_flag.side_effect = lambda flag: flag in (FlagsBeingInterdicted, FlagsFsdJump)
        detector.status.get_flag2.return_value = False
        detector.status.wait_for_flag_off.return_value = True
        detector.jn = Mock()
        detector.jn.ship_state.return_value = {"status": "starting_supercruise", "interdicted": True}
        detector.keys = Mock()
        detector.vce = Mock()
        detector.ap_ckb = Mock()
        detector.start_sco_monitoring = Mock()
        return detector

    def test_no_interdiction_sends_no_inputs(self):
        detector = self.detector()
        detector.status.get_flag.return_value = False
        detector.status.get_flag.side_effect = None
        self.assertFalse(detector.interdiction_check())
        detector.keys.send.assert_not_called()
        detector.jn.ship_state.assert_not_called()

    def test_waits_for_journal_after_supercruise_flag(self):
        detector = self.detector()
        supercruise = iter([False, True, True, True])
        detector.status.get_flag.side_effect = lambda flag: next(supercruise) if flag == FlagsSupercruise else True
        arriving = {"status": "starting_supercruise", "interdicted": True}
        arrived = {"status": "in_supercruise", "interdicted": True}
        detector.jn.ship_state.side_effect = [arriving, arriving, arrived]
        with patch("vision.detection.sleep") as sleep:
            self.assertTrue(detector.interdiction_check())
        self.assertTrue(arriving["interdicted"])
        self.assertFalse(arrived["interdicted"])
        self.assertEqual(detector.jn.ship_state.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertNotIn(("UseBoostJuice",), [call.args for call in detector.keys.send.call_args_list])

    def test_waits_for_supercruise_flag_after_journal(self):
        detector = self.detector()
        supercruise = iter([False, False, True])
        detector.status.get_flag.side_effect = lambda flag: next(supercruise) if flag == FlagsSupercruise else True
        detector.jn.ship_state.return_value["status"] = "in_supercruise"
        with patch("vision.detection.sleep") as sleep:
            self.assertTrue(detector.interdiction_check())
        sleep.assert_called_once_with(0.25)
        self.assertFalse(detector.jn.ship_state.return_value["interdicted"])

    def test_unconfirmed_supercruise_times_out_without_resetting_interdiction(self):
        for journal_ready, status_ready in ((False, False), (False, True), (True, False)):
            with self.subTest(journal_ready=journal_ready, status_ready=status_ready):
                detector = self.detector()
                supercruise = iter([False, status_ready])
                detector.status.get_flag.side_effect = lambda flag: (
                    next(supercruise) if flag == FlagsSupercruise else flag == FlagsBeingInterdicted
                )
                if journal_ready:
                    detector.jn.ship_state.return_value["status"] = "in_supercruise"
                with (
                    patch("vision.detection.time.monotonic", side_effect=[0, 0, 60]),
                    patch("vision.detection.sleep"),
                    self.assertLogs("ed_log", level="WARNING"),
                    self.assertRaisesRegex(TimeoutError, "supercruise after interdiction"),
                ):
                    detector.interdiction_check()
                self.assertTrue(detector.jn.ship_state.return_value["interdicted"])
                detector.ap_ckb.assert_any_call(
                    "log", "Could not confirm supercruise after interdiction. Assist stopped."
                )
                if not status_ready:
                    detector.keys.send.assert_any_call("UseBoostJuice")

    def test_boosts_only_until_transition_starts(self):
        detector = self.detector()
        supercruise = iter([False, False, False, True, True])
        jumping = iter([False, True])

        def flag_set(flag):
            if flag == FlagsSupercruise:
                return next(supercruise)
            if flag == FlagsFsdJump:
                return next(jumping)
            return flag == FlagsBeingInterdicted

        detector.status.get_flag.side_effect = flag_set
        arriving = {"status": "starting_supercruise", "interdicted": True}
        arrived = {"status": "in_supercruise", "interdicted": True}
        detector.jn.ship_state.side_effect = [arriving, arriving, arriving, arrived]
        with patch("vision.detection.sleep") as sleep:
            self.assertTrue(detector.interdiction_check())
        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (0.25,), (0.25,)])
        self.assertEqual(
            [call.args for call in detector.keys.send.call_args_list],
            [("SetSpeed100",), ("Supercruise",), ("UseBoostJuice",)],
        )

    def test_stop_interrupt_propagates_during_recovery(self):
        detector = self.detector()
        with (
            patch("vision.detection.sleep", side_effect=EdapInterruptError),
            self.assertRaises(EdapInterruptError),
        ):
            detector.interdiction_check()
        self.assertTrue(detector.jn.ship_state.return_value["interdicted"])


class MatchingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = ImageTemplates(2160)
        cls.images = {
            name: pad_to_region(load_fixture_image(name))
            for name in ("compass", "navpoint", "navpoint-behind", "navpoint-rim", "target", "disengage")
        }
        cls.images["message"] = load_fixture_image("disengage")
        cls.images["empty"] = np.zeros((700, 700, 3), np.uint8)

    def regions(self, image_name):
        return ScreenRegions(FakeScreen(self.images[image_name]), self.templates)

    def detect(self, method, image_name):
        return getattr(Detector(), method)(self.regions(image_name))


class CvViewTestCase(MatchingTestCase):
    def test_disengage_cv_view_accepts_bgra_crops_without_changing_ocr_images(self):
        for success_variant in range(3):
            with self.subTest(success_variant=success_variant):
                detector = Detector()
                detector.cv_frames.open("hud")
                detector.debug_overlay = False
                detector.ocr = Mock()
                detector.locale = Mock()
                detector.locale.all_values.return_value = ["PRESS TO DISENGAGE"]
                detector.ocr.string_similarity.side_effect = lambda expected, actual: float(expected == actual)
                bgra = np.full((80, 140, 4), 123, np.uint8)
                raw = bgra[:, :, :3]
                self.assertFalse(raw.flags.c_contiguous)
                variants = [raw.copy(), raw, cv2.resize(raw, None, fx=2, fy=2)]
                originals = [image.copy() for image in variants]
                regions = Mock()
                regions.message_image.side_effect = variants
                detector.ocr.image_simple_ocr.side_effect = [
                    ["PRESS TO DISENGAGE"] if i == success_variant else [] for i in range(3)
                ]
                with patch.object(detector, "show_cv_image") as show:
                    self.assertTrue(detector.sc_disengage_active(regions))
                show.assert_called_once()
                displayed = show.call_args.args[1]
                self.assertTrue(displayed.flags.c_contiguous)
                for original, image in zip(originals, variants):
                    self.assertTrue(np.array_equal(original, image))
                    self.assertFalse(np.shares_memory(displayed, image))

    def test_message_gate_cv_view_does_not_modify_capture(self):
        detector = Detector()
        detector.cv_frames.open("hud")
        regions = Mock()
        regions.message_text_present.return_value = False
        regions.last_message_fraction = 0.0
        regions.last_message_line = 0.0
        bgra = np.full((80, 140, 4), 123, np.uint8)
        image = bgra[:, :, :3]
        regions.message_image.return_value = image
        original = image.copy()
        with patch.object(detector, "show_cv_image") as show:
            self.assertFalse(detector.sc_disengage_label_up(regions))
        self.assertTrue(np.array_equal(image, original))
        self.assertFalse(np.shares_memory(show.call_args.args[1], image))

    def test_cv_view_preserves_detection_results_and_source_images(self):
        for method, image_name in (
            ("get_nav_offset", "compass"),
            ("get_destination_offset", "target"),
            ("is_destination_occluded", "empty"),
        ):
            with self.subTest(method=method):
                expected = self.detect(method, image_name)
                original = self.images[image_name].copy()
                detector = Detector()
                detector.cv_frames.open(
                    {
                        "get_nav_offset": "compass",
                        "get_destination_offset": "target",
                        "is_destination_occluded": "occluded",
                    }[method]
                )
                with (
                    patch.object(detector, "show_cv_image") as show,
                    patch("vision.detection.cv2.moveWindow"),
                    patch("vision.detection.cv2.waitKey"),
                    self.assertNoLogs("ed_log", level="ERROR"),
                ):
                    result = getattr(detector, method)(self.regions(image_name))
                self.assertEqual(result, expected)
                show.assert_called_once()
                self.assertTrue(np.array_equal(self.images[image_name], original))


class TemplateTestCase(MatchingTestCase):
    def test_all_templates_are_loaded(self):
        for name, template in self.templates.template.items():
            with self.subTest(template=name):
                self.assertIsNotNone(template["image"])
                self.assertEqual(template["image"].shape[::-1], (template["width"], template["height"]))

    def test_templates_are_scaled(self):
        half = ImageTemplates(1080)
        full = ImageTemplates(2160)

        for name in ("compass", "navpoint", "target"):
            with self.subTest(template=name):
                self.assertAlmostEqual(half.template[name]["width"], full.template[name]["width"] / 2, delta=1)
                self.assertAlmostEqual(half.template[name]["height"], full.template[name]["height"] / 2, delta=1)

    def test_template_is_found_at_its_position(self):
        regions = self.regions("empty")
        for name in ("compass", "target", "navpoint"):
            with self.subTest(template=name):
                template = self.templates.template[name]["image"]
                canvas = np.zeros((300, 400), np.uint8)
                canvas[40 : 40 + template.shape[0], 25 : 25 + template.shape[1]] = template

                _, (_, max_val, _, max_loc), _ = regions.match_template_in_image(canvas, name)

                self.assertGreater(max_val, 0.99)
                self.assertEqual(max_loc, (25, 40))

    def test_x3_matching_finds_the_template_in_a_colour_image(self):
        regions = self.regions("empty")
        template = self.templates.template["compass"]["image"]
        canvas = np.zeros((300, 400, 3), np.uint8)
        canvas[60 : 60 + template.shape[0], 90 : 90 + template.shape[1]] = cv2.cvtColor(template, cv2.COLOR_GRAY2BGR)

        _, (_, max_val, _, max_loc), _ = regions.match_template_in_image_x3(canvas, "compass")

        self.assertGreater(max_val, 0.99)
        self.assertEqual(max_loc, (90, 60))


class CompassTestCase(MatchingTestCase):
    def test_dim_cyan_rim_over_warm_glare_requires_a_single_hollow_marker(self):
        for scale in (1, 2):
            for shape in ("ring", "filled", "arc", "two-rings", "glare"):
                with self.subTest(scale=scale, shape=shape):
                    image = np.full((80 * scale, 80 * scale, 3), (121, 132, 165), dtype=np.uint8)
                    cv2.line(image, (28 * scale, 0), (46 * scale, 79 * scale), (135, 147, 169), 5 * scale)
                    rim = (155, 167, 143)
                    point = (39 * scale, 51 * scale)
                    if shape in ("ring", "filled", "two-rings"):
                        cv2.circle(image, point, 4 * scale, rim, -1 if shape == "filled" else scale)
                    if shape == "two-rings":
                        cv2.circle(image, (24 * scale, 28 * scale), 4 * scale, rim, scale)
                    if shape == "arc":
                        cv2.ellipse(image, point, (4 * scale, 4 * scale), 0, 0, 220, rim, scale)
                    # A near-equal warm highlight crosses the rim and makes its luminance silhouette ambiguous.
                    if shape == "ring":
                        cv2.line(image, (43 * scale, 49 * scale), (43 * scale, 51 * scale), (135, 147, 169), scale)

                    found = DetectionMixin.find_white_navpoint(image, (40 * scale, 40 * scale), 32 * scale)

                    if shape == "ring":
                        assert found is not None
                        self.assertTrue(found[2])
                        self.assertAlmostEqual(found[0], point[0], delta=scale)
                        self.assertAlmostEqual(found[1], point[1], delta=scale)
                    else:
                        self.assertIsNone(found)

    def test_local_panther_markers_keep_position_and_direction(self):
        folder = FIXTURES / "screenshots" / "diagnostics" / "compass"
        for filename, direction in (
            ("panther-filled-hull-reflection-context.png", 1.0),
            ("panther-filled-hull-missed-context.png", 1.0),
            ("panther-hollow-right-rim-context.png", -1.0),
        ):
            with self.subTest(filename=filename):
                path = folder / filename
                if not path.exists():
                    self.skipTest("local Panther compass crop unavailable")
                regions = ScreenRegions(
                    FakeScreen(load_screenshot(path), width=1920, height=1080), ImageTemplates(1080)
                )
                with patch.object(Detector, "dump_compass_debug"):
                    offset = Detector().get_nav_offset(regions)
                self.assertIsNotNone(offset)
                self.assertEqual(offset["z"], direction)
                if direction > 0:
                    self.assertLess(abs(offset["pit"]), 10)
                    self.assertLess(abs(offset["yaw"]), 10)
                else:
                    self.assertGreater(offset["x"], 0.8)

    def test_locally_contrasting_filled_marker_beats_a_broad_hull_reflection(self):
        image = np.zeros((80, 80, 3), np.uint8)
        cv2.ellipse(image, (38, 54), (6, 4), 0, 0, 360, (230, 230, 230), -1)
        cv2.circle(image, (39, 30), 4, (206, 206, 142), -1)
        for scale in (1, 2):
            with self.subTest(scale=scale):
                scaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                found = DetectionMixin.find_white_navpoint(scaled, (40 * scale, 40 * scale), 28 * scale)
                self.assertIsNotNone(found)
                self.assertFalse(found[2])
                self.assertAlmostEqual(found[0] / scale, 39, delta=1)
                self.assertAlmostEqual(found[1] / scale, 30, delta=1)

    def test_filled_fallback_rejects_ambiguous_points(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                image = np.zeros((80, 80, 3), np.uint8)
                for point in ((28, 35), (50, 35)):
                    cv2.circle(image, point, 4, (206, 206, 142), -1)
                scaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                self.assertIsNone(DetectionMixin.find_white_navpoint(scaled, (40 * scale, 40 * scale), 28 * scale))

    def test_local_filled_marker_on_bright_hull_stays_ahead(self):
        path = FIXTURES / "screenshots" / "diagnostics" / "compass" / "anaconda-filled-bright-hull-context.png"
        if not path.exists():
            self.skipTest("local Anaconda bright-hull compass crop unavailable")
        image = load_screenshot(path)
        regions = ScreenRegions(FakeScreen(image, width=1920, height=1080), ImageTemplates(1080))
        with patch.object(Detector, "dump_compass_debug"):
            offset = Detector().get_nav_offset(regions)
        self.assertIsNotNone(offset)
        self.assertEqual(offset["z"], 1.0)
        self.assertLess(abs(offset["pit"]), 30)
        self.assertLess(abs(offset["yaw"]), 30)

    def test_diagonal_hull_texture_is_not_a_round_marker_hole(self):
        image = np.zeros((72, 72, 3), np.uint8)
        texture = np.array(
            [
                [1, 1, 1, 1, 1, 1, 0],
                [1, 1, 1, 1, 1, 1, 0],
                [1, 1, 1, 1, 0, 1, 1],
                [1, 1, 1, 0, 0, 1, 1],
                [1, 1, 0, 0, 1, 1, 1],
                [1, 0, 1, 1, 1, 1, 1],
                [0, 1, 1, 1, 1, 1, 1],
            ],
            np.uint8,
        )
        image[35:42, 40:47] = np.where(texture[:, :, None], 160, 50)
        cv2.circle(image, (28, 36), 3, (160, 160, 160), -1)
        for scale in (1, 2):
            with self.subTest(scale=scale):
                scaled = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                hsv = cv2.cvtColor(scaled, cv2.COLOR_BGR2HSV)
                self.assertIsNone(DetectionMixin._navpoint_ring_hole(hsv, (36 * scale, 36 * scale), 28 * scale))

    def test_incoming_hollow_and_charging_rim_markers_keep_their_direction(self):
        folder = FIXTURES / "screenshots" / "diagnostics" / "incoming-20261116" / "compass"
        available = sorted(folder.glob("*_evaluated.png"))
        if not available:
            self.skipTest("local incoming compass cases unavailable")
        for path in available:
            with self.subTest(screenshot=path.name), patch.object(Detector, "dump_compass_debug"):
                regions = ScreenRegions(FakeScreen(load_screenshot(path), width=3840, height=2160), self.templates)
                offset = Detector().get_nav_offset(regions)
                self.assertIsNotNone(offset)
                self.assertEqual(offset["z"], -1.0 if "hollow" in path.name else 1.0)
                if "hollow" in path.name:
                    self.assertLess(offset["y"], -0.4)
                else:
                    self.assertLess(offset["x"], -0.8)

    def test_local_sdr_ring_markers_at_bottom_keep_position_and_direction(self):
        for filename, expected in (
            ("19_crop_compass-behind-bottom_sdr-hud100_de.png", (34, 51)),
            ("21_crop_compass-behind-bottom-glow_sdr-hud50_de.png", (36, 53)),
        ):
            path = FIXTURES / "screenshots" / "1080p" / filename
            if not path.exists():
                self.skipTest("local SDR compass fixtures unavailable")
            with self.subTest(filename=filename):
                image = load_screenshot(path)
                found = DetectionMixin.find_white_navpoint(image, (33, 32), 28)
                self.assertIsNotNone(found)
                self.assertTrue(found[2])
                self.assertAlmostEqual(found[0], expected[0], delta=2)
                self.assertAlmostEqual(found[1], expected[1], delta=2)

    def test_slightly_saturated_bottom_marker_is_detected_from_live_crop(self):
        path = FIXTURES / "screenshots" / "1080p" / "31_crop_compass-behind-bottom-slightly-saturated_sdr-hud75_de.png"
        if not path.exists():
            self.skipTest("local failed Compass crop unavailable")
        image = load_screenshot(path)
        regions = ScreenRegions(FakeScreen(image, width=1920, height=1080), ImageTemplates(1080))
        with patch.object(Detector, "dump_compass_debug"):
            offset = Detector().get_nav_offset(regions)

        self.assertIsNotNone(offset)
        self.assertEqual(offset["z"], -1.0)
        self.assertLess(offset["y"], -0.5)

    def test_hollow_marker_touching_compass_rim_is_found_at_both_scales(self):
        for radius in (28, 56):
            with self.subTest(radius=radius):
                center = radius + 8
                image = np.zeros((center * 2, center * 2, 3), np.uint8)
                cv2.circle(image, (center, center), radius, (160, 160, 160), 2)
                point = (center, center + radius - round(radius * 0.15))
                marker_radius = round(radius * 0.12)
                cv2.circle(image, point, marker_radius + 1, (160, 160, 160), -1)
                cv2.circle(image, point, max(1, round(radius * 0.04)), (20, 50, 60), -1)
                found = DetectionMixin.find_white_navpoint(image, (center, center), radius)
                self.assertIsNotNone(found)
                self.assertTrue(found[2])
                self.assertAlmostEqual(found[0], point[0], delta=1)
                self.assertAlmostEqual(found[1], point[1], delta=1)

    def test_ring_hole_fallback_rejects_ambiguous_holes(self):
        image = np.zeros((80, 80, 3), np.uint8)
        for point in ((30, 55), (50, 55)):
            cv2.circle(image, point, 4, (160, 160, 160), -1)
            cv2.circle(image, point, 1, (20, 50, 60), -1)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        self.assertIsNone(DetectionMixin._navpoint_ring_hole(hsv, (40, 40), 28))

    def test_dim_markers_keep_position_and_direction_at_both_resolutions(self):
        for radius in (28, 56):
            for value in (160, 195, 255):
                for hollow in (False, True):
                    with self.subTest(radius=radius, value=value, hollow=hollow):
                        center = radius + 8
                        position = (center, center - round(radius * 0.7))
                        image = np.zeros((center * 2, center * 2, 3), np.uint8)
                        cv2.circle(image, (center, center), radius, (0, 60, 120), 2)
                        marker_radius = round(radius * 0.12)
                        cv2.circle(image, position, marker_radius + 1, (0, 65, 130), -1)
                        cv2.circle(image, position, marker_radius, (value, value, value), -1)
                        if hollow:
                            cv2.circle(image, position, max(1, marker_radius - 2), (0, 65, 130), -1)
                        found = DetectionMixin.find_white_navpoint(image, (center, center), radius)
                        self.assertIsNotNone(found)
                        self.assertAlmostEqual(found[0], position[0], delta=1)
                        self.assertAlmostEqual(found[1], position[1], delta=1)
                        self.assertEqual(found[2], hollow)

    def test_colored_compass_lines_and_large_reflections_are_not_markers(self):
        for radius in (28, 56):
            center = radius + 8
            for color in ((0, 100, 200), (180, 150, 0), (180, 180, 180), (206, 206, 142)):
                with self.subTest(radius=radius, color=color):
                    image = np.zeros((center * 2, center * 2, 3), np.uint8)
                    cv2.circle(image, (center, center), radius, color, 2)
                    cv2.line(image, (center - radius, center), (center + radius, center), color, 1)
                    cv2.line(image, (center, center - radius), (center, center + radius), color, 1)
                    self.assertIsNone(DetectionMixin.find_white_navpoint(image, (center, center), radius))

    def test_dim_hollow_marker_is_preferred_to_bright_charging_glow(self):
        for radius in (28, 56):
            center = radius + 8
            image = np.zeros((center * 2, center * 2, 3), np.uint8)
            point = (center, center - round(radius * 0.7))
            marker_radius = round(radius * 0.12)
            cv2.circle(image, (center, center), marker_radius + 1, (255, 255, 255), -1)
            cv2.circle(image, point, marker_radius + 1, (0, 65, 130), -1)
            cv2.circle(image, point, marker_radius, (180, 180, 180), -1)
            cv2.circle(image, point, max(1, marker_radius - 2), (0, 65, 130), -1)
            found = DetectionMixin.find_white_navpoint(image, (center, center), radius)
            self.assertIsNotNone(found)
            self.assertEqual(found[2], True)
            self.assertAlmostEqual(found[1], point[1], delta=1)

    def test_rejected_filled_match_does_not_confirm_a_weak_behind_match(self):
        detector = Detector()
        regions = self.regions("compass")
        image = np.full((150, 150, 3), (160, 160, 0), np.uint8)
        regions.compass_debug_context = None
        regions.match_compass_multiscale = Mock(return_value=(image, (0, 1.0, (0, 0), (5, 5)), 1.0))
        regions.match_template_in_image_x3 = Mock(
            side_effect=[(image, (0, 0.9, (0, 0), (10, 10)), None), (image, (0, 0.5, (0, 0), (20, 20)), None)]
        )
        with (
            patch.object(detector, "find_white_navpoint", return_value=None) as fallback,
            patch.object(detector, "dump_compass_debug"),
        ):
            self.assertIsNone(detector.get_nav_offset(regions))
        fallback.assert_called_once()

    def test_conflicting_weak_template_and_white_blob_directions_are_rejected(self):
        detector = Detector()
        regions = self.regions("compass")
        image = self.images["compass"]
        regions.match_compass_multiscale = Mock(return_value=(image, (0, 0.9, (0, 0), (0, 0)), 1.0))
        regions.match_template_in_image_x3 = Mock(
            side_effect=[
                (image, (0, 0.72, (0, 0), (30, 30)), None),
                (image, (0, 0.78, (0, 0), (40, 40)), None),
            ]
        )
        regions.compass_debug_context = None
        with (
            patch.object(detector, "find_white_navpoint", return_value=(60, 60, False)) as fallback,
            patch.object(detector, "dump_compass_debug"),
        ):
            self.assertIsNone(detector.get_nav_offset(regions))
        fallback.assert_called_once()

    def test_type10_weak_compass_conflict_crop_is_rejected(self):
        path = FIXTURES / "screenshots" / "2160p" / "168_crop_compass-weak-direction-conflict_type10.png"
        if not path.exists():
            self.skipTest("local Type-10 compass regression crop unavailable")
        regions = ScreenRegions(FakeScreen(load_screenshot(path)), self.templates)
        detector = Detector()
        with patch.object(detector, "dump_compass_debug"):
            self.assertIsNone(detector.get_nav_offset(regions))

    def test_hollow_marker_overrides_conflicting_weak_filled_template(self):
        detector = Detector()
        regions = self.regions("compass")
        image = np.full((150, 150, 3), (160, 160, 0), np.uint8)
        regions.compass_debug_context = None
        regions.match_compass_multiscale = Mock(return_value=(image, (0, 0.92, (0, 0), (5, 5)), 1.0))
        regions.match_template_in_image_x3 = Mock(
            side_effect=[
                (image, (0, 0.72, (0, 0), (0, 28)), None),
                (image, (0, 0.53, (0, 0), (110, 68)), None),
            ]
        )
        with (
            patch.object(detector, "find_white_navpoint", return_value=(115, 65, True)),
            patch.object(detector, "dump_compass_debug"),
        ):
            offset = detector.get_nav_offset(regions)
        self.assertIsNotNone(offset)
        self.assertEqual(offset["z"], -1.0)
        self.assertGreater(offset["x"], 0.5)

    def test_type10_hollow_right_rim_marker_remains_usable(self):
        path = FIXTURES / "screenshots" / "diagnostics" / "compass" / "type10-hollow-right-rim-context.png"
        if not path.exists():
            self.skipTest("local Type-10 hollow marker crop unavailable")
        regions = ScreenRegions(FakeScreen(load_screenshot(path)), self.templates)
        with patch.object(Detector, "dump_compass_debug"):
            offset = Detector().get_nav_offset(regions)
        self.assertIsNotNone(offset)
        self.assertEqual(offset["z"], -1.0)
        self.assertGreater(offset["x"], 0.8)

    def test_missing_compass_measurements_are_debug_only(self):
        with self.assertNoLogs("ed_log", level="INFO"):
            self.assertFalse(self.detect("have_destination", "empty"))
        with self.assertLogs("ed_log", level="DEBUG") as logs:
            self.assertFalse(self.detect("have_destination", "empty"))
        measurements = [record for record in logs.records if "Compass not found:" in record.getMessage()]
        self.assertEqual(len(measurements), 1)
        self.assertEqual(measurements[0].levelno, logging.DEBUG)

    def test_compass_is_found(self):
        for name in ("compass", "navpoint", "navpoint-behind"):
            with self.subTest(image=name):
                self.assertTrue(self.detect("have_destination", name))

    def test_no_compass_in_other_screens(self):
        for name in ("empty", "disengage"):
            with self.subTest(image=name):
                self.assertFalse(self.detect("have_destination", name))

    def test_nav_point_ahead(self):
        offset = self.detect("get_nav_offset", "compass")

        self.assertEqual(offset["z"], 1.0)
        self.assertAlmostEqual(offset["x"], 0.0, delta=0.1)
        self.assertAlmostEqual(offset["roll"], 0, delta=15)

    def test_filled_nav_point_in_sun_glare_is_ahead(self):
        offset = self.detect("get_nav_offset", "navpoint")

        self.assertEqual(offset["z"], 1.0)
        self.assertAlmostEqual(offset["x"], 0.17, delta=0.1)
        self.assertGreater(offset["roll"], 90)
        self.assertLess(abs(offset["yaw"]), 20)

    def test_nav_point_behind_at_the_top(self):
        offset = self.detect("get_nav_offset", "navpoint-behind")

        self.assertEqual(offset["z"], -1.0)
        self.assertAlmostEqual(offset["x"], 0.0, delta=0.1)
        self.assertGreater(offset["roll"], 90)
        self.assertGreater(offset["yaw"], 150)

    def test_nav_point_ahead_at_the_side_rim_needs_a_roll(self):
        # Compass crop from a log where the alignment pitched back and forth instead of rolling
        with patch.object(DetectionMixin, "dump_compass_debug"):
            offset = self.detect("get_nav_offset", "navpoint-rim")

        self.assertEqual(offset["z"], 1.0)
        self.assertLess(offset["x"], -0.8)
        self.assertLess(offset["yaw"], -60)
        self.assertTrue(FlightMixin.nav_needs_reroll(offset, 10))

    def test_white_nav_point_touching_a_bright_edge_is_found(self):
        image = np.zeros((120, 120, 3), np.uint8)
        cv2.circle(image, (60, 60), 55, (0, 80, 160), 2)
        cv2.circle(image, (66, 28), 6, (255, 255, 255), -1)
        # A bright hull edge touching the dot merges it into one large blob
        cv2.line(image, (0, 10), (60, 34), (255, 255, 255), 4)

        found = DetectionMixin.find_white_navpoint(image, (60, 60), 55)

        self.assertIsNotNone(found)
        self.assertAlmostEqual(found[0], 66, delta=4)
        self.assertAlmostEqual(found[1], 28, delta=4)
        self.assertFalse(found[2], "filled dot, not a ring")

    def test_offset_values_stay_within_range(self):
        for name in ("compass", "navpoint", "navpoint-behind"):
            with self.subTest(image=name):
                offset = self.detect("get_nav_offset", name)
                self.assertTrue(-1.0 <= offset["x"] <= 1.0)
                self.assertTrue(-1.0 <= offset["y"] <= 1.0)
                self.assertTrue(-180 <= offset["roll"] <= 180)

    def test_no_offset_without_a_visible_compass(self):
        # A guessed position would make the compass alignment steer away from the target
        for name in ("target", "empty"):
            with self.subTest(image=name), patch.object(DetectionMixin, "dump_compass_debug") as dump:
                self.assertIsNone(self.detect("get_nav_offset", name))
                self.assertEqual(dump.call_args.kwargs["kind"], "missing")


class TargetTestCase(MatchingTestCase):
    def test_expanded_recovery_detects_an_upper_target_but_rejects_ambiguous_targets(self):
        for height in (1080, 2160):
            templates = ImageTemplates(height)
            template = templates.template["target"]["image"]
            width = height * 16 // 9
            color = cv2.cvtColor(np.uint8([[[20, 200, 240]]]), cv2.COLOR_HSV2BGR)[0, 0]
            for ambiguous in (False, True):
                with self.subTest(height=height, ambiguous=ambiguous):
                    image = np.zeros((height, width, 3), np.uint8)
                    y = int(height * 0.13)
                    positions = [int(width * 0.35)]
                    if ambiguous:
                        positions.append(int(width * 0.57))
                    for x in positions:
                        area = image[y : y + template.shape[0], x : x + template.shape[1]]
                        area[template > 128] = color
                    regions = ScreenRegions(ScreenshotScreen(image), templates)
                    detector = Detector()
                    self.assertIsNone(detector.get_destination_offset(regions))
                    offset = detector.get_recovery_target_offset(regions)
                    if ambiguous:
                        self.assertIsNone(offset)
                    else:
                        self.assertIsNotNone(offset)
                        self.assertLess(offset["x"], 0)
                        self.assertGreater(offset["y"], 0)

    def test_expanded_recovery_does_not_accept_an_unselected_circle(self):
        image = np.zeros((1080, 1920, 3), np.uint8)
        cv2.circle(image, (800, 200), 50, (0, 200, 240), 2)
        regions = ScreenRegions(ScreenshotScreen(image), ImageTemplates(1080))
        self.assertIsNone(Detector().get_recovery_target_offset(regions))

    def test_incoming_unselected_contact_and_empty_planet_are_not_targets(self):
        folder = FIXTURES / "screenshots" / "diagnostics" / "incoming-20261116" / "target"
        available = sorted(folder.glob("*_evaluated.png"))
        if not available:
            self.skipTest("local incoming target negatives unavailable")
        for path in available:
            with self.subTest(screenshot=path.name):
                regions = ScreenRegions(FakeScreen(load_screenshot(path), width=3840, height=2160), self.templates)
                self.assertIsNone(Detector().get_destination_offset(regions))

    def test_target_crossing_previous_search_top_is_detected(self):
        height, width = 2160, 3840
        image = np.zeros((height, width, 3), np.uint8)
        template = self.templates.template["target"]["image"]
        x = (width - template.shape[1]) // 2
        y = int(0.27 * height) - 25
        color = cv2.cvtColor(np.uint8([[[20, 200, 240]]]), cv2.COLOR_HSV2BGR)[0, 0]
        image[y : y + template.shape[0], x : x + template.shape[1]][template > 128] = color

        old_regions = ScreenRegions(ScreenshotScreen(image), self.templates)
        old_rect = old_regions.reg["target"]["rect"]
        old_rect[1] = int(0.27 * height)
        old_regions.reg["target"]["height"] = old_rect[3] - old_rect[1]
        _, old_result, _ = old_regions.match_target_multiscale()
        self.assertLess(old_result[1], old_regions.target_thresh)

        regions = ScreenRegions(ScreenshotScreen(image), self.templates)
        _, result, selected = regions.match_target_multiscale()
        self.assertGreaterEqual(result[1], regions.target_thresh)
        self.assertIs(selected, template)
        self.assertIsNotNone(Detector().get_destination_offset(regions))

    def test_scaled_target_has_the_correct_circle_center_and_is_not_hidden(self):
        for height in (1080, 2160):
            templates = ImageTemplates(height)
            native = templates.template["target"]
            for scale in (0.95, 1.05):
                target = cv2.resize(native["image"], None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                for hsv in ((20, 200, 240), (40, 200, 240)):
                    with self.subTest(height=height, scale=scale, hsv=hsv):
                        image = np.zeros((700, 700, 3), np.uint8)
                        color = cv2.cvtColor(np.uint8([[hsv]]), cv2.COLOR_HSV2BGR)[0, 0]
                        image[100 : 100 + target.shape[0], 200 : 200 + target.shape[1]][target > 128] = color
                        screen = FakeScreen(image, width=height * 16 // 9, height=height)
                        regions = ScreenRegions(screen, templates)
                        _, result, selected = regions.match_target_multiscale()
                        self.assertGreaterEqual(result[1], regions.target_thresh)
                        self.assertEqual(selected.shape, target.shape)
                        offset = Detector().get_destination_offset(regions)
                        anchor_x, anchor_y = native["anchor"]
                        expected_x = (
                            200
                            + (anchor_x + 0.5) * target.shape[1] / native["width"]
                            - 0.5
                            - (screen.screen_width / 2 - regions.reg["target"]["rect"][0])
                        )
                        expected_y = (
                            screen.screen_height / 2
                            - regions.reg["target"]["rect"][1]
                            - (100 + (anchor_y + 0.5) * target.shape[0] / native["height"] - 0.5)
                        )
                        self.assertAlmostEqual(offset["x"], expected_x, delta=2)
                        self.assertAlmostEqual(offset["y"], expected_y, delta=2)
                        detector = Detector()
                        with patch.object(detector, "have_destination") as have:
                            self.assertFalse(detector.is_destination_hidden_ahead(regions))
                            have.assert_not_called()

    def test_clearly_stronger_scaled_target_wins_even_when_native_passes(self):
        regions = self.regions("target")
        image = np.zeros((700, 700), np.uint8)
        native_result = (0, 0.56, (0, 0), (200, 100))
        with (
            patch.object(regions, "match_template_in_region", return_value=(image, native_result, None)),
            patch(
                "vision.regions.cv2.minMaxLoc",
                side_effect=[
                    (0, 0.4, (0, 0), (201, 102)),
                    (0, 0.83, (0, 0), (202, 103)),
                ],
            ),
        ):
            _, result, selected = regions.match_target_multiscale()
        self.assertEqual(result[1], 0.83)
        self.assertEqual(selected.shape, (201, 174))

    def test_small_scaled_score_advantage_does_not_displace_usable_native_target(self):
        regions = self.regions("target")
        image = np.zeros((700, 700), np.uint8)
        native_result = (0, 0.56, (0, 0), (200, 100))
        with (
            patch.object(regions, "match_template_in_region", return_value=(image, native_result, None)),
            patch(
                "vision.regions.cv2.minMaxLoc",
                side_effect=[
                    (0, 0.57, (0, 0), (201, 102)),
                    (0, 0.58, (0, 0), (202, 103)),
                ],
            ),
        ):
            _, result, selected = regions.match_target_multiscale()
        self.assertEqual(result, native_result)
        self.assertIs(selected, self.templates.template["target"]["image"])

    def test_native_target_keeps_near_ties_and_variants_reuse_one_capture(self):
        regions = self.regions("target")
        with (
            patch.object(regions.screen, "get_screen_region", wraps=regions.screen.get_screen_region) as capture,
            patch("vision.regions.cv2.resize", wraps=cv2.resize) as resize,
        ):
            _, result, selected = regions.match_target_multiscale()
        self.assertGreaterEqual(result[1], regions.target_thresh)
        self.assertIs(selected, self.templates.template["target"]["image"])
        capture.assert_called_once()
        self.assertEqual(resize.call_count, 2)
        regions = self.regions("empty")
        callback = Mock()
        with patch.object(
            regions.screen, "get_screen_region_with_context", wraps=regions.screen.get_screen_region_with_context
        ) as capture:
            with patch("vision.regions.logger.isEnabledFor", return_value=True):
                regions.match_target_multiscale(source_callback=callback)
        capture.assert_called_once()
        callback.assert_called_once()

    def test_local_target_size_variations_are_detected(self):
        folder = FIXTURES / "screenshots" / "2160p"
        names = (
            "151_crop_target-size-variation_en.png",
            "152_crop_target-size-variation_en.png",
            "153_crop_target-size-overlap_en.png",
        )
        available = [name for name in names if (folder / name).exists()]
        if not available:
            self.skipTest("local target size-variation crops unavailable")
        for name in available:
            with self.subTest(screenshot=name):
                path = folder / name
                image = load_screenshot(path)
                regions = ScreenRegions(FakeScreen(image, width=3840, height=2160), self.templates)
                _, native, _ = regions.match_template_in_region("target", "target", inv_col=False)
                self.assertLess(native[1], regions.target_thresh)
                _, result, selected = regions.match_target_multiscale()
                self.assertGreater(result[1], 0.7)
                self.assertEqual(selected.shape, (201, 174))
                self.assertIsNotNone(Detector().get_destination_offset(regions))

    def test_local_target_near_previous_search_top_is_detected(self):
        fixtures = (
            ("169_crop_target-upper-edge_type10_context.png", 0.27),
            ("170_crop_target-upper-edge_type10_context.png", 0.25),
        )
        available = [
            (FIXTURES / "screenshots" / "2160p" / name, source_top)
            for name, source_top in fixtures
            if (FIXTURES / "screenshots" / "2160p" / name).exists()
        ]
        if not available:
            self.skipTest("local upper-edge target context unavailable")

        width, height = 3840, 2160
        old_width = int(0.66 * width) - int(0.33 * width)
        for path, source_top in available:
            with self.subTest(screenshot=path.name):
                context = load_screenshot(path)
                source_top = int(source_top * height)
                source_height = int(0.70 * height) - source_top
                context_left = (context.shape[1] - old_width) // 2
                context_top = (context.shape[0] - source_height) // 2
                old_roi = context[context_top : context_top + source_height, context_left : context_left + old_width]
                old_regions = ScreenRegions(FakeScreen(old_roi, width=width, height=height), self.templates)
                _, old_result, _ = old_regions.match_target_multiscale()
                self.assertLess(old_result[1], old_regions.target_thresh)

                regions = ScreenRegions(FakeScreen(context, width=width, height=height), self.templates)
                rect = regions.reg["target"]["rect"]
                expanded_top = context_top - (source_top - rect[1])
                expanded_height = rect[3] - rect[1]
                target_roi = context[
                    expanded_top : expanded_top + expanded_height, context_left : context_left + old_width
                ]
                self.assertEqual(target_roi.shape[:2], (expanded_height, old_width))

                regions = ScreenRegions(FakeScreen(target_roi, width=width, height=height), self.templates)
                _, result, _ = regions.match_target_multiscale()
                self.assertGreaterEqual(result[1], regions.target_thresh)
                self.assertIsNotNone(Detector().get_destination_offset(regions))

    def test_text_and_unselected_circles_are_not_targets(self):
        for scene in ("text", "circle"):
            with self.subTest(scene=scene):
                image = np.zeros((700, 700, 3), np.uint8)
                if scene == "text":
                    cv2.putText(image, "DYSON ENTERPRISE", (80, 300), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 200, 240), 2)
                else:
                    cv2.circle(image, (300, 300), 100, (0, 200, 240), 2)
                regions = ScreenRegions(FakeScreen(image), self.templates)
                self.assertIsNone(Detector().get_destination_offset(regions))

    def test_target_circle_is_found_and_offset(self):
        offset = self.detect("get_destination_offset", "target")

        self.assertIsNotNone(offset)
        self.assertAlmostEqual(offset["x"], -475.6, delta=5)
        self.assertAlmostEqual(offset["y"], 263.9, delta=5)

    def test_target_match_score_and_offset_are_logged(self):
        with self.assertLogs("ed_log", level="DEBUG") as captured:
            self.detect("get_destination_offset", "target")

        target_log = next(message for message in captured.output if "Target detection:" in message)
        self.assertIn("roi=700x700", target_log)
        self.assertRegex(target_log, r"filter_pixels=\d+/490000 \(\d+\.\d+%\)")
        self.assertRegex(target_log, r"template=\d+x\d+, score=0\.\d{4}, threshold=0\.54")
        self.assertIn("offset x=", target_log)

    def test_target_miss_logs_filter_pixel_count(self):
        with self.assertLogs("ed_log", level="DEBUG") as captured:
            self.detect("get_destination_offset", "empty")

        target_log = next(message for message in captured.output if "Target detection:" in message)
        self.assertIn("filter_pixels=0/490000 (0.00%)", target_log)
        self.assertIn("score=0.0000, threshold=0.54", target_log)
        self.assertIn("(below threshold)", target_log)

    def test_target_debug_saves_the_original_failed_capture(self):
        image = np.full((700, 700, 3), (40, 20, 10), dtype=np.uint8)
        regions = ScreenRegions(FakeScreen(image), self.templates)
        detector = Detector()
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "DEBUG_DIR", Path(folder)):
            with self.assertLogs("ed_log", level="DEBUG") as logs:
                self.assertIsNone(detector.get_destination_offset(regions))
                image[:] = 0
                detector.dump_target_debug()
                detector.dump_target_debug()

            cases = list((Path(folder) / "target").glob("*_evaluated.png"))
            self.assertEqual(len(cases), 1)
            saved = cv2.imread(str(cases[0]))
            self.assertTrue(np.all(saved == (40, 20, 10)))
            context = cv2.imread(str(cases[0].with_name(cases[0].name.replace("_evaluated", "_context"))))
            self.assertTrue(np.all(context == (40, 20, 10)))
            self.assertTrue(any("Vision diagnostic case:" in line for line in logs.output))

    def test_target_debug_is_disabled_outside_debug_and_clears_after_success(self):
        detector = Detector()
        with (
            patch("vision.detection.logger.isEnabledFor", return_value=False),
            patch("vision.diagnostics.cv2.imwrite") as write,
        ):
            self.assertIsNone(detector.get_destination_offset(self.regions("empty")))
            detector.dump_target_debug()
            write.assert_not_called()
        with self.assertLogs("ed_log", level="DEBUG"), patch("vision.diagnostics.cv2.imwrite") as write:
            self.assertIsNone(detector.get_destination_offset(self.regions("empty")))
            self.assertIsNotNone(detector.get_destination_offset(self.regions("target")))
            detector.dump_target_debug()
            write.assert_not_called()

    def test_target_debug_write_failure_is_logged(self):
        detector = Detector()
        with tempfile.TemporaryDirectory() as folder, patch.object(paths, "DEBUG_DIR", Path(folder)):
            with patch("vision.diagnostics.cv2.imwrite", return_value=False):
                with self.assertLogs("ed_log", level="DEBUG") as logs:
                    detector.get_destination_offset(self.regions("empty"))
                    detector.dump_target_debug()
            self.assertTrue(any("Could not save vision diagnostic case" in line for line in logs.output))

    def test_no_target_in_other_screens(self):
        for name in ("empty", "compass", "disengage"):
            with self.subTest(image=name):
                self.assertIsNone(self.detect("get_destination_offset", name))

    def test_target_is_not_occluded(self):
        for name in ("empty", "target", "disengage"):
            with self.subTest(image=name):
                self.assertFalse(self.detect("is_destination_occluded", name))

    def test_occluded_template_is_recognised(self):
        template = self.templates.template["target_occluded"]["image"]
        image = np.zeros((700, 700, 3), np.uint8)
        # The region filter keeps only pixels within the occluded colour range, so draw the template in that colour.
        mask = template > 128
        image[: template.shape[0], : template.shape[1]][mask] = cv2.cvtColor(
            np.uint8([[[20, 120, 180]]]), cv2.COLOR_HSV2BGR
        )[0, 0]
        regions = ScreenRegions(FakeScreen(image), self.templates)

        self.assertTrue(Detector().is_destination_occluded(regions))

    def test_occluded_ring_tolerates_small_size_variations_in_white_and_beige(self):
        for height in (1080, 2160):
            templates = ImageTemplates(height)
            template = templates.template["target_occluded"]["image"]
            for scale in (0.95, 1.05):
                ring = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                for hsv in ((0, 0, 240), (20, 100, 200)):
                    with self.subTest(height=height, scale=scale, hsv=hsv):
                        image = np.zeros((400, 400, 3), np.uint8)
                        color = cv2.cvtColor(np.uint8([[hsv]]), cv2.COLOR_HSV2BGR)[0, 0]
                        image[50 : 50 + ring.shape[0], 60 : 60 + ring.shape[1]][ring > 128] = color
                        regions = ScreenRegions(FakeScreen(image), templates)
                        detector = Detector()
                        with patch.object(detector, "is_destination_hidden_ahead", return_value=False) as hidden:
                            self.assertTrue(detector.is_destination_occluded(regions))
                            hidden.assert_not_called()

    def test_beige_surfaces_do_not_become_occluded_targets(self):
        image = np.zeros((400, 400, 3), np.uint8)
        color = cv2.cvtColor(np.uint8([[[20, 100, 200]]]), cv2.COLOR_HSV2BGR)[0, 0]
        cv2.rectangle(image, (50, 50), (300, 300), color.tolist(), -1)
        detector = Detector()
        with patch.object(detector, "is_destination_hidden_ahead", return_value=False) as hidden:
            self.assertFalse(detector.is_destination_occluded(ScreenRegions(FakeScreen(image), self.templates)))
            hidden.assert_called_once()


class DisengageTestCase(MatchingTestCase):
    def test_gate_loss_saves_the_rejected_frame_and_masks_once_per_transition(self):
        regions = self.regions("message")
        present = regions.screen.image
        rejected = np.full_like(present, (7, 11, 13))
        color = cv2.cvtColor(np.uint8([[[90, 150, 220]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
        cv2.line(rejected, (0, 10), (rejected.shape[1] - 1, 10), color, 1)
        later = np.full_like(present, (20, 30, 40))
        detector = Detector()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(paths, "DEBUG_DIR", Path(folder)),
            patch.object(
                regions.screen, "get_screen_region", side_effect=[present, rejected, later, present, rejected]
            ) as capture,
            self.assertLogs("ed_log", level="DEBUG"),
        ):
            self.assertTrue(detector.sc_disengage_label_up(regions))
            self.assertFalse(detector.sc_disengage_label_up(regions))
            self.assertFalse(detector.sc_disengage_label_up(regions))
            self.assertTrue(detector.sc_disengage_label_up(regions))
            self.assertFalse(detector.sc_disengage_label_up(regions))
            self.assertEqual(capture.call_count, 5)
            cases = list((Path(folder) / "hud-disengage").glob("*_evaluated.png"))
            self.assertEqual(len(cases), 2)
            for case in cases:
                np.testing.assert_array_equal(cv2.imread(str(case)), rejected)
                for name, color_range in (
                    ("mask-bright", regions.message_color_range),
                    ("mask-dim", regions.message_dim_color_range),
                ):
                    mask_path = case.with_name(case.name.replace("_evaluated", f"_{name}"))
                    np.testing.assert_array_equal(
                        cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE),
                        regions.filter_by_color(rejected, color_range),
                    )

    def test_gate_loss_has_no_diagnostic_capture_outside_debug(self):
        detector = Detector()
        regions = self.regions("message")
        with (
            patch("vision.detection.logger.isEnabledFor", return_value=False),
            patch("vision.detection.save_case") as save,
            patch.object(regions.screen, "get_screen_region", wraps=regions.screen.get_screen_region) as capture,
        ):
            self.assertTrue(detector.sc_disengage_label_up(regions))
            regions.screen.image = np.zeros_like(regions.screen.image)
            self.assertFalse(detector.sc_disengage_label_up(regions))
        self.assertEqual(capture.call_count, 2)
        save.assert_not_called()

    def test_message_gate_diagnostics_are_debug_only(self):
        with self.assertNoLogs("ed_log", level="INFO"):
            self.assertTrue(Detector().sc_disengage_label_up(self.regions("message")))
        with self.assertLogs("ed_log", level="DEBUG") as logs:
            self.assertTrue(Detector().sc_disengage_label_up(self.regions("message")))
        self.assertTrue(any("Disengage message check:" in record.getMessage() for record in logs.records))
        self.assertTrue(any("reading it with OCR" in record.getMessage() for record in logs.records))
        self.assertTrue(any("text-like glyph components" in record.getMessage() for record in logs.records))
        self.assertTrue(all(record.levelno == logging.DEBUG for record in logs.records))

    def test_ocr_diagnostics_are_debug_only_and_exit_status_remains_info(self):
        detector = Detector()
        detector.debug_overlay = False
        detector.ocr = Mock()
        detector.locale = Mock()
        detector.locale.all_values.return_value = ["PRESS TO DISENGAGE"]
        detector.ocr.string_similarity.side_effect = lambda expected, actual: float(expected == actual)
        for level in ("INFO", "DEBUG"):
            with self.subTest(level=level):
                detector.ocr.image_simple_ocr.side_effect = [None, [], ["PRESS TO DISENGAGE"]]
                with self.assertLogs("ed_log", level=level) as logs:
                    self.assertTrue(detector.sc_disengage_active(self.regions("empty")))
                info = [record.getMessage() for record in logs.records if record.levelno == logging.INFO]
                self.assertEqual(info, ["Disengage message detected, sending the supercruise exit."])
                if level == "DEBUG":
                    debug = [record.getMessage() for record in logs.records if record.levelno == logging.DEBUG]
                    self.assertTrue(any("OCR returned no text" in message for message in debug))
                    self.assertTrue(any("Disengage similarity" in message for message in debug))

    def test_readable_but_unmatched_ocr_is_saved_once_per_message_gate_event(self):
        detector = Detector()
        detector.debug_overlay = False
        detector.ocr = Mock()
        detector.ocr.image_simple_ocr.return_value = ["1659"]
        detector.ocr.string_similarity.return_value = 0.0
        detector.locale = Mock()
        detector.locale.all_values.return_value = ["PRESS TO DISENGAGE"]
        regions = Mock()
        regions.message_text_present.side_effect = [True, False, True]
        regions.last_message_fraction = 0.02
        regions.last_message_line = 0.3
        regions.message_image.return_value = np.zeros((40, 120, 3), np.uint8)

        with patch("vision.detection.save_case") as save_case, self.assertLogs("ed_log", level="DEBUG"):
            self.assertTrue(detector.sc_disengage_label_up(regions))
            self.assertFalse(detector.sc_disengage_active(regions))
            self.assertFalse(detector.sc_disengage_active(regions))
            self.assertFalse(detector.sc_disengage_label_up(regions))
            self.assertTrue(detector.sc_disengage_label_up(regions))
            self.assertFalse(detector.sc_disengage_active(regions))

        self.assertEqual(save_case.call_count, 2)
        self.assertEqual(
            [call.args[:2] for call in save_case.call_args_list],
            [("hud-disengage", "unmatched"), ("hud-disengage", "unmatched")],
        )

    def test_disengage_message_is_recognised(self):
        self.assertTrue(Detector().sc_disengage_label_up(self.regions("message")))

    def test_empty_screen_has_no_message(self):
        self.assertFalse(self.detect("sc_disengage_label_up", "empty"))

    def test_dim_blue_lettering_with_bright_frame_passes_the_message_gate(self):
        for scale in (1, 2):
            with self.subTest(scale=scale):
                image = np.zeros((100 * scale, 200 * scale, 3), np.uint8)
                frame = cv2.cvtColor(np.uint8([[[100, 200, 220]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
                lettering = cv2.cvtColor(np.uint8([[[106, 200, 150]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
                cv2.line(image, (0, 20 * scale), (150 * scale, 20 * scale), frame, scale)
                for index in range(12):
                    x = (10 + index * 10) * scale
                    cv2.rectangle(image, (x, 35 * scale), (x + 2 * scale, 45 * scale), lettering, -1)
                regions = ScreenRegions(FakeScreen(image), self.templates)
                self.assertTrue(regions.message_text_present())
                self.assertEqual(regions.last_message_row_glyphs, 12)

                image[35 * scale : 46 * scale] = 0
                for index in range(12):
                    x = (10 + index % 4 * 30) * scale
                    y = (35 + index // 4 * 20) * scale
                    cv2.rectangle(image, (x, y), (x + 2 * scale, y + 8 * scale), lettering, -1)
                self.assertFalse(regions.message_text_present())
                self.assertEqual(regions.last_message_row_glyphs, 4)

    def test_bright_prompt_keeps_priority_over_connected_dim_blue_pixels(self):
        image = np.zeros((100, 200, 3), np.uint8)
        bright = cv2.cvtColor(np.uint8([[[90, 200, 255]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
        dim = cv2.cvtColor(np.uint8([[[106, 200, 150]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
        cv2.line(image, (0, 20), (150, 20), bright, 1)
        for index in range(12):
            x = 10 + index * 10
            cv2.rectangle(image, (x, 35), (x + 2, 45), bright, -1)
        cv2.line(image, (10, 40), (122, 40), dim, 1)
        regions = ScreenRegions(FakeScreen(image), self.templates)
        with patch.object(regions.screen, "get_screen_region", wraps=regions.screen.get_screen_region) as capture:
            self.assertTrue(regions.message_text_present())
        self.assertEqual(regions.last_message_row_glyphs, 12)
        capture.assert_called_once()

    def test_local_dim_blue_disengage_prompt_passes_the_message_gate(self):
        path = FIXTURES / "screenshots" / "diagnostics" / "hud-disengage" / "mandalay-dim-blue-disengage_evaluated.png"
        if not path.exists():
            self.skipTest("local dim blue disengage prompt unavailable")
        regions = ScreenRegions(FakeScreen(load_screenshot(path)), self.templates)
        self.assertTrue(regions.message_text_present())
        self.assertGreaterEqual(regions.last_message_row_glyphs, regions.message_row_glyphs_min)

    def test_small_dropout_prompt_passes_the_message_gate(self):
        image = np.zeros((100, 100, 3), np.uint8)
        color = cv2.cvtColor(np.uint8([[[90, 150, 255]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
        for index in range(20):
            x = 5 + index * 4
            cv2.rectangle(image, (x, 30), (x + 1, 40), color, -1)
        regions = ScreenRegions(FakeScreen(image), self.templates)

        self.assertTrue(regions.message_text_present())
        self.assertGreaterEqual(regions.last_message_row_glyphs, regions.message_row_glyphs_min)

    def test_short_prompt_lines_pass_without_a_16_glyph_requirement(self):
        for count in (12, 13, 15, 16):
            with self.subTest(glyphs=count):
                image = np.zeros((100, 100, 3), np.uint8)
                color = cv2.cvtColor(np.uint8([[[90, 150, 255]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
                cv2.line(image, (0, 20), (75, 20), color, 1)
                for index in range(count):
                    x = 5 + index * 5
                    cv2.rectangle(image, (x, 35), (x + 1, 45), color, -1)
                regions = ScreenRegions(FakeScreen(image), self.templates)
                self.assertTrue(regions.message_text_present())
                self.assertEqual(regions.last_message_glyph_components, count)
                self.assertEqual(regions.last_message_row_glyphs, count)

    def test_scattered_readout_with_15_glyphs_does_not_trigger_disengage_ocr(self):
        image = np.zeros((100, 100, 3), np.uint8)
        color = cv2.cvtColor(np.uint8([[[90, 150, 255]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
        cv2.line(image, (0, 20), (30, 20), color, 1)
        for index in range(15):
            x = 5 + (index % 5) * 15
            y = 35 + (index // 5) * 20
            cv2.rectangle(image, (x, y), (x + 1, y + 8), color, -1)
        regions = ScreenRegions(FakeScreen(image), self.templates)
        detector = Detector()
        detector.debug_overlay = False
        detector.ocr = Mock()
        detector.ocr.image_simple_ocr.return_value = []
        detector.ocr.string_similarity.return_value = 0.0
        detector.locale = Mock()
        detector.locale.all_values.return_value = ["PRESS TO DISENGAGE"]

        self.assertFalse(FlightMixin.try_sc_disengage(detector, regions))

        self.assertGreaterEqual(regions.last_message_fraction, regions.message_text_fraction)
        self.assertGreaterEqual(regions.last_message_line, regions.message_line_fraction)
        self.assertEqual(regions.last_message_glyph_components, 15)
        self.assertEqual(regions.last_message_row_glyphs, 5)
        detector.ocr.image_simple_ocr.assert_not_called()

    def test_cockpit_decor_crops_do_not_trigger_disengage_ocr(self):
        crops = (
            "171_crop_no-msg_type10-cockpit-decoration.png",
            "172_crop_no-msg_type10-cockpit-decoration.png",
        )
        folder = FIXTURES / "screenshots" / "2160p"
        available = [folder / name for name in crops if (folder / name).exists()]
        if not available:
            self.skipTest("local cockpit-decoration crops unavailable")

        detector = Detector()
        detector.ocr = Mock()
        for path in available:
            with self.subTest(screenshot=path.name):
                regions = ScreenRegions(FakeScreen(load_screenshot(path)), self.templates)
                self.assertFalse(FlightMixin.try_sc_disengage(detector, regions))
                detector.ocr.image_simple_ocr.assert_not_called()
                self.assertTrue(
                    regions.last_message_glyph_components > regions.message_glyph_components_max
                    or regions.last_message_row_glyphs < regions.message_row_glyphs_min
                )

    def test_scattered_glyphs_do_not_trigger_ocr_even_with_enough_total_components(self):
        image = np.zeros((100, 100, 3), np.uint8)
        color = cv2.cvtColor(np.uint8([[[90, 150, 255]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
        cv2.line(image, (0, 10), (40, 10), color, 1)
        for y in (25, 45, 65):
            for x in range(5, 85, 10):
                cv2.rectangle(image, (x, y), (x + 2, y + 5), color, -1)
        regions = ScreenRegions(FakeScreen(image), self.templates)
        detector = Detector()
        detector.ocr = Mock()
        self.assertFalse(FlightMixin.try_sc_disengage(detector, regions))
        self.assertEqual(regions.last_message_glyph_components, 24)
        self.assertEqual(regions.last_message_row_glyphs, 8)
        detector.ocr.image_simple_ocr.assert_not_called()

    def test_type10_scattered_hud_crops_do_not_trigger_ocr(self):
        folder = FIXTURES / "screenshots" / "diagnostics" / "hud-disengage"
        available = sorted(folder.glob("type10-no-msg-*.png"))
        if not available:
            self.skipTest("local Type-10 HUD readout crops unavailable")
        detector = Detector()
        detector.ocr = Mock()
        for path in available:
            with self.subTest(screenshot=path.name):
                regions = ScreenRegions(FakeScreen(load_screenshot(path)), self.templates)
                self.assertFalse(FlightMixin.try_sc_disengage(detector, regions))
                detector.ocr.image_simple_ocr.assert_not_called()


class FsdAlignmentPromptTestCase(MatchingTestCase):
    def make_detector(self, recognized_text):
        detector = Detector()
        detector.ocr = Mock()
        detector.ocr.image_simple_ocr.return_value = recognized_text
        detector.ocr.string_similarity.side_effect = lambda expected, actual: float(expected == actual)
        detector.locale = Mock()
        detector.locale.all_values.return_value = ["ALIGN WITH TARGET DESTINATION", "AUF ZIEL AUSRICHTEN"]
        return detector

    def test_english_and_german_prompts_are_recognized(self):
        for text in ("ALIGN WITH TARGET DESTINATION", "AUF ZIEL AUSRICHTEN"):
            with self.subTest(text=text):
                detector = self.make_detector([text])
                self.assertTrue(detector.fsd_alignment_prompt(self.regions("empty")))

    def test_unrelated_or_unreadable_text_is_not_a_prompt(self):
        for text in (["PRESS TO DISENGAGE"], None, []):
            with self.subTest(text=text):
                detector = self.make_detector(text)
                self.assertFalse(detector.fsd_alignment_prompt(self.regions("empty")))


class NativeTemplateTestCase(unittest.TestCase):
    def test_native_set_is_loaded_unscaled(self):
        templates = ImageTemplates(2160)

        self.assertEqual((templates.template["compass"]["width"], templates.template["compass"]["height"]), (112, 108))
        self.assertEqual(len(templates.template["target"]["anchor"]), 2)

    def test_lower_heights_scale_the_native_set_down(self):
        templates = ImageTemplates(1080)

        self.assertEqual((templates.template["compass"]["width"], templates.template["compass"]["height"]), (56, 54))
        native = ImageTemplates(2160).template["target"]["anchor"]
        scaled = templates.template["target"]["anchor"]
        self.assertAlmostEqual(scaled[0], native[0] / 2, delta=1.0)
        self.assertAlmostEqual(scaled[1], native[1] / 2, delta=1.0)

    def test_other_heights_are_derived_from_the_native_set(self):
        for height in (540, 1440, 4320):
            with self.subTest(height=height):
                templates = ImageTemplates(height)
                self.assertAlmostEqual(templates.template["compass"]["width"], 112 * height / 2160, delta=1)


class RegionFilterTestCase(MatchingTestCase):
    def test_region_rects_follow_the_screen_size(self):
        regions = ScreenRegions(FakeScreen(None, 1920, 1080), self.templates)

        compass = regions.reg["compass"]
        self.assertEqual(compass["rect"], [576, 702, 883, 1080])
        self.assertEqual((compass["width"], compass["height"]), (307, 378))

    def test_all_regions_lie_inside_the_screen(self):
        for width, height in ((1920, 1080), (3440, 1440), (2560, 1440)):
            regions = ScreenRegions(FakeScreen(None, width, height), self.templates)
            for name, region in regions.reg.items():
                with self.subTest(resolution=f"{width}x{height}", region=name):
                    left, top, right, bottom = region["rect"]
                    self.assertTrue(0 <= left < right <= width)
                    self.assertTrue(0 <= top < bottom <= height)

    def test_equalize_returns_a_grey_image(self):
        result = self.regions("compass").equalize(self.images["compass"])

        self.assertEqual(result.shape, self.images["compass"].shape[:2])

    def test_filter_by_color_keeps_only_pixels_in_range(self):
        regions = self.regions("empty")
        orange = cv2.cvtColor(np.uint8([[[10, 200, 180]]]), cv2.COLOR_HSV2BGR)[0, 0]
        image = np.zeros((10, 10, 3), np.uint8)
        image[:, :5] = orange
        image[:, 5:] = (255, 0, 0)

        mask = regions.filter_by_color(image, regions.orange_color_range)

        self.assertTrue((mask[:, :5] == 255).all())
        self.assertTrue((mask[:, 5:] == 0).all())

    def test_sun_filter_uses_the_threshold(self):
        regions = self.regions("empty")
        image = np.full((4, 4, 3), 100, np.uint8)

        regions.set_sun_threshold(125)
        self.assertEqual(regions.filter_sun(image).sum(), 0)
        regions.set_sun_threshold(50)
        self.assertTrue((regions.filter_sun(image) == 255).all())

    def test_sun_percent(self):
        image = np.zeros((100, 100, 3), np.uint8)
        image[:, :50] = 255
        regions = ScreenRegions(FakeScreen(image), self.templates)

        self.assertEqual(regions.sun_percent(regions.screen), 50)


if __name__ == "__main__":
    unittest.main()
