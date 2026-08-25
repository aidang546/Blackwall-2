"""Text to speech, plus playback that drives the visualiser.

Playback is deliberately not fire-and-forget: it streams the audio out in small
blocks and reports the amplitude of each one, which is what makes the Blackwall
move in time with the voice instead of just flailing on a timer.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import subprocess
import wave

from . import voicefx

log = logging.getLogger("erebus.tts")

try:  # pragma: no cover
    import numpy as np
    import sounddevice as sd

    PLAYBACK_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]
    PLAYBACK_AVAILABLE = False

try:  # pragma: no cover
    from piper.voice import PiperVoice

    PIPER_AVAILABLE = True
except ImportError:  # pragma: no cover
    PiperVoice = None  # type: ignore[assignment]
    PIPER_AVAILABLE = False


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
        try:
            self._voice = PiperVoice.load(self.voice_name)
            self._sample_rate = self._voice.config.sample_rate
        except Exception as exc:  # noqa: BLE001
            log.error(
                "could not load piper voice %r (%s). "
                "Download voices with: python -m erebus fetch-voice %s",
                self.voice_name, exc, self.voice_name,
            )
            self.backend = "sapi"
            return shutil.which("powershell") is not None
        log.info("piper voice ready: %s @ %d Hz", self.voice_name, self._sample_rate)
        return True

    @property
    def ready(self) -> bool:
        return self.backend == "sapi" or self._voice is not None

    # -- synthesis ----------------------------------------------------------

    def _synthesize_sync(self, text: str):
        """Return (float32 audio, sample_rate)."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            self._voice.synthesize(text, wav, length_scale=1.0 / max(0.1, self.rate))
        buffer.seek(0)
        with wave.open(buffer, "rb") as wav:
            sample_rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return audio, sample_rate

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
