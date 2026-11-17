"""Exclude application startup and program-file updates from one another on Windows."""

import ctypes
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path


@contextmanager
def installation_lock(root: Path):
    update_dir = root / ".update"
    update_dir.mkdir(exist_ok=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(str(update_dir / "installation.lock"), 0xC0000000, 0, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in (32, 33):
            raise RuntimeError("This EDFC installation is already running or being updated.") from None
        raise ctypes.WinError(error)
    try:
        if (update_dir / "pending.json").exists():
            raise RuntimeError("An interrupted update needs recovery. Run update_edfc.bat before starting EDFC.")
        yield
    finally:
        close_handle(handle)
