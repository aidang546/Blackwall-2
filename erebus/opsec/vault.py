"""Encryption at rest for anything personal.

Covers the profile, the journal, health data and case notes - an investigator's
sources and an operator's numbers. AES-256-GCM, so tampering is detected rather
than silently decrypted into something wrong.

The key is machine-bound, not passphrase-derived. On Windows it is sealed with
DPAPI against your user account; elsewhere it is a 0600 key file. Be clear
about what that buys and what it does not:

  * A copied folder, a pulled drive, a stolen backup, a synced cloud directory,
    an accidental commit - all useless without the machine.
  * Malware already running as you can ask DPAPI to unseal it exactly as Erebus
    does. Machine-bound encryption is not a defence against that, and pretending
    otherwise would be worse than leaving the files in plaintext, because you
    would trust them further than you should.

Passphrase-derived keys close that second gap, at the cost of typing one on
every boot and breaking autostart. That is a real trade and it is the operator's
to make - `vault.mode: passphrase` is left as the escape hatch.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("erebus.vault")

ROOT = Path(__file__).resolve().parents[2]
KEY_PATH = ROOT / ".erebus_key"

def _crypto_works() -> bool:
    """Import AND exercise AES-GCM.

    An import check alone is not enough: `cryptography` ships a Rust/CFFI
    backend that can import cleanly and then panic on first use when its
    bindings are half-installed - which is exactly what a broken system
    package looks like. Discovering that at the moment we try to seal the
    operator's notes would mean either a crash or, worse, a silent fallback
    to plaintext. A round trip here costs microseconds once at startup.
    """
    try:
        import secrets as _secrets

        key = _secrets.token_bytes(32)
        nonce = _secrets.token_bytes(12)
        probe = AESGCM(key)
        return probe.decrypt(nonce, probe.encrypt(nonce, b"probe", None), None) == b"probe"
    except Exception as exc:  # noqa: BLE001 - ImportError, PanicException, anything
        log.error(
            "the `cryptography` backend is present but not working (%s). "
            "The vault cannot be used. Try: pip install --force-reinstall "
            "cffi cryptography", type(exc).__name__,
        )
        return False


try:  # pragma: no cover - optional until someone turns the vault on
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    CRYPTO_AVAILABLE = _crypto_works()
except Exception:  # noqa: BLE001 - a broken backend can raise more than ImportError
    AESGCM = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

IS_WINDOWS = sys.platform == "win32"

#: Prefix on every file we wrote, so an encrypted file is never mistaken for a
#: corrupt plaintext one.
MAGIC = b"EREBUS1\n"


# --------------------------------------------------------------------------
#  Key material
# --------------------------------------------------------------------------

def _dpapi(data: bytes, unprotect: bool = False) -> bytes | None:
    """Seal or unseal bytes with Windows DPAPI, bound to the current user."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(raw: bytes) -> BLOB:
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    source = to_blob(data)
    result = BLOB()
    function = (ctypes.windll.crypt32.CryptUnprotectData if unprotect
                else ctypes.windll.crypt32.CryptProtectData)
    args = ([ctypes.byref(source), None, None, None, None, 0,
             ctypes.byref(result)] if unprotect else
            [ctypes.byref(source), None, None, None, None, 0,
             ctypes.byref(result)])
    if not function(*args):
        log.error("DPAPI %s failed", "unprotect" if unprotect else "protect")
        return None
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def load_or_create_key() -> bytes | None:
    """Return the 32-byte key, creating and sealing one on first use."""
    if not CRYPTO_AVAILABLE:
        log.error("the `cryptography` package is required for the vault")
        return None

    if KEY_PATH.exists():
        blob = KEY_PATH.read_bytes()
        if blob.startswith(b"DPAPI:"):
            key = _dpapi(blob[6:], unprotect=True)
            if key is None:
                log.error(
                    "could not unseal %s - it was sealed by a different "
                    "Windows account or on a different machine",
                    KEY_PATH.name,
                )
            return key
        if blob.startswith(b"RAW:"):
            return blob[4:]
        log.error("%s is not a recognised key file", KEY_PATH.name)
        return None

    key = secrets.token_bytes(32)
    sealed = _dpapi(key) if IS_WINDOWS else None
    if sealed is not None:
        KEY_PATH.write_bytes(b"DPAPI:" + sealed)
        log.info("vault key created and sealed to this Windows account")
    else:
        KEY_PATH.write_bytes(b"RAW:" + key)
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
        if IS_WINDOWS:
            log.warning("DPAPI unavailable - key stored unsealed in %s",
                        KEY_PATH.name)
        else:
            log.info("vault key created at %s (owner-only)", KEY_PATH.name)
    return key


