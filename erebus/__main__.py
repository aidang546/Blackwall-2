"""Command line entry point.

    python -m erebus                 run the assistant
    python -m erebus --no-voice      UI + server only (no models needed)
    python -m erebus devices         list microphones
    python -m erebus actions         list everything it can do
    python -m erebus say "text"      test the voice and its effects
    python -m erebus say "x" --out a.wav --dry     render to a file, no effects
    python -m erebus voices          list downloaded voices
    python -m erebus fetch-voice NAME  download a Piper voice
    python -m erebus brief           compose today's briefing (add --out a.wav)
    python -m erebus pair            print the URL and QR data for your phone

  Investigation
    python -m erebus archive URL --case NAME    capture a page, hash it, archive it
    python -m erebus examine FILE               hashes, EXIF, GPS, disguised types
    python -m erebus domain example.com         RDAP, DNS, TLS, CT logs, wayback
    python -m erebus cases [NAME]               what has been captured, and is it intact

  Security
    python -m erebus security        posture at a glance
    python -m erebus audit           the tamper-evident record of what it did
    python -m erebus vault --encrypt seal the profile, journal and health data
    python -m erebus rotate-token    invalidate every paired device
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from .core.assistant import Assistant
from .core.bus import EventBus
from .core.config import Config
from .server.app import create_app

BANNER = r"""
   ___ ___ ___ ___ _   _ ___
  | __| _ \ __| _ ) | | / __|      behind the blackwall
  | _||   / _|| _ \ |_| \__ \
  |___|_|_\___|___/\___/|___/
"""


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(name)-18s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # These are chatty and never interesting unless something is on fire.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def lan_address() -> str:
    """Best guess at this machine's LAN IP, for pairing a phone."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))   # no packets sent; just picks a route
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# --------------------------------------------------------------------------
#  Subcommands
# --------------------------------------------------------------------------

def cmd_devices() -> int:
    from .pipeline.audio import list_devices

    devices = list_devices()
    if not devices:
        print("No input devices found (is `sounddevice` installed?)")
        return 1
    print(f"{'idx':>4}  {'rate':>6}  name")
    for device in devices:
        print(f"{device['index']:>4}  {device['default_samplerate']:>6}  {device['name']}")
    print("\nSet the one you want as audio.input_device in config.local.yaml")
    return 0


def cmd_actions(config: Config) -> int:
    from .actions.registry import Registry

    registry = Registry(config)
    current = None
    for action in registry.describe():
        if action["kind"] != current:
            current = action["kind"]
            print(f"\n  {current.upper()}")
        phrases = ", ".join(f'"{p}"' for p in action["phrases"])
        print(f"    {action['name']:<16} {phrases}")
    print(f"\n  {len(registry.actions)} actions total")
    return 0


def cmd_say(config: Config, text: str, out: str | None, dry: bool, voice: str | None) -> int:
    from .pipeline.tts import Speaker, write_wav

    effects = dict(config.section("tts").get("effects", {}))
    if dry:
        effects["enabled"] = False

    speaker = Speaker(
        backend=config.get("tts.backend", "piper"),
        voice=voice or config.get("tts.voice"),
        effects=effects,
        rate=config.get("tts.rate", 1.0),
    )
    if not speaker.load():
        print("No speech backend available.")
        return 1

    if out:
        # Render to a file rather than a sound card, so the voice chain can be
        # auditioned over SSH or in a container.
        audio, sample_rate = asyncio.run(speaker.synthesize(text))
        if audio is None or not len(audio):
            print("Nothing was synthesised.")
            return 1
        write_wav(out, audio, sample_rate)
        print(f"wrote {out}  ({len(audio) / sample_rate:.1f}s @ {sample_rate} Hz)")
        return 0

    asyncio.run(speaker.speak(text))
    return 0


def cmd_fetch_voice(name: str) -> int:
    from .pipeline.tts import fetch_voice

    try:
        path = fetch_voice(name)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not fetch {name}: {exc}")
        print("\nBrowse the catalogue at https://rhasspy.github.io/piper-samples/")
        return 1
    print(f"\n  {path}\n\nSet it in config.local.yaml:\n\n  tts:\n    voice: {name}\n")
    return 0


def cmd_voices(config: Config) -> int:
    from .pipeline.tts import MODELS_DIR, resolve_voice

    installed = sorted(MODELS_DIR.glob("*.onnx")) if MODELS_DIR.exists() else []
    if not installed:
        print("No voices downloaded yet. Get one with:\n")
        print("  python -m erebus fetch-voice en_GB-alan-medium\n")
        return 0
    active = resolve_voice(config.get("tts.voice", ""))
    print()
    for path in installed:
        mark = "*" if active and path.samefile(active) else " "
        print(f"  {mark} {path.stem:<38} {path.stat().st_size / 1e6:6.1f} MB")
    print("\n  * = currently selected\n")
    return 0


