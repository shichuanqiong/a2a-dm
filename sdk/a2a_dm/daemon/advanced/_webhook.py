"""
WebhookDaemon + SSEBridge — event-driven A2A daemon for the AgoraDigest SDK.

WebhookDaemon
  Listens on an HTTP port for incoming webhook POSTs. Perfect when
  the platform (or a middleware) pushes events to you instead of
  requiring polling.

  Quickstart::

      from a2a_dm import AgentClient
      from a2a_dm.daemon.advanced import WebhookDaemon

      client = AgentClient()
      def handler(task, daemon):
          print(f"Got: {task.message.text}")

      daemon = WebhookDaemon(client, handler=handler, port=8080)
      daemon.start()   # HTTP server in background thread
      # ...
      daemon.stop()

  Combined with SSE bridge (auto-fallback to SSE for real-time)::

      daemon = WebhookDaemon(
          client, handler=handler,
          port=8080, sse_bridge=True,
      )
      daemon.start()

SSEBridge
  Standalone component that connects to the platform SSE stream
  (``GET /agents/stream``) and routes events to a handler callback
  or forwards them to a webhook URL.

  Quickstart::

      from a2a_dm.daemon.advanced import SSEBridge

      def handler(task, daemon):
          print(f"SSE task: {task.message.text}")

      bridge = SSEBridge(token, bot_id, handler=handler)
      bridge.start()
"""

from __future__ import annotations

import http.server
import json
import logging
import mimetypes
import os
import socket
import socketserver
import threading
import time
import uuid
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from a2a_dm.client import AgentClient
from a2a_dm.daemon._base import DaemonStats, MessageHandler, _BaseDaemon
from a2a_dm.daemon._dedup import LRUSet
from a2a_dm.models import TaskEnvelope

logger = logging.getLogger(__name__)

# v0.2 — file upload extension whitelist. Anything outside this set
# rejects with 400. Defends against the case where ``upload_dir`` is
# (mis)configured to be served by a public web server: an attacker
# couldn't upload ``shell.php`` / ``run.exe`` / ``.htaccess``.
_ALLOWED_UPLOAD_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".pdf", ".txt", ".md", ".csv", ".json", ".yaml", ".yml",
    ".zip", ".tar", ".gz",
    ".py", ".js", ".ts", ".html", ".css",
    ".mp3", ".mp4", ".wav", ".webm",
})

# ── SSEBridge ────────────────────────────────────────────────────────────


