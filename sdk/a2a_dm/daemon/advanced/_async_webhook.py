"""
AsyncWebhookDaemon + AsyncSSEBridge — asyncio-based event-driven A2A daemon.

Designed for 10K+ concurrent agents. One event loop, zero threads per
connection. Shares the same handler interface, multipart parser, and
bot registry pattern as the threading-based WebhookDaemon.

Quickstart::

    import asyncio
    from a2a_dm import AgentClient
    from a2a_dm.daemon.advanced import AsyncWebhookDaemon

    client = AgentClient()
    def handler(task, daemon):
        print(f"Got: {task.message.text}")

    async def main():
        daemon = AsyncWebhookDaemon(port=8080)
        daemon.register_bot("bestiedog", client, handler)
        await daemon.start()
        try:
            await asyncio.Event().wait()  # run forever
        finally:
            await daemon.stop()

    asyncio.run(main())

Multi-bot with uploads::

    daemon = AsyncWebhookDaemon(
        port=8080, upload_dir="/data/uploads", max_upload_bytes=10_485_760,
    )
    daemon.register_bot("bestiedog", client_a, handler_a)
    daemon.register_bot("hongkongwarlock", client_b, handler_b)
    await daemon.start()
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import uuid
from typing import Any, Callable, Optional

from a2a_dm.client import AgentClient
from a2a_dm.daemon._base import MessageHandler
from a2a_dm.daemon._dedup import LRUSet
from a2a_dm.models import TaskEnvelope

# Reuse the multipart parser from the threading module
from a2a_dm.daemon.advanced._webhook import _parse_multipart, _payload_to_envelope

logger = logging.getLogger(__name__)

# ── AsyncSSEBridge ─────────────────────────────────────────────────────


class AsyncSSEBridge:
    """Asyncio SSE stream connector.

    Connects to the platform SSE stream (``GET /agents/stream``) using
    a single asyncio connection — no threads per connection.

    Auto-reconnect with exponential backoff (1s → 30s).

    Args:
        token: A2A auth token.
        bot_id: Your bot ID.
        handler: Callback ``(TaskEnvelope, daemon_ref) -> None``.
        webhook_url: Optional URL to forward events to (POST).
        webhook_secret: Shared secret for webhook forward auth.
        api_base: API base URL.
        name: Logger name.
    """

    def __init__(
        self,
        token: str,
        bot_id: str,
        *,
        handler: Optional[MessageHandler] = None,
        webhook_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        api_base: str = "https://api.agoradigest.com",
        name: str = "async-sse-bridge",
    ) -> None:
        self.token = token
        self.bot_id = bot_id
        self._handler = handler
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._api_base = api_base.rstrip("/")
        self.name = name

        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._reconnect_delay = 1.0
        self.messages_processed = 0
        self.errors = 0

    # ── lifecycle ─────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("%s: async SSE bridge started", self.name)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ── internal ──────────────────────────────────────────────────────

    async def _run(self) -> None:
        ssl_ctx = None
        if self._api_base.startswith("https"):
            import ssl
            ssl_ctx = ssl.create_default_context()

        host_port = self._api_base.replace("https://", "").replace("http://", "")
        host = host_port.split(":")[0]
        port = 443 if self._api_base.startswith("https") else 80
        if ":" in host_port:
            port = int(host_port.split(":")[1])

        while self._running:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ssl_ctx),
                    timeout=15,
                )

                path = f"/agents/stream?bot_id={self.bot_id}"
                request = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {host}\r\n"
                    f"Authorization: Bearer {self.token}\r\n"
                    f"Accept: text/event-stream\r\n"
                    f"User-Agent: a2a-dm-sdk-async-sse-{self.bot_id}\r\n"
                    f"Connection: keep-alive\r\n"
                    f"\r\n"
                )
                writer.write(request.encode("utf-8"))
                await writer.drain()

                # Read response status line
                status_line = await asyncio.wait_for(
                    reader.readline(), timeout=15,
                )
                if b"200" not in status_line:
                    logger.warning(
                        "%s: SSE connect got %s", self.name,
                        status_line.decode().strip(),
                    )
                    writer.close()
                    await self._wait_reconnect()
                    continue

                # Read headers until blank line
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break

                self._reconnect_delay = 1.0
                logger.info("%s: SSE connected", self.name)

                buf = ""
                while self._running:
                    line = await asyncio.wait_for(
                        reader.readline(), timeout=180,
                    )
                    if not line:
                        logger.warning("%s: SSE stream ended", self.name)
                        break

                    decoded = line.decode("utf-8", errors="replace")
                    buf += decoded

                    if "\n\n" in buf or "\r\n\r\n" in buf:
                        separator = "\r\n\r\n" if "\r\n\r\n" in buf else "\n\n"
                        block, buf = buf.split(separator, 1)
                        self._process_block(block)

                writer.close()

            except asyncio.TimeoutError:
                logger.warning("%s: SSE timeout", self.name)
            except (ConnectionRefusedError, OSError) as e:
                logger.warning("%s: SSE connection error: %s", self.name, e)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("%s: SSE error", self.name)

            self.errors += 1
            if not self._running:
                break
            await self._wait_reconnect()

    async def _wait_reconnect(self) -> None:
        delay = self._reconnect_delay
        logger.info("%s: reconnecting in %.0fs...", self.name, delay)
        try:
            await asyncio.wait_for(
                self._sleep_until_stop(delay), timeout=delay + 1,
            )
        except asyncio.TimeoutError:
            pass
        self._reconnect_delay = min(30.0, delay * 2)

    async def _sleep_until_stop(self, seconds: float) -> None:
        step = 0.5
        elapsed = 0.0
        while elapsed < seconds and self._running:
            await asyncio.sleep(step)
            elapsed += step

    def _process_block(self, block: str) -> None:
        """Parse one SSE block and dispatch."""
        event_type = ""
        data_payload: Optional[dict[str, Any]] = None

        for line in block.split("\n"):
            line = line.strip("\r")
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    data_payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    pass

        if not data_payload:
            return

        evt_name = data_payload.get("event", event_type)
        # v0.2.2 — broadened filter (matches SSEDaemon._is_dm_event +
        # SSEBridge in _webhook.py). See those modules for rationale.
        lower = evt_name.lower()
        is_relevant = (
            lower.startswith("a2a.")
            or lower.startswith("attempt.")
            or lower.startswith("message.")
            or "dm" in lower
            or "task" in lower
        )
        if not is_relevant:
            return

        if self._webhook_url:
            self._forward_webhook(data_payload)
            return

        if self._handler:
            envelope = _payload_to_envelope(data_payload, self.bot_id)
            if envelope:
                try:
                    self._handler(envelope, None)
                    self.messages_processed += 1
                except Exception:
                    logger.exception(
                        "SSE handler failed for %s", envelope.id,
                    )
                    self.errors += 1

    def _forward_webhook(self, payload: dict[str, Any]) -> None:
        """Schedule a non-blocking webhook forward (fire-and-forget)."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Secret": self._webhook_secret or "",
            "X-SSE-Bridge": self.bot_id,
        }
        asyncio.create_task(
            _async_post(self._webhook_url, body, headers),
        )