def cmd_brief(config: Config, out: str | None, observe: str | None) -> int:
    """Compose a briefing and print it, optionally speaking it."""
    from .briefing.compose import Briefer
    from .pipeline.brain import Brain

    async def go() -> int:
        brain = Brain(
            backend=config.get("brain.backend", "ollama"),
            host=config.get("brain.host", "http://127.0.0.1:11434"),
            model=config.get("brain.model", "llama3.1:8b"),
        )
        if not await brain.load():
            print("\n  Ollama is not reachable, so there is nothing to think with.")
            print("  Start it with `ollama serve`, then try again.\n")
            return 1
        briefer = Briefer(config, brain)
        if not briefer.profile.configured:
            print("\n  No profile.local.yaml - the briefing will say so.")
            print("  Copy profile.example.yaml and fill it in.\n")
        # Stream to the terminal exactly as the daemon speaks it, so the
        # chunking is visible while you are tuning the persona.
        from .pipeline.chunker import to_sentences

        chunks = []
        print()
        async for chunk in to_sentences(briefer.compose_stream(observation=observe)):
            chunks.append(chunk)
            print("\n".join(_wrap(chunk, 76)))
        print()
        text = " ".join(chunks)
        await brain.close()
        if not text.strip():
            print("  Nothing came back.\n")
            return 1

        if out:
            from .pipeline.tts import Speaker, write_wav

            speaker = Speaker(
                backend=config.get("tts.backend", "piper"),
                voice=config.get("tts.voice"),
                effects=config.section("tts").get("effects", {}),
                rate=config.get("tts.rate", 1.0),
            )
            if speaker.load():
                audio, rate = await speaker.synthesize(text)
                write_wav(out, audio, rate)
                print(f"  spoken to {out}\n")
        return 0

    return asyncio.run(go())


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    lines = []
    for paragraph in text.split("\n"):
        lines.extend(textwrap.wrap(paragraph, width,
                                   initial_indent="  ", subsequent_indent="  ")
                     or [""])
    return lines


# --------------------------------------------------------------------------
#  Investigation and security
# --------------------------------------------------------------------------

def cmd_archive(config: Config, url: str, case: str) -> int:
    """Capture a page and attest to what it said."""
    from .opsec.audit import AuditLog
    from .osint.archive import Archivist

    async def go() -> int:
        archivist = Archivist(case=case, audit=AuditLog())
        record = await archivist.capture(url)
        print("\n" + record.summary() + "\n")
        return 1 if record.error else 0

    return asyncio.run(go())


def cmd_examine(config: Config, path: str) -> int:
    """What a file can tell you about where it came from."""
    from .opsec.audit import AuditLog
    from .osint.media import examine

    report = examine(path, audit=AuditLog())
    print("\n" + report.summary())
    if report.reverse_search:
        print("\n  reverse image search")
        for name, link in report.reverse_search.items():
            print(f"    {name:<14} {link}")
    print()
    return 0


def cmd_domain(config: Config, domain: str, subdomains: bool) -> int:
    """Who runs a site, and what else they run."""
    from .opsec.audit import AuditLog
    from .osint.domain import investigate

    async def go() -> int:
        report = await investigate(domain, audit=AuditLog(),
                                   subdomains=subdomains)
        print("\n" + report.summary() + "\n")
        return 0

    return asyncio.run(go())


def cmd_cases(config: Config, case: str | None) -> int:
    from .osint.archive import Archivist, list_cases

    if case:
        archivist = Archivist(case=case)
        entries = archivist.entries()
        print(f"\n  {case}: {len(entries)} captures\n")
        for entry in entries:
            print(f"    {entry['fetched_at']}  {entry.get('sha256','')[:12]}…  "
                  f"{entry.get('title') or entry['url']}")
        print("\n  integrity")
        for result in archivist.verify():
            mark = "ok  " if result["state"] == "ok" else result["state"]
            print(f"    {mark}  {result['file']}")
        print()
        return 0

    cases = list_cases()
    print()
    for name in cases:
        count = len(Archivist(case=name).entries())
        print(f"  {name:<30} {count} captures")
    print(f"\n  {len(cases)} case(s)\n" if cases else "  no cases yet\n")
    return 0


def cmd_audit(config: Config, tail: int, kind: str | None) -> int:
    from .opsec.audit import AuditLog

    audit = AuditLog()
    intact, message = audit.verify()
    print(f"\n  {'INTACT' if intact else 'BROKEN'}  {message}\n")
    for record in audit.tail(limit=tail, kind=kind):
        detail = " ".join(f"{k}={v}" for k, v in record.data.items()
                          if v not in (None, "", [], {}))
        print(f"  {record.ts:%Y-%m-%d %H:%M:%S}  {record.kind:<18} {detail}")
    print()
    return 0 if intact else 1


