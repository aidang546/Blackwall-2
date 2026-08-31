"""The system-wide key, as far as it can be tested off Windows.

The registration itself needs user32, but the part that actually goes wrong is
parsing: a combo with a typo in it silently never fires, and "it does nothing"
is indistinguishable from "it is broken". So every combo is validated before
anything is registered, and that validation is tested here.

    python tests/test_hotkey.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus import hotkey as H   # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {label:<46} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label:<46} {detail}")


print("\nPARSING")
for combo, mods, key in [
    ("ctrl+alt+space", H.MOD_CONTROL | H.MOD_ALT, 0x20),
    ("ctrl+shift+e", H.MOD_CONTROL | H.MOD_SHIFT, ord("E")),
    ("alt+f4", H.MOD_ALT, 0x73),
    ("win+d", H.MOD_WIN, ord("D")),
    ("CTRL+ALT+X", H.MOD_CONTROL | H.MOD_ALT, ord("X")),
    ("ctrl + alt + 1", H.MOD_CONTROL | H.MOD_ALT, ord("1")),
]:
    got_mods, got_key = H.parse(combo)
    check(f"{combo!r}", got_mods == mods | H.MOD_NOREPEAT and got_key == key,
          f"{got_mods:#x}, {got_key:#x}")

check("auto-repeat is always suppressed",
      H.parse("ctrl+alt+space")[0] & H.MOD_NOREPEAT != 0,
      "otherwise holding the key fires a turn per repeat")

print("\nREJECTION")
for bad, why in [
    ("space", "a bare key would be swallowed system-wide"),
    ("", "empty"),
    ("ctrl+nope", "not a key"),
    ("frobnicate+space", "not a modifier"),
    ("ctrl+alt+f99", "no such function key"),
]:
    try:
        H.parse(bad)
        check(f"{bad!r} rejected", False, "it was accepted")
    except H.HotkeyError as exc:
        check(f"{bad!r} rejected", True, f"{why}: {str(exc)[:38]}")

print("\nBINDING")
loop = asyncio.new_event_loop()
keys = H.Hotkeys(loop)
check("binding a good combo is quiet", keys.bind("ctrl+alt+space", lambda: None) is None)
try:
    keys.bind("space", lambda: None)
    check("a bad combo raises at bind time, not at press time", False)
except H.HotkeyError:
    check("a bad combo raises at bind time, not at press time", True)

check("an empty combo is ignored, not an error",
      keys.bind("", lambda: None) is None)

started = keys.start()
if H.IS_WINDOWS:
    check("starts on Windows", started)
else:
    check("off Windows it declines rather than pretending", started is False)
    check("and claims no active bindings", keys.active == [])
keys.stop()

empty = H.Hotkeys(loop)
check("nothing bound means nothing started", empty.start() is False)
empty.stop()
loop.close()

print("\nFIRING")
# _fire must never run the callback on the hotkey thread: everything it touches
# belongs to the event loop.
loop = asyncio.new_event_loop()
fired = []
keys = H.Hotkeys(loop)
keys._fire(lambda: fired.append(1))
check("the callback is deferred, not run inline", fired == [])
loop.call_soon(loop.stop)
loop.run_forever()
check("and runs once the loop turns", fired == [1])
loop.close()

closed = asyncio.new_event_loop()
closed.close()
keys = H.Hotkeys(closed)
try:
    keys._fire(lambda: None)
    check("firing into a closed loop is survivable", True, "shutdown race")
except Exception as exc:  # noqa: BLE001
    check("firing into a closed loop is survivable", False, repr(exc))

print(f"\n  {PASSED}/{PASSED + FAILED} passed")
raise SystemExit(1 if FAILED else 0)
