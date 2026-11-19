"""Regression tests with real game screenshots.

The screenshots are not part of the repository. Put them in tests/fixtures/screenshots/<height>p/ named
NN_<scene>_<state>[_<language>].png (for example 2160p/12_target-occluded_moon.png); the tests skip when there are
none. The file names tell what is expected:
 - target-occluded / target-visible / aligned-target: the target ring is detected as occluded or not
 - compass / aligned-target: the compass is found
 - jump-align-msg: OCR recognizes the localized FSD alignment prompt in the HUD
 - disengage-msg: the message gate's color, line and compact-glyph checks accept the prompt; the OCR is not run
 - no-msg: the message gate must not pass (guards against false alarms that trigger slow OCR runs)
 - station-services: OCR recognizes the localized header in each provided color scheme
 - galaxy-map: OCR recognizes the map header in each provided color scheme
 - trade-buy / trade-sell: the modal commodity and transaction direction match; background goods do not
 - market-list: the active buy/sell list heading matches, independently of focus and details
 - nav-panel: the selected system/navigation/transactions/contacts tab is identified in both color schemes
 - internal-panel: the selected right-panel tab, including icon tabs, is identified in both color schemes
Screenshots whose names contain "crop" or "head-turned" are ignored.
Diagnostic crops are replayed separately from full-screen fixtures; later navigation panel views are context only.
"""

import unittest
from unittest import skipUnless
from unittest.mock import Mock, patch

import cv2
import numpy as np

from core import paths
from core.constants import GuiFocusGalaxyMap
from gui.cv_view import CvFrames
from i18n.localization import LocalizationManager
from panels.galaxy_map import EDGalaxyMap
from panels.internal_status import EDInternalStatusPanel
from panels.navigation import EDNavigationPanel
from panels.station_services import EDStationServicesInShip
from tests.helpers import FIXTURES, ScreenshotScreen, load_screenshot
from vision.detection import DetectionMixin
from vision.regions import ScreenRegions
from vision.templates import ImageTemplates

try:
    from vision.ocr import OCR
except ImportError:
    OCR = None

SCREENSHOTS = FIXTURES / "screenshots"
IGNORED = ("crop", "head-turned")
ALIGNMENT_SCREENSHOTS = tuple(sorted(SCREENSHOTS.glob("*p/*jump-align-msg*.png")))
STATION_SCREENSHOTS = tuple(sorted(SCREENSHOTS.glob("*p/*station-services*.png")))
TRADE_SCREENSHOTS = tuple(sorted(SCREENSHOTS.glob("*p/*trade-*.png")))
MARKET_LIST_SCREENSHOTS = tuple(sorted(SCREENSHOTS.glob("*p/*market-list*.png")))
GALAXY_MAP_SCREENSHOTS = tuple(sorted(SCREENSHOTS.glob("*p/*galaxy-map*.png")))
NAV_PANEL_SCREENSHOTS = tuple(sorted(SCREENSHOTS.glob("*p/*nav-panel*.png")))
INTERNAL_PANEL_SCREENSHOTS = tuple(sorted(SCREENSHOTS.glob("*p/*internal-panel*.png")))
NAVIGATION_FAILURES = SCREENSHOTS / "diagnostics" / "navigation-failures-20261118"


def screenshot_language(path):
    return "en" if "en" in path.stem.split("_") else "de"


def screenshot_ocr(path, engines):
    language = screenshot_language(path)
    if language not in engines:
        locale = LocalizationManager(str(paths.LOCALES_DIR), language)
        engines[language] = OCR(screen=None, language=locale.ocr_language)
    return engines[language]


