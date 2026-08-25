"""Text to speech, plus playback that drives the visualiser.

Playback is deliberately not fire-and-forget: it streams the audio out in small
blocks and reports the amplitude of each one, which is what makes the Blackwall
move in time with the voice instead of just flailing on a timer.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import shutil
import subprocess
import wave

from . import voicefx

log = logging.getLogger("erebus.tts")

# Synthesis needs numpy; playback additionally needs a sound card. Keeping the
# two apart is what lets `erebus say --out file.wav` work on a headless box.
try:  # pragma: no cover
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    NUMPY_AVAILABLE = False

try:  # pragma: no cover
    import sounddevice as sd

    PLAYBACK_AVAILABLE = True
except (ImportError, OSError):  # pragma: no cover
    # OSError: PortAudio present as a wheel but no audio device / no libportaudio.
    sd = None  # type: ignore[assignment]
    PLAYBACK_AVAILABLE = False

try:  # pragma: no cover
    from piper.config import SynthesisConfig
    from piper.voice import PiperVoice

    PIPER_AVAILABLE = True
except ImportError:  # pragma: no cover
    PiperVoice = None  # type: ignore[assignment]
    SynthesisConfig = None  # type: ignore[assignment]
    PIPER_AVAILABLE = False


#: Where downloaded voices live, relative to the repo root.
MODELS_DIR = pathlib.Path(__file__).resolve().parents[2] / "models"


def write_wav(path, audio, sample_rate: int) -> None:
    """Save float32 audio to a 16-bit WAV.

    Used by `erebus say --out`, which is how you audition the voice chain on a
    machine with no sound card.
    """
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def resolve_voice(name: str) -> pathlib.Path | None:
    """Find a voice on disk.

    Accepts a bare name ("en_GB-alan-medium"), a path to the .onnx, or a path
    without the extension - so config can stay readable regardless of how the
    file got there.
    """
    candidate = pathlib.Path(name)
    options = [
        candidate,
        candidate.with_suffix(".onnx"),
        MODELS_DIR / name,
        MODELS_DIR / f"{name}.onnx",
    ]
    for option in options:
        if option.is_file() and option.suffix == ".onnx":
            return option
    return None


def fetch_voice(name: str, dest: pathlib.Path | None = None) -> pathlib.Path:
    """Download a Piper voice, verifying we got all of it.

    Piper's own downloader writes a short file without complaining when a
    transfer is cut off, and the only symptom is an opaque protobuf error at
    load time. Checking Content-Length against what landed on disk turns that
    into an error you can act on.
    """
    import urllib.request

    dest = dest or MODELS_DIR
    dest.mkdir(parents=True, exist_ok=True)

    # en_GB-alan-medium -> en/en_GB/alan/medium
    try:
        locale, speaker, quality = name.split("-", 2)
        language = locale.split("_")[0]
    except ValueError as exc:
        raise ValueError(
            f"{name!r} is not a Piper voice name "
            "(expected something like en_GB-alan-medium)"
        ) from exc

    base = (
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
        f"{language}/{locale}/{speaker}/{quality}/{name}"
    )

    model_path = dest / f"{name}.onnx"
    for url, target in ((f"{base}.onnx", model_path),
                        (f"{base}.onnx.json", dest / f"{name}.onnx.json")):
        _download_resumable(url, target)
    return model_path


def _download_resumable(url: str, target, attempts: int = 6) -> None:
    """Fetch a URL to disk, resuming if the connection is cut mid-transfer.

    Voice models are 60+ MB and some networks (and some corporate proxies) drop
    long transfers partway. A short read here is silent corruption that only
    shows up later as an opaque protobuf error at model-load time, so the size
    is checked against Content-Length and any shortfall is re-requested with a
    Range header rather than restarting from zero.
    """
    import urllib.request

    partial = target.with_suffix(target.suffix + ".part")
    got = partial.stat().st_size if partial.exists() else 0
    total = 0

    for attempt in range(attempts):
        request = urllib.request.Request(url)
        if got:
            request.add_header("Range", f"bytes={got}-")
        try:
            with urllib.request.urlopen(request) as response:
                if not total:
                    length = int(response.headers.get("Content-Length") or 0)
                    # On a 206 the length is what remains, not the whole file.
                    total = got + length if response.status == 206 else length
                mode = "ab" if got else "wb"
                with open(partial, mode) as fh:
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        fh.write(block)
                        got += len(block)
        except Exception as exc:  # noqa: BLE001 - any transport failure resumes
            got = partial.stat().st_size if partial.exists() else 0
            if attempt == attempts - 1:
                raise IOError(f"{target.name}: {exc} (got {got} of {total} bytes)") from exc
            log.warning(
                "%s: transfer interrupted at %.1f/%.1f MB, resuming (%d/%d)",
                target.name, got / 1e6, total / 1e6, attempt + 2, attempts,
            )
            continue

        if total and got < total:
            log.warning(
                "%s: short read %.1f/%.1f MB, resuming (%d/%d)",
                target.name, got / 1e6, total / 1e6, attempt + 2, attempts,
            )
            continue
        break
    else:
        raise IOError(f"{target.name}: could not complete after {attempts} attempts")

    if total and got != total:
        raise IOError(f"{target.name}: got {got} bytes, expected {total}")

    partial.replace(target)
    log.info("fetched %s (%.1f MB)", target.name, got / 1e6)


class Speaker:
    """Synthesise, mangle, and play. Interruptible."""

    def __init__(
        self,
        backend: str = "piper",
        voice: str = "en_GB-alan-medium",
        effects: dict | None = None,
        device: int | str | None = None,
        rate: float = 1.0,
    ) -> None:
        self.backend = backend
        self.voice_name = voice
        self.effects = effects or {}
        self.device = device
        self.rate = rate
        self._voice = None
        self._sample_rate = 22050
        self._stop = asyncio.Event()

    def load(self) -> bool:
        if self.backend == "none":
            return False
        if self.backend == "sapi":
            return shutil.which("powershell") is not None
        if not PIPER_AVAILABLE:
            log.warning("piper-tts not installed - falling back to Windows SAPI")
            self.backend = "sapi"
            return shutil.which("powershell") is not None
        path = resolve_voice(self.voice_name)
        if path is None:
            log.error(
                "piper voice %r not found. Download it with: "
                "python -m erebus fetch-voice %s",
                self.voice_name, self.voice_name,
            )
            self.backend = "sapi"
            return shutil.which("powershell") is not None
        try:
            self._voice = PiperVoice.load(path)
            self._sample_rate = self._voice.config.sample_rate
        except Exception as exc:  # noqa: BLE001
            log.error(
                "could not load piper voice at %s (%s). A truncated download is "
                "the usual cause - re-run: python -m erebus fetch-voice %s",
                path, exc, self.voice_name,
            )
            self.backend = "sapi"
            return shutil.which("powershell") is not None
        log.info("piper voice ready: %s @ %d Hz", path.name, self._sample_rate)
        return True

    @property
    def ready(self) -> bool:
        return self.backend == "sapi" or self._voice is not None

    # -- synthesis ----------------------------------------------------------

    def _synthesize_sync(self, text: str):
        """Return (float32 audio, sample_rate).

        Piper yields the sentence in chunks. Taking `audio_float_array` off each
        one avoids a WAV encode/decode round trip purely to get back the floats
        we started with.
        """
        config = SynthesisConfig(length_scale=1.0 / max(0.1, self.rate))
        chunks = []
        sample_rate = self._sample_rate
        for chunk in self._voice.synthesize(text, syn_config=config):
            chunks.append(chunk.audio_float_array)
            sample_rate = chunk.sample_rate
        if not chunks:
            return np.zeros(0, dtype=np.float32), sample_rate
        return np.concatenate(chunks).astype(np.float32), sample_rate

    async def synthesize(self, text: str):
        if self._voice is None:
            return None, self._sample_rate
        loop = asyncio.get_running_loop()
        audio, sample_rate = await loop.run_in_executor(
            None, self._synthesize_sync, text
        )
        audio = voicefx.process(audio, sample_rate, self.effects)
        return audio, sample_rate

    # -- playback -----------------------------------------------------------

    def interrupt(self) -> None:
        """Cut off whatever is being said. Called when you speak over it."""
        self._stop.set()

    async def speak(self, text: str, on_level=None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._stop.clear()

        if self.backend == "sapi" or self._voice is None:
            await self._speak_sapi(text)
            return

        audio, sample_rate = await self.synthesize(text)
        if audio is None or not PLAYBACK_AVAILABLE:
            return
        await self._play(audio, sample_rate, on_level)

    async def _play(self, audio, sample_rate: int, on_level=None) -> None:
        """Stream out in blocks, reporting the envelope as we go."""
        block = max(256, int(sample_rate * 0.03))   # ~30 ms, matches a 33fps UI
        loop = asyncio.get_running_loop()

        stream = sd.OutputStream(
            samplerate=sample_rate, channels=1, dtype="float32", device=self.device
        )
        stream.start()
        try:
            for start in range(0, len(audio), block):
                if self._stop.is_set():
                    break
                chunk = audio[start : start + block]
                if on_level is not None:
                    peak = float(np.abs(chunk).max()) if len(chunk) else 0.0
                    on_level(peak)
                await loop.run_in_executor(None, stream.write, chunk)
        finally:
            if on_level is not None:
                on_level(0.0)
            stream.stop()
            stream.close()

    async def _speak_sapi(self, text: str) -> None:
        """Windows built-in voice. No effects, but it always works."""
        safe = text.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Rate = -1; "
            f"$s.Speak('{safe}')"
        )
        proc = await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-Command", script,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()
