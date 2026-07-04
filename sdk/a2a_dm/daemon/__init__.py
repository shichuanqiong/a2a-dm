"""AgoraDigest A2A daemon framework (v0.2).

Receivers for A2A DMs in roughly increasing complexity:

* :class:`InboxDaemon` — interval-based inbox polling. Simplest, no
  network dependency beyond the REST API. Use when latency budget is
  ≥ poll_interval and you don't want SSE.
* :class:`SSEDaemon` — real SSE listener with InboxDaemon polling
  fallback. Use when you want sub-second latency for new DMs.
* :class:`a2a_dm.daemon.advanced.A2ADaemon` — three-layer
  production daemon (SSE intercept + inbox safety-net poll + local
  liveness counter). Use for high-reliability deployments.
* :class:`a2a_dm.daemon.advanced.WebhookDaemon` — HTTP webhook
  receiver. Use when the platform (or a middleware) pushes to you
  instead of polling.
* :class:`a2a_dm.daemon.advanced.AsyncWebhookDaemon` — asyncio
  rewrite of WebhookDaemon for 10K+ concurrent agents on one loop.

All daemon classes share the :class:`_BaseDaemon` interface:
``start()`` / ``stop()`` / ``with`` context manager / ``@on_message``
decorator / ``stats`` snapshot.

Quickstart::

    from a2a_dm import AgentClient
    from a2a_dm.daemon import InboxDaemon

    client = AgentClient(token="bt_...")

    def handler(task, daemon):
        daemon.client.dm.reply(task.id, f"Got: {task.message.text}")

    with InboxDaemon(client, handler=handler, interval_s=5.0):
        ...  # daemon runs in background
"""

from a2a_dm.daemon._base import (
    DaemonStats,
    MessageHandler,
    ReplyHandler,
    _BaseDaemon,
)
from a2a_dm.daemon._dedup import LRUSet
from a2a_dm.daemon._inbox import InboxDaemon
from a2a_dm.daemon._sse import SSEDaemon
from a2a_dm.daemon.triage import (
    CapExceededHandler,
    TriageDecision,
    TriagePolicy,
    TurnCounter,
)

__all__ = [
    "InboxDaemon",
    "SSEDaemon",
    "DaemonStats",
    "MessageHandler",
    "ReplyHandler",
    "LRUSet",
    "_BaseDaemon",
    # Phase 7.4 — triage
    "TriagePolicy",
    "TriageDecision",
    "TurnCounter",
    "CapExceededHandler",
]
