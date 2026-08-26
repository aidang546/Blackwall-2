"""The reasoning layer, backed by a local LLM through Ollama.

Two jobs, in this order:

1. **Route.** Decide whether an utterance is a command or a question. Exact
   phrase matches never reach the LLM at all - "gaming mode" should fire in
   microseconds, not wait on token generation. The LLM only sees utterances the
   registry could not match confidently.
2. **Converse.** Answer anything that is not a command, in character.

The LLM is never allowed to invent a shell command. It may only name an action
that already exists in the registry, and the registry is what actually runs it.
That constraint is the whole security model, and it is enforced in
`actions/registry.py` rather than by asking the model nicely.
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass

import httpx

log = logging.getLogger("erebus.brain")


@dataclass
class Decision:
    """What to do with an utterance."""
    kind: str                     # "action" | "speak" | "none"
    action: str | None = None
    value: str | None = None
    text: str = ""


ROUTER_INSTRUCTIONS = """\
You route a spoken utterance for a voice assistant.

Reply with ONE line of JSON and nothing else:
  {"action": "<name from the list>", "value": "<optional argument>"}
  {"action": null}

Choose an action ONLY if the utterance clearly asks for it. If the operator is
asking a question, making conversation, or you are unsure, return {"action": null}.
Never invent an action name that is not in the list.

Available actions:
%s
"""


class Brain:
    def __init__(
        self,
        backend: str = "ollama",
        host: str = "http://127.0.0.1:11434",
        model: str = "llama3.1:8b",
        persona: str = "",
        max_tokens: int = 220,
        temperature: float = 0.6,
        history_turns: int = 6,
    ) -> None:
        self.backend = backend
        self.host = host.rstrip("/")
        self.model = model
        self.persona = persona.strip()
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._history: deque = deque(maxlen=history_turns * 2)
        self._client: httpx.AsyncClient | None = None
        self._available = False

    async def load(self) -> bool:
        if self.backend != "ollama":
            return False
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=3.0))
        try:
            response = await self._client.get(f"{self.host}/api/tags")
            response.raise_for_status()
            installed = {m["name"] for m in response.json().get("models", [])}
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Ollama unreachable at %s (%s) - running in commands-only mode. "
                "Start it with `ollama serve`.", self.host, exc,
            )
            return False

        # Ollama tags carry a :tag suffix; accept either form.
        if self.model not in installed and f"{self.model}:latest" not in installed:
            log.warning(
                "model %r not pulled. Run: ollama pull %s", self.model, self.model
            )
            return False
        self._available = True
        log.info("brain ready: %s via ollama", self.model)
        return True

    @property
    def ready(self) -> bool:
        return self._available

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # -- low level ----------------------------------------------------------

    async def _chat(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        assert self._client is not None
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        response = await self._client.post(f"{self.host}/api/chat", json=payload)
        response.raise_for_status()
        return response.json().get("message", {}).get("content", "").strip()

    # -- routing ------------------------------------------------------------

    async def route(self, utterance: str, catalog: list[str]) -> Decision:
        """Ask the LLM whether this utterance names a known action."""
        if not self._available or not catalog:
            return Decision(kind="none")
        listing = "\n".join(f"- {name}" for name in catalog)
        try:
            raw = await self._chat(
                [
                    {"role": "system", "content": ROUTER_INSTRUCTIONS % listing},
                    {"role": "user", "content": utterance},
                ],
                temperature=0.0,
                max_tokens=64,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("router call failed: %s", exc)
            return Decision(kind="none")

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return Decision(kind="none")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return Decision(kind="none")

        action = parsed.get("action")
        # The model does hallucinate names occasionally; the catalog is the truth.
        if not action or action not in catalog:
            return Decision(kind="none")
        value = parsed.get("value")
        return Decision(kind="action", action=action, value=str(value) if value else None)

    # -- one-shot -----------------------------------------------------------

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """A single exchange with its own system prompt, outside the conversation.

        The briefing needs its own persona and must not leak into - or be
        coloured by - ordinary chat history, so it cannot go through
        `converse`. Nothing here is remembered.
        """
        if not self._available:
            return ""
        try:
            return await self._chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=self.max_tokens if max_tokens is None else max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("completion failed: %s", exc)
            return ""

    # -- conversation -------------------------------------------------------

    async def converse(self, utterance: str) -> str:
        if not self._available:
            return "My reasoning core is offline. Commands still function."
        messages = [{"role": "system", "content": self.persona}]
        messages.extend(self._history)
        messages.append({"role": "user", "content": utterance})
        try:
            reply = await self._chat(messages, self.temperature, self.max_tokens)
        except Exception as exc:  # noqa: BLE001
            log.error("conversation call failed: %s", exc)
            return "The connection faltered. Say it again."
        self._history.append({"role": "user", "content": utterance})
        self._history.append({"role": "assistant", "content": reply})
        return reply

    def forget(self) -> None:
        self._history.clear()
