"""What it has learned about this operator, and keeps across restarts.

Two different things live here, and they arrive differently.

*Phrases* are learned by being corrected. Every time Erebus asks "which one?"
and gets an answer, that is a labelled example - this wording, that action,
confirmed by the person who said it. Writing those down means it stops asking:
say something your own way once, answer once, and it is your vocabulary from
then on.

*Facts* are told to it outright. "Remember that I train on Tuesdays." They go
into the conversation's system prompt, so it knows them the way it knows its
own persona rather than by being reminded.

Append-only, like the journal, and for the same reason: forgetting is recorded
as an event rather than by deleting a line. A store you can quietly revise
cannot be trusted to tell you what it thinks it knows.

Nothing here leaves the machine, and `memory.local.jsonl` is gitignored.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("erebus.memory")

ROOT = Path(__file__).resolve().parents[1]
MEMORY_PATH = ROOT / "memory.local.jsonl"

#: How many facts reach the system prompt. A prompt that grows without limit
#: slows every reply and eventually crowds out the persona itself.
MAX_FACTS_IN_PROMPT = 40

#: Learned phrases are only consulted for utterances of a sensible length -
#: a single word is too likely to collide with something else entirely.
MIN_LEARNED_PHRASE = 4

_WORDS = re.compile(r"[^a-z0-9]+")

#: Openers stripped from "remember that I train on Tuesdays" before storing.
FACT_PREFIXES = (
    "remember that ", "remember ", "note that ", "make a note that ",
    "make a note ", "keep in mind that ", "keep in mind ", "know that ",
)


def normalize(text: str) -> str:
    return " ".join(_WORDS.split(text.lower())).strip()


@dataclass
class Note:
    ts: datetime
    kind: str          # "phrase" | "fact" | "forget"
    key: str
    value: str
    source: str = ""

    def to_json(self) -> dict:
        return {"ts": self.ts.isoformat(), "kind": self.kind,
                "key": self.key, "value": self.value, "source": self.source}


class Memory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else MEMORY_PATH
        self._phrases: dict[str, str] = {}
        self._facts: list[str] = []
        self._load()

    # -- reading ------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # One malformed line must not cost the whole memory.
                log.warning("skipping unreadable memory line")
                continue
            self._apply(row.get("kind", ""), row.get("key", ""),
                        row.get("value", ""))
        log.info("memory: %d phrases, %d facts",
                 len(self._phrases), len(self._facts))

    def _apply(self, kind: str, key: str, value: str) -> None:
        """Replay one record. Order matters - later entries win."""
        if kind == "phrase" and key:
            self._phrases[key] = value
        elif kind == "fact" and value:
            if value not in self._facts:
                self._facts.append(value)
        elif kind == "forget":
            self._phrases.pop(key, None)
            self._facts = [f for f in self._facts if normalize(key) not in normalize(f)]

    def _append(self, note: Note) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(note.to_json()) + "\n")
        except OSError as exc:
            # Learning is a convenience; failing to write must never break a turn.
            log.warning("could not write memory (%s)", exc)

    # -- phrases ------------------------------------------------------------

    def learn_phrase(self, utterance: str, action: str) -> None:
        """Record that this wording means this action, as confirmed by them.

        A bare verb is never learned. "Open" followed by picking Spotify once
        does not mean "open" is Spotify forever - it means the question was
        answered that time. Pinning it would replace a useful question with a
        wrong guess, which is the opposite of the point.
        """
        from .actions.registry import is_bare_launch_verb

        key = normalize(utterance)
        if len(key) < MIN_LEARNED_PHRASE or not action:
            return
        if is_bare_launch_verb(key):
            return
        if self._phrases.get(key) == action:
            return
        self._phrases[key] = action
        self._append(Note(datetime.now(timezone.utc), "phrase", key, action,
                          "confirmed"))
        log.info("learned %r -> %s", key, action)

    def phrase_for(self, utterance: str) -> str | None:
        return self._phrases.get(normalize(utterance))

    @property
    def phrases(self) -> dict[str, str]:
        return dict(self._phrases)

    # -- facts --------------------------------------------------------------

    def remember(self, text: str) -> str:
        """Store something told outright. Returns what was actually stored."""
        cleaned = text.strip()
        lowered = cleaned.lower()
        for prefix in FACT_PREFIXES:
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
        if not cleaned:
            return ""
        if cleaned not in self._facts:
            self._facts.append(cleaned)
            self._append(Note(datetime.now(timezone.utc), "fact", "", cleaned,
                              "taught"))
        return cleaned

    @property
    def facts(self) -> list[str]:
        return list(self._facts)

    def forget(self, query: str) -> int:
        """Drop anything matching. Recorded, not erased."""
        needle = normalize(query)
        if not needle:
            return 0
        gone = [f for f in self._facts if needle in normalize(f)]
        keys = [k for k, v in self._phrases.items()
                if needle in k or needle in normalize(v)]
        if not gone and not keys:
            return 0
        for key in keys:
            self._phrases.pop(key, None)
        self._facts = [f for f in self._facts if f not in gone]
        self._append(Note(datetime.now(timezone.utc), "forget", query, "", "asked"))
        return len(gone) + len(keys)

    # -- what it knows ------------------------------------------------------

    def prompt_block(self) -> str:
        """The facts, shaped for the system prompt.

        Capped, and newest last so that if the model attends unevenly it is the
        most recent corrections that survive.
        """
        if not self._facts:
            return ""
        recent = self._facts[-MAX_FACTS_IN_PROMPT:]
        lines = "\n".join(f"- {fact}" for fact in recent)
        return (
            "\nWhat you know about this operator, because he told you. Treat it "
            "as established fact and do not ask him to repeat it:\n" + lines
        )

    def summary(self) -> str:
        """Spoken answer to 'what do you know about me'."""
        if not self._facts and not self._phrases:
            return "Nothing yet. Tell me something and I will keep it."
        parts = []
        if self._facts:
            parts.append(" ".join(f"{i}. {f}" for i, f in enumerate(self._facts, 1)))
        if self._phrases:
            parts.append(
                f"I have also learned {len(self._phrases)} of your phrasings.")
        return " ".join(parts)
