"""Finding programs the way Windows itself finds them.

PATH is not where Windows looks. The shell also consults the App Paths registry
key, which is how `start chrome` works on a machine with no chrome.exe anywhere
on PATH - and neither Chrome nor Edge puts itself on PATH by default.

Getting this wrong is quiet in both directions: selftest warned that a working
application was missing, and the wall opened as an ordinary browser tab because
the chromeless window needed a browser it could not find.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

#: Where the browsers actually install, for the case where even the registry
#: has nothing - a portable copy, or a machine mid-update.
KNOWN_LOCATIONS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "msedge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
}


def app_paths_entry(name: str) -> str | None:
    """The executable's full path from the App Paths registry key, or None."""
    if sys.platform != "win32":
        return None
    import winreg

    if not name.lower().endswith(".exe"):
        name += ".exe"
    key = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" "\\" + name)
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "")
                if value and Path(value).exists():
                    return value
        except OSError:
            continue
    return None


def find_executable(name: str) -> str | None:
    """PATH first, then the registry, then where it is actually installed."""
    found = shutil.which(name)
    if found:
        return found
    found = app_paths_entry(name)
    if found:
        return found
    if sys.platform != "win32":
        return None
    for candidate in KNOWN_LOCATIONS.get(name.lower().removesuffix(".exe"), []):
        if Path(candidate).exists():
            return candidate
    return None


def is_installed(name: str) -> bool:
    return find_executable(name) is not None
