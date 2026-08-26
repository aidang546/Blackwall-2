"""A tamper-evident record of everything Erebus did and everyone who reached it.

Append-only JSONL, hash-chained: each entry carries the SHA-256 of the previous
line, so altering or removing any entry breaks every hash after it. That does
not make the log unalterable - anything running as you can rewrite the whole
chain - but it does make quiet edits detectable, which is the realistic threat.
Someone tampering with one line to hide one action will not usually rebuild the
chain behind it.

The same property is what makes it usable as a provenance record for
investigative work: an entry saying a page was archived at a given time is
worth something only if the log around it can be shown to be intact.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("erebus.audit")

ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "audit.local.jsonl"

#: The chain has to start somewhere.
GENESIS = "0" * 64


@dataclass
class Record:
    ts: datetime
    kind: str
    prev: str
    digest: str
    data: dict[str, Any]


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    """Append-only, hash-chained. Safe to call from any thread."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or AUDIT_PATH
        self._lock = threading.Lock()
        self._tip: str | None = None

    # -- writing -------------------------------------------------------------

    def _load_tip(self) -> str:
        """Digest of the last line, or GENESIS for an empty log."""
        if self._tip is not None:
            return self._tip
        tip = GENESIS
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        tip = _digest(line)
        self._tip = tip
        return tip

    def record(self, kind: str, /, **data: Any) -> str:
        """Append an entry. Returns its digest.

        `kind` is positional-only so a payload may legitimately carry a field
        called "kind" - an action's own category, for instance. The same
        collision bit the event bus, and the fix is the same: reserve the
        envelope names structurally rather than hoping callers avoid them.

        Never raises: an assistant that refuses to act because it could not
        write its own log would be worse than one that acts unlogged, and the
        failure is loud in the application log either way.
        """
        try:
            with self._lock:
                prev = self._load_tip()
                # Envelope keys are written last so a payload field can never
                # shadow the entry's own identity or break the chain.
                entry = {
                    **data,
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "kind": kind,
                    "prev": prev,
                }
                line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())   # a log lost to a crash is not a log
                self._tip = _digest(line)
                return self._tip
        except OSError as exc:
            log.error("could not write audit entry (%s): %s", kind, exc)
            return ""

    # -- reading -------------------------------------------------------------

    def entries(self) -> Iterator[Record]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    ts = datetime.fromisoformat(raw.pop("ts"))
                    kind = raw.pop("kind")
                    prev = raw.pop("prev")
                except (json.JSONDecodeError, KeyError, ValueError):
                    log.warning("unreadable audit line, skipping")
                    continue
                yield Record(ts, kind, prev, _digest(line), raw)

    def verify(self) -> tuple[bool, str]:
        """Walk the chain. Returns (intact, human-readable explanation)."""
        if not self.path.exists():
            return True, "No audit log yet."

        expected = GENESIS
        count = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for number, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    return False, f"Line {number} is not valid JSON."
                if raw.get("prev") != expected:
                    return False, (
                        f"Chain breaks at line {number}: it follows "
                        f"{str(raw.get('prev'))[:12]}… but the previous line "
                        f"hashes to {expected[:12]}…. An entry was altered or "
                        "removed at or before this point."
                    )
                expected = _digest(line)
        return True, f"Chain intact across {count} entries."

    def tail(self, limit: int = 20, kind: str | None = None) -> list[Record]:
        found = [e for e in self.entries() if kind is None or e.kind == kind]
        return found[-limit:]
