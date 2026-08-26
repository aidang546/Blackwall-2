"""What has happened, in the operator's own words and the machine's.

Append-only JSONL. Append-only matters: the point of the journal is that it can
quote you on something you would rather it forgot, and a store you can quietly
revise cannot do that.

Entries are deliberately loose - {ts, kind, data} - because what is worth
recording changes as the thing gets used, and a rigid schema would mean a
migration every time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("erebus.briefing.journal")

ROOT = Path(__file__).resolve().parents[2]
JOURNAL_PATH = ROOT / "journal.local.jsonl"


@dataclass
class Entry:
    ts: datetime
    kind: str
    data: dict[str, Any]

    @property
    def day(self) -> date:
        return self.ts.date()


class Journal:
    """Append-only record of briefings given and commitments made."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or JOURNAL_PATH

    # -- writing -------------------------------------------------------------

    def append(self, kind: str, **data: Any) -> Entry:
        entry = Entry(datetime.now(), kind, data)
        line = json.dumps(
            {"ts": entry.ts.isoformat(), "kind": kind, **data}, ensure_ascii=False
        )
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return entry

    # -- reading -------------------------------------------------------------

    def entries(self) -> Iterator[Entry]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    ts = datetime.fromisoformat(raw.pop("ts"))
                    kind = raw.pop("kind")
                except (json.JSONDecodeError, KeyError, ValueError):
                    # One corrupt line must not cost the whole history.
                    log.warning("journal line %d is unreadable, skipping", number)
                    continue
                yield Entry(ts, kind, raw)

    def recent(self, days: int = 14, kind: str | None = None) -> list[Entry]:
        cutoff = datetime.now() - timedelta(days=days)
        return [
            e for e in self.entries()
            if e.ts >= cutoff and (kind is None or e.kind == kind)
        ]

    def last(self, kind: str) -> Entry | None:
        found = None
        for entry in self.entries():
            if entry.kind == kind:
                found = entry
        return found

    # -- derived -------------------------------------------------------------

    def days_since(self, kind: str) -> int | None:
        """Whole days since the last entry of this kind, or None if never."""
        entry = self.last(kind)
        if entry is None:
            return None
        return (date.today() - entry.day).days

    def streak(self, kind: str, *, allow_gap: int = 0) -> int:
        """Consecutive days ending today (or yesterday) with an entry of `kind`.

        `allow_gap` tolerates rest days - a training streak should not reset
        because you correctly took Sunday off.
        """
        days = {e.day for e in self.entries() if e.kind == kind}
        if not days:
            return 0
        cursor = date.today()
        if cursor not in days:
            # Today may simply not have happened yet.
            cursor -= timedelta(days=1)
            if cursor not in days:
                return 0
        count, misses = 0, 0
        while True:
            if cursor in days:
                count += 1
                misses = 0
            else:
                misses += 1
                if misses > allow_gap:
                    return count
            cursor -= timedelta(days=1)
            if count > 3650:      # a decade; something is wrong with the data
                return count

    def counts_by_day(self, kind: str, days: int = 7) -> dict[date, int]:
        out: dict[date, int] = {}
        for entry in self.recent(days, kind):
            out[entry.day] = out.get(entry.day, 0) + 1
        return out

    def as_prompt(self, days: int = 10) -> str:
        """Recent history, for the model to hold you to."""
        entries = self.recent(days)
        if not entries:
            return "No history recorded yet. This is the first briefing."
        lines = []
        for entry in entries[-40:]:
            when = entry.ts.strftime("%a %d %b")
            detail = ", ".join(
                f"{k}={v}" for k, v in entry.data.items() if v not in (None, "", [])
            )
            lines.append(f"  {when}  {entry.kind}: {detail}" if detail
                         else f"  {when}  {entry.kind}")
        return "Recent history:\n" + "\n".join(lines)
