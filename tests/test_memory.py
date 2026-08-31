"""Remembering, and being corrected.

Two kinds of memory, arriving two ways. Phrases are learned by being corrected
- every "which one?" that gets an answer is a labelled example, and writing it
down is what stops it asking again. Facts are told outright and go into the
system prompt, so the model knows them rather than being reminded of them.

The parts that need guarding are the edges: what survives a restart, what
happens when the store is damaged, and whether a wrong belief can actually be
removed.

    python tests/test_memory.py
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)

from erebus import memory as mem_mod             # noqa: E402
from erebus.actions.registry import Registry     # noqa: E402
from erebus.core.assistant import Assistant      # noqa: E402
from erebus.core.bus import EventBus             # noqa: E402
from erebus.core.config import Config            # noqa: E402
from erebus.memory import Memory                 # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok    {label:<52} {detail}")
    else:
        FAILED += 1
        print(f"  FAIL  {label:<52} {detail}")


tmp = pathlib.Path(tempfile.mkdtemp())
store = tmp / "memory.local.jsonl"

print("\nPHRASES")
m = Memory(store)
m.learn_phrase("open the music thing", "spotify")
check("a taught phrase is recalled", m.phrase_for("open the music thing") == "spotify")
check("casing and punctuation do not matter",
      m.phrase_for("Open the music thing!") == "spotify")
check("an unknown phrase is not invented", m.phrase_for("open the door") is None)
m.learn_phrase("open the music thing", "browser")
check("being corrected twice keeps the newer answer",
      m.phrase_for("open the music thing") == "browser")
m.learn_phrase("go", "spotify")
check("a phrase too short to be safe is not learned",
      m.phrase_for("go") is None, "single words collide with everything")
# Answering "which one?" after a bare "open" does not make "open" mean that
# thing forever - it means the question was answered that time. Pinning it
# would replace a useful question with a wrong guess.
for verb in ("open", "launch", "put on"):
    m.learn_phrase(verb, "spotify")
    check(f"a bare {verb!r} is never pinned to one answer",
          m.phrase_for(verb) is None)
m.learn_phrase("something long enough", "")
check("a phrase with no action is not learned",
      m.phrase_for("something long enough") is None)

print("\nFACTS")
m.remember("remember that I train on Tuesdays and Thursdays")
check("the opener is stripped, the rest kept verbatim",
      m.facts == ["I train on Tuesdays and Thursdays"], str(m.facts))
m.remember("my channel is about urban exploration")
check("a fact with no opener is kept as-is", len(m.facts) == 2)
m.remember("I train on Tuesdays and Thursdays")
check("the same fact twice is stored once", len(m.facts) == 2)
check("an empty fact stores nothing", m.remember("   ") == "" and len(m.facts) == 2)

block = m.prompt_block()
check("facts reach the system prompt", "urban exploration" in block)
check("and are framed as established, not as a reminder",
      "do not ask him to repeat" in block)
check("an empty memory contributes no prompt at all",
      Memory(tmp / "empty.jsonl").prompt_block() == "")

print("\nBEING WRONG, AND BEING TOLD SO")
check("forgetting matches on content",
      m.forget("urban exploration") == 1, str(m.facts))
check("and the fact is gone", not any("urban" in f for f in m.facts))
check("the other fact survives", len(m.facts) == 1)
check("forgetting something it never knew changes nothing",
      m.forget("my birthday") == 0)
check("forgetting nothing in particular is not a wildcard",
      m.forget("") == 0, "an empty query must not wipe the store")

print("\nACROSS A RESTART")
again = Memory(store)
check("facts survive", again.facts == m.facts, str(again.facts))
check("phrases survive", again.phrase_for("open the music thing") == "browser")
check("a forget survives too", not any("urban" in f for f in again.facts))

print("\nA DAMAGED STORE")
broken = tmp / "broken.jsonl"
broken.write_text(
    '{"kind":"fact","key":"","value":"good one"}\n'
    "this line is not json at all\n"
    '{"kind":"fact","key":"","value":"second good one"}\n'
)
salvaged = Memory(broken)
check("one unreadable line does not cost the rest",
      salvaged.facts == ["good one", "second good one"], str(salvaged.facts))

missing = Memory(tmp / "does-not-exist.jsonl")
check("a store that does not exist yet is simply empty", missing.facts == [])

print("\nCOMMANDS THAT CARRY THE REST OF THE SENTENCE")
reg = Registry(Config.load())
hit = reg.match("remember that I train on Tuesdays and Thursdays")
check("a long sentence still routes to remember",
      hit is not None and hit.action.name == "remember",
      hit.action.name if hit else "nothing")
check("and carries the operator's own words, not a normalised copy",
      hit.value == "I train on Tuesdays and Thursdays", repr(hit.value))
hit = reg.match("forget about urban exploration")
check("forget carries its argument too",
      hit is not None and hit.value == "urban exploration", repr(hit.value))
check("a bare 'remember' still routes, with nothing to store",
      reg.match("remember") is not None)
# The coverage rule that rejects passing mentions must not be undone for
# everything else.
check("an ordinary command is still judged on coverage",
      reg.match("i am going to lock up now some time later today") is None)


# -- the exchange -----------------------------------------------------------

async def exchange():
    print("\nTEACHING IT BY ANSWERING ONCE")
    mem_mod.MEMORY_PATH = tmp / "assistant.jsonl"
    cfg = Config.load()
    cfg._data.setdefault("brain", {})["backend"] = "echo"
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    a = Assistant(cfg, bus)
    said, ran = [], []

    async def say(text):
        said.append(text)

    a.say = say
    a.say_stream = lambda frags: say("<conversation>")
    real_run = a.registry.run

    async def run(action, value=None, say=None):
        ran.append(action.name)
        if action.kind == "builtin":
            return await real_run(action, value, say=say)
        return ""

    a.registry.run = run

    async def turn(text):
        said.clear()
        before = len(ran)
        await a._handle(text)
        return " ".join(said), ran[before:]

    spoken, did = await turn("open the music thing")
    check("an unknown wording is asked about", "?" in spoken and not did, spoken)
    spoken, did = await turn("yes")
    check("answering runs it", did == ["spotify"], str(did))
    spoken, did = await turn("open the music thing")
    check("and it is never asked again", did == ["spotify"] and "?" not in spoken,
          "learned from one correction")

    spoken, did = await turn("remember that I train on Tuesdays")
    check("a fact is taken", "held" in spoken.lower(), spoken)
    check("and reaches the model's prompt",
          "Tuesdays" in a.brain.persona, "in the persona, not the chat history")
    spoken, _ = await turn("what do you know about me")
    check("it can say what it thinks it knows", "Tuesdays" in spoken, spoken)

    spoken, _ = await turn("forget about Tuesdays")
    check("and a wrong belief can be removed", "dropped" in spoken.lower(), spoken)
    check("the prompt drops it too", "Tuesdays" not in a.brain.persona)

    base = a._base_persona
    await turn("remember that I film on Sundays")
    await turn("remember that I edit on Mondays")
    check("facts do not accumulate into the persona twice",
          a.brain.persona.count(base[:40]) == 1,
          "rebuilt from the base each time")

    await a.stop()


asyncio.run(exchange())

print(f"\n  {PASSED}/{PASSED + FAILED} passed")
raise SystemExit(1 if FAILED else 0)