def cmd_security(config: Config) -> int:
    from .opsec.actions import security_report
    from .opsec.audit import AuditLog
    from .opsec.guard import Listening
    from .opsec.vault import Vault

    report = security_report(
        AuditLog(), Listening(),
        Vault(enabled=config.get("opsec.vault.enabled", False)),
    )
    print("\n" + "\n".join(f"  {line}" for line in report.splitlines()) + "\n")
    return 0


def cmd_vault(config: Config, action: str) -> int:
    from .opsec.actions import PROTECTED
    from .opsec.vault import Vault

    vault = Vault(enabled=True)
    if not vault.ready:
        print("\n  The vault is unavailable - see the error above.\n")
        return 1

    if action == "status":
        print()
        for row in vault.status(PROTECTED):
            size = f"{row.get('bytes', 0)} bytes" if row["state"] != "absent" else ""
            print(f"  {row['file']:<28} {row['state']:<12} {size}")
        print()
        return 0

    encrypting = action == "encrypt"
    changed = 0
    for path in PROTECTED:
        if not path.exists():
            continue
        raw = path.read_bytes()
        is_sealed = raw.startswith(b"EREBUS1\n")
        if encrypting and not is_sealed:
            path.write_bytes(vault.encrypt(raw))
            changed += 1
        elif not encrypting and is_sealed:
            plain = vault.decrypt(raw)
            if plain is None:
                print(f"  {path.name}: could not decrypt, leaving it alone")
                continue
            path.write_bytes(plain)
            changed += 1
    print(f"\n  {'Encrypted' if encrypting else 'Decrypted'} {changed} file(s).\n")
    return 0


def cmd_rotate_token(config: Config) -> int:
    from .opsec.audit import AuditLog
    from .opsec.guard import rotate_token

    token = rotate_token()
    AuditLog().record("opsec.token_rotated")
    print(f"\n  New token: {token}\n")
    print("  Every paired device is now locked out. Re-pair with:\n")
    print("    python -m erebus pair\n")
    return 0


def cmd_pair(config: Config) -> int:
    token = config.resolve_token()
    port = config.get("server.port", 8848)
    host = config.get("server.host", "127.0.0.1")
    url = f"http://{lan_address()}:{port}/#token={token}"

    print(BANNER)
    print("  Open this on your phone (same Wi-Fi):\n")
    print(f"    {url}\n")
    if host == "127.0.0.1":
        print("  NOTE: server.host is 127.0.0.1, so only this machine can connect.")
        print("        Set `server: {host: 0.0.0.0}` in config.local.yaml to allow")
        print("        your phone in, then restart.\n")
    print("  The token is in .erebus_token. Anyone with it, on your network,")
    print("  can run the actions in your registry. Treat it like a password.\n")
    return 0


# --------------------------------------------------------------------------
#  Main run loop
# --------------------------------------------------------------------------

async def run(config: Config, no_voice: bool, open_ui: bool) -> None:
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())

    assistant = Assistant(config, bus)
    app = create_app(config, bus, assistant)

    host = config.get("server.host", "127.0.0.1")
    port = int(config.get("server.port", 8848))

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    )
    server_task = asyncio.create_task(server.serve())

    # Wait for the socket to be up before we send anyone to it.
    for _ in range(50):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)

    local_url = f"http://127.0.0.1:{port}/"
    print(BANNER)
    print(f"  wall     {local_url}")
    if host != "127.0.0.1":
        print(f"  phone    http://{lan_address()}:{port}/#token={config.resolve_token()}")
    print(f"  wake     {', '.join(config.get('identity.wake_words', []))}")
    print("  keys     [space] push to talk   [esc] interrupt   [ctrl-c] quit\n")

    if open_ui:
        threading.Thread(target=lambda: (time.sleep(0.4), open_window(local_url)),
                         daemon=True).start()

    # With a fake microphone this is a one-shot run: watch the bus, print what
    # happened, and stop. Subscribe before starting so nothing is missed.
    replay = bus.subscribe() if config.get("audio.fake_mic") else None

    if not no_voice:
        await assistant.start()
    else:
        await bus.publish(
            "capabilities", wake=False, stt=False, tts=False, brain=False, audio=False
        )

    try:
        if replay is not None:
            await _watch_replay(bus, replay)
            server.should_exit = True
        await server_task
    finally:
        await assistant.stop()


