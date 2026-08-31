"""One command that does the install, and stops at the first thing that needs you.

docs/SETUP.md is eleven steps. Most are `pip install` and waiting, two need a
package manager, and the order matters in ways that are not obvious - installing
CUDA support after Whisper has already cached a CPU model, say. Doing them by
hand at the end of a working day is how a twenty-minute setup becomes an
evening.

This runs the ones that can be automated and tells you exactly what to type for
the ones that cannot. It is safe to re-run: every step checks whether it is
already done first, so a failed run continues rather than starting over.

    python install.py            do it
    python install.py --check    say what it would do, change nothing

Deliberately depends on nothing but the standard library, because it has to run
before anything is installed.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
IS_WINDOWS = os.name == "nt"

#: Where pip and python live inside the venv, which differs by platform.
BIN = VENV / ("Scripts" if IS_WINDOWS else "bin")
PYTHON = BIN / ("python.exe" if IS_WINDOWS else "python")

DEFAULT_VOICE = "en_GB-alan-medium"
DEFAULT_MODEL = "llama3.1:8b"


class Stop(Exception):
    """Something needs a human. The message says what to type."""


def say(message: str = "") -> None:
    print(message, flush=True)


def step(number: int, title: str) -> None:
    say(f"\n  [{number}] {title}")


def run(command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit {result.returncode}"
        raise Stop(f"{' '.join(command[:3])}... failed: {detail}")
    return result


# --------------------------------------------------------------------------

def check_python() -> str:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 11):
        raise Stop(
            f"This is Python {version}. Erebus needs 3.11 or 3.12.\n"
            "      winget install Python.Python.3.12"
        )
    if (major, minor) >= (3, 13):
        say(f"      Python {version}. Several audio wheels have no 3.13 build")
        say("      yet - if the voice install fails, that is why. 3.12 is safer.")
    return version


def make_venv(dry: bool) -> str:
    if PYTHON.exists():
        return "already there"
    if dry:
        return "would create .venv"
    run([sys.executable, "-m", "venv", str(VENV)])
    return "created"


def pip_install(args: list[str], dry: bool) -> str:
    if dry:
        return f"would pip install {' '.join(args)}"
    run([str(PYTHON), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
    run([str(PYTHON), "-m", "pip", "install", "--quiet", *args])
    return "installed"


def installed(module: str) -> bool:
    result = subprocess.run([str(PYTHON), "-c", f"import {module}"],
                            capture_output=True)
    return result.returncode == 0


def has_nvidia() -> bool:
    """An NVIDIA GPU worth installing cuBLAS and cuDNN for."""
    if shutil.which("nvidia-smi") is None:
        return False
    return subprocess.run(["nvidia-smi"], capture_output=True).returncode == 0


def fetch_voice(dry: bool) -> str:
    models = ROOT / "models"
    if models.exists() and any(models.glob("*.onnx")):
        return "already downloaded"
    if dry:
        return f"would download {DEFAULT_VOICE}"
    run([str(PYTHON), "-m", "erebus", "fetch-voice", DEFAULT_VOICE])
    return f"{DEFAULT_VOICE} downloaded"


def ollama_running() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2):
            return True
    except Exception:  # noqa: BLE001 - not running, not installed, no network
        return False


def ollama_has_model(name: str) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as f:
            return name.split(":")[0] in f.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False


def setup_ollama(dry: bool) -> str:
    """The brain. Installing it is a package manager's job, not this script's."""
    if ollama_running():
        if ollama_has_model(DEFAULT_MODEL):
            return f"running, {DEFAULT_MODEL} present"
        if dry:
            return f"running; would pull {DEFAULT_MODEL}"
        say(f"      pulling {DEFAULT_MODEL} - this is several GB, give it a while")
        run(["ollama", "pull", DEFAULT_MODEL])
        return f"{DEFAULT_MODEL} pulled"

    if shutil.which("ollama") is None:
        raise Stop(
            "Ollama is not installed. It is the only piece this cannot do for you:\n"
            "      winget install Ollama.Ollama\n"
            "      ...then run this script again. Everything else is done."
        )
    raise Stop(
        "Ollama is installed but not answering on 127.0.0.1:11434.\n"
        "      Start it, then run this script again:  ollama serve"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install Erebus")
    parser.add_argument("--check", action="store_true",
                        help="say what would happen, change nothing")
    parser.add_argument("--no-gpu", action="store_true",
                        help="skip the CUDA libraries even if a GPU is present")
    args = parser.parse_args(argv)
    dry = args.check

    say("\n  Erebus install" + ("  (check only, nothing will change)" if dry else ""))
    say(f"  {platform.system()} {platform.release()}, {ROOT}")

    try:
        step(1, "Python")
        say(f"      {check_python()}")

        step(2, "Virtual environment")
        say(f"      {make_venv(dry)}")
        if dry and not PYTHON.exists():
            say("\n  Stopping here: the rest needs the venv that --check did not create.\n")
            return 0

        step(3, "Core packages")
        say(f"      {'already installed' if installed('fastapi') else pip_install(['-r', 'requirements.txt'], dry)}")

        step(4, "Voice packages")
        say(f"      {'already installed' if installed('sounddevice') else pip_install(['-r', 'requirements-voice.txt'], dry)}")

        step(5, "GPU acceleration")
        if args.no_gpu:
            say("      skipped (--no-gpu)")
        elif not has_nvidia():
            say("      no NVIDIA GPU found - Whisper will run on CPU.")
            say("      Usable, but set stt.model: base.en if replies feel slow.")
        elif installed("nvidia.cublas"):
            say("      cuBLAS and cuDNN already installed")
        else:
            say(f"      {pip_install(['nvidia-cublas-cu12', 'nvidia-cudnn-cu12'], dry)}")

        step(6, "Voice model")
        say(f"      {fetch_voice(dry)}")

        step(7, "The brain")
        say(f"      {setup_ollama(dry)}")

    except Stop as exc:
        say(f"\n  Stopped: {exc}\n")
        return 1
    except KeyboardInterrupt:
        say("\n  Interrupted. Re-run when ready - it picks up where it left off.\n")
        return 130

    if dry:
        say("\n  That is everything --check can tell you. Re-run without it.\n")
        return 0

    say("\n  Installed. Two things left, both of which need you at the machine:\n")
    say(f"      {PYTHON} -m erebus doctor      what is missing, if anything")
    say(f"      {PYTHON} -m erebus calibrate   measures your room, ~30 seconds")
    say(f"      {PYTHON} -m erebus selftest    proves it works, ~1 minute")
    say(f"\n  Then:  {PYTHON} -m erebus")
    say("  and ctrl+alt+space from any window.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
