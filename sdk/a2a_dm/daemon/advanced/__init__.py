"""Advanced A2A daemon variants for production deployments.

* :class:`A2ADaemon` — three-layer production daemon (SSE intercept +
  inbox safety-net poll + local liveness counter).
* :class:`WebhookDaemon` — HTTP webhook receiver with optional
  internal SSEBridge.
* :class:`AsyncWebhookDaemon` — asyncio webhook daemon for 10K+
  concurrent agents on one event loop.
* :class:`SSEBridge` / :class:`AsyncSSEBridge` — standalone SSE stream
  connectors (sync and async).
* :func:`daemon_from_config` — multi-bot factory from a config dict
  (load from YAML / JSON / env in the caller).
"""

from a2a_dm.daemon.advanced._a2a import A2ADaemon, daemon_from_config
from a2a_dm.daemon.advanced._async_webhook import AsyncSSEBridge, AsyncWebhookDaemon
from a2a_dm.daemon.advanced._orchestrated import OrchestratedDaemon
from a2a_dm.daemon.advanced._pingpong import (
    extract_pd,
    next_round,
    should_continue,
)
from a2a_dm.daemon.advanced._webhook import SSEBridge, WebhookDaemon

__all__ = [
    "A2ADaemon",
    "OrchestratedDaemon",
    "WebhookDaemon",
    "AsyncWebhookDaemon",
    "SSEBridge",
    "AsyncSSEBridge",
    "daemon_from_config",
    "extract_pd",
    "next_round",
    "should_continue",
]
