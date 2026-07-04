"""SDK agents catalog API tests (v0.9.3).

Coverage:
  * AgentSummary.from_dict happy + defensive paths
  * client.agents.catalog() — basic call, capability filter,
    verified_only filter, limit cap
  * client.agents.search() — substring on display_name + bot_id
  * client.agents.by_capability() — convenience wrapper
  * Auth NOT required (catalog is public)
  * Empty / garbage server responses don't crash

These tests also exist to catch the class of bug that bit v0.9.1 →
v0.9.2 (wrong HTTP kwarg name): the `responses` library verifies
the actual HTTP path emitted, so a future signature change in
``HTTPClient.request`` would surface here instead of mid-flight in
production.
"""

from __future__ import annotations

import json

import pytest
import responses

from a2a_dm import AgentClient, AgentsAPI, AgentSummary


# ── AgentSummary dataclass ──────────────────────────────────────


def _row(bot_id: str = "bestiedog", **overrides) -> dict:
    base = {
        "bot_id": bot_id,
        "display_name": "Bestie Dog",
        "score": 87.4,
        "tier": "expert",
        "tier_label": "Expert",
        "capabilities": ["mcp-server", "a2a"],
        "is_verified": True,
        "attempts_total": 142,
        "digest_adopted_count": 23,
        # v0.9.5 — leaderboard surfaces these from persona.
        "avatar_emoji": "🐶",
        "avatar_color": "#0369a1",
        "completed_rate": 0.92,
    }
    base.update(overrides)
    return base


def test_agent_summary_from_dict_happy_path():
    s = AgentSummary.from_dict(_row())
    assert s.bot_id == "bestiedog"
    assert s.display_name == "Bestie Dog"
    assert s.score == 87.4
    assert s.tier == "expert"
    assert s.capabilities == ["mcp-server", "a2a"]
    assert s.is_verified is True
    assert s.attempts_total == 142
    assert s.digest_adopted_count == 23
    # v0.9.5 additions
    assert s.avatar_emoji == "🐶"
    assert s.avatar_color == "#0369a1"
    assert s.completed_rate == 0.92


def test_agent_summary_from_dict_empty_is_safe():
    s = AgentSummary.from_dict({})
    assert s.bot_id == ""
    assert s.display_name is None
    assert s.capabilities == []
    assert s.is_verified is False
    # v0.9.5 — new optionals also default to None.
    assert s.avatar_emoji is None
    assert s.avatar_color is None
    assert s.completed_rate is None


def test_agent_summary_from_dict_non_dict_is_safe():
    s = AgentSummary.from_dict(None)  # type: ignore[arg-type]
    assert s.bot_id == ""
    # Also confirms no AttributeError on .get()
    s2 = AgentSummary.from_dict("not a dict")  # type: ignore[arg-type]
    assert s2.bot_id == ""


def test_agent_summary_drops_non_string_capabilities():
    # The server should never emit these, but be defensive.
    s = AgentSummary.from_dict(
        _row(capabilities=["mcp", 42, None, "a2a"])
    )
    assert s.capabilities == ["mcp", "a2a"]


# ── v0.9.5 avatar defensiveness ─────────────────────────────────


def test_avatar_color_invalid_format_drops_to_none():
    """A malformed avatar_color shouldn't crash render layers —
    we sanity-check #RRGGBB at parse time."""
    for bad in ["rgb(1,2,3)", "purple", "#abc", "#1234567", "12abcd", ""]:
        s = AgentSummary.from_dict(_row(avatar_color=bad))
        assert s.avatar_color is None, f"expected None for {bad!r}"


def test_avatar_color_valid_format_kept():
    for good in ["#0369a1", "#ffffff", "#000000", "#AABBCC"]:
        s = AgentSummary.from_dict(_row(avatar_color=good))
        assert s.avatar_color == good


def test_avatar_emoji_whitespace_stripped():
    s = AgentSummary.from_dict(_row(avatar_emoji="  🐶  "))
    assert s.avatar_emoji == "🐶"


def test_avatar_emoji_empty_string_normalises_to_none():
    s = AgentSummary.from_dict(_row(avatar_emoji=""))
    assert s.avatar_emoji is None
    s2 = AgentSummary.from_dict(_row(avatar_emoji="   "))
    assert s2.avatar_emoji is None


def test_completed_rate_non_numeric_drops_to_none():
    s = AgentSummary.from_dict(_row(completed_rate="not a number"))
    assert s.completed_rate is None
    s2 = AgentSummary.from_dict(_row(completed_rate=None))
    assert s2.completed_rate is None


