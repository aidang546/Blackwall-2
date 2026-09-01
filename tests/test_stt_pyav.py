"""Whisper must survive PyAV being blocked.

Found on the first real Windows install. faster_whisper imports `av` at module
level - only for `decode_audio`, which turns an audio *file* into samples, and
which Erebus never calls because it hands the model an array it captured
itself. PyAV ships unsigned FFmpeg DLLs, and Smart App Control (on by default
on new Windows 11 machines) blocks them:

    ImportError: DLL load failed while importing hwaccel:
    An Application Control policy has blocked this file.

That took out the whole of faster_whisper, so the assistant lost its hearing
over a dependency it does not use. The other fix is for the operator to turn
off Smart App Control, which cannot be undone without reinstalling Windows.

Tested in a subprocess with a sabotaged `av`, because the import happens once
per interpreter and cannot be undone in-process.

    python tests/test_stt_pyav.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {label:<50} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label:<50} {detail}")


def run_with_blocked_av(body: str, leftover: str = "") -> subprocess.CompletedProcess:
    """Run `body` in a fresh interpreter where importing `av` raises.

    `leftover` injects extra setup - used to reproduce the half-built module a
    mid-import DLL failure leaves behind.
    """
    script = textwrap.dedent(f"""
        import sys, importlib.abc, importlib.machinery
        sys.path.insert(0, {str(ROOT)!r})

        class _BlockAv(importlib.abc.MetaPathFinder):
            '''Fail to *load* av, the way a blocked DLL does.

            An earlier version of this harness replaced __import__ itself,
            which is harsher than reality: real Windows lets the import
            machinery run and only fails when the extension module loads its
            DLL. Crucially, that means sys.modules is still consulted first -
            which is exactly what the fix relies on. Overriding __import__
            skipped that lookup and made a working fix look broken.'''

            def find_spec(self, name, path=None, target=None):
                if name == "av" or name.startswith("av."):
                    raise ImportError(
                        "DLL load failed while importing hwaccel: "
                        "An Application Control policy has blocked this file")
                return None

        for _name in [n for n in sys.modules if n == "av" or n.startswith("av.")]:
            del sys.modules[_name]
        sys.meta_path.insert(0, _BlockAv())
        {leftover}

        {body}
    """)
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=300)


print("\nIMPORTING WITH PyAV BLOCKED")
proc = run_with_blocked_av("""
        from erebus.pipeline.stt import STT_AVAILABLE, PYAV_BLOCKED
        print("AVAILABLE", STT_AVAILABLE)
        print("BLOCKED", PYAV_BLOCKED)
""")
check("the module imports at all", proc.returncode == 0,
      proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "")
check("speech recognition is still available",
      "AVAILABLE True" in proc.stdout, proc.stdout.strip())
check("and it knows it substituted a stand-in",
      "BLOCKED True" in proc.stdout, proc.stdout.strip())

print("\nTRANSCRIBING WITH PyAV BLOCKED")
proc = run_with_blocked_av("""
        import numpy as np
        from erebus.pipeline.stt import STT_AVAILABLE, WhisperModel
        if not STT_AVAILABLE:
            print("SKIP faster-whisper not installed")
        else:
            m = WhisperModel("tiny.en", device="cpu", compute_type="int8")
            sr = 16000
            audio = np.zeros(sr, dtype=np.float32)
            segments, info = m.transcribe(audio, language="en")
            list(segments)
            print("DURATION", round(info.duration, 1))
""")
if "SKIP" in proc.stdout:
    print("  --    faster-whisper not installed - skipping the model run")
else:
    check("a model loads and consumes an array", "DURATION 1.0" in proc.stdout,
          proc.stdout.strip() or proc.stderr.strip()[-160:])

print("\nWHEN A FAILED IMPORT LEAVES A HALF-BUILT MODULE BEHIND")
# A DLL that dies part-way through an extension module's init leaves a
# partially-initialised module in sys.modules. The first version of the fix
# used setdefault, which will not replace one - so the stand-in was never
# installed and faster_whisper imported the broken remains instead.
# `av` itself must still fail to import - the blocker above does that - while
# stale submodules survive from the attempt, which is what Python leaves when
# a DLL dies part-way through init.
proc = run_with_blocked_av("""
        from erebus.pipeline.stt import STT_AVAILABLE, PYAV_BLOCKED
        import sys
        print("AVAILABLE", STT_AVAILABLE)
        print("BLOCKED", PYAV_BLOCKED)
        print("STANDIN", type(sys.modules["av"]).__name__)
        print("SUBMODULE", type(sys.modules["av.audio"]).__name__)
""", leftover=(
    'import types\n'
    '        sys.modules["av.audio"] = types.ModuleType("av.audio")\n'
    '        sys.modules["av.codec"] = types.ModuleType("av.codec")'
))
check("whisper still imports", "AVAILABLE True" in proc.stdout,
      proc.stdout.strip() or proc.stderr.strip()[-200:])
check("the stand-in replaced the module", "STANDIN _Absent" in proc.stdout,
      proc.stdout.strip())
check("and the stale submodules were purged too",
      "SUBMODULE _Absent" in proc.stdout, proc.stdout.strip())

print("\nWHEN PyAV IS FINE, NOTHING IS TOUCHED")
proc = subprocess.run(
    [sys.executable, "-c",
     f"import sys; sys.path.insert(0, {str(ROOT)!r});"
     " from erebus.pipeline.stt import PYAV_BLOCKED; print('BLOCKED', PYAV_BLOCKED)"],
    capture_output=True, text=True, timeout=300)
try:
    import av  # noqa: F401
    have_av = True
except Exception:  # noqa: BLE001
    have_av = False
if have_av:
    check("a working PyAV is left alone", "BLOCKED False" in proc.stdout,
          proc.stdout.strip())
else:
    print("  --    PyAV not importable here either - nothing to compare")

print(f"\n  {PASSED}/{PASSED + FAILED} passed")
raise SystemExit(1 if FAILED else 0)
