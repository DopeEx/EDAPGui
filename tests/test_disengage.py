"""Background prompt recognition without live screen capture or PaddleOCR."""

import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from core.types import EdapInterruptError, ScTargetAlignReturn
from routines.assists import AssistsMixin
from routines.flight import FlightMixin
from tests.test_detection import Detector
from vision.disengage import DisengageWorker


class BackgroundDisengageTestCase(unittest.TestCase):
    def setUp(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.worker = DisengageWorker()
        self.ocr = Mock()
        self.ocr.image_simple_ocr.side_effect = self.recognize
        self.ocr.string_similarity.return_value = 1.0
        self.image = np.zeros((20, 40, 3), np.uint8)
        self.target = (123, 2, "Station")

    def tearDown(self):
        self.release.set()
        if self.worker._future is not None:
            self.worker._future.result(timeout=5)

    def recognize(self, image):
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError("Test did not release OCR")
        return ["PRESS TO DISENGAGE"]

    def submit(self):
        self.worker.submit(self.target, self.ocr, ("PRESS TO DISENGAGE",), self.image, self.image)
        self.assertTrue(self.started.wait(5))

    def finish(self):
        self.release.set()
        self.worker._future.result(timeout=5)

    def test_inflight_ocr_does_not_queue_another_frame_and_owns_its_images(self):
        self.submit()
        self.image[:] = 255
        self.worker.submit(self.target, self.ocr, (), self.image, self.image)
        self.assertIsNone(self.worker.poll(self.target))
        self.assertEqual(self.ocr.image_simple_ocr.call_count, 1)
        self.finish()
        reading = self.worker.poll(self.target)
        self.assertTrue(reading.matched)
        self.assertFalse(reading.image.any())

    def test_stop_and_target_changes_discard_late_results(self):
        for change_target in (False, True):
            with self.subTest(change_target=change_target):
                self.started.clear()
                self.release.clear()
                self.submit()
                if change_target:
                    self.assertIsNone(self.worker.poll((123, 3, "Other station")))
                else:
                    self.worker.invalidate()
                self.finish()
                self.assertIsNone(self.worker.poll(self.target))

    def test_obsolete_results_are_not_used(self):
        self.submit()
        self.finish()
        with patch("vision.disengage.time.monotonic", return_value=self.worker._started + 6):
            self.assertIsNone(self.worker.poll(self.target))

    def test_slow_negative_results_remain_available_for_diagnostics(self):
        self.ocr.string_similarity.return_value = 0.1
        flight = self.make_flight()
        flight.sc_disengage_label_up.return_value = False
        with patch("vision.disengage.logger.isEnabledFor", return_value=True):
            self.submit()
            self.finish()
        with (
            patch("vision.disengage.time.monotonic", return_value=self.worker._started + 9),
            patch("vision.detection.save_case") as save,
        ):
            self.assertFalse(flight.try_sc_disengage(self.make_regions()))
        save.assert_called_once()
        self.assertEqual(save.call_args.args[:2], ("hud-disengage", "unmatched"))
        self.assertEqual(set(save.call_args.args[2]), {"masked", "raw", "raw-upscaled"})
        flight.keys.send.assert_not_called()
        self.assertEqual(self.ocr.image_simple_ocr.call_count, 3)

    def test_interrupted_approach_discards_results_before_next_approach(self):
        flight = self.make_flight()

        def interrupted(regions):
            self.submit()
            raise EdapInterruptError()

        flight._sc_assist = interrupted
        with self.assertRaises(EdapInterruptError):
            AssistsMixin.sc_assist(flight, Mock())
        self.assertFalse(flight._disengage_session)
        self.finish()

        def restarted(regions):
            self.assertTrue(flight._disengage_session)
            self.assertFalse(flight.try_sc_disengage(regions))
            flight.keys.send.assert_not_called()
            self.worker._future.result(timeout=5)

        flight._sc_assist = restarted
        regions = self.make_regions()
        AssistsMixin.sc_assist(flight, regions)
        self.assertFalse(flight._disengage_session)
        self.assertIsNone(self.worker.poll(self.target))

    def test_completed_prompt_is_not_used_after_gate_or_supercruise_disappears(self):
        for absent in ("gate", "supercruise"):
            with self.subTest(absent=absent):
                self.started.clear()
                self.release.clear()
                flight = self.make_flight()
                self.submit()
                self.finish()
                if absent == "gate":
                    flight.sc_disengage_label_up.return_value = False
                else:
                    flight.jn.ship_state.return_value = {"status": "in_space"}
                self.assertFalse(flight.try_sc_disengage(self.make_regions()))
                flight.keys.send.assert_not_called()
                self.assertIsNone(self.worker.poll(self.target))

    def test_worker_errors_are_logged_and_propagate(self):
        self.ocr.image_simple_ocr.side_effect = ValueError("inference failed")
        with self.assertLogs("ed_log", level="ERROR"):
            self.worker.submit(self.target, self.ocr, ("prompt",), self.image, self.image)
            future = self.worker._future
            with self.assertRaisesRegex(ValueError, "inference failed"):
                future.result(timeout=5)
        with self.assertRaisesRegex(ValueError, "inference failed"):
            self.worker.poll(self.target)

    def make_flight(self):
        flight = Detector()
        flight._disengage_worker = self.worker
        flight._disengage_session = True
        flight.debug_overlay = False
        flight.ocr = self.ocr
        flight.locale = Mock()
        flight.locale.all_values.return_value = ["PRESS TO DISENGAGE"]
        flight.status = Mock()
        flight.status.get_cleaned_data.return_value = dict(
            zip(("Destination_System", "Destination_Body", "Destination_Name"), self.target)
        )
        flight.jn = Mock()
        flight.jn.ship_state.return_value = {"status": "in_supercruise"}
        flight.sc_disengage_label_up = Mock(return_value=True)
        flight.keys = Mock()
        flight.ap_ckb = Mock()
        flight.stop_sco_monitoring = Mock()
        flight.try_sc_disengage = lambda regions: FlightMixin.try_sc_disengage(flight, regions)
        return flight

    def make_regions(self):
        regions = Mock()
        regions.message_image.return_value = self.image
        regions.filter_by_color.return_value = np.full(self.image.shape[:2], 255, np.uint8)
        return regions

    def test_alignment_continues_while_ocr_is_waiting_and_only_flight_sends_exit(self):
        flight = self.make_flight()
        flight.get_destination_offset = Mock(side_effect=[{"x": 20, "y": 0}, {"x": 0, "y": 0}])
        flight.is_destination_occluded = Mock(return_value=False)
        regions = self.make_regions()

        with patch("routines.flight.sleep"):
            self.assertEqual(FlightMixin.sc_target_align(flight, regions), ScTargetAlignReturn.Found)
        self.assertTrue(self.started.wait(5))
        self.assertEqual(flight.get_destination_offset.call_count, 2)
        flight.keys.send.assert_called_once_with("YawRightButton", hold=0.09)
        self.finish()
        self.assertTrue(flight.try_sc_disengage(regions))
        flight.keys.send.assert_called_with("HyperSuperCombination")
        flight.stop_sco_monitoring.assert_called_once()
