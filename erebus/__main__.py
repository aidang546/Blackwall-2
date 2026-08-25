"""Command line entry point.

    python -m erebus                 run the assistant
    python -m erebus --no-voice      UI + server only (no models needed)
    python -m erebus devices         list microphones
    python -m erebus actions         list everything it can do
    python -m erebus say "text"      test the voice and its effects
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


def cmd_say(config: Config, text: str) -> int:
    from .pipeline.tts import Speaker

    speaker = Speaker(
        backend=config.get("tts.backend", "piper"),
        voice=config.get("tts.voice"),
        effects=config.section("tts").get("effects", {}),
        rate=config.get("tts.rate", 1.0),
    )
    if not speaker.load():
        print("No speech backend available.")
        return 1
    asyncio.run(speaker.speak(text))
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

    if not no_voice:
        await assistant.start()
    else:
        await bus.publish(
            "capabilities", wake=False, stt=False, tts=False, brain=False, audio=False
        )

    try:
        await server_task
    finally:
        await assistant.stop()


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
                        choices=["run", "devices", "actions", "say", "pair"])
    parser.add_argument("text", nargs="*", help="text for `say`")
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
    if args.command == "say":
        if not args.text:
            print('usage: python -m erebus say "something to say"')
            return 2
        return cmd_say(config, " ".join(args.text))

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