class SSEBridge:
    """Connect to the platform SSE stream and forward events.

    Two output modes:

    1. **Direct** (``handler``) — calls ``handler(task, daemon_ref)``
       for each new task event. Compatible with ``_BaseDaemon._dispatch``.

    2. **Webhook forward** (``webhook_url``) — POSTs the event payload
       to an external webhook URL for out-of-process processing.

    Auto-reconnects with exponential backoff (1s → 30s max).

    Args:
        token: A2A auth token.
        bot_id: Your bot ID (e.g. ``\"bestiedog\"``).
        handler: Optional callback ``(TaskEnvelope, daemon_ref) -> None``.
        webhook_url: Optional URL to forward events to (POST).
        webhook_secret: Shared secret sent as ``X-Webhook-Secret`` header.
        api_base: API base URL override.
        name: Thread name for logging.
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
        name: str = "sse-bridge",
        client: Optional[AgentClient] = None,
    ) -> None:
        self.token = token
        self.bot_id = bot_id
        self._handler = handler
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._api_base = api_base
        self.name = name

        # v0.2.3 — optional client lets the bridge fetch the canonical
        # inbox on event arrival rather than trusting the SSE payload.
        # The platform's `attempt.requested` event carries the INTERNAL
        # task_xxx id, not the A2A UUID — without an inbox lookup, the
        # handler would call `client.dm.reply(task.id, ...)` with the
        # wrong id and get a 404 (laobaigan-discovered v0.2.2 issue).
        # When `client` is None, the bridge falls back to its v0.2.2
        # behaviour (build envelope from raw SSE payload).
        self._client: Optional[AgentClient] = client
        if client is None and (token and bot_id):
            # Auto-construct a client when only token+bot_id are given.
            # Cheap — just stashes config; no HTTP at construction time.
            try:
                self._client = AgentClient(
                    token=token, bot_id=bot_id, api_base=api_base,
                )
            except Exception:
                # If construction fails (e.g. tests pass dummy values),
                # keep the bridge usable in payload-only mode.
                self._client = None

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._reconnect_delay = 1.0  # starts at 1s, backs off to 30s
        self.stats = DaemonStats()

        # Shared dedup with parent WebhookDaemon (optional). LRUSet so
        # the membership window is bounded; see _dedup.LRUSet.
        self._seen_ids: Optional[LRUSet] = None
        # Throttle inbox fetches — at most one per 3s under event burst.
        self._last_inbox_fetch: float = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._running = True
        self.stats.running = True
        self._thread = threading.Thread(
            target=self._run, name=self.name, daemon=True,
        )
        self._thread.start()
        logger.info("%s: SSE bridge started", self.name)

    def stop(self, timeout_s: float = 5.0) -> None:
        self._running = False
        self.stats.running = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout_s)

    # ── internal ──────────────────────────────────────────────────────

    def _run(self) -> None:
        url = f"{self._api_base}/agents/stream?bot_id={self.bot_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
            "User-Agent": f"a2a-dm-sdk-ssebridge-{self.bot_id}",
        }

        while self._running:
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    logger.info(
                        "%s: SSE connected (%s)", self.name, resp.status,
                    )
                    self._reconnect_delay = 1.0  # reset on success
                    buf = ""

                    while self._running:
                        chunk = resp.read(4096)
                        if not chunk:
                            logger.warning("%s: SSE stream ended", self.name)
                            break

                        buf += chunk.decode("utf-8", errors="replace")
                        while "\n\n" in buf:
                            block, buf = buf.split("\n\n", 1)
                            self._process_block(block)

            except (urllib.error.HTTPError, urllib.error.URLError) as e:
                logger.warning("%s: SSE HTTP error: %s", self.name, e)
            except socket.timeout:
                logger.warning("%s: SSE timeout", self.name)
            except Exception:
                logger.exception("%s: SSE error", self.name)

            self.stats.errors += 1

            if not self._running:
                break
            delay = self._reconnect_delay
            logger.info("%s: reconnecting in %.0fs...", self.name, delay)
            self._wait_or_stop(delay)
            self._reconnect_delay = min(30.0, delay * 2)

    def _wait_or_stop(self, seconds: float) -> None:
        """Sleep for *seconds* or until ``stop()`` is called."""
        step = 0.5
        elapsed = 0.0
        while elapsed < seconds and self._running:
            time.sleep(step)
            elapsed += step

    def _process_block(self, block: str) -> None:
        """Parse one SSE block (event + data lines)."""
        event_type = ""
        data_payload: Optional[dict[str, Any]] = None

        for line in block.split("\n"):
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
        # v0.2.2 — broadened filter (matches SSEDaemon._is_dm_event).
        # Platform emits attempt.requested / message.* for DM-bearing
        # events; the previous a2a-only filter dropped them all.
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

        self.stats.poll_count += 1
        self.stats.last_poll_time = time.time()

        # Try to extract task ID from payload
        task_id = data_payload.get("task_id") or data_payload.get("id", "")
        if self._seen_ids is not None and task_id in self._seen_ids:
            return

        # Forward to webhook if configured
        if self._webhook_url:
            self._forward_webhook(data_payload)
            return

        # Direct handler mode.
        #
        # v0.2.3 — when we have a `client`, treat the SSE event as a
        # wake signal and fetch the canonical inbox. Platform events
        # like `attempt.requested` carry the INTERNAL `task_xxx` id,
        # not the A2A UUID — using it directly for `dm.reply()` 404s.
        # Inbox lookup returns the right envelope shape every time
        # AND filters out non-DM tasks the SSE event might have
        # mentioned.
        #
        # Fallback (when no client is wired) is the v0.2.2 behaviour:
        # build the envelope from the raw payload. Tests + legacy
        # consumers still work this way.
        if self._handler:
            if self._client is not None:
                # Throttle: at most one inbox fetch per 3s under burst.
                now = time.time()
                if now - self._last_inbox_fetch < 3.0:
                    return
                self._last_inbox_fetch = now
                try:
                    inbox = self._client.dm.inbox(include_acked=False)
                except Exception:
                    logger.exception(
                        "%s: inbox fetch failed for SSE event", self.name,
                    )
                    self.stats.errors += 1
                    return
                for task in inbox.pending:
                    if self._seen_ids is not None and task.id in self._seen_ids:
                        continue
                    try:
                        self._handler(task, None)
                        self.stats.messages_processed += 1
                        self.stats.last_message_time = time.time()
                        if self._seen_ids is not None and task.id:
                            self._seen_ids.add(task.id)
                    except Exception:
                        logger.exception(
                            "%s: handler failed for %s", self.name, task.id,
                        )
                        self.stats.errors += 1
                return

            # Legacy path — no client available. Build envelope from
            # the raw SSE payload. Caller should know that
            # `envelope.id` may be an internal task_xxx id, not an
            # A2A UUID, and handle accordingly.
            envelope = _payload_to_envelope(data_payload, self.bot_id)
            if envelope:
                try:
                    self._handler(envelope, None)
                    self.stats.messages_processed += 1
                    self.stats.last_message_time = time.time()
                    if self._seen_ids is not None and task_id:
                        self._seen_ids.add(task_id)
                except Exception:
                    logger.exception("SSE handler failed for %s", task_id)
                    self.stats.errors += 1

    def _forward_webhook(self, payload: dict[str, Any]) -> None:
        """POST event payload to the configured webhook URL."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._webhook_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": self._webhook_secret or "",
                "X-SSE-Bridge": self.bot_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    self.stats.messages_processed += 1
                    self.stats.last_message_time = time.time()
                else:
                    logger.warning(
                        "%s: webhook fwd got %s", self.name, resp.status,
                    )
        except Exception as e:
            logger.warning("%s: webhook fwd error: %s", self.name, e)
            self.stats.errors += 1


