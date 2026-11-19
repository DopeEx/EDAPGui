"""Tests for localized station-services detection without the game."""

import unittest
from unittest import mock

from core import paths
from i18n.localization import LocalizationManager
from panels.station_services import EDStationServicesInShip, TradeDialogUnconfirmedError


class StationServicesTestCase(unittest.TestCase):
    def test_detection_uses_only_the_active_language(self):
        for language in ("en", "de"):
            with self.subTest(language=language):
                services = self.make_services(language)
                services.ocr.wait_for_text.return_value = True
                self.assertTrue(services.goto_station_services())
                texts = services.ocr.wait_for_text.call_args.args[1]
                expected = {"CONNECTED TO"} if language == "en" else {"VERBUNDEN MIT"}
                self.assertEqual(set(texts), expected)
                services.ap_ckb.assert_not_called()

    def test_timeout_is_not_reported_as_success(self):
        services = self.make_services("en")
        services.ocr.wait_for_text.return_value = False
        with self.assertLogs("ed_log", level="WARNING"):
            self.assertFalse(services.goto_station_services())
        services.ap_ckb.assert_called_once_with("log", "Could not confirm station services. Waypoint Assist stopped.")

    @staticmethod
    def make_services(language):
        services = EDStationServicesInShip.__new__(EDStationServicesInShip)
        services.ap = mock.Mock()
        services.keys = mock.Mock()
        services.ocr = mock.Mock()
        services.ap_ckb = mock.Mock()
        services.locale = LocalizationManager(str(paths.LOCALES_DIR), language)
        services.screen = mock.Mock(screen_width=1920, screen_height=1080)
        services.reg = {"connected_to": {"rect": [0.0, 0.0, 0.25, 0.25]}}
        return services


class MarketListSafetyTestCase(unittest.TestCase):
    def setUp(self):
        self.services = StationServicesTestCase.make_services("en")
        self.services.ocr.capture_region_pct.return_value = mock.Mock()

    def test_heading_requires_exact_expected_direction_and_active_language(self):
        for language, words, expected_sell, expected_buy in (
            ("en", ["SELL", "TO", "MARKET"], True, False),
            ("en", ["BUY", "FROM MARKET"], False, True),
            ("de", ["AM", "MARKT VERKAUFEN"], True, False),
            ("de", ["AM MARKT", "KAUFEN"], False, True),
            ("en", ["BUY", "FROM", "MARKET", "SELL"], False, False),
            ("en", ["AM MARKT KAUFEN"], False, False),
            ("en", [], False, False),
        ):
            with self.subTest(language=language, words=words):
                self.services.locale = LocalizationManager(str(paths.LOCALES_DIR), language)
                self.services.ocr.image_simple_ocr.return_value = words
                self.assertEqual(self.services.market_list_matches(True), expected_sell)
                self.assertEqual(self.services.market_list_matches(False), expected_buy)
                self.assertEqual(self.services.market_list_matches(None), expected_sell or expected_buy)

    def test_visible_commodity_dialog_rejects_background_list_heading(self):
        for title in ("BUY COMMODITY", "SELL COMMODITY"):
            with self.subTest(title=title):
                self.services.ocr.image_simple_ocr.side_effect = [["BUY FROM MARKET"], [title]]
                self.assertFalse(self.services.market_list_matches(False))

    def test_selection_only_enters_goods_list_after_matching_heading(self):
        for selling in (False, True):
            for matched in (False, True):
                with self.subTest(selling=selling, matched=matched):
                    self.services.keys.reset_mock()
                    with mock.patch.object(self.services, "wait_for_market_list", return_value=matched) as wait:
                        select = self.services.select_sell if selling else self.services.select_buy
                        self.assertEqual(select(self.services.keys), matched)
                    wait.assert_called_once_with(selling=selling)
                    calls = [call.args[0] for call in self.services.keys.send.call_args_list]
                    self.assertEqual("UI_Right" in calls, matched)

    def test_timeout_records_only_debug_diagnostics(self):
        with (
            mock.patch.object(self.services, "market_list_matches", return_value=False),
            mock.patch("panels.station_services.monotonic", side_effect=[0, 0, 11]),
            mock.patch("panels.station_services.sleep"),
            mock.patch("panels.station_services.save_case") as save,
            self.assertLogs("ed_log", level="WARNING"),
        ):
            self.assertFalse(self.services.wait_for_market_list(True))
        save.assert_not_called()

    def test_wait_for_market_list_accepts_a_later_matching_heading(self):
        with (
            mock.patch.object(self.services, "market_list_matches", side_effect=[False, True]) as matches,
            mock.patch("panels.station_services.monotonic", side_effect=[0, 0, 1]),
            mock.patch("panels.station_services.sleep"),
        ):
            self.assertTrue(self.services.wait_for_market_list(True))
        self.assertEqual(matches.call_count, 2)

    def test_timeout_records_heading_only_when_debug_is_enabled(self):
        image = mock.Mock()
        self.services._market_list_debug_image = image
        with (
            mock.patch.object(self.services, "market_list_matches", return_value=False),
            mock.patch("panels.station_services.monotonic", side_effect=[0, 0, 11]),
            mock.patch("panels.station_services.sleep"),
            mock.patch("panels.station_services.logger.isEnabledFor", return_value=True),
            mock.patch("panels.station_services.save_case") as save,
        ):
            self.assertFalse(self.services.wait_for_market_list(True))
        save.assert_called_once_with("market-list", "unconfirmed", {"heading": image})


