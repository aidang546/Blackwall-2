"""The OPSEC and OSINT surface exposed to voice and to the CLI.

Kept apart from the modules themselves so those stay usable as libraries -
`archive.Archivist` should not need an assistant to run.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..osint import archive as archive_mod
from . import guard as guard_mod
from .vault import KEY_PATH, Vault

log = logging.getLogger("erebus.opsec.actions")

ROOT = Path(__file__).resolve().parents[2]

#: Files the vault covers and `security` reports on.
PROTECTED = [
    ROOT / "profile.local.yaml",
    ROOT / "journal.local.jsonl",
    ROOT / "health.local.jsonl",
]


def security_report(audit, listening, vault: Vault) -> str:
    """A plain statement of the current posture. Read-only."""
    lines = []

    intact, message = audit.verify()
    lines.append(f"Audit chain: {'intact' if intact else 'BROKEN'}. {message}")

    lines.append(
        f"Microphone: {'live' if listening.active else 'STOOD DOWN'}."
    )

    if vault.enabled and vault.ready:
        states = vault.status(PROTECTED)
        plaintext = [s["file"] for s in states if s["state"] == "PLAINTEXT"]
        if plaintext:
            lines.append(
                "Vault is on but these are still plaintext: "
                + ", ".join(plaintext)
                + ". Run `erebus vault --encrypt`."
            )
        else:
            present = [s["file"] for s in states if s["state"] == "encrypted"]
            lines.append(
                f"Vault: {len(present)} file(s) encrypted."
                if present else "Vault: on, nothing stored yet."
            )
        if KEY_PATH.exists():
            lines.append(f"Key: {KEY_PATH.name}, machine-bound.")
    else:
        lines.append("Vault: OFF. Personal files are plaintext on disk.")

    token = ROOT / ".erebus_token"
    if token.exists():
        import time

        age_days = int((time.time() - token.stat().st_mtime) // 86400)
        lines.append(
            f"Access token: {age_days} days old."
            + ("  Consider rotating it." if age_days > 90 else "")
        )

    recent = audit.tail(limit=200, kind="auth.denied")
    if recent:
        lines.append(f"Rejected connections on record: {len(recent)}.")

    cases = archive_mod.list_cases()
    if cases:
        lines.append(f"Case files: {', '.join(cases)}.")

    return "\n".join(lines)


def spoken_summary(audit, listening, vault: Vault) -> str:
    """The same posture, short enough to say out loud."""
    intact, _ = audit.verify()
    parts = []
    if not intact:
        parts.append("The audit chain is broken. Something altered the record.")
    if not listening.active:
        parts.append("I am stood down.")
    if not (vault.enabled and vault.ready):
        parts.append("The vault is off. Your files are in the clear.")
    else:
        plaintext = [s for s in vault.status(PROTECTED) if s["state"] == "PLAINTEXT"]
        if plaintext:
            parts.append(f"{len(plaintext)} files are still unencrypted.")
    if not parts:
        return "Chain intact, vault sealed, microphone live. Nothing to report."
    return " ".join(parts)
