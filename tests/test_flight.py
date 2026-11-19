"""Tests for flight and target-alignment routines."""

import unittest
from unittest.mock import Mock, call, patch

from core.constants import (
    FlagsDocked,
    FlagsFsdCharging,
    FlagsFsdJump,
    FlagsHasLatLong,
    FlagsLanded,
    FlagsSupercruise,
)
from core.types import EdapInterruptError, ScTargetAlignReturn
from routines.flight import FlightMixin


class Keys:
    def __init__(self):
        self.sent = []

    def send(self, key):
        self.sent.append(key)


class RotationProfileTestCase(unittest.TestCase):
    def setUp(self):
        self.flight = FlightMixin()
        self.flight.keys = Mock()
        self.flight.status = Mock()
        self.flight.jn = Mock()
        self.flight.jn.ship_state.return_value = {"type": "anaconda"}
        self.flight.pitchrate = 14.0
        self.flight.rollrate = 42.0  # Custom Supercruise setting
        self.flight.yawrate = 6.0

    def test_six_steering_inputs_switch_profiles_on_dropout_and_reentry(self):
        for in_supercruise, pitch, roll, yaw in (
            (True, 14.0, 42.0, 6.0),
            (False, 25.6, 60.2, 10.5),
            (True, 14.0, 42.0, 6.0),
        ):
            with self.subTest(in_supercruise=in_supercruise):
                flight = self.flight
                flight.status.get_flag.return_value = in_supercruise
                flight.keys.send.reset_mock()

                flight.pitch_up(90)
                flight.pitch_down(90)
                flight.rotate_left(90)
                flight.rotate_right(90)
                flight.yaw_left(90)
                flight.yaw_right(90)

                self.assertEqual(
                    flight.keys.send.call_args_list,
                    [
                        call("PitchUpButton", 90 / pitch),
                        call("PitchDownButton", 90 / pitch),
                        call("RollLeftButton", hold=90 / roll),
                        call("RollRightButton", hold=90 / roll),
                        call("YawLeftButton", hold=90 / yaw),
                        call("YawRightButton", hold=90 / yaw),
                    ],
                )
                flight.status.get_flag.assert_called_with(FlagsSupercruise)

    def test_normal_space_fsd_correction_uses_the_same_profile(self):
        flight = self.flight
        flight.status.get_flag.return_value = False
        flight.measure_fsd_alignment = Mock(return_value=(0.2, 11.0))

        self.assertTrue(flight.correct_fsd_alignment(None))

        flight.keys.send.assert_called_once()
        self.assertEqual(flight.keys.send.call_args.args, ("PitchUpButton",))
        self.assertAlmostEqual(flight.keys.send.call_args.kwargs["hold"], 11.0 * 0.8 / 25.6)

    def test_missing_normal_space_profile_stops_without_a_turn_input(self):
        flight = self.flight
        flight.status.get_flag.return_value = False
        flight.jn.ship_state.return_value = {"type": "unsupported_ship"}
        flight.notify_user = Mock()

        with self.assertLogs("ed_log", level="WARNING"), self.assertRaisesRegex(EdapInterruptError, "unsupported_ship"):
            flight.pitch_up(90)

        flight.keys.send.assert_not_called()
        flight.notify_user.assert_called_once()