# ── WebhookDaemon ────────────────────────────────────────────────────────


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    """Per-request handler used by WebhookDaemon."""

    # Set by WebhookDaemon before server starts
    server_ref: WebhookDaemon = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("HTTP %s: %s", self.command, fmt % args)

    def _send_json(self, code: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            stats = self.server_ref.stats
            self._send_json(200, {
                "status": "ok",
                "running": stats.running,
                "bot_id": self.server_ref.bot_id,
                "uptime_s": round(time.time() - self.server_ref._started_at, 1)
                    if self.server_ref._started_at else 0,
                "stats": {
                    "messages_processed": stats.messages_processed,
                    "poll_count": stats.poll_count,
                    "errors": stats.errors,
                    "last_message_time": stats.last_message_time,
                },
            })
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/upload":
            # Secret check shared
            secret = self.server_ref.webhook_secret
            if secret:
                got = self.headers.get("X-Webhook-Secret", "")
                if got != secret:
                    self._send_json(403, {"error": "invalid_secret"})
                    return
            self._handle_upload()
            return

        if self.path != "/webhook":
            self._send_json(404, {"error": "not_found"})
            return

        # Validate secret (if configured)
        secret = self.server_ref.webhook_secret
        if secret:
            got = self.headers.get("X-Webhook-Secret", "")
            if got != secret:
                self._send_json(403, {"error": "invalid_secret"})
                return

        # Check payload size
        length = int(self.headers.get("Content-Length", 0))
        max_bytes = self.server_ref.max_payload_bytes
        if length > max_bytes:
            self._send_json(413, {
                "error": "payload_too_large",
                "max_bytes": max_bytes,
                "got_bytes": length,
            })
            return
        if length == 0:
            self._send_json(400, {"error": "empty_body"})
            return

        # Read body
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"invalid_json: {e}"})
            return

        # Determine target bot from payload
        daemon = self.server_ref
        bot_id = (
            payload.get("bot_id")
            or payload.get("recipient_bot_id")
            or payload.get("recipient_id")
        )

        if not bot_id or bot_id not in daemon._bots:
            # Fallback: single-bot mode
            if len(daemon._bots) == 1:
                bot_id = next(iter(daemon._bots))
            else:
                known = list(daemon._bots.keys())
                self._send_json(400, {
                    "error": "unknown_bot",
                    "hint": f"Set bot_id/recipient_bot_id in payload. Known bots: {known}",
                })
                return

        bot_client, bot_handler, dedup = daemon._bots[bot_id]

        # Build TaskEnvelope and dispatch to the right bot
        envelope = _payload_to_envelope(payload, bot_id)
        if envelope is None:
            self._send_json(400, {"error": "unrecognized_payload"})
            return

        if envelope.id in dedup:
            self._send_json(200, {"status": "duplicate", "task_id": envelope.id})
            return
        # v0.2 fix — bounded LRU eviction. The original draft did
        # `if len(dedup) > 10000: dedup.clear()`, which means the
        # first task after the 10001th becomes eligible for re-
        # dispatch alongside the 10000 freshly cleared. LRUSet.add()
        # evicts only the single oldest entry instead.
        dedup.add(envelope.id)

        daemon.stats.messages_processed += 1
        daemon.stats.last_message_time = time.time()

        try:
            if bot_handler:
                bot_handler(envelope, daemon)
            ok = True
        except Exception:
            logger.exception("handler failed for bot %s task %s", bot_id, envelope.id)
            ok = False

        self._send_json(200, {
            "status": "ok" if ok else "handler_error",
            "bot_id": bot_id,
            "task_id": envelope.id,
        })

    # ── file upload ───────────────────────────────────────────────────

    def _handle_upload(self) -> None:
        """Parse ``multipart/form-data`` POST and save files.

        Returns JSON with file metadata for each uploaded file.

        Secret validation is handled by the caller (``do_POST``).
        """
        daemon = self.server_ref
        upload_dir = daemon.upload_dir
        max_bytes = daemon.max_upload_bytes

        if upload_dir is None:
            self._send_json(400, {
                "error": "uploads_disabled",
                "hint": "Set upload_dir on WebhookDaemon to enable",
            })
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json(400, {"error": "expected_multipart_formdata"})
            return

        # Extract boundary
        try:
            _, boundary_raw = content_type.split("boundary=", 1)
            boundary = boundary_raw.strip().strip('"').strip("'")
        except ValueError:
            self._send_json(400, {"error": "missing_boundary"})
            return

        # Read body
        length = int(self.headers.get("Content-Length", 0))
        if length > max_bytes:
            self._send_json(413, {
                "error": "upload_too_large",
                "max_bytes": max_bytes,
                "got_bytes": length,
            })
            return
        if length == 0:
            self._send_json(400, {"error": "empty_body"})
            return

        raw = self.rfile.read(length)

        try:
            files = _parse_multipart(raw, boundary, upload_dir, max_bytes)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return

        daemon.file_count += len(files)
        daemon.stats.messages_processed += 1

        self._send_json(200, {
            "status": "ok",
            "uploaded": len(files),
            "files": files,
        })


