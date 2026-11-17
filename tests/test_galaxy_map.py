import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import numpy as np

from core import paths
from core.constants import GuiFocusGalaxyMap
from panels.galaxy_map import EDGalaxyMap
from vision.diagnostics import save_case
from vision.screen import Screen


class GalaxyMapTestCase(unittest.TestCase):
    def setUp(self):
        self.ap = Mock()
        screen = Mock(screen_width=1920, screen_height=1080, mon={"left": 0, "top": 0})
        screen.screen_rect_to_abs.side_effect = lambda rect: Screen.screen_rect_to_abs(screen, rect)
        with patch("panels.galaxy_map.StatusParser"), patch("panels.galaxy_map.MousePoint"):
            self.galaxy_map = EDGalaxyMap(self.ap, screen, self.ap.keys, Mock())
        self.galaxy_map.status_parser.get_gui_focus.return_value = GuiFocusGalaxyMap
        self.ap.keys.activate_window = False
        self.ap.ocr.image_simple_ocr.return_value = ["LHS 20"]
        recorder = patch("panels.galaxy_map.save_case")
        self.save_case = recorder.start()
        self.addCleanup(recorder.stop)
        for name in ("dpi_aware", "set_focus_elite_window", "hotkey", "typewrite"):
            patched = patch(f"panels.galaxy_map.{name}")
            patched.start()
            self.addCleanup(patched.stop)

    def test_search_debug_does_not_capture_when_debug_is_disabled(self):
        with patch("panels.galaxy_map.logger.isEnabledFor", return_value=False):
            self.galaxy_map._save_search_debug("first-result-unconfirmed", "LHS 20")

        self.galaxy_map.screen.get_screen_full.assert_not_called()
        self.save_case.assert_not_called()

    def test_search_debug_saves_full_frame_with_stage_and_input_context(self):
        image = self.galaxy_map.screen.get_screen_full.return_value
        self.galaxy_map.status_parser.get_gui_focus.return_value = 6
        self.ap.keys.activate_window = True
        with (
            patch("panels.galaxy_map.logger.isEnabledFor", return_value=True),
            patch("panels.galaxy_map.logger.debug") as debug,
        ):
            self.galaxy_map._save_search_debug("route-unconfirmed", "LHS 20", 2)

        self.save_case.assert_called_once_with("galaxy-map", "route-unconfirmed", {"map": image})
        self.assertEqual(debug.call_args.args[1:], ("route-unconfirmed", "LHS 20", 2, 6, True))

    def test_opening_or_reusing_map_does_not_capture_diagnostic_images(self):
        for focus in (0, GuiFocusGalaxyMap):
            with self.subTest(focus=focus):
                self.galaxy_map.status_parser.get_gui_focus.return_value = focus
                with patch("panels.galaxy_map.logger.isEnabledFor", return_value=True):
                    self.galaxy_map.goto_galaxy_map()
                self.galaxy_map.screen.get_screen_full.assert_not_called()
                self.save_case.assert_not_called()

    def test_keyboard_plots_after_key_sequence_without_waiting_for_visual_state(self):
        self.ap.nav_route.get_last_system.side_effect = ["", "", "LHS 20"]
        events = Mock()
        events.attach_mock(self.ap.keys.send, "send")
        events.attach_mock(self.ap.keys.send_key, "send_key")

        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(paths, "DEBUG_DIR", Path(folder)),
            patch("panels.galaxy_map.save_case", wraps=save_case),
            self.assertLogs("ed_log", level="DEBUG") as logs,
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch("panels.galaxy_map.sleep"),
            patch("panels.galaxy_map.typewrite") as typewrite,
        ):
            self.assertTrue(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
            self.assertFalse((Path(folder) / "galaxy-map").exists())
            self.assertFalse(any(record.levelname in ("WARNING", "ERROR", "CRITICAL") for record in logs.records))

        self.galaxy_map.screen.get_screen_full.assert_not_called()
        self.ap.ocr.capture_region_pct.assert_not_called()
        self.ap.ocr.image_simple_ocr.assert_not_called()
        typewrite.assert_called_once_with("LHS 20", interval=0.25)
        self.galaxy_map.mouse.do_click.assert_not_called()
        self.assertEqual(
            events.mock_calls,
            [
                call.send("UI_Up"),
                call.send("UI_Select"),
                call.send_key("Down", 28),
                call.send_key("Up", 28),
                call.send("UI_Down"),
                call.send("UI_Up"),
                call.send("UI_Right"),
                call.send("UI_Select"),
                call.send("CamZoomIn"),
                call.send("UI_Select", hold=0.75),
                call.send("GalaxyMapOpen"),
            ],
        )

    def test_keyboard_unconfirmed_route_saves_one_image_and_falls_back_to_mouse(self):
        self.ap.nav_route.get_last_system.side_effect = ["", "", "", "", "LHS 20"]
        clock = [0.0]
        self.galaxy_map.screen.get_screen_full.return_value = np.zeros((4, 5, 3), dtype=np.uint8)
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(paths, "DEBUG_DIR", Path(folder)),
            patch("panels.galaxy_map.save_case", wraps=save_case) as recorder,
            self.assertLogs("ed_log", level="DEBUG"),
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name", return_value=True),
            patch.object(self.galaxy_map, "_select_first_result", return_value=True),
            patch("panels.galaxy_map.NAV_ROUTE_UPDATE_TIMEOUT", 0.1),
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        ):
            self.assertTrue(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
            self.assertEqual(len(list((Path(folder) / "galaxy-map").glob("*_map.png"))), 1)
            recorder.assert_called_once_with(
                "galaxy-map", "route-unconfirmed", {"map": self.galaxy_map.screen.get_screen_full.return_value}
            )
        self.assertEqual(
            self.ap.keys.send.call_args_list,
            [
                call("UI_Up"),
                call("UI_Select"),
                call("UI_Down"),
                call("UI_Up"),
                call("UI_Right"),
                call("UI_Select"),
                call("CamZoomIn"),
                call("UI_Select", hold=0.75),
                call("GalaxyMapOpen"),
            ],
        )
        self.galaxy_map.mouse.do_click.assert_called_once_with(1816, 605)

    def test_keyboard_navigation_stops_when_map_loses_focus(self):
        self.ap.ocr.capture_region_pct.side_effect = lambda region: region
        steps = ["UI_Down", "UI_Up", "UI_Right", "UI_Select", "CamZoomIn"]
        for lost_after in (0, 4):
            with self.subTest(key=steps[lost_after]):
                self.ap.keys.send.reset_mock()
                self.galaxy_map.status_parser.get_gui_focus.side_effect = lambda: (
                    0 if call(steps[lost_after]) in self.ap.keys.send.call_args_list else GuiFocusGalaxyMap
                )
                with patch("panels.galaxy_map.sleep"), patch.object(self.galaxy_map, "_save_search_debug") as snapshot:
                    self.assertFalse(self.galaxy_map._select_first_result_keyboard("LHS 20", 1))
                self.assertEqual(self.ap.keys.send.call_args_list, [call(key) for key in steps[: lost_after + 1]])
                self.galaxy_map.mouse.do_click.assert_not_called()
                snapshot.assert_called_once_with("focus-lost", "LHS 20", 1)

    def test_keyboard_focus_loss_before_typing_stops_input_and_fallback(self):
        self.ap.nav_route.get_last_system.return_value = ""
        self.galaxy_map.status_parser.get_gui_focus.side_effect = [GuiFocusGalaxyMap, 0, 0, 0]
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_save_search_debug"),
            patch("panels.galaxy_map.sleep"),
            patch("panels.galaxy_map.hotkey") as hotkey,
            patch("panels.galaxy_map.typewrite") as typewrite,
        ):
            self.assertFalse(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
        hotkey.assert_not_called()
        typewrite.assert_not_called()
        self.galaxy_map.mouse.do_click.assert_not_called()
        self.assertEqual(self.ap.keys.send.call_args_list, [call("UI_Up"), call("UI_Select")])

    def test_delayed_route_confirmation_avoids_a_second_plot(self):
        self.ap.nav_route.get_last_system.side_effect = ["", "", "", "LHS 20"]
        clock = [0.0]
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=True),
            patch.object(self.galaxy_map, "_select_first_result_keyboard", return_value=True),
            patch.object(self.galaxy_map, "_enter_system_name", return_value=True),
            patch.object(self.galaxy_map, "_select_first_result", return_value=True),
            patch("panels.galaxy_map.NAV_ROUTE_UPDATE_TIMEOUT", 0.1),
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        ):
            self.assertTrue(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
        self.galaxy_map.mouse.do_click.assert_not_called()
        self.assertEqual(self.ap.keys.send.call_args_list, [call("UI_Select", hold=0.75), call("GalaxyMapOpen")])

    def test_successful_mouse_search_does_not_capture_snapshots(self):
        self.ap.nav_route.get_last_system.side_effect = ["", "", "LHS 20"]
        events = Mock()
        events.attach_mock(self.ap.keys.send, "send")
        events.attach_mock(self.galaxy_map.mouse.do_click, "click")
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch.object(self.galaxy_map, "_select_first_result", return_value=True),
            patch.object(self.galaxy_map, "_save_search_debug") as snapshot,
            patch("panels.galaxy_map.hotkey") as hotkey,
            patch("panels.galaxy_map.typewrite") as typewrite,
            patch("panels.galaxy_map.sleep"),
        ):
            events.attach_mock(snapshot, "snapshot")
            events.attach_mock(hotkey, "hotkey")
            events.attach_mock(typewrite, "typewrite")
            self.assertTrue(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))

        events.assert_has_calls(
            [
                call.click(926, 125),
                call.hotkey("ctrl", "a"),
                call.typewrite("LHS 20", interval=0.25),
            ]
        )
        snapshot.assert_not_called()

    def test_first_result_waits_for_exact_result_and_system_information(self):
        self.ap.ocr.image_simple_ocr.side_effect = [
            ["SUCHEN ..."],
            ["LHS 200"],
            ["LHS 20"],
            ["LHS 200"],
            ["LHS 20"],
        ]
        with patch("panels.galaxy_map.sleep"):
            self.assertTrue(self.galaxy_map._select_first_result("LHS 20", 1))
        self.galaxy_map.mouse.do_click.assert_called_once_with(939, 157)
        self.ap.keys.send.assert_not_called()
        self.save_case.assert_not_called()

    def test_wrong_system_information_stops_before_plot_click(self):
        self.ap.ocr.image_simple_ocr.side_effect = lambda image: (
            ["LHS 20"] if self.ap.ocr.image_simple_ocr.call_count == 1 else ["LHS 200"]
        )
        clock = [0.0]
        with (
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
            patch.object(self.galaxy_map, "_save_search_debug") as snapshot,
        ):
            self.assertFalse(self.galaxy_map._select_first_result("LHS 20", 1))
        self.galaxy_map.mouse.do_click.assert_called_once_with(939, 157)
        snapshot.assert_called_once_with("selected-system-unconfirmed", "LHS 20", 1)
        self.ap.keys.send.assert_not_called()

    def test_plot_button_click_scales_and_follows_system_confirmation_without_select_keys(self):
        screen = self.galaxy_map.screen
        screen.screen_width, screen.screen_height = 3840, 2160
        screen.mon = {"left": -3840, "top": 120}
        self.ap.nav_route.get_last_system.side_effect = ["", "", "LHS 20"]
        events = Mock()
        events.attach_mock(self.galaxy_map.mouse.do_click, "click")
        events.attach_mock(self.ap.nav_route.get_last_system, "route")
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch.object(self.galaxy_map, "_enter_system_name", return_value=True),
            patch("panels.galaxy_map.sleep"),
            patch.object(self.galaxy_map, "_save_search_debug") as snapshot,
        ):
            events.attach_mock(snapshot, "snapshot")
            self.assertTrue(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))

        events.assert_has_calls(
            [
                call.route(),
                call.click(-208, 1330),
                call.route(),
            ]
        )
        self.assertEqual(self.galaxy_map.mouse.do_click.call_args_list, [call(-1961, 436), call(-208, 1330)])
        self.assertEqual(self.ap.keys.send.call_args_list, [call("GalaxyMapOpen")])
        self.ap.keys.send_key.assert_not_called()
        snapshot.assert_not_called()

    def test_wrong_first_result_is_not_clicked(self):
        self.ap.ocr.image_simple_ocr.return_value = ["LHS 200"]
        clock = [0.0]
        with (
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        ):
            self.assertFalse(self.galaxy_map._select_first_result("LHS 20", 1))
        self.galaxy_map.mouse.do_click.assert_not_called()

    def test_unconfirmed_system_information_never_clicks_plot_button(self):
        self.ap.nav_route.get_last_system.return_value = ""
        self.ap.ocr.image_simple_ocr.side_effect = lambda image: (
            ["LHS 200"]
            if self.ap.ocr.capture_region_pct.call_args.args[0] == self.galaxy_map.reg["selected-system"]
            else ["LHS 20"]
        )
        clock = [0.0]
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch.object(self.galaxy_map, "_enter_system_name", return_value=True),
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        ):
            self.assertFalse(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
        self.assertEqual(self.galaxy_map.mouse.do_click.call_args_list, [call(939, 157)] * 2)
        self.assertEqual(self.ap.keys.send.call_args_list, [call("GalaxyMapOpen")])

    def test_leaving_map_after_selection_prevents_plot_button_click(self):
        self.ap.nav_route.get_last_system.return_value = ""
        self.galaxy_map.status_parser.get_gui_focus.return_value = 0
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch.object(self.galaxy_map, "_enter_system_name", return_value=True),
            patch.object(self.galaxy_map, "_select_first_result", return_value=True),
            patch("panels.galaxy_map.sleep"),
        ):
            self.assertFalse(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
        self.galaxy_map.mouse.do_click.assert_not_called()
        self.ap.keys.send.assert_not_called()

    def test_unconfirmed_selection_never_sends_plot_keys(self):
        self.ap.nav_route.get_last_system.return_value = ""
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch.object(self.galaxy_map, "_enter_system_name", return_value=True),
            patch.object(self.galaxy_map, "_select_first_result", return_value=False) as select,
            patch("panels.galaxy_map.sleep"),
        ):
            self.assertFalse(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
        self.assertEqual(select.call_count, 2)
        self.assertEqual(self.ap.keys.send.call_args_list, [call("GalaxyMapOpen")])
        self.ap.keys.send_key.assert_not_called()
        self.galaxy_map.mouse.do_click.assert_not_called()

    def test_search_text_requires_exact_name_but_ignores_case_and_extra_spaces(self):
        for texts, expected in (
            (["lhs", " 20 "], True),
            (["LHS 200"], False),
            (["GALAXIE DURCHSUCHEN ..."], False),
            (None, False),
        ):
            with self.subTest(texts=texts):
                self.ap.ocr.image_simple_ocr.return_value = texts
                self.assertEqual(self.galaxy_map._search_field_matches("LHS 20"), expected)

    def test_search_click_scales_and_includes_monitor_offset(self):
        screen = self.galaxy_map.screen
        screen.screen_width, screen.screen_height = 3840, 2160
        screen.mon = {"left": -3840, "top": 120}
        self.ap.keys.activate_window = True
        with patch("panels.galaxy_map.sleep"), patch("panels.galaxy_map.set_focus_elite_window") as focus:
            self.assertTrue(self.galaxy_map._enter_system_name("LHS 20"))

        focus.assert_called_once()
        self.galaxy_map.mouse.do_click.assert_called_once_with(-1988, 370)

    def test_failed_text_entry_is_retried_without_submitting_or_plotting(self):
        self.ap.nav_route.get_last_system.return_value = ""
        self.ap.ocr.image_simple_ocr.return_value = ["GALAXIE DURCHSUCHEN ..."]
        clock = [0.0]
        with (
            patch("panels.galaxy_map.logger.isEnabledFor", return_value=True),
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
            patch("panels.galaxy_map.hotkey") as hotkey,
            patch("panels.galaxy_map.typewrite") as typewrite,
        ):
            self.assertFalse(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))

        self.assertEqual(self.galaxy_map.mouse.do_click.call_count, 3)
        self.assertEqual(self.save_case.call_count, 3)
        self.assertEqual(hotkey.call_args_list, [call("ctrl", "a")] * 3)
        self.assertEqual(typewrite.call_args_list, [call("LHS 20", interval=0.25)] * 3)
        self.ap.keys.send_key.assert_not_called()
        self.assertEqual(self.ap.keys.send.call_args_list, [call("GalaxyMapOpen")])

    def test_search_entry_recovers_on_third_click(self):
        self.ap.ocr.image_simple_ocr.side_effect = lambda image: (
            ["LHS 20"] if self.galaxy_map.mouse.do_click.call_count == 3 else None
        )
        clock = [0.0]
        with (
            patch("panels.galaxy_map.logger.isEnabledFor", return_value=True),
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        ):
            self.assertTrue(self.galaxy_map._enter_system_name("LHS 20"))
        self.assertEqual(self.galaxy_map.mouse.do_click.call_count, 3)
        self.assertEqual(self.save_case.call_count, 2)

    def test_no_search_click_if_galaxy_map_is_not_active(self):
        self.galaxy_map.status_parser.get_gui_focus.return_value = 0
        self.ap.nav_route.get_last_system.return_value = ""
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch("panels.galaxy_map.typewrite") as typewrite,
        ):
            self.assertFalse(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))
        self.galaxy_map.mouse.do_click.assert_not_called()
        self.ap.keys.send.assert_not_called()
        typewrite.assert_not_called()

    def test_existing_route_is_reused_without_clicking_or_typing(self):
        self.assertTrue(self.run_destination_search(["LHS 20"]))
        self.galaxy_map.mouse.do_click.assert_not_called()
        self.ap.ocr.capture_region_pct.assert_not_called()

    def test_existing_route_does_not_send_map_key_after_focus_loss(self):
        self.galaxy_map.status_parser.get_gui_focus.return_value = 0
        self.assertTrue(self.run_destination_search(["LHS 20"]))
        self.ap.keys.send.assert_not_called()
        self.galaxy_map.mouse.do_click.assert_not_called()

    def run_destination_search(self, nav_route_values):
        self.ap.nav_route.get_last_system.side_effect = nav_route_values
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch.object(self.galaxy_map, "_select_first_result", return_value=True),
            patch("panels.galaxy_map.typewrite"),
            patch("panels.galaxy_map.sleep"),
        ):
            return self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20")

    def test_retries_wrong_route_and_succeeds_when_destination_is_confirmed(self):
        self.assertTrue(self.run_destination_search(["", "", "WRONG", "WRONG", "LHS 20"]))

        plot_attempts = [
            clicked for clicked in self.galaxy_map.mouse.do_click.call_args_list if clicked == call(1816, 605)
        ]
        self.assertEqual(len(plot_attempts), 2)
        self.assertEqual(self.ap.keys.send.call_args_list, [call("GalaxyMapOpen")])

    def test_stops_after_two_unconfirmed_mouse_plot_attempts_after_keyboard_failure(self):
        clock = [0.0]
        self.ap.nav_route.get_last_system.return_value = ""
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch.object(self.galaxy_map, "_enter_system_name_keyboard", return_value=False),
            patch.object(self.galaxy_map, "_select_first_result", return_value=True),
            patch.object(self.galaxy_map, "_save_search_debug") as snapshot,
            patch("panels.galaxy_map.typewrite"),
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch("panels.galaxy_map.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        ):
            self.assertFalse(self.galaxy_map.set_gal_map_destination_text_odyssey(self.ap, "LHS 20"))

        plot_attempts = [
            clicked for clicked in self.galaxy_map.mouse.do_click.call_args_list if clicked == call(1816, 605)
        ]
        self.assertEqual(len(plot_attempts), 2)
        self.assertEqual(self.ap.keys.send.call_args_list, [call("GalaxyMapOpen")])
        self.assertEqual(
            snapshot.call_args_list,
            [call("route-unconfirmed", "LHS 20", attempt) for attempt in range(2, 4)],
        )

    def test_bookmark_plot_succeeds_when_nav_route_matches_expected_system(self):
        self.ap.nav_route.get_last_system.return_value = "LHS 20"
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch("panels.galaxy_map.sleep"),
        ):
            result = self.galaxy_map.set_gal_map_dest_bookmark(self.ap, "Fav", 1, expected_system="LHS 20")

        self.assertTrue(result)
        self.assertEqual(self.ap.keys.send.call_args_list[-1], call("GalaxyMapOpen"))

    def test_bookmark_plot_fails_when_nav_route_never_matches_expected_system(self):
        clock = [0.0]
        self.ap.nav_route.get_last_system.return_value = "WRONG"
        with (
            patch.object(self.galaxy_map, "goto_galaxy_map"),
            patch("panels.galaxy_map.monotonic", side_effect=lambda: clock[0]),
            patch(
                "panels.galaxy_map.sleep",
                side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            ),
        ):
            result = self.galaxy_map.set_gal_map_dest_bookmark(self.ap, "Fav", 1, expected_system="LHS 20")

        self.assertFalse(result)
        self.assertGreaterEqual(self.ap.nav_route.get_last_system.call_count, 60)


if __name__ == "__main__":
    unittest.main()
