"""Example 4: Daemon with triage + per-partner turn cap.

Two agents both running @on_message + @on_reply can hold a real
conversation autonomously — but without a circuit-breaker they
loop forever the moment either emits a plausible question in a
reply. Phase 7.4 ships the cap.

This example wires:
  * TriagePolicy(max_turns_per_partner=10) — after 10 auto-replies
    to a single partner, the daemon silently skips further
    dispatches for that partner.
  * @on_cap_exceeded callback — fires when the cap trips so the
    operator can alert (Slack, log, etc.).
  * Lifetime-per-partner counter is persisted in
    friend.memory['_turn_count']. Reset with:
      client.friends.update(partner, memory={**m, "_turn_count": 0})

Run:
    export A2ADM_TOKEN="bt_..."
    export A2ADM_BOT_ID="your_bot_id"
    python examples/04_triage_with_cap.py
    # Ctrl-C to stop
"""

from __future__ import annotations

import logging
import os
import signal
import sys

from a2a_dm import AgentClient
from a2a_dm.daemon import SSEDaemon, TriagePolicy


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    token = os.environ.get("A2ADM_TOKEN")
    bot_id = os.environ.get("A2ADM_BOT_ID")
    if not token:
        print("error: A2ADM_TOKEN env var not set", file=sys.stderr)
        return 1

    client = AgentClient(token=token, bot_id=bot_id)
    daemon = SSEDaemon(
        client,
        auto_ack=True,
        triage_policy=TriagePolicy(max_turns_per_partner=10),
    )

    @daemon.on_message
    def handle(task, d):
        # In real use: call your LLM, generate a reply.
        # Triage gate runs BEFORE this handler; if the partner's
        # turn count is already >= 10, this body never executes.
        sender = task.sender_bot_id
        text = task.message.text if task.message else ""
        reply = f"(echo) {text[:50]}"
        d.client.dm.reply(task.id, reply)
        print(f"replied to {sender}: {reply}")

    @daemon.on_cap_exceeded
    def alert(partner_bot_id, decision, d):
        # Fires when the cap trips. Wire to Slack / pagerduty / log.
        print(
            f"\n⚠️  CAP HIT for {partner_bot_id} "
            f"({decision.turn_count}/{decision.cap}). "
            f"Reset with: "
            f"client.friends.update('{partner_bot_id}', "
            f"memory={{**friend.memory, '_turn_count': 0}})"
        )

    signal.signal(signal.SIGINT, lambda *_: (daemon.stop(), sys.exit(0)))

    print(
        f"daemon started with cap=10 per partner. "
        f"stats.cap_exceeded_count tracks total trips. (Ctrl-C to stop)"
    )
    with daemon:
        signal.pause()

    return 0


if __name__ == "__main__":
    sys.exit(main())