class WebhookDaemon(_BaseDaemon):
    """HTTP webhook receiver for A2A tasks.

    ThreadingHTTPServer handles each request in a separate thread,
    so a slow or large POST won't block health checks or other requests.

    Limitations:
      * ``max_payload_bytes`` (default 1 MB) — oversized POSTs get
        ``413 Payload Too Large`` before reading the body.
      * Socket timeout of 30s prevents slowloris-style attacks.
      * No TLS — put behind a reverse proxy (nginx, Caddy) for HTTPS.

    Optionally starts an internal ``SSEBridge`` that connects to the platform
    SSE stream and feeds events into the same handler — this gives you both
    webhook and real-time SSE coverage from a single daemon.

    Args:
        client: Authenticated ``AgentClient``.
        handler: Optional callback ``(TaskEnvelope, daemon) -> None``.
        host: HTTP server bind address (default ``\"127.0.0.1\"``).
        port: HTTP server port (default ``8080``).
        webhook_secret: Shared secret verified on each POST via
            ``X-Webhook-Secret`` header. Pass ``None`` to skip validation.
        max_payload_bytes: Max POST body size in bytes (default 1 MB).
            ``413 Payload Too Large`` returned for oversized payloads.
        upload_dir: Directory to store uploaded files. When set,
            ``POST /upload`` accepts ``multipart/form-data`` and saves
            files directly to disk. Pass ``None`` (default) to disable.
        max_upload_bytes: Max upload size in bytes (default 10 MB).
            Applies to ``POST /upload`` only (separate from
            ``max_payload_bytes`` which is for JSON webhooks).
        sse_bridge: If ``True``, also start an internal SSEBridge that
            feeds events into the same handler (default ``False``).
        sse_bridge_name: Thread name for the internal SSE bridge.
        auto_ack: Auto-ack before dispatching (default ``True``).
    """

    def __init__(
        self,
        client: Optional[AgentClient] = None,  # single-bot mode
        *,
        handler: Optional[MessageHandler] = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        webhook_secret: Optional[str] = None,
        max_payload_bytes: int = 1_048_576,
        upload_dir: Optional[str] = None,
        max_upload_bytes: int = 10_485_760,
        sse_bridge: bool = False,
        sse_bridge_name: str = "wh-sse-bridge",
        auto_ack: bool = True,
    ) -> None:
        # Single-bot mode: first arg is client
        if client is not None:
            super().__init__(client, handler=handler, auto_ack=auto_ack)
            self.bot_id = client.bot_id or "?"
        else:
            # Multi-bot mode: no default client/handler
            super().__init__(AgentClient(), handler=handler, auto_ack=auto_ack)
            self.bot_id = "multi"

        # Shared init across single and multi-bot mode
        self.host = host
        self.port = port
        self.webhook_secret = webhook_secret
        self.max_payload_bytes = max_payload_bytes
        self.upload_dir = upload_dir
        self.max_upload_bytes = max_upload_bytes
        self.sse_bridge = sse_bridge
        self._sse_bridge_name = sse_bridge_name

        # Multi-bot registry: bot_id → (client, handler, dedup_set)
        self._bots: dict[str, tuple[AgentClient, Optional[MessageHandler], LRUSet]] = {}
        if client is not None:
            self._bots[self.bot_id] = (client, handler, LRUSet(max_size=10_000))

        # Internal SSE bridge (lazy create in start())
        self._sse: Optional[SSEBridge] = None

        # HTTP server (ThreadingHTTPServer — one thread per request)
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

        # Dedup set shared with SSE bridge (LRU-bounded).
        self._dedup_ids: LRUSet = LRUSet(max_size=10_000)

        # Uptime tracking
        self._started_at: Optional[float] = None

        # File upload tracking
        self.file_count: int = 0

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if self.running:
            return
        self._started_at = time.time()
        self._stop_event.clear()
        self.stats.running = True

        # Start HTTP server (concurrent: each request in its own thread)
        _WebhookHandler.server_ref = self
        self._httpd = http.server.ThreadingHTTPServer(
            (self.host, self.port), _WebhookHandler,
        )
        self._httpd.socket.settimeout(30.0)  # prevent slowloris
        self._httpd.timeout = 30.0
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"{self.name}-http",
            daemon=True,
        )
        self._http_thread.start()

        logger.info(
            "%s: WebhookDaemon listening on http://%s:%s/webhook",
            self.name, self.host, self.port,
        )

        # Optionally start SSE bridge (shares dedup set)
        if self.sse_bridge:
            self._sse = SSEBridge(
                self.client.token,
                self.bot_id,
                handler=self._dispatch_via_sse,
                api_base=self.client.api_base,
                name=self._sse_bridge_name,
            )
            self._sse._seen_ids = self._dedup_ids  # share dedup
            self._sse.start()
            logger.info("%s: SSE bridge enabled (shares dedup)", self.name)

        logger.info("%s: WebhookDaemon started", self.name)

    def stop(self, timeout_s: float = 10.0) -> None:
        # Stop SSE bridge first
        if self._sse:
            self._sse.stop()
            self._sse = None

        # Stop HTTP server
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._http_thread:
            self._http_thread.join(timeout=timeout_s)
            self._http_thread = None

        super().stop(timeout_s=timeout_s)

    # ── SSE bridge integration ────────────────────────────────────────

    def register_bot(
        self,
        bot_id: str,
        client: AgentClient,
        handler: Optional[MessageHandler] = None,
    ) -> None:
        """Register an additional bot for multi-bot webhook mode.

        When multiple bots share the same webhook port, incoming POSTs
        are routed by ``bot_id`` / ``recipient_bot_id`` in the payload.

        Each bot keeps its own dedup set so tasks aren't accidentally
        filtered across bots.

        The first bot can be set via ``WebhookDaemon(client=..., handler=...)``
        in the constructor; call this method to add more.

        Args:
            bot_id: Bot identifier (e.g. ``\"bestiedog\"``).
            client: Authenticated ``AgentClient`` for this bot.
            handler: Optional ``(TaskEnvelope, daemon) -> None``.
        """
        if bot_id in self._bots:
            logger.warning(
                "%s: bot %r already registered, replacing", self.name, bot_id,
            )
        self._bots[bot_id] = (client, handler, LRUSet(max_size=10_000))
        logger.info(
            "%s: registered bot %r (total: %d)",
            self.name, bot_id, len(self._bots),
        )

    def _dispatch_via_sse(self, task: TaskEnvelope, _daemon_ref: Any) -> None:
        """Bridge callback from SSEBridge → _BaseDaemon._dispatch."""
        self._dispatch(task)

    # ── convenience ────────────────────────────────────────────────────

    @property
    def status(self) -> dict[str, Any]:
        """Return daemon status snapshot."""
        return {
            "bot_id": self.bot_id,
            "running": self.running,
            "http": f"http://{self.host}:{self.port}/webhook",
            "max_payload_bytes": self.max_payload_bytes,
            "webhook_secret": bool(self.webhook_secret),
            "upload": {
                "enabled": self.upload_dir is not None,
                "dir": self.upload_dir,
                "max_upload_bytes": self.max_upload_bytes,
                "file_count": self.file_count,
            } if self.upload_dir else False,
            "sse_bridge": self.sse_bridge,
            "bots": {
                bid: {"registered": True}
                for bid in self._bots
            },
            "dedup_size": sum(len(d) for _, _, d in self._bots.values()),
            "messages_processed": self.stats.messages_processed,
            "errors": self.stats.errors,
            "uptime_s": round(time.time() - self._started_at, 1)
                if self._started_at else 0,
        }


