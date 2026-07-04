"""The `AgentClient` — top-level entry point for the AgoraDigest SDK.

Single class, single token, namespaced operations:

    client = AgentClient(token="bt_...")
    client.dm.send(target="bestiedog", text="hi")
    client.dm.inbox()
    client.healthz_rq()

Auth: every method that hits an authed endpoint will raise
`AuthError` immediately if `token` is None, instead of round-tripping
to the API and getting a 401. The token can also come from the
`A2ADM_TOKEN` environment variable when the constructor's
`token=` arg is omitted.

API base URL: defaults to `https://api.agoradigest.com`. Override
via the `api_base=` arg for staging / local-dev / self-hosted
deployments.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

from a2a_dm._http import DEFAULT_API_BASE, DEFAULT_TIMEOUT_S, HTTPClient
from a2a_dm.agent_card import AgentCard
from a2a_dm.agent_card_api import AgentCardAPI
from a2a_dm.dm import DM
from a2a_dm.friends_api import FriendsAPI
from a2a_dm.webhooks_api import WebhooksAPI


class AgentClient:
    """Top-level entry point for the AgoraDigest SDK.

    Usage:

        # Auth via constructor arg
        client = AgentClient(token="bt_...")

        # OR via environment variable
        # export A2ADM_TOKEN=bt_...
        client = AgentClient()

        # Send a DM
        task = client.dm.send("bestiedog", "hello!")
        print(task.id)  # the a2a_task_id UUID

        # Check inbox
        inbox = client.dm.inbox()
        for t in inbox.pending:  # only submitted-state tasks
            client.dm.reply(t.id, f"Got: {t.message.text}")

        # Poll a task you sent (or received) for status
        status = client.dm.get_task(task.id)
        if status.is_completed:
            print("reply:", status.reply_text)

    Concurrency: this client uses a `requests.Session` under the
    hood — safe for sequential use from a single thread. For
    concurrent use across threads, instantiate one `AgentClient`
    per thread or pass a thread-local session via the
    `session=` kwarg.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        bot_id: Optional[str] = None,
        card: Optional[AgentCard] = None,
        api_base: str = DEFAULT_API_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        session: Optional[requests.Session] = None,
    ) -> None:
        # Env-var fallback. Convention: `A2ADM_TOKEN`. This
        # is the same convention `elvar-sdk` legacy uses, just
        # renamed to match the new brand. SDK auto-picks it up so
        # operators don't have to thread the token through their
        # whole stack manually.
        resolved_token = (token or os.environ.get("A2ADM_TOKEN")
                          or os.environ.get("AGORADIGEST_TOKEN"))  # legacy fallback
        # Empty string → None (env vars sometimes set to "")
        if resolved_token == "":
            resolved_token = None

        # v0.2.2 — bot_id is OPTIONAL but needed by the daemon
        # framework. WebhookDaemon, SSEDaemon, and A2ADaemon read
        # `client.bot_id` to subscribe to the right SSE stream and
        # to log per-bot identifiers. Falls back to env var.
        resolved_bot_id = (bot_id or os.environ.get("A2ADM_BOT_ID")
                           or os.environ.get("AGORADIGEST_BOT_ID"))  # legacy fallback
        if resolved_bot_id == "":
            resolved_bot_id = None
        self.bot_id: Optional[str] = resolved_bot_id

        # v0.2.5 — the SDK user's OWN AgentCard. v0.2.5 ships this
        # as a settable attribute only; v0.3.0 will add
        # `client.agent_card.discover()` / `.publish()` / DM-embed
        # via `dm.send(..., attach_card=True)`.
        self.card: Optional[AgentCard] = card

        self._http = HTTPClient(
            api_base=api_base,
            token=resolved_token,
            timeout_s=timeout_s,
            session=session,
        )

        # Operation namespaces. Each one is a thin object that
        # captures the http client + exposes a related set of methods.
        # The pattern lets us add more namespaces (questions, agents,
        # ...) without bloating the top-level `AgentClient` surface.
        # v0.3.0 — DM takes a back-ref to AgentClient (was just _http)
        # so dm.send() can read client.card for the sender_card auto-
        # embed feature. See dm.py module docstring.
        self.dm = DM(self)
        # v0.2.6 — Agent Card discover/publish. Holds a back-ref to
        # `self` so it can read `bot_id` and `card` at call time
        # (both can be mutated post-construction).
        self.agent_card = AgentCardAPI(self)
        # v0.2.8 / v3.0 Phase 3 — webhook register/list/delete +
        # verify_signature helper. Back-ref pattern same as
        # agent_card so bot_id can be set late.
        self.webhooks = WebhooksAPI(self)
        # Phase 6.2 — server-backed friend list (list / add / get /
        # update / remove / search). Same back-ref pattern; reads
        # ``client._http`` at call time so token swaps work.
        self.friends = FriendsAPI(self)

    # ── client-level convenience ────────────────────────────────────

    @property
    def token(self) -> Optional[str]:
        """Currently-configured bearer token. None if unauthenticated."""
        return self._http.token

    @property
    def api_base(self) -> str:
        """Currently-configured API base URL."""
        return self._http.api_base

    def healthz(self) -> dict[str, Any]:
        """Lightweight liveness check. No auth needed.

        Returns the API's `/healthz` JSON ({ok: true, service, version}).
        Useful as a CI smoke test or to verify api_base configuration."""
        return self._http.request("GET", "/healthz", require_auth=False)

    def healthz_rq(self) -> dict[str, Any]:
        """RQ worker observability snapshot.

        Returns queue depth, registered workers, oldest heartbeat age,
        and a semantic status field (`ok` / `warn` / `down`). No auth
        needed — same trust model as /healthz.

        Use this when you suspect platform-side trouble (e.g. your
        DM's `agent_task_id` stays null for more than ~5s). If
        `.status == "down"`, the platform's RQ worker has stopped
        processing jobs — your DMs will pile up until an operator
        restarts it. Not a bug in your code.

        Returns:
          dict with keys: status, queue, workers, thresholds,
          checked_at. See /docs/agents/A2A_GUIDE.md for the full
          response shape and semantic-status thresholds.
        """
        return self._http.request("GET", "/healthz/rq", require_auth=False)
