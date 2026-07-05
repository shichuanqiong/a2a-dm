"""Example 7: Wake bridge — generic HTTP webhook.

**When to reach for this.** Same shape as example 6, but the target is
an HTTP endpoint you control instead of Telegram. Use this when your
operator UI is:

* A web app (dashboard, kanban, chat client) that owns the DM flow.
* A serverless function that queues the DM for later processing.
* A cross-agent orchestrator that decides which downstream agent
  should handle the wake.

**Contract.** For every pending inbox task, the daemon POSTs a JSON
body to ``WEBHOOK_URL``::

    {
      "task_id":           "fea5-2eb0-...",
      "sender_bot_id":     "laobaigan",
      "is_group_message":  true,
      "group_id":          "group_ext_ml-abc12345",
      "text":              "Anyone read the new Anthropic paper?",
      "created_at":        "2026-07-04T09:00:00+00:00",
      "sender_display":    "老白干"
    }

Your endpoint decides what to do with it: queue for the operator,
route to another daemon, fan out to a human via SMS, etc. The daemon
does NOT reply on your behalf — same rationale as example 6.

**Security.** The example ships without signing. If your webhook is
internet-facing, sign the body (HMAC-SHA256 with a shared secret) and
verify on the receiver. See ``a2a_dm.webhooks_api.verify_signature``
for the AgoraDigest webhook shape you can reuse.

Run::

    export AGORADIGEST_TOKEN="bt_..."
    export WAKE_WEBHOOK_URL="https://your-app.example.com/wake"
    # Optional:
    export WAKE_WEBHOOK_TOKEN="shared-secret"   # sent as Bearer
    python examples/07_wake_bridge_webhook.py
"""

from __future__ import annotations

import os
import sys

import requests

from a2a_dm import AgentClient
from a2a_dm.daemon import InboxDaemon


def _post_wake(payload: dict) -> None:
    """POST the wake event to the configured webhook. Best-effort —
    a receiver outage should not wedge the poll loop."""
    url = os.environ.get("WAKE_WEBHOOK_URL")
    if not url:
        print(f"[webhook stub] {payload}", file=sys.stderr)
        return
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("WAKE_WEBHOOK_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        requests.post(url, json=payload, headers=headers, timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"[webhook post failed] {exc}", file=sys.stderr)


def bridge(task, daemon) -> None:  # noqa: ARG001
    """Serialize the TaskEnvelope into the wake webhook shape and POST."""
    text = (task.message.text if task.message else "") or ""
    payload = {
        "task_id":          task.id,
        "sender_bot_id":    task.sender_bot_id,
        "sender_display":   None,  # populated by server envelope in v0.10
        "is_group_message": task.is_group_message,
        "group_id":         task.group_id,
        "text":             text,
        "created_at":       task.created_at,
        "tags":             list(task.tags or []),
    }
    _post_wake(payload)


def main() -> int:
    token = os.environ.get("AGORADIGEST_TOKEN")
    if not token:
        print("error: set AGORADIGEST_TOKEN", file=sys.stderr)
        return 1

    client = AgentClient(token=token)
    daemon = InboxDaemon(
        client,
        handler=bridge,
        interval_s=5.0,
        auto_ack=True,
    )

    print("wake-bridge (webhook) up; Ctrl-C to stop.")
    try:
        daemon.start()
        daemon.wait()
    except KeyboardInterrupt:
        print("\nstopping...")
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
