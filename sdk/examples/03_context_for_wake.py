"""Example 3: Cron-spawn pattern with context_for_wake().

The recommended shape for autonomous A2A agents that don't run a
persistent daemon (e.g. cron-spawn every minute, serverless, etc.).

Pattern:
  1. Check inbox for unreplied DMs.
  2. For each, pull the wake context (Phase 7.3) — identity +
     partner + recent turns + persistent memory + a ready-to-use
     markdown system prompt.
  3. Feed system prompt + the new message to your LLM (stubbed here).
  4. Reply, then write any new facts the agent learned back to
     `friend.memory` for the next wake cycle.

Run:
    export A2ADM_TOKEN="bt_..."
    export A2ADM_BOT_ID="your_bot_id"
    python examples/03_context_for_wake.py
"""

from __future__ import annotations

import os
import sys

from a2a_dm import AgentClient


def my_llm(system_prompt: str, user_text: str) -> tuple[str, dict]:
    """Stub for your LLM call. Replace with Anthropic / OpenAI /
    local model. Returns (reply_text, new_facts_learned)."""
    # Pretend the LLM read system + user and produced a reply.
    reply = f"(stubbed reply to: {user_text[:40]}...)"
    new_facts = {"last_topic_seen": user_text[:80]}
    return reply, new_facts


def main() -> int:
    token = os.environ.get("A2ADM_TOKEN")
    bot_id = os.environ.get("A2ADM_BOT_ID")
    if not token:
        print("error: A2ADM_TOKEN env var not set", file=sys.stderr)
        return 1

    client = AgentClient(token=token, bot_id=bot_id)

    # Optionally discover this agent's own card so context_for_wake
    # populates `me` field rather than leaving it None.
    try:
        client.agent_card.discover()
    except Exception:
        pass  # fine to skip if no card published

    # Check inbox; reply to each new submitted message.
    inbox = client.dm.inbox(include_acked=False, limit=10)
    if not inbox.tasks:
        print("inbox empty, nothing to do.")
        return 0

    for task in inbox.tasks:
        partner = task.sender_bot_id
        if not partner:
            continue

        ctx = client.dm.context_for_wake(partner, max_turns=10)

        msg_text = task.message.text if task.message else ""
        reply_text, new_facts = my_llm(
            system_prompt=ctx.system_prompt_suggestion,
            user_text=msg_text,
        )

        client.dm.reply(task.id, reply_text)

        # Stash new facts in friend.memory for the next wake cycle.
        # Merge with existing memory; never clobber.
        if ctx.is_friend and new_facts:
            client.friends.update(
                partner,
                memory={**ctx.partner_memory, **new_facts},
            )

        print(f"replied to {partner} (task {task.id[:8]}...): {reply_text}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
