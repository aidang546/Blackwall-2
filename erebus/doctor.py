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
    # 3.11, not 3.10: asyncio.timeout() is used in the run loop and the tests.
    if (major, minor) < (3, 11):
        return Check("python", FAIL, version, "Erebus needs Python 3.11 or 3.12.")
    if (major, minor) >= (3, 13):
        return Check("python", WARN, version,
                     "Several audio wheels have no 3.13 build yet. 3.12 is safer.")
    return Check("python", PASS, version)


def check_core() -> list[Check]:
    """The packages the server and the CLI need.

    These used to be imported at the top of __main__, which meant a machine
    missing them never reached this report - it got ModuleNotFoundError first.
    They are now imported inside `run()`, so doctor can actually say which one
    is absent.
    """
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


def check_config(config) -> Check:
    """Whether the config loaded, using the one main() already tried to load.

    Reloading here would raise the same exception that main() caught, so the
    error it recorded is passed through instead.
    """
    error = getattr(config, "load_error", None)
    if error:
        return Check("config", FAIL, error,
                     "Fix the YAML in config.yaml / config.local.yaml.")
    try:
        actions = len(config.get("actions.apps") or {})
    except Exception as exc:  # noqa: BLE001
        return Check("config", FAIL, f"{type(exc).__name__}: {exc}",
                     "Check config.yaml and config.local.yaml parse as YAML.")
    return Check("config", PASS, f"{actions} apps configured")


def check_port(port: int) -> Check:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        busy = probe.connect_ex(("127.0.0.1", port)) == 0
    if busy:
        return Check("port", WARN, f"{port} already in use",
                     "Erebus may already be running, or change server.port.")
    return Check("port", PASS, f"{port} free")


def check_microphone(config) -> list[Check]:
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

    out = [Check("microphone", PASS, f"{len(devices)} input(s) available")]

    # A pinned index that no longer exists fails much later, at stream open,
    # with an error that does not mention the config.
    pinned = config.get("audio.input_device")
    if pinned is not None:
        known = {d["index"] for d in devices}
        if isinstance(pinned, int) and pinned not in known:
            out.append(Check("input device", FAIL,
                             f"audio.input_device is {pinned}, which no longer exists",
                             "python -m erebus devices, then update "
                             "config.local.yaml"))
        else:
            match = next((d for d in devices if d["index"] == pinned), None)
            out.append(Check("input device", PASS,
                             f"{pinned}: {match['name'][:40]}" if match else str(pinned)))
    return out


def check_stt() -> Check:
    from .pipeline.stt import PYAV_BLOCKED, STT_AVAILABLE, STT_IMPORT_ERROR

    if not STT_AVAILABLE:
        if STT_IMPORT_ERROR:
            # Say what actually went wrong. "not installed" sent the operator
            # to reinstall a package that was installed the whole time.
            return Check("speech in", FAIL,
                         f"faster-whisper will not import - {STT_IMPORT_ERROR}",
                         "If that names a DLL or a policy, it is the operating "
                         "system blocking a bundled library, not a missing "
                         "package.")
        return Check("speech in", FAIL, "faster-whisper not installed",
                     "pip install -r requirements-voice.txt")
    if PYAV_BLOCKED:
        # Worth saying out loud rather than leaving in a log: the operator sees
        # a security prompt or a blocked file and needs to know it was handled.
        return Check("speech in", PASS,
                     "faster-whisper installed (PyAV is blocked on this machine "
                     "and was routed around)",
                     "Nothing to do. PyAV only decodes audio files, which "
                     "Erebus does not use.")

    # Report whether CUDA will actually be used, without loading a model -
    # loading one here would make `doctor` take a minute.
    cuda = _module("ctranslate2")
    detail = "faster-whisper installed"
    if cuda is not None:
        try:
            count = cuda.get_cuda_device_count()
        except Exception:  # noqa: BLE001
            return Check("speech in", PASS, detail)

        if not count:
            return Check("speech in", WARN, f"{detail}, no CUDA device",
                         "It will run on CPU. For GPU: pip install "
                         "nvidia-cublas-cu12 nvidia-cudnn-cu12")

        # A visible device is not the same as a usable one: get_cuda_device_count
        # asks the driver, while CTranslate2 additionally needs cuBLAS and
        # cuDNN at load time. Actually proving it would mean downloading and
        # loading a model, which is too slow for a diagnostic - so say what was
        # and was not established rather than reporting a green tick for the
        # exact failure the docs warn about.
        libs = all(_module(m) is not None
                   for m in ("nvidia.cublas", "nvidia.cudnn"))
        if libs:
            return Check("speech in", PASS,
                         f"{detail}, {count} CUDA device(s), cuBLAS/cuDNN present")
        return Check("speech in", WARN,
                     f"{detail}, {count} CUDA device(s), cuBLAS/cuDNN not found",
                     "May still work if they are installed system-wide. The "
                     "startup log says which it used: 'ready on cuda' or "
                     "'retrying on CPU'.")
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


