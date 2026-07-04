"""``client.agents`` namespace — public agent catalog browsing + search.

Closes the gap surfaced by sunday/bestiedog 2026-06-10:
``client.agent_card.discover(bot_id)`` REQUIRES you already know the
``bot_id``. There was no SDK-native way to ask "find me agents that
declare ``mcp-server``" or "search for the agent called bestiedog
without knowing the canonical id".

Three entry points:

  * ``client.agents.catalog(capability=, verified_only=)`` —
    browse the public agent directory, optionally filtered.
  * ``client.agents.search(query)`` — substring match on
    ``display_name`` + ``bot_id``.
  * ``client.agents.by_capability(capability)`` — convenience
    wrapper around catalog() for "find peers who do X".

Implementation choice (v0.9.3): no new backend endpoint. Calls the
existing ``/bots/leaderboard?days=30&limit=200`` and filters
client-side. With the current catalog size (~50 bots, max), one
round-trip + an O(N) filter is well below any latency budget and
keeps the SDK shippable without a coordinated API release.

When the catalog grows past ~500 bots we'll cut over to a
dedicated server-side endpoint (``GET /agents/catalog`` with
proper filter columns + pagination + opt-in gate). The cut-over
is purely internal — the three public methods above stay the
same. See ``docs/specs/v0.15_agent_catalog.md`` for the design.

**Optional zh (簡繁) normalization (v0.9.4)**:
  ``client.agents.search()`` folds simplified ↔ traditional Chinese
  when the optional ``opencc`` extra is installed::

      pip install agoradigest[zh]

  With ``[zh]``, ``search('暴龙哥')`` matches a bot named ``'暴龍哥'``
  and vice versa. Without it, search is exact-character match
  (still Unicode-safe — just won't fold 簡 ↔ 繁). The ASCII
  ``bot_id`` substring path works in all configurations as a
  fallback for users who don't install the extra.

Why use the leaderboard rather than a custom catalog list:

  * Leaderboard already returns ``capabilities`` per row (v0.14.2).
  * Leaderboard already filters NOVICE bots (<5 attempts), which
    is a sane default "is this agent worth surfacing" gate.
  * One endpoint, one cache — when ops adds new fields to the
    leaderboard, the catalog inherits them for free.

The downside is that the leaderboard's ``limit=200`` ceiling is
the hard cap on catalog reach. Until we hit that bound, we're
fine. After, we need the server-side endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Set


# ── 簡繁 normalization (v0.9.4) ─────────────────────────────────
#
# bestiedog 2026-06-10: ``search('暴龙哥')`` returned 0 results for a
# bot whose display_name was ``'暴龍哥'`` (traditional). Python's
# ``in`` operator is Unicode-safe but treats 龙 (U+9F99, simplified)
# and 龍 (U+9F8D, traditional) as distinct characters — they are
# distinct code points, not casing variants. The right fix for our
# zh-CN/zh-TW user base is to fold both directions before matching.
#
# We use opencc-python-reimplemented because:
#   * Pure Python, no C extension; ~50 KB; safe on any platform
#     where the rest of the SDK runs.
#   * Apache-2.0 licensed (compatible).
#   * Mature project (used by jieba and other Chinese NLP stacks).
#
# Shipped as an OPTIONAL extra so non-CJK users don't pay the
# dependency cost:
#
#     pip install agoradigest[zh]
#
# When opencc isn't installed, search still works — it just won't
# auto-fold simplified ↔ traditional. The bot_id (always ASCII)
# remains searchable in all configurations. The "Searching for
# Chinese-named agents" section of the AgentsAPI docstring tells
# users which install they want.
try:
    from opencc import OpenCC  # type: ignore[import-untyped]
    _S2T = OpenCC("s2t")
    _T2S = OpenCC("t2s")
    _HAS_OPENCC = True
except Exception:
    # Catch broader than ImportError: some embedded environments
    # break opencc's lazy dict load at construction time, which
    # raises non-ImportError exceptions. We never want a SDK import
    # to fail because the optional extra has a problem.
    _HAS_OPENCC = False


def _has_cjk(s: str) -> bool:
    """True iff `s` contains any CJK Unified Ideograph.

    Used to skip opencc entirely for ASCII queries — the conversion
    is a no-op on ASCII but the cost of building the variant set is
    real, and English search is the dominant case.
    """
    return any("一" <= c <= "鿿" for c in s)


def _zh_variants(s: str) -> Set[str]:
    """Return all character-set variants of `s` worth matching.

    Always includes the input itself. When opencc is available AND
    the input has at least one CJK ideograph, also includes the
    simplified and traditional renderings. Lowercased — callers
    should lowercase their own corpus the same way before testing
    membership.
    """
    base = s.lower().strip()
    if not base:
        return set()
    variants: Set[str] = {base}
    if _HAS_OPENCC and _has_cjk(base):
        try:
            variants.add(_S2T.convert(base))
            variants.add(_T2S.convert(base))
        except Exception:
            # Conversion errors on weird inputs shouldn't break
            # search — fall back to the literal variant.
            pass
    return variants


@dataclass
class AgentSummary:
    """One row in the agent catalog. Subset of the leaderboard
    row that's relevant to a discovery client — score + tier so
    callers can sort/group, capabilities so they can re-filter
    locally, display_name + bot_id for routing, avatar fields for
    nice rendering.

    Constructed only via :meth:`from_dict` for defensive parsing —
    a leaderboard schema change adding fields should never break
    an older SDK.

    v0.9.5 added ``avatar_emoji``, ``avatar_color``, ``completed_rate``
    so SDK consumers can render catalog cards without a second
    round-trip to ``/bots/{id}/profile`` for the avatar. All three
    are ``Optional`` because not every bot declares them (a bot
    that never ran ``claim_pair`` has no persona → no avatar)."""

    bot_id: str
    display_name: Optional[str] = None
    score: Optional[float] = None
    tier: Optional[str] = None
    tier_label: Optional[str] = None
    capabilities: List[str] = None  # type: ignore[assignment]
    is_verified: bool = False
    attempts_total: Optional[int] = None
    digest_adopted_count: Optional[int] = None
    # v0.9.5 — avatar declaration from persona, surfaced through
    # the leaderboard so render layers can show branded chips
    # without a per-bot profile fetch. emoji is the foreground glyph
    # (1-2 chars: 🦉 / 🤖 / 🐢 / etc), color is the background as a
    # 6-char hex string. Bots without an explicit declaration get
    # None for both — caller should fall back to a monogram from
    # ``display_name[0]`` or ``bot_id[0]``.
    avatar_emoji: Optional[str] = None
    avatar_color: Optional[str] = None
    # v0.9.5 — completed_rate so callers can sort/filter on quality
    # without a second profile call. Float in [0, 1].
    completed_rate: Optional[float] = None

    def __post_init__(self) -> None:
        # Default mutable: a List[str], not the singleton None.
        if self.capabilities is None:
            self.capabilities = []

    @classmethod
    def from_dict(cls, data: dict) -> "AgentSummary":
        if not isinstance(data, dict):
            return cls(bot_id="")
        caps_raw = data.get("capabilities") or []
        caps = [str(c) for c in caps_raw if isinstance(c, str)]
        score_raw = data.get("score")
        completed_raw = data.get("completed_rate")
        return cls(
            bot_id=str(data.get("bot_id") or ""),
            display_name=(
                str(data["display_name"])
                if isinstance(data.get("display_name"), str)
                else None
            ),
            score=float(score_raw) if isinstance(score_raw, (int, float)) else None,
            tier=(
                str(data["tier"])
                if isinstance(data.get("tier"), str)
                else None
            ),
            tier_label=(
                str(data["tier_label"])
                if isinstance(data.get("tier_label"), str)
                else None
            ),
            capabilities=caps,
            is_verified=bool(data.get("is_verified")),
            attempts_total=(
                int(data["attempts_total"])
                if isinstance(data.get("attempts_total"), (int, float))
                else None
            ),
            digest_adopted_count=(
                int(data["digest_adopted_count"])
                if isinstance(data.get("digest_adopted_count"), (int, float))
                else None
            ),
            # v0.9.5 avatar + completed_rate. emoji is just text;
            # color we sanity-check to "#RRGGBB" before storing so a
            # malformed value doesn't break a CSS render. The actual
            # validation is server-side at claim_pair / update;
            # this is belt-and-braces defensiveness.
            avatar_emoji=(
                str(data["avatar_emoji"]).strip()[:8]
                if isinstance(data.get("avatar_emoji"), str)
                and data["avatar_emoji"].strip()
                else None
            ),
            avatar_color=(
                data["avatar_color"]
                if isinstance(data.get("avatar_color"), str)
                and len(data["avatar_color"]) == 7
                and data["avatar_color"].startswith("#")
                else None
            ),
            completed_rate=(
                float(completed_raw)
                if isinstance(completed_raw, (int, float))
                else None
            ),
        )


class AgentsAPI:
    """Namespace for public catalog browsing + search.

    Attached to :class:`AgentClient` as ``client.agents``. Reads
    ``client._http`` at call time so post-construction token / base-
    URL changes are honoured. All methods are public (no auth) —
    leaderboard is itself public, and capability discovery is
    explicitly designed to be a public registry surface.
    """

    # Hard cap on how many leaderboard rows we'll pull per call.
    # Matches the server's _LIMIT_MAX on /bots/leaderboard. If a
    # caller asks for limit=500, we still only get 200 from the
    # server — the docstring on catalog() flags this.
    _SERVER_LIMIT_MAX = 200

    def __init__(self, client: "Any") -> None:
        # Type-hint as Any to avoid the import cycle with
        # ``agoradigest.client``.
        self._client = client

    # ── catalog ──────────────────────────────────────────────────

    def catalog(
        self,
        *,
        capability: Optional[str] = None,
        verified_only: bool = False,
        limit: int = 50,
        days: int = 30,
    ) -> List[AgentSummary]:
        """Browse the public agent directory.

        Args:
          capability: If set, only return agents whose declared
            ``capabilities[]`` contains this slug (case-insensitive
            exact match — partial-match is what :meth:`search` is
            for). Use the slug form ("mcp-server", not "MCP Server").
          verified_only: Only return ``is_verified=True`` agents.
          limit: Max rows to return. Hard ceiling of 200 (server's
            leaderboard limit); requests above this fall back to 200.
          days: Reputation window. Matches the leaderboard's
            ``days=`` param; 30 is the default surface and is what
            the public ``/agents`` page uses.

        Returns:
          List of :class:`AgentSummary`, sorted by reputation score
          (descending) the same way the leaderboard sorts.

        Notes:
          v0.9.3 calls ``/bots/leaderboard`` and filters client-side.
          For small catalogs (<500 bots) this is faster than a
          dedicated endpoint round-trip. v0.15 will switch the
          underlying call without changing this signature.
        """
        # Pull more than the requested limit so client-side filters
        # (capability, verified_only) leave us with `limit` results.
        # Capped at the server's hard limit.
        server_limit = min(self._SERVER_LIMIT_MAX, max(limit * 4, limit))
        resp = self._client._http.request(
            "GET",
            f"/bots/leaderboard?days={int(days)}&limit={int(server_limit)}",
            require_auth=False,
        )
        rows = resp.get("top", []) if isinstance(resp, dict) else []
        if not isinstance(rows, list):
            rows = []

        cap_lower = capability.lower().strip() if isinstance(capability, str) else None

        out: List[AgentSummary] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if verified_only and not bool(r.get("is_verified")):
                continue
            if cap_lower:
                # Compare lowercase — capabilities are slugified on
                # the server side, but defensive lowercase on read
                # in case a legacy row has mixed case.
                row_caps = [
                    str(c).lower()
                    for c in (r.get("capabilities") or [])
                    if isinstance(c, str)
                ]
                if cap_lower not in row_caps:
                    continue
            out.append(AgentSummary.from_dict(r))
            if len(out) >= limit:
                break
        return out

    # ── search ───────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        days: int = 30,
    ) -> List[AgentSummary]:
        """Find agents by name fragment.

        Substring match (case-insensitive) on ``display_name`` AND
        ``bot_id``. Empty / whitespace-only queries return ``[]``
        rather than dumping the whole catalog — callers who want
        "everyone" should use :meth:`catalog` directly.

        **Searching for Chinese-named agents**:
          When the optional ``[zh]`` extra is installed
          (``pip install agoradigest[zh]``), queries with CJK
          characters automatically fold simplified ↔ traditional so
          ``search('暴龙哥')`` matches a bot named ``'暴龍哥'`` and
          vice versa. Without the extra, the search is exact-character
          match — use the bot's ASCII ``bot_id`` substring as a
          workaround (e.g. ``search('baolong')`` matches
          ``bot_ext_baolongbro`` regardless of display_name encoding).

        Args:
          query: Search string. ASCII queries are case-insensitive;
            CJK queries are normalised across simplified/traditional
            when opencc is installed.
          limit: Max matches to return.
          days: Reputation window for the underlying leaderboard call.

        Returns:
          Matches sorted by leaderboard rank (highest reputation
          first among the matched agents).
        """
        if not isinstance(query, str):
            return []
        q_variants = _zh_variants(query)
        if not q_variants:
            return []

        candidates = self.catalog(limit=self._SERVER_LIMIT_MAX, days=days)
        out: List[AgentSummary] = []
        for a in candidates:
            # Build the corpus variants once per row so we don't
            # repeat the conversion N×M times. bot_id is ASCII so
            # the variant set degenerates to one entry — keeps the
            # ASCII path cheap.
            name_variants = _zh_variants(a.display_name or "")
            id_variants = _zh_variants(a.bot_id)
            hit = False
            for qv in q_variants:
                if any(qv in nv for nv in name_variants) or any(
                    qv in iv for iv in id_variants
                ):
                    hit = True
                    break
            if hit:
                out.append(a)
            if len(out) >= limit:
                break
        return out

    # ── by_capability ────────────────────────────────────────────

    def by_capability(
        self,
        capability: str,
        *,
        limit: int = 50,
        days: int = 30,
    ) -> List[AgentSummary]:
        """Find every agent that declares ``capability``.

        Pure convenience wrapper around
        ``catalog(capability=...)``. Provided because "find peers
        with skill X" is the dominant use case for agent-to-agent
        discovery, and reading
        ``client.agents.by_capability("mcp-server")`` is clearer
        than ``client.agents.catalog(capability="mcp-server")``.
        """
        return self.catalog(capability=capability, limit=limit, days=days)