class SupercruiseEntryTestCase(unittest.TestCase):
    def flight(self):
        flight = FlightMixin()
        flight.keys = Mock()
        flight.status = Mock()
        flight.status.get_flag.side_effect = lambda flag: flag == FlagsSupercruise
        flight.jn = Mock()
        flight.jn.ship_state.return_value = {"status": "in_supercruise", "cur_station_type": ""}
        flight.set_ap_status = Mock()
        flight.start_sco_monitoring = Mock()
        flight.stop_sco_monitoring = Mock()
        flight.notify_user = Mock()
        flight.pitch_up = Mock()
        return flight

    def test_engage_waits_for_both_status_and_journal_before_reducing_throttle(self):
        for delayed_source in ("status", "journal"):
            with self.subTest(delayed_source=delayed_source):
                flight = self.flight()
                if delayed_source == "status":
                    readings = iter([False, True])
                    flight.status.get_flag.side_effect = lambda flag: (
                        next(readings) if flag == FlagsSupercruise else False
                    )
                else:
                    flight.jn.ship_state.side_effect = [
                        {"status": "starting_supercruise"},
                        {"status": "in_supercruise"},
                    ]

                def waiting(_):
                    self.assertNotIn(call("SetSpeed50"), flight.keys.send.call_args_list)

                with patch("routines.flight.sleep", side_effect=waiting) as sleep:
                    flight.sc_engage()
                sleep.assert_called_once_with(0.25)
                self.assertEqual(
                    flight.keys.send.call_args_list,
                    [call("SetSpeed100"), call("Supercruise", hold=0.001), call("SetSpeed50")],
                )
                flight.notify_user.assert_not_called()

    def test_engage_timeout_stops_instead_of_returning_to_later_flight(self):
        for flag, journal_status in (
            (False, "in_space"),
            (False, "in_supercruise"),
            (True, "starting_supercruise"),
        ):
            with self.subTest(flag=flag, journal_status=journal_status):
                flight = self.flight()
                flight.status.get_flag.side_effect = None
                flight.status.get_flag.return_value = flag
                flight.jn.ship_state.return_value["status"] = journal_status
                with (
                    patch(
                        "routines.flight.time.monotonic",
                        side_effect=[0, 0, 60 if journal_status == "starting_supercruise" else 30],
                    ),
                    patch("routines.flight.sleep"),
                    self.assertLogs("ed_log", level="WARNING"),
                    self.assertRaisesRegex(EdapInterruptError, "supercruise entry"),
                ):
                    flight.sc_engage()
                self.assertEqual(
                    flight.keys.send.call_args_list,
                    [call("SetSpeed100"), call("Supercruise", hold=0.001), call("SetSpeedZero")],
                )
                flight.stop_sco_monitoring.assert_called_once_with()
                flight.notify_user.assert_called_once()

    def planetary_flight(self):
        flight = self.flight()
        flight.status.get_flag.side_effect = lambda flag: (
            flag
            in (
                FlagsHasLatLong,
                FlagsLanded,
                FlagsSupercruise,
            )
        )
        flight.status.wait_for_flag_off.return_value = True
        return flight

    def test_planetary_departure_does_not_enable_sco_when_entry_times_out(self):
        flight = self.planetary_flight()
        flight.jn.ship_state.return_value["status"] = "starting_supercruise"
        with (
            patch("routines.flight.time.monotonic", side_effect=[0, 0, 60]),
            patch("routines.flight.sleep"),
            self.assertLogs("ed_log", level="WARNING"),
            self.assertRaisesRegex(EdapInterruptError, "supercruise entry"),
        ):
            flight.waypoint_undock_seq()
        self.assertNotIn(call("UseBoostJuice"), flight.keys.send.call_args_list)
        self.assertEqual(flight.keys.send.call_args_list[-1], call("SetSpeedZero"))
        self.assertNotIn(call(FlagsHasLatLong, timeout=60), flight.status.wait_for_flag_off.call_args_list)

    def test_orbit_timeout_sends_only_safety_stop_after_wait(self):
        flight = self.planetary_flight()

        def wait(flag, timeout):
            if flag == FlagsHasLatLong:
                flight.keys.send.reset_mock()
                return False
            return True

        flight.status.wait_for_flag_off.side_effect = wait
        with (
            patch("routines.flight.sleep"),
            self.assertLogs("ed_log", level="WARNING"),
            self.assertRaisesRegex(EdapInterruptError, "orbital cruise"),
        ):
            flight.waypoint_undock_seq()
        flight.keys.send.assert_called_once_with("SetSpeedZero")
        flight.stop_sco_monitoring.assert_called_once_with()
        flight.notify_user.assert_called_once()

    def test_confirmed_planetary_departure_preserves_sco_sequence(self):
        flight = self.planetary_flight()
        with patch("routines.flight.sleep"):
            flight.waypoint_undock_seq()
        self.assertEqual(
            flight.keys.send.call_args_list[-3:],
            [call("UseBoostJuice"), call("UseBoostJuice"), call("SetSpeed50")],
        )
        flight.status.wait_for_flag_off.assert_any_call(FlagsHasLatLong, timeout=60)
        flight.notify_user.assert_not_called()

    def test_carrier_departure_confirms_entry_before_configured_flight(self):
        for entered in (True, False):
            with self.subTest(entered=entered):
                flight = self.flight()
                ship = flight.jn.ship_state.return_value
                ship.update(status="in_space", cur_station_type="FleetCarrier", has_adv_dock_comp=True)
                docked = True

                def undock():
                    nonlocal docked
                    docked = False

                def flag_set(flag):
                    return docked if flag == FlagsDocked else entered and flag == FlagsSupercruise

                def send(binding):
                    if binding == "Supercruise" and entered:
                        ship["status"] = "in_supercruise"

                flight.undock = Mock(side_effect=undock)
                flight.status.get_flag.side_effect = flag_set
                flight.keys.send.side_effect = send
                flight.config = {"FCDepartureTime": 17}
                flight.ap_ckb = Mock()
                with patch("routines.flight.sleep") as sleep:
                    if entered:
                        flight.waypoint_undock_seq()
                        sleep.assert_called_once_with(17)
                        self.assertEqual(flight.keys.send.call_args_list[-1], call("SetSpeed50"))
                    else:
                        with (
                            patch("routines.flight.time.monotonic", side_effect=[0, 0, 30]),
                            self.assertLogs("ed_log", level="WARNING"),
                            self.assertRaisesRegex(EdapInterruptError, "supercruise entry"),
                        ):
                            flight.waypoint_undock_seq()
                        self.assertNotIn(call(17), sleep.call_args_list)
                        self.assertEqual(flight.keys.send.call_args_list[-1], call("SetSpeedZero"))
                        flight.ap_ckb.assert_called_once_with("log+vce", "Maneuvering")

    def test_stop_interrupt_is_not_swallowed_while_waiting(self):
        flight = self.flight()
        flight.jn.ship_state.return_value["status"] = "starting_supercruise"
        with (
            patch("routines.flight.sleep", side_effect=EdapInterruptError),
            self.assertRaises(EdapInterruptError),
        ):
            flight.sc_engage()
        self.assertNotIn(call("SetSpeed50"), flight.keys.send.call_args_list)
        flight.notify_user.assert_not_called()

    def test_slow_entry_gets_separate_time_after_fsd_start(self):
        flight = self.flight()
        elapsed = 0.0

        def ship_state():
            return {
                "status": "in_space" if elapsed < 5 else "starting_supercruise" if elapsed < 35 else "in_supercruise"
            }

        def waiting(_):
            nonlocal elapsed
            self.assertNotIn(call("SetSpeed50"), flight.keys.send.call_args_list)
            elapsed += 1

        flight.jn.ship_state.side_effect = ship_state
        flight.status.get_flag.side_effect = lambda flag: flag == FlagsSupercruise and elapsed >= 35
        with (
            patch("routines.flight.time.monotonic", side_effect=lambda: elapsed),
            patch("routines.flight.sleep", side_effect=waiting),
        ):
            flight.sc_engage()
        self.assertEqual(elapsed, 35)
        flight.notify_user.assert_not_called()
        self.assertEqual(flight.keys.send.call_args_list[-1], call("SetSpeed50"))

    def test_repeated_charging_does_not_extend_entry_deadline(self):
        flight = self.flight()
        flight.status.get_flag.side_effect = lambda flag: flag == FlagsFsdCharging
        flight.jn.ship_state.return_value["status"] = "in_space"
        with (
            patch("routines.flight.time.monotonic", side_effect=[0, 29, 59, 89]),
            patch("routines.flight.sleep"),
            self.assertLogs("ed_log", level="WARNING"),
            self.assertRaises(EdapInterruptError),
        ):
            flight.sc_engage()
        self.assertEqual(flight.keys.send.call_args_list[-1], call("SetSpeedZero"))


