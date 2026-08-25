"""Wake word detection.

openWakeWord runs a small ONNX model over a rolling 16 kHz buffer at maybe 1-2%
of one core, which is what makes always-on listening reasonable. Nothing is
recorded or sent anywhere until it fires.

"Erebus" is not one of the stock models. Until you train one (docs/WAKEWORD.md)
this loads a stock model as a stand-in so the pipeline is testable end to end.
"""

from __future__ import annotations

import collections
import logging
import time

log = logging.getLogger("erebus.wake")

try:  # pragma: no cover - optional heavy dep
    from openwakeword.model import Model as _OWWModel

    WAKE_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OWWModel = None  # type: ignore[assignment]
    WAKE_AVAILABLE = False


class WakeDetector:
    """Feed it frames; it tells you when your word was spoken.

    It also keeps the last ~1.5s of audio. When the word fires, that buffer is
    handed to the recorder as preroll so a command run together with the wake
    word survives intact.
    """

    def __init__(
        self,
        model: str = "hey_jarvis",
        threshold: float = 0.55,
        refractory: float = 2.0,
        sample_rate: int = 16000,
        preroll_seconds: float = 1.5,
    ) -> None:
        self.threshold = threshold
        self.refractory = refractory
        self.model_name = model
        self._last_fire = 0.0
        self._model = None
        frames_per_second = sample_rate / (sample_rate * 0.08)
        self._preroll: collections.deque = collections.deque(
            maxlen=max(1, int(preroll_seconds * frames_per_second))
        )

    def load(self) -> bool:
        if not WAKE_AVAILABLE:
            log.warning(
                "openwakeword not installed - wake word disabled, "
                "use push-to-talk from the UI"
            )
            return False
        try:
            self._model = _OWWModel(wakeword_models=[self.model_name])
        except Exception as exc:  # noqa: BLE001 - surface any load failure clearly
            log.error("could not load wake model %r: %s", self.model_name, exc)
            return False
        log.info("wake model loaded: %s (threshold %.2f)", self.model_name, self.threshold)
        return True

    @property
    def ready(self) -> bool:
        return self._model is not None

    def preroll(self) -> list:
        """The audio just before the wake word fired."""
        return list(self._preroll)

    def reset(self) -> None:
        self._preroll.clear()
        if self._model is not None:
            self._model.reset()

    def push(self, frame) -> float:
        """Push one frame. Returns the detection score (0..1)."""
        self._preroll.append(frame)
        if self._model is None:
            return 0.0

        # openWakeWord wants int16 PCM.
        import numpy as np

        pcm = (frame * 32767).astype(np.int16)
        scores = self._model.predict(pcm)
        return max(scores.values()) if scores else 0.0

    def fired(self, score: float) -> bool:
        """True if this score should trigger, respecting the refractory period."""
        if score < self.threshold:
            return False
        now = time.monotonic()
        if now - self._last_fire < self.refractory:
            return False
        self._last_fire = now
        return True