def check_wake(config) -> Check:
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
    # "N models available" is true and useless. What matters is which word
    # actually wakes it, and that is not the one in identity.name: openWakeWord
    # ships no "erebus" model, and measured against all six stock models the
    # word scores 0.000. Report the phrase the loaded model was trained on.
    if not config.get("wake.enabled", True):
        return Check("wake word", WARN, "disabled - hotkey and wall only",
                     "That is a valid setup. wake.enabled: true to turn it on.")
    model = str(config.get("wake.model", "hey_jarvis"))
    if not any(model in f.name for f in onnx):
        return Check("wake word", FAIL, f"{model!r} is not one of the installed models",
                     f"Installed: {', '.join(sorted(f.stem for f in onnx))}")
    spoken = model.replace("_", " ")
    return Check("wake word", WARN, f'responds to "{spoken}", not to "Erebus"',
                 "No stock model matches the name and four synthetic voices were "
                 "not enough to train one - see docs/WAKEWORD.md. Use the hotkey.")


def check_hotkey(config) -> Check:
    """The system-wide key is the one input path that does not depend on luck."""
    from .hotkey import IS_WINDOWS, HotkeyError, parse

    if not config.get("hotkey.enabled", True):
        return Check("hotkey", WARN, "disabled",
                     "hotkey.enabled: true gives you a key that works from any window.")
    combos = [(k, config.get(f"hotkey.{k}")) for k in ("talk", "interrupt")]
    combos = [(k, v) for k, v in combos if v]
    if not combos:
        return Check("hotkey", WARN, "none configured",
                     'Set hotkey.talk, e.g. "ctrl+alt+space".')
    for name, combo in combos:
        try:
            parse(combo)
        except HotkeyError as exc:
            return Check("hotkey", FAIL, f"hotkey.{name}: {exc}",
                         'Something like "ctrl+alt+space".')
    listed = ", ".join(v for _, v in combos)
    if not IS_WINDOWS:
        return Check("hotkey", WARN, f"{listed} - Windows only, inert here",
                     "Registers for real on the target machine.")
    return Check("hotkey", PASS, listed)