async def _watch_replay(bus: EventBus, queue) -> None:
    """Print the transcript, action and reply from a --fake-mic turn."""
    print("  replaying...\n")
    try:
        async with asyncio.timeout(180):
            while True:
                event = await queue.get()
                if event.kind == "transcript":
                    print(f"    heard    {event.data.get('text')!r}")
                elif event.kind == "action":
                    value = event.data.get("value")
                    print(f"    action   {event.data.get('name')}"
                          f"{f' = {value}' if value else ''}")
                elif event.kind == "reply":
                    print(f"    said     {event.data.get('text')!r}")
                elif event.kind == "replay_done":
                    break
    except (TimeoutError, asyncio.TimeoutError):
        print("    timed out")
    finally:
        bus.unsubscribe(queue)
    print()


def open_window(url: str) -> None:
    """Open a chromeless window if we can, otherwise just a tab.

    App mode matters here - browser chrome around a full-bleed red wall ruins
    the effect entirely.
    """
    import shutil
    import subprocess

    for browser in ("chrome", "msedge", "chromium", "brave"):
        path = shutil.which(browser)
        if path:
            try:
                subprocess.Popen(
                    [path, f"--app={url}", "--window-size=1280,760",
                     "--disable-features=TranslateUI"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return
            except OSError:
                continue
    webbrowser.open(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="erebus", description="Erebus voice assistant")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "devices", "actions", "say", "pair",
                                 "voices", "fetch-voice", "brief",
                                 "archive", "examine", "domain", "cases",
                                 "audit", "security", "vault", "rotate-token"])
    parser.add_argument("text", nargs="*",
                        help="text for `say`, or a voice name for `fetch-voice`")
    parser.add_argument("--out", metavar="FILE",
                        help="say: render to a WAV instead of playing it")
    parser.add_argument("--dry", action="store_true",
                        help="say: bypass the voice effects chain")
    parser.add_argument("--voice", metavar="NAME",
                        help="say: override the configured voice")
    parser.add_argument("--case", default="inbox",
                        help="archive: which case file to file the capture under")
    parser.add_argument("--no-subdomains", action="store_true",
                        help="domain: skip the certificate-transparency lookup")
    parser.add_argument("--tail", type=int, default=25,
                        help="audit: how many entries to show")
    parser.add_argument("--vault", default="status",
                        choices=["status", "encrypt", "decrypt"],
                        help="vault: what to do")
    parser.add_argument("--observe", metavar="TEXT",
                        help="brief: describe what the assistant can see "
                             "(stands in for the webcam until vision lands)")
    parser.add_argument("--fake-mic", metavar="WAV",
                        help="run: feed a WAV file in place of the microphone, "
                             "take one turn from it, and exit")
    parser.add_argument("--no-voice", action="store_true",
                        help="server and UI only; skip loading any models")
    parser.add_argument("--no-window", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    config = Config.load()

    if args.command == "devices":
        return cmd_devices()
    if args.command == "actions":
        return cmd_actions(config)
    if args.command == "pair":
        return cmd_pair(config)
    if args.command == "archive":
        if not args.text:
            print("usage: python -m erebus archive https://example.com [--case name]")
            return 2
        return cmd_archive(config, args.text[0], args.case)
    if args.command == "examine":
        if not args.text:
            print("usage: python -m erebus examine path/to/file.jpg")
            return 2
        return cmd_examine(config, args.text[0])
    if args.command == "domain":
        if not args.text:
            print("usage: python -m erebus domain example.com")
            return 2
        return cmd_domain(config, args.text[0], not args.no_subdomains)
    if args.command == "cases":
        return cmd_cases(config, args.text[0] if args.text else None)
    if args.command == "audit":
        return cmd_audit(config, args.tail, args.text[0] if args.text else None)
    if args.command == "security":
        return cmd_security(config)
    if args.command == "vault":
        return cmd_vault(config, args.vault)
    if args.command == "rotate-token":
        return cmd_rotate_token(config)
    if args.command == "brief":
        return cmd_brief(config, args.out, args.observe)
    if args.command == "voices":
        return cmd_voices(config)
    if args.command == "fetch-voice":
        if not args.text:
            print("usage: python -m erebus fetch-voice en_GB-alan-medium")
            return 2
        return cmd_fetch_voice(args.text[0])
    if args.command == "say":
        if not args.text:
            print('usage: python -m erebus say "something to say"')
            return 2
        return cmd_say(config, " ".join(args.text), args.out, args.dry, args.voice)

    if args.fake_mic:
        Config._assign(config.data, "audio.fake_mic", args.fake_mic)

    open_ui = config.get("ui.autolaunch", True) and not args.no_window
    try:
        asyncio.run(run(config, args.no_voice, open_ui))
    except KeyboardInterrupt:
        print("\n  offline.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `python -m erebus actions | head` closes the pipe under us. Exit
        # quietly rather than dumping a traceback the user cannot act on.
        try:
            sys.stdout.close()
        except OSError:
            pass
        sys.exit(0)
