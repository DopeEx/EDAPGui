"""Tests for the end-of-route decision in the assists."""

import unittest
from unittest.mock import Mock, call, patch

from core.constants import (
    FlagsAnalysisMode,
    FlagsCargoScoopDeployed,
    FlagsFsdCharging,
    FlagsFsdMassLocked,
    FlagsHardpointsDeployed,
    FlagsInMainShip,
    FlagsLandingGearDown,
    FlagsSupercruise,
)
from core.types import EdapInterruptError, FsdAssistResult, ScTargetAlignReturn
from routines.afk_combat import AfkCombat
from routines.assists import AFK_COMBAT_POLL_INTERVAL, AssistsMixin
from routines.flight import FlightMixin


class Assists(AssistsMixin):
    def __init__(self, compass_readings):
        self.have_destination = Mock(side_effect=compass_readings)
        self.travel_assist_enabled = True


class AfkCombatLoopTestCase(unittest.TestCase):
    def test_polls_once_per_interval_and_reports_each_phase(self):
        ship_states = [
            {"status": "in_space", "shieldsup": True, "fighter_destroyed": False},
            {"status": "in_space", "shieldsup": True, "fighter_destroyed": True},
            {"status": "in_space", "shieldsup": False, "fighter_destroyed": False},
        ]
        assists = Assists([])
        assists.jn = Mock()
        assists.jn.ship_state.side_effect = ship_states
        assists.afk_combat = AfkCombat(Mock(), assists.jn)
        assists.afk_combat.evade = Mock(return_value=True)
        assists.afk_combat.launch_fighter = Mock()
        assists.set_ap_status = Mock()
        assists.vce = Mock()
        launch_statuses = []
        assists.afk_combat.launch_fighter.side_effect = lambda: (
            launch_statuses.append(assists.set_ap_status.call_args.args[0]) or True
        )

        with (
            patch("routines.assists.sleep") as sleep,
            patch("routines.assists.set_focus_elite_window"),
            patch("routines.assists.logger") as logger,
        ):
            assists.afk_combat_loop()

        self.assertEqual(assists.jn.ship_state.call_count, len(ship_states))
        self.assertEqual(
            assists.set_ap_status.call_args_list,
            [
                call("Monitoring shields"),
                call("Deploying fighter", speak=True),
                call("Monitoring shields"),
                call("Evading", speak=True),
                call("Deploying fighter", speak=True),
            ],
        )
        self.assertEqual(launch_statuses, ["Deploying fighter", "Deploying fighter"])
        self.assertEqual(sleep.call_args_list, [call(AFK_COMBAT_POLL_INTERVAL)] * 2)
        self.assertFalse(ship_states[1]["fighter_destroyed"])
        assists.afk_combat.evade.assert_called_once_with()
        self.assertEqual(assists.afk_combat.launch_fighter.call_count, 2)
        assists.vce.say.assert_not_called()
        logger.debug.assert_any_call(
            "AFK combat started: status=%s, shieldsup=%s, fighter_destroyed=%s", "in_space", True, False
        )
        logger.debug.assert_any_call("AFK combat: fighter destroyed, starting redeployment")
        logger.debug.assert_any_call("AFK combat: shields down, starting evasion")
        logger.debug.assert_any_call("AFK combat: finished after evasion and fighter deployment sequence")

    def test_failed_evasion_or_launch_notifies_and_does_not_resume(self):
        for failure in ("evade", "launch_fighter", "redeploy"):
            with self.subTest(failure=failure):
                assists = Assists([])
                assists.jn = Mock()
                assists.jn.ship_state.return_value = {
                    "status": "in_space",
                    "shieldsup": failure == "redeploy",
                    "fighter_destroyed": failure == "redeploy",
                }
                assists.afk_combat = AfkCombat(Mock(), assists.jn)
                assists.afk_combat.evade = Mock(return_value=failure != "evade")
                assists.afk_combat.launch_fighter = Mock(return_value=False)
                assists.afk_combat.failure_reason = "Confirmation timed out."
                assists.set_ap_status = Mock()
                assists.notify_user = Mock()
                assists.vce = Mock()
                with patch("routines.assists.set_focus_elite_window"):
                    assists.afk_combat_loop()
                assists.notify_user.assert_called_once_with("Confirmation timed out.", speak=True)
                assists.vce.say.assert_not_called()
                if failure == "evade":
                    assists.afk_combat.launch_fighter.assert_not_called()


class DiscoveryScannerAssistTestCase(unittest.TestCase):
    def make_assist(self, ship_states, analysis_mode, scan_results):
        assists = Assists([])
        assists.jn = Mock()
        assists.jn.ship_state.side_effect = ship_states
        assists.status = Mock()
        assists.status.get_flag.side_effect = analysis_mode
        assists.discovery_scan = Mock(side_effect=scan_results)
        assists.set_ap_status = Mock()
        assists.ap_ckb = Mock()
        assists._prev_discovery_scan_system = None
        assists._discovery_scan_attempt_system = None
        return assists

    def test_scan_skips_without_analysis_mode_then_runs_when_mode_is_enabled(self):
        system = {"status": "in_supercruise", "cur_star_system": "Sol"}
        assists = self.make_assist([system, system], [False, True], [True])

        with (
            patch("routines.assists.sleep", side_effect=[None, None, EdapInterruptError()]),
            patch("routines.assists.set_focus_elite_window"),
            self.assertRaises(EdapInterruptError),
        ):
            assists.discovery_scan_assist()

        assists.discovery_scan.assert_called_once_with()
        self.assertEqual(
            assists.status.get_flag.call_args_list,
            [call(FlagsAnalysisMode), call(FlagsAnalysisMode)],
        )
        self.assertEqual(assists._prev_discovery_scan_system, "Sol")

    def test_failed_scan_is_not_recorded_and_retries_after_leaving_supercruise(self):
        states = [
            {"status": "in_supercruise", "cur_star_system": "Sol"},
            {"status": "in_supercruise", "cur_star_system": "Sol"},
            {"status": "in_space", "cur_star_system": "Sol"},
            {"status": "in_supercruise", "cur_star_system": "Sol"},
        ]
        assists = self.make_assist(states, [True, True], [False, True])

        with (
            patch("routines.assists.sleep", side_effect=[None] * 4 + [EdapInterruptError()]),
            patch("routines.assists.set_focus_elite_window"),
            self.assertRaises(EdapInterruptError),
        ):
            assists.discovery_scan_assist()

        self.assertEqual(assists.discovery_scan.call_count, 2)
        self.assertEqual(assists._prev_discovery_scan_system, "Sol")
        self.assertEqual(
            assists.set_ap_status.call_args_list,
            [call("Discovery Scan"), call("Idle"), call("Discovery Scan"), call("Idle")],
        )


