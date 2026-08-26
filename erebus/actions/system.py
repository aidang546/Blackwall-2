"""Native Windows system control.

Everything here degrades to a no-op with an explanatory message off Windows, so
the project stays runnable and testable on any machine even though it targets
Windows first.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys

log = logging.getLogger("erebus.system")

IS_WINDOWS = sys.platform == "win32"

# Virtual key codes for the media keys the keyboard driver already understands.
VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002


def _press(vk: int) -> None:
    if not IS_WINDOWS:
        return
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _volume_interface():
    """Get the master audio endpoint via pycaw, or None.

    COM has to be initialised on whichever thread touches it, and these
    handlers run inside a thread-pool executor rather than on the main thread -
    so without this the very first "volume up" fails with "CoInitialize has not
    been called". Calling it again on an already-initialised thread is
    harmless; it returns S_FALSE rather than erroring.
    """
    if not IS_WINDOWS:
        return None
    try:
        import comtypes
        from comtypes import CLSCTX_ALL
        from ctypes import POINTER, cast
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        try:
            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001 - already initialised on this thread
            pass

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "audio endpoint unavailable (%s: %s) - volume control disabled",
            type(exc).__name__, exc,
        )
        return None


def get_volume() -> int | None:
    volume = _volume_interface()
    if volume is None:
        return None
    return int(round(volume.GetMasterVolumeLevelScalar() * 100))


def set_volume(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    volume = _volume_interface()
    if volume is None:
        return "Audio interface unavailable."
    volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
    return f"Volume {percent}."


def nudge_volume(delta: int) -> str:
    current = get_volume()
    if current is None:
        return "Audio interface unavailable."
    return set_volume(current + delta)


def set_mute(muted: bool) -> str:
    volume = _volume_interface()
    if volume is None:
        return "Audio interface unavailable."
    volume.SetMute(1 if muted else 0, None)
    return "Muted." if muted else "Unmuted."


def play_pause() -> str:
    _press(VK_MEDIA_PLAY_PAUSE)
    return ""


def next_track() -> str:
    _press(VK_MEDIA_NEXT)
    return ""


def prev_track() -> str:
    _press(VK_MEDIA_PREV)
    return ""


def lock() -> str:
    if IS_WINDOWS:
        ctypes.windll.user32.LockWorkStation()
    return "Locked."


def sleep() -> str:
    if IS_WINDOWS:
        subprocess.run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False
        )
    return "Sleeping."


def shutdown(delay: int = 10) -> str:
    if IS_WINDOWS:
        subprocess.run(["shutdown", "/s", "/t", str(delay)], check=False)
    return f"Shutting down in {delay} seconds. Say cancel to abort."


def restart(delay: int = 10) -> str:
    if IS_WINDOWS:
        subprocess.run(["shutdown", "/r", "/t", str(delay)], check=False)
    return f"Restarting in {delay} seconds. Say cancel to abort."


def abort_shutdown() -> str:
    if IS_WINDOWS:
        subprocess.run(["shutdown", "/a"], check=False)
    return "Aborted."


#: Everything the registry is allowed to call by name.
HANDLERS = {
    "volume_up": lambda value=None: nudge_volume(10),
    "volume_down": lambda value=None: nudge_volume(-10),
    "volume_set": lambda value=None: set_volume(int(value or 50)),
    "mute": lambda value=None: set_mute(True),
    "unmute": lambda value=None: set_mute(False),
    "play_pause": lambda value=None: play_pause(),
    "next_track": lambda value=None: next_track(),
    "prev_track": lambda value=None: prev_track(),
    "lock": lambda value=None: lock(),
    "sleep": lambda value=None: sleep(),
    "shutdown": lambda value=None: shutdown(),
    "restart": lambda value=None: restart(),
    "abort_shutdown": lambda value=None: abort_shutdown(),
}
