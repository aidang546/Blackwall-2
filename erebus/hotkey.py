"""A system-wide key that reaches Erebus from inside anything else.

The wall already has push-to-talk on the space bar, but only while its window
has focus - which is exactly when you do not need it. The point of a desktop
assistant is to reach it from inside a game, a timeline, a terminal. That needs
a hotkey the operating system routes before the foreground app sees it.

Windows offers this through RegisterHotKey, which is in user32 and therefore
reachable with ctypes: no extra dependency for a feature this central. The
catch is that RegisterHotKey reports presses, not holds, so this is tap-to-talk
rather than push-to-talk - tap it, speak, and the silence gate ends the turn on
its own. For a hotkey you press while your hands are on a keyboard doing
something else, that is the better shape anyway.

Off Windows it degrades to an inert object, so the rest of the program does not
have to care which platform it is on.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger("erebus.hotkey")

IS_WINDOWS = sys.platform == "win32"

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
#: Without this, holding the key auto-repeats and fires a turn per repeat.
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}

#: Virtual key codes for the keys worth binding. Letters and digits are their
#: ASCII values, so they do not need listing.
KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "scrolllock": 0x91, "numlock": 0x90,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}


class HotkeyError(ValueError):
    """A combo string that cannot be turned into a real key binding."""


def parse(combo: str) -> tuple[int, int]:
    """"ctrl+alt+space" -> (modifier mask, virtual key code).

    Raises rather than guessing: a typo here is silent otherwise, and a hotkey
    that quietly does not exist is worse than one that refuses to start.
    """
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise HotkeyError("empty hotkey")

    mods = 0
    for part in parts[:-1]:
        if part not in MODIFIERS:
            raise HotkeyError(f"{part!r} is not a modifier")
        mods |= MODIFIERS[part]

    key = parts[-1]
    if key in KEYS:
        code = KEYS[key]
    elif len(key) == 1 and key.isalnum():
        code = ord(key.upper())
    else:
        raise HotkeyError(f"{key!r} is not a key this can bind")

    if not mods:
        # A bare key would be swallowed system-wide - you could not type it.
        raise HotkeyError(f"{combo!r} needs a modifier (ctrl, alt, shift or win)")
    return mods | MOD_NOREPEAT, code


class Hotkeys:
    """Registers combos and calls back on the asyncio loop when they fire."""

    def __init__(self, loop) -> None:
        self._loop = loop
        self._bindings: list[tuple[str, int, int, object]] = []
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self.active: list[str] = []

    def bind(self, combo: str, callback) -> None:
        """Queue a binding. Nothing is registered until start()."""
        if not combo:
            return
        mods, key = parse(combo)          # raises on a typo, before we start
        self._bindings.append((combo, mods, key, callback))

    def start(self) -> bool:
        """Register everything on a thread of its own. False if unavailable."""
        if not self._bindings:
            return False
        if not IS_WINDOWS:
            log.info("global hotkeys are Windows-only - skipping (%s)",
                     ", ".join(c for c, _, _, _ in self._bindings))
            return False
        self._thread = threading.Thread(target=self._run, name="hotkeys",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return bool(self.active)

    def stop(self) -> None:
        if self._thread_id is not None:
            # Nudges GetMessageW out of its block so the thread can unwind.
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread_id = None

    # -- the message loop, on its own thread --------------------------------

    def _run(self) -> None:
        # wintypes raises on import off Windows, so it cannot live at the top
        # of the file even though nothing outside this thread touches it.
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        # RegisterHotKey binds to the calling thread's message queue, so this
        # has to happen here rather than wherever start() was called from.
        registered: dict[int, object] = {}
        for index, (combo, mods, key, callback) in enumerate(self._bindings, start=1):
            if user32.RegisterHotKey(None, index, mods, key):
                registered[index] = callback
                self.active.append(combo)
            else:
                # Almost always means another program already owns the combo.
                log.warning("could not register %s - another program has it", combo)
        self._ready.set()
        if not registered:
            return

        message = ctypes.wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    callback = registered.get(message.wParam)
                    if callback is not None:
                        self._fire(callback)
        except Exception as exc:  # noqa: BLE001
            log.warning("hotkey loop stopped (%s: %s)", type(exc).__name__, exc)
        finally:
            for index in registered:
                user32.UnregisterHotKey(None, index)

    def _fire(self, callback) -> None:
        """Hand the callback to the event loop; never run it on this thread."""
        try:
            self._loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass          # loop already closed, we are shutting down
