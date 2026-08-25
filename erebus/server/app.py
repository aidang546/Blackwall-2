"""HTTP + WebSocket front end.

The desktop window and (later) the phone are the same client talking to this
server, which is why the phone needs no extra code path - it is just another
browser pointing at the same URL.

Auth: loopback needs nothing. Anything else must present the shared token, and
must come from a private network range. Both checks, not either - a token that
leaks should still not be reachable from the open internet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..core.bus import EventBus
from ..core.state import State

log = logging.getLogger("erebus.server")

STATIC_DIR = Path(__file__).parent / "static"


def client_ip(request_or_ws) -> str:
    client = getattr(request_or_ws, "client", None)
    return client.host if client else "unknown"


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def in_allowed_networks(host: str, networks: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    for network in networks:
        try:
            if address in ipaddress.ip_network(network, strict=False):
                return True
        except ValueError:
            continue
    return False


def create_app(config, bus: EventBus, assistant) -> FastAPI:
    app = FastAPI(title="Erebus", docs_url=None, redoc_url=None)
    token = config.resolve_token()
    allowed = config.get("server.allow_networks") or ["127.0.0.0/8"]

    def authorized(scope, supplied: str | None) -> bool:
        host = client_ip(scope)
        if is_loopback(host):
            return True
        if not in_allowed_networks(host, allowed):
            log.warning("rejected %s: outside allowed networks", host)
            return False
        if supplied != token:
            log.warning("rejected %s: bad or missing token", host)
            return False
        return True

    # -- pages --------------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- api ----------------------------------------------------------------

    @app.get("/api/state")
    async def get_state(request: Request, token_q: str | None = Query(None, alias="token")):
        if not authorized(request, token_q):
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        return {
            "state": str(bus.state),
            "name": config.get("identity.name", "Erebus"),
            "wake_words": config.get("identity.wake_words", []),
            "ui": config.section("ui"),
        }

    @app.get("/api/actions")
    async def get_actions(request: Request, token_q: str | None = Query(None, alias="token")):
        if not authorized(request, token_q):
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        return {"actions": assistant.registry.describe()}

    # -- websocket ----------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket, token_q: str | None = Query(None, alias="token")):
        if not authorized(websocket, token_q):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        queue = bus.subscribe()
        log.info("client connected from %s (%d total)", client_ip(websocket), bus.subscriber_count)

        # Bring the new client up to date immediately - otherwise a page that
        # loads mid-sentence sits on an idle wall while Erebus is talking.
        await websocket.send_text(json.dumps({
            "kind": "hello",
            "state": str(bus.state),
            "name": config.get("identity.name", "Erebus"),
            "ui": config.section("ui"),
            "actions": assistant.registry.describe(),
        }))

        async def pump() -> None:
            """Bus -> client."""
            while True:
                event = await queue.get()
                await websocket.send_text(json.dumps(event.to_json()))

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                raw = await websocket.receive_text()
                await _handle_client_message(raw, assistant, bus)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("websocket closed: %s", exc)
        finally:
            pump_task.cancel()
            bus.unsubscribe(queue)
            log.info("client disconnected (%d remain)", bus.subscriber_count)

    return app


async def _handle_client_message(raw: str, assistant, bus: EventBus) -> None:
    """Commands the UI (or the phone) can send us."""
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        return
    kind = message.get("kind")

    if kind == "text":
        # Typed into the UI, or sent from the phone's tap-to-talk transcript.
        text = (message.get("text") or "").strip()
        if text:
            await bus.publish("transcript", text=text, source="text")
            asyncio.create_task(assistant.handle(text))

    elif kind == "ptt":
        # Push-to-talk: skip the wake word and record immediately.
        asyncio.create_task(assistant.take_turn())

    elif kind == "interrupt":
        assistant.interrupt()
        await bus.set_state(State.IDLE)

    elif kind == "forget":
        assistant.brain.forget()
        await bus.publish("notice", text="Context cleared.")