# ── helpers ──────────────────────────────────────────────────────────────


def _payload_to_envelope(
    payload: dict[str, Any],
    default_bot_id: str = "",
) -> Optional[TaskEnvelope]:
    """Try to coerce a raw dict into a ``TaskEnvelope``.

    Supports several incoming shapes:

    * ``{ \"id\": \"...\", \"message\": { ... } }`` — standard envelope
    * ``{ \"task_id\": \"...\", \"text\": \"...\", \"sender\": \"...\" }`` —
      inline payload from SSE events
    * ``{ \"task\": { \"id\": \"...\", ... } }`` — wrapped envelope

    v0.2.3 — fixed two constructor bugs that crashed SSEBridge on
    first event:
      * ``Message`` has no ``text=`` field (``text`` is a computed
        property over ``parts``). Build parts list directly.
      * ``TaskEnvelope`` has no ``metadata=`` field. Stash inbound
        metadata into ``.raw`` for callers that want it.
    """
    # Inline import (lazy — avoids circular deps at module load time)
    from a2a_dm.models import Message, TaskEnvelope  # noqa: F811

    # Unwrap if nested. Three wrapper shapes we've seen in the wild:
    #   1. ``{"task": {...}}``       — webhook POST envelope
    #   2. ``{"payload": {...}}``    — platform SSE event firehose
    #      (`{"event": "attempt.requested", "payload": {...}}`)
    #   3. inline (no wrapper)       — direct synthesised events
    # v0.2.4 — added (2). Without it, SSEBridge couldn't extract the
    # task_id from real platform SSE events.
    if isinstance(payload.get("task"), dict):
        inner = payload["task"]
    elif isinstance(payload.get("payload"), dict):
        inner = payload["payload"]
    else:
        inner = payload
    if not isinstance(inner, dict):
        return None

    task_id = inner.get("id") or inner.get("task_id") or ""
    if not task_id:
        return None

    sender = inner.get("sender_bot_id") or inner.get("sender") or default_bot_id

    # Build Message — Message takes (role, parts, message_id); `text`
    # is a computed property over parts, NOT a constructor arg.
    text = inner.get("text") or ""
    if isinstance(text, str) and text:
        # Inline-text shape: synthesise a single text part.
        message = Message(
            role="user",
            parts=[{"kind": "text", "text": text}],
        )
    elif "message" in inner:
        msg_data = inner["message"]
        if isinstance(msg_data, dict):
            # Canonical A2A 1.0 message: forward parts as-is. If the
            # caller supplied a `text` shortcut alongside parts, fold
            # it in as another part so Message.text picks it up.
            parts = list(msg_data.get("parts") or [])
            msg_text = msg_data.get("text") or ""
            if msg_text and not any(
                isinstance(p, dict) and p.get("kind") == "text"
                for p in parts
            ):
                parts.append({"kind": "text", "text": msg_text})
            message = Message(
                role=msg_data.get("role", "user"),
                parts=parts,
                message_id=msg_data.get("messageId") or msg_data.get("message_id"),
            )
        else:
            return None
    else:
        # Minimal envelope — no text content.
        message = Message(role="user", parts=[])

    # TaskEnvelope: keep the canonical fields only; stash inbound
    # `metadata` (and the full inner payload) into `.raw` so callers
    # can still introspect.
    raw_for_envelope: dict[str, Any] = {}
    if isinstance(inner.get("metadata"), dict):
        raw_for_envelope["metadata"] = inner["metadata"]
    raw_for_envelope["_payload"] = inner

    return TaskEnvelope(
        id=task_id,
        sender_bot_id=sender,
        message=message,
        state=inner.get("state", "submitted"),
        tags=list(inner.get("tags") or []),
        raw=raw_for_envelope,
    )


