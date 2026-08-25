"""The state machine the whole UI renders from.

There are exactly five states and every component - wake detector, STT, brain,
TTS - does nothing but move between them. The visualiser subscribes and animates
accordingly, so keeping this small is what keeps the UI honest.
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    #: Wall is a single stationary line. Wake detector running, nothing else.
    IDLE = "idle"
    #: Wake word heard. Capturing speech; the wall reacts to your voice level.
    LISTENING = "listening"
    #: Transcribing / deciding / running an action. Slow turbulent churn.
    THINKING = "thinking"
    #: Speaking a reply. Wall is driven by the outgoing audio envelope.
    SPEAKING = "speaking"
    #: Something broke. Wall fractures and goes dim.
    ERROR = "error"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value
