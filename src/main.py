"""Start script of the application."""

import ctypes
import os
import sys
from pathlib import Path

from core.install_lock import installation_lock

if __name__ == "__main__":
    try:
        with installation_lock(Path(__file__).resolve().parent.parent):
            from gui.main_window import main

            restart_requested = main()
    except (OSError, RuntimeError) as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), "ED Flight Control could not start", 0x10)
        raise
    if restart_requested:
        os.execv(sys.executable, [sys.executable, *sys.argv])
