"""OPSEC and OSINT: the audit chain, the vault, access control, and forensics.

No network. The modules that reach out (archive, domain) are covered by their
pure parts; their transport is exercised by hand against live sites.

    python tests/test_opsec.py
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus.opsec.audit import AuditLog                      # noqa: E402
from erebus.opsec.guard import Listening, Lockout, purge     # noqa: E402
from erebus.opsec.vault import MAGIC, Vault                  # noqa: E402
from erebus.osint import media                               # noqa: E402
from erebus.osint.archive import slugify                     # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "fixtures"))
from make_jpeg import LAT, LON, MAKE, MODEL, build           # noqa: E402

FAILURES = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILURES
    FAILURES += not ok
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")


def test_audit(tmp: pathlib.Path) -> None:
    print("\nAUDIT CHAIN")
    audit = AuditLog(tmp / "audit.jsonl")

    check("an empty log verifies", audit.verify()[0])
    for i in range(5):
        audit.record("action", name=f"cmd{i}")
    intact, message = audit.verify()
    check("a written chain verifies", intact, message)
    check("entries read back", len(list(audit.entries())) == 5)
    check("filtering by kind works", len(audit.tail(kind="action")) == 5)
    check("an absent kind returns nothing", audit.tail(kind="nope") == [])

    # Tampering must be detectable - this is the whole reason for the chain.
    lines = (tmp / "audit.jsonl").read_text().splitlines()
    edited = json.loads(lines[2]); edited["name"] = "something-else"
    lines[2] = json.dumps(edited, ensure_ascii=False, sort_keys=True)
    (tmp / "audit.jsonl").write_text("\n".join(lines) + "\n")
    fresh = AuditLog(tmp / "audit.jsonl")
    intact, message = fresh.verify()
    check("EDITING an entry breaks the chain", not intact, message.split(".")[0])

    (tmp / "audit.jsonl").write_text(
        "\n".join(l for i, l in enumerate(lines) if i != 3) + "\n"
    )
    fresh = AuditLog(tmp / "audit.jsonl")
    check("DELETING an entry breaks the chain", not fresh.verify()[0])

    # Appending after a break must not silently "repair" it.
    fresh.record("action", name="later")
    check("appending does not hide an earlier break", not fresh.verify()[0])

    # A payload may carry the envelope's own field names. This exact collision
    # broke macro replies once already, on the event bus, and again here.
    reserved = AuditLog(tmp / "reserved.jsonl")
    reserved.record("action", name="work_mode", kind="macro", ts="whenever",
                    prev="nonsense")
    entry = list(reserved.entries())[0]
    check("a payload field named 'kind' does not collide",
          entry.kind == "action", entry.kind)
    check("the payload keeps its own value under a safe name",
          entry.data.get("kind") is None and entry.data["name"] == "work_mode")
    check("the chain still verifies with reserved names in the payload",
          reserved.verify()[0])

    # Reopening must continue the chain, not restart it.
    clean = tmp / "audit2.jsonl"
    a = AuditLog(clean); a.record("x", n=1)
    b = AuditLog(clean); b.record("x", n=2)
    check("a reopened log continues the same chain", AuditLog(clean).verify()[0])


def test_vault(tmp: pathlib.Path) -> None:
    print("\nVAULT")
    vault = Vault(enabled=True)
    if not vault.ready:
        print("  skipped - no working crypto backend")
        return

    secret = "source: J. contacted 14:05, will not go on record"
    sealed = vault.encrypt(secret.encode())
    check("output is marked as ours", sealed.startswith(MAGIC))
    check("the plaintext is not in the output", secret.encode() not in sealed)
    check("round trips", vault.decrypt(sealed).decode() == secret)

    # A fresh nonce every time, or identical notes would be linkable.
    check("the same input seals differently each time",
          vault.encrypt(secret.encode()) != sealed)

    flipped = bytearray(sealed); flipped[-1] ^= 0x01
    check("TAMPERING is detected, not decrypted to garbage",
          vault.decrypt(bytes(flipped)) is None)

    check("plaintext passes through untouched",
          vault.decrypt(b'{"plain": true}') == b'{"plain": true}')

    path = tmp / "notes.txt"
    vault.write_text(path, secret)
    check("written files are sealed on disk",
          path.read_bytes().startswith(MAGIC))
    check("and read back", vault.read_text(path) == secret)

    lines_path = tmp / "lines.jsonl"
    for i in range(3):
        vault.append_line(lines_path, json.dumps({"n": i}))
    check("append-only files seal per line",
          len(lines_path.read_text().splitlines()) == 3)
    check("each line is independently sealed",
          base64.b64decode(lines_path.read_text().splitlines()[0]).startswith(MAGIC))
    read = vault.read_lines(lines_path)
    check("and read back in order",
          [json.loads(l)["n"] for l in read] == [0, 1, 2])

    # Mixed files matter: a journal that predates the vault must still open.
    with open(lines_path, "a", encoding="utf-8") as fh:
        fh.write('{"n": 3}\n')
    read = vault.read_lines(lines_path)
    check("plaintext lines from before the vault still read",
          [json.loads(l)["n"] for l in read] == [0, 1, 2, 3])

    status = {r["file"]: r["state"] for r in vault.status([path, tmp / "gone"])}
    check("status reports sealed files correctly", status["notes.txt"] == "encrypted")
    check("status reports absent files", status["gone"] == "absent")

    plain = tmp / "plain.yaml"
    plain.write_text("name: Aidan\n")
    state = vault.status([plain])[0]["state"]
    check("PLAINTEXT yaml is not mistaken for sealed", state == "PLAINTEXT", state)


def test_guard(tmp: pathlib.Path) -> None:
    print("\nACCESS CONTROL")
    lock = Lockout(max_attempts=3, window=300, penalty=900)
    address = "192.168.1.99"

    check("a fresh address is not locked", not lock.is_locked(address))
    check("first failure does not lock", not lock.record_failure(address))
    check("second does not lock", not lock.record_failure(address))
    check("third does", lock.record_failure(address))
    check("and it stays locked", lock.is_locked(address))
    check("with time remaining", lock.seconds_remaining(address) > 800)
    check("a different address is unaffected", not lock.is_locked("192.168.1.50"))
    lock.record_success(address)
    check("a success clears the lockout", not lock.is_locked(address))

    print("\nSTANDING DOWN")
    listening = Listening()
    check("starts live", listening.active)
    listening.stand_down()
    check("stands down", not listening.active)
    check("and says so in its state", listening.state["listening"] is False)
    listening.resume()
    check("resumes", listening.active)

    print("\nPURGE")
    targets = []
    for name in ("a.jsonl", "b.jsonl"):
        path = tmp / name
        path.write_text("data")
        targets.append(path)
    keep = tmp / "keep.yaml"
    keep.write_text("kept")
    removed = purge(targets)
    check("removes what it was given", sorted(removed) == ["a.jsonl", "b.jsonl"])
    check("files are gone", not any(t.exists() for t in targets))
    check("leaves everything else alone", keep.exists())
    check("purging nothing is not an error", purge(targets) == [])


def test_media(tmp: pathlib.Path) -> None:
    print("\nMEDIA FORENSICS")
    with_gps = tmp / "photo.jpg"
    with_gps.write_bytes(build(with_gps=True))

    report = media.examine(with_gps)
    check("identifies the container by magic bytes",
          report.detected_type == "image/jpeg")
    check("hashes it", len(report.sha256) == 64)
    check("reads the camera make", report.exif.get("make") == MAKE)
    check("reads the model", report.exif.get("model") == MODEL)
    check("reads the capture time", "2026:08:26" in report.exif.get("datetime_original", ""))
    check("extracts latitude", abs(report.gps["lat"] - LAT) < 1e-5,
          f"{report.gps['lat']:.6f}")
    check("extracts longitude, with the western sign",
          abs(report.gps["lon"] - LON) < 1e-5, f"{report.gps['lon']:.6f}")
    check("offers a map link", "maps" in report.gps)
    check("flags coordinates as a lead, not proof",
          any("not proof" in n for n in report.notes))
    check("a correctly named jpg is NOT flagged as disguised",
          not any("Extension says" in n for n in report.notes))

    stripped = tmp / "stripped.jpg"
    stripped.write_bytes(build(with_exif=False))
    report = media.examine(stripped)
    check("no EXIF is reported as a finding",
          any("No EXIF" in n for n in report.notes))
    check("and no location is invented", report.gps is None)

    disguised = tmp / "actually_a_jpeg.png"
    disguised.write_bytes(build(with_gps=True))
    report = media.examine(disguised)
    check("a renamed file IS flagged",
          any("Extension says" in n for n in report.notes))

    check("a missing file is handled", media.examine(tmp / "nope.jpg").bytes == 0)

    truncated = tmp / "truncated.jpg"
    truncated.write_bytes(build(with_gps=True)[:60])
    report = media.examine(truncated)
    check("a truncated file does not crash the parser",
          report.sha256 != "" and report.gps is None)

    print("\nSLUGS")
    check("slugify strips punctuation",
          slugify("Hello, World! (2026)") == "hello-world-2026")
    check("slugify never returns empty", slugify("!!!") == "untitled")
    check("slugify truncates", len(slugify("x" * 200)) <= 60)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        test_audit(tmp)
        test_vault(tmp)
        test_guard(tmp)
        test_media(tmp)
    print(f"\n  {'all checks passed' if not FAILURES else f'{FAILURES} failed'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
