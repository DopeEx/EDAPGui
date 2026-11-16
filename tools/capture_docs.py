"""Generate documentation screenshots with real widgets and isolated example data (Windows only)."""

from __future__ import annotations

import argparse
import ast
import copy
import ctypes
import logging
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
MARGIN = 12
BACKGROUND = "#171b24"
SCENES = (
    "main",
    "waypoints",
    "waypoint_edit",
    "waypoint_buy",
    "waypoint_import",
    "overlay",
    "settings",
    "info",
    "collapsed",
)
TRADE_TEXT = """From Ohm City | LHS 20
To Rota Obscura | WISE 0359-5401
Station distance
2 Ls
Buy
Polymers
Station distance
3 Ls
Sell
Polymers
"""


def read_defaults(source: Path, overlay_order: tuple[str, ...]) -> dict:
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and target.attr == "config"
            for target in node.targets
        ):
            continue
        return {
            ast.literal_eval(key): (
                list(overlay_order) if ast.literal_eval(key) == "OverlayLineOrder" else ast.literal_eval(value)
            )
            for key, value in zip(node.value.keys, node.value.values)
        }
    raise ValueError(f"Autopilot configuration defaults not found in {source}")


def window_bounds(hwnd: int) -> tuple[int, int, int, int]:
    get_bounds = ctypes.WinDLL("dwmapi").DwmGetWindowAttribute
    get_bounds.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.POINTER(wintypes.RECT), wintypes.DWORD]
    get_bounds.restype = ctypes.c_long
    rect = wintypes.RECT()
    # Extended frame bounds exclude invisible resize borders, but retain the visible native frame.
    result = get_bounds(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect))
    if result != 0:
        raise RuntimeError(f"Could not read the visible window frame (HRESULT {result:#x}).")
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise RuntimeError("Windows returned an empty window frame.")
    return rect.left, rect.top, rect.right, rect.bottom


def check_capture_area(hwnd: int, backdrop_hwnd: int, area: tuple[int, int, int, int]) -> None:
    import win32gui

    left, top, right, bottom = area
    windows = []
    win32gui.EnumWindows(lambda handle, _: windows.append(handle), None)
    found_window = False
    for handle in windows:
        if handle == backdrop_hwnd:
            if found_window and win32gui.IsWindowVisible(handle):
                bg_left, bg_top, bg_right, bg_bottom = win32gui.GetWindowRect(handle)
                if bg_left <= left and bg_top <= top and bg_right >= right and bg_bottom >= bottom:
                    return
            break
        if handle == hwnd:
            if win32gui.IsIconic(handle) or not win32gui.IsWindowVisible(handle):
                break
            found_window = True
        elif win32gui.IsWindowVisible(handle):
            other_left, other_top, other_right, other_bottom = win32gui.GetWindowRect(handle)
            if other_left < right and other_top < bottom and other_right > left and other_bottom > top:
                raise RuntimeError(
                    "Another window overlaps the capture area. Keep the documentation preview unobscured."
                )
    raise RuntimeError("The documentation window and its neutral backdrop must both be visible.")


def capture_window(hwnd: int, backdrop_hwnd: int) -> Image.Image:
    import mss

    bounds = window_bounds(hwnd)
    left, top, right, bottom = bounds
    area = (left - MARGIN, top - MARGIN, right + MARGIN, bottom + MARGIN)
    check_capture_area(hwnd, backdrop_hwnd, area)
    expected_size = (right - left + 2 * MARGIN, bottom - top + 2 * MARGIN)
    with mss.mss() as screen:
        if not any(
            monitor["left"] <= area[0]
            and monitor["top"] <= area[1]
            and monitor["left"] + monitor["width"] >= area[2]
            and monitor["top"] + monitor["height"] >= area[3]
            for monitor in screen.monitors[1:]
        ):
            raise RuntimeError("The complete documentation window and its margin must fit on one monitor.")
        shot = screen.grab({"left": area[0], "top": area[1], "width": expected_size[0], "height": expected_size[1]})
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if window_bounds(hwnd) != bounds:
        raise RuntimeError("The documentation window moved during capture. Keep it stationary.")
    check_capture_area(hwnd, backdrop_hwnd, area)
    if image.size != expected_size:
        raise RuntimeError(f"Incomplete window capture: expected {expected_size}, received {image.size}")
    if all(high - low < 3 for low, high in image.convert("RGB").getextrema()):
        raise RuntimeError("Window capture returned a blank image; existing documentation images were not replaced.")
    return image


@contextmanager
def preserve_exception_hooks():
    with ExitStack() as hooks:
        for owner, name in ((sys, "excepthook"), (sys, "unraisablehook"), (threading, "excepthook")):
            hooks.enter_context(patch.object(owner, name, getattr(owner, name)))
        yield


