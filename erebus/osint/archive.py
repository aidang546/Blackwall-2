"""Capture a page, and be able to prove later what it said.

The core problem in investigative work is that the web is mutable: the post you
are writing about gets edited, the page gets pulled, the company quietly
rewrites its policy. A screenshot proves nothing - anyone can make one.

What this does instead, for a given URL:

  * fetches it and stores the exact bytes received;
  * hashes those bytes with SHA-256, so any later change is demonstrable;
  * records the response headers, status and time, which is where a
    Last-Modified or an ETag often turns out to matter;
  * asks the Wayback Machine to take its own independent copy, so the claim
    does not rest solely on a file on your disk;
  * writes a provenance record into the case file and the audit chain.

None of this makes a capture legally authoritative on its own. What it does is
make it checkable: an independent third-party snapshot taken the same minute,
plus a hash chain that would have to be rebuilt to alter the record quietly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("erebus.osint.archive")

ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = ROOT / "cases"

WAYBACK_SAVE = "https://web.archive.org/save/"
WAYBACK_AVAILABLE = "https://archive.org/wayback/available"

#: Identify ourselves rather than pretending to be a browser. Investigative
#: tooling that lies about who it is invites being blocked and is harder to
#: defend if the collection is ever questioned.
USER_AGENT = "Erebus/0.1 (research archiving; +local)"

_SAFE = re.compile(r"[^a-z0-9._-]+")


def slugify(text: str, limit: int = 60) -> str:
    return _SAFE.sub("-", text.lower()).strip("-")[:limit] or "untitled"


@dataclass
class Capture:
    url: str
    fetched_at: str
    status: int | None = None
    sha256: str | None = None
    bytes: int | None = None
    content_type: str | None = None
    final_url: str | None = None
    redirects: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    wayback_url: str | None = None
    wayback_note: str | None = None
    stored_at: str | None = None
    title: str | None = None
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"{self.url} — capture failed: {self.error}"
        lines = [
            f"{self.title or self.url}",
            f"  fetched   {self.fetched_at}",
            f"  status    {self.status}  {self.content_type or ''}",
            f"  sha256    {self.sha256}",
            f"  bytes     {self.bytes}",
        ]
        if self.redirects:
            lines.append(f"  redirects {' -> '.join(self.redirects)}")
        if self.wayback_url:
            lines.append(f"  wayback   {self.wayback_url}")
        elif self.wayback_note:
            lines.append(f"  wayback   {self.wayback_note}")
        if self.stored_at:
            lines.append(f"  stored    {self.stored_at}")
        return "\n".join(lines)


def _title_of(body: bytes) -> str | None:
    match = re.search(rb"<title[^>]*>(.*?)</title>", body[:200_000],
                      re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    text = match.group(1).decode("utf-8", errors="replace")
    return re.sub(r"\s+", " ", text).strip()[:200] or None


class Archivist:
    """Captures pages into a case folder with provenance."""

    def __init__(self, case: str = "inbox", audit=None,
                 timeout: float = 30.0) -> None:
        self.case = slugify(case)
        self.audit = audit
        self.timeout = timeout

    @property
    def directory(self) -> Path:
        return CASES_DIR / self.case

    async def capture(self, url: str, wayback: bool = True) -> Capture:
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url

        record = Capture(
            url=url,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                response = await client.get(url)
                body = response.content

                record.status = response.status_code
                record.sha256 = hashlib.sha256(body).hexdigest()
                record.bytes = len(body)
                record.content_type = response.headers.get("content-type")
                record.final_url = str(response.url)
                record.redirects = [str(r.url) for r in response.history]
                # Keep the headers that carry evidentiary weight; the rest is noise.
                record.headers = {
                    k: v for k, v in response.headers.items()
                    if k.lower() in {
                        "date", "last-modified", "etag", "server",
                        "content-type", "content-length", "location",
                        "x-powered-by", "cf-ray", "via", "age",
                    }
                }
                record.title = _title_of(body)
                record.stored_at = str(self._store(record, body))
        except Exception as exc:  # noqa: BLE001 - any transport failure
            record.error = f"{type(exc).__name__}: {exc}"
            log.error("capture failed for %s: %s", url, record.error)

        if wayback and not record.error:
            await self._wayback(record)

        self._write_manifest(record)
        if self.audit is not None:
            self.audit.record(
                "osint.capture", url=url, case=self.case,
                sha256=record.sha256, status=record.status,
                wayback=record.wayback_url, error=record.error,
            )
        return record

    def _store(self, record: Capture, body: bytes) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = record.fetched_at.replace(":", "").replace("-", "")
        name = f"{stamp}-{slugify(record.title or record.url, 48)}"
        suffix = ".html"
        if record.content_type:
            if "json" in record.content_type:
                suffix = ".json"
            elif "pdf" in record.content_type:
                suffix = ".pdf"
            elif "text/plain" in record.content_type:
                suffix = ".txt"
        path = self.directory / f"{name}{suffix}"
        path.write_bytes(body)
        return path

    async def _wayback(self, record: Capture) -> None:
        """Ask the Internet Archive for an independent copy.

        Best effort by design: the save endpoint is rate limited and often slow,
        and a failure here must not cost the local capture. Any existing
        snapshot is reported too, since one predating your interest is often
        the more useful artifact.
        """
        try:
            async with httpx.AsyncClient(
                timeout=20.0, follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                existing = await client.get(
                    WAYBACK_AVAILABLE, params={"url": record.url}
                )
                if existing.status_code == 200:
                    snapshot = (existing.json()
                                .get("archived_snapshots", {})
                                .get("closest", {}))
                    if snapshot.get("available"):
                        record.wayback_url = snapshot.get("url")
                        record.wayback_note = (
                            f"existing snapshot from {snapshot.get('timestamp')}"
                        )

                saved = await client.get(WAYBACK_SAVE + record.url)
                if saved.status_code in (200, 302):
                    record.wayback_url = str(saved.url)
                    record.wayback_note = "saved now"
        except Exception as exc:  # noqa: BLE001
            record.wayback_note = f"not archived ({type(exc).__name__})"
            log.warning("wayback unavailable for %s: %s", record.url, exc)

    def _write_manifest(self, record: Capture) -> None:
        """One JSON line per capture: the case file's index."""
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = self.directory / "manifest.jsonl"
        with open(manifest, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    # -- reading back --------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        manifest = self.directory / "manifest.jsonl"
        if not manifest.exists():
            return []
        out = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def verify(self) -> list[dict[str, Any]]:
        """Re-hash every stored file and compare against the manifest.

        Catches a file altered on disk after collection - including by you,
        which is the point. An investigator who cannot show their own working
        copy is unmodified has a weaker record than one who can.
        """
        results = []
        for entry in self.entries():
            stored = entry.get("stored_at")
            expected = entry.get("sha256")
            if not stored or not expected:
                continue
            path = Path(stored)
            if not path.exists():
                results.append({"file": path.name, "state": "MISSING"})
                continue
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            results.append({
                "file": path.name,
                "state": "ok" if actual == expected else "ALTERED",
                "sha256": actual,
            })
        return results


def list_cases() -> list[str]:
    if not CASES_DIR.exists():
        return []
    return sorted(d.name for d in CASES_DIR.iterdir() if d.is_dir())
