"""Measure this machine and this room, then write the numbers down.

Three of the settings in config.yaml are not preferences, they are properties
of a room: how loud the silence is, how loud you are, and how much of Erebus's
own voice comes back through the microphone. Shipped defaults are guesses about
someone else's room, and getting them wrong is what turns a first run into an
evening of nudging numbers - it wakes at nothing, or cuts you off, or talks
over itself.

So this measures them. Everything it writes is derived from something it
recorded, and the report says what it measured and what it concluded, so a bad
number is visible rather than mysterious.

    python -m erebus calibrate

The analysis is deliberately separate from the capture: the functions that turn
levels into settings are pure, and tested without a microphone anywhere near
them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger("erebus.calibrate")

#: How far above the noise floor the silence gate sits. A gate on the floor
#: itself never closes; one far above it clips the ends off quiet words.
GATE_HEADROOM = 2.5

#: Minimum ratio of speech to noise for any of this to work. Below it the
#: microphone is picking up more room than person.
USABLE_SNR = 4.0


@dataclass
class Reading:
    """One measured level, kept with the evidence behind it."""
    name: str
    rms: float
    frames: int = 0
    note: str = ""


@dataclass
class Result:
    readings: list[Reading] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, reading: Reading) -> Reading:
        self.readings.append(reading)
        return reading


# --------------------------------------------------------------------------
#  The analysis. No audio hardware is involved past this point.
# --------------------------------------------------------------------------

def percentile_rms(levels, q: float) -> float:
    """A percentile of frame levels, without numpy.

    Used instead of a mean because both ends matter and both are skewed: one
    door slam should not set the noise floor, and one loud syllable should not
    define speech.
    """
    values = sorted(float(v) for v in levels)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * max(0.0, min(1.0, q))
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def derive_gate(noise: float, speech: float) -> tuple[float, list[str]]:
    """Where silence ends and speech begins, from the two measured levels."""
    notes: list[str] = []
    gate = noise * GATE_HEADROOM

    if speech <= 0:
        return max(gate, 0.004), ["No speech was measured - keeping a cautious gate."]

    snr = speech / max(noise, 1e-9)
    if snr < USABLE_SNR:
        notes.append(
            f"Speech is only {snr:.1f}x the room noise. Move the microphone "
            "closer or reduce the noise; below about 4x nothing here can be "
            "tuned into working."
        )
    # Never let the gate sit above the quiet parts of actual speech, or the
    # ends of sentences get cut off - which reads as "it keeps interrupting me".
    ceiling = speech * 0.35
    if gate > ceiling:
        gate = ceiling
        notes.append(
            "The noise floor is close to speech level, so the gate was placed "
            "by your voice rather than by the room."
        )
    return round(max(gate, 0.002), 4), notes


def derive_barge_in(gate: float, speech: float, echo: float) -> tuple[dict, list[str]]:
    """How loud you must be, over its own voice, to actually interrupt it.

    The hard case is speakers rather than headphones: Erebus's own voice comes
    back through the microphone, and if the interrupt level sits under that, it
    hears itself and stops mid-sentence every time.
    """
    notes: list[str] = []
    if gate <= 0:
        gate = 0.004

    # Above the echo with margin, and above the gate by at least the default.
    from_echo = echo * 1.6
    multiplier = max(3.5, from_echo / gate)

    if speech > 0 and from_echo > speech * 0.8:
        notes.append(
            f"Its own voice comes back at {echo:.4f}, close to your own "
            f"{speech:.4f}. On speakers it cannot reliably tell you apart from "
            "itself - use headphones, or set audio.barge_in.enabled: false."
        )
    if multiplier > 12:
        notes.append(
            "The echo is loud enough that interrupting would need a shout. "
            "Turning the output volume down helps more than any setting here."
        )
        multiplier = 12.0
    return {"threshold_multiplier": round(multiplier, 1)}, notes


def choose_device(levels: dict[int, float], names: dict[int, str]) -> tuple[int | None, list[str]]:
    """Pick the input that actually heard something.

    A machine with four inputs usually has three that are silent - a disabled
    line-in, a webcam nobody is pointing at, a virtual cable. The default is
    often one of those, and the symptom is Erebus never hearing anything with
    no error anywhere.
    """
    notes: list[str] = []
    live = {i: v for i, v in levels.items() if v > 0.0008}
    if not live:
        return None, ["No input heard anything. Check the microphone is not muted."]
    best = max(live, key=lambda i: levels[i])
    if len(live) > 1:
        ranked = sorted(live, key=lambda i: -levels[i])[:3]
        notes.append("Heard: " + ", ".join(
            f"{names.get(i, i)} ({levels[i]:.4f})" for i in ranked))
    return best, notes


def settings_from(result: Result) -> dict:
    """Fold the readings into the shape config.local.yaml expects."""
    audio = {}
    if result.settings.get("input_device") is not None:
        audio["input_device"] = result.settings["input_device"]
    if "silence_threshold" in result.settings:
        audio["silence_threshold"] = result.settings["silence_threshold"]
    if "barge_in" in result.settings:
        audio["barge_in"] = result.settings["barge_in"]
    return {"audio": audio} if audio else {}


# --------------------------------------------------------------------------
#  The capture. Everything here needs real hardware and degrades without it.
# --------------------------------------------------------------------------

async def _levels(seconds: float, device=None, sample_rate: int = 16000) -> list[float]:
    """Frame levels over a window, straight off the input.

    The deadline is enforced from outside the loop as well as inside it.
    `Microphone.frames()` yields nothing at all while its queue is empty, so a
    device that opens successfully and then never fires its callback - which is
    exactly what a muted or seized input does - would otherwise wedge this
    forever. `_probe_devices` opens every input on the machine in turn, so one
    such device is enough to hang the whole command with no output.
    """
    from .pipeline import audio as audio_mod

    config = audio_mod.AudioConfig(sample_rate=sample_rate, device=device)
    mic = audio_mod.Microphone(config)
    out: list[float] = []

    async def collect() -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        async for frame in mic.frames():
            out.append(audio_mod.rms(frame))
            if asyncio.get_running_loop().time() >= deadline:
                return

    mic.start()
    try:
        await asyncio.wait_for(collect(), timeout=seconds + 2.0)
    except asyncio.TimeoutError:
        log.debug("device %s opened but delivered %d frames", device, len(out))
    finally:
        mic.stop()
    return out


def _countdown(message: str, seconds: float) -> None:
    import sys
    import time

    print(f"  {message}")
    for remaining in range(int(seconds), 0, -1):
        sys.stdout.write(f"\r    {remaining}... ")
        sys.stdout.flush()
        time.sleep(1.0)
    sys.stdout.write("\r    listening   \n")
    sys.stdout.flush()


async def _probe_devices(sample_rate: int) -> tuple[dict, dict]:
    """A short listen on every input, to find the one that is really connected."""
    from .pipeline import audio as audio_mod

    levels, names = {}, {}
    for device in audio_mod.list_devices():
        index = device["index"]
        names[index] = device["name"][:32]
        try:
            frames = await _levels(0.8, device=index, sample_rate=sample_rate)
            levels[index] = percentile_rms(frames, 0.9)
        except Exception as exc:  # noqa: BLE001 - a busy or dead device is data
            log.debug("device %s unavailable: %s", index, exc)
            levels[index] = 0.0
    return levels, names


async def measure(config, speak) -> Result:
    """Walk the room measurements in order, returning what was found.

    `speak` is an async callable that says a line out loud; the echo test needs
    Erebus's actual output path, not a synthetic tone, because what matters is
    how loud *it* is in *this* microphone.
    """
    result = Result()
    sample_rate = int(config.get("audio.sample_rate", 16000))

    print("\n  Looking for a microphone that is actually connected.")
    levels, names = await _probe_devices(sample_rate)
    device, notes = choose_device(levels, names)
    result.warnings += notes
    if device is None:
        result.warnings.append("Calibration stopped: nothing to measure.")
        return result
    result.settings["input_device"] = device
    print(f"  Using {names.get(device, device)} (input {device}).\n")

    # Each capture is guarded: a device seized by another program, or unplugged
    # halfway through, must not throw away the readings already taken.
    async def capture(seconds: float, label: str) -> list[float] | None:
        try:
            return await _levels(seconds, device=device, sample_rate=sample_rate)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(
                f"The microphone stopped working while measuring {label} "
                f"({type(exc).__name__}: {exc}).")
            return None

    _countdown("Stay quiet. Measuring the room.", 3)
    quiet = await capture(3.0, "the room")
    if quiet is None:
        return result
    noise = result.add(Reading("room noise", percentile_rms(quiet, 0.9), len(quiet),
                               "90th percentile, so one noise does not set it"))

    _countdown('Now speak normally - say "Erebus, open the browser" a few times.', 3)
    spoken = await capture(5.0, "your voice")
    if spoken is None:
        return result
    # The top third: the quiet gaps between words are not speech.
    speech = result.add(Reading("your voice", percentile_rms(spoken, 0.75), len(spoken)))

    gate, notes = derive_gate(noise.rms, speech.rms)
    result.settings["silence_threshold"] = gate
    result.warnings += notes

    print("\n  Now measuring how much of its own voice comes back to the mic.")
    print("  Stay quiet while it talks.\n")
    echo_task = asyncio.create_task(_levels(6.0, device=device, sample_rate=sample_rate))
    await asyncio.sleep(0.3)
    spoke = True
    try:
        await speak("Measuring the echo path. This is what the microphone hears "
                    "when I speak, and it decides whether you can interrupt me.")
    except Exception as exc:  # noqa: BLE001
        spoke = False
        result.warnings.append(f"Could not play audio for the echo test ({exc}).")
    try:
        echo_frames = await echo_task
    except Exception as exc:  # noqa: BLE001
        echo_frames = []
        result.warnings.append(f"The echo measurement failed ({exc}).")

    if not spoke or not echo_frames:
        # Six seconds of silence would measure as a perfectly quiet echo path
        # and write the most permissive barge-in setting there is - from a
        # measurement that never happened. Leave it at the shipped default.
        result.warnings.append(
            "Barge-in was left alone: the echo path could not be measured, and "
            "guessing it from silence is worse than the default.")
        return result

    echo = result.add(Reading("its own voice, via the mic",
                              percentile_rms(echo_frames, 0.9), len(echo_frames)))
    barge, notes = derive_barge_in(gate, speech.rms, echo.rms)
    result.settings["barge_in"] = barge
    result.warnings += notes
    return result


def report(result: Result) -> None:
    print("\n  measured")
    for reading in result.readings:
        note = f"   {reading.note}" if reading.note else ""
        print(f"    {reading.name:<28} {reading.rms:.4f}"
              f"   {reading.frames} frames{note}")

    if result.readings:
        noise = next((r.rms for r in result.readings if r.name == "room noise"), 0)
        voice = next((r.rms for r in result.readings if r.name == "your voice"), 0)
        if noise > 0 and voice > 0:
            print(f"\n    signal to noise              {voice / noise:.1f}x"
                  f"   (4x is the minimum worth tuning)")

    if result.settings:
        print("\n  concluded")
        for key, value in settings_from(result).get("audio", {}).items():
            print(f"    audio.{key:<26} {value}")

    for warning in result.warnings:
        print(f"\n  ! {warning}")


def write_local(settings: dict, path=None) -> str:
    """Merge into config.local.yaml without discarding what is already there."""
    import pathlib

    import yaml

    target = pathlib.Path(path) if path else ROOT_LOCAL
    existing = {}
    if target.exists():
        try:
            existing = yaml.safe_load(target.read_text()) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"{target} is not valid YAML ({exc}) - not touching it")

    for section, values in settings.items():
        current = existing.get(section)
        if isinstance(current, dict) and isinstance(values, dict):
            for key, value in values.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value
        else:
            existing[section] = values

    header = (
        "# Written by `python -m erebus calibrate`. Every number here was\n"
        "# measured in your room - edit freely, or re-run to measure again.\n"
    )
    target.write_text(header + yaml.safe_dump(existing, sort_keys=False))
    return str(target)


import pathlib as _pathlib  # noqa: E402

ROOT_LOCAL = _pathlib.Path(__file__).resolve().parents[1] / "config.local.yaml"