def render_overlay() -> Image.Image:
    from gui.overlay import _render_image

    values = (
        ("AP MODE: ", "Travel Assist", "active"),
        ("AP STATUS: ", "Refueling", "active"),
        ("SHIP STATUS: ", "Supercruise", "value"),
        ("FUEL: ", "64 % (scooping)", "value"),
        ("CURRENT SYSTEM: ", "Eol Prou RS-T d3-94 | K", "value"),
        ("DESTINATION: ", "Colonia", "value"),
        ("NEXT STAR: ", "F (scoopable)", "value"),
        ("JUMPS: ", "12 of 40 (28 left)", "value"),
        ("ETA: ", "43 min", "value"),
        ("DISTANCE: ", "3,412.8 of 7,320.6 ly (3,907.8 left)", "value"),
    )
    text = {}
    for index, (label, value, role) in enumerate(values):
        row = index if index < 8 else index + 1
        text[str(index)] = (((label, "label"), (value, role)), row, 0)
    panel, _position = _render_image(
        1600,
        1000,
        {},
        text,
        {},
        "Rajdhani",
        14,
        120,
        (0, 0),
        panel_opacity=50,
        bars={"jumps_bar": (0.3, 8), "distance_bar": (3412.8 / 7320.6, 11)},
    )
    margin = 30
    background = Image.new("RGBA", (panel.width + 2 * margin, panel.height + 2 * margin))
    draw = ImageDraw.Draw(background)
    for y in range(background.height):
        for x in range(background.width):
            draw.point(
                (x, y),
                fill=(
                    round(30 * x / background.width),
                    round(20 * y / background.height),
                    round(48 * x / background.width + 20 * y / background.height),
                    255,
                ),
            )
    stars = random.Random(42)
    for _ in range(65):
        brightness = stars.randrange(90, 200)
        draw.point(
            (stars.randrange(background.width), stars.randrange(background.height)),
            fill=(brightness, brightness, brightness, 255),
        )
    background.alpha_composite(panel, (margin, margin))
    return background.convert("RGB")