async def check_brain(config) -> list[Check]:
    from .pipeline.brain import Brain

    backend = config.get("brain.backend", "ollama")
    if backend != "ollama":
        # `backend: echo` is a documented, correct configuration - commands
        # only, no LLM. Reporting it as a blocking failure would mean a
        # properly configured machine exits non-zero.
        return [Check("brain", WARN, f"backend is {backend!r} - no LLM",
                      "Commands work; conversation and briefings do not. "
                      "Set brain.backend: ollama to enable them.")]

    host = config.get("brain.host", "http://127.0.0.1:11434")
    model = config.get("brain.model", "llama3.1:8b")
    brain = Brain(host=host, model=model)
    ready = await brain.load()
    await brain.close()

    if ready:
        return [Check("brain", PASS, f"{model} via ollama")]

    # Distinguish the cases, which need different fixes. Something listening on
    # the port is not the same as Ollama being there: check the status and that
    # the body looks like a model list, or a 500 gets reported as "not pulled"
    # with a useless `ollama pull` suggestion.
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{host.rstrip('/')}/api/tags")
    except Exception as exc:  # noqa: BLE001
        return [Check("brain", FAIL,
                      f"nothing reachable at {host} ({type(exc).__name__})",
                      "Start it: ollama serve   (or: winget install Ollama.Ollama)")]

    if response.status_code != 200:
        return [Check("brain", FAIL,
                      f"{host} answered HTTP {response.status_code}",
                      "Ollama is unhealthy. Restart it: ollama serve")]
    try:
        installed = {m["name"] for m in response.json().get("models", [])}
    except Exception:  # noqa: BLE001 - not JSON, or not Ollama's shape
        return [Check("brain", FAIL, f"{host} is not an Ollama server",
                      "Something else is on that port. Check brain.host.")]

    listed = ", ".join(sorted(installed)[:3]) or "none"
    return [Check("brain", FAIL,
                  f"ollama running, {model} not pulled (has: {listed})",
                  f"ollama pull {model}")]


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
        # "pycaw available" is not the same question as "the volume works" -
        # it reported green on a machine where reaching the endpoint raised.
        from .actions.system import VOLUME_ERROR, get_volume

        level = get_volume()
        if level is None:
            out.append(Check("volume control", FAIL,
                             f"pycaw installed but the endpoint failed"
                             + (f" - {VOLUME_ERROR}" if VOLUME_ERROR else ""),
                             "python -m erebus selftest exercises this properly."))
        else:
            out.append(Check("volume control", PASS, f"reads {level}"))

    cuda = _module("nvidia")
    if cuda is not None:
        from .pipeline.stt import CUDA_DLL_DIRS

        if CUDA_DLL_DIRS:
            out.append(Check("cuda libraries", PASS,
                             f"{len(CUDA_DLL_DIRS)} directories on the DLL path"))
        else:
            # Installed, and invisible to the loader: the exact shape of
            # "cublas64_12.dll is not found" on a machine that has cuBLAS.
            out.append(Check("cuda libraries", WARN,
                             "the nvidia packages are installed but no DLL "
                             "directory was found inside them",
                             "Whisper will fall back to CPU. Reinstall inside "
                             "the venv: pip install nvidia-cublas-cu12 "
                             "nvidia-cudnn-cu12"))
    if shutil.which("powershell") is None:
        out.append(Check("powershell", WARN, "not on PATH",
                         "Needed only for the SAPI voice fallback."))
    return out


async def _guarded(name: str, thunk) -> list[Check]:
    """Run one check, converting any failure into a finding.

    Checks import optional backends whose own guards catch only ImportError,
    while a half-installed native wheel raises OSError or worse. Without this,
    one broken DLL aborts the entire report - on precisely the machine that
    most needs to see the rest of it.
    """
    try:
        result = thunk()
        if hasattr(result, "__await__"):
            result = await result
        return result if isinstance(result, list) else [result]
    except Exception as exc:  # noqa: BLE001
        return [Check(name, FAIL, f"check itself failed: {type(exc).__name__}: {exc}",
                      "This is a bug in doctor, not necessarily in your setup.")]


async def run(config=None) -> int:
    from .core.config import Config

    if config is None:
        try:
            config = Config.load()
        except Exception as exc:  # noqa: BLE001
            config = Config({})
            config.load_error = f"{type(exc).__name__}: {exc}"

    checks: list[Check] = []
    checks += await _guarded("python", check_python)
    checks += await _guarded("core deps", check_core)
    checks += await _guarded("config", lambda: check_config(config))
    checks += await _guarded(
        "port", lambda: check_port(int(config.get("server.port", 8848) or 8848)))
    checks += await _guarded("microphone", lambda: check_microphone(config))
    checks += await _guarded("speech in", check_stt)
    checks += await _guarded("speech out", check_tts)
    checks += await _guarded("wake word", lambda: check_wake(config))
    checks += await _guarded("hotkey", lambda: check_hotkey(config))
    checks += await _guarded("brain", lambda: check_brain(config))
    checks += await _guarded("vault", lambda: check_vault(config))
    checks += await _guarded("profile", check_profile)
    checks += await _guarded("system control", check_windows)

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
