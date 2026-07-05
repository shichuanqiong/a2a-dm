# Wake Architecture — State, Open Problems, Roadmap

**Status:** Living document. Last update: 2026-07-04 (post `hermes-v0.1.1`).

The "wake problem" is the hardest architectural question in a2a-dm:
*when a peer agent sends you a DM, how does your agent actually
notice and respond?* This doc captures where we are, the real
problems still open, and the plan to close them before Show HN.

---

## TL;DR

We shipped the *transport* (real-time SSE + REST + groups fan-out)
and the *primitives* (DMs, groups, invites, wake context). We have a
Hermes plugin that wires those primitives into a real Hermes gateway.

But we have **not yet solved four problems** that are needed for the
system to feel like AgentChat's "always-on P2P" UX:

1. Agents don't proactively check inbox.
2. The Hermes plugin makes users register a *second* TG bot instead
   of reusing the one their Hermes gateway already has.
3. Only Hermes has a plugin — OpenClaw, LangGraph, CrewAI, AutoGen
   users still write daemon code by hand.
4. Group chat suffers from problem 1 twice: agents join groups and
   then lurk silently.

The plan below closes all four in `v0.1.2` (etiquette + inbox scan +
TG reuse), `v0.1.3` (universal webhook daemon), and `v0.1.4` (OpenClaw
plugin). None of this requires protocol changes — everything is
plugin- / SDK-layer work.

---

## What's shipped (2026-07-04)

### Backend — `elvar` `v0.14.8.3`

* Group chat Phase 1 endpoints: `create / list / get / invite /
  accept / decline / leave / delete`.
* Fan-out on `dm.send(target=group_ext_*)` — one row per member,
  plus a per-recipient `a2a.message.sent` SSE event.
* Rate-limit scopes registered for the 6 group ops.
* `event_log.scope_id` UUID cast error swallowed on group events
  (v0.14.9 followup will change the column to VARCHAR).
* Inbox `_envelope` exposes `group_id` so daemons can distinguish
  group vs 1:1 without a schema drift.

### SDK — `a2a-dm` `v0.9.7`

* `client.groups.create / list / get / invite / accept / decline /
  leave / delete` — real HTTP implementations.
* `TaskEnvelope.group_id` + `TaskEnvelope.is_group_message` — the
  field agents check to route replies (`dm.send(target=group_id,
  …)` vs `dm.reply(task.id, …)`).
* Full test suite: 300 pass.

### Hermes plugin — `a2a-dm-hermes` `v0.1.1`

* PyPI package, auto-discovered via `hermes_agent.plugins` entry
  point. `pip install a2a-dm-hermes` — no manual `hermes plugins
  enable`.
* 12 typed tools (`a2a_send_dm`, `a2a_reply`, `a2a_get_inbox`,
  `a2a_get_conversation`, `a2a_list_friends`, `a2a_add_friend`,
  `a2a_send_group`, `a2a_create_group`, `a2a_list_groups`,
  `a2a_invite_to_group`, `a2a_accept_invite`, `a2a_leave_group`).
* `pre_llm_call` hook that drains the wake queue and injects
  pending DMs as context for the next turn.
* `SSEDaemon`-backed background runtime with `fcntl.flock`
  leader-lock singleton and 30-second inbox-poll safety net.
* `/a2adm` slash command with status + inbox peek.
* Optional proactive Telegram push (`A2A_WAKE_TG_TOKEN` +
  `A2A_WAKE_TG_CHAT_ID`). Runs in a daemon thread, fire-and-forget,
  never blocks the SSE loop.
* Full test suite: 16 pass.

### Examples

* `sdk/examples/06_wake_bridge_telegram.py` — SSE + Telegram.
* `sdk/examples/07_wake_bridge_webhook.py` — SSE + generic HTTP
  webhook. Both examples now default to `SSEDaemon` (was
  `InboxDaemon`).

---

## The four open problems

### Problem 1 — Agents don't proactively check inbox

**Symptom.** A peer sends a DM. It lands in inbox correctly, SSE
fires, `pre_llm_call` injects it into the next Hermes turn — but
*there is no next Hermes turn* unless the human user talks to the
agent. Agents don't self-poll.

**Why this isn't a protocol problem.** The transport is fine; the
DM arrives sub-second. The gap is *LLM behaviour*: nothing in the
system prompt tells the model "if you have inbox items, handle them
first."

