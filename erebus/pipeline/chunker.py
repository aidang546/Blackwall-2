"""Cutting a token stream into speakable pieces.

Piper synthesises a whole string at once, so streaming speech means deciding
where to cut. The tension is simple: cut early and the first word arrives
sooner; cut late and the prosody is better, because Piper chooses intonation
from the whole string it is given.

The compromise here is to cut at real boundaries only - sentence enders always,
clause boundaries once a chunk is long enough to be worth speaking on its own -
and to allow a much shorter first chunk than subsequent ones. The first cut is
the only one the listener experiences as latency; every cut after that happens
while audio is already playing, so it can afford to wait for a better break.
"""

from __future__ import annotations

import re

#: Sentence enders, followed by whitespace or end of string.
_SENTENCE_END = re.compile(r'[.!?…]["\')\]]?(?=\s|$)')

#: Weaker breaks, used only once a chunk is already long enough.
_CLAUSE_END = re.compile(r'[,;:—-](?=\s)')

#: Titles and abbreviations whose full stop does not end a sentence. Short list
#: on purpose - a false negative here costs a slightly late cut, while a false
#: positive costs a sentence spoken in two halves with a gap in the middle.
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "eg",
    "ie", "approx", "no", "fig", "al", "inc", "ltd", "co", "am", "pm",
}

FIRST_CHUNK_MIN = 12      # characters; below this, wait rather than speak a fragment
LATER_CHUNK_MIN = 45      # once audio is playing, hold out for a proper break
CLAUSE_MIN = 60           # only break at a comma past this length
HARD_MAX = 240            # a sentence this long is not arriving; cut it anyway


def _ends_on_abbreviation(text: str) -> bool:
    """True if the full stop at the end of `text` belongs to an abbreviation."""
    match = re.search(r'(\w+)\.$', text)
    if not match:
        return False
    word = match.group(1).lower()
    if word in _ABBREVIATIONS:
        return True
    # Single letters are initials: "J. Smith", and decimals like "3.5".
    return len(word) == 1


def find_break(buffer: str, minimum: int) -> int:
    """Index just past a good cut point, or 0 if the buffer should keep growing."""
    if len(buffer) < minimum:
        return 0

    for match in reversed(list(_SENTENCE_END.finditer(buffer))):
        end = match.end()
        if end >= minimum and not _ends_on_abbreviation(buffer[:end]):
            return end

    if len(buffer) >= CLAUSE_MIN:
        for match in reversed(list(_CLAUSE_END.finditer(buffer))):
            if match.end() >= minimum:
                return match.end()

    if len(buffer) >= HARD_MAX:
        # Break on the last space so a word is never split.
        space = buffer.rfind(" ", 0, HARD_MAX)
        return space + 1 if space > minimum else HARD_MAX

    return 0


async def to_sentences(fragments):
    """Consume a stream of text fragments, yield speakable chunks.

    The first chunk is allowed to be short so speech starts promptly; later
    chunks wait for a cleaner break, since by then nobody is waiting on them.
    """
    buffer = ""
    first = True

    async for fragment in fragments:
        buffer += fragment
        while True:
            minimum = FIRST_CHUNK_MIN if first else LATER_CHUNK_MIN
            cut = find_break(buffer, minimum)
            if not cut:
                break
            chunk = buffer[:cut].strip()
            buffer = buffer[cut:].lstrip()
            if chunk:
                yield chunk
                first = False

    tail = buffer.strip()
    if tail:
        yield tail