@skipUnless(OCR is not None, "PaddleOCR unavailable")
class NavigationFailureScreenshotTestCase(unittest.TestCase):
    def test_selected_destination_in_local_failure_crops(self):
        cases = (
            ("01_navigation_ohm-city-reread_de.png", 1920, 1080, "OHM CITY"),
            ("03_navigation_tauri-b7-highlight_en.png", 3840, 2160, "60 TAURI B 7"),
            ("04_navigation_tauri-b7-highlight_en.png", 3840, 2160, "60 TAURI B 7"),
            ("07_navigation_arcadian-selected_de.png", 1920, 1080, "ARCADIAN"),
            ("09_navigation_wolf-works-selected_de.png", 1920, 1080, "WOLF WORKS"),
            ("19_navigation_ziegler-selected_asp_de.png", 1920, 1080, "ZIEGLER CHEMICAL"),
        )
        if not any((NAVIGATION_FAILURES / name).is_file() for name, *_ in cases):
            self.skipTest("local navigation failure crops unavailable")

        engines = {}
        for name, width, height, expected in cases:
            with self.subTest(screenshot=name):
                path = NAVIGATION_FAILURES / name
                if not path.is_file():
                    self.skipTest("local navigation failure crop unavailable")
                ocr = screenshot_ocr(path, engines)
                panel = EDNavigationPanel.__new__(EDNavigationPanel)
                panel.screen = Mock(screen_width=width, screen_height=height)
                panel.ocr = ocr
                with (
                    patch.object(ocr, "capture_region_pct", return_value=load_screenshot(path)),
                    patch.object(panel, "_save_recognition_failure") as failure,
                ):
                    self.assertEqual(panel._selected_destination(), expected)
                failure.assert_not_called()


