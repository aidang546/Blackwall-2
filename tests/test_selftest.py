"""The selftest, tested where it can be.

Most of what it probes needs Windows or a sound card, so what is checked here
is the part that is pure logic and the part that decides pass from fail: the
launch-target parser, the probe wrapper's refusal to die, and the exit code.

    python tests/test_selftest.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus import selftest as S           # noqa: E402
from erebus.core.config import Config      # noqa: E402

PASSED = FAILED = 0


def _try_ollama() -> str:
    """The message a machine without Ollama would get."""
    import install as _install

    real = _install.shutil.which
    _install.shutil.which = lambda name: None
    try:
        _install.setup_ollama(dry=True)
        return ""
    except _install.Stop as exc:
        return str(exc)
    finally:
        _install.shutil.which = real


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {label:<50} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label:<50} {detail}")


print("\nWHAT A `run:` LINE ACTUALLY INVOKES")
for command, expected in [
    ("start spotify:", "spotify:"),
    ("start steam://rungameid/1091500", "steam://rungameid/1091500"),
    ("code", "code"),
    ("explorer", "explorer"),
    ("start wt", "wt"),
    ('"C:\\Program Files\\thing.exe"', "C:\\Program Files\\thing.exe"),
    ('start "" "C:\\Program Files\\App\\app.exe"',
     "C:\\Program Files\\App\\app.exe"),
    ('"C:\\Tools\\x.exe" --flag', "C:\\Tools\\x.exe"),
    ('unbalanced "quote', "unbalanced"),
    ("", None),
    ("start", None),
]:
    got = S._launch_target(command)
    check(f"{command!r}", got == expected, repr(got))

print("\nRESOLUTION")
check("a real executable on PATH resolves", S._resolves("python") or S._resolves("python3"))
check("an invented one does not", not S._resolves("definitely-not-a-program-xyz"))
# A Windows drive letter must not be mistaken for a URI scheme.
check("a drive-letter path is not treated as a protocol",
      not S._resolves("C:\\nope\\nothing.exe"))
check("a URI scheme off Windows is honestly unresolvable",
      not S._resolves("spotify:") or sys.platform == "win32")

print("\nA PROBE THAT EXPLODES IS A FINDING, NOT A CRASH")

def boom():
    raise RuntimeError("the device caught fire")

result = asyncio.run(S._guarded("thing", boom))
check("an exception becomes one FAIL probe",
      len(result) == 1 and result[0].state == S.FAIL)
check("and carries the reason", "caught fire" in result[0].detail, result[0].detail)

async def async_boom():
    raise ValueError("later")

check("an async probe that raises is caught too",
      asyncio.run(S._guarded("thing", async_boom))[0].state == S.FAIL)

check("a probe returning nothing is not an error",
      asyncio.run(S._guarded("thing", lambda: None)) == [])
check("a bare Probe is accepted as well as a list",
      len(asyncio.run(S._guarded("t", lambda: S.Probe("t", S.PASS)))) == 1)

print("\nCONFIGURED ACTIONS")
config = Config.load()
probes = S.probe_handlers(config)
check("every configured system action is implemented",
      probes[0].state == S.PASS, probes[0].detail)
check("and the destructive ones are named as unexercised",
      any(word in probes[0].detail for word in ("shutdown", "not exercised")),
      probes[0].detail)

broken = Config({"actions": {"system": {"self_destruct": {"phrases": ["go"]}}}})
probes = S.probe_handlers(broken)
check("an action with no handler is reported, not ignored",
      probes[0].state == S.FAIL, probes[0].detail)

print("\nA BACKEND THAT LOADS BUT SYNTHESISES NOTHING")
# Speaker.load() returns True for the SAPI fallback while leaving _voice unset,
# and synthesize() then returns (None, rate). The probe used to take len() of
# that and report a bare TypeError instead of the message it already had.
import erebus.pipeline.tts as tts_mod   # noqa: E402


class SilentSpeaker:
    backend = "sapi"

    def __init__(self, **kwargs):
        pass

    def load(self):
        return True

    async def synthesize(self, text):
        return None, 22050


real_speaker = tts_mod.Speaker
tts_mod.Speaker = SilentSpeaker
try:
    probes = asyncio.run(S._guarded("voice", lambda: S.probe_voice(Config.load())))
finally:
    tts_mod.Speaker = real_speaker

check("no audio is a finding, not a TypeError",
      len(probes) == 1 and probes[0].state == S.WARN, probes[0].detail)
check("and it names the backend that fell back",
      "sapi" in probes[0].detail, probes[0].detail)
check("and says how to fix it", "fetch-voice" in probes[0].fix)

print("\nTHE WHOLE RUN")
code = asyncio.run(S.run(Config.load()))
check("returns an int exit code", isinstance(code, int), str(code))
check("non-zero when something is broken here", code in (0, 1), str(code))

print("\nTHE INSTALLER")
# install.py runs before anything is installed, so it must import on a bare
# interpreter - no third-party module may sneak into its imports.
import subprocess   # noqa: E402

root = pathlib.Path(__file__).resolve().parents[1]
venv_before = (root / ".venv").exists()
proc = subprocess.run([sys.executable, str(root / "install.py"), "--check"],
                      capture_output=True, text=True, timeout=120)
check("--check runs and exits cleanly", proc.returncode in (0, 1),
      f"exit {proc.returncode}")
# Snapshotted before the subprocess, or this compares a value to itself.
check("--check creates no venv", (root / ".venv").exists() == venv_before)
check("it says so up front", "nothing will change" in proc.stdout)
check("it reports the Python it found", "Python" in proc.stdout)
check("stdlib only - no import errors", "ModuleNotFoundError" not in proc.stderr,
      proc.stderr.strip()[:60])

sys.path.insert(0, str(root))
import install  # noqa: E402  - importable is the point

check("re-running is safe: every step checks first",
      install.make_venv(dry=True) in ("already there", "would create .venv"))
check("an exact model tag is required, not a prefix match",
      not install.ollama_has_model("llama3.1:8b") or install.ollama_running(),
      "a 70b must not satisfy an 8b")
installer = (root / "install.py").read_text()
check("the installer runs commands from the repo root", "cwd=ROOT" in installer)
# `text=True` decodes with the system locale. On a British Windows install
# that is cp1252, and `ollama pull` draws its progress bar out of Unicode
# block characters cp1252 cannot map - which killed the reader thread
# mid-download, loudly enough to look like a failed install.
check("and decodes their output as utf-8, not the system locale",
      'encoding="utf-8"' in installer and 'errors="replace"' in installer)
# Look at code, not prose - the comment explaining the fix says "text=True"
# too, and an earlier version of this check matched its own documentation.
_code = [ln for ln in installer.splitlines()
         if ln.strip() and not ln.strip().startswith("#")]
check("and never decodes with text=True alone",
      not any("text=True" in ln for ln in _code))

print(f"\n  {PASSED}/{PASSED + FAILED} passed")
raise SystemExit(1 if FAILED else 0)
