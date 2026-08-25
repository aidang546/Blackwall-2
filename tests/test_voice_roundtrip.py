"""Speech round trip: synthesise a command, transcribe it, route it.

This is the closest we can get to a real interaction without a microphone. It
proves the three heavy stages actually talk to each other - Piper's output is
shaped the way Whisper expects, Whisper's text is shaped the way the matcher
expects - which is exactly the seam that unit tests miss.

Needs the voice extras and one Piper voice:

    pip install -r requirements-voice.txt
    python -m erebus fetch-voice en_GB-alan-medium

Skips cleanly (exit 0) if either is missing, so it is safe in CI.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus.actions.registry import Registry          # noqa: E402
from erebus.core.config import Config                 # noqa: E402
from erebus.pipeline import tts as tts_mod            # noqa: E402
from erebus.pipeline.stt import STT_AVAILABLE, Transcriber   # noqa: E402
from erebus.pipeline.tts import Speaker               # noqa: E402

#: Whisper wants 16 kHz mono; Piper emits 22.05 kHz.
STT_RATE = 16000

# What a person would actually say, and where it must land.
CASES = [
    ("Gaming mode.", "gaming_mode"),
    ("Open Spotify.", "spotify"),
    ("Volume up.", "volume_up"),
    ("Next track.", "next_track"),
    ("Lock the computer.", "lock"),
]


def resample(audio, source_rate: int, target_rate: int):
    import numpy as np

    if source_rate == target_rate:
        return audio
    duration = len(audio) / source_rate
    target_n = int(duration * target_rate)
    idx = np.linspace(0, len(audio) - 1, target_n)
    lo = np.floor(idx).astype(np.int32)
    hi = np.clip(lo + 1, 0, len(audio) - 1)
    frac = (idx - lo).astype(np.float32)
    return (audio[lo] * (1 - frac) + audio[hi] * frac).astype(np.float32)


async def main() -> int:
    config = Config.load()
    registry = Registry(config)

    voice = config.get("tts.voice", "en_GB-alan-medium")
    if not STT_AVAILABLE or tts_mod.resolve_voice(voice) is None:
        print("  skipped - voice extras or a Piper voice are not installed")
        return 0

    # Speak dry: the effects chain is for the operator's ears, and band-limiting
    # the signal before transcription would only be testing the effects.
    speaker = Speaker(backend="piper", voice=voice, effects={"enabled": False})
    if not speaker.load():
        print("  skipped - Piper voice would not load")
        return 0

    stt = Transcriber(
        model=config.get("stt.model", "tiny.en"),
        device=config.get("stt.device", "cpu"),
        compute_type=config.get("stt.compute_type", "int8"),
    )
    if not stt.load():
        print("  skipped - Whisper would not load")
        return 0

    failures = 0
    print(f"\n  voice={voice}  stt={stt.model_name} on {stt.device}\n")

    for spoken, want in CASES:
        audio, rate = await speaker.synthesize(spoken)
        audio = resample(audio, rate, STT_RATE)

        started = time.monotonic()
        heard = await stt.transcribe(audio)
        elapsed = (time.monotonic() - started) * 1000

        match = registry.match(heard)
        got = match.action.name if match and (match.exact or match.score >= 6) else None

        ok = got == want
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  said {spoken!r:<24} heard {heard!r:<26} "
              f"-> {got}{'' if ok else f'  (wanted {want})'}   [{elapsed:.0f}ms]")

    print(f"\n  {len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