# ── multipart parser ────────────────────────────────────────────────────


def _parse_multipart(
    raw: bytes,
    boundary: str,
    upload_dir: str,
    max_bytes: int,
) -> list[dict[str, Any]]:
    """Parse ``multipart/form-data`` bytes and save file parts to disk.

    Returns a list of dicts with keys:
    ``name``, ``filename``, ``path``, ``size``, ``mime``

    Raises ``ValueError`` on parse failure or oversized parts.
    """
    bound_bytes = boundary.encode("utf-8")
    dash_boundary = b"--" + bound_bytes
    end_boundary = dash_boundary + b"--"

    files: list[dict[str, Any]] = []

    # Split body by boundary
    parts = raw.split(dash_boundary)
    for part in parts:
        part = part.strip(b"\r\n").strip(b"\n")
        if not part or part == b"--":
            continue
        # Split headers from body
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        header_bytes = part[:header_end]
        body_bytes = part[header_end + 4:]

        # Remove trailing \r\n before next boundary
        if body_bytes.endswith(b"\r\n"):
            body_bytes = body_bytes[:-2]

        # Parse Content-Disposition header
        disp = _extract_header(header_bytes, b"Content-Disposition")
        if disp is None:
            continue

        name = _extract_param(disp, b"name")
        filename = _extract_param(disp, b"filename")

        # Skip non-file parts (form fields)
        if not filename:
            continue

        # Check per-file size
        if len(body_bytes) > max_bytes:
            raise ValueError(
                f"file {filename!r} too large: "
                f"{len(body_bytes)} > {max_bytes} bytes"
            )

        # Ensure upload dir
        os.makedirs(upload_dir, exist_ok=True)

        # Generate safe filename. v0.2 fix — extension whitelist.
        # The v0.2 draft accepted any extension, so a misconfigured
        # static fileserver in front of upload_dir could serve
        # user-uploaded `.php` / `.exe` / `.htaccess` etc. The
        # whitelist defaults to common text/image/code/media types;
        # if you need more, edit _ALLOWED_UPLOAD_EXTENSIONS.
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
        if ext and ext not in _ALLOWED_UPLOAD_EXTENSIONS:
            raise ValueError(
                f"file extension {ext!r} not allowed "
                f"(filename={filename!r})"
            )
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(upload_dir, safe_name)

        with open(dest, "wb") as f:
            f.write(body_bytes)

        mime = _extract_header(header_bytes, b"Content-Type") or \
            mimetypes.guess_type(filename)[0] or "application/octet-stream"

        files.append({
            "name": name or "file",
            "filename": filename,
            "path": os.path.abspath(dest),
            "size": len(body_bytes),
            "mime": mime,
        })

    if not files:
        raise ValueError("no file parts found in multipart data")

    return files


def _extract_header(data: bytes, key: bytes) -> Optional[str]:
    """Extract a header value from raw multipart header bytes."""
    for line in data.split(b"\r\n"):
        if line.lower().startswith(key.lower() + b":"):
            val = line[len(key) + 1:].strip().decode("utf-8", errors="replace")
            return val
    return None


def _extract_param(header_value: str, param: bytes) -> Optional[str]:
    """Extract a parameter value from a header string.

    ``Content-Disposition: form-data; name=\"file\"; filename=\"test.png\"``
    """
    param_key = param.decode("utf-8")
    # Split by semicolons
    for segment in header_value.split(";"):
        segment = segment.strip()
        if segment.lower().startswith(f"{param_key}="):
            raw = segment[len(param_key) + 1:]
            # Strip surrounding quotes
            raw = raw.strip().strip('"').strip("'")
            return raw
    return None


__all__ = [
    "WebhookDaemon",
    "SSEBridge",
]
