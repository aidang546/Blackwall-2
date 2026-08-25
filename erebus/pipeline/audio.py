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
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("erebus.audio")

# numpy and the sound card are separate concerns: a FileMicrophone needs only
# the former. Bundling the imports would make a machine with no audio device
# look like a machine with no numpy.
try:  # pragma: no cover
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:  # pragma: no cover - hardware dependent
    import sounddevice as sd

    AUDIO_AVAILABLE = np is not None
except (ImportError, OSError):  # pragma: no cover
    # OSError: the wheel is present but libportaudio or a device is not.
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

async def record_utterance(config: AudioConfig, frames, on_level=None, preroll=None):
    """Consume frames until the speaker stops, then return the whole utterance.

    Takes a frame source rather than reading the microphone itself, because
    there must only ever be one reader of the capture queue. The wake loop owns
    that read and forwards frames here for the duration of a turn; two
    coroutines pulling from the same queue would silently split the audio
    between them and hand Whisper every other frame.

    `preroll` is audio captured *before* this was called - the wake detector
    passes the tail of its buffer so a command run together with the wake word
    ("Erebus, gaming mode") is not clipped.

    Returns a float32 array, or None if nothing above the noise floor arrived.
    """
    chunks: list = list(preroll or [])
    started = time.monotonic()
    last_voice = started
    heard_voice = False

    async for frame in frames:
        chunks.append(frame)
        level = rms(frame)
        if on_level is not None:
            on_level(level)

        now = time.monotonic()
        if level > config.silence_threshold:
            heard_voice = True
            last_voice = now

        if heard_voice and (now - last_voice) > config.silence_timeout:
            break
        # Nothing at all was said - give up rather than hang open.
        if not heard_voice and (now - started) > config.silence_timeout * 3:
            return None
        if (now - started) > config.max_utterance:
            log.warning("utterance hit max length, cutting off")
            break

    if not chunks or not heard_voice:
        return None
    return np.concatenate(chunks)


class FileMicrophone(Microphone):
    """A microphone that plays back a WAV file instead of listening.

    Everything downstream - silence detection, the recorder, Whisper, the
    matcher, the visualiser - behaves exactly as it does with real hardware,
    because it is the same code receiving the same frames. That makes the whole
    loop testable on a machine with no sound card, and makes a misheard command
    reproducible: capture the audio once, then replay it while you fix things.

    Frames are fed at wall-clock speed on purpose. Pushing the file through as
    fast as it will go would defeat the silence timeout and mask timing bugs.
    """

    def __init__(self, config: AudioConfig, path) -> None:
        super().__init__(config)
        self.path = str(path)
        self._thread = None

    def _load(self):
        import wave

        with wave.open(self.path, "rb") as wav:
            rate = wav.getframerate()
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            raw = wav.readframes(wav.getnframes())

        if width != 2:
            raise ValueError(f"{self.path}: need 16-bit PCM, got {width * 8}-bit")

        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)

        if rate != self.config.sample_rate:
            target_n = int(len(audio) / rate * self.config.sample_rate)
            idx = np.linspace(0, len(audio) - 1, target_n)
            lo = np.floor(idx).astype(np.int32)
            hi = np.clip(lo + 1, 0, len(audio) - 1)
            frac = (idx - lo).astype(np.float32)
            audio = audio[lo] * (1 - frac) + audio[hi] * frac
            log.info("resampled %s from %d to %d Hz", self.path, rate,
                     self.config.sample_rate)
        return audio.astype(np.float32)

    def _pump(self, audio) -> None:
        import time as _time

        frame_size = self.config.frame_size
        interval = frame_size / self.config.sample_rate
        # Trailing silence so the recorder's end-of-speech timeout fires just as
        # it would if you had stopped talking.
        tail = np.zeros(
            int(self.config.sample_rate * (self.config.silence_timeout + 0.6)),
            dtype=np.float32,
        )
        stream = np.concatenate([audio, tail])

        next_at = _time.monotonic()
        for start in range(0, len(stream), frame_size):
            if not self._running:
                return
            frame = stream[start : start + frame_size]
            if len(frame) < frame_size:
                frame = np.pad(frame, (0, frame_size - len(frame)))
            try:
                self._queue.put(frame, timeout=1.0)
            except queue.Full:
                pass
            next_at += interval
            delay = next_at - _time.monotonic()
            if delay > 0:
                _time.sleep(delay)

        # Keep feeding silence rather than stopping, so a caller that is still
        # waiting on frames does not hang on an empty queue.
        silence = np.zeros(frame_size, dtype=np.float32)
        while self._running:
            try:
                self._queue.put(silence, timeout=0.5)
            except queue.Full:
                pass
            _time.sleep(interval)

    def start(self) -> None:
        if self._running:
            return
        if np is None:
            raise RuntimeError("FileMicrophone needs numpy")
        audio = self._load()
        self._running = True
        self._thread = threading.Thread(
            target=self._pump, args=(audio,), daemon=True
        )
        self._thread.start()
        log.info("fake microphone: %s (%.1fs)", self.path,
                 len(audio) / self.config.sample_rate)

    def stop(self) -> None:
        self._running = False
        self._thread = None
