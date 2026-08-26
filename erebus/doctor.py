"""One command that tells you what is wrong and what to type to fix it.

A first run touches a dozen things that can each fail independently - a missing
wheel, an undownloaded voice, Ollama not started, the wrong Python. Hitting
those one at a time, each as a different error deep in a log, is the slowest
possible way to find out. This checks them all at once and prints the fix next
to each failure.

Nothing here changes anything. It only looks.
"""

from __future__ import annotations

import importlib
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS, WARN, FAIL = "ok", "warn", "FAIL"


@dataclass
class Check:
    name: str
    state: str
    detail: str = ""
    fix: str = ""


def _module(name: str):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - a broken backend raises more than ImportError
        return None


def check_python() -> Check:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 10):
        return Check("python", FAIL, version, "Erebus needs Python 3.11 or 3.12.")
    if (major, minor) >= (3, 13):
        return Check("python", WARN, version,
                     "Several audio wheels have no 3.13 build yet. 3.12 is safer.")
    return Check("python", PASS, version)


def check_core() -> list[Check]:
    out = []
    for module, label in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                          ("yaml", "pyyaml"), ("httpx", "httpx")):
        found = _module(module)
        out.append(Check(
            label, PASS if found else FAIL,
            "" if found else "not installed",
            "" if found else "pip install -r requirements.txt",
        ))
    return out


def check_config() -> Check:
    try:
        from .core.config import Config

        config = Config.load()
        actions = len(config.get("actions.apps") or {})
        return Check("config", PASS, f"{actions} apps configured")
    except Exception as exc:  # noqa: BLE001
        return Check("config", FAIL, f"{type(exc).__name__}: {exc}",
                     "Check config.yaml and config.local.yaml parse as YAML.")


def check_port(port: int) -> Check:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        busy = probe.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        return Check("port", WARN, f"{port} already in use",
                     "Erebus may already be running, or change server.port.")
    return Check("port", PASS, f"{port} free")


def check_microphone() -> list[Check]:
    from .pipeline import audio as audio_mod

    if not audio_mod.AUDIO_AVAILABLE:
        return [Check("microphone", FAIL, "sounddevice not available",
                      "pip install -r requirements-voice.txt")]
    try:
        devices = audio_mod.list_devices()
    except Exception as exc:  # noqa: BLE001
        return [Check("microphone", FAIL, f"{type(exc).__name__}: {exc}",
                      "Check that an input device exists and is not in use.")]
    if not devices:
        return [Check("microphone", FAIL, "no input devices",
                      "Plug in a microphone, then: python -m erebus devices")]
    return [Check("microphone", PASS,
                  f"{len(devices)} input(s), default: {devices[0]['name'][:40]}")]


def check_stt() -> Check:
    from .pipeline.stt import STT_AVAILABLE

    if not STT_AVAILABLE:
        return Check("speech in", FAIL, "faster-whisper not installed",
                     "pip install -r requirements-voice.txt")

    # Report whether CUDA will actually be used, without loading a model -
    # loading one here would make `doctor` take a minute.
    cuda = _module("ctranslate2")
    detail = "faster-whisper installed"
    if cuda is not None:
        try:
            count = cuda.get_cuda_device_count()
            if count:
                return Check("speech in", PASS, f"{detail}, {count} CUDA device(s)")
            return Check("speech in", WARN, f"{detail}, no CUDA device",
                         "It will run on CPU. For GPU: pip install "
                         "nvidia-cublas-cu12 nvidia-cudnn-cu12")
        except Exception:  # noqa: BLE001
            pass
    return Check("speech in", PASS, detail)


def check_tts() -> list[Check]:
    from .pipeline.tts import PIPER_AVAILABLE, MODELS_DIR

    out = []
    if not PIPER_AVAILABLE:
        out.append(Check("speech out", FAIL, "piper-tts not installed",
                         "pip install -r requirements-voice.txt"))
        return out
    out.append(Check("speech out", PASS, "piper-tts installed"))

    voices = sorted(MODELS_DIR.glob("*.onnx")) if MODELS_DIR.exists() else []
    if not voices:
        out.append(Check("voice", FAIL, "no voice downloaded",
                         "python -m erebus fetch-voice en_GB-alan-medium"))
    else:
        # A truncated download is the classic failure and only shows up later
        # as an opaque protobuf error, so check the size looks sane.
        small = [v.name for v in voices if v.stat().st_size < 10_000_000]
        if small:
            out.append(Check("voice", FAIL, f"truncated: {', '.join(small)}",
                             "Delete it from models/ and re-run fetch-voice."))
        else:
            out.append(Check("voice", PASS,
                             f"{len(voices)}: {', '.join(v.stem for v in voices[:3])}"))
    return out


