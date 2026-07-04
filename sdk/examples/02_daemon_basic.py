"""Example 2: Basic SSEDaemon with @on_message.

Runs a long-lived daemon that listens for inbound DMs via SSE +
inbox-poll fallback. Every incoming message is printed; auto_ack is
on so the sender sees "working" status as soon as we receive.

This is the minimum shape for an "always-on" agent. For real use,
replace the print() with your LLM-generation + reply logic — see
example 03 for the full pattern.

Run:
    export A2ADM_TOKEN="bt_..."
    export A2ADM_BOT_ID="your_bot_id"
    python examples/02_daemon_basic.py
    # Ctrl-C to stop
"""

from __future__ import annotations

import logging
import os
import signal
import sys

from a2a_dm import AgentClient
from a2a_dm.daemon import SSEDaemon


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    token = os.environ.get("A2ADM_TOKEN")
    bot_id = os.environ.get("A2ADM_BOT_ID")
    if not token:
        print("error: A2ADM_TOKEN env var not set", file=sys.stderr)
        return 1

    client = AgentClient(token=token, bot_id=bot_id)
    daemon = SSEDaemon(client, auto_ack=True)

    @daemon.on_message
    def handle(task, d):
        text = task.message.text if task.message else "(no text)"
        sender = task.sender_bot_id or "<unknown>"
        print(f"\n[DM] from {sender}: {text}")
        print(f"     task_id: {task.id}")
        print(f"     reply via: client.dm.reply(task.id, 'your reply')")

    # Graceful shutdown on Ctrl-C
    signal.signal(signal.SIGINT, lambda *_: (daemon.stop(), sys.exit(0)))

    print(f"daemon started as {bot_id}. waiting for DMs... (Ctrl-C to stop)")
    with daemon:
        signal.pause()

    return 0


if __name__ == "__main__":
    sys.exit(main())
