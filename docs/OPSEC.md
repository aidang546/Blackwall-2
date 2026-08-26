# OPSEC and investigation

Two halves of one thing. For investigative work, protecting notes and sources
*is* infrastructure, not hygiene — so the security tooling and the research
tooling share an audit trail.

**Scope.** This covers entities, documents, domains and media artifacts — the
standard open-source investigation toolkit. It deliberately does not include
people-tracking: no breach lookups, no address resolution, nothing built to
profile a private individual.

---

## Erebus was the weak point

Before any of this, Erebus held your business figures, health data and notes in
plaintext, ran shell commands leaving no trace, and accepted unlimited token
guesses from anywhere on your LAN. Fixed, in that order of severity.

### The audit chain

Everything Erebus does is recorded to `audit.local.jsonl`, hash-chained: each
entry carries the SHA-256 of the line before it.

```
python -m erebus audit
```
```
  INTACT  Chain intact across 2 entries.

  2026-08-26 12:56:02  osint.capture   url=https://example.com sha256=ff67a9d7…
  2026-08-26 12:56:04  osint.examine   file=with_gps.jpg had_gps=True
```

Altering or deleting any entry breaks every hash after it:

```
  BROKEN  Chain breaks at line 4: it follows 588e5937420e… but the previous
          line hashes to 7f177b92913a…. An entry was altered or removed.
```

Be clear what this does and does not buy. Anything running as you can rewrite
the whole chain. What it stops is *quiet* edits — someone changing one line to
hide one action, without rebuilding everything after it. That is the realistic
threat, and it is also what makes the log usable as provenance: an entry saying
a page was captured at a given time means something only if the log around it
can be shown intact.

### The vault

Encryption at rest for `profile.local.yaml`, `journal.local.jsonl` and
`health.local.jsonl`. AES-256-GCM, so tampering is detected rather than
decrypted into something subtly wrong.

```yaml
opsec:
  vault:
    enabled: true
```
```
python -m erebus vault --vault status
python -m erebus vault --vault encrypt
```

The key is machine-bound — sealed with Windows DPAPI against your user account.
Honestly, what that means:

- **Protects against:** a copied folder, a pulled drive, a stolen backup, a
  synced cloud directory, an accidental commit.
- **Does not protect against:** malware already running as you, which can ask
  DPAPI to unseal it exactly as Erebus does.

Machine-bound encryption is not a defence against local compromise, and
pretending otherwise would be worse than plaintext — you would trust the files
further than you should. If you need that, `vault.mode: passphrase` is the
escape hatch, at the cost of typing one every boot and losing autostart.

### Access control

```yaml
server:
  lockout:
    max_attempts: 5
    window: 300       # seconds over which failures accumulate
    penalty: 900      # how long a locked-out address stays out
```

Failures are tracked per address, so one clumsy device cannot lock out the
house. Every rejection is written to the audit chain — a token reachable from
the whole LAN is a password, and without a record there is no way to notice it
being tried.

```
python -m erebus rotate-token     # invalidates every paired device
```

### Standing down

| say | effect |
|---|---|
| "stand down" / "go dark" | stops capturing. Frames are drained but nothing looks at them: no wake detection, no recording, no level published. |
| "resume listening" | back to normal |
| "security status" | posture, out loud |
| "purge the record" | destroys the journal, health data and audit log — **confirmation-gated** |

An always-listening assistant is a microphone in the room, and one that
remembers everything is a record of who you spoke to. For investigative work
both need to be one sentence away.

Purge is a delete, not a secure wipe. On an SSD with wear levelling, overwriting
a file does not reliably destroy the old blocks, and claiming otherwise would be
a lie you might rely on. Full-disk encryption is what makes deletion meaningful;
the vault is what makes the files useless beforehand.

---

## Capture and attest

The core problem in investigative work is that the web is mutable. The post gets
edited, the page gets pulled, the policy quietly changes. A screenshot proves
nothing — anyone can make one.

```
python -m erebus archive https://example.com --case my-investigation
```
```
Example Domain
  fetched   2026-08-26T12:55:48+00:00
  status    200  text/html
  sha256    ff67a9d764d6a2367a187734e697f6a53217db9a21c101d410a113ca871a299d
  bytes     559
  wayback   http://web.archive.org/web/20260826030733/https://example.com/
  stored    cases/my-investigation/20260826T125548+0000-example-domain.html
```

For each capture it stores the exact bytes received, hashes them, records the
response headers (a `Last-Modified` or `ETag` often turns out to matter), and
asks the Wayback Machine for an independent copy so the claim does not rest
solely on a file on your disk.

```
python -m erebus cases my-investigation
```

re-hashes every stored file against the manifest and reports `ok`, `ALTERED` or
`MISSING`. That includes catching changes *you* made — an investigator who
cannot show their working copy is unmodified has a weaker record than one who
can.

None of this makes a capture legally authoritative on its own. It makes it
checkable.

---

## Media forensics

```
python -m erebus examine photo.jpg
```
```
  type      image/jpeg
  sha256    d90cd0d8…
  exif
    make                 TestCam
    model                Model-1
    datetime_original    2026:08:26 14:30:00
  location
    coordinates          51.507400, -0.127800
    maps                 https://www.google.com/maps/search/?api=1&query=…
  ! Coordinates present. Treat as a lead, not proof — EXIF is trivially
    editable and the clock may be wrong.
```

EXIF is parsed in pure stdlib rather than via Pillow or exifread. Not purism:
this reads bytes out of files handed to you by strangers, and every dependency
in that path is attack surface.

Two things it tells you that are easy to miss:

- **Absent EXIF is itself a finding.** Every major platform strips it on upload,
  so a photo "straight off someone's phone" with none has been through
  something.
- **Extension mismatches.** A `.png` whose contents are JPEG is renamed,
  mislabelled, or disguised. Checked against a real extension table — "jpg" is
  not a substring of "jpeg", and a checker that cries wolf on every ordinary
  photo is worse than none.

---

## Domain research

```
python -m erebus domain example.com
```

Six independent views, gathered concurrently, one failing never blocking
another:

| | |
|---|---|
| **RDAP** | registration, registrar, dates — structured JSON, so no scraping free-text WHOIS |
| **DNS** | where it resolves, plus reverse lookups |
| **TLS** | issuer, validity, and the subject alternative names, which routinely list sibling domains the operator did not mean to connect publicly |
| **CT logs** | every certificate ever issued, via crt.sh — the richest subdomain source, reaching back to hosts long gone |
| **Wayback** | how far back it is archived, and when last seen |
| **HTTP** | redirect chain and server headers |

All read-only, all public record, all from sources built to be queried. No port
scanning, no probing beyond an ordinary HTTPS request.

The certificate is read without verifying it — an expired or mismatched cert is
a *finding*, and refusing to look would discard exactly the case worth knowing
about. Both are reported explicitly.

---

## Testing

```
python tests/test_opsec.py
```

51 checks: the chain detects edits, deletions and appends-after-a-break; the
vault detects tampering and never mistakes plaintext for sealed; lockout is
per-address; EXIF extraction is verified against a fixture built byte by byte
so the expected coordinates are known exactly.

No network required.
