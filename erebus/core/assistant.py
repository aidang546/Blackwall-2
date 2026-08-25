"""The orchestrator: wake -> listen -> transcribe -> decide -> act -> speak.

This is the only file that knows the whole loop. Everything it uses is optional:
if Whisper is missing you still get a working push-to-talk text console, if
Ollama is missing you still get every command in the registry. Degrading instead
of refusing to start is deliberate - a half-installed assistant should still be
useful while you finish installing it.
"""

from __future__ import annotations

import asyncio
import logging

from ..actions.registry import Registry
from ..pipeline import audio as audio_mod
from ..pipeline.brain import Brain, Decision
from ..pipeline.stt import Transcriber
from ..pipeline.tts import Speaker
from ..pipeline.wake import WakeDetector
from .bus import EventBus
from .state import State

log = logging.getLogger("erebus.assistant")

CONFIRM_YES = {"yes", "confirm", "do it", "affirmative", "go ahead", "yeah", "proceed"}


class Assistant:
    def __init__(self, config, bus: EventBus) -> None:
        self.config = config
        self.bus = bus

        self.registry = Registry(config, confirm=set(config.get("safety.confirm") or []))

        self.audio_config = audio_mod.AudioConfig(
            sample_rate=config.get("audio.sample_rate", 16000),
            device=config.get("audio.input_device"),
            silence_timeout=config.get("audio.silence_timeout", 1.1),
            silence_threshold=config.get("audio.silence_threshold", 0.012),
            max_utterance=config.get("audio.max_utterance", 15.0),
        )
        self.mic = audio_mod.Microphone(self.audio_config)

        self.wake = WakeDetector(
            model=config.get("wake.model", "hey_jarvis"),
            threshold=config.get("wake.threshold", 0.55),
            refractory=config.get("wake.refractory", 2.0),
            sample_rate=self.audio_config.sample_rate,
        )
        self.stt = Transcriber(
            model=config.get("stt.model", "small.en"),
            device=config.get("stt.device", "cuda"),
            compute_type=config.get("stt.compute_type", "float16"),
            language=config.get("stt.language", "en"),
        )
        self.speaker = Speaker(
            backend=config.get("tts.backend", "piper"),
            voice=config.get("tts.voice", "en_GB-alan-medium"),
            effects=config.section("tts").get("effects", {}),
            device=config.get("audio.output_device"),
            rate=config.get("tts.rate", 1.0),
        )
        self.brain = Brain(
            backend=config.get("brain.backend", "ollama"),
            host=config.get("brain.host", "http://127.0.0.1:11434"),
            model=config.get("brain.model", "llama3.1:8b"),
            persona=config.get("brain.persona", ""),
            max_tokens=config.get("brain.max_tokens", 220),
            temperature=config.get("brain.temperature", 0.6),
            history_turns=config.get("brain.history_turns", 6),
        )

        self._pending_confirm: tuple | None = None
        self._busy = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._voice_enabled = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Load what we can, then start the always-on loop if audio exists."""
        capabilities = {
            "wake": self.config.get("wake.enabled", True) and self.wake.load(),
            "stt": self.stt.load(),
            "tts": self.speaker.load(),
            "brain": await self.brain.load(),
            "audio": audio_mod.AUDIO_AVAILABLE,
        }
        log.info("capabilities: %s", capabilities)
        await self.bus.publish("capabilities", **capabilities)

        if capabilities["audio"] and capabilities["stt"]:
            try:
                self.mic.start()
                self._voice_enabled = True
                self._task = asyncio.create_task(self._listen_forever())
            except Exception as exc:  # noqa: BLE001
                log.error("microphone unavailable: %s", exc)
        else:
            log.warning(
                "voice input disabled - use the text box in the UI. "
                "Install: pip install -r requirements-voice.txt"
            )
        await self.bus.set_state(State.IDLE)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self.mic.stop()
        await self.brain.close()

    # -- the loop -----------------------------------------------------------

    async def _listen_forever(self) -> None:
        """Always-on wake detection. Cheap enough to leave running for days."""
        log.info("listening for wake word")
        async for frame in self.mic.frames():
            if self.bus.state is not State.IDLE:
                continue   # already handling a turn; ignore until we're back
            level = audio_mod.rms(frame)
            await self.bus.publish("level", value=level)

            if not self.wake.ready:
                continue
            score = self.wake.push(frame)
            if self.wake.fired(score):
                log.info("wake word detected (%.2f)", score)
                await self.bus.publish("wake", score=score)
                asyncio.create_task(self.take_turn())

    async def take_turn(self) -> None:
        """One full interaction, from capture to spoken reply."""
        if self._busy.locked():
            return
        async with self._busy:
            try:
                await self.bus.set_state(State.LISTENING)
                preroll = self.wake.preroll()
                self.wake.reset()

                def on_level(value: float) -> None:
                    self.bus.publish_soon("level", value=value)

                recording = await self.mic.record_utterance(
                    on_level=on_level, preroll=preroll
                )
                if recording is None:
                    log.info("nothing said")
                    await self.bus.set_state(State.IDLE)
                    return

                await self.bus.set_state(State.THINKING)
                text = await self.stt.transcribe(recording)
                if not text:
                    await self.bus.set_state(State.IDLE)
                    return
                await self.bus.publish("transcript", text=text, source="voice")
                await self._handle(text)
            except Exception as exc:  # noqa: BLE001
                log.exception("turn failed")
                await self.bus.set_state(State.ERROR, message=str(exc))
                await asyncio.sleep(1.5)
            finally:
                if self.bus.state is not State.IDLE:
                    await self.bus.set_state(State.IDLE)

    # -- decision -----------------------------------------------------------

    async def handle(self, text: str) -> None:
        """Route one utterance, from voice or from the UI's text box.

        Callers launch this as a task, so it has to swallow nothing: an
        unhandled failure here would otherwise vanish into an orphaned task and
        leave the wall stuck mid-thought.
        """
        try:
            await self._handle(text)
        except Exception as exc:  # noqa: BLE001
            log.exception("handling %r failed", text)
            await self.bus.set_state(State.ERROR, message=str(exc))
            await asyncio.sleep(1.5)
            await self.bus.set_state(State.IDLE)

    async def _handle(self, text: str) -> None:
        await self.bus.set_state(State.THINKING)

        # A pending destructive action takes priority over everything else.
        if self._pending_confirm is not None:
            action, value = self._pending_confirm
            self._pending_confirm = None
            if any(word in text.lower() for word in CONFIRM_YES):
                reply = await self.registry.run(action, value, say=self.say)
                await self.say(reply or f"{action.label} confirmed.")
            else:
                await self.say("Cancelled.")
            return

        # 1. Exact/deterministic match - no model in the path.
        match = self.registry.match(text)
        if match is not None and (match.exact or match.score >= 6):
            await self._execute(match.action, match.value)
            return

        # 2. Loose phrasing - let the router look at it.
        decision: Decision = await self.brain.route(text, self.registry.catalog)
        if decision.kind == "action" and decision.action in self.registry.actions:
            await self._execute(self.registry.actions[decision.action], decision.value)
            return

        # 3. Not a command. Talk.
        if self.brain.ready:
            reply = await self.brain.converse(text)
        elif match is not None:
            await self._execute(match.action, match.value)
            return
        else:
            reply = "I have no action for that, and no reasoning core to improvise."
        await self.say(reply)

    async def _execute(self, action, value: str | None) -> None:
        if self.registry.needs_confirmation(action):
            self._pending_confirm = (action, value)
            await self.say(f"Confirm {action.label}.")
            return
        await self.bus.publish(
            "action", name=action.name, category=action.kind, value=value
        )
        reply = await self.registry.run(action, value, say=self.say)
        if reply:
            await self.say(reply)
        else:
            # Silent success still needs to land somewhere the operator can see.
            await self.bus.publish("done", name=action.name)

    # -- output -------------------------------------------------------------

    async def say(self, text: str) -> None:
        if not text:
            return
        await self.bus.publish("reply", text=text)
        await self.bus.set_state(State.SPEAKING, text=text)

        def on_level(value: float) -> None:
            self.bus.publish_soon("level", value=value)

        try:
            await self.speaker.speak(text, on_level=on_level)
        except Exception as exc:  # noqa: BLE001
            log.error("speech failed: %s", exc)
        await self.bus.set_state(State.IDLE)

    def interrupt(self) -> None:
        self.speaker.interrupt()