class AfkCombatActionsTestCase(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.keys = Mock()
        self.journal = Mock()
        self.status = Mock()
        self.status.get_cleaned_data.return_value = {"Flags": FlagsInMainShip}
        self.combat = AfkCombat(self.keys, self.journal, status=self.status)
        self.power_patch = patch.object(self.combat, "_set_power", return_value=True)
        self.set_power = self.power_patch.start()
        self.addCleanup(self.power_patch.stop)
        self.clock_patch = patch("routines.afk_combat.monotonic", side_effect=lambda: self.now)
        self.sleep_patch = patch("routines.afk_combat.sleep", side_effect=self.advance)
        self.clock_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.sleep_patch.stop)

    def advance(self, seconds):
        self.now += seconds

    def test_evasion_retracts_only_deployed_items_and_waits_through_charging(self):
        for deployed in (0, FlagsHardpointsDeployed | FlagsLandingGearDown | FlagsCargoScoopDeployed):
            with self.subTest(deployed=deployed):
                self.keys.reset_mock()
                self.now = 0.0
                self.status.get_cleaned_data.side_effect = [
                    {"Flags": FlagsInMainShip | deployed},
                    {"Flags": FlagsInMainShip},
                    {"Flags": FlagsInMainShip | FlagsFsdCharging},
                    {"Flags": FlagsInMainShip | FlagsFsdCharging},
                    {"Flags": FlagsInMainShip | FlagsSupercruise},
                    {"Flags": FlagsInMainShip | FlagsSupercruise},
                    {"Flags": FlagsInMainShip | FlagsSupercruise},
                    {"Flags": FlagsInMainShip},
                ]
                self.assertTrue(self.combat.evade())
                self.assertEqual(self.set_power.call_args_list[-2:], [call("engine"), call("system")])
                sent = [item.args[0] for item in self.keys.send.call_args_list]
                self.assertNotIn("HyperSuperCombination", sent)
                self.assertEqual(sent.count("Supercruise"), 2)  # Entry and confirmed-exit request.
                for binding in ("DeployHardpointToggle", "LandingGearToggle", "ToggleCargoScoop"):
                    self.assertEqual(sent.count(binding), bool(deployed))

    def test_aborted_charge_is_retried_without_repeated_commands_while_charging(self):
        def current_status(**kwargs):
            if self.now < 3:
                flags = FlagsInMainShip
            elif self.now < 13:
                flags = FlagsInMainShip | FlagsFsdCharging
            elif self.now < 23:
                flags = FlagsInMainShip
            elif self.now < 55:
                flags = FlagsInMainShip | FlagsSupercruise
            else:
                flags = FlagsInMainShip
            return {"Flags": flags}

        self.status.get_cleaned_data.side_effect = current_status
        commands = []
        self.keys.send.side_effect = lambda binding, **kwargs: commands.append((binding, self.now))
        self.assertTrue(self.combat.evade())
        entry_times = [time for binding, time in commands if binding == "Supercruise"][:-1]
        self.assertEqual(entry_times, [2.0, 22.0])

    def test_failed_start_logs_once_and_later_charging_is_detected_without_changing_retries(self):
        def current_status(**kwargs):
            flags = FlagsInMainShip
            if 13 <= self.now < 23:
                flags |= FlagsFsdCharging
            elif 23 <= self.now < 55:
                flags |= FlagsSupercruise
            return {"Flags": flags}

        self.status.get_cleaned_data.side_effect = current_status
        commands = []
        self.keys.send.side_effect = lambda binding, **kwargs: commands.append((binding, self.now))
        with patch("routines.afk_combat.logger") as logger:
            self.assertTrue(self.combat.evade())
        self.assertEqual([time for binding, time in commands if binding == "Supercruise"][:-1], [2.0, 12.0])
        diagnostic_calls = [
            item
            for item in logger.debug.call_args_list
            if item.args[0].startswith(("AFK combat: no FSD charging", "AFK combat: FSD charging/jump"))
        ]
        self.assertEqual(
            diagnostic_calls,
            [
                call(
                    "AFK combat: no FSD charging detected after supercruise attempt %s; "
                    "retry window reached, cause unknown",
                    1,
                ),
                call("AFK combat: FSD charging/jump detected for supercruise attempt %s", 2),
            ],
        )

    def test_entry_timeout_keeps_boosting_but_never_starts_while_mass_locked(self):
        self.status.get_cleaned_data.return_value = {"Flags": FlagsInMainShip | FlagsFsdMassLocked}
        self.assertFalse(self.combat.evade())
        self.assertEqual(self.now, 120.0)
        sent = [item.args[0] for item in self.keys.send.call_args_list]
        self.assertGreater(sent.count("UseBoostJuice"), 1)
        self.assertNotIn("Supercruise", sent)
        self.assertNotIn("SetSpeedZero", sent)
        self.assertIn("120 seconds", self.combat.failure_reason)

    def test_exit_timeout_does_not_set_post_escape_pips_or_deploy_fighter(self):
        self.status.get_cleaned_data.side_effect = [
            {"Flags": FlagsInMainShip},
            {"Flags": FlagsInMainShip | FlagsSupercruise},
            {"Flags": FlagsInMainShip | FlagsSupercruise},
        ] + [{"Flags": FlagsInMainShip | FlagsSupercruise}] * 61
        self.assertFalse(self.combat.evade())
        self.assertEqual(self.now, 63.0)
        self.assertIn("exit", self.combat.failure_reason)
        self.assertNotIn("IncreaseWeaponsPower", [item.args[0] for item in self.keys.send.call_args_list])

    def test_fighter_requires_fresh_npc_launch_not_a_historical_event(self):
        for pilot in (False, True, None):
            with self.subTest(pilot=pilot):
                self.journal.ship_state.side_effect = [
                    {"fighter_launch_sequence": 5, "fighter_launch_player_controlled": False},
                    {"fighter_launch_sequence": 5, "fighter_launch_player_controlled": False},
                    {"fighter_launch_sequence": 6, "fighter_launch_player_controlled": pilot},
                ]
                self.assertEqual(self.combat.launch_fighter(), pilot is False)
                if pilot is not False:
                    self.assertIn("NPC-controlled", self.combat.failure_reason)

    def test_fighter_confirmation_timeout_does_not_retry_menu_inputs(self):
        self.journal.ship_state.return_value = {"fighter_launch_sequence": 5, "fighter_launch_player_controlled": False}
        self.assertFalse(self.combat.launch_fighter())
        self.assertIn("30 seconds", self.combat.failure_reason)
        self.assertEqual([item.args[0] for item in self.keys.send.call_args_list].count("UIFocus"), 2)

    def test_fighter_panel_releases_focus_when_navigation_is_interrupted_or_fails(self):
        self.journal.ship_state.return_value = {"fighter_launch_sequence": 0}
        for error in (EdapInterruptError(), RuntimeError("Navigation failed")):
            with self.subTest(error=type(error).__name__):
                self.keys.reset_mock()

                def send(binding, **kwargs):
                    if binding == "UI_Down":
                        raise error

                self.keys.send.side_effect = send
                with self.assertRaises(type(error)):
                    self.combat.launch_fighter()
                self.assertEqual(self.keys.send.call_args_list[-1], call("UIFocus", state=0))
                self.assertNotIn("UI_Select", [item.args[0] for item in self.keys.send.call_args_list])

    def test_deployment_and_evasion_reject_supercruise_without_sending_inputs(self):
        self.status.get_cleaned_data.return_value = {"Flags": FlagsInMainShip | FlagsSupercruise}
        self.assertFalse(self.combat.launch_fighter())
        self.assertFalse(self.combat.evade())
        self.keys.send.assert_not_called()

    def test_failed_power_confirmation_aborts_evasion_before_boosting(self):
        self.set_power.return_value = False
        self.assertFalse(self.combat.evade())
        self.assertNotIn("UseBoostJuice", [item.args[0] for item in self.keys.send.call_args_list])


