"""
WebSocket RPC bridge.

The packaged shell cannot reach the backend over HTTP: every fetch and XHR from
the Tauri webview fails with a bare "Failed to fetch", while the same URL works
in Edge and works over WebSocket. So the whole API is tunnelled through one
WebSocket.

The bridge does not reimplement any endpoint. It calls the ASGI application
directly with a synthetic HTTP scope and forwards whatever the app writes back.
That keeps routing, validation, dependency injection and every existing handler
untouched, while adding no HTTP client, no socket and no second network hop.

Wire format (JSON, one object per frame):

    -> {"id": 1, "method": "GET", "path": "/companions"}
    -> {"id": 2, "method": "POST", "path": "/companions/chat/stream", "body": {...}}

    <- {"id": 1, "type": "chunk", "data": "..."}      zero or more
    <- {"id": 1, "type": "end", "status": 200}         always last
    <- {"id": 1, "type": "error", "status": 500, "detail": "..."}

A non-streaming call is just a response with a single chunk.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger("ws_bridge")

# Response bytes per WebSocket frame; see the slice in send().
MAX_CHUNK = 96 * 1024


def _split_path(raw: str) -> Tuple[str, bytes]:
    """Return (path, query_string) from a request target."""
    if "?" in raw:
        path, query = raw.split("?", 1)
        return path or "/", query.encode()
    return raw or "/", b""


class WsBridge:
    """Drives the ASGI app directly and streams its output over one socket."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        # Every request runs in its own task, so several of them can finish at
        # the same moment and call send_json concurrently. websockets raises
        # ConcurrencyError on that, and its message is empty, which surfaced as
        # a bare "ws bridge GET ... failed:" in the log. One writer at a time.
        self._send_lock = asyncio.Lock()

    async def _send(self, ws: WebSocket, payload: Dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send_json(payload)

    async def handle(self, ws: WebSocket) -> None:
        # Log who is connecting: a webview sends Origin, a script client does
        # not, and the access log alone cannot tell them apart.
        logger.info(
            "ws connect from %s origin=%r ua=%r",
            ws.client,
            ws.headers.get("origin"),
            (ws.headers.get("user-agent") or "")[:40],
        )
        await ws.accept()
        # Map active request id -> (task, cancel_event) so a client abort can
        # stop a specific in-flight stream without tearing down the whole socket.
        inflight: Dict[Any, Tuple[asyncio.Task, asyncio.Event]] = {}
        disconnect = asyncio.Event()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    await self._send(ws, {"type": "error", "detail": "bad json"})
                    continue
                if frame.get("type") == "cancel":
                    entry = inflight.get(frame.get("id"))
                    if entry is not None:
                        target_task, cancel_ev = entry
                        cancel_ev.set()
                        target_task.cancel()
                    continue
                # One task per request: a long stream must not block the next
                # call arriving on the same socket.
                rid = frame.get("id")
                cancel_ev = asyncio.Event()
                task = asyncio.create_task(
                    self._serve(ws, frame, disconnect, cancel_ev, inflight)
                )
                if rid is not None:
                    inflight[rid] = (task, cancel_ev)
        except WebSocketDisconnect:
            logger.info("ws disconnect from %s", ws.client)
        except Exception:
            logger.exception("ws handler failed for %s", ws.client)
        finally:
            disconnect.set()
            for task, cancel_ev in list(inflight.values()):
                cancel_ev.set()
                task.cancel()

    async def _serve(
        self,
        ws: WebSocket,
        frame: Dict[str, Any],
        disconnect: asyncio.Event,
        cancel_ev: asyncio.Event,
        inflight: Dict[Any, Tuple[asyncio.Task, asyncio.Event]],
    ) -> None:
        rid = frame.get("id")
        method = str(frame.get("method") or "GET").upper()
        path, query = _split_path(str(frame.get("path") or "/"))
        body = frame.get("body")

        raw_body = b""
        headers: List[Tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8002")]
        if body is not None:
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers.append((b"content-type", b"application/json"))
            headers.append((b"content-length", str(len(raw_body)).encode()))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 8002),
        }

        delivered = False

        async def receive() -> Dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            # Starlette polls receive() while a streaming response is being
            # written, to notice a client that has gone away. Wait for either a
            # socket disconnect or a per-request cancel frame.
            wait_disc = asyncio.create_task(disconnect.wait())
            wait_canc = asyncio.create_task(cancel_ev.wait())
            try:
                _, pending = await asyncio.wait(
                    {wait_disc, wait_canc}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
            except asyncio.CancelledError:
                wait_disc.cancel()
                wait_canc.cancel()
                raise
            return {"type": "http.disconnect"}

        status = 500

        async def send(message: Dict[str, Any]) -> None:
            nonlocal status
            kind = message.get("type")
            if kind == "http.response.start":
                status = int(message.get("status", 200))
            elif kind == "http.response.body":
                data = message.get("body", b"") or b""
                # A body can be several megabytes: the companion list embeds every
                # card's raw JSON. One frame that size trips the 1 MB frame cap and
                # the socket is closed with 1009, so each body is sliced. 96 KB
                # leaves room for JSON escaping of control bytes.
                for offset in range(0, len(data), MAX_CHUNK):
                    piece = data[offset : offset + MAX_CHUNK]
                    await self._send(
                        ws, {"id": rid, "type": "chunk", "data": piece.decode("latin-1")}
                    )

        try:
            await self._app(scope, receive, send)
            if not cancel_ev.is_set():
                await self._send(ws, {"id": rid, "type": "end", "status": status})
        except asyncio.CancelledError:
            # Cancelled cleanly via client cancel frame or socket close.
            pass
        except Exception as exc:  # noqa: BLE001 - report instead of dropping
            if not cancel_ev.is_set():
                logger.warning("ws bridge %s %s failed: %r", method, path, exc)
                try:
                    await self._send(
                        ws, {"id": rid, "type": "error", "status": 500, "detail": str(exc)}
                    )
                except Exception:  # noqa: BLE001 - socket already gone
                    pass
        finally:
            if rid is not None:
                inflight.pop(rid, None)


def install(app: FastAPI) -> None:
    """Mount the bridge on /ws."""
    bridge = WsBridge(app)

    @app.websocket("/ws")
    async def _ws(ws: WebSocket) -> None:
        await bridge.handle(ws)
