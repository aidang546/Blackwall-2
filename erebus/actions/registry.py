"""Everything Erebus can do, and the only path by which it can do it.

Security model, stated plainly:

* Shell strings come from `config.yaml` only. You write them; they are trusted
  because they are yours.
* Speech and the LLM can only ever select an action *by name* from this
  registry. Neither can compose, extend, or parameterise a shell command.
* So the worst a misheard sentence (or a confused model) can do is run one of
  the commands you already wrote down - never something new.

`safety.registry_only: false` would relax rule two. It is off by default and
there is deliberately no voice command to turn it on.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from . import system

log = logging.getLogger("erebus.actions")

#: Stripped from the front of an utterance before matching, so "open spotify",
#: "launch spotify" and "spotify" all land on the same action.
LAUNCH_PREFIXES = (
    "please ", "can you ", "could you ",
    "open up ", "open ", "launch ", "start ", "run ", "fire up ", "boot ",
    "go to ", "put on ", "pull up ",
)

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

#: A substring match must cover at least this fraction of the utterance,
#: otherwise it is a passing mention rather than a command.
MIN_COVERAGE = 0.45

#: Fuzzy-matching limits. See Registry._fuzzy_match for why each one is here.
FUZZY_THRESHOLD = 0.80      # similarity floor ("coming mode" -> "gaming mode")
FUZZY_MIN_LENGTH = 7        # shortest phrase eligible, in characters
FUZZY_LENGTH_RATIO = 0.75   # the two strings must be comparable in length


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return " ".join(_WORD_SPLIT.split(text.lower())).strip()


def strip_prefixes(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for prefix in LAUNCH_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):]
                changed = True
    return text.strip()


@dataclass
class Action:
    name: str
    kind: str                       # "app" | "system" | "macro" | "builtin"
    phrases: list[str]
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.name.replace("_", " ")


@dataclass
class Match:
    action: Action
    value: str | None
    score: float
    #: True when the phrase matched exactly rather than as a substring.
    exact: bool


class Registry:
    def __init__(self, config, confirm: set[str] | None = None) -> None:
        self.actions: dict[str, Action] = {}
        self._builtins: dict[str, Any] = {}
        self.confirm = confirm or set()
        self._load(config)

    def _load(self, config) -> None:
        for name, spec in (config.get("actions.apps") or {}).items():
            self.actions[name] = Action(
                name, "app", [normalize(p) for p in spec.get("phrases", [name])], spec
            )
        for name, spec in (config.get("actions.system") or {}).items():
            self.actions[name] = Action(
                name, "system", [normalize(p) for p in (spec or {}).get("phrases", [name])],
                spec or {},
            )
        for name, spec in (config.get("actions.macros") or {}).items():
            self.actions[name] = Action(
                name, "macro", [normalize(p) for p in spec.get("phrases", [name])], spec
            )
        for name, spec in (config.get("actions.builtin") or {}).items():
            self.actions[name] = Action(
                name, "builtin",
                [normalize(p) for p in (spec or {}).get("phrases", [name])], spec or {},
            )
        log.info(
            "registry loaded: %d actions (%d apps, %d system, %d macros, %d builtin)",
            len(self.actions),
            sum(1 for a in self.actions.values() if a.kind == "app"),
            sum(1 for a in self.actions.values() if a.kind == "system"),
            sum(1 for a in self.actions.values() if a.kind == "macro"),
            sum(1 for a in self.actions.values() if a.kind == "builtin"),
        )

    def register_builtin(self, name: str, handler) -> None:
        """Attach the implementation of a builtin action.

        Builtins are declared in config like everything else - so they match by
        phrase, appear in `erebus actions`, and can be renamed or given extra
        phrasings without touching code - but their implementation lives in
        Python rather than in a shell string. The assistant supplies it at
        startup, which keeps the registry ignorant of what a briefing is.
        """
        if name not in self.actions:
            log.warning(
                "no config entry for builtin %r - add it under actions.builtin "
                "to give it phrases", name,
            )
            return
        self._builtins[name] = handler

    @property
    def catalog(self) -> list[str]:
        """Action names, for handing to the LLM router."""
        return sorted(self.actions)

    def describe(self) -> list[dict]:
        return [
            {"name": a.name, "kind": a.kind, "phrases": a.phrases}
            for a in sorted(self.actions.values(), key=lambda a: (a.kind, a.name))
        ]

    # -- matching -----------------------------------------------------------

    def match(self, utterance: str) -> Match | None:
        """Find the best deterministic match, or None to defer to the LLM.

        Longer phrases win, because "volume down" must beat a bare "volume", and
        "previous track" must beat "track".
        """
        text = strip_prefixes(normalize(utterance))
        if not text:
            return None

        best: Match | None = None
        for action in self.actions.values():
            for phrase in action.phrases:
                if not phrase:
                    continue
                if text == phrase:
                    score, exact = len(phrase) + 100, True
                elif self._contains_phrase(text, phrase):
                    # A command has to be most of what you said, not an aside.
                    # "lock up" is a real phrase, but in "i am going to lock up
                    # now" it is a mention, not an instruction - and locking the
                    # machine because someone said it in passing is exactly the
                    # kind of surprise this assistant must not produce.
                    if len(phrase) / len(text) < MIN_COVERAGE:
                        continue
                    score, exact = len(phrase), False
                else:
                    continue
                if best is None or score > best.score:
                    best = Match(action, None, score, exact)

        if best is None:
            best = self._fuzzy_match(text)
        if best is None:
            return None

        # Pull a number out for actions that take one ("set volume to 40").
        if best.action.name == "volume_set":
            number = re.search(r"\b(\d{1,3})\b", text)
            if not number:
                return None   # "volume to" with no number is not a command yet
            best.value = number.group(1)
        return best

    def _fuzzy_match(self, text: str) -> Match | None:
        """Catch near-misses from speech recognition.

        Whisper reliably produces plausible-but-wrong neighbours - "coming mode"
        for "gaming mode", "lock the computer" as "log the computer". Those are
        one or two characters off a phrase we know, and failing them sends a
        perfectly clear command to the LLM or to nothing at all.

        Kept deliberately tight:

        * only whole-utterance comparisons, never substrings, so a long sentence
          cannot drift into a short command;
        * only phrases of a real length, since short ones ("play", "mute") have
          too many close neighbours in ordinary speech;
        * a high similarity floor, and the length of the two strings must be
          comparable - "lock" and "lock the computer and open chrome" are not
          near-misses of each other however well the prefix lines up.

        Anything softer than this belongs to the LLM router, which sees the
        utterance next and has the context to judge it.
        """
        best: Match | None = None
        for action in self.actions.values():
            for phrase in action.phrases:
                if len(phrase) < FUZZY_MIN_LENGTH:
                    continue
                shorter, longer = sorted((len(text), len(phrase)))
                if shorter / longer < FUZZY_LENGTH_RATIO:
                    continue
                ratio = difflib.SequenceMatcher(None, text, phrase).ratio()
                if ratio < FUZZY_THRESHOLD:
                    continue
                # Scale into the same range as a substring hit so the
                # assistant's confidence gate treats it consistently.
                score = len(phrase) * ratio
                if best is None or score > best.score:
                    best = Match(action, None, score, False)
        if best is not None:
            log.info("fuzzy match: %r -> %s", text, best.action.name)
        return best

    @staticmethod
    def _contains_phrase(text: str, phrase: str) -> bool:
        """Substring match that respects word boundaries.

        Without this, "play" matches inside "player" and "display", which is how
        you end up pausing your music by asking about the display.
        """
        return re.search(rf"(?:^|\s){re.escape(phrase)}(?:\s|$)", text) is not None

    def needs_confirmation(self, action: Action) -> bool:
        return action.name in self.confirm

    # -- execution ----------------------------------------------------------

    async def run(self, action: Action, value: str | None = None, say=None) -> str:
        """Execute an action. Returns the line to speak back (may be empty)."""
        log.info("running action %s (%s) value=%r", action.name, action.kind, value)
        if action.kind == "app":
            return await self._run_shell(action.spec.get("run", ""), action.label)
        if action.kind == "system":
            return await self._run_system(action.name, value)
        if action.kind == "macro":
            return await self._run_macro(action, say)
        if action.kind == "builtin":
            return await self._run_builtin(action)
        return ""

    async def _run_builtin(self, action: Action) -> str:
        handler = self._builtins.get(action.name)
        if handler is None:
            return f"{action.label} is not wired up."
        try:
            return await handler()
        except Exception as exc:  # noqa: BLE001
            log.exception("builtin %s failed", action.name)
            return f"{action.label} failed: {exc}"

    async def _run_shell(self, command: str, label: str) -> str:
        if not command:
            return f"No command configured for {label}."
        try:
            # shell=True is intentional: these strings are hand-written in
            # config.yaml by the operator and often rely on `start`, which is a
            # shell builtin on Windows. Nothing user-spoken reaches this string.
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("command failed (%s): %s", command, exc)
            return f"{label} did not start."
        return ""

    async def _run_system(self, name: str, value: str | None) -> str:
        handler = system.HANDLERS.get(name)
        if handler is None:
            return f"No handler for {name}."
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, lambda: handler(value)
            )
        except Exception as exc:  # noqa: BLE001
            log.error("system action %s failed: %s", name, exc)
            return "That did not take."

    async def _run_macro(self, action: Action, say=None) -> str:
        for step in action.spec.get("steps", []) or []:
            if "run" in step:
                await self._run_shell(step["run"], action.label)
            elif "do" in step:
                await self._run_system(step["do"], step.get("value"))
            elif "say" in step and say is not None:
                await say(step["say"])
            delay = step.get("wait")
            if delay:
                await asyncio.sleep(float(delay))
        return action.spec.get("say", "") or ""
