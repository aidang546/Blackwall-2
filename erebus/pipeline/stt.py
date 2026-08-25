"""Speech to text via faster-whisper (CTranslate2).

On an RTX card with `small.en` in float16 a three-second command transcribes in
roughly 150-250 ms, which is the difference between an assistant that feels
instant and one that feels like a web form. The model is loaded once at startup
and kept resident.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("erebus.stt")

try:  # pragma: no cover - optional heavy dep
    from faster_whisper import WhisperModel

    STT_AVAILABLE = True
except ImportError:  # pragma: no cover
    WhisperModel = None  # type: ignore[assignment]
    STT_AVAILABLE = False


class Transcriber:
    def __init__(
        self,
        model: str = "small.en",
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "en",
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model = None

    def load(self) -> bool:
        if not STT_AVAILABLE:
            log.warning("faster-whisper not installed - speech recognition disabled")
            return False
        started = time.monotonic()
        try:
            self._model = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
        except Exception as exc:  # noqa: BLE001
            # Almost always "no CUDA" or "cuDNN missing" - fall back rather than
            # dying, a CPU-transcribed command still beats a dead assistant.
            log.error("GPU load failed (%s); retrying on CPU", exc)
            try:
                self._model = WhisperModel(
                    self.model_name, device="cpu", compute_type="int8"
                )
                self.device = "cpu"
            except Exception as exc2:  # noqa: BLE001
                log.error("could not load Whisper at all: %s", exc2)
                return False
        log.info(
            "whisper %s ready on %s in %.1fs",
            self.model_name,
            self.device,
            time.monotonic() - started,
        )
        return True

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _transcribe_sync(self, audio) -> str:
        segments, _info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=1,          # greedy: commands are short, beams cost latency
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def transcribe(self, audio) -> str:
        """Transcribe a float32 16 kHz array. Runs off-loop so the UI keeps ticking."""
        if self._model is None:
            return ""
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        text = await loop.run_in_executor(None, self._transcribe_sync, audio)
        log.info("transcribed in %.0fms: %r", (time.monotonic() - started) * 1000, text)
        return text
