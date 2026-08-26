"""Wake detection, driven by synthesized speech.

Verifies the thing that decides whether the assistant is listening at all:
that the wake word scores high, that ordinary commands score nothing, that the
preroll buffer holds enough audio for a command spoken in the same breath, and
that the refractory window stops one utterance firing twice.

Uses `hey_jarvis` because that is what ships pretrained; the same test is what
you run against a custom `erebus` model once you have trained one (see
docs/WAKEWORD.md), by pointing WAKE_MODEL and WAKE_PHRASE at it.

Needs the voice extras and a Piper voice. Skips cleanly (exit 0) without them.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus.pipeline import tts as tts_mod                    # noqa: E402
from erebus.pipeline.audio import FRAME_MS                    # noqa: E402
from erebus.pipeline.tts import Speaker                       # noqa: E402
from erebus.pipeline.wake import WAKE_AVAILABLE, WakeDetector  # noqa: E402

RATE = 16000
FRAME = int(RATE * FRAME_MS / 1000)

WAKE_MODEL = "hey_jarvis"
WAKE_PHRASE = "Hey Jarvis"
VOICE = "en_GB-alan-medium"

#: Things you say to the assistant that must never wake it.
NEGATIVES = [
    "Gaming mode",
    "Open Spotify please",
    "What is the weather like today",
    "Volume up",
    "Lock the computer",
]


def resample(audio, src: int, dst: int):
    import numpy as np

    n = int(len(audio) / src * dst)
    idx = np.linspace(0, len(audio) - 1, n)
    lo = np.floor(idx).astype(np.int32)
    hi = np.clip(lo + 1, 0, len(audio) - 1)
    frac = (idx - lo).astype(np.float32)
    return (audio[lo] * (1 - frac) + audio[hi] * frac).astype(np.float32)


async def speak(speaker: Speaker, text: str):
    audio, rate = await speaker.synthesize(text)
    return resample(audio, rate, RATE)


def feed(detector: WakeDetector, audio):
    """Push audio through in production-sized frames. Returns (peak, fired_at)."""
    import numpy as np

    # Lead-in silence primes the model's internal buffers the way an always-on
    # stream would.
    stream = np.concatenate([
        np.zeros(RATE, dtype=np.float32), audio,
        np.zeros(RATE // 2, dtype=np.float32),
    ])
    peak, fired_at = 0.0, None
    for i in range(0, len(stream) - FRAME, FRAME):
        score = detector.push(stream[i:i + FRAME])
        peak = max(peak, score)
        if fired_at is None and detector.fired(score):
            fired_at = i / RATE
    return peak, fired_at


async def run() -> int:
    if not WAKE_AVAILABLE or tts_mod.resolve_voice(VOICE) is None:
        print("  skipped - voice extras or a Piper voice are not installed")
        return 0

    speaker = Speaker(backend="piper", voice=VOICE, effects={"enabled": False})
    if not speaker.load():
        print("  skipped - Piper voice would not load")
        return 0

    detector = WakeDetector(model=WAKE_MODEL, threshold=0.55, sample_rate=RATE)
    if not detector.load():
        print("  skipped - wake model would not load")
        return 0

    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")

    def fresh() -> WakeDetector:
        detector.reset()
        detector._last_fire = 0.0
        return detector

    # --- the wake word itself ------------------------------------------------
    peak, fired_at = feed(fresh(), await speak(speaker, WAKE_PHRASE))
    check(f"{WAKE_PHRASE!r} fires", fired_at is not None, f"peak={peak:.3f}")
    wake_peak = peak

    # --- ordinary commands must not wake it ----------------------------------
    worst = 0.0
    for phrase in NEGATIVES:
        peak, fired_at = feed(fresh(), await speak(speaker, phrase))
        worst = max(worst, peak)
        check(f"{phrase!r} does not fire", fired_at is None, f"peak={peak:.3f}")

    # A threshold is only meaningful if there is daylight either side of it.
    check("separation is wide enough to be safe",
          wake_peak - worst > 0.5, f"{worst:.3f} .. {wake_peak:.3f}")

    # --- preroll -------------------------------------------------------------
    # "Erebus, gaming mode" said in one breath: the command is already spoken by
    # the time the word fires, so the buffer has to have kept it.
    import numpy as np

    detector = fresh()
    audio = await speak(speaker, f"{WAKE_PHRASE}, gaming mode")
    stream = np.concatenate([np.zeros(RATE, dtype=np.float32), audio])
    held = 0.0
    for i in range(0, len(stream) - FRAME, FRAME):
        if detector.fired(detector.push(stream[i:i + FRAME])):
            held = sum(len(f) for f in detector.preroll()) / RATE
            break
    check("preroll retains over a second of lead-in", held >= 1.0, f"{held:.2f}s")

    # --- refractory ----------------------------------------------------------
    guard = WakeDetector(model=WAKE_MODEL, threshold=0.55, refractory=1.0,
                         sample_rate=RATE)
    guard.load()
    accepted = sum(1 for _ in range(5) if guard.fired(0.9))
    check("one utterance cannot fire twice", accepted == 1, f"{accepted} accepted")
    time.sleep(1.1)
    check("it re-arms after the window", guard.fired(0.9))

    print(f"\n  {'all checks passed' if not failures else f'{failures} failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
