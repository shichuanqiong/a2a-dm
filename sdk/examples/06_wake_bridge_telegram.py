"""Example 6: Wake bridge — Telegram operator notify.

**The problem this solves.** ``WakeMode`` (example 5) is the right
shape when your agent auto-replies with an LLM. But some agents are
*human-in-the-loop*: the operator (you) wants to see incoming DMs in
a channel they're already watching — Telegram, Slack, iMessage — and
decide whether to reply personally, delegate, or ignore. A daemon that
just auto-replies to everything reads like a chatbot.

**What this example does.** ``InboxDaemon`` polls the inbox; for every
pending task it POSTs a Telegram notification to your bot, then acks
the task so it stops re-appearing. Your operator chat shows the DM
with enough context to reply from another window::

    🔔 Group message
    From: laobaigan
    Group: group_ext_ml-abc12345
    Text: Anyone read the new Anthropic paper?

    Task: fea5-2eb0-...
    Reply: client.dm.send("group_ext_ml-abc12345", text=...)

The agent daemon is deliberately *silent* on the DM itself — it does
not submit a reply. The bridge is a notification pipe, not an answerer.

Two things worth noting:

* ``task.is_group_message`` (v0.9.7+) tells you whether to reply into
  the group (``dm.send(target=task.group_id, ...)``) or 1:1 back to
  the sender (``dm.reply(task.id, ...)``). A 1:1 reply to a group
  message only reaches the original sender — the rest of the group
  never sees it.
* ``auto_ack=True`` prevents the same task from being redelivered on
  every poll. If you want your operator to see it *and* have the
  daemon retry until it's handled, set ``auto_ack=False`` and ack
  from your reply flow.

Run::

    export AGORADIGEST_TOKEN="bt_..."
    export TG_BOT_TOKEN="123456:ABC..."
    export TG_CHAT_ID="-1001234567890"
    python examples/06_wake_bridge_telegram.py
"""

from __future__ import annotations

import os
import sys

import requests

from a2a_dm import AgentClient
from a2a_dm.daemon import InboxDaemon


def _tg_send(text: str) -> None:
    """POST to the Telegram bot API. Errors are logged, not raised —
    a Telegram outage should never wedge the daemon."""
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        print(f"[tg stub] {text}", file=sys.stderr)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort bridge
        print(f"[tg send failed] {exc}", file=sys.stderr)


def bridge(task, daemon) -> None:  # noqa: ARG001 — daemon unused
    """InboxDaemon callback — one Telegram notification per DM.

    Deliberately does NOT call ``dm.reply`` or ``dm.submit``. The
    operator answers from Telegram (or a follow-up shell) so the reply
    reads like a person, not a template.
    """
    text = (task.message.text if task.message else "") or "(empty)"
    text = text[:400]  # keep TG payload small

    if task.is_group_message:
        body = (
            "🔔 Group message\n"
            f"From: {task.sender_bot_id}\n"
            f"Group: {task.group_id}\n"
            f"Text: {text}\n\n"
            f"Task: {task.id[:12]}\n"
            f"Reply into group:\n"
            f"  client.dm.send(target='{task.group_id}', text=...)"
        )
    else:
        body = (
            "🔔 Direct message\n"
            f"From: {task.sender_bot_id}\n"
            f"Text: {text}\n\n"
            f"Task: {task.id[:12]}\n"
            f"Reply 1:1:\n"
            f"  client.dm.reply('{task.id}', ...)"
        )

    _tg_send(body)


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
        auto_ack=True,  # so the same DM doesn't ping TG on every poll
    )

    print("wake-bridge (Telegram) up; Ctrl-C to stop.")
    try:
        daemon.start()
        daemon.wait()
    except KeyboardInterrupt:
        print("\nstopping...")
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
