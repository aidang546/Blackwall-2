"""Microphone capture.

One continuous input stream feeds two consumers: the wake detector (always) and
the utterance recorder (only between wake and end-of-speech). Running a single
stream rather than opening one on demand matters - opening a WASAPI device takes
long enough on Windows that you clip the first syllable of every command.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from dataclasses import dataclass

log = logging.getLogger("erebus.audio")

try:  # pragma: no cover - hardware dependent
    import numpy as np
    import sounddevice as sd

    AUDIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    sd = None  # type: ignore[assignment]
    AUDIO_AVAILABLE = False


#: openWakeWord and Whisper both want 16 kHz mono float32.
FRAME_MS = 80


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    device: int | str | None = None
    silence_timeout: float = 1.1
    silence_threshold: float = 0.012
    max_utterance: float = 15.0

    @property
    def frame_size(self) -> int:
        return int(self.sample_rate * FRAME_MS / 1000)


def list_devices() -> list[dict]:
    """Enumerate input devices so the user can pin one in config."""
    if not AUDIO_AVAILABLE:
        return []
    out = []
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            out.append(
                {
                    "index": index,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "default_samplerate": int(dev["default_samplerate"]),
                }
            )
    return out


def rms(frame) -> float:
    """Root-mean-square level of a float32 frame, 0..1-ish."""
    if np is None or frame is None or len(frame) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame))))


class Microphone:
    """Async iterator over fixed-size float32 frames.

    The PortAudio callback runs on its own thread, so it does the absolute
    minimum: push into a queue and return. Everything else happens on the loop.
    """

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._stream = None
        self._running = False

    def _callback(self, indata, frames, time_info, status) -> None:  # pragma: no cover
        if status:
            log.debug("input stream status: %s", status)
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            # Better to drop a frame than to let the callback overrun.
            pass

    def start(self) -> None:
        if not AUDIO_AVAILABLE:
            raise RuntimeError(
                "Audio capture needs `sounddevice`. "
                "Install it with: pip install -r requirements-voice.txt"
            )
        if self._running:
            return
        self._stream = sd.InputStream(
            samplerate=self.config.sample_rate,
            blocksize=self.config.frame_size,
            device=self.config.device,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self._running = True
        log.info(
            "microphone open: %s Hz, %s ms frames",
            self.config.sample_rate,
            FRAME_MS,
        )

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def frames(self):
        """Yield frames as they arrive, without blocking the event loop."""
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                frame = await loop.run_in_executor(None, self._queue.get, True, 0.5)
            except queue.Empty:
                continue
            yield frame

    async def record_utterance(self, on_level=None, preroll=None):
        """Capture speech until the speaker stops, then return the whole thing.

        `preroll` is audio captured *before* this was called - the wake detector
        hands over the tail of its buffer so a command spoken immediately after
        the wake word ("Erebus, gaming mode") is not truncated.

        Returns a float32 array, or None if nothing above the noise floor
        arrived before the timeout.
        """
        chunks: list = list(preroll or [])
        started = time.monotonic()
        last_voice = started
        heard_voice = False

        async for frame in self.frames():
            chunks.append(frame)
            level = rms(frame)
            if on_level is not None:
                on_level(level)

            now = time.monotonic()
            if level > self.config.silence_threshold:
                heard_voice = True
                last_voice = now

            if heard_voice and (now - last_voice) > self.config.silence_timeout:
                break
            # Nothing at all was said - give up rather than hang open.
            if not heard_voice and (now - started) > self.config.silence_timeout * 3:
                return None
            if (now - started) > self.config.max_utterance:
                log.warning("utterance hit max length, cutting off")
                break

        if not chunks or not heard_voice:
            return None
        return np.concatenate(chunks)
