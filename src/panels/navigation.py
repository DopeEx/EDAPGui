"""Handles the navigation panel."""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from time import monotonic, sleep

import cv2
import numpy as np

from core.constants import GuiFocusExternalPanel
from core.logger import logger
from game.status import StatusParser
from i18n.localization import normalize_text
from panels.ship_control import COCKPIT_VIEW_TIMEOUT
from panels.tab_navigation import confirm_panel_tab
from vision.diagnostics import save_case
from vision.regions import size_scale_for_station


class EDNavigationPanel:
    """The Navigation (Left hand) Ship Status Panel."""

    def __init__(self, ed_ap, screen, keys, cb):
        self.ap = ed_ap
        self.ocr = ed_ap.ocr
        self.screen = screen
        self.keys = keys
        self.ap_ckb = cb
        self.locale = self.ap.locale
        self.status_parser = StatusParser()

        self.nav_pnl_coords = None  # [top left, top right, bottom left, bottom right]
        self._destination_position: tuple[int, int, int | None] | None = None
        self._last_destination_name_image: np.ndarray | None = None

        # The rect is [L, T, R, B], top left x, y, and bottom right x, y in fraction of screen resolution
        # Nav Panel region covers the entire navigation panel.
        self.reg = {"nav_panel": {"rect": [0.11, 0.21, 0.70, 0.86]}, "temp_tab_bar": {"rect": [0.0, 0.2, 0.7, 0.35]}}
        self.sub_reg = {"tab_bar": {"rect": [0.0, 0.0, 1.0, 0.08]}, "location_panel": {"rect": [0.2218, 0.3, 0.8, 1.0]}}

        self.nav_pnl_tab_width = 260  # Nav panel tab width in pixels at 1920x1080
        self.nav_pnl_tab_height = 35  # Nav panel tab height in pixels at 1920x1080
        self.nav_pnl_location_width = 500  # Nav panel location width in pixels at 1920x1080
        self.nav_pnl_location_height = 35  # Nav panel location height in pixels at 1920x1080

    def _navigation_tab(self) -> bool:
        return self._show_tab("navigation")

    def _show_tab(self, target: str) -> bool:
        if not self.ap.ship_control.goto_cockpit_view(timeout=COCKPIT_VIEW_TIMEOUT):
            logger.warning("Could not return to the cockpit before opening the navigation panel.")
            return False
        self.keys.send("HeadLookReset")
        self.keys.send("UIFocus", state=1)
        try:
            self.keys.send("UI_Left")
        finally:
            self.keys.send("UIFocus", state=0)
        overlay_enabled = self.ap.debug_overlay
        if overlay_enabled:
            rect = self.screen.screen_rect_to_abs([0.10, 0.21, 0.70, 0.30])
            self.ap.overlay.overlay_rect1("nav_panel_active", rect, (0, 255, 0), 2)
            self.ap.overlay.overlay_paint()
        try:
            return confirm_panel_tab(
                self.status_parser, self.keys, self._selected_tab, GuiFocusExternalPanel, target, "Navigation panel"
            )
        finally:
            if overlay_enabled:
                self.ap.overlay.overlay_remove_rect("nav_panel_active")
                self.ap.overlay.overlay_paint()

    def _selected_tab(self) -> str | None:
        region = {"rect": [0.10, 0.21, 0.70, 0.30]}
        image = self.ocr.capture_region_pct(region)
        width, height = size_scale_for_station(35, 35, self.screen.screen_width, self.screen.screen_height)
        items = self.ocr.solid_highlighted_items(image, width, height)
        if not items:
            items = self.ocr.solid_highlighted_items(
                image, width, height, remove_thin_lines=True, intermediate_gray_fallback=True
            )
        if len(items) != 1:
            logger.debug("Navigation tab highlight count=%s; no unique selected tab.", len(items))
            self._save_recognition_failure("tab", image)
            return None
        cropped, x, _y, w, h = items[0]
        center = region["rect"][0] + (x + w / 2) / self.screen.screen_width
        if 0.11 <= center <= 0.15 and 0.7 <= w / h <= 1.4:
            return "system"
        if cropped.shape[1] < width * 4:
            self._save_recognition_failure("tab", image)
            return None
        text = normalize_text(" ".join(self.ocr.image_simple_ocr(cropped) or []))
        logger.debug("Navigation selected-tab OCR: text=%r", text)
        for tab in ("navigation", "transactions", "contacts", "target"):
            titles = {
                normalize_text(value) for value in self.locale.all_values(f"NAV_PNL_TAB_{tab.upper()}") if value != "?"
            }
            if text in titles:
                return tab
        self._save_recognition_failure("tab", image)
        return None

    def _save_recognition_failure(self, reason, image):
        if logger.isEnabledFor(logging.DEBUG):
            context = self.ocr.capture_region_pct(self.reg["nav_panel"])
            save_case("navigation-panel", reason, {"evaluated": image, "later-panel": context})

    def _selected_destination(self) -> str:
        self._destination_position = None
        image = self.ocr.capture_region_pct({"rect": [0.11, 0.35, 0.70, 0.86]})
        debug = logger.isEnabledFor(logging.DEBUG)
        if debug:
            self._last_destination_image = image.copy()
            self._last_destination_name_image = None
        width, height = size_scale_for_station(350, 25, self.screen.screen_width, self.screen.screen_height)
        items = self.ocr.solid_highlighted_items(image, width, height)
        if not items:
            items = self.ocr.solid_highlighted_items(
                image, width, height, remove_thin_lines=True, intermediate_value_fallback=True
            )
        if len(items) != 1:
            logger.debug("Navigation destination highlight count=%s; row not confirmed.", len(items))
            self._save_recognition_failure("destination", image)
            return ""
        row = items[0][0]
        name_image = self._destination_name_image(row)
        if row.shape[0] < 32:
            # Small glyphs can blur together in OCR; preserve the text rather than correcting O/0 afterward.
            name_image = cv2.resize(name_image, None, fx=1.75, fy=1.75, interpolation=cv2.INTER_CUBIC)
        if debug:
            self._last_destination_name_image = name_image.copy()
        text = normalize_text(" ".join(self.ocr.image_simple_ocr(name_image) or []))
        logger.debug("Navigation destination-name OCR: name=%r", text)
        if not text:
            self._save_recognition_failure("destination", image)
        else:
            _, _x, y, _w, h = items[0]
            self._destination_position = (y, h, self._destination_scrollbar_y(image))
        return text

    @staticmethod
    def _destination_scrollbar_y(image) -> int | None:
        strip = image[:, -max(2, round(image.shape[1] * 0.006)) :, :3]
        _, _, stats, centers = cv2.connectedComponentsWithStats((strip.max(axis=2) >= 140).astype(np.uint8))
        bars = [
            round(center[1])
            for (_x, _y, width, height, area), center in zip(stats[1:], centers[1:])
            if width >= 2 and height >= image.shape[0] * 0.04 and height >= 4 * width and area >= width * height * 0.6
        ]
        return bars[0] if len(bars) == 1 else None

    @staticmethod
    def _destination_name_image(row):
        """Isolate the name column of an already rectified highlighted destination."""
        height, width = row.shape[:2]
        # Keep the complete leading symbol until its component height has been measured.
        image = row[:, : int(width * 0.86)].copy()
        background = np.median(image.reshape(-1, 3), axis=0).astype(np.uint8)
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        markers = cv2.inRange(hsv, (35, 80, 40), (140, 255, 255))
        image[markers != 0] = background
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        background_gray = int(cv2.cvtColor(background.reshape(1, 1, 3), cv2.COLOR_BGR2GRAY)[0, 0])
        ink = cv2.inRange(gray, 0, max(0, background_gray - 35))
        # A rectified row border can join a leading icon to unrelated pixels.
        ink[[0, -1], :] = 0
        ink[:, [0, -1]] = 0
        count, labels, stats, _ = cv2.connectedComponentsWithStats(ink)
        components = [stat for stat in stats[1:] if height * 0.25 <= stat[cv2.CC_STAT_HEIGHT] <= height * 0.65]
        name_start = int(width * 0.04)
        icon_limit = width * 0.08
        spans = sorted((x, x + w) for x, _y, w, _h, _area in components if x + w > name_start)
        text_left = spans[0][0] if spans else 0
        for label in range(1, count):
            # Map pins and shields extend beyond the text line; do not OCR them as letters.
            x, _, w, h, _ = stats[label]
            if h > height * 0.65:
                image[labels == label] = background
                if name_start < x + w <= icon_limit and text_left - (x + w) > height * 0.25:
                    # Clear the soft-colored edges of a separated leading icon as well as its dark core.
                    image[:, : x + w + round(height * 0.2)] = background
        compact_icon_removed = False
        if spans:
            text_height = float(np.median([stat[cv2.CC_STAT_HEIGHT] for stat in components]))
            right = 0
            for left, end in spans:
                # Compact icons can be text-height, but are separated from the name by a wider gap.
                if right and left - right > text_height * 1.1 and right <= icon_limit and left <= width * 0.15:
                    image[:, :left] = background
                    compact_icon_removed = True
                    break
                right = max(right, end)
        # In shallow highlights the name can extend almost to the rectified row border.
        if height < 32:
            inset = max(1, round(height * 0.07))
            cropped = image[inset : height - inset, name_start:]
            # At this size, color on the selection chevrons can make OCR read them as letters.
            cropped = cv2.cvtColor(cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
            background[:] = background_gray
        else:
            cropped = image[int(height * 0.15) : int(height * 0.85), name_start:]
        if compact_icon_removed or cropped.shape[0] < 32:
            # Small rows and icon-cleaned crops benefit from additional OCR context.
            padding = max(1, cropped.shape[0] // 2)
            return cv2.copyMakeBorder(
                cropped,
                padding,
                padding,
                padding,
                padding,
                cv2.BORDER_CONSTANT,
                value=tuple(int(v) for v in background),
            )
        return cropped

    def _next_destination(self, previous: str, deadline: float) -> str | None:
        previous_position = getattr(self, "_destination_position", None)
        self.keys.send("UI_Down")
        change_deadline = min(deadline, monotonic() + 2.5)
        stable_reads = 0
        while monotonic() < change_deadline:
            sleep(0.2)
            current = self._selected_destination()
            position = getattr(self, "_destination_position", None)
            moved = False
            if previous_position is not None and position is not None:
                old_y, old_height, old_scroll = previous_position
                y, height, scroll = position
                moved = abs(y - old_y) > min(height, old_height) * 0.3 or (
                    old_scroll is not None and scroll is not None and abs(scroll - old_scroll) >= 2
                )
            if current and (current != previous or moved):
                if current == previous:
                    logger.debug(
                        "Navigation selection moved with unchanged name %r: %s -> %s",
                        current,
                        previous_position,
                        position,
                    )
                return current
            stable_reads = stable_reads + 1 if current == previous else 0
        # Only repeated readable, unchanged rows indicate the list boundary.
        return None if stable_reads >= 3 and change_deadline < deadline else ""

    def _lock_visible_destination(self, name: str, expected: str, deadline: float, attempt: int) -> bool:
        debug = logger.isEnabledFor(logging.DEBUG)
        near_match_saved = False
        if debug:
            self._last_destination_image = None
            self._last_destination_name_image = None
        if not self._navigation_tab():
            return False
        self.keys.send("UI_Up", hold=5)
        sleep(0.5)
        current = self._selected_destination()
        rows = 0
        while current and rows < 200 and monotonic() < deadline:
            rows += 1
            parsed_name = current
            logger.debug(
                "Station OCR scan: attempt=%s row=%s selected=%r parsed_name=%r expected=%r match=%s",
                attempt + 1,
                rows,
                current,
                parsed_name,
                expected,
                parsed_name == expected,
            )
            if parsed_name == expected:
                # Re-read before selection; never rely on a counted row position.
                reread = self._selected_destination()
                if reread != current:
                    logger.debug("Station OCR row changed before selection: previous=%r current=%r", current, reread)
                    self._save_last_destination_failure("row-changed")
                    return False
                self.keys.send("UI_Select")
                sleep(0.5)
                self.keys.send("UI_Select")
                if self.ap.wait_for_expected_in_system_target(name):
                    logger.debug("Station OCR target confirmed: target=%r attempt=%s", name, attempt + 1)
                    return True
                logger.warning("Station OCR selection did not confirm the requested target: %r", name)
                self._save_last_destination_failure("unconfirmed")
                return False
            if (
                debug
                and not near_match_saved
                and self._last_destination_image is not None
                and self._last_destination_name_image is not None
                and SequenceMatcher(None, expected, parsed_name, autojunk=False).ratio() >= 0.8
            ):
                save_case(
                    "navigation-panel",
                    "destination-near-match",
                    {"evaluated": self._last_destination_image, "name-crop": self._last_destination_name_image},
                )
                near_match_saved = True
            current = self._next_destination(current, deadline)
        logger.warning(
            "Station OCR did not find the requested destination: target=%r scan_complete=%s", name, current is None
        )
        logger.debug(
            "Station OCR scan stopped: rows=%s last_selection=%r deadline_reached=%s",
            rows,
            current,
            monotonic() >= deadline,
        )
        self._save_last_destination_failure("not-found")
        return False

    def _save_last_destination_failure(self, reason):
        if logger.isEnabledFor(logging.DEBUG):
            image = getattr(self, "_last_destination_image", None)
            if image is not None:
                self._save_recognition_failure(reason, image)
            self._last_destination_image = None

    def lock_destination(self, name: str) -> bool:
        """Select the first exact name match and verify the selected game target."""
        expected = normalize_text(name)
        if not expected:
            logger.warning("Station OCR rejected an empty destination.")
            self.ap_ckb("log", "Enter a station name before selecting a navigation target.")
            return False
        if self.ap.has_expected_in_system_target(self.ap.status.get_cleaned_data(), name):
            return True
        deadline = monotonic() + 120
        for attempt in range(2):
            if monotonic() >= deadline:
                break
            try:
                confirmed = self._lock_visible_destination(name, expected, deadline, attempt)
            finally:
                cockpit_ready = self.ap.ship_control.goto_cockpit_view(timeout=COCKPIT_VIEW_TIMEOUT)
                self.keys.send("HeadLookReset")
            if confirmed and cockpit_ready:
                return True
            if not cockpit_ready:
                logger.warning("Station OCR could not return to the cockpit; stopping target selection.")
                break
            if monotonic() >= deadline:
                break
        self.ap_ckb(
            "log",
            f"Could not confirm station '{name}' in the Navigation panel. "
            "Check the name and navigation filters, or configure a station bookmark.",
        )
        return False

    def request_docking(self) -> bool:
        """Request docking from Nav Panel."""
        try:
            if not self._show_tab("contacts"):
                self.ap_ckb("log", "Could not confirm the contacts tab. Docking request stopped.")
                return False
            self.keys.send("UI_Up", hold=4)
            self.keys.send("UI_Right")
            self.keys.send("UI_Select")
            sleep(0.5)
            return True
        finally:
            self.hide_nav_panel()

    def hide_nav_panel(self):
        """Hides the Nav Panel if open."""
        # Is nav panel active?
        if self.status_parser.get_gui_focus() == GuiFocusExternalPanel:
            self.ap.ship_control.goto_cockpit_view(timeout=COCKPIT_VIEW_TIMEOUT)


def dummy_cb(msg, body=None):
    pass


# Usage Example
if __name__ == "__main__":
    logger.setLevel(logging.DEBUG)  # Default to log all debug when running this file.
    from core.autopilot import Autopilot

    ap = Autopilot(cb=dummy_cb)
    ap.keys.activate_window = True  # Helps with single steps testing

    from vision.screen import set_focus_elite_window

    set_focus_elite_window()
    nav_pnl = EDNavigationPanel(ap, ap.scr, ap.keys, dummy_cb)
    nav_pnl.request_docking()
