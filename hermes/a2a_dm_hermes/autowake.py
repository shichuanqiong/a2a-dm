"""True auto-wake — turn an inbound DM into a real agent turn (v0.1.2).

The v0.1.1 wake path only *queued* DMs for the next turn; nothing ran
until the human talked to the agent. This module closes that gap using
the Hermes gateway's generic webhook adapter:

  SSE event → signed POST to ``/webhooks/a2a-dm-wake`` → the gateway
  renders our prompt template, spins up an ``AIAgent``, and the agent
  handles the DM immediately — replying via ``a2a_reply`` /
  ``a2a_send_group`` with the bundled ``a2a-dm`` skill loaded. The
  agent's response is additionally delivered to the operator's home
  channel (Telegram by default), so the human sees the exchange live.

Route registration is fully automatic: Hermes hot-reloads dynamic
routes from ``~/.hermes/webhook_subscriptions.json`` (mtime-gated, on
every POST), so :func:`ensure_ready` just writes the file — no
config.yaml edits, no gateway restart.

Idempotency: we pass the a2a task_id as ``X-Request-ID``; the gateway
keeps a 1-hour TTL cache of delivery IDs, so the SSE path and the
30-second polling fallback can never double-trigger a turn for the
same DM.

Env:
  A2A_AUTO_WAKE        "1" (default) — set "0" to disable turn injection.
  A2A_WAKE_WEBHOOK_URL override gateway webhook base URL.
  A2A_WAKE_HOME        where responses/notifications land
                       ("telegram" | "telegram:<chat_id>" | ...).
"""

from __future__ import annotations

import logging
import os
import threading

from a2a_dm_hermes import gatewaycfg

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return (os.environ.get("A2A_AUTO_WAKE") or "1").strip() not in ("0", "false", "no")


class AutoWake:
    """Owns route registration + DM→turn injection. Thread-safe."""

    def __init__(self, bot_id: str) -> None:
        self._bot_id = bot_id
        self._lock = threading.Lock()
        self._ready = False
        self._warned_unavailable = False

    def ensure_ready(self) -> bool:
        """Register/refresh our dynamic routes. Cheap after first call."""
        if not enabled():
            return False
        with self._lock:
            if self._ready:
                return True
            if not gatewaycfg.webhook_platform_enabled():
                if not self._warned_unavailable:
                    self._warned_unavailable = True
                    logger.warning(
                        "a2a-dm: auto-wake unavailable — the gateway's "
                        "webhook platform is not enabled. Add to "
                        "~/.hermes/config.yaml:\n"
                        "  platforms:\n"
                        "    webhook:\n"
                        "      enabled: true\n"
                        "      extra: { host: 127.0.0.1, port: 8644 }\n"
                        "then restart the gateway. Falling back to "
                        "next-turn wake injection."
                    )
                return False
            self._ready = gatewaycfg.ensure_routes(self._bot_id)
            return self._ready

    def wake(self, entry: dict) -> bool:
        """POST one DM entry to the wake route → triggers an agent turn.

        *entry* is the WakeRuntime dict (task_id / sender_bot_id / text
        / group_id / created_at). Returns True if the gateway accepted
        (2xx, including the idempotent-duplicate 200).
        """
        if not self.ensure_ready():
            return False
        payload = {
            "event_type": "a2a.dm",
            "task_id": entry.get("task_id") or "",
            "sender_bot_id": entry.get("sender_bot_id") or "",
            "text": entry.get("text") or "",
            "group_id": entry.get("group_id") or "",
            "created_at": entry.get("created_at") or "",
        }
        ok = gatewaycfg.post_route(
            gatewaycfg.WAKE_ROUTE,
            payload,
            request_id=payload["task_id"] or None,
        )
        if ok:
            logger.info(
                "a2a-dm: auto-wake turn injected (from=%s task=%s)",
                payload["sender_bot_id"], payload["task_id"][:12],
            )
        return ok