def run(output: Path, preview: bool) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    with tempfile.TemporaryDirectory(prefix="edfc-docs-") as directory, ExitStack() as stack:
        stack.enter_context(preserve_exception_hooks())
        stack.callback(logging.shutdown)
        temporary = Path(directory)
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "EDFC_LOG_DIR": str(temporary / "logs"),
                    "EDFC_DEBUG_DIR": str(temporary / "debug"),
                },
            )
        )
        from core import paths

        folders = {
            "USER_DATA": temporary,
            "CONFIG_DIR": temporary / "configs",
            "WAYPOINTS_DIR": temporary / "waypoints",
            "WAYPOINT_PROGRESS_DIR": temporary / "waypoints" / "progress",
            "INCOMING_DEBUG_DIR": temporary / "incoming-debug",
            "PADDLEX_CACHE_DIR": paths.PADDLEX_CACHE_DIR,
        }
        files = {
            "AP_CONFIG": folders["CONFIG_DIR"] / "AP.json",
            "SHIP_CONFIGS": folders["CONFIG_DIR"] / "ship_configs.json",
            "RESOLUTION_CONFIG": folders["CONFIG_DIR"] / "resolution.json",
            "GUI_STATE": folders["CONFIG_DIR"] / "gui.json",
            "LAST_WAYPOINT_ROUTE": folders["CONFIG_DIR"] / "last_waypoint_route.json",
            "STATISTICS_DB": temporary / "stats" / "statistics.sqlite3",
        }
        for name, value in (folders | files).items():
            stack.enter_context(patch.object(paths, name, value))
        paths.ensure_user_data()

        import tkinter as tk

        import ttkbootstrap as ttk

        from core.commodities import get_localization
        from gui import main_window
        from gui.cv_view import CvFrames
        from gui.fields import DEFAULT_OVERLAY_LINE_ORDER
        from gui.waypoint_model import RouteDocument, new_waypoint

        stack.enter_context(
            patch.object(
                main_window.window_state,
                "load",
                return_value={
                    "x": None,
                    "y": None,
                    "collapsed": False,
                    "topmost": False,
                },
            )
        )
        stack.enter_context(patch.object(main_window.window_state, "save"))
        stack.enter_context(patch.object(main_window, "MousePoint"))
        stack.enter_context(
            patch.object(
                main_window,
                "datetime",
                SimpleNamespace(now=lambda: datetime(2026, 1, 1, 18, 0, 0)),
            )
        )
        ap = Mock()
        ap.config = read_defaults(ROOT / "src" / "core" / "autopilot.py", DEFAULT_OVERLAY_LINE_ORDER)
        ap.locale = get_localization("en")
        ap.cv_frames = CvFrames()
        ap.rollrate, ap.pitchrate, ap.yawrate, ap.sunpitchuptime = 80.0, 33.0, 8.0, 0.0
        ap.waypoint_assist_enabled = False
        ap.waypoint = SimpleNamespace(route_loaded=False, filename="", waypoints={}, executing=False)
        totals = SimpleNamespace(
            jumps=124, distance=4975.6, assist_seconds=11400, travel_seconds=10200, average=92, samples=118
        )
        ap.statistics = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                available=True,
                identity=SimpleNamespace(ship_id=7),
                commander=totals,
                ship=totals,
            )
        )
        main_window.enable_dpi_awareness()
        icon = str(paths.ASSETS_DIR / "icons" / "edfc.ico")
        backdrop = ttk.Window(themename="darkly", iconphoto=str(paths.ASSETS_DIR / "icons" / "edfc.png"))
        backdrop.withdraw()
        backdrop.overrideredirect(True)
        backdrop.configure(bg=BACKGROUND)
        root = ttk.Toplevel(master=backdrop)
        root.withdraw()
        root.iconbitmap(default=icon)
        root.iconbitmap(icon)
        closed = False

        def close():
            nonlocal closed
            if not closed:
                closed = True
                backdrop.destroy()

        errors = []

        def report_error(kind, error, traceback):
            errors.append(error)
            logging.error("Documentation preview callback failed", exc_info=(kind, error, traceback))
            close()

        root.report_callback_exception = report_error
        backdrop.report_callback_exception = report_error
        try:
            app = main_window.APGui(root, ed_ap=ap, services=False)
            app.set_mode = Mock()
            app.stop_all_assists = Mock()
            app.close_window = close
            root.protocol("WM_DELETE_WINDOW", close)
            root.geometry("+80+80")
            document = RouteDocument()
            document.data["Repeat"] = True
            document.data["waypoints"] = [
                dict(
                    new_waypoint(),
                    SystemName="LHS 20",
                    StationName="Ohm City",
                    BuyCommodities={"128049197": 12},
                    SellCommodities={"128049154": 8},
                ),
                dict(
                    new_waypoint(),
                    SystemName="Arima",
                    StationName="Kent's Legacy",
                    BuyCommodities={"128049154": 8},
                    SellCommodities={"128049197": 12},
                ),
                dict(new_waypoint(), SystemName="Leuctra", StationName="Regiomontanus Enterprise", Enabled=False),
            ]
            document.save(paths.WAYPOINTS_DIR / "example_trade_route.json")
            editor = app.waypoint_editor
            editor.document = document
            editor.refresh()
            editor.refresh_routes()
            editor.tree.selection_set(document.data["waypoints"][0]["Id"])

            def generate():
                root.configure(menu="")
                staging = temporary / "images"
                staging.mkdir(exist_ok=True)
                try:
                    for scene in SCENES:
                        if app.collapsed:
                            app.toggle_collapse()
                        if editor.form is not None:
                            editor.cancel_form()
                        tab = {
                            "main": 0,
                            "waypoints": 1,
                            "waypoint_edit": 1,
                            "waypoint_buy": 1,
                            "waypoint_import": 1,
                            "overlay": 2,
                            "settings": 3,
                            "info": len(app.tabs.tabs()) - 1,
                            "collapsed": 0,
                        }[scene]
                        app.tabs.select(tab)
                        if scene in ("waypoint_edit", "waypoint_buy"):
                            editor.begin_form(copy.deepcopy(document.data["waypoints"][0]), 0, False)
                            if scene == "waypoint_buy":
                                editor.form.notebook.select(1)
                        elif scene == "waypoint_import":
                            editor.import_route()
                            editor.form.text.insert("1.0", TRADE_TEXT)
                            editor.form.preview()
                        elif scene == "collapsed":
                            app.toggle_collapse()
                        root.update()
                        hwnd = int(root.wm_frame(), 0)
                        left, top, right, bottom = window_bounds(hwnd)
                        backdrop.geometry(
                            f"{right - left + 2 * MARGIN}x{bottom - top + 2 * MARGIN}"
                            f"{left - MARGIN:+d}{top - MARGIN:+d}"
                        )
                        backdrop.deiconify()
                        root.lift(backdrop)
                        root.update()
                        time.sleep(0.25)
                        root.update()
                        capture_window(hwnd, int(backdrop.wm_frame(), 0)).save(staging / f"screen_cap_{scene}.png")
                    render_overlay().save(staging / "screen_cap_status_overlay.png")
                    if errors:
                        raise RuntimeError("Documentation rendering failed") from errors[0]
                    output.mkdir(parents=True, exist_ok=True)
                    for image in sorted(staging.glob("*.png")):
                        shutil.copyfile(image, output / image.name)
                        print(f"Saved {output / image.name}", flush=True)
                finally:
                    close()

            menu = tk.Menu(root)
            menu.add_command(label="Capture documentation images", command=generate)
            menu.add_command(label="Close preview", command=close)
            with main_window.window_icons(root, icon):
                root.deiconify()
                if preview:
                    root.configure(menu=menu)
                    root.mainloop()
                    if errors:
                        raise RuntimeError("Documentation preview failed") from errors[0]
                else:
                    generate()
        finally:
            close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument(
        "--preview", action="store_true", help="Open the isolated GUI; capture through its tool-only menu."
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Documentation window captures require Windows.")
    run(args.output_dir.resolve(), args.preview)


if __name__ == "__main__":
    main()