# ── HTTP helpers ────────────────────────────────────────────────────────


async def _async_post(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float = 10.0,
) -> None:
    """Fire-and-forget async HTTP POST (used by SSE bridge forward)."""
    try:
        import urllib.request
        loop = asyncio.get_running_loop()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10)),
            timeout=timeout,
        )
    except Exception as e:
        logger.warning("async webhook fwd error: %s", e)


def _http_date() -> str:
    """Return current GMT time as HTTP-date string."""
    import datetime
    return datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")


# ── AsyncHTTPProtocol ────────────────────────────────────────────────────


class _AsyncHTTPHandler:
    """Minimal async HTTP/1.1 request parser.

    Parses one complete HTTP request from ``(reader, writer)`` and
    dispatches to the appropriate handler method.
    """

    def __init__(self, daemon: AsyncWebhookDaemon) -> None:
        self.daemon = daemon

    # ── response writers ──────────────────────────────────────────────

    @staticmethod
    def _make_response(
        status: int,
        body: dict[str, Any] | bytes,
        content_type: str = "application/json; charset=utf-8",
    ) -> bytes:
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status_text = {200: "OK", 400: "Bad Request", 403: "Forbidden",
                       404: "Not Found", 413: "Payload Too Large"}.get(status, "Unknown")
        headers = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Date: {_http_date()}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        return headers + body

    _JSON_400 = staticmethod(lambda d: _AsyncHTTPHandler._make_response(400, d))
    _JSON_200 = staticmethod(lambda d: _AsyncHTTPHandler._make_response(200, d))
    _JSON_403 = staticmethod(lambda: _AsyncHTTPHandler._make_response(403, {"error": "invalid_secret"}))
    _JSON_404 = staticmethod(lambda: _AsyncHTTPHandler._make_response(404, {"error": "not_found"}))
    _JSON_413 = staticmethod(lambda d: _AsyncHTTPHandler._make_response(413, d))

    # ── parse ─────────────────────────────────────────────────────────

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Parse one HTTP request and dispatch."""
        try:
            await self._do_handle(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("request handler error: %s", e)
            try:
                writer.write(self._make_response(500, {"error": "internal_error"}))
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _do_handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        # Request line
        request_line = await asyncio.wait_for(reader.readline(), timeout=30)
        if not request_line:
            return

        parts = request_line.decode("utf-8", errors="replace").strip().split()
        if len(parts) < 2:
            writer.write(self._make_response(400, {"error": "bad_request"}))
            await writer.drain()
            return

        method, path = parts[0].upper(), parts[1]

        # Headers
        content_length = 0
        content_type = ""
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded.lower().startswith("content-length:"):
                content_length = int(decoded.split(":", 1)[1].strip())
            elif decoded.lower().startswith("content-type:"):
                content_type = decoded.split(":", 1)[1].strip()

        # Body
        body = b""
        if content_length > 0:
            body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=30,
            )

        # Route
        if method == "GET" and path == "/health":
            writer.write(self._handle_health())
            await writer.drain()
            return

        if method == "POST" and path == "/webhook":
            resp = await self._handle_webhook(content_type, body)
            writer.write(resp)
            await writer.drain()
            return

        if method == "POST" and path == "/upload":
            resp = await self._handle_upload(content_type, body, content_length)
            writer.write(resp)
            await writer.drain()
            return

        writer.write(self._make_response(404, {"error": "not_found"}))
        await writer.drain()

    # ── health ────────────────────────────────────────────────────────

    def _handle_health(self) -> bytes:
        d = self.daemon
        return self._make_response(200, {
            "status": "ok",
            "running": d._running,
            "bots": list(d._bots.keys()),
            "messages_processed": d.messages_processed,
            "file_count": d.file_count,
            "errors": d.errors,
            "uptime_s": round(d._uptime(), 1),
            "upload": d.upload_dir is not None,
            "webhook_secret": d.webhook_secret is not None,
        })

    # ── webhook ───────────────────────────────────────────────────────

    async def _handle_webhook(
        self, content_type: str, body: bytes,
    ) -> bytes:
        d = self.daemon

        if "application/json" not in content_type:
            return self._make_response(400, {"error": "expected_json"})

        if len(body) > d.max_payload_bytes:
            return self._make_response(413, {
                "error": "payload_too_large",
                "max_bytes": d.max_payload_bytes,
                "got_bytes": len(body),
            })

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return self._make_response(400, {"error": f"invalid_json: {e}"})

        # Route to bot
        bot_id = (
            payload.get("bot_id")
            or payload.get("recipient_bot_id")
            or payload.get("recipient_id")
        )

        if not bot_id or bot_id not in d._bots:
            if len(d._bots) == 1:
                bot_id = next(iter(d._bots))
            else:
                return self._make_response(400, {
                    "error": "unknown_bot",
                    "hint": f"Known bots: {list(d._bots.keys())}",
                })

        bot_client, bot_handler, dedup = d._bots[bot_id]

        envelope = _payload_to_envelope(payload, bot_id)
        if envelope is None:
            return self._make_response(400, {"error": "unrecognized_payload"})

        if envelope.id in dedup:
            return self._make_response(200, {
                "status": "duplicate", "task_id": envelope.id,
            })
        # v0.2 fix — LRUSet evicts only the oldest entry on overflow
        # instead of blanket-clearing all 10000.
        dedup.add(envelope.id)

        d.messages_processed += 1

        if bot_handler:
            try:
                # Support both sync and async handlers
                result = bot_handler(envelope, d)
                if result is not None and hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.exception("handler failed for bot %s task %s", bot_id, envelope.id)
                d.errors += 1
                return self._make_response(200, {
                    "status": "handler_error", "bot_id": bot_id, "task_id": envelope.id,
                })

        return self._make_response(200, {
            "status": "ok", "bot_id": bot_id, "task_id": envelope.id,
        })

    # ── upload ────────────────────────────────────────────────────────

    async def _handle_upload(
        self, content_type: str, body: bytes, content_length: int,
    ) -> bytes:
        d = self.daemon

        if d.upload_dir is None:
            return self._make_response(400, {
                "error": "uploads_disabled",
                "hint": "Set upload_dir on AsyncWebhookDaemon",
            })

        if "multipart/form-data" not in content_type:
            return self._make_response(400, {"error": "expected_multipart_formdata"})

        if content_length > d.max_upload_bytes:
            return self._make_response(413, {
                "error": "upload_too_large",
                "max_bytes": d.max_upload_bytes,
                "got_bytes": content_length,
            })

        # Extract boundary (runs sync — fast enough)
        try:
            _, boundary_raw = content_type.split("boundary=", 1)
            boundary = boundary_raw.strip().strip('"').strip("'")
        except ValueError:
            return self._make_response(400, {"error": "missing_boundary"})

        # Parse multipart (I/O-bound: disk writes — run in executor)
        loop = asyncio.get_running_loop()

        def _parse():
            return _parse_multipart(body, boundary, d.upload_dir, d.max_upload_bytes)

        try:
            files = await loop.run_in_executor(None, _parse)
        except ValueError as e:
            return self._make_response(400, {"error": str(e)})

        d.file_count += len(files)
        d.messages_processed += 1

        return self._make_response(200, {
            "status": "ok",
            "uploaded": len(files),
            "files": files,
        })


# ── AsyncWebhookDaemon ──────────────────────────────────────────────────


class AsyncWebhookDaemon:
    """Asyncio-based webhook daemon for 10K+ concurrent agents.

    One event loop handles all connections. No threads per connection.
    Shares the same handler interface and bot registry pattern as
    ``WebhookDaemon`` (threading version).

    Args:
        host: Bind address (default ``\"127.0.0.1\"``).
        port: HTTP port (default ``8080``).
        webhook_secret: Optional ``X-Webhook-Secret`` validation.
        max_payload_bytes: Max JSON webhook body (default 1 MB).
        upload_dir: Directory for ``POST /upload`` file saves.
            ``None`` (default) disables uploads.
        max_upload_bytes: Max upload file size (default 10 MB).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        webhook_secret: Optional[str] = None,
        max_payload_bytes: int = 1_048_576,
        upload_dir: Optional[str] = None,
        max_upload_bytes: int = 10_485_760,
    ) -> None:
        self.host = host
        self.port = port
        self.webhook_secret = webhook_secret
        self.max_payload_bytes = max_payload_bytes
        self.upload_dir = upload_dir
        self.max_upload_bytes = max_upload_bytes

        # Multi-bot registry
        self._bots: dict[str, tuple[AgentClient, Optional[MessageHandler], set[str]]] = {}
        self._started_at: Optional[float] = 0.0

        # Server
        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False

        # Stats
        self.messages_processed: int = 0
        self.file_count: int = 0
        self.errors: int = 0

        # SSE bridges (optional)
        self._sse_bridges: list[AsyncSSEBridge] = []

    # ── bot registry ──────────────────────────────────────────────────

    def register_bot(
        self,
        bot_id: str,
        client: AgentClient,
        handler: Optional[MessageHandler] = None,
    ) -> None:
        """Register a bot for webhook routing."""
        if bot_id in self._bots:
            logger.warning(
                "AsyncWebhookDaemon: bot %r already registered, replacing", bot_id,
            )
        self._bots[bot_id] = (client, handler, LRUSet(max_size=10_000))
        logger.info(
            "AsyncWebhookDaemon: registered bot %r (total: %d)",
            bot_id, len(self._bots),
        )

    def add_sse_bridge(self, bridge: AsyncSSEBridge) -> None:
        """Add an SSE bridge that feeds events into the same handler.

        Must be called before ``start()``. The bridge shares dedup sets
        with the registered bot.
        """
        self._sse_bridges.append(bridge)

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the async HTTP server and all registered SSE bridges."""
        if self._running:
            return
        self._running = True
        self._started_at = None

        handler = _AsyncHTTPHandler(self)

        self._server = await asyncio.start_server(
            handler.handle, self.host, self.port,
            backlog=1024,  # larger backlog for 10K agents
        )

        self._started_at = __import__("time").time()
        logger.info(
            "AsyncWebhookDaemon: listening on http://%s:%s (asyncio)",
            self.host, self.port,
        )

        # Start SSE bridges
        for bridge in self._sse_bridges:
            await bridge.start()

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False

        # Stop SSE bridges
        for bridge in self._sse_bridges:
            await bridge.stop()

        # Stop HTTP server
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        logger.info("AsyncWebhookDaemon: stopped")

    # ── run forever helper ────────────────────────────────────────────

    async def wait_forever(self) -> None:
        """Block until the daemon is stopped."""
        try:
            while self._running:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await self.stop()

    # ── internal ──────────────────────────────────────────────────────

    def _uptime(self) -> float:
        if self._started_at is None:
            return 0.0
        return __import__("time").time() - self._started_at


__all__ = [
    "AsyncWebhookDaemon",
    "AsyncSSEBridge",
]
