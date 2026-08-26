"""Access control, standing down, and burning it all.

Three concerns that share one idea: the operator must be able to change what
Erebus can do, right now, without editing a file or restarting anything.

Standing down and purging exist for a specific reason. An assistant that always
listens is a microphone in the room, and an assistant that remembers everything
is a record of who you spoke to. For an investigator those are not abstract
risks, so both need to be one spoken sentence away.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("erebus.guard")

ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = ROOT / ".erebus_token"

#: Files a purge destroys. Everything personal, nothing structural.
PURGEABLE = [
    ROOT / "journal.local.jsonl",
    ROOT / "health.local.jsonl",
    ROOT / "audit.local.jsonl",
]


@dataclass
class Lockout:
    """Rate limiting on authentication.

    Without this the token is a password with unlimited guesses from anywhere
    on the network. A 24-character urlsafe token is not realistically brute
    forced, but a short one someone set by hand is, and the cost of the guard
    is nothing.

    Failures are tracked per address so one clumsy device cannot lock out the
    rest of the house.
    """

    max_attempts: int = 5
    window: float = 300.0      # seconds over which failures accumulate
    penalty: float = 900.0     # how long a locked-out address stays out

    _failures: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    _locked: dict[str, float] = field(default_factory=dict)

    def is_locked(self, address: str) -> bool:
        until = self._locked.get(address)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._locked[address]
            self._failures.pop(address, None)
            return False
        return True

    def seconds_remaining(self, address: str) -> int:
        until = self._locked.get(address)
        return max(0, int(until - time.monotonic())) if until else 0

    def record_failure(self, address: str) -> bool:
        """Note a failed attempt. Returns True if this one caused a lockout."""
        now = time.monotonic()
        recent = [t for t in self._failures[address] if now - t < self.window]
        recent.append(now)
        self._failures[address] = recent
        if len(recent) >= self.max_attempts:
            self._locked[address] = now + self.penalty
            log.warning(
                "locked out %s for %.0f minutes after %d failed attempts",
                address, self.penalty / 60, len(recent),
            )
            return True
        return False

    def record_success(self, address: str) -> None:
        self._failures.pop(address, None)
        self._locked.pop(address, None)


def rotate_token() -> str:
    """Issue a new token, invalidating every paired device immediately."""
    token = secrets.token_urlsafe(24)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    try:
        import os

        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    log.warning("token rotated - every paired device must be re-paired")
    return token


def purge(paths: list[Path] | None = None) -> list[str]:
    """Delete the personal record. Returns what was removed.

    Deliberately not a secure wipe: on an SSD with wear levelling, overwriting
    a file does not reliably destroy the old blocks, and claiming otherwise
    would be a lie the operator might rely on. Full-disk encryption is what
    makes deletion meaningful, and the vault is what makes the files useless
    before deletion.
    """
    removed = []
    for path in paths or PURGEABLE:
        if path.exists():
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                log.error("could not remove %s: %s", path.name, exc)
    log.warning("purged: %s", ", ".join(removed) or "nothing")
    return removed


class Listening:
    """Whether the microphone is live.

    Standing down is not the same as muting the speakers. This stops audio
    being captured at all, and the wall shows it, because a listening indicator
    you cannot trust is worse than none.
    """

    def __init__(self) -> None:
        self._active = True
        self._since = time.time()

    @property
    def active(self) -> bool:
        return self._active

    def stand_down(self) -> str:
        self._active = False
        self._since = time.time()
        log.warning("standing down - microphone capture disabled")
        return "Standing down. I am not listening."

    def resume(self) -> str:
        self._active = True
        self._since = time.time()
        log.info("listening resumed")
        return "Listening."

    @property
    def state(self) -> dict:
        return {"listening": self._active, "since": self._since}