class AfkCombatPowerTestCase(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.keys = Mock()
        self.status = Mock()
        self.combat = AfkCombat(self.keys, Mock(), status=self.status)

    def advance(self, seconds):
        self.now += seconds

    def test_reset_and_exact_escape_and_recovery_distributions_are_confirmed(self):
        reset = {"system": 2.0, "engine": 2.0, "weapons": 2.0}
        for priority, target in (
            ("engine", {"system": 2.0, "engine": 4.0, "weapons": 0.0}),
            ("system", {"system": 4.0, "engine": 2.0, "weapons": 0.0}),
        ):
            with self.subTest(priority=priority):
                self.keys.reset_mock()
                self.status.get_cleaned_data.side_effect = [
                    {"pips": {"system": 1.0, "engine": 4.0, "weapons": 1.0}},
                    {"pips": reset},
                    {"pips": reset},
                    {"pips": target},
                ]
                with (
                    patch("controls.power.monotonic", side_effect=lambda: self.now),
                    patch("controls.power.sleep", side_effect=self.advance),
                ):
                    self.assertTrue(self.combat._set_power(priority))
                self.assertEqual(self.keys.send.call_args_list[0], call("ResetPowerDistribution"))
                self.assertNotIn("IncreaseWeaponsPower", [item.args[0] for item in self.keys.send.call_args_list])

    def test_unconfirmed_reset_or_final_distribution_stops_instead_of_claiming_success(self):
        for failure in ("reset", "final"):
            with self.subTest(failure=failure):
                self.now = 0.0
                self.keys.reset_mock()
                mismatch = {"pips": {"system": 1.0, "engine": 4.0, "weapons": 1.0}}
                states = [{"pips": {"system": 2.0, "engine": 2.0, "weapons": 2.0}}] if failure == "final" else []
                self.status.get_cleaned_data.side_effect = states + [mismatch] * 10
                with (
                    patch("controls.power.monotonic", side_effect=lambda: self.now),
                    patch("controls.power.sleep", side_effect=self.advance),
                ):
                    self.assertFalse(self.combat._set_power("engine"))
                self.assertIn("Power distribution", self.combat.failure_reason)
                self.assertEqual(self.now, 5.0)
                if failure == "reset":
                    self.keys.send.assert_called_once_with("ResetPowerDistribution")


def status(name="", body=-1):
    return {"Destination_System": "", "Destination_Name": name, "Destination_Body": body}


def system_status(name, address):
    return {"Destination_System": address, "Destination_Name": name, "Destination_Body": 0}


class InSystemTargetTestCase(unittest.TestCase):
    def test_a_station_in_the_status_is_a_target_without_looking_at_the_compass(self):
        assists = Assists([])

        self.assertTrue(assists.has_in_system_target(status("Some Station", 20)))
        assists.have_destination.assert_not_called()

    def test_main_star_or_empty_destination_does_not_use_the_compass(self):
        for current in (status(), status("Some Star", 0)):
            with self.subTest(status=current):
                assists = Assists([True, True])
                self.assertFalse(assists.has_in_system_target(current))
                assists.have_destination.assert_not_called()

    def test_expected_local_target_requires_a_name_match(self):
        assists = Assists([])

        self.assertTrue(assists.has_expected_in_system_target(status("Kent's Legacy", 20), "KENT'S LEGACY"))
        self.assertFalse(assists.has_expected_in_system_target(status("Ohm City", 20), "Kent's Legacy"))
        self.assertFalse(assists.has_expected_in_system_target(status("Kent's Legacy", 0), "Kent's Legacy"))
        self.assertTrue(assists.has_expected_in_system_target(status("ABC-123", 20), "My Carrier ABC-123"))

    def test_wait_for_expected_local_target_until_status_updates(self):
        assists = Assists([])
        assists.status = Mock()
        assists.status.get_cleaned_data.side_effect = [
            status("Wrong Station", 20),
            status("Kent's Legacy", 20),
        ]
        with (
            patch("routines.assists.monotonic", side_effect=[0, 0, 1]),
            patch("routines.assists.sleep"),
        ):
            self.assertTrue(assists.wait_for_expected_in_system_target("Kent's Legacy", timeout=5))

    def test_wait_for_expected_local_target_times_out_on_mismatch(self):
        assists = Assists([])
        assists.status = Mock()
        assists.status.get_cleaned_data.return_value = status("Wrong Station", 20)
        with (
            patch("routines.assists.monotonic", side_effect=[0, 0, 5]),
            patch("routines.assists.sleep"),
        ):
            self.assertFalse(assists.wait_for_expected_in_system_target("Kent's Legacy", timeout=5))

    def test_explicit_waypoint_does_not_start_sc_assist_without_a_status_destination(self):
        assists = Assists([])
        assists.ap_ckb = Mock()
        assists.jn = Mock()
        assists.jn.ship_state.return_value = {"status": "in_supercruise"}
        assists.status = Mock()
        assists.status.get_cleaned_data.return_value = status()
        assists.status.get_flag.return_value = False
        assists.set_ap_status = Mock()
        assists.sc_assist = Mock()

        with (
            patch.object(assists, "wait_for_expected_in_system_target", return_value=False),
            patch("routines.assists.sleep"),
        ):
            self.assertFalse(assists.supercruise_to_station(None, "Some Station"))

        assists.have_destination.assert_not_called()
        assists.sc_assist.assert_not_called()

    def test_explicit_waypoint_does_not_fly_to_a_different_local_destination(self):
        assists = Assists([])
        assists.ap_ckb = Mock()
        assists.jn = Mock()
        assists.jn.ship_state.return_value = {"status": "in_supercruise"}
        assists.status = Mock()
        assists.status.get_cleaned_data.return_value = status("Other Station", 20)
        assists.status.get_flag.return_value = False
        assists.set_ap_status = Mock()
        assists.sc_assist = Mock()
        assists.wait_for_expected_in_system_target = Mock(return_value=False)
        assists.sc_engage = Mock()

        with patch("routines.assists.sleep"):
            self.assertFalse(assists.supercruise_to_station(None, "Kent's Legacy"))

        assists.sc_engage.assert_not_called()
        assists.sc_assist.assert_not_called()

    def test_sc_assist_checks_status_before_touching_the_game_window(self):
        assists = Assists([True])
        assists.ap_ckb = Mock()
        assists.status = Mock()
        assists.status.get_cleaned_data.return_value = status()
        assists.ship_control = Mock()

        assists.sc_assist(None)

        assists.ship_control.goto_cockpit_view.assert_not_called()
        assists.have_destination.assert_not_called()
        assists.ap_ckb.assert_called_once_with("log", "No local target found. Select a target and try again.")


class ScAssistStatusTestCase(unittest.TestCase):
    def make_assists(self, ap_state, drop_type=None):
        assists = Assists([True])
        assists.ap_state = ap_state
        statuses = []

        def set_ap_status(text):
            assists.ap_state = text
            statuses.append(text)

        assists.set_ap_status = set_ap_status
        assists.ap_ckb = Mock()
        assists.vce = Mock()
        assists.keys = Mock()
        assists.ship_control = Mock()
        assists.status = Mock()
        assists.status.get_cleaned_data.return_value = status("Jameson Memorial", 20)
        assists.status.get_flag.return_value = False
        assists.jn = Mock()
        assists.jn.ship_state.return_value = {
            "status": "in_supercruise",
            "interdicted": False,
            "has_adv_dock_comp": True,
            "has_std_dock_comp": False,
            "SupercruiseDestinationDrop_type": drop_type,
        }
        assists.nav_align = Mock()
        assists.sc_target_align = Mock(return_value=ScTargetAlignReturn.Disengage)
        assists.debug_overlay = False
        assists.dock = Mock()
        return assists, statuses

    def run_sc_assist(self, assists):
        with patch("routines.assists.sleep"):
            assists.sc_assist(None)

    def test_travel_assist_reports_each_phase_until_docked(self):
        assists, statuses = self.make_assists("SC to Target")

        self.run_sc_assist(assists)

        self.assertEqual(statuses, ["Align", "SC to Target", "Approaching target", "Docking Complete"])
        assists.dock.assert_called_once_with(None)

    def test_unconfirmed_alignment_does_not_accelerate_or_start_target_approach(self):
        assists, statuses = self.make_assists("SC to Target")
        assists.nav_align = FlightMixin.nav_align.__get__(assists)
        assists.config = {"NavAlignTries": 1}
        assists.read_nav_offset = Mock(return_value={"z": 1, "yaw": 0, "pit": 0})
        assists._compass_confirmation = Mock(return_value=(False, None))
        assists.get_destination_offset = Mock(return_value=None)
        assists._recover_expanded_target = Mock(return_value=ScTargetAlignReturn.Lost)
        assists.notify_user = Mock()

        with (
            self.assertLogs("ed_log", level="WARNING"),
            self.assertRaisesRegex(RuntimeError, "target alignment unconfirmed"),
        ):
            self.run_sc_assist(assists)

        self.assertEqual(statuses, ["Align"])
        self.assertEqual(assists.keys.send.call_args_list, [call("SetSpeed50"), call("SetSpeedZero")])
        assists.sc_target_align.assert_not_called()
        assists.dock.assert_not_called()

    def test_waypoint_station_name_is_kept_for_the_cruise_phase(self):
        assists, statuses = self.make_assists("SC to Station: Jameson Memorial")

        self.run_sc_assist(assists)

        self.assertEqual(statuses[:2], ["Align", "SC to Station: Jameson Memorial"])

    def test_initial_ring_miss_keeps_alignment_throttle_until_target_is_centered(self):
        assists, _ = self.make_assists("SC to Target")
        timeline = Mock()
        timeline.align.side_effect = [
            ScTargetAlignReturn.Lost,
            ScTargetAlignReturn.Found,
            ScTargetAlignReturn.Disengage,
        ]
        assists.sc_target_align = timeline.align
        assists.keys.send = timeline.throttle
        timeline.compass.return_value = False
        assists.nav_align = timeline.compass
        assists.wait_for_sc_disengage = Mock(return_value=False)
        assists.interdiction_check = Mock(return_value=False)
        assists.try_sc_disengage = Mock(return_value=False)

        self.run_sc_assist(assists)

        self.assertEqual(
            timeline.mock_calls,
            [
                call.throttle("SetSpeed50"),
                call.compass(None, allow_target_recovery=True),
                call.align(None),
                call.compass(None, abort_on_disengage=True, allow_target_recovery=True),
                call.align(None),
                call.throttle("SetSpeed75"),
                call.align(None),
                call.throttle("SetSpeedZero"),
            ],
        )
        assists.wait_for_sc_disengage.assert_not_called()

    def test_lost_cruise_target_preserves_exit_prompt_before_realigning(self):
        assists, _ = self.make_assists("SC to Target")
        assists.sc_target_align.side_effect = [ScTargetAlignReturn.Found, ScTargetAlignReturn.Lost]
        assists.wait_for_sc_disengage = Mock(return_value=True)
        assists.interdiction_check = Mock(return_value=False)
        assists.try_sc_disengage = Mock(return_value=False)

        self.run_sc_assist(assists)

        assists.wait_for_sc_disengage.assert_called_once_with(None, 10)
        assists.nav_align.assert_called_once_with(None, allow_target_recovery=True)
        self.assertEqual(
            assists.keys.send.call_args_list, [call("SetSpeed50"), call("SetSpeed75"), call("SetSpeedZero")]
        )

    def test_lost_cruise_target_realigns_at_50_and_stays_there_after_reacquisition(self):
        assists, _ = self.make_assists("SC to Target")
        timeline = Mock()
        timeline.align.side_effect = [
            ScTargetAlignReturn.Found,
            ScTargetAlignReturn.Lost,
            ScTargetAlignReturn.Found,
            ScTargetAlignReturn.Disengage,
        ]
        timeline.compass.return_value = False
        timeline.wait.return_value = False
        assists.sc_target_align = timeline.align
        assists.keys.send = timeline.throttle
        assists.nav_align = timeline.compass
        assists.wait_for_sc_disengage = timeline.wait
        assists.interdiction_check = Mock(return_value=False)
        assists.try_sc_disengage = Mock(return_value=False)

        self.run_sc_assist(assists)

        self.assertEqual(
            timeline.mock_calls,
            [
                call.throttle("SetSpeed50"),
                call.compass(None, allow_target_recovery=True),
                call.align(None),
                call.throttle("SetSpeed75"),
                call.align(None),
                call.wait(None, 10),
                call.throttle("SetSpeed50"),
                call.compass(None, abort_on_disengage=True, allow_target_recovery=True),
                call.align(None),
                call.align(None),
                call.throttle("SetSpeedZero"),
            ],
        )

    def test_target_without_docking_reports_arrival(self):
        assists, statuses = self.make_assists("SC to Target", drop_type="$USS_Type_Ceremonial;")

        self.run_sc_assist(assists)

        self.assertEqual(statuses[-1], "Arrived at target")
        assists.dock.assert_not_called()

    def test_repeated_ring_loss_does_not_overwrite_reduced_recovery_throttle(self):
        assists, _ = self.make_assists("SC to Target")
        results = iter(
            [
                ScTargetAlignReturn.Found,
                ScTargetAlignReturn.Lost,
                ScTargetAlignReturn.Lost,
                ScTargetAlignReturn.Disengage,
            ]
        )

        def align(_scr_reg):
            result = next(results)
            if result is ScTargetAlignReturn.Found:
                self.assertIsNone(assists._sc_loop_recovery)
            return result

        def compass(_scr_reg, **kwargs):
            if kwargs.get("abort_on_disengage"):
                self.assertIsNotNone(assists._sc_loop_recovery)
                assists._sc_loop_recovery.stage = 2
            return False

        assists.sc_target_align = Mock(side_effect=align)
        assists.nav_align = compass
        assists.wait_for_sc_disengage = Mock(return_value=False)
        assists.interdiction_check = Mock(return_value=False)
        assists.try_sc_disengage = Mock(return_value=False)
        self.run_sc_assist(assists)
        self.assertEqual(
            assists.keys.send.call_args_list,
            [call("SetSpeed50"), call("SetSpeed75"), call("SetSpeed50"), call("SetSpeedZero")],
        )
        self.assertIsNone(assists._sc_loop_recovery)


class TravelAssistTestCase(unittest.TestCase):
    def setUp(self):
        self.assists = Assists([])
        self.assists.jn = Mock()
        self.assists.fsd_assist = Mock()
        self.assists.sc_assist = Mock()
        self.assists.set_ap_status = Mock()
        self.assists.status = Mock()
        self.assists.ap_ckb = Mock()
        self.assists.keys = Mock()
        self.assists.ship_control = Mock()
        self.assists.notify_user = Mock()
        power_patch = patch("routines.assists.set_power_distribution", return_value=True)
        self.power = power_patch.start()
        self.addCleanup(power_patch.stop)

    def test_power_is_confirmed_before_flight(self):
        self.assists.jn.ship_state.return_value = {"target": "Sol"}
        self.assists.fsd_assist.return_value = FsdAssistResult.RouteComplete
        self.assists.fsd_assist.side_effect = lambda _: self.power.assert_called_once_with(
            self.assists.keys, self.assists.status, "engine", skip_if_set=True
        )

        self.assists.travel_assist(None)

        self.assists.ship_control.goto_cockpit_view.assert_called_once_with(timeout=5.0)
        self.assists.fsd_assist.assert_called_once_with(None)

    def test_unconfirmed_power_notifies_and_continues_system_or_local_flight(self):
        for system_target in ("Sol", None):
            with self.subTest(system_target=system_target):
                self.assists.jn.ship_state.return_value = {"target": system_target}
                self.assists.status.get_cleaned_data.return_value = status("Some Station", 20)
                self.assists.fsd_assist.return_value = FsdAssistResult.RouteComplete
                self.power.return_value = False
                self.assists.notify_user.reset_mock()
                self.assists.fsd_assist.reset_mock()
                self.assists.sc_assist.reset_mock()

                self.assists.travel_assist(None)

                self.assists.keys.send.assert_not_called()
                self.assists.notify_user.assert_called_once_with(
                    "Power distribution could not be confirmed. Travel Assist will continue."
                )
                if system_target:
                    self.assists.fsd_assist.assert_called_once_with(None)
                    self.assists.sc_assist.assert_not_called()
                else:
                    self.assists.fsd_assist.assert_not_called()
                    self.assists.sc_assist.assert_called_once_with(None)

    def test_power_inputs_are_not_sent_without_cockpit_focus(self):
        self.assists.jn.ship_state.return_value = {"target": "Sol"}
        self.assists.ship_control.goto_cockpit_view.return_value = False

        self.assists.travel_assist(None)

        self.power.assert_not_called()
        self.assists.fsd_assist.assert_not_called()
        self.assists.sc_assist.assert_not_called()

    def test_system_target_runs_fsd_route_without_local_target(self):
        self.assists.jn.ship_state.return_value = {"target": "Alpha Centauri"}
        self.assists.fsd_assist.return_value = FsdAssistResult.RouteComplete

        self.assists.travel_assist(None)

        self.assists.fsd_assist.assert_called_once_with(None)
        self.assists.sc_assist.assert_not_called()

    def test_system_route_hands_off_to_local_target(self):
        self.assists.jn.ship_state.return_value = {"target": "Alpha Centauri"}
        self.assists.fsd_assist.return_value = FsdAssistResult.InSystemTarget

        self.assists.travel_assist(None)

        self.assists.fsd_assist.assert_called_once_with(None)
        self.assists.sc_assist.assert_called_once_with(None)

    def test_local_target_runs_supercruise_without_system_route(self):
        self.assists.jn.ship_state.return_value = {"target": None}
        self.assists.status.get_cleaned_data.return_value = status("Some Station", 20)

        self.assists.travel_assist(None)

        self.assists.fsd_assist.assert_not_called()
        self.assists.sc_assist.assert_called_once_with(None)

    def test_local_phase_does_not_start_without_a_status_destination(self):
        self.assists.jn.ship_state.return_value = {"target": None}
        self.assists.status.get_cleaned_data.return_value = status()

        self.assists.travel_assist(None)

        self.assists.fsd_assist.assert_not_called()
        self.assists.sc_assist.assert_not_called()
        self.assists.ap_ckb.assert_called_once_with("log", "No local target found. Select a target and try again.")
        self.power.assert_not_called()

    def test_stopped_travel_assist_does_not_start_local_phase(self):
        self.assists.jn.ship_state.return_value = {"target": "Alpha Centauri"}
        self.assists.fsd_assist.return_value = FsdAssistResult.InSystemTarget
        self.assists.travel_assist_enabled = False

        self.assists.travel_assist(None)

        self.assists.sc_assist.assert_not_called()

    def test_system_target_from_status_runs_fsd_route_without_journal_target(self):
        # A re-plotted route with the same next system produces no new FSDTarget after NavRouteClear.
        self.assists.jn.ship_state.return_value = {"target": None, "system_address": 1, "location": "Sol"}
        self.assists.status.get_cleaned_data.return_value = system_status("Alpha Centauri", 2)
        self.assists.fsd_assist.return_value = FsdAssistResult.RouteComplete

        self.assists.travel_assist(None)

        self.assists.fsd_assist.assert_called_once_with(None)
        self.assists.ap_ckb.assert_not_called()


class JumpTargetTestCase(unittest.TestCase):
    def make_assists(self, ship, destination):
        assists = Assists([])
        assists.jn = Mock()
        assists.jn.ship_state.return_value = ship
        assists.status = Mock()
        assists.status.get_cleaned_data.return_value = destination
        return assists

    def test_journal_target_is_preferred(self):
        assists = self.make_assists({"target": "Arima"}, system_status("Alpha Centauri", 2))

        self.assertEqual(assists.jump_target(), "Arima")
        assists.status.get_cleaned_data.assert_not_called()

    def test_other_system_in_status_is_used_without_journal_target(self):
        assists = self.make_assists(
            {"target": None, "system_address": 1, "location": "Sol"}, system_status("Alpha Centauri", 2)
        )

        self.assertEqual(assists.jump_target(), "Alpha Centauri")

    def test_status_without_another_system_is_no_jump_target(self):
        cases = {
            "no destination": ({"target": None, "system_address": 1, "location": "Sol"}, status()),
            "current system": ({"target": None, "system_address": 1, "location": "Sol"}, system_status("Sol", 1)),
            "current system by name": (
                {"target": None, "system_address": None, "location": "Sol"},
                system_status("sol", 1),
            ),
            "local body": (
                {"target": None, "system_address": 1, "location": "Sol"},
                {"Destination_System": 1, "Destination_Name": "Abraham Lincoln", "Destination_Body": 12},
            ),
        }
        for name, (ship, destination) in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.make_assists(ship, destination).jump_target())


