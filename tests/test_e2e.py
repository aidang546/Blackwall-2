"""End-to-end over the real websocket: boot the daemon, drive it, read the bus.

Runs with no models installed - `--no-voice` skips every heavy import, so this
is safe in CI and on a machine that has not downloaded a gigabyte of weights.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import time

import websockets

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8899
URL = f"ws://127.0.0.1:{PORT}/ws"


async def drain(ws, seconds: float = 2.5) -> list[dict]:
    """Collect events for a while, ignoring the amplitude firehose."""
    out: list[dict] = []
    try:
        async with asyncio.timeout(seconds):
            while True:
                msg = json.loads(await ws.recv())
                if msg["kind"] != "level":
                    out.append(msg)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    return out


def kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


async def scenarios() -> int:
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")

    async with websockets.connect(URL) as ws:
        hello = json.loads(await ws.recv())
        check("handshake", hello["kind"] == "hello" and hello["state"] == "idle")
        check("registry served", len(hello["actions"]) > 10,
              f"{len(hello['actions'])} actions")

        # --- a plain command ------------------------------------------------
        await ws.send(json.dumps({"kind": "text", "text": "volume up"}))
        events = await drain(ws)
        check("command routes to an action",
              any(e.get("name") == "volume_up" for e in events))
        check("returns to idle",
              events and events[-1].get("state") == "idle")

        # --- the event envelope must never be shadowed ----------------------
        action = next((e for e in events if e["kind"] == "action"), None)
        check("action event keeps its own kind",
              action is not None and action["kind"] == "action"
              and action["category"] == "system")

        # --- a macro --------------------------------------------------------
        await ws.send(json.dumps({"kind": "text", "text": "work mode"}))
        events = await drain(ws)
        check("macro runs", any(e.get("name") == "work_mode" for e in events))
        check("macro speaks its line",
              any(e["kind"] == "reply" and "Workspace" in e.get("text", "")
                  for e in events))

        # --- destructive actions are gated ----------------------------------
        await ws.send(json.dumps({"kind": "text", "text": "shut down"}))
        events = await drain(ws)
        check("shutdown asks first",
              any(e["kind"] == "reply" and "Confirm" in e.get("text", "")
                  for e in events))
        check("shutdown did not fire",
              not any(e["kind"] == "action" for e in events))

        await ws.send(json.dumps({"kind": "text", "text": "no, cancel that"}))
        events = await drain(ws)
        check("declining cancels",
              any(e["kind"] == "reply" and "Cancel" in e.get("text", "")
                  for e in events))

        # --- unknown input must not crash or invent an action ---------------
        await ws.send(json.dumps({"kind": "text", "text": "what is the weather"}))
        events = await drain(ws)
        check("unknown input runs nothing",
              not any(e["kind"] == "action" for e in events))
        check("unknown input still answers", "reply" in kinds(events))
        check("no error state", not any(e.get("state") == "error" for e in events))

    return failures


def main() -> int:
    # A log file rather than a PIPE. Nothing here ever reads the pipe, so once
    # the server logged more than the buffer holds it would block on its own
    # stdout and never finish starting - a hang that looks exactly like a slow
    # machine, and gets blamed on one.
    import tempfile

    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
    server = subprocess.Popen(
        [sys.executable, "-m", "erebus", "--no-voice", "--no-window"],
        cwd=ROOT,
        env={**__import__("os").environ, "EREBUS_PORT": str(PORT)},
        stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # Wait for the port rather than sleeping a fixed amount. The budget is
        # generous because this also runs on a machine busy doing something
        # else - ten seconds was enough when idle and not when it was not.
        import socket
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if server.poll() is not None:
                break
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                    break
            time.sleep(0.1)
        else:
            pass
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) != 0:
                log.flush()
                print("  FAIL  server never came up"
                      + (f" (exited {server.returncode})"
                         if server.poll() is not None else " within 30s"))
                # Say why, rather than leaving the reader to reproduce it.
                print(pathlib.Path(log.name).read_text()[-1500:])
                return 1

        failures = asyncio.run(scenarios())
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    print(f"\n  {'all checks passed' if not failures else str(failures) + ' failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