@skipUnless(OCR is not None and NAV_PANEL_SCREENSHOTS, "PaddleOCR or local navigation-panel screenshots unavailable")
class NavigationPanelScreenshotTestCase(unittest.TestCase):
    def test_selected_tab_and_station_in_both_color_schemes(self):
        engines = {}
        for path in NAV_PANEL_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                ocr = screenshot_ocr(path, engines)
                screen = ScreenshotScreen(load_screenshot(path))
                ocr.screen = screen
                locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                with patch("panels.navigation.StatusParser"):
                    panel = EDNavigationPanel(Mock(ocr=ocr, locale=locale), screen, Mock(), Mock())
                expected = path.stem.split("_")[2]
                self.assertEqual(panel._selected_tab(), expected)
                if expected == "navigation" and "tab-only" not in path.stem.split("_"):
                    destination = "NAV BEACON" if "nav-beacon" in path.stem.split("_") else "OHM CITY"
                    self.assertEqual(panel._selected_destination(), destination)

    def test_long_name_and_numeric_suffix_survive_icons_and_noisy_distance_column(self):
        ocr = OCR(screen=None)
        name = "ORBITAL CONSTRUCTION SITE WINGROVES INHERITANCE ABC123"
        for background in ((255, 255, 255), (0, 255, 255)):
            with self.subTest(background=background):
                row = np.full((80, 1600, 3), background, dtype=np.uint8)
                cv2.putText(row, name, (90, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)
                cv2.rectangle(row, (10, 5), (45, 70), (0, 0, 0), 3)
                cv2.rectangle(row, (1100, 5), (1125, 70), (0, 0, 0), 3)
                cv2.putText(row, "1187LS 0", (1400, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
                image = EDNavigationPanel._destination_name_image(row)
                self.assertEqual(" ".join(ocr.image_simple_ocr(image) or []), name)


@skipUnless(OCR is not None and INTERNAL_PANEL_SCREENSHOTS, "PaddleOCR or local internal-panel screenshots unavailable")
class InternalPanelScreenshotTestCase(unittest.TestCase):
    def test_selected_tab_in_both_color_schemes(self):
        engines = {}
        for path in INTERNAL_PANEL_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                ocr = screenshot_ocr(path, engines)
                screen = ScreenshotScreen(load_screenshot(path))
                ocr.screen = screen
                locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                with patch("panels.internal_status.StatusParser"):
                    panel = EDInternalStatusPanel(Mock(ocr=ocr, locale=locale), screen, Mock(), Mock())
                self.assertEqual(panel._selected_tab(), path.stem.split("_")[2].replace("-", "_"))


class NoOcr:
    def image_simple_ocr(self, image):
        return None


class Detector(DetectionMixin):
    def __init__(self):
        self.cv_frames = CvFrames()

    ocr = NoOcr()


class CompassFailureScreenshotTestCase(unittest.TestCase):
    def test_panther_hollow_marker_over_cockpit_glare(self):
        path = SCREENSHOTS / "diagnostics" / "compass" / "01_compass-behind_panther-hull-glare-context.png"
        if not path.is_file():
            self.skipTest("local Panther compass diagnostic context unavailable")
        screenshot = np.zeros((1080, 1920, 3), dtype=np.uint8)
        # The doubled compass ROI was clipped at the bottom of the original frame.
        screenshot[513:1080, 423:1037] = load_screenshot(path)
        regions = ScreenRegions(ScreenshotScreen(screenshot), ImageTemplates(1080))
        with patch.object(Detector, "dump_compass_debug"):
            offset = Detector().get_nav_offset(regions)
        assert offset is not None, "the visible hollow marker was not detected"
        self.assertEqual(offset["z"], -1.0)
        self.assertLess(abs(offset["x"]), 0.12)
        self.assertTrue(-0.5 < offset["y"] < -0.25, offset)

    def test_left_shifted_compass_is_detected_instead_of_radar(self):
        path = NAVIGATION_FAILURES / "12_compass_radar-false-match-context_de.png"
        if not path.is_file():
            self.skipTest("local compass diagnostic context unavailable")
        context = load_screenshot(path)
        height, width = context.shape[:2]
        screenshot = np.zeros((1080, 1920, 3), dtype=np.uint8)
        # The expanded diagnostic crop originally started at (508, 513) in the 1080p frame.
        screenshot[513 : 513 + height, 508 : 508 + width] = context
        regions = ScreenRegions(ScreenshotScreen(screenshot), ImageTemplates(1080))
        with patch.object(Detector, "dump_compass_debug"):
            offset = Detector().get_nav_offset(regions)
        assert offset is not None, "the visible compass marker was not detected"
        self.assertEqual(offset["z"], 1.0)
        self.assertLess(abs(offset["x"]), 0.1)
        self.assertLess(abs(offset["y"]), 0.1)


def screenshots():
    for path in sorted(SCREENSHOTS.glob("*p/*.png")):
        if not any(word in path.name for word in IGNORED):
            yield path


@unittest.skipUnless(any(screenshots()), "no local screenshots in tests/fixtures/screenshots")
class ScreenshotTestCase(unittest.TestCase):
    def test_screenshots(self):
        templates = {}
        for path in screenshots():
            image = load_screenshot(path)
            height = image.shape[0]
            self.assertEqual(f"{height}p", path.parent.name, f"{path.name} is in the wrong folder")
            if height not in templates:
                templates[height] = ImageTemplates(height)
            regions = ScreenRegions(ScreenshotScreen(image), templates[height])
            detector = Detector()
            name = path.name

            with self.subTest(screenshot=f"{path.parent.name}/{name}"):
                self.assertEqual(
                    detector.is_destination_occluded(regions),
                    "target-occluded" in name,
                    "occluded target",
                )
                if "compass" in name or "aligned-target" in name:
                    self.assertTrue(detector.have_destination(regions), "compass")
                if "compass-behind" in name:
                    self.assertEqual(detector.get_nav_offset(regions)["z"], -1.0, "nav point behind")
                if "type9" in name and "compass" in name:
                    _, (_, score, _, _), scale = regions.match_compass_multiscale()
                    self.assertGreater(scale, 1.1, "larger compass sphere of the Type-9")
                    if "compass-behind-on-rim" in name or "aligned-target" in name or "compass-ahead" in name:
                        self.assertGreater(score, 0.75, "compass match")
                if "compass-behind-on-rim" in name:
                    offset = detector.get_nav_offset(regions)
                    self.assertGreater(max(abs(offset["x"]), abs(offset["y"])), 0.5, "nav point at the sphere rim")
                if "compass-ahead" in name:
                    self.assertEqual(detector.get_nav_offset(regions)["z"], 1.0, "nav point ahead")
                if "aligned-target_compass-ahead_type9" in name:
                    self.assertLess(abs(detector.get_nav_offset(regions)["pit"]), 6, "centered nav point")
                if "compass-ahead-off-target" in name:
                    self.assertGreater(detector.get_nav_offset(regions)["pit"], 10, "nav point above the center")
                # The message gate is only a cheap pre-check (bright scenes may pass it, the OCR decides)
                if "disengage-msg" in name:
                    self.assertTrue(regions.message_text_present(), "disengage message")
                if "no-msg" in name:
                    self.assertFalse(regions.message_text_present(), "false message alarm")


@skipUnless(OCR is not None and ALIGNMENT_SCREENSHOTS, "PaddleOCR or local FSD alignment screenshots unavailable")
class FsdAlignmentPromptScreenshotTestCase(unittest.TestCase):
    def test_alignment_prompt_is_read_in_all_local_screenshots(self):
        templates = ImageTemplates(2160)
        detector = Detector()
        engines = {}

        for path in ALIGNMENT_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                detector.locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                detector.ocr = screenshot_ocr(path, engines)
                image = load_screenshot(path)
                regions = ScreenRegions(ScreenshotScreen(image), templates)
                self.assertTrue(detector.fsd_alignment_prompt(regions))


@skipUnless(OCR is not None and STATION_SCREENSHOTS, "PaddleOCR or local station-services screenshots unavailable")
class StationServicesScreenshotTestCase(unittest.TestCase):
    def test_station_header_is_recognized_in_all_color_schemes(self):
        engines = {}
        for path in STATION_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                ocr = screenshot_ocr(path, engines)
                screen = ScreenshotScreen(load_screenshot(path))
                ocr.screen = screen
                services = EDStationServicesInShip.__new__(EDStationServicesInShip)
                services.ap = Mock(debug_overlay=False)
                services.keys = Mock()
                services.ap_ckb = Mock()
                services.screen = screen
                services.ocr = ocr
                services.locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                services.reg = {"connected_to": {"rect": [0.0, 0.0, 0.25, 0.25]}}
                # One real OCR poll is sufficient for a fully rendered static screenshot.
                wait = ocr.wait_for_text
                with patch.object(
                    ocr, "wait_for_text", side_effect=lambda ap, texts, region: wait(ap, texts, region, timeout=0.01)
                ):
                    self.assertTrue(services.goto_station_services())
                    services.ap_ckb.assert_not_called()


@skipUnless(OCR is not None and GALAXY_MAP_SCREENSHOTS, "PaddleOCR or local galaxy-map screenshots unavailable")
class GalaxyMapScreenshotTestCase(unittest.TestCase):
    def test_search_field_text_matches_only_the_complete_requested_system(self):
        ocr = OCR(screen=None)
        for path in GALAXY_MAP_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                ocr.screen = ScreenshotScreen(load_screenshot(path))
                galaxy = EDGalaxyMap.__new__(EDGalaxyMap)
                galaxy.ocr = ocr
                galaxy.reg = {"search": {"rect": [0.36, 0.10, 0.605, 0.132]}}
                entered = "search-entered-arcadian" in path.stem
                lhs = "first-result-lhs20" in path.stem
                self.assertEqual(galaxy._search_field_matches("ARCADIAN"), entered)
                self.assertFalse(galaxy._search_field_matches("ARCADIA"))
                text = " ".join(ocr.image_simple_ocr(ocr.capture_region_pct(galaxy.reg["search"])) or []).upper()
                self.assertIn("ARCADIAN" if entered else "LHS" if lhs else "GALAXIE DURCHSUCHEN", text)

    def test_first_result_is_exact_even_with_similar_systems_below(self):
        ocr = OCR(screen=None)
        tested = False
        for path in GALAXY_MAP_SCREENSHOTS:
            if "first-result-lhs20" not in path.stem:
                continue
            tested = True
            with self.subTest(screenshot=path.name):
                ocr.screen = ScreenshotScreen(load_screenshot(path))
                galaxy = EDGalaxyMap.__new__(EDGalaxyMap)
                galaxy.ocr = ocr
                galaxy.reg = {"first-result": {"rect": [0.379, 0.134, 0.60, 0.159]}}
                self.assertTrue(galaxy._region_matches("first-result", "LHS 20"))
                self.assertFalse(galaxy._region_matches("first-result", "LHS 200"))
        if not tested:
            self.skipTest("local first-result screenshots unavailable")

    def test_map_opening_header_is_recognized_on_first_poll_in_both_colors(self):
        ocr = OCR(screen=None)
        for path in GALAXY_MAP_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                screen = ScreenshotScreen(load_screenshot(path))
                ocr.screen = screen
                galaxy = EDGalaxyMap.__new__(EDGalaxyMap)
                galaxy.ap = Mock(debug_overlay=False)
                galaxy.ap.ship_control = Mock()
                galaxy.keys = Mock()
                galaxy.screen = screen
                galaxy.ocr = ocr
                galaxy.status_parser = Mock()
                galaxy.status_parser.get_gui_focus.return_value = GuiFocusGalaxyMap - 1
                galaxy.reg = {"cartographics": {"rect": [0.0, 0.0, 0.25, 0.25]}}
                wait = ocr.wait_for_text

                def recognize_once(ap, texts, region):
                    result = wait(ap, texts, region, timeout=0.01)
                    self.assertTrue(result, "map header must be recognized, not just time out")
                    return result

                with (
                    patch.object(
                        ocr,
                        "wait_for_text",
                        side_effect=recognize_once,
                    ) as recognition,
                    patch.object(ocr, "is_text_in_region", wraps=ocr.is_text_in_region) as poll,
                ):
                    galaxy.goto_galaxy_map()
                recognition.assert_called_once()
                poll.assert_called_once()
                self.assertIn("UI_Up", [call.args[0] for call in galaxy.keys.send.call_args_list])


@skipUnless(OCR is not None and MARKET_LIST_SCREENSHOTS, "PaddleOCR or local market-list screenshots unavailable")
class MarketListScreenshotTestCase(unittest.TestCase):
    def test_heading_identifies_list_direction_in_both_languages_and_color_schemes(self):
        engines = {}
        for path in MARKET_LIST_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                ocr = screenshot_ocr(path, engines)
                services = EDStationServicesInShip.__new__(EDStationServicesInShip)
                services.locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                services.screen = ScreenshotScreen(load_screenshot(path))
                services.ocr = ocr
                ocr.screen = services.screen
                selling = "sell" in path.stem.split("_")
                self.assertTrue(services.market_list_matches(selling))
                self.assertFalse(services.market_list_matches(not selling))

        for path in TRADE_SCREENSHOTS:
            with self.subTest(open_dialog=path.name):
                ocr = screenshot_ocr(path, engines)
                services.locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                services.screen = ScreenshotScreen(load_screenshot(path))
                services.ocr = ocr
                ocr.screen = services.screen
                self.assertFalse(services.market_list_matches(None))


@skipUnless(OCR is not None and TRADE_SCREENSHOTS, "PaddleOCR or local trade-dialog screenshots unavailable")
class TradeDialogScreenshotTestCase(unittest.TestCase):
    def test_selected_commodity_and_direction_in_both_color_schemes(self):
        engines = {}
        for path in TRADE_SCREENSHOTS:
            with self.subTest(screenshot=path.name):
                ocr = screenshot_ocr(path, engines)
                services = EDStationServicesInShip.__new__(EDStationServicesInShip)
                services.locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                services.screen = ScreenshotScreen(load_screenshot(path))
                services.ocr = ocr
                ocr.screen = services.screen
                selling = "trade-sell" in path.name
                name = "Halbleiter" if "halbleiter" in path.name else "Polymere"
                self.assertTrue(services.trade_dialog_matches(name, selling))
                self.assertFalse(
                    services.trade_dialog_matches("Polymere" if name == "Halbleiter" else "Halbleiter", selling)
                )
                self.assertFalse(services.trade_dialog_matches(name, not selling))

        for path in STATION_SCREENSHOTS:
            with self.subTest(no_trade_dialog=path.name):
                ocr = screenshot_ocr(path, engines)
                services.ocr = ocr
                services.locale = LocalizationManager(str(paths.LOCALES_DIR), screenshot_language(path))
                services.screen = ScreenshotScreen(load_screenshot(path))
                ocr.screen = services.screen
                self.assertFalse(services.trade_dialog_matches("Polymere", False))
                self.assertFalse(services.trade_dialog_matches("Polymere", True))


if __name__ == "__main__":
    unittest.main()