def check_wake() -> Check:
    from .pipeline.wake import WAKE_AVAILABLE

    if not WAKE_AVAILABLE:
        return Check("wake word", WARN, "openwakeword not installed",
                     "Push-to-talk still works. pip install -r requirements-voice.txt")
    module = _module("openwakeword")
    if module is None:
        return Check("wake word", WARN, "openwakeword will not import", "")
    models = Path(module.__file__).parent / "resources" / "models"
    onnx = list(models.glob("*.onnx")) if models.exists() else []
    if not onnx:
        return Check("wake word", WARN, "models not downloaded yet",
                     "Erebus fetches them automatically on first run.")
    return Check("wake word", PASS, f"{len(onnx)} models available")


async def check_brain(config) -> list[Check]:
    from .pipeline.brain import Brain

    host = config.get("brain.host", "http://127.0.0.1:11434")
    model = config.get("brain.model", "llama3.1:8b")
    brain = Brain(host=host, model=model)
    ready = await brain.load()
    await brain.close()

    if ready:
        return [Check("brain", PASS, f"{model} via ollama")]

    # Distinguish "not running" from "model not pulled" - different fixes.
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            await client.get(f"{host}/api/tags")
        return [Check("brain", FAIL, f"ollama running, {model} not pulled",
                      f"ollama pull {model}")]
    except Exception:  # noqa: BLE001
        return [Check("brain", FAIL, f"ollama not reachable at {host}",
                      "Start it: ollama serve   (or install: winget install Ollama.Ollama)")]


def check_vault(config) -> Check:
    from .opsec.vault import CRYPTO_AVAILABLE

    enabled = config.get("opsec.vault.enabled", False)
    if not enabled:
        return Check("vault", WARN, "disabled - personal files are plaintext",
                     "Optional. opsec.vault.enabled: true, then "
                     "python -m erebus vault --vault encrypt")
    if not CRYPTO_AVAILABLE:
        return Check("vault", FAIL, "enabled but the crypto backend is broken",
                     "pip install --force-reinstall cffi cryptography")
    return Check("vault", PASS, "enabled and working")


def check_profile() -> Check:
    from .briefing.profile import Profile

    profile = Profile.load()
    if not profile.configured:
        return Check("profile", WARN, "not filled in - briefings will be generic",
                     "copy profile.example.yaml profile.local.yaml, then edit it")
    return Check("profile", PASS, f"configured for {profile.name}")


def check_windows() -> list[Check]:
    if sys.platform != "win32":
        return [Check("system control", WARN, f"not Windows ({sys.platform})",
                      "Volume, media keys and lock are Windows-only and will "
                      "no-op here.")]
    out = []
    if _module("pycaw") is None:
        out.append(Check("volume control", FAIL, "pycaw not installed",
                         "pip install -r requirements-voice.txt"))
    else:
        out.append(Check("volume control", PASS, "pycaw available"))
    if shutil.which("powershell") is None:
        out.append(Check("powershell", WARN, "not on PATH",
                         "Needed only for the SAPI voice fallback."))
    return out


async def run() -> int:
    from .core.config import Config

    config = Config.load()
    checks: list[Check] = [check_python()]
    checks += check_core()
    checks.append(check_config())
    checks.append(check_port(int(config.get("server.port", 8848))))
    checks += check_microphone()
    checks.append(check_stt())
    checks += check_tts()
    checks.append(check_wake())
    checks += await check_brain(config)
    checks.append(check_vault(config))
    checks.append(check_profile())
    checks += check_windows()

    print()
    for check in checks:
        mark = {PASS: "  ok  ", WARN: "  warn", FAIL: "  FAIL"}[check.state]
        print(f"{mark}  {check.name:<16} {check.detail}")
        if check.fix:
            print(f"          {'':<16} -> {check.fix}")

    failed = [c for c in checks if c.state == FAIL]
    warned = [c for c in checks if c.state == WARN]
    print()
    if failed:
        print(f"  {len(failed)} blocking, {len(warned)} optional.\n")
        print("  Erebus will still start - every stage degrades rather than")
        print("  refusing to run - but the failures above are switched off.\n")
        return 1
    if warned:
        print(f"  Everything essential works. {len(warned)} optional item(s) above.\n")
        return 0
    print("  Everything works.\n")
    return 0