class TargetAlignment(FlightMixin):
    def __init__(self):
        self.keys = Keys()
        self.stopped_monitoring = False

    def get_destination_offset(self, scr_reg):
        return None

    def sc_disengage_label_up(self, scr_reg):
        return True

    def sc_disengage_active(self, scr_reg):
        return True

    def ap_ckb(self, *args):
        pass

    def stop_sco_monitoring(self):
        self.stopped_monitoring = True


class TargetAlignmentTestCase(unittest.TestCase):
    def test_disengage_is_checked_when_target_is_not_found(self):
        alignment = TargetAlignment()

        with patch("routines.flight.sleep"):
            result = alignment.sc_target_align(None)

        self.assertEqual(result, ScTargetAlignReturn.Disengage)
        self.assertEqual(alignment.keys.sent, ["HyperSuperCombination"])
        self.assertTrue(alignment.stopped_monitoring)

    def test_missing_target_saves_last_failed_capture_after_retries(self):
        flight = Mock()
        flight.get_destination_offset.return_value = None
        flight.try_sc_disengage.return_value = False
        flight.is_destination_occluded.return_value = False
        with patch("routines.flight.sleep"):
            result = FlightMixin.sc_target_align(flight, None)
        self.assertEqual(result, ScTargetAlignReturn.Lost)
        self.assertEqual(flight.get_destination_offset.call_count, 5)
        flight.dump_target_debug.assert_called_once_with()

    def test_target_lost_during_alignment_saves_failed_capture(self):
        flight = Mock()
        flight.get_destination_offset.side_effect = [{"x": 20, "y": 0}, None]
        flight.is_destination_occluded.return_value = False
        flight.try_sc_disengage.return_value = False
        with patch("routines.flight.sleep"):
            result = FlightMixin.sc_target_align(flight, None)
        self.assertEqual(result, ScTargetAlignReturn.Lost)
        flight.dump_target_debug.assert_called_once_with()

    def test_wait_for_disengage_returns_when_requested(self):
        alignment = TargetAlignment()

        with patch("routines.flight.sleep"):
            self.assertTrue(alignment.wait_for_sc_disengage(None, 10))

        self.assertEqual(alignment.keys.sent, ["HyperSuperCombination"])

    def test_wait_for_disengage_times_out_without_message(self):
        alignment = TargetAlignment()
        alignment.sc_disengage_label_up = lambda scr_reg: False
        alignment.get_nav_offset = Mock(
            side_effect=[{"z": -1, "yaw": 170, "pit": 170}, None, {"z": 1, "yaw": 2, "pit": 3}]
        )

        with (
            patch("routines.flight.sleep") as wait,
            patch("routines.flight.time.monotonic", side_effect=[0, 1, 2, 3, 10]),
        ):
            self.assertFalse(alignment.wait_for_sc_disengage(None, 10))

        self.assertEqual(wait.call_count, 3)
        self.assertEqual(alignment.keys.sent, [])

    def test_wait_for_disengage_ends_early_on_repeated_off_course_readings(self):
        for offset in ({"z": -1, "yaw": 170, "pit": 170}, {"z": 1, "yaw": 3, "pit": 18}):
            with self.subTest(offset=offset):
                flight = Mock()
                flight.try_sc_disengage.return_value = False
                flight.get_nav_offset.return_value = offset
                with (
                    patch("routines.flight.sleep") as wait,
                    patch("routines.flight.time.monotonic", side_effect=[0, 1, 2, 10]),
                ):
                    self.assertFalse(FlightMixin.wait_for_sc_disengage(flight, None, 10))

                self.assertEqual(flight.try_sc_disengage.call_count, 2)
                self.assertEqual(flight.get_nav_offset.call_count, 2)
                wait.assert_called_once()


