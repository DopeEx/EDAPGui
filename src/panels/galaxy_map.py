"""Handles the Galaxy Map."""

from __future__ import annotations

import logging
from time import monotonic, sleep

from pyautogui import hotkey, typewrite

from controls.mouse_point import MousePoint
from core.constants import GuiFocusGalaxyMap
from core.logger import logger
from game.status import StatusParser
from vision.diagnostics import save_case
from vision.regions import reg_scale_for_station
from vision.screen import dpi_aware, set_focus_elite_window

MAX_NAV_ROUTE_PLOT_ATTEMPTS = 3
NAV_ROUTE_UPDATE_POLL_INTERVAL = 0.25
NAV_ROUTE_UPDATE_TIMEOUT = 15.0
MAX_SEARCH_ENTRY_ATTEMPTS = 3
SEARCH_ENTRY_CONFIRM_TIMEOUT = 3.0
SEARCH_RESULT_TIMEOUT = 10.0


class EDGalaxyMap:
    """Handles the Galaxy Map."""

    def __init__(self, ed_ap, screen, keys, cb, is_odyssey=True):
        self.ap = ed_ap
        self.ocr = ed_ap.ocr
        self.is_odyssey = is_odyssey
        self.screen = screen
        self.keys = keys
        self.mouse = MousePoint()
        self.status_parser = StatusParser()
        self.ap_ckb = cb
        # The rect is top left x, y, and bottom right x, y in fraction of screen resolution
        self.reg = {
            "cartographics": {"rect": [0.0, 0.0, 0.25, 0.25]},
            "search": {"rect": [0.36, 0.10, 0.605, 0.132]},
            "first-result": {"rect": [0.379, 0.134, 0.60, 0.159]},
            "selected-system": {"rect": [0.70, 0.195, 0.925, 0.225]},
            "plot-route": {"rect": [0.935, 0.539, 0.957, 0.582]},
        }

    def _save_search_debug(self, stage: str, target_name: str | None = None, attempt: int | None = None) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        image = self.screen.get_screen_full()
        logger.debug(
            "Galaxy Map search snapshot: stage=%s target=%r attempt=%s gui_focus=%s activate_window=%s",
            stage,
            target_name,
            attempt,
            self.status_parser.get_gui_focus(),
            self.keys.activate_window,
        )
        save_case("galaxy-map", stage, {"map": image})

    def _search_field_matches(self, target_name: str) -> bool:
        return self._region_matches("search", target_name)

    def _region_text(self, region: str) -> str:
        image = self.ocr.capture_region_pct(self.reg[region])
        return " ".join(" ".join(self.ocr.image_simple_ocr(image) or []).upper().split())

    def _region_matches(self, region: str, target_name: str) -> bool:
        recognized = self._region_text(region)
        expected = " ".join(target_name.upper().split())
        matched = bool(expected) and recognized == expected
        logger.debug(
            "Galaxy Map OCR: region=%s expected=%r recognized=%r matched=%s", region, expected, recognized, matched
        )
        return matched

    def _region_center(self, region: str) -> tuple[int, int]:
        left, top, right, bottom = self.screen.screen_rect_to_abs(self.reg[region]["rect"])
        x = self.screen.mon["left"] + (left + right) // 2
        y = self.screen.mon["top"] + (top + bottom) // 2
        return x, y

    def _click_region(self, region: str) -> None:
        x, y = self._region_center(region)
        if self.keys.activate_window:
            set_focus_elite_window()
        logger.debug("Galaxy Map click: region=%s x=%s y=%s", region, x, y)
        with dpi_aware():
            self.mouse.do_click(x, y)

    def _wait_for_first_result(self, target_name: str, attempt: int) -> bool:
        deadline = monotonic() + SEARCH_RESULT_TIMEOUT
        while monotonic() < deadline:
            if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                logger.warning("Galaxy Map is not active; result selection stopped.")
                self._save_search_debug("focus-lost", target_name, attempt)
                return False
            if self._region_matches("first-result", target_name):
                return True
            sleep(0.25)
        else:
            self._save_search_debug("first-result-unconfirmed", target_name, attempt)
            logger.warning("Galaxy Map first result did not confirm %s.", target_name)
            return False

    def _select_first_result(self, target_name: str, attempt: int) -> bool:
        if not self._wait_for_first_result(target_name, attempt):
            return False
        if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
            logger.warning("Galaxy Map is not active; result click stopped.")
            self._save_search_debug("focus-lost", target_name, attempt)
            return False
        self._click_region("first-result")
        sleep(0.25)
        deadline = monotonic() + SEARCH_RESULT_TIMEOUT
        while monotonic() < deadline:
            if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                logger.warning("Galaxy Map is not active; selected-system check stopped.")
                self._save_search_debug("focus-lost", target_name, attempt)
                return False
            if self._region_matches("selected-system", target_name):
                break
            sleep(0.25)
        else:
            self._save_search_debug("selected-system-unconfirmed", target_name, attempt)
            logger.warning("Galaxy Map system information did not confirm %s.", target_name)
            return False

        return True

    def _enter_system_name_keyboard(self, target_name: str) -> bool:
        if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
            logger.warning("Galaxy Map is not active; keyboard search stopped.")
            self._save_search_debug("focus-lost", target_name, 1)
            return False
        self.keys.send("UI_Up")
        self.keys.send("UI_Select")
        sleep(0.2)
        if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
            logger.warning("Galaxy Map is not active; keyboard text entry stopped.")
            self._save_search_debug("focus-lost", target_name, 1)
            return False
        hotkey("ctrl", "a")
        typewrite(target_name, interval=0.25)
        sleep(0.2)
        return True

    def _select_first_result_keyboard(self, target_name: str, attempt: int) -> bool:
        if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
            logger.warning("Galaxy Map is not active; keyboard result selection stopped.")
            self._save_search_debug("focus-lost", target_name, attempt)
            return False
        self.keys.send_key("Down", 28)
        sleep(0.05)
        self.keys.send_key("Up", 28)
        sleep(0.25)

        for key, delay in (
            ("UI_Down", 0.05),
            ("UI_Up", 0.05),
            ("UI_Right", 0.05),
            ("UI_Select", 0.5),
            ("CamZoomIn", 0.05),
        ):
            if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                logger.warning("Galaxy Map is not active; keyboard navigation stopped.")
                self._save_search_debug("focus-lost", target_name, attempt)
                return False
            self.keys.send(key)
            sleep(delay)
            if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                logger.warning("Galaxy Map is not active; keyboard navigation stopped.")
                self._save_search_debug("focus-lost", target_name, attempt)
                return False

        return True

    def _enter_system_name(self, target_name: str) -> bool:
        for attempt in range(1, MAX_SEARCH_ENTRY_ATTEMPTS + 1):
            if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                logger.warning("Galaxy Map is not active; system-name entry stopped.")
                self._save_search_debug("focus-lost", target_name, attempt)
                return False
            self._click_region("search")
            sleep(0.2)
            if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                logger.warning("Galaxy Map is not active; system-name entry stopped.")
                self._save_search_debug("focus-lost", target_name, attempt)
                return False
            hotkey("ctrl", "a")
            typewrite(target_name, interval=0.25)
            sleep(0.2)

            deadline = monotonic() + SEARCH_ENTRY_CONFIRM_TIMEOUT
            while monotonic() < deadline:
                if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                    logger.warning("Galaxy Map is not active; search confirmation stopped.")
                    self._save_search_debug("focus-lost", target_name, attempt)
                    return False
                if self._search_field_matches(target_name):
                    return True
                sleep(0.25)
            self._save_search_debug("search-text-unconfirmed", target_name, attempt)

        logger.warning(
            "Unable to confirm Galaxy Map search text for %s after %s attempts.", target_name, MAX_SEARCH_ENTRY_ATTEMPTS
        )
        return False

    def set_gal_map_dest_bookmark(
        self,
        ap,
        bookmark_type: str,
        bookmark_position: int,
        expected_system: str | None = None,
    ) -> bool:
        """Set the gal map destination using a bookmark.
        @param ap: ED_AP reference.
        @param bookmark_type: The bookmark type (Favorite, System, Body, Station or Settlement), Favorite
         being the default if no match is made with the other options.
        @param bookmark_position: The position in the bookmark list, starting at 1 for the first bookmark.
        @return: True if bookmark could be selected, else False
        """
        if self.is_odyssey and bookmark_position > 0:
            self.goto_galaxy_map()

            ap.keys.send("UI_Left")  # Go to BOOKMARKS
            sleep(0.5)
            ap.keys.send("UI_Select")  # Select BOOKMARKS
            sleep(0.25)
            ap.keys.send("UI_Right")  # Go to FAVORITES
            sleep(0.25)

            # If bookmark type is Fav, do nothing as this is the first item
            if bookmark_type.lower().startswith("sys"):
                ap.keys.send("UI_Down")  # Go to SYSTEMS
            elif bookmark_type.lower().startswith("bod"):
                ap.keys.send("UI_Down", repeat=2)  # Go to BODIES
            elif bookmark_type.lower().startswith("sta"):
                ap.keys.send("UI_Down", repeat=3)  # Go to STATIONS
            elif bookmark_type.lower().startswith("set"):
                ap.keys.send("UI_Down", repeat=4)  # Go to SETTLEMENTS

            sleep(0.25)
            ap.keys.send("UI_Select")  # Select bookmark type, moves you to bookmark list
            sleep(0.25)
            ap.keys.send("UI_Down", repeat=bookmark_position - 1)
            sleep(0.25)
            ap.keys.send("UI_Select", hold=3.0)

            # Close Galaxy map
            ap.keys.send("GalaxyMapOpen")
            sleep(0.5)
            if expected_system is None:
                return True
            return self.wait_for_nav_route(ap, expected_system)

        return False

    @staticmethod
    def wait_for_nav_route(ap, target_system: str, timeout: float = 15.0) -> bool:
        """Wait until NavRoute.json reports the requested final system."""
        if ap.nav_route is None:
            return False

        target_system_uc = target_system.upper()
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            current_system = ap.nav_route.get_last_system()
            if current_system.upper() == target_system_uc:
                logger.debug(f"Galaxy Map bookmark route confirmed: {target_system_uc}.")
                return True
            sleep(NAV_ROUTE_UPDATE_POLL_INTERVAL)

        logger.debug(f"Galaxy Map bookmark did not plot the expected system: {target_system_uc}.")
        return False

    def set_gal_map_destination_text(self, ap, target_name, target_select_cb=None) -> bool:
        """Call either the Odyssey or Horizons version of the Galactic Map sequence."""
        if not self.is_odyssey:
            return ap.galaxy_map.set_gal_map_destination_text_horizons(ap, target_name, target_select_cb)
        else:
            return ap.galaxy_map.set_gal_map_destination_text_odyssey(ap, target_name)

    def set_gal_map_destination_text_horizons(self, ap, target_name, target_select_cb=None) -> bool:
        """This sequence for the Horizons."""
        self.goto_galaxy_map()

        ap.keys.send("CycleNextPanel")
        sleep(1)
        ap.keys.send("UI_Select")
        sleep(2)

        typewrite(target_name, interval=0.25)
        sleep(1)

        # send enter key
        ap.keys.send_key("Down", 28)
        sleep(0.05)
        ap.keys.send_key("Up", 28)

        sleep(7)
        ap.keys.send("UI_Right")
        sleep(1)
        ap.keys.send("UI_Select")

        # if got passed through the ship() object, lets call it to see if a target has been
        # selected yet... otherwise we wait.  If long route, it may take a few seconds
        if target_select_cb is not None:
            while not target_select_cb()["target"]:
                sleep(1)

        # Close Galaxy map
        ap.keys.send("GalaxyMapOpen")
        sleep(2)
        return True

    def set_gal_map_destination_text_odyssey(self, ap, target_name) -> bool:
        """Set an Odyssey destination, retrying when the plotted route is not confirmed."""
        self.goto_galaxy_map()

        target_name_uc = target_name.upper()

        # Check if the current nav route is to the target system
        last_nav_route_sys = ap.nav_route.get_last_system()
        last_nav_route_sys_uc = last_nav_route_sys.upper()
        if last_nav_route_sys_uc == target_name_uc:
            if self.status_parser.get_gui_focus() == GuiFocusGalaxyMap:
                ap.keys.send("GalaxyMapOpen")
            return True

        correct_route = False
        for attempt in range(1, MAX_NAV_ROUTE_PLOT_ATTEMPTS + 1):
            keyboard = attempt == 1
            if keyboard:
                if not self._enter_system_name_keyboard(target_name_uc):
                    if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                        break
                    continue
                if not self._select_first_result_keyboard(target_name_uc, attempt):
                    if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                        break
                    continue
            else:
                if not self._enter_system_name(target_name_uc):
                    break
                if not self._select_first_result(target_name_uc, attempt):
                    if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                        break
                    continue
            # Store the current nav route system
            last_nav_route_sys = ap.nav_route.get_last_system()
            last_nav_route_sys_uc = last_nav_route_sys.upper()
            logger.debug(f"Previous Nav Route dest: {last_nav_route_sys_uc}.")
            if last_nav_route_sys_uc == target_name_uc:
                logger.debug("Galaxy Map route confirmed before another plot request.")
                correct_route = True
                break

            if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
                logger.warning("Galaxy Map is not active; route plotting stopped.")
                self._save_search_debug("focus-lost", target_name_uc, attempt)
                break
            if keyboard:
                self.keys.send("UI_Select", hold=0.75)
            else:
                self._click_region("plot-route")

            sleep(0.05)

            # Wait for the game to update NavRoute.json after plotting.
            if ap.nav_route is not None:
                logger.debug(
                    f"Waiting up to {NAV_ROUTE_UPDATE_TIMEOUT:g} seconds for Nav Route to update "
                    f"(attempt {attempt}/{MAX_NAV_ROUTE_PLOT_ATTEMPTS})."
                )
                deadline = monotonic() + NAV_ROUTE_UPDATE_TIMEOUT
                route_changed = False
                while monotonic() < deadline:
                    curr_nav_route_sys = ap.nav_route.get_last_system()
                    curr_nav_route_sys_uc = curr_nav_route_sys.upper()
                    # Check if the nav route has been changed (right or wrong)
                    if curr_nav_route_sys_uc != last_nav_route_sys_uc:
                        route_changed = True
                        logger.debug(
                            f"Nav Route dest changed from: {last_nav_route_sys_uc} to: {curr_nav_route_sys_uc}."
                        )
                        # Check if this nav route is correct
                        if curr_nav_route_sys_uc == target_name_uc:
                            logger.debug(f"Nav Route correctly updated to {target_name_uc}.")
                            # Break loop and exit
                            correct_route = True
                            break
                        else:
                            # Try the next system, go back to the search bar
                            logger.debug(
                                f"Nav Route updated with wrong target: {curr_nav_route_sys_uc}. Retrying selection."
                            )
                            break
                    sleep(NAV_ROUTE_UPDATE_POLL_INTERVAL)
            else:
                logger.warning("Unable to verify Galaxy Map route: NavRoute data is unavailable.")
                self._save_search_debug("route-unavailable", target_name_uc, attempt)
                break

            if correct_route:
                break

            self._save_search_debug("route-unconfirmed", target_name_uc, attempt)
            if not route_changed:
                logger.debug(
                    f"Nav Route did not update within {NAV_ROUTE_UPDATE_TIMEOUT:g} seconds "
                    f"(attempt {attempt}/{MAX_NAV_ROUTE_PLOT_ATTEMPTS})."
                )

        # Close Galaxy map
        if self.status_parser.get_gui_focus() == GuiFocusGalaxyMap:
            ap.keys.send("GalaxyMapOpen")
        sleep(0.5)
        if not correct_route:
            logger.warning(
                f"Unable to confirm Galaxy Map route to {target_name} after {MAX_NAV_ROUTE_PLOT_ATTEMPTS} attempts."
            )
        return correct_route

    def set_next_system(self, ap, target_system) -> bool:
        """Sets the next system to jump to, or the final system to jump to.
        If the system is already selected or is selected correctly, returns True,
        otherwise False.
        """
        # Call sequence to select route
        if self.set_gal_map_destination_text(ap, target_system, None):
            return True
        else:
            # Error setting target
            logger.warning("Error setting waypoint, breaking")
            return False

    def goto_galaxy_map(self):
        """Open Galaxy Map if we are not there. Waits for map to load. Selects the search bar."""
        if self.status_parser.get_gui_focus() != GuiFocusGalaxyMap:
            logger.debug("Opening Galaxy Map")
            # Goto cockpit view
            self.ap.ship_control.goto_cockpit_view()
            # Goto Galaxy Map
            self.keys.send("GalaxyMapOpen")

            # Scale the regions based on the target resolution.
            scl_reg = reg_scale_for_station(
                self.reg["cartographics"], self.screen.screen_width, self.screen.screen_height
            )

            # Wait for screen to appear. The text is the same, regardless of language.
            self.ocr.wait_for_text(self.ap, ["CARTOGRAPHICS"], scl_reg)

            self.keys.send("UI_Up")  # Go up to search bar
        else:
            logger.debug("Galaxy Map is already open")
            self.keys.send("UI_Left", repeat=2)
            self.keys.send("UI_Up", hold=2)  # Go up to search bar. Allows 1 left to bookmarks.
