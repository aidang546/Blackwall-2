"""Asking instead of guessing.

Erebus used to require the exact configured phrase: "open spotify" worked,
"open" fell through to conversation, and "open the music thing" got a chat
reply about music. That makes the assistant a vocabulary to memorise rather
than something to talk to.

Now an unresolved request to open something is answered with a question. The
part that needs guarding is the other side of it: this must not hijack ordinary
conversation, and answering the question must never reach anything that is not
already in the registry.

    python tests/test_clarify.py
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from erebus.actions.registry import (          # noqa: E402
    Registry, has_launch_prefix, is_bare_launch_verb, normalize,
)
from erebus.core.assistant import Assistant     # noqa: E402
from erebus.core.bus import EventBus            # noqa: E402
from erebus.core.config import Config           # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {label:<52} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label:<52} {detail}")


config = Config.load()
reg = Registry(config)

print("\nRECOGNISING A REQUEST WITH ITS OBJECT MISSING")
for verb in ("open", "launch", "start", "run", "put on", "fire up"):
    check(f"{verb!r} is a request to open something",
          is_bare_launch_verb(normalize(verb)))
for other in ("opens", "openly", "what is the weather", "", "spotify"):
    check(f"{other!r} is not", not is_bare_launch_verb(normalize(other)))

print("\nWHAT IT OFFERS")
options = reg.candidates("open")
check("a bare verb offers things that are opened",
      options and all(a.kind in ("app", "macro") for a in options),
      ", ".join(a.name for a in options))
check("and never offers to shut the machine down",
      not any(a.name in ("shutdown", "restart", "purge") for a in options))

for said, want in [("open the music thing", "spotify"),
                   ("start the game", "gaming_mode"),
                   ("open something to write code", "vscode"),
                   ("i want to work", "work_mode")]:
    got = [a.name for a in reg.candidates(said)]
    check(f"{said!r} suggests {want}", want in got, ", ".join(got) or "nothing")

print("\nCHOOSING FROM WHAT WAS OFFERED")
apps = reg.candidates("open")
check("by name", reg.choose("spotify", apps).name == "spotify")
check("by an alias it was read out under",
      reg.choose("the internet", apps).name == "browser")
check("by position", reg.choose("the second one", apps).name == apps[1].name)
check("by bare number", reg.choose("three", apps).name == apps[2].name)
check("'last' picks the last", reg.choose("last", apps).name == apps[-1].name)
check("nonsense chooses nothing", reg.choose("banana split", apps) is None)
check("an empty answer chooses nothing", reg.choose("", apps) is None)
check("no options means no choice", reg.choose("spotify", []) is None)
# The invariant: a choice is a selection from a list, never a new command.
check("it can only return one of the options offered",
      all(reg.choose(word, apps) in apps or reg.choose(word, apps) is None
          for word in ("spotify", "steam", "shutdown", "format the disk")),
      "including when asked for something not on the list")
check("something off the list is not smuggled in",
      reg.choose("shut down the computer", apps) is None
      or reg.choose("shut down the computer", apps) in apps)

print("\nIT MUST NOT HIJACK CONVERSATION")
for chat in ("what is the weather", "why is the sky blue", "tell me a joke",
             "how am i doing", "i am tired"):
    check(f"{chat!r} is not treated as a launch request",
          not has_launch_prefix(normalize(chat)))


# -- the whole exchange, with no model and no audio -------------------------

class Harness:
    def __init__(self):
        self.said: list[str] = []
        self.ran: list[str] = []

    async def build(self):
        cfg = Config.load()
        cfg._data.setdefault("brain", {})["backend"] = "echo"
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        self.a = Assistant(cfg, bus)
        self.a.say = self._say
        self.a.say_stream = lambda frags: self._say("<conversation>")

        async def run(action, value=None, say=None):
            self.ran.append(action.name)
            return ""

        self.a.registry.run = run
        return self.a

    async def _say(self, text):
        self.said.append(text)

    async def turn(self, text):
        self.said.clear()
        before = len(self.ran)
        await self.a._handle(text)
        return " ".join(self.said), self.ran[before:]


async def exchanges():
    print("\nTHE EXCHANGE")
    h = Harness()
    await h.build()

    spoken, ran = await h.turn("open")
    check("a bare 'open' asks rather than guessing",
          "which" in spoken.lower() and not ran, spoken)
    spoken, ran = await h.turn("spotify")
    check("and the answer launches it", ran == ["spotify"], str(ran))

    await h.turn("open")
    spoken, ran = await h.turn("the second one")
    check("a position answers it too", len(ran) == 1, str(ran))

    await h.turn("open")
    spoken, ran = await h.turn("never mind")
    check("and it can be dropped", not ran and "dropped" in spoken.lower(), spoken)

    spoken, ran = await h.turn("open the music thing")
    check("one likely match is offered as a question",
          spoken.strip().endswith("?") and not ran, spoken)
    spoken, ran = await h.turn("yes")
    check("which a yes answers", ran == ["spotify"], str(ran))

    spoken, ran = await h.turn("open spotify")
    check("an unambiguous command still just runs", ran == ["spotify"], str(ran))

    spoken, ran = await h.turn("what is the weather")
    check("a question is still a question", not ran, spoken)

    # An unanswered question must not linger and catch a later stray word.
    await h.turn("open")
    h.a._pending_choice = (h.a._pending_choice[0], 0.0)      # expire it
    spoken, ran = await h.turn("steam")
    check("an expired question does not swallow the next utterance",
          ran == ["steam"], "matched normally, not as an answer")

    # Answering with something unrelated should fall through, not insist.
    await h.turn("open")
    spoken, ran = await h.turn("volume up")
    check("a different command escapes the question",
          ran == ["volume_up"], str(ran))

    await h.a.stop()


asyncio.run(exchanges())

print(f"\n  {PASSED}/{PASSED + FAILED} passed")
raise SystemExit(1 if FAILED else 0)