class DiscoveryScanTestCase(unittest.TestCase):
    def test_scan_uses_the_configured_fire_button_and_releases_it_after_confirmation(self):
        flight = Mock()
        flight.status.get_flag.return_value = True
        flight.config = {"DiscoveryScanButton": "Secondary"}
        ship = {"discovery_scans": 0}
        flight.jn.ship_state.return_value = ship
        flight.keys.send.side_effect = lambda _binding, state=None: (
            ship.update(discovery_scans=1) if state == 1 else None
        )

        with patch("routines.flight.time.time", side_effect=[0, 0.1]), patch("routines.flight.sleep"):
            self.assertTrue(FlightMixin.discovery_scan(flight))

        self.assertEqual(
            flight.keys.send.call_args_list,
            [call("SecondaryFire", state=1), call("SecondaryFire", state=0)],
        )

    def test_scan_does_not_send_fire_input_outside_analysis_mode(self):
        flight = Mock()
        flight.status.get_flag.return_value = False

        self.assertFalse(FlightMixin.discovery_scan(flight))

        flight.keys.send.assert_not_called()
        flight.ap_ckb.assert_called_once_with("log", "Not in Analysis Mode. Skipping Discovery Scanner (Honk).")


class JumpRetryTestCase(unittest.TestCase):
    def target_alignment_flight(self):
        flight = FlightMixin()
        flight.keys = Mock()
        flight.status = Mock()
        flight.status.get_flag.return_value = True
        flight.jn = Mock()
        flight.jn.ship_state.return_value = {"status": "in_supercruise"}
        flight.vce = Mock()
        flight.get_destination_offset = Mock()
        flight.rollrate = 80.0
        return flight

    def test_target_align_shortens_holds_near_the_target_when_proportional(self):
        flight = self.target_alignment_flight()
        flight.pitchrate = 40.0
        flight.yawrate = 24.0
        flight.get_destination_offset.side_effect = [{"x": -100, "y": 20}, {"x": 8, "y": 3}]

        with patch("routines.flight.sleep"):
            FlightMixin.fsd_target_align(flight, None, close=10, proportional=True)

        sent = [(call.args[0], call.kwargs["hold"]) for call in flight.keys.send.call_args_list]
        self.assertEqual(sent[0], ("YawLeftButton", 0.15))
        self.assertEqual(sent[1][0], "PitchUpButton")
        self.assertAlmostEqual(sent[1][1], 0.06)
        self.assertEqual(len(sent), 2)

    def test_target_align_keeps_fixed_holds_for_the_reference_ship(self):
        flight = self.target_alignment_flight()
        flight.pitchrate = 40.0
        flight.yawrate = 24.0
        flight.get_destination_offset.side_effect = [{"x": -100, "y": 80}, {"x": 8, "y": 3}]

        with patch("routines.flight.sleep"):
            FlightMixin.fsd_target_align(flight, None, close=50)

        sent = [(call.args[0], call.kwargs["hold"]) for call in flight.keys.send.call_args_list]
        self.assertEqual(sent, [("YawLeftButton", 0.3), ("PitchUpButton", 0.15)])

    def test_target_align_scales_holds_with_ship_rates(self):
        for pitchrate, yawrate, expected_yaw, expected_pitch in (
            (20.0, 12.0, 0.6, 0.3),  # half as agile: twice as long
            (80.0, 48.0, 0.15, 0.075),  # twice as agile: half as long
            (5.0, 3.0, 0.9, 0.45),  # very sluggish: capped at three times the base hold
        ):
            with self.subTest(pitchrate=pitchrate, yawrate=yawrate):
                flight = self.target_alignment_flight()
                flight.pitchrate = pitchrate
                flight.yawrate = yawrate
                flight.get_destination_offset.side_effect = [{"x": -150, "y": 150}, {"x": 8, "y": 3}]

                with patch("routines.flight.sleep"):
                    FlightMixin.fsd_target_align(flight, None, close=50)

                sent = {call.args[0]: call.kwargs["hold"] for call in flight.keys.send.call_args_list}
                self.assertAlmostEqual(sent["YawLeftButton"], expected_yaw)
                self.assertAlmostEqual(sent["PitchUpButton"], expected_pitch)

    def test_target_align_learns_the_step_size_after_an_overshoot(self):
        flight = self.target_alignment_flight()
        flight.pitchrate = 5.0
        flight.yawrate = 3.0  # configured as sluggish, but the ship really moves 160 px per second of hold
        flight.get_destination_offset.side_effect = [{"x": 75, "y": 0}, {"x": -69, "y": 0}, {"x": 5, "y": 0}]

        with patch("routines.flight.sleep"):
            FlightMixin.fsd_target_align(flight, None, close=50)

        sent = [(call.args[0], call.kwargs["hold"]) for call in flight.keys.send.call_args_list]
        self.assertEqual(sent[0][0], "YawRightButton")
        self.assertAlmostEqual(sent[0][1], 0.9)
        self.assertEqual(sent[1][0], "YawLeftButton")
        self.assertAlmostEqual(sent[1][1], 0.8 * 69 / (144 / 0.9))
        self.assertEqual(len(sent), 2)

    def test_first_attempt_alignment_is_not_proportional(self):
        flight = Mock()
        flight.jn.ship_state.return_value = {"status": "in_supercruise"}

        FlightMixin.mnvr_to_target(flight, None)

        flight.fsd_target_align.assert_called_once_with(
            None, close=50, allow_starting_hyperspace=False, proportional=False
        )

    def test_failed_jump_start_realigns_before_retry(self):
        flight = Mock()
        flight.config = {"JumpTries": 2}
        flight.jn.ship_state.side_effect = [
            {"status": "in_space"},
            {"status": "starting_hyperspace"},
        ]
        flight.status.wait_for_flag_on.side_effect = [True, True]
        flight.status.wait_for_flag_off.side_effect = [True, True, True]
        flight.wait_for_fsd_jump_start.side_effect = [False, True]

        with patch("routines.flight.sleep"):
            self.assertTrue(FlightMixin.jump(flight, None))

        flight.mnvr_to_target.assert_called_once_with(None, retry=True)
        self.assertEqual(flight.wait_for_fsd_jump_start.call_count, 2)
        flight.status.wait_for_flag_off.assert_any_call(FlagsFsdJump, 5)
        flight.status.wait_for_flag_off.assert_any_call(FlagsFsdCharging, 5)

    def test_retry_alignment_allows_stale_hyperspace_journal_status(self):
        flight = Mock()
        flight.jn.ship_state.return_value = {"status": "starting_hyperspace"}

        FlightMixin.mnvr_to_target(flight, None, retry=True)

        flight.fsd_target_align.assert_called_once_with(
            None, close=20, allow_starting_hyperspace=True, proportional=True
        )

    def test_journal_jump_flag_takes_priority_over_prompt_check(self):
        flight = Mock()
        flight.status.get_flag.return_value = True

        with patch("routines.flight.time.monotonic", return_value=0):
            self.assertTrue(FlightMixin.wait_for_fsd_jump_start(flight, None))

        flight.fsd_alignment_prompt.assert_not_called()

    def test_alignment_prompt_only_requests_a_small_correction(self):
        flight = Mock()
        flight.status.get_flag.side_effect = [False, True, True]
        flight.fsd_alignment_prompt.return_value = True

        with patch("routines.flight.time.monotonic", side_effect=[0, 0]), patch("routines.flight.sleep"):
            self.assertTrue(FlightMixin.wait_for_fsd_jump_start(flight, None))

        flight.correct_fsd_alignment.assert_called_once_with(None)

    def test_no_prompt_text_preserves_wait_timeout(self):
        flight = Mock()
        flight.status.get_flag.return_value = False
        flight.status.get_flag.side_effect = [False, True, False]
        flight.fsd_alignment_prompt.return_value = False

        with patch("routines.flight.time.monotonic", side_effect=[0, 0, 31]), patch("routines.flight.sleep"):
            self.assertFalse(FlightMixin.wait_for_fsd_jump_start(flight, None))

        flight.correct_fsd_alignment.assert_not_called()

    def test_retry_cancels_running_charge_before_realigning(self):
        flight = Mock()
        flight.config = {"JumpTries": 2}
        flight.jn.ship_state.return_value = {"status": "in_space"}
        flight.status.get_flag.side_effect = lambda flag: flag == FlagsFsdCharging
        flight.status.wait_for_flag_on.return_value = True
        flight.status.wait_for_flag_off.return_value = True
        flight.wait_for_fsd_jump_start.side_effect = [False, True]

        with patch("routines.flight.sleep"):
            self.assertTrue(FlightMixin.jump(flight, None))

        sent = [call.args[0] for call in flight.keys.send.call_args_list]
        self.assertEqual(sent[:3], ["HyperSuperCombination"] * 3)
        flight.mnvr_to_target.assert_called_once_with(None, retry=True)

    def test_compass_reading_rejects_implausible_values(self):
        for nav in (
            None,
            {"z": -1, "yaw": 90, "pit": -90, "roll": 142},
            {"z": 1, "yaw": 2.74, "pit": -24.59, "roll": 174},
        ):
            with self.subTest(nav=nav):
                flight = Mock()
                flight.fsd_behind_readings = 0
                flight.get_nav_offset.return_value = nav
                self.assertIsNone(FlightMixin.read_compass_alignment(flight, None))

    def test_compass_reading_counts_target_behind(self):
        flight = Mock()
        flight.fsd_behind_readings = 0
        flight.get_nav_offset.return_value = {"z": -1, "yaw": -176.0, "pit": -107.0, "roll": -177.0}

        self.assertIsNone(FlightMixin.read_compass_alignment(flight, None))

        self.assertEqual(flight.fsd_behind_readings, 1)

    def test_prompt_with_target_behind_ends_the_wait_for_a_retry(self):
        flight = Mock()
        flight.status.get_flag.side_effect = [False, True, True]
        flight.fsd_alignment_prompt.return_value = True
        flight.correct_fsd_alignment.return_value = False
        flight.fsd_behind_readings = 5

        with patch("routines.flight.time.monotonic", side_effect=[0, 0]), patch("routines.flight.sleep"):
            self.assertFalse(FlightMixin.wait_for_fsd_jump_start(flight, None))

    def test_centered_compass_reading_must_be_confirmed(self):
        centered = {"z": 1, "yaw": 0.0, "pit": 0.0, "roll": 0}
        for readings, expected in (
            ([centered, centered], True),
            ([centered, {"z": -1, "yaw": -176.0, "pit": -107.0, "roll": -177}], False),
            ([centered, None], False),
            ([{"z": 1, "yaw": -0.35, "pit": -25.0, "roll": -106}, centered], False),
        ):
            with self.subTest(readings=readings):
                flight = Mock()
                flight.get_nav_offset.side_effect = readings
                with patch("routines.flight.sleep"):
                    self.assertEqual(FlightMixin.compass_confirmed(flight, None, 10), expected)

    def test_compass_reading_accepts_rolled_but_plausible_values(self):
        flight = Mock()
        flight.get_nav_offset.return_value = {"z": 1, "yaw": 6.39, "pit": 12.4, "roll": 26.98}
        self.assertEqual(FlightMixin.read_compass_alignment(flight, None), (6.39, 12.4))

    def test_measurement_drops_flickering_readings_and_averages_the_rest(self):
        flight = Mock()
        flight.read_compass_alignment.side_effect = [None, (0, 11.0), None, (0.4, 12.0), (30.0, -24.6), None]
        with patch("routines.flight.sleep"):
            self.assertEqual(FlightMixin.measure_fsd_alignment(flight, None), (0.2, 11.5))

    def test_measurement_needs_two_plausible_agreeing_readings(self):
        flight = Mock()
        flight.read_compass_alignment.side_effect = [None, (0, 11.0), None, None, None, None]
        with patch("routines.flight.sleep"):
            self.assertIsNone(FlightMixin.measure_fsd_alignment(flight, None))

        flight.read_compass_alignment.side_effect = [(0, 11.0), (0, -24.6), None, None, None, None]
        with patch("routines.flight.sleep"):
            self.assertIsNone(FlightMixin.measure_fsd_alignment(flight, None))

    def test_correction_pitches_up_proportionally_toward_compass_point(self):
        flight = FlightMixin()
        flight.keys = Mock()
        flight.status = Mock()
        flight.status.get_flag.side_effect = lambda flag: flag == FlagsSupercruise
        flight.measure_fsd_alignment = Mock(return_value=(0.2, 11.0))
        flight.pitchrate = 33.0
        flight.rollrate = 80.0
        flight.yawrate = 8.0

        self.assertTrue(FlightMixin.correct_fsd_alignment(flight, None))

        flight.keys.send.assert_called_once()
        key = flight.keys.send.call_args.args[0]
        hold = flight.keys.send.call_args.kwargs["hold"]
        self.assertEqual(key, "PitchUpButton")
        self.assertAlmostEqual(hold, 11.0 * 0.8 / 33.0)

    def test_correction_yaws_left_and_caps_hold_time(self):
        flight = FlightMixin()
        flight.keys = Mock()
        flight.status = Mock()
        flight.status.get_flag.side_effect = lambda flag: flag == FlagsSupercruise
        flight.measure_fsd_alignment = Mock(return_value=(-20.0, 1.0))
        flight.pitchrate = 33.0
        flight.rollrate = 80.0
        flight.yawrate = 8.0

        FlightMixin.correct_fsd_alignment(flight, None)

        flight.keys.send.assert_called_once_with("YawLeftButton", hold=0.5)

    def test_correction_is_skipped_without_trusted_measurement(self):
        flight = Mock()
        flight.status.get_flag.return_value = False
        flight.measure_fsd_alignment.return_value = None

        self.assertFalse(FlightMixin.correct_fsd_alignment(flight, None))

        flight.keys.send.assert_not_called()

    def test_correction_is_skipped_within_tolerance(self):
        flight = Mock()
        flight.status.get_flag.return_value = False
        flight.measure_fsd_alignment.return_value = (0.5, -1.0)

        self.assertFalse(FlightMixin.correct_fsd_alignment(flight, None))

        flight.keys.send.assert_not_called()

    def test_correction_is_skipped_after_journal_reports_jump_started(self):
        flight = Mock()
        flight.status.get_flag.return_value = True

        self.assertFalse(FlightMixin.correct_fsd_alignment(flight, None))

        flight.measure_fsd_alignment.assert_not_called()
        flight.keys.send.assert_not_called()


