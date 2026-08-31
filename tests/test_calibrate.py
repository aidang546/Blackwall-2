"""Calibration: the arithmetic, and the walk through it.

Three settings in config.yaml are properties of a room rather than
preferences, and shipped defaults are guesses about someone else's room.
`erebus calibrate` measures them instead. None of that can be tested with a
real microphone here, so the analysis is written as pure functions and the
capture is stubbed - which is also why the analysis is pure in the first place.

    python tests/test_calibrate.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus import calibrate as cal        # noqa: E402
from erebus.core.config import Config      # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {label:<48} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label:<48} {detail}")


print("\nPERCENTILES")
check("interpolates between samples",
      cal.percentile_rms([0.1, 0.2, 0.3, 0.4], 0.5) == 0.25)
check("an empty run is zero, not a crash", cal.percentile_rms([], 0.9) == 0.0)
check("one sample is itself", cal.percentile_rms([0.42], 0.1) == 0.42)
check("clamps out-of-range quantiles",
      cal.percentile_rms([1.0, 2.0], 5.0) == 2.0)
# The reason it is a percentile and not a mean.
noisy_floor = [0.003] * 99 + [0.9]           # 99 quiet frames and one door slam
check("one loud frame does not set the noise floor",
      cal.percentile_rms(noisy_floor, 0.9) < 0.01,
      f"{cal.percentile_rms(noisy_floor, 0.9):.4f}")

print("\nSILENCE GATE")
gate, notes = cal.derive_gate(0.004, 0.09)
check("a quiet room puts the gate above the floor", 0.004 < gate < 0.09, f"{gate}")
check("and says nothing when there is nothing to say", not notes)

gate, notes = cal.derive_gate(0.03, 0.05)
check("a noisy room still yields a usable gate", 0 < gate < 0.05, f"{gate}")
check("and warns that the ratio is too low", any("4x" in n for n in notes))

gate, _ = cal.derive_gate(0.02, 0.09)
check("the gate never rises above quiet speech", gate <= 0.09 * 0.35, f"{gate}")

gate, notes = cal.derive_gate(0.004, 0.0)
check("no speech measured still gives a gate", gate > 0, f"{gate}")
check("and says so", any("No speech" in n for n in notes))

check("a silent room does not give a zero gate",
      cal.derive_gate(0.0, 0.05)[0] > 0)

print("\nBARGE-IN")
setting, notes = cal.derive_barge_in(0.01, 0.09, 0.005)
check("headphones keep the default", setting["threshold_multiplier"] == 3.5)
check("and raise no warning", not notes)

setting, notes = cal.derive_barge_in(0.01, 0.09, 0.05)
check("loud speakers raise the bar", setting["threshold_multiplier"] > 3.5,
      f"{setting['threshold_multiplier']}")
check("and warn about the echo path", any("headphones" in n for n in notes))

setting, notes = cal.derive_barge_in(0.01, 0.09, 0.30)
check("an unwinnable echo path is capped, not infinite",
      setting["threshold_multiplier"] <= 12, f"{setting['threshold_multiplier']}")
check("and says turning the volume down is the real fix",
      any("volume down" in n for n in notes))

print("\nDEVICE CHOICE")
device, notes = cal.choose_device(
    {0: 0.00001, 1: 0.03, 2: 0.006}, {0: "line in", 1: "usb mic", 2: "webcam"})
check("picks the input that actually heard something", device == 1, f"{device}")
check("and reports the runners-up", any("webcam" in n for n in notes))
check("silence everywhere picks nothing",
      cal.choose_device({0: 0.0, 1: 0.0}, {})[0] is None)
check("and explains why",
      "muted" in cal.choose_device({0: 0.0}, {})[1][0])

print("\nTHE WHOLE WALK, WITH A STUBBED MICROPHONE")

QUIET = [0.004] * 40
SPOKEN = [0.004] * 10 + [0.11] * 40 + [0.004] * 10
ECHO = [0.004] * 5 + [0.02] * 50

async def fake_levels(seconds, device=None, sample_rate=16000):
    """Keyed on duration, not call order.

    An earlier version counted calls and got the sequence wrong, so the walk
    silently measured the wrong thing and still passed - the gate came out at
    its floor rather than derived from these levels.
    """
    fake_levels.seen.append(seconds)
    return {3.0: QUIET, 5.0: SPOKEN, 6.0: ECHO}[seconds]

fake_levels.seen = []

spoken_lines = []

async def fake_speak(text):
    spoken_lines.append(text)

real_levels = cal._levels
cal._levels = fake_levels
cal._countdown = lambda message, seconds: None
cal._probe_devices = lambda sr: asyncio.sleep(
    0, result=({0: 0.0001, 1: 0.02}, {0: "line in", 1: "usb mic"}))

result = asyncio.run(cal.measure(Config.load(), fake_speak))

check("chose the live input", result.settings.get("input_device") == 1,
      str(result.settings.get("input_device")))
check("measured all three windows", fake_levels.seen == [3.0, 5.0, 6.0],
      str(fake_levels.seen))
gate_value = result.settings.get("silence_threshold", 0)
check("derived the gate from the room, not from a floor",
      abs(gate_value - 0.004 * cal.GATE_HEADROOM) < 1e-6, str(gate_value))
check("the gate sits between the noise and the speech",
      0.004 < gate_value < 0.11, str(gate_value))
multiplier = result.settings.get("barge_in", {}).get("threshold_multiplier", 0)
check("raised the barge-in bar above the measured echo",
      multiplier * gate_value > 0.02, f"{multiplier} x {gate_value}")
check("recorded every reading it took", len(result.readings) == 3,
      f"{[r.name for r in result.readings]}")
check("played something for the echo test", len(spoken_lines) == 1)

print("\nA DEVICE THAT OPENS AND THEN SAYS NOTHING")
# Microphone.frames() yields nothing at all while its queue is empty, so a
# deadline checked only inside the loop never fires. A muted or seized input
# would wedge `erebus calibrate` with no output and no timeout - and
# _probe_devices opens every input on the machine in turn, so one is enough.
import types  # noqa: E402

class SilentMic:
    def __init__(self, config):
        self.config = config
    def start(self):
        pass
    def stop(self):
        SilentMic.stopped = True
    async def frames(self):
        while True:                       # exactly what the real one does
            await asyncio.sleep(0.05)
            if False:
                yield None

SilentMic.stopped = False
cal._levels = real_levels      # the walk above stubbed it; this test needs the real one
import erebus.pipeline.audio as real_audio   # noqa: E402  - imported lazily by _levels
fake = types.SimpleNamespace(
    AudioConfig=real_audio.AudioConfig, Microphone=SilentMic,
    rms=lambda f: 0.0, AUDIO_AVAILABLE=True)
# `from .pipeline import audio` reads the attribute on the package, not
# sys.modules, so both have to be swapped for the stub to take effect.
import erebus.pipeline   # noqa: E402

sys.modules["erebus.pipeline.audio"] = fake
erebus.pipeline.audio = fake

started = time.monotonic()
levels = asyncio.run(cal._levels(0.4))
elapsed = time.monotonic() - started
sys.modules["erebus.pipeline.audio"] = real_audio
erebus.pipeline.audio = real_audio

check("a silent device returns instead of hanging", elapsed < 5.0, f"{elapsed:.1f}s")
check("and returns no levels rather than inventing them", levels == [])
check("and the stream is closed on the way out", SilentMic.stopped)

print("\nWRITING config.local.yaml")
with tempfile.TemporaryDirectory() as tmp:
    target = pathlib.Path(tmp) / "config.local.yaml"

    target.write_text("audio:\n  output_device: 7\nbrain:\n  model: keep-me\n")
    cal.write_local(cal.settings_from(result), target)
    import yaml

    written = yaml.safe_load(target.read_text())
    check("keeps unrelated sections", written["brain"]["model"] == "keep-me")
    check("keeps unrelated keys in the same section",
          written["audio"]["output_device"] == 7)
    check("adds what it measured", "silence_threshold" in written["audio"])
    check("the result is loadable YAML", isinstance(written, dict))

    # Re-running must not compound or duplicate.
    cal.write_local(cal.settings_from(result), target)
    again = yaml.safe_load(target.read_text())
    check("re-running is idempotent", again == written)

    broken = pathlib.Path(tmp) / "broken.yaml"
    broken.write_text("audio: [unclosed\n")
    try:
        cal.write_local({"audio": {"x": 1}}, broken)
        check("refuses to overwrite a file it cannot parse", False)
    except RuntimeError as exc:
        check("refuses to overwrite a file it cannot parse", True, str(exc)[:40])

print(f"\n  {PASSED}/{PASSED + FAILED} passed")
raise SystemExit(1 if FAILED else 0)