@responses.activate
def test_catalog_returns_avatar_fields():
    """End-to-end: avatar fields plumb through catalog() so callers
    don't need a second profile fetch."""
    _mock_leaderboard([
        _row("bestiedog", avatar_emoji="🐶", avatar_color="#0369a1"),
        _row("nomansland", avatar_emoji=None, avatar_color=None),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.catalog()
    assert len(result) == 2
    by_id = {r.bot_id: r for r in result}
    assert by_id["bestiedog"].avatar_emoji == "🐶"
    assert by_id["bestiedog"].avatar_color == "#0369a1"
    # Bots without persona-declared avatars → None (caller falls
    # back to a monogram).
    assert by_id["nomansland"].avatar_emoji is None
    assert by_id["nomansland"].avatar_color is None


# ── catalog() — happy path + filters ────────────────────────────


def _mock_leaderboard(rows: list[dict]) -> None:
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/bots/leaderboard",
        json={"top": rows, "count": len(rows), "days": 30, "limit": 200},
        status=200,
    )


@responses.activate
def test_catalog_returns_summaries_no_filter():
    _mock_leaderboard([_row("bestiedog"), _row("nomansland", capabilities=[])])
    client = AgentClient(token="bt_test")
    result = client.agents.catalog()
    assert len(result) == 2
    assert all(isinstance(r, AgentSummary) for r in result)
    assert {r.bot_id for r in result} == {"bestiedog", "nomansland"}


@responses.activate
def test_catalog_filters_by_capability():
    _mock_leaderboard([
        _row("bestiedog", capabilities=["mcp-server", "a2a"]),
        _row("nomansland", capabilities=["wake-mode"]),
        _row("laobaigan", capabilities=["mcp-server"]),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.catalog(capability="mcp-server")
    assert {r.bot_id for r in result} == {"bestiedog", "laobaigan"}


@responses.activate
def test_catalog_capability_filter_is_case_insensitive():
    _mock_leaderboard([
        _row("bestiedog", capabilities=["mcp-server"]),
    ])
    client = AgentClient(token="bt_test")
    # Caller uses uppercase; server-side data is lowercase.
    result = client.agents.catalog(capability="MCP-SERVER")
    assert len(result) == 1
    assert result[0].bot_id == "bestiedog"


@responses.activate
def test_catalog_verified_only_filter():
    _mock_leaderboard([
        _row("bestiedog", is_verified=True),
        _row("nomansland", is_verified=False),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.catalog(verified_only=True)
    assert len(result) == 1
    assert result[0].bot_id == "bestiedog"


@responses.activate
def test_catalog_respects_limit():
    rows = [_row(f"bot_{i}") for i in range(20)]
    _mock_leaderboard(rows)
    client = AgentClient(token="bt_test")
    result = client.agents.catalog(limit=5)
    assert len(result) == 5


@responses.activate
def test_catalog_uses_public_endpoint_no_auth_required():
    # The endpoint is public; we should be able to call it without
    # a token (regression — would catch a future require_auth slip).
    _mock_leaderboard([_row("bestiedog")])
    client = AgentClient()  # no token, no env var
    result = client.agents.catalog()
    assert len(result) == 1


@responses.activate
def test_catalog_handles_garbage_response_shape():
    # Server returns something not matching the contract — we
    # should not crash, just return [].
    responses.add(
        responses.GET,
        "https://api.agoradigest.com/bots/leaderboard",
        json={"unexpected": "shape"},
        status=200,
    )
    client = AgentClient(token="bt_test")
    result = client.agents.catalog()
    assert result == []


# ── search() ────────────────────────────────────────────────────


@responses.activate
def test_search_matches_display_name_substring():
    _mock_leaderboard([
        _row("bot_a", display_name="Bestie Dog"),
        _row("bot_b", display_name="Wake Server"),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.search("bestie")
    assert len(result) == 1
    assert result[0].bot_id == "bot_a"


@responses.activate
def test_search_matches_bot_id_substring():
    _mock_leaderboard([
        _row("bestiedog", display_name="Bestie Dog"),
        _row("nomansland", display_name="No Man's Land"),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.search("man")
    assert len(result) == 1
    assert result[0].bot_id == "nomansland"


@responses.activate
def test_search_is_case_insensitive():
    _mock_leaderboard([_row("bestiedog", display_name="Bestie")])
    client = AgentClient(token="bt_test")
    result = client.agents.search("BESTIE")
    assert len(result) == 1


def test_search_empty_query_returns_empty_without_calling_api():
    # No @responses.activate — if it tried to hit the network,
    # the request would fail in the absence of registered mocks.
    client = AgentClient(token="bt_test")
    assert client.agents.search("") == []
    assert client.agents.search("   ") == []


# ── by_capability() ─────────────────────────────────────────────


@responses.activate
def test_by_capability_is_convenience_wrapper():
    _mock_leaderboard([
        _row("bestiedog", capabilities=["mcp-server"]),
        _row("nomansland", capabilities=["wake-mode"]),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.by_capability("mcp-server")
    assert len(result) == 1
    assert result[0].bot_id == "bestiedog"


# ── client wiring sanity ────────────────────────────────────────


def test_client_exposes_agents_namespace():
    client = AgentClient(token="bt_test")
    assert isinstance(client.agents, AgentsAPI)


# ── v0.9.4 簡繁 normalization ───────────────────────────────────
#
# bestiedog 2026-06-10 reported: ``search('暴龙哥')`` returned 0
# results for a bot named ``'暴龍哥'``. Root cause was that 龙 and
# 龍 are distinct Unicode code points — not a casing variant. v0.9.4
# adds opencc-based simplified ↔ traditional folding behind the
# optional ``[zh]`` install. These tests pin both the "with opencc"
# behavior and the "without opencc" graceful degradation.

from a2a_dm import agents_api as _agents_api_module


@responses.activate
def test_search_simplified_query_matches_traditional_name():
    """Simplified query (暴龙哥) should match a bot with a
    traditional display_name (暴龍哥) — requires opencc installed
    in the test environment."""
    if not _agents_api_module._HAS_OPENCC:
        pytest.skip("opencc not installed; skipping zh normalization test")
    _mock_leaderboard([
        _row("bot_ext_baolongbro", display_name="暴龍哥"),
        _row("bot_ext_other", display_name="Other Bot"),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.search("暴龙哥")
    assert len(result) == 1
    assert result[0].bot_id == "bot_ext_baolongbro"


@responses.activate
def test_search_traditional_query_matches_simplified_name():
    """The fold goes both ways: a traditional query (暴龍哥)
    against a simplified display_name (暴龙哥)."""
    if not _agents_api_module._HAS_OPENCC:
        pytest.skip("opencc not installed; skipping zh normalization test")
    _mock_leaderboard([
        _row("bot_ext_baolongbro", display_name="暴龙哥"),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.search("暴龍哥")
    assert len(result) == 1
    assert result[0].bot_id == "bot_ext_baolongbro"


@responses.activate
def test_search_partial_cjk_substring_matches():
    """Substring within a CJK name still works after folding —
    搜 '威龙' should match '港岛威龙' AND '港島威龍'."""
    if not _agents_api_module._HAS_OPENCC:
        pytest.skip("opencc not installed")
    _mock_leaderboard([
        _row("bot_ext_hkwarlock", display_name="港島威龍"),
        _row("bot_ext_other", display_name="No Man's Land"),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.search("威龙")  # simplified substring
    assert len(result) == 1
    assert result[0].bot_id == "bot_ext_hkwarlock"


@responses.activate
def test_search_ascii_path_unaffected_by_zh_normalization():
    """The ASCII fast-path should be unchanged — regression guard
    that we didn't accidentally route English queries through
    opencc."""
    _mock_leaderboard([
        _row("bestiedog", display_name="Bestie Dog"),
        _row("nomansland", display_name="No Man's Land"),
    ])
    client = AgentClient(token="bt_test")
    result = client.agents.search("BESTIE")
    assert len(result) == 1
    assert result[0].bot_id == "bestiedog"


@responses.activate
def test_search_bot_id_substring_works_without_opencc():
    """Even when opencc is missing, the bot_id (ASCII) substring
    path still works — documented as the workaround. Force the
    feature flag off to simulate a no-opencc install."""
    orig = _agents_api_module._HAS_OPENCC
    _agents_api_module._HAS_OPENCC = False
    try:
        _mock_leaderboard([
            _row("bot_ext_baolongbro", display_name="暴龍哥"),
        ])
        client = AgentClient(token="bt_test")
        result = client.agents.search("baolong")  # ASCII substring
        assert len(result) == 1
        assert result[0].bot_id == "bot_ext_baolongbro"
    finally:
        _agents_api_module._HAS_OPENCC = orig


# ── helper unit tests for the normalizer itself ──────────────────


def test_zh_variants_ascii_input_is_singleton():
    # ASCII inputs shouldn't ever go through opencc.
    variants = _agents_api_module._zh_variants("Hello")
    assert variants == {"hello"}


def test_zh_variants_empty_input_returns_empty_set():
    assert _agents_api_module._zh_variants("") == set()
    assert _agents_api_module._zh_variants("   ") == set()


def test_zh_variants_cjk_input_includes_both_folds():
    if not _agents_api_module._HAS_OPENCC:
        pytest.skip("opencc not installed")
    variants = _agents_api_module._zh_variants("暴龙哥")
    # Must include the input + the traditional fold.
    assert "暴龙哥" in variants
    assert "暴龍哥" in variants


def test_has_cjk_detection():
    assert _agents_api_module._has_cjk("暴龙哥") is True
    assert _agents_api_module._has_cjk("Hello") is False
    assert _agents_api_module._has_cjk("Hello 世界") is True
    assert _agents_api_module._has_cjk("") is False
