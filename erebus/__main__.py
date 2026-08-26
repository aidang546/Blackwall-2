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
                                 "voices", "fetch-voice", "brief"])
    parser.add_argument("text", nargs="*",
                        help="text for `say`, or a voice name for `fetch-voice`")
    parser.add_argument("--out", metavar="FILE",
                        help="say: render to a WAV instead of playing it")
    parser.add_argument("--dry", action="store_true",
                        help="say: bypass the voice effects chain")
    parser.add_argument("--voice", metavar="NAME",
                        help="say: override the configured voice")
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