class TradeDialogSafetyTestCase(unittest.TestCase):
    def setUp(self):
        self.services = StationServicesTestCase.make_services("en")
        self.cargo_parser = mock.Mock()
        self.cargo_parser.get_item_by_commodity_id.return_value = {"Count": 10}
        self.services.market_parser = mock.Mock()
        target = {
            "id": 128049197,
            "Name_Localised": "Polymere",
            "Stock": 2,
            "Demand": 1,
            "StockBracket": 3,
        }
        self.services.market_parser.current_data = {"Items": [target]}
        self.services.market_parser.get_item.return_value = target
        self.services.market_parser.can_buy_item.return_value = True
        self.services.market_parser.can_sell_item.return_value = True
        self.services.market_parser.get_buyable_items.return_value = [
            {"id": 128049204, "Name_Localised": "Halbleiter", "Stock": 10},
            target,
        ]
        self.services.market_parser.get_sellable_items.return_value = [target]
        patcher = mock.patch("panels.station_services.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mismatch_prevents_buy_and_sell_confirmation_and_quantity_inputs(self):
        for selling in (False, True):
            with self.subTest(selling=selling):
                services = self.services
                services.keys.reset_mock()
                with (
                    mock.patch.object(services, "confirm_trade_dialog", return_value=False),
                    self.assertRaises(TradeDialogUnconfirmedError),
                ):
                    if selling:
                        services.sell_commodity(services.keys, "128049197", 2, self.cargo_parser)
                    else:
                        services.buy_commodity(services.keys, "128049197", 2, 10)
                calls = services.keys.send.call_args_list
                self.assertEqual(sum(call.args[0] == "UI_Select" for call in calls), 1)
                self.assertFalse(any(call.args[0] in ("UI_Right", "UI_Left") for call in calls))
                self.assertEqual(services.market_parser.current_data["Items"][0]["StockBracket"], 3)

    def test_verified_dialog_allows_existing_trade_inputs(self):
        for selling in (False, True):
            with self.subTest(selling=selling):
                self.services.keys.reset_mock()
                with mock.patch.object(self.services, "confirm_trade_dialog", return_value=True) as confirm:
                    result = (
                        self.services.sell_commodity(self.services.keys, "128049197", 2, self.cargo_parser)
                        if selling
                        else self.services.buy_commodity(self.services.keys, "128049197", 2, 10)
                    )
                self.assertEqual(result, (True, 2))
                confirm.assert_called_once_with("Polymere", selling=selling)
                calls = self.services.keys.send.call_args_list
                self.assertEqual(sum(call.args[0] == "UI_Select" for call in calls), 2)
                self.assertIn(
                    mock.call("UI_Down", hold=0.05, repeat=0 if selling else 1),
                    calls,
                )

    def test_multi_row_selection_preserves_normal_down_input_timing(self):
        for selling in (False, True):
            with self.subTest(selling=selling):
                other_items = [
                    {"Name_Localised": f"Commodity {index}", "Stock": 10, "Demand": 10} for index in range(14)
                ]
                target = {
                    "id": 128049197,
                    "Name_Localised": "Polymere",
                    "Stock": 2,
                    "Demand": 2,
                }
                if selling:
                    self.services.market_parser.get_sellable_items.return_value = [*other_items, target]
                else:
                    self.services.market_parser.get_buyable_items.return_value = [*other_items, target]
                self.services.keys.reset_mock()
                with mock.patch.object(self.services, "confirm_trade_dialog", return_value=True):
                    result = (
                        self.services.sell_commodity(self.services.keys, "128049197", 1, self.cargo_parser)
                        if selling
                        else self.services.buy_commodity(self.services.keys, "128049197", 1, 10)
                    )

                self.assertEqual(result, (True, 1))
                self.assertIn(
                    mock.call("UI_Down", hold=0.05, repeat=14),
                    self.services.keys.send.call_args_list,
                )

    def test_no_matching_list_item_reports_failure(self):
        self.services.market_parser.get_buyable_items.return_value = []
        self.assertEqual(self.services.buy_commodity(self.services.keys, "128049197", 2, 10), (False, 0))
        self.services.keys.send.assert_not_called()

    def test_unknown_market_commodity_id_cannot_be_traded(self):
        self.services.market_parser.get_item.return_value = None
        self.assertEqual(self.services.buy_commodity(self.services.keys, "999999999", 2, 10), (False, 0))
        self.assertEqual(self.services.sell_commodity(self.services.keys, "999999999", 2, mock.Mock()), (False, 0))
        self.services.keys.send.assert_not_called()

    def test_buy_all_is_limited_by_stock_and_free_cargo(self):
        for stock, free_cargo, expected in ((10, 3, 3), (2, 10, 2)):
            with self.subTest(stock=stock, free_cargo=free_cargo):
                self.services.keys.reset_mock()
                self.services.market_parser.get_buyable_items.return_value = [
                    {"id": 128049197, "Name_Localised": "Polymere", "Stock": stock}
                ]
                with mock.patch.object(self.services, "confirm_trade_dialog", return_value=True):
                    self.assertEqual(
                        self.services.buy_commodity(self.services.keys, "128049197", 9999, free_cargo),
                        (True, expected),
                    )
                self.assertIn(mock.call("UI_Right", hold=4), self.services.keys.send.call_args_list)

    def test_sell_uses_maximum_only_when_request_covers_held_commodity(self):
        for requested, held, expected, maximum in (
            (9999, 23, 23, True),
            (2000, 23, 23, True),
            (23, 23, 23, True),
            (22, 23, 22, False),
        ):
            with self.subTest(requested=requested, held=held):
                self.services.keys.reset_mock()
                self.cargo_parser.get_item_by_commodity_id.return_value = {"Count": held}
                with mock.patch.object(self.services, "confirm_trade_dialog", return_value=True):
                    result = self.services.sell_commodity(self.services.keys, "128049197", requested, self.cargo_parser)
                self.assertEqual(result, (True, expected))
                calls = self.services.keys.send.call_args_list
                self.assertNotIn(mock.call("UI_Right", hold=4), calls)
                self.assertEqual(mock.call("UI_Left", hold=4.0) in calls, not maximum)
                self.assertEqual(mock.call("UI_Right", hold=0.04, repeat=expected) in calls, not maximum)
                if maximum:
                    quantity_calls = [sent for sent in calls if sent.args[0] in ("UI_Left", "UI_Right")]
                    self.assertEqual(quantity_calls, [])
                    self.assertEqual(calls[-2:], [mock.call("UI_Down"), mock.call("UI_Select")])

    def test_sell_without_held_commodity_stops_before_inputs(self):
        for item in (None, {"Count": 0}):
            with self.subTest(item=item):
                self.cargo_parser.get_item_by_commodity_id.return_value = item
                self.assertEqual(
                    self.services.sell_commodity(self.services.keys, "128049197", 9999, self.cargo_parser),
                    (False, 0),
                )
                self.services.keys.send.assert_not_called()
                self.services.ap_ckb.assert_called_with(
                    "log", "No units of 'Polymere' are available for sale. Waypoint Assist stopped."
                )

    def test_fixed_buy_uses_maximum_only_when_stock_or_capacity_limits_request(self):
        for requested, stock, free_cargo, expected, maximum in (
            (23, 100, 23, 23, True),
            (2000, 100, 23, 23, True),
            (23, 23, 100, 23, True),
            (22, 100, 23, 22, False),
        ):
            with self.subTest(requested=requested, stock=stock, free_cargo=free_cargo):
                self.services.keys.reset_mock()
                self.services.market_parser.get_buyable_items.return_value = [
                    {"id": 128049197, "Name_Localised": "Polymere", "Stock": stock}
                ]
                with mock.patch.object(self.services, "confirm_trade_dialog", return_value=True):
                    self.assertEqual(
                        self.services.buy_commodity(self.services.keys, "128049197", requested, free_cargo),
                        (True, expected),
                    )
                calls = self.services.keys.send.call_args_list
                self.assertEqual(mock.call("UI_Right", hold=4) in calls, maximum)
                self.assertEqual(mock.call("UI_Right", hold=0.04, repeat=expected) in calls, not maximum)

    def test_confirmation_timeout_sends_no_inputs(self):
        with (
            mock.patch.object(self.services, "trade_dialog_matches", return_value=False),
            mock.patch("panels.station_services.monotonic", side_effect=[100, 114.9, 115.1]),
        ):
            self.assertFalse(self.services.confirm_trade_dialog("Polymere", False))
        self.services.keys.send.assert_not_called()
        self.services.ap_ckb.assert_not_called()

    def test_text_must_match_whole_name_and_trade_direction(self):
        for language, title, name, expected in (
            ("de", "WARE KAUFEN", "POLYMERE", True),
            ("en", "BUY COMMODITY", "POLYMERE", True),
            ("de", "WARE VERKAUFEN", "POLYMERE", False),
            ("de", "WARE KAUFEN", "POLYMERE EXTRA", False),
            ("de", "WARE KAUFEN", "", False),
            ("de", "", "POLYMERE", False),
            ("en", "WARE KAUFEN", "POLYMERE", False),
        ):
            with self.subTest(language=language, title=title, name=name):
                self.services.locale = LocalizationManager(str(paths.LOCALES_DIR), language)
                self.services.ocr.image_simple_ocr.side_effect = [[title], [name]]
                self.assertEqual(self.services.trade_dialog_matches("Polymere", False), expected)

    def test_mismatched_dialog_remembers_recognized_commodity_only_with_valid_title(self):
        for title, name, expected_stored in (
            ("BUY COMMODITY", "SAMARIUM", "SAMARIUM"),
            ("BUY COMMODITY", "POLYMERE", None),
            ("SOMETHING ELSE", "SAMARIUM", None),
            ("BUY COMMODITY", "", None),
        ):
            with self.subTest(title=title, name=name):
                self.services.ocr.image_simple_ocr.side_effect = [[title], [name]]
                self.services.trade_dialog_matches("Polymere", False)
                self.assertEqual(self.services._last_dialog_commodity, expected_stored)

    def test_correction_realigns_cursor_and_reverifies_dialog(self):
        items = [{"Name_Localised": "Praseodym"}, {"Name_Localised": "Samarium"}, {"Name_Localised": "Silber"}]
        for recognized, expected_index, direction, delta in (
            ("SAMARIUM", 2, "UI_Down", 1),
            ("SILBER", 0, "UI_Up", 2),
        ):
            with self.subTest(recognized=recognized):
                self.services.keys.reset_mock()
                self.services._last_dialog_commodity = recognized
                with (
                    mock.patch.object(self.services, "wait_for_market_list", return_value=True) as wait,
                    mock.patch.object(self.services, "confirm_trade_dialog", return_value=True) as confirm,
                ):
                    self.assertTrue(
                        self.services.correct_trade_selection(self.services.keys, items, expected_index, "Silber", True)
                    )
                wait.assert_called_once_with(True)
                confirm.assert_called_once_with("Silber", True)
                calls = self.services.keys.send.call_args_list
                self.assertEqual(calls[0], mock.call("UI_Back"))
                self.assertIn(mock.call(direction, hold=0.05, repeat=delta), calls)
                self.assertEqual(calls[-1], mock.call("UI_Select"))

    def test_correction_requires_an_unambiguous_recognized_commodity(self):
        items = [{"Name_Localised": "Samarium"}, {"Name_Localised": "Samarium"}, {"Name_Localised": "Silber"}]
        for recognized, expected_index in (
            (None, 2),  # dialog was never readable
            ("GOLD", 2),  # not in the navigated list
            ("SAMARIUM", 2),  # ambiguous duplicate rows
            ("SILBER", 2),  # OCR disagrees with the match result
        ):
            with self.subTest(recognized=recognized):
                self.services.keys.reset_mock()
                self.services._last_dialog_commodity = recognized
                self.assertFalse(
                    self.services.correct_trade_selection(self.services.keys, items, expected_index, "Silber", True)
                )
                self.services.keys.send.assert_not_called()

    def test_correction_stops_without_trading_when_market_list_is_not_confirmed(self):
        items = [{"Name_Localised": "Samarium"}, {"Name_Localised": "Silber"}]
        self.services._last_dialog_commodity = "SAMARIUM"
        with (
            mock.patch.object(self.services, "wait_for_market_list", return_value=False),
            mock.patch.object(self.services, "confirm_trade_dialog") as confirm,
        ):
            self.assertFalse(self.services.correct_trade_selection(self.services.keys, items, 1, "Silber", True))
        confirm.assert_not_called()
        calls = [call.args[0] for call in self.services.keys.send.call_args_list]
        self.assertEqual(calls, ["UI_Back"])

    def test_unconfirmed_dialog_attempts_correction_before_raising(self):
        for selling in (False, True):
            for corrected in (False, True):
                with self.subTest(selling=selling, corrected=corrected):
                    services = self.services
                    services.keys.reset_mock()
                    with (
                        mock.patch.object(services, "confirm_trade_dialog", return_value=False),
                        mock.patch.object(services, "correct_trade_selection", return_value=corrected) as correct,
                    ):
                        if corrected:
                            result = (
                                services.sell_commodity(services.keys, "128049197", 2, self.cargo_parser)
                                if selling
                                else services.buy_commodity(services.keys, "128049197", 2, 10)
                            )
                            self.assertEqual(result, (True, 2))
                        else:
                            with self.assertRaises(TradeDialogUnconfirmedError):
                                if selling:
                                    services.sell_commodity(services.keys, "128049197", 2, self.cargo_parser)
                                else:
                                    services.buy_commodity(services.keys, "128049197", 2, 10)
                    expected_items = (
                        services.market_parser.get_sellable_items.return_value
                        if selling
                        else services.market_parser.get_buyable_items.return_value
                    )
                    expected_index = expected_items.index(
                        next(item for item in expected_items if str(item.get("id", "")) == "128049197")
                    )
                    correct.assert_called_once_with(
                        services.keys, expected_items, expected_index, "Polymere", selling=selling
                    )