class FsdAssistResultTestCase(unittest.TestCase):
    def make_assists(self):
        assists = Assists([])
        assists.ap_ckb = Mock()
        assists.config = {"AutomaticLogout": False}
        assists.jn = Mock()
        assists.jn.ship_state.return_value = {
            "target": False,
            "status": "in_space",
            "cur_star_system": "Arima",
            "location": "Sol",
            "dist_jumped": 0,
            "jumps_remains": 0,
            "fuel_percent": 100,
        }
        assists.keys = Mock()
        assists.set_ap_status = Mock()
        assists.status = Mock()
        assists.status.get_cleaned_data.return_value = status()
        assists.status.get_flag.return_value = False
        assists.nav_route = Mock()
        assists.nav_route.get_last_system.return_value = ""
        assists.total_dist_jumped = 0
        assists.update_overlay = Mock()
        assists.vce = Mock()
        return assists

    def test_fsd_route_without_local_target_reports_route_complete(self):
        assists = self.make_assists()

        with patch("routines.assists.sleep"):
            result = assists.fsd_assist(None)

        self.assertIs(result, FsdAssistResult.RouteComplete)

    def test_fsd_route_with_local_target_reports_target_ready(self):
        assists = self.make_assists()
        assists.status.get_cleaned_data.return_value = status("Kent's Legacy", 20)

        with patch("routines.assists.sleep"):
            result = assists.fsd_assist(None)

        self.assertIs(result, FsdAssistResult.InSystemTarget)

    def test_failed_fsd_jump_does_not_report_route_complete(self):
        assists = self.make_assists()
        assists.jn.ship_state.return_value["target"] = True
        assists.jump = Mock(return_value=False)
        assists.mnvr_to_target = Mock()

        with patch("routines.assists.sleep"):
            result = assists.fsd_assist(None)

        self.assertIs(result, FsdAssistResult.Failed)

    def test_fsd_route_jumps_to_status_target_without_journal_target(self):
        assists = self.make_assists()
        ship = assists.jn.ship_state.return_value
        ship.update({"target": None, "system_address": 1})
        assists.config["FuelThreasholdAbortAP"] = 10
        assists.status.get_cleaned_data.return_value = system_status("Arima", 2)

        def arrive(_scr_reg):
            ship["system_address"] = 2
            ship["location"] = "Arima"
            return True

        assists.jump = Mock(side_effect=arrive)
        assists.mnvr_to_target = Mock()
        assists.refuel = Mock(return_value=False)
        assists.position = Mock()
        assists.discovery_scan = Mock()

        with patch("routines.assists.sleep"):
            result = assists.fsd_assist(None)

        assists.jump.assert_called_once_with(None)
        self.assertIs(result, FsdAssistResult.RouteComplete)

    def test_jump_to_system_accepts_station_bookmark_with_expected_nav_route(self):
        assists = self.make_assists()
        assists.status.get_cleaned_data.return_value = status("Kent's Legacy", 20)
        assists.nav_route = Mock()
        assists.nav_route.get_last_system.return_value = "Arima"
        assists.jn.ship_state.return_value["status"] = "in_supercruise"
        assists.fsd_assist = Mock(return_value=FsdAssistResult.InSystemTarget)

        result = assists.jump_to_system(None, expected_system="Arima")

        self.assertIs(result, FsdAssistResult.InSystemTarget)

    def test_jump_to_system_rejects_mismatched_nav_route_before_flying(self):
        assists = self.make_assists()
        assists.status.get_cleaned_data.return_value = status("Kent's Legacy", 20)
        assists.nav_route = Mock()
        assists.nav_route.get_last_system.return_value = "Wrong System"
        assists.fsd_assist = Mock()

        result = assists.jump_to_system(None, expected_system="Arima")

        self.assertIs(result, FsdAssistResult.Failed)
        assists.fsd_assist.assert_not_called()

    def test_jump_to_system_rejects_route_that_did_not_reach_expected_system(self):
        assists = self.make_assists()
        assists.status.get_cleaned_data.return_value = status("Arima", 0)
        assists.nav_route = Mock()
        assists.nav_route.get_last_system.return_value = "Arima"
        assists.jn.ship_state.return_value["cur_star_system"] = "LHS 20"
        assists.jn.ship_state.return_value["status"] = "in_supercruise"
        assists.fsd_assist = Mock(return_value=FsdAssistResult.RouteComplete)
        with (
            patch("routines.assists.monotonic", side_effect=[0, 0, 10]),
            patch("routines.assists.sleep"),
        ):
            result = assists.jump_to_system(None, expected_system="Arima")

        self.assertIs(result, FsdAssistResult.Failed)
        assists.ap_ckb.assert_any_call("log", "Failed to reach Arima.")

    def make_waypoint_jump(self, destination=None):
        assists = self.make_assists()
        assists.jn.ship_state.return_value.update(
            target="Arima", cur_star_system="LHS 20", status="in_supercruise", system_address=1
        )
        assists.nav_route.get_last_system.return_value = "Arima"
        assists.nav_route.get_next_system.return_value = ("Arima", 2)
        assists.status.get_cleaned_data.return_value = destination or status("Ohm City", 20)
        assists.status.wait_for_flag_on.return_value = True
        assists.mnvr_to_target = Mock()
        assists.jump = Mock(return_value=False)
        return assists

    def test_waypoint_stale_station_target_never_aligns_or_jumps(self):
        assists = self.make_waypoint_jump()
        with (
            patch("routines.assists.monotonic", side_effect=[0, 0, 5]),
            patch("routines.assists.sleep"),
        ):
            result = assists.jump_to_system(None, expected_system="Arima")

        self.assertIs(result, FsdAssistResult.Failed)
        assists.keys.send.assert_any_call("TargetNextRouteSystem")
        assists.keys.send.assert_any_call("SetSpeedZero")
        assists.mnvr_to_target.assert_not_called()
        assists.jump.assert_not_called()
        assists.ap_ckb.assert_called_once()

    def test_waypoint_waits_for_status_before_alignment(self):
        assists = self.make_waypoint_jump()
        confirmed = dict(status("ARIMA", 0), Destination_System=2)
        assists.status.get_cleaned_data.side_effect = [
            status("Ohm City", 20),
            status("Ohm City", 20),
            confirmed,
            confirmed,
        ]
        events = []
        assists.keys.send.side_effect = lambda name: events.append(name)
        assists.mnvr_to_target.side_effect = lambda _: events.append("align")
        assists.jump.side_effect = lambda _: events.append("jump") or False
        with (
            patch("routines.assists.monotonic", side_effect=[0, 0, 1]),
            patch("routines.assists.sleep") as sleep,
        ):
            result = assists.fsd_assist(None, expected_system="Arima")

        self.assertIs(result, FsdAssistResult.Failed)  # Deliberately stop at the mocked jump.
        self.assertEqual(events[:3], ["TargetNextRouteSystem", "align", "jump"])
        sleep.assert_any_call(0.25)
        assists.status.wait_for_flag_on.assert_called_once_with(FlagsSupercruise, 5.0)

    def test_waypoint_checks_next_hop_not_final_destination(self):
        assists = self.make_waypoint_jump()
        assists.nav_route.get_next_system.return_value = ("Intermediate", 3)
        assists.status.get_cleaned_data.return_value = dict(status("Intermediate", 0), Destination_System=3)
        with patch("routines.assists.sleep"):
            assists.fsd_assist(None, expected_system="Arima")

        assists.mnvr_to_target.assert_called_once_with(None)
        assists.jump.assert_called_once_with(None)

    def test_waypoint_rejects_wrong_address_name_or_local_body(self):
        for name, body, address in (("Arima", 0, 99), ("Wrong", 0, 2), ("Arima", 20, 2)):
            with self.subTest(name=name, body=body, address=address):
                assists = self.make_waypoint_jump(dict(status(name, body), Destination_System=address))
                with (
                    patch("routines.assists.monotonic", side_effect=[0, 0, 5]),
                    patch("routines.assists.sleep"),
                ):
                    result = assists.fsd_assist(None, expected_system="Arima")
                self.assertIs(result, FsdAssistResult.Failed)
                assists.mnvr_to_target.assert_not_called()
                assists.jump.assert_not_called()

    def test_waypoint_rejects_failed_supercruise_or_missing_next_hop(self):
        for failure in ("supercruise", "next_hop", "route_changed"):
            with self.subTest(failure=failure):
                assists = self.make_waypoint_jump()
                if failure == "supercruise":
                    assists.status.wait_for_flag_on.return_value = False
                elif failure == "next_hop":
                    assists.nav_route.get_next_system.return_value = None
                else:
                    assists.nav_route.get_last_system.return_value = "Wrong"
                with patch("routines.assists.sleep"):
                    result = assists.fsd_assist(None, expected_system="Arima")
                self.assertIs(result, FsdAssistResult.Failed)
                assists.keys.send.assert_any_call("SetSpeedZero")
                assists.mnvr_to_target.assert_not_called()
                assists.jump.assert_not_called()

    def test_waypoint_confirms_target_even_without_journal_target(self):
        assists = self.make_waypoint_jump()
        assists.jn.ship_state.return_value["target"] = None
        with (
            patch("routines.assists.monotonic", side_effect=[0, 0, 5]),
            patch("routines.assists.sleep"),
        ):
            result = assists.fsd_assist(None, expected_system="Arima")
        self.assertIs(result, FsdAssistResult.Failed)
        assists.keys.send.assert_any_call("TargetNextRouteSystem")
        assists.mnvr_to_target.assert_not_called()

    def test_waypoint_selects_only_after_undocking_and_supercruise(self):
        assists = self.make_waypoint_jump()
        assists.jn.ship_state.return_value["status"] = "in_space"
        assists.status.get_flag.return_value = True
        events = []

        def undock():
            events.append("undock")
            assists.status.get_flag.return_value = False

        def enter_supercruise():
            events.append("supercruise")
            assists.jn.ship_state.return_value["status"] = "in_supercruise"

        def send(key):
            events.append(key)
            if key == "TargetNextRouteSystem":
                assists.status.get_cleaned_data.return_value = dict(status("Arima", 0), Destination_System=2)

        assists.waypoint_undock_seq = Mock(side_effect=undock)
        assists.sc_engage = Mock(side_effect=enter_supercruise)
        assists.keys.send.side_effect = send
        assists.mnvr_to_target.side_effect = lambda _: events.append("align")
        with patch("routines.assists.sleep"):
            assists.jump_to_system(None, expected_system="Arima")

        self.assertEqual(events[:4], ["undock", "supercruise", "TargetNextRouteSystem", "align"])

    def test_waypoint_reconfirms_each_hop_and_finishes_at_final_system(self):
        assists = self.make_waypoint_jump()
        ship = assists.jn.ship_state.return_value
        assists.config["FuelThreasholdAbortAP"] = 10
        assists.refuel = Mock()
        assists.position = Mock()
        assists.discovery_scan = Mock()
        assists.nav_route.get_next_system.side_effect = lambda name, address: (
            ("Intermediate", 3) if name == "LHS 20" else ("Arima", 2)
        )
        selected = []

        def send(key):
            if key == "TargetNextRouteSystem":
                name, address = assists.nav_route.get_next_system(ship["cur_star_system"], ship["system_address"])
                selected.append(name)
                assists.status.get_cleaned_data.return_value = dict(status(name, 0), Destination_System=address)

        def jump(_):
            current = assists.status.get_cleaned_data.return_value
            ship.update(cur_star_system=current["Destination_Name"], system_address=current["Destination_System"])
            if ship["cur_star_system"] == "Arima":
                ship["target"] = None
            return True

        assists.keys.send.side_effect = send
        assists.jump.side_effect = jump
        with patch("routines.assists.sleep"), patch("routines.assists.threading.Thread"):
            result = assists.jump_to_system(None, expected_system="Arima")

        self.assertIs(result, FsdAssistResult.RouteComplete)
        self.assertEqual(selected, ["Intermediate", "Arima"])
        self.assertEqual(assists.mnvr_to_target.call_count, 2)
        self.assertEqual(assists.jump.call_count, 2)

    def test_waypoint_route_change_during_confirmation_stops_before_alignment(self):
        assists = self.make_waypoint_jump()
        assists.nav_route.get_last_system.side_effect = ["Arima", "Wrong"]
        with patch("routines.assists.sleep"):
            result = assists.fsd_assist(None, expected_system="Arima")
        self.assertIs(result, FsdAssistResult.Failed)
        assists.keys.send.assert_any_call("SetSpeedZero")
        assists.mnvr_to_target.assert_not_called()
        assists.jump.assert_not_called()

    def test_travel_assist_only_hands_off_to_supercruise_for_local_target(self):
        for result in FsdAssistResult:
            with self.subTest(result=result):
                assists = self.make_assists()
                assists.jn.ship_state.return_value["target"] = True
                assists.fsd_assist = Mock(return_value=result)
                assists.sc_assist = Mock()
                assists.ship_control = Mock()
                with patch("routines.assists.set_power_distribution", return_value=True):
                    assists.travel_assist(None)

                if result is FsdAssistResult.InSystemTarget:
                    assists.sc_assist.assert_called_once_with(None)
                else:
                    assists.sc_assist.assert_not_called()


if __name__ == "__main__":
    unittest.main()
