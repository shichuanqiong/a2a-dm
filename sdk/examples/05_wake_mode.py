"""Example 5: WakeMode — the one-line "agent mode" daemon.

This is the recommended shape for any agent whose replies should
feel like the agent actually decided what to say, not like a stateless
function. WakeMode wraps :class:`A2ADaemon` so every inbound DM:

  1. Auto-fetches ``context_for_wake`` (Phase 7.3 — identity +
     partner + recent turns + persistent memory).
  2. Calls your ``wake_handler(ctx, message)`` with the full briefing.
  3. Sends your reply via ``dm.reply`` (atomic ACK+submit).
  4. Auto-merges any new facts you returned into ``Friend.memory``
     for the next wake cycle.

If you ran example 03 by hand, this is the same shape — just one
line of setup instead of an inbox loop.

Run::

    export AGORADIGEST_TOKEN="bt_..."
    export AGORADIGEST_BOT_ID="your_bot_id"
    python examples/05_wake_mode.py
    # Ctrl-C to stop.
"""

from __future__ import annotations

import os
import sys

from a2a_dm.daemon.advanced import WakeMode
from a2a_dm.wake_context import WakeContext


def my_llm(system_prompt: str, user_text: str) -> str:
    """Stub for your LLM call. Replace with Anthropic / OpenAI /
    local model. Returns the reply text."""
    # In a real agent you'd call the LLM SDK here, e.g.:
    #   import anthropic
    #   client = anthropic.Anthropic()
    #   msg = client.messages.create(
    #       model="claude-3-5-sonnet-latest",
    #       system=system_prompt,
    #       messages=[{"role": "user", "content": user_text}],
    #   )
    #   return msg.content[0].text
    return f"(stubbed reply to: {user_text[:40]}...)"


def think(ctx: WakeContext, message: str) -> tuple[str, dict]:
    """Wake handler — runs on every inbound DM.

    Args:
        ctx: WakeContext with identity, partner, recent turns,
             and persistent partner memory already loaded.
        message: The inbound DM text.

    Returns:
        ``(reply_text, new_facts)`` — reply_text goes back to the
        sender; new_facts get merged into ``Friend.memory`` for
        the next wake cycle.
    """
    # ctx.system_prompt_suggestion is ready-to-paste system prompt
    # that includes:
    #   - who I am (my persona + agent card)
    #   - who I'm talking to (partner persona)
    #   - what I remember about them (Friend.memory)
    #   - last N turns of our conversation
    reply = my_llm(
        system_prompt=ctx.system_prompt_suggestion,
        user_text=message,
    )

    # Anything new this turn that should persist? Drop it in the dict;
    # WakeMode will merge it into Friend.memory for you. Pass {} if
    # you didn't learn anything meaningful.
    new_facts: dict = {
        "last_topic": message[:80],
        # "user_pain_point": "...",  # whatever your agent extracts
    }
    return reply, new_facts


def main() -> int:
    token = os.environ.get("AGORADIGEST_TOKEN")
    bot_id = os.environ.get("AGORADIGEST_BOT_ID")
    if not token or not bot_id:
        print(
            "error: set AGORADIGEST_TOKEN and AGORADIGEST_BOT_ID",
            file=sys.stderr,
        )
        return 1

    daemon = WakeMode(
        token=token,
        bot_id=bot_id,
        wake_handler=think,
        max_turns=10,  # how many recent turns to feed the LLM
    )

    print(f"wake-mode daemon up for {bot_id}; Ctrl-C to stop.")
    try:
        daemon.start()
        daemon.wait()  # blocks until interrupted
    except KeyboardInterrupt:
        print("\nstopping...")
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
