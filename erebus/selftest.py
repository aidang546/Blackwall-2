"""Prove the things doctor can only assume.

`doctor` checks that parts are *present*: pycaw imports, a voice file exists,
a port is free. That is the right check to run when something is broken, and it
is not the same question as "does this work on this machine". Half of Erebus is
Windows-only code that has never executed anywhere but a Windows box - the
volume interface, the DPAPI seal, the app launcher - and "pycaw is installed"
says nothing about whether COM initialises on a worker thread.

So this one *runs* things. Every probe is either non-destructive or restores
what it changed, and the ones that cannot be (shutdown, sleep, lock) are
reported as wired-but-unexercised rather than being tried.

    python -m erebus selftest

It is slower than doctor - it loads models and records audio - and it is meant
to be run once after installing, not when something is on fire.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from dataclasses import dataclass

log = logging.getLogger("erebus.selftest")

PASS, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"

#: Handlers that would do something you cannot undo. Being in this list is not
#: a gap in coverage - it is the reason the list exists.
DESTRUCTIVE = {"shutdown", "restart", "sleep", "lock", "abort_shutdown"}


@dataclass
class Probe:
    name: str
    state: str
    detail: str = ""
    fix: str = ""


async def _guarded(name: str, thunk) -> list[Probe]:
    """Run one probe; a crash becomes a finding rather than ending the run."""
    try:
        result = thunk()
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, Probe):
            return [result]
        return list(result or [])
    except Exception as exc:  # noqa: BLE001 - the whole point is to survive these
        log.debug("%s raised", name, exc_info=True)
        return [Probe(name, FAIL, f"{type(exc).__name__}: {exc}")]


# --------------------------------------------------------------------------
#  Windows control surfaces
# --------------------------------------------------------------------------

def probe_volume() -> list[Probe]:
    """Read the volume, move it, put it back.

    This is the probe that matters most. The volume path runs inside a thread
    pool, COM has to be initialised per thread, and getting that wrong fails
    only on the first real call - which is to say, never during development on
    a machine that has no audio endpoint at all.
    """
    from .actions.system import IS_WINDOWS, get_volume, set_volume

    if not IS_WINDOWS:
        return [Probe("volume", SKIP, f"Windows only (this is {sys.platform})")]

    before = get_volume()
    if before is None:
        return [Probe("volume", FAIL, "no audio endpoint",
                      "pip install pycaw comtypes, then re-run.")]

    target = 40 if before > 50 else 60
    set_volume(target)
    after = get_volume()
    set_volume(before)
    restored = get_volume()

    if after is None or abs(after - target) > 2:
        return [Probe("volume", FAIL, f"set {target}, read back {after}",
                      "The endpoint accepted the call but did not move.")]
    if restored is None or abs(restored - before) > 2:
        return [Probe("volume", WARN, f"moved fine, but restore left it at {restored}",
                      f"It was {before} before this ran.")]
    return [Probe("volume", PASS, f"read {before}, set {target}, restored")]


async def probe_worker_volume() -> list[Probe]:
    """The same call from a thread pool, which is where it really runs.

    Every handler is dispatched through run_in_executor, so the volume
    interface is touched from a worker thread rather than the main one. COM
    demands per-thread initialisation, and the gap between this probe and the
    one above is exactly the bug that produces "CoInitialize has not been
    called" on the very first spoken "volume up".
    """
    from .actions.system import IS_WINDOWS, get_volume

    if not IS_WINDOWS:
        return [Probe("volume via worker thread", SKIP, "Windows only")]
    loop = asyncio.get_running_loop()
    value = await loop.run_in_executor(None, get_volume)
    if value is None:
        return [Probe("volume via worker thread", FAIL,
                      "worked on the main thread, failed on a worker",
                      "This is the COM initialisation path - every spoken "
                      "'volume up' goes through it.")]
    return [Probe("volume via worker thread", PASS, f"read {value}")]


def probe_handlers(config) -> list[Probe]:
    """Every system action is registered and callable - without calling the
    ones that would turn the machine off."""
    from .actions.system import HANDLERS

    configured = list((config.section("actions").get("system") or {}).keys())
    missing = [name for name in configured if name not in HANDLERS]
    out = []
    if missing:
        out.append(Probe("system actions", FAIL,
                         f"configured but not implemented: {', '.join(missing)}",
                         "Remove them from config.yaml or they will silently "
                         "do nothing."))
    else:
        held = sorted(set(configured) & DESTRUCTIVE)
        out.append(Probe("system actions", PASS,
                         f"{len(configured)} wired"
                         + (f", {len(held)} not exercised: {', '.join(held)}" if held else "")))
    return out


def probe_apps(config) -> list[Probe]:
    """Do the configured launch commands point at anything real?

    A `run:` line that names a program you never installed fails at the moment
    you say its name, with the failure buried in a log. Checking a bare
    executable is `where`; checking `start spotify:` means asking the registry
    whether anything claims that protocol.
    """
    apps = config.section("actions").get("apps") or {}
    if not apps:
        return [Probe("apps", WARN, "none configured")]

    found, unknown, unchecked = [], [], []
    for name, entry in apps.items():
        command = str((entry or {}).get("run", "")).strip()
        target = _launch_target(command)
        if target is None:
            unchecked.append(name)
        elif _resolves(target):
            found.append(name)
        else:
            unknown.append(f"{name} ({target})")

    out = [Probe("apps", PASS if not unknown else WARN,
                 f"{len(found)}/{len(apps)} resolve"
                 + (f", {len(unchecked)} not checkable" if unchecked else ""))]
    if unknown:
        out.append(Probe("  not found", WARN, ", ".join(unknown),
                         "Either install them or edit the `run:` lines in "
                         "config.yaml - saying their name will do nothing."))
    return out


def _launch_target(command: str) -> str | None:
    """The thing a `run:` line actually invokes, or None if we cannot tell.

    Splitting on whitespace is wrong on the platform this targets: half of
    Windows lives under `C:\\Program Files`, and a bare path to it would come
    back as `C:\\Program`. Quoted arguments are respected, and an unquoted
    string that names a real file is taken whole.
    """
    import os
    import shlex

    command = command.strip()
    if not command:
        return None
    # An unquoted path with spaces in it, which no tokeniser can distinguish
    # from a command plus arguments without asking the filesystem.
    if os.path.exists(command):
        return command
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:              # an unbalanced quote
        parts = command.split()
    parts = [p.strip('"') for p in parts if p.strip('"')]
    if not parts:
        return None
    if parts[0].lower() == "start":
        return parts[1] if len(parts) > 1 else None
    return parts[0]


def _resolves(target: str) -> bool:
    """Is this an executable on PATH, or a registered URI scheme?"""
    if ":" in target and not target[1:2] == ":":       # spotify:, steam://
        scheme = target.split(":", 1)[0]
        if sys.platform != "win32":
            return False
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
                winreg.QueryValueEx(key, "URL Protocol")
            return True
        except OSError:
            return False
    return shutil.which(target) is not None


def probe_hotkey(config) -> list[Probe]:
    """Register the real combos, then let them go.

    Another program owning the combo is the common failure and it is silent:
    Erebus starts, the key does nothing, and nothing anywhere says why.
    """
    from .hotkey import IS_WINDOWS, Hotkeys, HotkeyError

    if not config.get("hotkey.enabled", True):
        return [Probe("hotkey", SKIP, "disabled in config")]
    combos = [config.get(f"hotkey.{k}") for k in ("talk", "interrupt")]
    combos = [c for c in combos if c]
    if not combos:
        return [Probe("hotkey", WARN, "none configured")]
    if not IS_WINDOWS:
        return [Probe("hotkey", SKIP, f"Windows only ({', '.join(combos)})")]

    keys = Hotkeys(asyncio.get_event_loop())
    try:
        for combo in combos:
            keys.bind(combo, lambda: None)
    except HotkeyError as exc:
        return [Probe("hotkey", FAIL, str(exc))]
    keys.start()
    active = list(keys.active)
    keys.stop()

    taken = [c for c in combos if c not in active]
    if taken:
        return [Probe("hotkey", FAIL,
                      f"{', '.join(taken)} already owned by another program",
                      "Pick a different combo under `hotkey:` in "
                      "config.local.yaml.")]
    return [Probe("hotkey", PASS, f"registered and released: {', '.join(active)}")]


# --------------------------------------------------------------------------
#  The pipeline, end to end and timed
# --------------------------------------------------------------------------

def probe_vault(config) -> list[Probe]:
    """Seal a throwaway key and open it again.

    The vault protects case files with a key sealed by DPAPI, which is bound to
    this Windows account. If the seal works but the unseal does not, you find
    out when you next try to read a case - by which time the plaintext is gone.
    """
    from .opsec.vault import CRYPTO_AVAILABLE, Vault

    if not CRYPTO_AVAILABLE:
        return [Probe("vault", SKIP, "cryptography not installed")]
    if not config.get("opsec.vault.enabled", False):
        return [Probe("vault", SKIP, "disabled - case files are plaintext")]

    vault = Vault(enabled=True)
    if not vault.ready:
        return [Probe("vault", FAIL, "enabled but not ready",
                      "The key could not be created or unsealed.")]
    secret = b"selftest - not a real secret"
    opened = vault.decrypt(vault.encrypt(secret))
    if opened != secret:
        return [Probe("vault", FAIL, "a sealed value did not come back intact",
                      "Do not put anything in the vault until this passes.")]
    detail = "seal and unseal round-trip"
    if sys.platform == "win32":
        detail += ", key sealed to this Windows account"
    return [Probe("vault", PASS, detail)]


async def probe_voice(config) -> list[Probe]:
    """Say something, hear it back, and route it - with the clock running.

    This is the one probe that exercises the whole chain the way a real turn
    does. The timings matter as much as the pass: a reply that takes eight
    seconds to start is technically working and practically unusable.
    """
    from .actions.registry import Registry
    from .pipeline.stt import STT_AVAILABLE, Transcriber
    from .pipeline.tts import Speaker

    out: list[Probe] = []
    speaker = Speaker(
        backend=config.get("tts.backend", "piper"),
        voice=config.get("tts.voice") or "en_GB-alan-medium",
        effects=config.section("tts").get("effects", {}),
        rate=float(config.get("tts.rate", 1.0)),
    )
    if not speaker.load():
        return [Probe("voice", FAIL, "no voice model loaded",
                      "python -m erebus fetch-voice en_GB-alan-medium")]

    phrase = "Lock the computer."
    started = time.perf_counter()
    audio, sample_rate = await speaker.synthesize(phrase)
    synth_ms = (time.perf_counter() - started) * 1000
    seconds = len(audio) / sample_rate
    out.append(Probe("speech out", PASS,
                     f"{synth_ms:.0f} ms to synthesise {seconds:.1f}s "
                     f"({seconds * 1000 / max(synth_ms, 1):.0f}x real time)"))

    if not STT_AVAILABLE:
        out.append(Probe("speech in", SKIP, "faster-whisper not installed"))
        return out

    transcriber = Transcriber(
        model=config.get("stt.model", "small.en"),
        device=config.get("stt.device", "cuda"),
        compute_type=config.get("stt.compute_type", "float16"),
    )
    started = time.perf_counter()
    # load() is synchronous and can take seconds; keep the loop free so a
    # slow first model download does not look like a hang.
    loop = asyncio.get_running_loop()
    loaded = await loop.run_in_executor(None, transcriber.load)
    load_s = time.perf_counter() - started
    if not loaded:
        out.append(Probe("speech in", FAIL, "the model would not load",
                         "python -m erebus doctor says which piece is missing."))
        return out
    # `device` is rewritten to "cpu" by the fallback inside load(), so this
    # reports where it actually ended up rather than what was asked for.
    on_cpu = transcriber.device == "cpu"
    out.append(Probe("speech in", WARN if on_cpu else PASS,
                     f"{transcriber.model_name} on {transcriber.device} in {load_s:.1f}s",
                     "It fell back to CPU - that is the single biggest thing "
                     "slowing replies down. docs/SETUP.md step 3." if on_cpu else ""))

    started = time.perf_counter()
    heard = await transcriber.transcribe(_to_16k(audio, sample_rate))
    listen_ms = (time.perf_counter() - started) * 1000
    ratio = seconds * 1000 / max(listen_ms, 1)
    state = PASS if ratio > 1.5 else WARN
    out.append(Probe("transcription speed", state,
                     f"{listen_ms:.0f} ms for {seconds:.1f}s of audio ({ratio:.1f}x)",
                     "" if state == PASS else
                     "Slower than real time. Check the startup log for "
                     "'retrying on CPU', or drop stt.model to base.en."))

    registry = Registry(config)
    match = registry.match(heard or "")
    if match is None:
        out.append(Probe("round trip", FAIL,
                         f'said {phrase!r}, heard {(heard or "")!r}, matched nothing',
                         "The three stages are not agreeing. Check the "
                         "microphone sample rate and tts.effects."))
    else:
        out.append(Probe("round trip", PASS,
                         f'{phrase!r} -> {(heard or "").strip()!r} -> {match.action.name}'))
    return out


def _to_16k(audio, sample_rate: int):
    """Whisper wants 16 kHz; Piper gives 22.05 kHz.

    Feeding it the wrong rate does not error - it transcribes a chipmunk and
    the round trip fails for a reason that looks like a model problem.
    """
    import numpy as np

    if sample_rate == 16000:
        return np.asarray(audio, dtype=np.float32)
    target = int(len(audio) * 16000 / sample_rate)
    index = np.linspace(0, len(audio) - 1, target)
    low = np.floor(index).astype(np.int32)
    high = np.clip(low + 1, 0, len(audio) - 1)
    frac = (index - low).astype(np.float32)
    return (np.asarray(audio)[low] * (1 - frac)
            + np.asarray(audio)[high] * frac).astype(np.float32)


async def probe_audio_devices(config) -> list[Probe]:
    """Open the input for real, and see whether anything arrives."""
    from .pipeline import audio as audio_mod

    if not audio_mod.AUDIO_AVAILABLE:
        return [Probe("microphone", FAIL, "sounddevice not installed")]

    device = config.get("audio.input_device")
    sample_rate = int(config.get("audio.sample_rate", 16000))
    cfg = audio_mod.AudioConfig(sample_rate=sample_rate, device=device)
    mic = audio_mod.Microphone(cfg)
    mic.start()
    levels = []
    try:
        deadline = asyncio.get_running_loop().time() + 1.2
        async for frame in mic.frames():
            levels.append(audio_mod.rms(frame))
            if asyncio.get_running_loop().time() >= deadline:
                break
    finally:
        mic.stop()

    if not levels:
        return [Probe("microphone", FAIL, "opened, but no frames arrived",
                      "The device is there and delivering nothing.")]
    peak = max(levels)
    gate = float(config.get("audio.silence_threshold", 0.012))
    if peak < 1e-5:
        return [Probe("microphone", FAIL, f"{len(levels)} frames, all digital silence",
                      "Muted, or the wrong input. python -m erebus calibrate")]
    state = PASS if peak > gate * 0.5 else WARN
    return [Probe("microphone", state,
                  f"{len(levels)} frames, peak {peak:.4f} against a {gate} gate",
                  "" if state == PASS else
                  "Very quiet. If that was you speaking, run "
                  "python -m erebus calibrate.")]


# --------------------------------------------------------------------------
#  Runner
# --------------------------------------------------------------------------

async def run(config) -> int:
    print("\n  Running things rather than checking they are installed.")
    print("  Volume is moved and put back; nothing else is changed.\n")

    probes: list[Probe] = []
    probes += await _guarded("volume", probe_volume)
    probes += await _guarded("volume via worker thread", probe_worker_volume)
    probes += await _guarded("system actions", lambda: probe_handlers(config))
    probes += await _guarded("apps", lambda: probe_apps(config))
    probes += await _guarded("hotkey", lambda: probe_hotkey(config))
    probes += await _guarded("microphone", lambda: probe_audio_devices(config))
    probes += await _guarded("vault", lambda: probe_vault(config))
    probes += await _guarded("voice", lambda: probe_voice(config))

    for probe in probes:
        mark = {PASS: "  ok  ", WARN: "  warn", FAIL: "  FAIL", SKIP: "  --  "}[probe.state]
        print(f"{mark}  {probe.name:<26} {probe.detail}")
        if probe.fix:
            print(f"          {'':<26} -> {probe.fix}")

    failed = [p for p in probes if p.state == FAIL]
    warned = [p for p in probes if p.state == WARN]
    skipped = [p for p in probes if p.state == SKIP]
    print()
    if failed:
        print(f"  {len(failed)} broken, {len(warned)} worth a look, "
              f"{len(skipped)} not applicable here.\n")
        return 1
    print(f"  Everything exercised works. {len(warned)} worth a look, "
          f"{len(skipped)} not applicable here.")
    print("  Note that shutdown, restart, sleep and lock are wired but were")
    print("  deliberately not run.\n")
    return 0