# --------------------------------------------------------------------------
#  Encrypt / decrypt
# --------------------------------------------------------------------------

class Vault:
    """Encrypted read/write for the project's personal files."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and CRYPTO_AVAILABLE
        self._key: bytes | None = None

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        if self._key is None:
            self._key = load_or_create_key()
        return self._key is not None

    def encrypt(self, plaintext: bytes) -> bytes:
        """MAGIC || nonce || ciphertext. The nonce is fresh every time."""
        if not self.ready:
            return plaintext
        nonce = secrets.token_bytes(12)
        sealed = AESGCM(self._key).encrypt(nonce, plaintext, None)
        return MAGIC + nonce + sealed

    def decrypt(self, blob: bytes) -> bytes | None:
        """Returns plaintext, or the input unchanged if it was never encrypted.

        None means the file *is* ours but would not authenticate - a wrong key
        or a modified file. That is deliberately distinguished from "plaintext":
        silently returning garbage for a tampered file is how corrupt data ends
        up in a report.
        """
        if not blob.startswith(MAGIC):
            return blob
        if not self.ready:
            log.error("file is encrypted but the vault key is unavailable")
            return None
        body = blob[len(MAGIC):]
        try:
            return AESGCM(self._key).decrypt(body[:12], body[12:], None)
        except Exception:  # noqa: BLE001 - InvalidTag and friends
            log.error("decryption failed: wrong key, or the file was altered")
            return None

    # -- file helpers --------------------------------------------------------

    def read_text(self, path: Path) -> str | None:
        if not path.exists():
            return None
        plain = self.decrypt(path.read_bytes())
        return None if plain is None else plain.decode("utf-8")

    def write_text(self, path: Path, text: str) -> None:
        payload = self.encrypt(text.encode("utf-8"))
        # Write beside and replace, so an interrupted write cannot leave a
        # half-encrypted file where the original used to be.
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)

    def append_line(self, path: Path, line: str) -> None:
        """Append to a line-oriented file.

        Each line is sealed independently and base64'd onto one physical line,
        so appending never requires rewriting - and never requires holding the
        whole journal in memory to add one entry.
        """
        if not self.ready:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return
        sealed = base64.b64encode(self.encrypt(line.encode("utf-8"))).decode()
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(sealed + "\n")

    def read_lines(self, path: Path) -> list[str]:
        """Read a file written by append_line, encrypted or not."""
        if not path.exists():
            return []
        out: list[str] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            if raw.startswith("{"):
                out.append(raw)          # plaintext, from before the vault
                continue
            try:
                plain = self.decrypt(base64.b64decode(raw, validate=True))
            except Exception:  # noqa: BLE001 - not base64 after all
                out.append(raw)
                continue
            if plain is not None:
                out.append(plain.decode("utf-8"))
        return out

    # -- migration -----------------------------------------------------------

    def status(self, paths: list[Path]) -> list[dict[str, Any]]:
        """Report which protected files are actually sealed.

        Determined by looking for the magic bytes, never by guessing from the
        first character. An earlier version treated "does not start with { or ["
        as encrypted, which called every plaintext YAML file sealed - and a
        security report that says your notes are protected when they are not is
        worse than having no report.
        """
        report = []
        for path in paths:
            if not path.exists():
                report.append({"file": path.name, "state": "absent"})
                continue

            with path.open("rb") as fh:
                head = fh.read(len(MAGIC))
                encrypted = head == MAGIC
                if not encrypted:
                    # Line-oriented files are sealed per line and base64'd, so
                    # decode the first line and look for the magic there.
                    fh.seek(0)
                    first = fh.readline().strip()
                    if first:
                        try:
                            encrypted = base64.b64decode(
                                first, validate=True
                            ).startswith(MAGIC)
                        except Exception:  # noqa: BLE001 - not base64
                            encrypted = False

            report.append({
                "file": path.name,
                "state": "encrypted" if encrypted else "PLAINTEXT",
                "bytes": path.stat().st_size,
            })
        return report