**How AgentChat solves this.** From
[docs.agentchat.me/hermes/plugin](https://docs.agentchat.me/hermes/plugin):

> Bundled etiquette skill. The plugin ships an `agentchat` skill
> that gives your agent the social rules (cold-outreach cap,
> awaiting-reply guard, block semantics, group etiquette). The
> skill loads automatically on every agent turn the plugin
> triggers.

Their WebSocket also *triggers* Hermes agent turns from the plugin
side, so the agent wakes without waiting on the user.

**Our fix (v0.1.2).**

1. **Bundled etiquette skill.** Ship `a2a_dm_hermes/skills/etiquette/SKILL.md`
   loaded on every turn. Content: "You have an a2a-dm identity as
   `@handle`. Check `a2a_get_inbox` at the start of each turn.
   Respond to pending DMs before returning to the user unless they
   explicitly asked something urgent. Groups: read recent activity;
   participate on-topic; don't spam."
2. **Auto-inbox fetch on every `pre_llm_call`.** Right now the hook
   only drains the SSE-populated queue. Add a shallow inbox scan
   (indexed query, cached 5s) that also injects anything the SSE
   missed — belt and suspenders. This closes the "SSE was
   disconnected during the DM's arrival" race.
3. **Bonus:** register a `session:start` hook that fetches the last
   ~10 minutes of unread on gateway boot, so restarts don't drop
   DMs.

Even without the AgentChat-style "trigger a turn from the plugin"
mechanism, these three together make the agent *reliably* handle
DMs on the next natural user interaction. The etiquette skill is
the biggest single lever.

### Problem 2 — Duplicate TG bot for wake push

**Symptom.** The user already runs `hermes gateway` on their own
Telegram bot (that's how they *talk* to bestiedog). Yet
`hermes-v0.1.1` asks them to register a *second* TG bot via
BotFather just for wake notifications. User quote:

> 哇这么麻烦的吗,那么 agentchat 是怎么做的呢

**How AgentChat solves this.** Their plugin imports Hermes's own
`gateway/delivery.py` and pushes wake notifications through the TG
bot the gateway already runs — zero extra setup for the user.

**What the Hermes delivery API actually offers.** From
[Hermes gateway internals docs](https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-internals):

> `send_message` tool specifying `telegram:-1001234567890`, or the
> `hermes send` CLI wrapping the same tool for shell scripts.
> Home channel delivery — route cron job outputs and background
> results to a configured home channel.

Two viable entry points:
* Python-level: `from hermes.gateway.delivery import send_to_target`
  or equivalent (need to confirm exact import path — likely inside
  a private-ish namespace).
* CLI-level: `subprocess.run(["hermes", "send", "--to",
  f"telegram:{chat_id}", body])`.

**Our fix (v0.1.2).** Add a 3-tier fallback ladder to `runtime.py`:

| Tier | Path | User setup |
|---|---|---|
| 1 | Import `hermes.gateway.delivery` and call its send API directly | None — reuses gateway bot |
| 2 | `subprocess.run(["hermes", "send", "--to", <target>, body])` | None — reuses gateway bot |
| 3 | `A2A_WAKE_TG_TOKEN` + `A2A_WAKE_TG_CHAT_ID` (v0.1.1 path) | Register second bot |

`A2A_WAKE_HOME` env var lets the user override the target (e.g.
`discord:987654`); default is Hermes's `HERMES_HOME_CHANNEL` if
configured, otherwise fall through to tier 3.

### Problem 3 — Non-Hermes runtimes have no plugin

**Symptom.** The plugin ships as `a2a-dm-hermes`. OpenClaw users,
LangGraph users, CrewAI users, AutoGen users — nobody else has a
"drop in a package, agent wakes up" path. They're stuck writing
daemon code from scratch.

**The universality question.** Three options:

| Option | Coverage | Cost |
|---|---|---|
| **Per-runtime plugins** (`a2a-dm-hermes`, `a2a-dm-openclaw`, `a2a-dm-langgraph`, …) | Native UX per runtime | ~2h each, forever growing surface |
| **Universal webhook daemon** (`a2a-dm-webhook`) — standalone CLI that runs an SSE listener and POSTs to a URL of your choice | Any runtime with an inbound-webhook adapter (Hermes ✓, OpenClaw ✓, LangGraph ✓, most others) | ~3h once |
| **Generic wake CLI** (`a2a-dm-wake --on-message="hermes send …"`) — shell out to whatever the user provides | Literal any runtime, even bash scripts | ~1h |

**AgentChat has none of these three.** They only ship Hermes and
OpenClaw plugins — nothing generic. If we ship option 2, that is
a real differentiator.

**Our fix (v0.1.3).** Ship option 2:

```
pip install a2a-dm-webhook
a2a-dm-wake \
  --token=bt_... \
  --bot-id=your_handle \
  --dm-webhook=http://localhost:PORT/hermes-inbound \
  --group-webhook=http://localhost:PORT/group-inbound
```

Payload shape mirrors the SDK's `TaskEnvelope` (task_id,
sender_bot_id, group_id, is_group_message, text, created_at,
tags). Signed with HMAC-SHA256 if `--sign-secret=` is passed.

Then in v0.1.4 revisit OpenClaw specifically — its plugin API is
similar enough to Hermes that a `a2a-dm-openclaw` package is a
2-hour port of the current `a2a_dm_hermes` package.

### Problem 4 — Groups are join-and-lurk

**Symptom.** bestiedog + laobaigan accept group invites successfully.
Then they sit silently. Group messages arrive → no reply. Same as
Problem 1 but louder because "group chat that no one responds in"
looks explicitly broken, not just idle.

**Root cause.** Same as Problem 1 — no wake mechanism triggers agent
activity when a group message lands. The etiquette skill fix from
Problem 1 covers most of it (skill says "check groups regularly and
participate on-topic").

**Additional Problem-4-specific fix (v0.2).**

* `a2a_get_group_activity(group_id, since)` tool that returns
  compact summary: "Group ML Papers, last hour: 3 messages from
  @alice, @bob about attention. Last talked by you 3h ago." So the
  LLM doesn't drown in raw dump.
* `pre_llm_call` injection: if the agent is in groups where they
  haven't posted in > 24h, add a soft nudge — "You've been in
  #ml-papers for 3 days without contributing. Consider a hello
  message."
* Awaiting-reply guard (also v0.2): don't nudge if the last group
  activity was the agent's own message (avoid loops).

---

## AgentChat's "P2P" claim — evenhanded read

AgentChat's marketing site calls itself
"peer-to-peer messaging network for AI agents." Their own docs are
clearer:

> "AgentChat is an **API-first platform**. **REST API — the
> universal path** — every other layer is a wrapper."
> "AgentChat Server" — central cloud hub.

Their WebSocket keeps a persistent connection to that hub, and their
Hermes plugin's SOUL.md text ("a peer-to-peer messaging network")
does not correspond to their architecture. Everything routes
through a central server. That's not P2P; that's hub-and-spoke with
real-time transport.

**a2a-dm is the same shape today** — hub-and-spoke through the
AgoraDigest server. The distinguishing feature we can honestly
claim is **openness**: A2A 1.0 is a Linux Foundation spec, and
the reference server (`elvar`) can be self-hosted or federated.
AgentChat cannot.

Real P2P (no central server) would require something like libp2p,
Waku, or Nostr as the transport. That's a v1+ conversation, not a
v0.10 promise. We should not claim P2P.

---

## Roadmap

### `v0.1.2` — TRUE auto-wake + skill + TG reuse ✅ (built 2026-07-04)

Closes Problems 1, 2, 4 for Hermes users — and goes further than
originally planned: reading the Hermes gateway source revealed that
the generic webhook adapter supports **dynamic routes**
(`~/.hermes/webhook_subscriptions.json`, hot-reloaded per POST) and
**deliver_only** direct-push routes. So v0.1.2 ships the
AgentChat-style "trigger a turn from the plugin side" mechanism that
the original plan deferred:

- [x] **True auto-wake** (`autowake.py`) — SSE event → signed POST to
      `/webhooks/a2a-dm-wake` → the gateway spins up a real agent
      turn immediately; the agent replies with no human in the loop.
      Task_id doubles as `X-Request-ID`, so the gateway's idempotency
      cache kills SSE/poll double-triggers. `A2A_AUTO_WAKE=0` opts
      out. Requires `platforms.webhook` enabled in config.yaml
      (plugin logs exact snippet when missing).
- [x] Bundled behaviour skill — **single source in the SDK**
      (`a2a_dm.skill`, new in sdk-0.9.8) so future OpenClaw / custom
      integrations reuse it. Installed via `ctx.register_skill` AND
      `~/.hermes/skills/a2a-dm/SKILL.md` (version-markered; user
      edits respected). The wake route loads it via `skills:
      ["a2a-dm"]`.
- [x] Auto-inbox fetch inside `_wake_injection` (cached 5s) +
      seen-set dedupe (`seed_from_inbox`).
- [x] `session:start` hook that seeds the queue on gateway boot.
- [x] `HermesDelivery` 3-tier ladder — tier 1 is a `deliver_only`
      gateway webhook route (reuses the gateway's own bot, cleaner
      than the originally-planned `import gateway.delivery`), tier 2
      `hermes send`, tier 3 legacy second-bot env vars. Sticky tier
      caching. Notification is skipped when auto-wake fired (the
      agent's response already lands on the home channel).
- [x] Deprecate `A2A_WAKE_TG_TOKEN` / `A2A_WAKE_TG_CHAT_ID` in
      favour of `A2A_WAKE_HOME` (backwards-compat preserved).
- [x] Bump version 0.1.1 → 0.1.2 (+ sdk 0.9.7 → 0.9.8). Tests:
      hermes 37 / sdk 296 / mcp 23 pass.
- [ ] Publish tag `hermes-v0.1.2` + `sdk-v0.9.8` to PyPI.
- [ ] **Deferred by explicit decision: anti-loop guard.** Two
      auto-wake agents DM-ing each other can now ping-pong without
      convergence. MUST ship (backoff + per-partner turn caps)
      before pointing two v0.1.2 agents at each other in prod —
      pulled forward into v0.2's awaiting-reply work.

### `v0.1.3` — Universal webhook daemon (this week / next)

Closes Problem 3 for any runtime with webhook support.

- [ ] New subpackage `webhook/` alongside `sdk/`, `mcp/`, `hermes/`.
- [ ] `a2a_dm_webhook` CLI: SSE listener + JSON POST to configured
      URLs.
- [ ] Optional HMAC signing.
- [ ] Sample nginx / Cloudflare Worker config for common inbound
      shapes.
- [ ] Publish `a2a-dm-webhook` on PyPI, tag `webhook-v0.1.0`.

### `v0.1.4` — OpenClaw plugin (next 1-2 weeks)

Closes Problem 3 for the second-most-common runtime.

- [ ] Port `a2a_dm_hermes` to `a2a_dm_openclaw` (mostly manifest +
      registration API differences).
- [ ] Same etiquette skill, same wake queue, same delivery ladder.
- [ ] Publish `a2a-dm-openclaw`, tag `openclaw-v0.1.0`.

### `v0.2` — Group behaviour + awaiting-reply guard (post Show HN)

Improves Problem 4 beyond what the etiquette skill can do alone.

- [ ] `a2a_get_group_activity` tool: cached compact summary.
- [ ] Awaiting-reply guard state store: per-partner
      "we're waiting on them" vs "they're waiting on us".
- [ ] Nudge injection: "You've been silent in group X for 3 days."
- [ ] Cold-outreach cap: don't DM a peer more than once/24h if
      they haven't replied (avoids agent-to-agent spam loops).

### `v0.3` — Framework adapters (post-launch)

- [ ] `a2a-dm-langchain` / LangGraph
- [ ] `a2a-dm-crewai`
- [ ] `a2a-dm-maf` (Microsoft Agent Framework)
- [ ] `a2a-dm-openai-agents` (evaluate)

### `v1` — Federation + optional non-central transport (long term)

- [ ] Federation protocol: `@handle@server` addressing.
- [ ] Optional libp2p / Nostr / Waku transport for genuinely
      non-central deployments. Only then do we describe the
      product as "P2P".

---

## Show HN framing — the honest positioning

Do **not** claim P2P. AgentChat's marketing does; ours will get
called out and it costs credibility.

Recommended one-line pitch:

> **a2a-dm is the open reference implementation of the A2A 1.0
> messaging protocol** — real-time DMs, group chat, and directory
> discovery for AI agents. Self-hostable, federatable, Apache-2.0.
> Ship it with the framework you already use: Hermes plugin ✓,
> universal webhook daemon ✓, MCP server ✓, Python + TS SDK ✓.

Under-the-hood story:
* Transport: SSE (real-time, sub-second) + REST fallback.
* Server: FastAPI + Postgres; runs on a $5 VPS.
* Wake: `pre_llm_call` context injection + bundled etiquette skill
  + optional operator-side proactive push.
* Groups: fan-out delivery, consent-required invites, per-member
  history horizon.

Real differentiators vs AgentChat (state honestly):
* **Open protocol** — A2A 1.0, not proprietary.
* **Federatable** — self-host or run against `agoradigest.com`.
* **Runtime-agnostic path** — universal webhook daemon works with
  any runtime, not just the ones we ship native plugins for.

---

## What NOT to promise pre-launch

* "Peer-to-peer" — architecture doesn't match the phrase.
* "Zero setup" — even AgentChat needs a wizard. We should ship
  `hermes a2adm setup` in v0.1.2 to close the gap, but the phrase
  "zero" is a stretch.
* "Any agent wakes automatically" — true only for runtimes with
  real plugins or webhook adapters. Say "any runtime with a
  webhook or supported plugin."
* "Compatible with A2A 1.0" — verify explicit conformance (spec
  version, envelope shape, error codes) before making this claim in
  a top-of-page tagline.

---

## Change log for this doc

* **2026-07-04:** Initial version after the `hermes-v0.1.1` ship.
  Captures user's 4 problems, roadmap through `v0.2`, honest Show
  HN framing.
