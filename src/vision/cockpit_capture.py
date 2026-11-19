"""Local, OCR-independent capture of cockpit views for fixture review."""

import json
import re
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep

import cv2
import numpy as np
import win32gui
from mss.exception import ScreenShotError

from core.constants import (
    FlagsInMainShip,
    GuiFocusExternalPanel,
    GuiFocusInternalPanel,
    GuiFocusNoFocus,
    GuiFocusRolePanel,
)
from vision.screen import elite_dangerous_window

START_FOCUS_TIMEOUT = 5.0

PANELS = (
    ("left", "UI_Left", GuiFocusExternalPanel, 5),
    ("right", "UI_Right", GuiFocusInternalPanel, 8),
    ("bottom", "UI_Down", GuiFocusRolePanel, 4),
)


class CaptureStoppedError(RuntimeError):
    """Capture could not continue without sending input to an uncertain UI."""


def _token(value: str | None, fallback: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-") or fallback


def _game_is_foreground():
    return win32gui.GetWindowText(win32gui.GetForegroundWindow()) == elite_dangerous_window


def _left_tab_bar(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    strip = image[round(0.21 * height) : round(0.30 * height), round(0.10 * width) : round(0.70 * width)]
    return cv2.resize(strip, (240, 36), interpolation=cv2.INTER_AREA)


def capture_cockpit(ap, root: Path, metadata: dict) -> tuple[Path, dict]:
    """Capture the current game window without relying on recognized tabs."""
    state = ap.jn.ship_state()
    journal_language = state.get("language")
    language = ap.locale.journal_language_code(journal_language)
    ship = _token(state.get("type"), "ship-unknown")
    language_token = _token(language or journal_language, "lang-unknown")
    folder = root / f"{datetime.now():%Y%m%d-%H%M%S-%f}_{ship}_{language_token}"
    folder.mkdir(parents=True)
    manifest = {
        **metadata,
        "ship_type": state.get("type"),
        "game_language": language or journal_language,
        "resolution": None,
        "review_required": True,
        "frames": [],
        "skipped": [],
        "duplicates": [],
        "stopped": None,
    }

    def check_running():
        if ap.terminate or ap.assist_is_enabled():
            raise CaptureStoppedError("An assist was requested or EDFC is closing.")

    def check():
        check_running()
        if not _game_is_foreground():
            raise CaptureStoppedError("Elite Dangerous is not the foreground window.")
        status = ap.status.get_cleaned_data(timeout=0.5)
        if not status or not (status.get("Flags", 0) & FlagsInMainShip):
            raise CaptureStoppedError("The game has not confirmed that you are in your ship cockpit.")
        return status.get("GuiFocus")

    def send(binding, focus, **options):
        check_running()
        if not _game_is_foreground():
            ap.keys.activate_elite_for_input()
        if check() != focus:
            raise CaptureStoppedError(f"Unexpected game UI focus before {binding}.")
        ap.keys.send(binding, **options)

    def wait_for(focus, previous):
        deadline = monotonic() + 2.0
        while monotonic() < deadline:
            current = check()
            if current == focus:
                return True
            if current != previous:
                raise CaptureStoppedError("An unexpected game screen opened.")
            sleep(0.1)
        return False

    def take(view, step, focus, first_left_tab: np.ndarray | None = None) -> np.ndarray | None:
        sleep(0.45)
        if check() != focus:
            raise CaptureStoppedError(f"The {view} view lost focus before capture.")
        image = ap.scr.get_screen_full()
        if check() != focus:
            raise CaptureStoppedError(f"The {view} view lost focus during capture.")
        if image is None or not image.size:
            raise CaptureStoppedError("The game screen could not be captured.")
        height, width = image.shape[:2]
        resolution = f"{width}x{height}"
        if manifest["resolution"] not in (None, resolution):
            raise CaptureStoppedError("The capture resolution changed during the set.")
        manifest["resolution"] = resolution
        if first_left_tab is not None:
            # Compare only the tab bar; HUD animations elsewhere can make the full frame differ.
            difference = cv2.absdiff(_left_tab_bar(image), first_left_tab).max(axis=2)
            if (difference > 24).mean() < 0.02:
                manifest["duplicates"].append({"view": view, "step": step, "same_as_step": 1})
                return None
        index = len(manifest["frames"])
        name = f"RENAME-BEFORE-FIXTURE__{index:02d}_{view}_step-{step:02d}_{ship}_{resolution}_{language_token}.png"
        ok, png = cv2.imencode(".png", image)
        if not ok:
            raise OSError(f"Could not encode {name}.")
        (folder / name).write_bytes(png.tobytes())
        manifest["frames"].append({"file": name, "view": view, "step": step, "gui_focus": focus})
        return image

    try:
        check_running()
        ap.keys.activate_elite_for_input()
        deadline = monotonic() + START_FOCUS_TIMEOUT
        while not _game_is_foreground():
            check_running()
            if monotonic() >= deadline:
                raise CaptureStoppedError(
                    "Elite Dangerous is not the foreground window. Switch to the game after starting the capture."
                )
            sleep(0.1)
        if check() != GuiFocusNoFocus:
            raise CaptureStoppedError("Start the capture from the unobstructed cockpit view.")
        send("HeadLookReset", GuiFocusNoFocus)
        take("forward", 0, GuiFocusNoFocus)
        for view, direction, focus, steps in PANELS:
            send("UIFocus", GuiFocusNoFocus, state=1)
            try:
                send(direction, GuiFocusNoFocus)
            finally:
                # Always release a held modifier, including on interruption.
                ap.keys.send("UIFocus", state=0)
            if not wait_for(focus, GuiFocusNoFocus):
                manifest["skipped"].append({"view": view, "reason": "Panel focus was not confirmed."})
                continue
            if focus == GuiFocusRolePanel:
                # The role panel remembers its selection and does not reliably wrap to the top.
                send("UI_Up", focus, hold=1.5)
            first_left_tab = None
            for step in range(1, steps + 1):
                if step > 1:
                    send("CycleNextPanel", focus)
                image = take(view, step, focus, first_left_tab if step == steps else None)
                if focus == GuiFocusExternalPanel and step == 1 and image is not None:
                    first_left_tab = _left_tab_bar(image)
            send("UI_Back", focus)
            if not wait_for(GuiFocusNoFocus, focus):
                raise CaptureStoppedError(f"Could not close the {view} panel.")
        send("HeadLookReset", GuiFocusNoFocus)
    except (CaptureStoppedError, OSError, ScreenShotError, cv2.error) as error:
        manifest["stopped"] = str(error)
    finally:
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return folder, manifest
