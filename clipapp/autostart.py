"""Opening with Windows: a value under the current user's Run key, which only this account
sees and which needs no administrator rights. It starts the app in the tray, window closed."""

from __future__ import annotations

import os
import sys
import winreg
from pathlib import Path

from . import APP_NAME

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TRAY_FLAG = "--tray"


def _key_path() -> str:
    return os.environ.get("CLIPAPP_RUN_KEY", RUN_KEY)  # the tests point this at a key of their own


def frozen() -> bool:
    """True when running as the built PasteStack.exe rather than from the source."""
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def command() -> str:
    if frozen():
        return f'"{sys.executable}" {TRAY_FLAG}'
    pythonw = Path(sys.executable).with_name("pythonw.exe")  # no console window at sign-in
    launcher = Path(__file__).resolve().parent.parent / f"{APP_NAME}.pyw"
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{launcher}" {TRAY_FLAG}'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _key_path()) as key:
            return bool(winreg.QueryValueEx(key, APP_NAME)[0])
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    """Raises OSError if Windows refuses the change."""
    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _key_path()) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command())
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _key_path(), 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass


def refresh() -> None:
    """Points an existing entry at where the app is now, in case the folder moved."""
    if is_enabled():
        try:
            set_enabled(True)
        except OSError:
            pass