class CompassAlignTestCase(unittest.TestCase):
    def flight(self, readings, tries=1):
        flight = Mock()
        flight.config = {"NavAlignTries": tries}
        flight.jn.ship_state.return_value = {"status": "in_supercruise"}
        flight.read_nav_offset.side_effect = readings
        flight.nav_needs_reroll = FlightMixin.nav_needs_reroll
        return flight

    def test_no_steering_while_the_compass_is_not_visible(self):
        flight = self.flight([None], tries=3)
        flight.get_destination_offset.return_value = None

        with (
            patch("routines.flight.sleep"),
            self.assertLogs("ed_log", level="WARNING"),
            self.assertRaisesRegex(RuntimeError, "target alignment unconfirmed"),
        ):
            FlightMixin.nav_align(flight, None)

        for maneuver in ("pitch_up", "pitch_down", "yaw_left", "yaw_right", "rotate_left", "rotate_right"):
            getattr(flight, maneuver).assert_not_called()
        flight.keys.send.assert_called_once_with("SetSpeedZero")
        flight.notify_user.assert_called_once()

    def test_unconfirmed_centered_reading_requires_a_visible_target_ring(self):
        for target in (None, {"x": 20, "y": -10}):
            with self.subTest(target=target):
                flight = self.flight([{"z": 1, "yaw": 0, "pit": 0}])
                flight.compass_confirmed.return_value = False
                flight.get_destination_offset.return_value = target
                with patch("routines.flight.sleep"), self.assertLogs("ed_log", level="WARNING"):
                    if target is None:
                        with self.assertRaisesRegex(RuntimeError, "target alignment unconfirmed"):
                            FlightMixin.nav_align(flight, None)
                        flight.keys.send.assert_called_once_with("SetSpeedZero")
                    else:
                        self.assertFalse(FlightMixin.nav_align(flight, None))
                        flight.keys.send.assert_not_called()
                flight.get_destination_offset.assert_called_once_with(None)

    def test_confirmed_compass_does_not_require_a_visible_target_ring(self):
        flight = self.flight([{"z": 1, "yaw": 0, "pit": 0}])
        flight.compass_confirmed.return_value = True
        with patch("routines.flight.sleep"):
            self.assertFalse(FlightMixin.nav_align(flight, None))
        flight.get_destination_offset.assert_not_called()

    def test_missing_compass_can_hand_off_to_a_visible_target_ring(self):
        flight = self.flight([None])
        flight.get_destination_offset.return_value = {"x": 20, "y": -10}
        with patch("routines.flight.sleep"), self.assertLogs("ed_log", level="WARNING"):
            self.assertFalse(FlightMixin.nav_align(flight, None))
        flight.get_destination_offset.assert_called_once_with(None)
        flight.keys.send.assert_not_called()

    def test_last_steering_attempt_also_requires_compass_confirmation(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                flight = self.flight(
                    [
                        {"z": 1, "x": 0, "y": 0.3, "yaw": 0, "pit": 20, "roll": 0},
                        {"z": 1, "x": 0, "y": 0, "yaw": 0, "pit": 0, "roll": 0},
                    ]
                )
                flight.compass_confirmed.return_value = confirmed
                flight.get_destination_offset.return_value = None
                with patch("routines.flight.sleep"):
                    if confirmed:
                        self.assertFalse(FlightMixin.nav_align(flight, None))
                        flight.get_destination_offset.assert_not_called()
                    else:
                        with (
                            self.assertLogs("ed_log", level="WARNING"),
                            self.assertRaisesRegex(RuntimeError, "target alignment unconfirmed"),
                        ):
                            FlightMixin.nav_align(flight, None)
                flight.compass_confirmed.assert_called_once_with(None, 10)
                flight.pitch_up.assert_called_once_with(20)

    def test_disengage_request_still_ends_compass_alignment_immediately(self):
        flight = self.flight([])
        flight.try_sc_disengage.return_value = True
        with patch("routines.flight.sleep"):
            self.assertTrue(FlightMixin.nav_align(flight, None, abort_on_disengage=True))
        flight.read_nav_offset.assert_not_called()
        flight.get_destination_offset.assert_not_called()

    def test_alignment_is_available_after_a_docking_denial(self):
        flight = self.flight([{"z": 1, "yaw": 0, "pit": 0}])
        flight.jn.ship_state.return_value = {"status": "dockingdenied"}
        flight.compass_confirmed.return_value = True
        with patch("routines.flight.sleep"):
            FlightMixin.nav_align(flight, None)
        flight.compass_confirmed.assert_called_once_with(None, 10)

    def test_lost_nav_point_stops_the_maneuver_instead_of_guessing(self):
        flight = self.flight(
            [
                {"z": 1, "x": 0.0, "y": 0.5, "roll": 0.0, "pit": 30.0, "yaw": 0.0},
                None,
                None,
            ],
            tries=2,
        )

        with patch("routines.flight.sleep"), self.assertLogs("ed_log", level="WARNING"):
            FlightMixin.nav_align(flight, None)

        flight.pitch_up.assert_called_once_with(30.0)
        flight.yaw_left.assert_not_called()
        flight.yaw_right.assert_not_called()

    def test_nav_point_far_to_the_side_is_rolled_up_instead_of_pitched(self):
        # Readings from a log: after the first pitch the nav point sat at the left sphere rim, and the old
        # alignment pitched up and down six times while the point stayed there.
        flight = self.flight(
            [
                {"z": 1, "x": 0.04, "y": -0.37, "roll": 173.31, "pit": -21.79, "yaw": 2.68},
                {"z": 1, "x": -0.9, "y": 0.19, "roll": -78.0, "pit": 26.0, "yaw": -66.5},
                {"z": 1, "x": 0.0, "y": 0.9, "roll": 0.0, "pit": 64.0, "yaw": 0.0},
                {"z": 1, "x": 0.0, "y": 0.02, "roll": 0.0, "pit": 2.0, "yaw": 1.0},
            ]
        )

        with patch("routines.flight.sleep"):
            FlightMixin.nav_align(flight, None)

        flight.pitch_down.assert_called_once_with(21.79)
        flight.rotate_left.assert_called_once_with(78.0)
        flight.pitch_up.assert_called_once_with(64.0)
        flight.yaw_left.assert_not_called()
        flight.yaw_right.assert_not_called()

    def test_reroll_only_for_a_nav_point_ahead_far_off_the_vertical_axis(self):
        for off, expected in (
            ({"z": 1, "roll": -78.0, "yaw": -66.5}, True),
            ({"z": 1, "roll": -78.0, "yaw": -20.0}, False),  # close enough for the yaw step
            ({"z": 1, "roll": 175.0, "yaw": 40.0}, False),  # already on the vertical axis
            ({"z": -1, "roll": 60.0, "yaw": 160.0}, False),  # behind: the pitch over the top handles it
        ):
            with self.subTest(off=off):
                self.assertEqual(FlightMixin.nav_needs_reroll(off, 10), expected)

    def test_reading_waits_for_the_compass_to_reappear(self):
        reading = {"z": 1, "x": 0.0, "y": 0.0, "roll": 0.0, "pit": 0.0, "yaw": 0.0}
        flight = Mock()
        flight.get_nav_offset.side_effect = [None, None, reading]

        with patch("routines.flight.sleep") as sleep:
            self.assertEqual(FlightMixin.read_nav_offset(flight, None), reading)

        self.assertEqual(sleep.call_count, 2)

    def test_reading_gives_up_when_the_compass_stays_invisible(self):
        flight = Mock()
        flight.get_nav_offset.return_value = None

        with patch("routines.flight.sleep"):
            self.assertIsNone(FlightMixin.read_nav_offset(flight, None))

        self.assertEqual(flight.get_nav_offset.call_count, 4)


class DockStatusTestCase(unittest.TestCase):
    def test_docking_reports_each_step_and_still_restocks(self):
        ship = {"status": "in_space", "no_dock_reason": None}
        statuses = []
        dock = FlightMixin.__new__(FlightMixin)
        dock.config = {"DockingRetries": 1, "WaitForAutoDockTimer": 3}
        dock.jn = Mock(ship_state=Mock(return_value=ship))
        dock.keys = Mock()
        dock.vce = Mock()
        dock.ap_ckb = Mock()
        dock.compass_confirmed = Mock(return_value=True)
        dock.set_ap_status = statuses.append

        def request_docking():
            ship["status"] = "dockinggranted"

        def sleep(_seconds):
            if statuses and statuses[-1] == "Docking":
                ship["status"] = "in_station"

        dock.request_docking = request_docking
        with patch("routines.flight.sleep", side_effect=sleep):
            dock.dock(None)

        self.assertEqual(statuses, ["Approaching target", "Requesting docking", "Docking", "Refueling and repairing"])
        sent = [c.args[0] for c in dock.keys.send.call_args_list]
        self.assertEqual(sent.count("UI_Select"), 3)

    def make_dock(self):
        dock = FlightMixin()
        dock.keys = Mock()
        dock.jn = Mock()
        dock.jn.ship_state.return_value = {"status": "in_space", "no_dock_reason": None}
        dock.set_ap_status = Mock()
        dock.notify_user = Mock()
        dock.nav_align = Mock(return_value=False)
        dock.compass_confirmed = Mock(return_value=True)
        dock.request_docking = Mock()
        return dock

    def test_centered_target_needs_no_turn_before_approach(self):
        dock = self.make_dock()
        with patch("routines.flight.sleep"):
            dock._align_for_docking(None)
        dock.keys.send.assert_called_once_with("SetSpeedZero", repeat=2)
        dock.nav_align.assert_not_called()
        dock.set_ap_status.assert_called_once_with("Approaching target")

    def test_turn_is_followed_by_confirmation_before_forward_throttle(self):
        dock = self.make_dock()
        dock.compass_confirmed.side_effect = [False, True]
        with patch("routines.flight.sleep"):
            dock._align_for_docking(None)
        dock.nav_align.assert_called_once_with(None)
        self.assertEqual(dock.compass_confirmed.call_count, 2)
        self.assertEqual(dock.set_ap_status.call_args_list, [call("Align"), call("Approaching target")])
        self.assertNotIn(call("SetSpeed50"), dock.keys.send.call_args_list)

    def test_unconfirmed_alignment_or_flight_state_blocks_approach_and_docking(self):
        for state, confirmed in (("in_space", False), ("in_supercruise", True)):
            with self.subTest(state=state):
                dock = self.make_dock()
                dock.jn.ship_state.return_value["status"] = state
                dock.compass_confirmed.return_value = confirmed
                with patch("routines.flight.sleep"), self.assertRaises(RuntimeError):
                    dock.dock(None)
                self.assertNotIn(call("SetSpeed50"), dock.keys.send.call_args_list)
                dock.request_docking.assert_not_called()
                dock.notify_user.assert_called_once()

    def test_distance_denial_rechecks_alignment_before_further_approach(self):
        dock = self.make_dock()
        dock.config = {"DockingRetries": 1}
        dock.vce = Mock()
        dock.compass_confirmed.side_effect = [True, False, False]

        def deny():
            dock.jn.ship_state.return_value.update(status="dockingdenied", no_dock_reason="Distance")

        dock.request_docking.side_effect = deny
        with patch("routines.flight.sleep"), self.assertRaisesRegex(RuntimeError, "alignment"):
            dock.dock(None)
        dock.request_docking.assert_called_once_with()
        dock.nav_align.assert_called_once_with(None)
        self.assertEqual(dock.keys.send.call_args_list.count(call("SetSpeed50")), 1)

    def test_compass_confirmation_rejects_rear_or_missing_target(self):
        for reading in (None, {"z": -1, "yaw": 0, "pit": 0}, {"z": 1, "yaw": 20, "pit": 0}):
            with self.subTest(reading=reading):
                dock = self.make_dock()
                dock.get_nav_offset = Mock(return_value=reading)
                with patch("routines.flight.sleep"):
                    self.assertFalse(FlightMixin.compass_confirmed(dock, None, 10))
