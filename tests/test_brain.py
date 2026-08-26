"""The LLM layer, against a scripted Ollama.

A real model is non-deterministic and slow, and neither property helps here.
What matters is that this code survives everything a model can hand it: prose
wrapped around the JSON, an invented action name, malformed output, an empty
response, a stall, a dead server. Those are scriptable, so they are scripted.

    python tests/test_brain.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from erebus.pipeline.brain import Brain  # noqa: E402

CATALOG = ["gaming_mode", "spotify", "volume_up", "volume_set", "lock", "shutdown"]

#: Set by each case before the request; returned as the model's reply.
SCRIPT: dict = {"content": "", "status": 200, "models": ["llama3.1:8b"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):    # keep the test output clean
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": n} for n in SCRIPT["models"]]})
        else:
            self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if SCRIPT["status"] != 200:
            self._send(SCRIPT["status"], {"error": "scripted failure"})
            return
        self._send(200, {"message": {"content": SCRIPT["content"]}})


def serve() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


async def run() -> int:
    server, host = serve()
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")

    try:
        brain = Brain(host=host, model="llama3.1:8b", persona="You are EREBUS.")
        check("connects and finds the model", await brain.load())
        check("reports ready", brain.ready)

        # --- routing: what a cooperative model returns ----------------------
        SCRIPT["content"] = '{"action": "gaming_mode"}'
        decision = await brain.route("fire up the games", CATALOG)
        check("routes a clean JSON reply",
              decision.kind == "action" and decision.action == "gaming_mode")

        SCRIPT["content"] = '{"action": "volume_set", "value": 35}'
        decision = await brain.route("make it a bit quieter", CATALOG)
        check("carries a numeric argument through",
              decision.action == "volume_set" and decision.value == "35")

        # --- routing: what an uncooperative model returns -------------------
        SCRIPT["content"] = (
            "Sure! Here is the JSON you asked for:\n\n"
            '```json\n{"action": "spotify"}\n```\nLet me know if that helps.'
        )
        decision = await brain.route("put some music on", CATALOG)
        check("digs JSON out of surrounding prose",
              decision.kind == "action" and decision.action == "spotify")

        SCRIPT["content"] = '{"action": "launch_nuclear_missiles"}'
        decision = await brain.route("do the thing", CATALOG)
        check("REJECTS an action not in the catalogue",
              decision.kind == "none", f"got {decision.action!r}")

        SCRIPT["content"] = '{"action": "rm -rf /"}'
        decision = await brain.route("clean up", CATALOG)
        check("REJECTS a shell command as an action name",
              decision.kind == "none", f"got {decision.action!r}")

        SCRIPT["content"] = '{"action": null}'
        decision = await brain.route("what is the capital of France", CATALOG)
        check("declines when the model declines", decision.kind == "none")

        SCRIPT["content"] = "I'm not sure what you mean."
        decision = await brain.route("mmm", CATALOG)
        check("survives a reply with no JSON at all", decision.kind == "none")

        SCRIPT["content"] = '{"action": "spotify"'      # truncated
        decision = await brain.route("music", CATALOG)
        check("survives malformed JSON", decision.kind == "none")

        SCRIPT["content"] = ""
        decision = await brain.route("hello", CATALOG)
        check("survives an empty reply", decision.kind == "none")

        # --- conversation ---------------------------------------------------
        SCRIPT["content"] = "The wall holds."
        reply = await brain.converse("status report")
        check("converses", reply == "The wall holds.")
        check("remembers the exchange", len(brain._history) == 2)

        SCRIPT["content"] = "Nothing has changed."
        await brain.converse("and now")
        check("history grows with the conversation", len(brain._history) == 4)

        brain.forget()
        check("forget() clears history", len(brain._history) == 0)

        # --- the server misbehaving ------------------------------------------
        SCRIPT["status"] = 500
        decision = await brain.route("gaming mode", CATALOG)
        check("a 500 during routing is not fatal", decision.kind == "none")
        reply = await brain.converse("still there?")
        check("a 500 during conversation returns something speakable",
              isinstance(reply, str) and len(reply) > 0, repr(reply))
        check("a failed exchange is not recorded as history",
              len(brain._history) == 0)
        SCRIPT["status"] = 200

        await brain.close()

        # --- server absent entirely -------------------------------------------
        dead = Brain(host="http://127.0.0.1:1", model="llama3.1:8b")
        check("reports not-ready when Ollama is down", not await dead.load())
        check("routing without a brain is a no-op",
              (await dead.route("gaming mode", CATALOG)).kind == "none")
        reply = await dead.converse("hello")
        check("conversation without a brain still says something",
              isinstance(reply, str) and len(reply) > 0)
        await dead.close()

        # --- model not pulled --------------------------------------------------
        SCRIPT["models"] = ["some-other-model:latest"]
        missing = Brain(host=host, model="llama3.1:8b")
        check("detects the model is not pulled", not await missing.load())
        await missing.close()
        SCRIPT["models"] = ["llama3.1:8b"]

        # --- a tag carrying the :latest suffix ---------------------------------
        SCRIPT["models"] = ["llama3.1:8b:latest"]
        suffixed = Brain(host=host, model="llama3.1:8b")
        check("accepts a :latest-suffixed tag", await suffixed.load())
        await suffixed.close()

    finally:
        server.shutdown()

    print(f"\n  {'all checks passed' if not failures else f'{failures} failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
