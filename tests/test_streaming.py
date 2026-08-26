"""Streaming speech and barge-in.

Two things decide whether talking to Erebus feels like talking to something or
like operating a machine: how long it waits before the first word, and whether
you can cut it off. Both are covered here without a model, a microphone or a
sound card.

    python tests/test_streaming.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus.core.assistant import Assistant          # noqa: E402
from erebus.core.bus import EventBus                 # noqa: E402
from erebus.core.config import Config                # noqa: E402
from erebus.core.state import State                  # noqa: E402
from erebus.pipeline import chunker                  # noqa: E402

FAILURES = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    FAILURES += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")


async def feed(text: str, size: int = 5):
    """Emit text in small pieces, the way a model streams it."""
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def collect(text: str, size: int = 5) -> list[str]:
    return [c async for c in chunker.to_sentences(feed(text, size))]


# --------------------------------------------------------------------------

async def test_chunker() -> None:
    print("\nCHUNKING")

    chunks = await collect(
        "Two videos in thirty days. You are four behind. Fix it."
    )
    check("splits on sentence ends", len(chunks) >= 2, f"{len(chunks)}")
    check("the first chunk is exactly the first sentence",
          chunks[0] == "Two videos in thirty days.", repr(chunks[0]))
    check("keeps the punctuation", chunks[0].endswith("."))
    # Later chunks hold out for a longer break, so two short trailing sentences
    # are spoken together rather than with a gap between them. That is the
    # intent: only the first cut is heard as latency.
    check("short trailing sentences are merged, not spoken choppily",
          chunks[-1] == "You are four behind. Fix it.", repr(chunks[-1]))

    # The whole point: the first chunk must be short so speech starts.
    long_reply = (
        "Consistency beats intensity because it compounds. "
        "Intensity is a single withdrawal against a balance you have not built. "
        "The work you do every day is the only work that accumulates."
    )
    chunks = await collect(long_reply)
    check("first chunk is a single sentence, not the whole reply",
          len(chunks[0]) < len(long_reply) / 2, f"{len(chunks[0])} chars")

    for text in [
        "Dr. Smith said 3.5 kg at 7 a.m. That is not what you wrote down.",
        "Yes.",
        "One long clause that never terminates and simply keeps going well past "
        "any reasonable length without punctuation of any kind whatsoever",
        "",
    ]:
        chunks = await collect(text)
        joined = "".join(chunks).replace(" ", "")
        check(f"loses no text: {text[:34]!r}...",
              joined == text.replace(" ", ""))

    abbrev = await collect("Dr. Smith weighs 3.5 kg. He arrived at 7 a.m. sharp.")
    check("does not split on an abbreviation's full stop",
          all("Dr." not in c or "Smith" in c for c in abbrev),
          f"{len(abbrev)} chunks")

    check("an empty stream yields nothing", await collect("") == [])

    # A chunk of pure whitespace must not reach the synthesiser.
    check("whitespace-only input yields nothing", await collect("   \n  ") == [])


async def test_presets() -> None:
    """Every preset must produce finite, un-clipped audio.

    A preset is a bag of keys read by name; a typo produces silence or a burst
    of noise rather than an error, so each one is actually run.
    """
    print("\nVOICE PRESETS")
    from erebus.pipeline import voicefx

    if not voicefx.FX_AVAILABLE:
        print("  skipped - numpy/scipy not installed")
        return

    import numpy as np

    sample_rate = 22050
    speech = (np.sin(np.linspace(0, 400 * np.pi, sample_rate * 2)) * 0.3
              ).astype(np.float32)

    for name, preset in voicefx.PRESETS.items():
        settings = dict(preset)
        settings["enabled"] = True
        out = voicefx.process(speech, sample_rate, settings)
        check(f"{name}: finite and bounded",
              np.isfinite(out).all() and 0.05 < np.abs(out).max() <= 1.0,
              f"peak {np.abs(out).max():.2f}")
        check(f"{name}: length preserved", len(out) == len(speech))

    # A preset name plus an override must merge, not replace.
    merged = voicefx.resolve({"preset": "blackwall", "reverb": 0.9})
    check("an explicit key overrides the preset", merged["reverb"] == 0.9)
    check("and the rest of the preset survives",
          merged.get("detune_voices") == 3)
    check("an unknown preset degrades rather than raising",
          voicefx.resolve({"preset": "nonsense", "reverb": 0.1})["reverb"] == 0.1)
    check("no preset passes settings through untouched",
          voicefx.resolve({"reverb": 0.5}) == {"reverb": 0.5})


async def test_barge_in() -> None:
    print("\nBARGE-IN")

    config = Config.load()
    bus = EventBus()
    assistant = Assistant(config, bus)

    interrupted = {"count": 0}
    assistant.speaker.interrupt = lambda: interrupted.__setitem__(
        "count", interrupted["count"] + 1
    )

    quiet = assistant.barge_in_threshold * 0.2
    loud = assistant.barge_in_threshold * 2.0
    needed = assistant.barge_in_frames

    check("gate sits well above the silence floor",
          assistant.barge_in_threshold > config.get("audio.silence_threshold"),
          f"{assistant.barge_in_threshold:.3f}")
    check("sustain is more than one frame", needed > 1, f"{needed} frames")

    # Silence must never trigger it.
    for _ in range(needed * 3):
        assistant._check_barge_in(quiet)
    check("silence does not interrupt", interrupted["count"] == 0)

    # A single loud frame is a cough or a door, not an interruption.
    assistant._check_barge_in(loud)
    check("one loud frame does not interrupt", interrupted["count"] == 0)

    # Its own voice leaking back is loud but intermittent.
    for _ in range(needed * 4):
        assistant._check_barge_in(loud)
        assistant._check_barge_in(quiet)
    check("loud-then-quiet flapping does not interrupt",
          interrupted["count"] == 0, f"{interrupted['count']}")

    # Sustained speech does.
    for _ in range(needed):
        assistant._check_barge_in(loud)
    check("sustained speech interrupts", interrupted["count"] == 1,
          f"{interrupted['count']}")

    # And it re-arms for the next reply.
    for _ in range(needed):
        assistant._check_barge_in(loud)
    check("re-arms afterwards", interrupted["count"] == 2)

    assistant.barge_in_enabled = False
    for _ in range(needed * 2):
        assistant._check_barge_in(loud)
    check("respects being switched off", interrupted["count"] == 2)

    await assistant.stop()


async def test_speak_stream_contract() -> None:
    """speak_stream must report what was *spoken*, not what was generated."""
    print("\nSPOKEN-VS-GENERATED")

    config = Config.load()
    bus = EventBus()
    assistant = Assistant(config, bus)

    # No sound card in CI, so the speaker returns the text unplayed. The
    # contract under test is the assistant's, not PortAudio's.
    spoken = await assistant.say_stream(feed("First part. Second part."))
    check("returns the text it handled", "First part." in spoken, repr(spoken))
    check("returns to idle afterwards", bus.state is State.IDLE)

    published = []
    queue = bus.subscribe()
    await assistant.say_stream(feed("Alpha. Beta. Gamma."))
    while not queue.empty():
        published.append(queue.get_nowait())
    replies = [e for e in published if e.kind == "reply"]
    check("publishes the caption progressively", len(replies) >= 2,
          f"{len(replies)} reply events")
    if replies:
        check("the caption accumulates rather than replacing",
              len(replies[-1].data["text"]) > len(replies[0].data["text"]))

    await assistant.stop()


async def main() -> int:
    await test_chunker()
    await test_presets()
    await test_barge_in()
    await test_speak_stream_contract()
    print(f"\n  {'all checks passed' if not FAILURES else f'{FAILURES} failed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
