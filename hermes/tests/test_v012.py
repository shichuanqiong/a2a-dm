"""v0.1.2 tests — auto-wake, delivery ladder, skill install, inbox seed."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── gatewaycfg ────────────────────────────────────────────────────


def test_secret_is_persisted_and_stable(_isolated_hermes_home):
    from a2a_dm_hermes import gatewaycfg

    s1 = gatewaycfg.get_or_create_secret()
    s2 = gatewaycfg.get_or_create_secret()
    assert s1 == s2
    assert len(s1) == 64  # 32 bytes hex
    secret_file = _isolated_hermes_home / "a2a-dm-webhook.secret"
    assert secret_file.read_text().strip() == s1


def test_webhook_base_url_default_and_override(monkeypatch):
    from a2a_dm_hermes import gatewaycfg

    assert gatewaycfg.webhook_base_url() == "http://127.0.0.1:8644"
    monkeypatch.setenv("A2A_WAKE_WEBHOOK_URL", "http://10.0.0.5:9999/")
    assert gatewaycfg.webhook_base_url() == "http://10.0.0.5:9999"


def test_webhook_base_url_reads_config_port(_isolated_hermes_home):
    from a2a_dm_hermes import gatewaycfg

    (_isolated_hermes_home / "config.yaml").write_text(
        "platforms:\n  webhook:\n    port: 9001\n", encoding="utf-8"
    )
    assert gatewaycfg.webhook_base_url() == "http://127.0.0.1:9001"


def test_webhook_platform_enabled_detection(_isolated_hermes_home):
    from a2a_dm_hermes import gatewaycfg

    assert gatewaycfg.webhook_platform_enabled() is False  # no config
    cfg = _isolated_hermes_home / "config.yaml"
    cfg.write_text("platforms:\n  webhook:\n    port: 8644\n")
    assert gatewaycfg.webhook_platform_enabled() is True
    cfg.write_text("platforms:\n  webhook:\n    enabled: false\n")
    assert gatewaycfg.webhook_platform_enabled() is False


def test_ensure_routes_writes_both_and_preserves_existing(
    _isolated_hermes_home,
):
    from a2a_dm_hermes import gatewaycfg

    subs = _isolated_hermes_home / "webhook_subscriptions.json"
    subs.write_text(json.dumps({"user-route": {"secret": "x"}}))

    assert gatewaycfg.ensure_routes("bestiedog") is True
    data = json.loads(subs.read_text())
    assert "user-route" in data  # untouched
    assert gatewaycfg.WAKE_ROUTE in data
    assert gatewaycfg.NOTIFY_ROUTE in data

    wake = data[gatewaycfg.WAKE_ROUTE]
    assert wake["secret"] == gatewaycfg.get_or_create_secret()
    assert "a2a_reply" in wake["prompt"]
    assert "bestiedog" in wake["prompt"]
    assert wake["skills"] == ["a2a-dm"]
    assert wake["deliver"] == "telegram"  # default home target

    notify = data[gatewaycfg.NOTIFY_ROUTE]
    assert notify["deliver_only"] is True
    assert notify["prompt"] == "{text}"

    # Idempotent — second call, no rewrite needed, still True.
    assert gatewaycfg.ensure_routes("bestiedog") is True


def test_wake_home_env_sets_deliver_target(monkeypatch):
    from a2a_dm_hermes import gatewaycfg

    monkeypatch.setenv("A2A_WAKE_HOME", "discord:987654")
    routes = gatewaycfg.desired_routes("b")
    wake = routes[gatewaycfg.WAKE_ROUTE]
    assert wake["deliver"] == "discord"
    assert wake["deliver_extra"] == {"chat_id": "987654"}


def test_post_route_signs_with_hmac(monkeypatch):
    from a2a_dm_hermes import gatewaycfg

    captured = {}

    class FakeResp:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setattr(
        "a2a_dm_hermes.gatewaycfg.urllib.request.urlopen", fake_urlopen
    )
    ok = gatewaycfg.post_route(
        "a2a-dm-wake", {"text": "hi"}, request_id="task-123"
    )
    assert ok is True
    assert captured["url"].endswith("/webhooks/a2a-dm-wake")
    assert captured["headers"]["X-request-id"] == "task-123"
    expected_sig = hmac.new(
        gatewaycfg.get_or_create_secret().encode(),
        captured["body"],
        hashlib.sha256,
    ).hexdigest()
    assert captured["headers"]["X-webhook-signature"] == expected_sig


# ── autowake ──────────────────────────────────────────────────────


def test_autowake_disabled_by_env(monkeypatch):
    from a2a_dm_hermes.autowake import AutoWake, enabled

    monkeypatch.setenv("A2A_AUTO_WAKE", "0")
    assert enabled() is False
    aw = AutoWake("bestiedog")
    assert aw.ensure_ready() is False
    assert aw.wake({"task_id": "t1"}) is False


def test_autowake_requires_webhook_platform(monkeypatch):
    from a2a_dm_hermes.autowake import AutoWake

    monkeypatch.setenv("A2A_AUTO_WAKE", "1")
    aw = AutoWake("bestiedog")  # no config.yaml → platform disabled
    assert aw.ensure_ready() is False


def test_autowake_posts_with_task_id_as_request_id(
    monkeypatch, _isolated_hermes_home
):
    from a2a_dm_hermes import autowake as aw_mod
    from a2a_dm_hermes.autowake import AutoWake

    monkeypatch.setenv("A2A_AUTO_WAKE", "1")
    (_isolated_hermes_home / "config.yaml").write_text(
        "platforms:\n  webhook:\n    port: 8644\n"
    )

    calls = {}

    def fake_post(route, payload, request_id=None):
        calls["route"] = route
        calls["payload"] = payload
        calls["request_id"] = request_id
        return True

    monkeypatch.setattr(aw_mod.gatewaycfg, "post_route", fake_post)

    aw = AutoWake("bestiedog")
    entry = {
        "task_id": "task-42",
        "sender_bot_id": "laobaigan",
        "text": "hello",
        "group_id": None,
        "created_at": "2026-07-04T00:00:00+00:00",
    }
    assert aw.wake(entry) is True
    assert calls["route"] == "a2a-dm-wake"
    assert calls["request_id"] == "task-42"
    assert calls["payload"]["sender_bot_id"] == "laobaigan"
    assert calls["payload"]["group_id"] == ""  # None normalised

    # Routes were registered in the subscriptions file.
    subs = json.loads(
        (_isolated_hermes_home / "webhook_subscriptions.json").read_text()
    )
    assert "a2a-dm-wake" in subs


# ── delivery ladder ───────────────────────────────────────────────


def test_delivery_ladder_order_and_sticky(monkeypatch):
    from a2a_dm_hermes.delivery import HermesDelivery

    calls = []

    def t1(text):
        calls.append("t1")
        return False

    def t2(text):
        calls.append("t2")
        return True

    d = HermesDelivery()
    monkeypatch.setattr(
        HermesDelivery, "_TIERS",
        (("gateway-webhook", t1), ("hermes-send", t2)),
    )
    assert d.notify("a") is True
    assert calls == ["t1", "t2"]
    # Sticky: second notify goes straight to t2.
    assert d.notify("b") is True
    assert calls == ["t1", "t2", "t2"]


def test_delivery_falls_back_to_legacy_tg(monkeypatch):
    from a2a_dm_hermes import delivery as dmod
    from a2a_dm_hermes.delivery import HermesDelivery

    monkeypatch.setattr(
        HermesDelivery, "_TIERS",
        (("t1", lambda _: False), ("t2", lambda _: False)),
    )
    monkeypatch.setenv("A2A_WAKE_TG_TOKEN", "tok")
    monkeypatch.setenv("A2A_WAKE_TG_CHAT_ID", "42")
    sent = {}

    def fake_tg(token, chat_id, text):
        sent.update(token=token, chat_id=chat_id, text=text)
        return True

    monkeypatch.setattr(dmod, "_tg_send", fake_tg)
    d = HermesDelivery()
    assert d.notify("ping") is True
    assert sent == {"token": "tok", "chat_id": "42", "text": "ping"}


def test_delivery_returns_false_when_all_tiers_fail(monkeypatch):
    from a2a_dm_hermes.delivery import HermesDelivery

    monkeypatch.setattr(
        HermesDelivery, "_TIERS",
        (("t1", lambda _: False), ("t2", lambda _: False)),
    )
    d = HermesDelivery()
    assert d.notify("x") is False


# ── skill install ─────────────────────────────────────────────────


def test_skill_file_installed_and_idempotent(_isolated_hermes_home):
    from a2a_dm_hermes import skillinstall

    assert skillinstall.install_skill_file("bestiedog") is True
    path = _isolated_hermes_home / "skills" / "a2a-dm" / "SKILL.md"
    content = path.read_text()
    assert "@bestiedog" in content
    assert "a2a_get_inbox" in content
    assert "a2a-dm-skill-version:" in content

    mtime = path.stat().st_mtime_ns
    assert skillinstall.install_skill_file("bestiedog") is True
    assert path.stat().st_mtime_ns == mtime  # unchanged → not rewritten


def test_skill_file_respects_user_owned(_isolated_hermes_home):
    from a2a_dm_hermes import skillinstall

    path = _isolated_hermes_home / "skills" / "a2a-dm" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# my custom skill, hands off")
    assert skillinstall.install_skill_file("bestiedog") is True
    assert path.read_text() == "# my custom skill, hands off"


def test_register_installs_skill(monkeypatch, _isolated_hermes_home):
    monkeypatch.setenv("AGORADIGEST_TOKEN", "")
    monkeypatch.setenv("AGORADIGEST_BOT_ID", "")
    import importlib

    import a2a_dm_hermes
    importlib.reload(a2a_dm_hermes)

    class Ctx:
        def __init__(self):
            self.skills = {}
            self.hooks = {}

        def register_tool(self, **kw):
            pass

        def register_hook(self, event, cb):
            self.hooks.setdefault(event, []).append(cb)

        def register_command(self, *a, **kw):
            pass

        def register_skill(self, name, content):
            self.skills[name] = content

    ctx = Ctx()
    a2a_dm_hermes.register(ctx)
    assert "a2a-dm" in ctx.skills
    assert "session:start" in ctx.hooks
    assert (
        _isolated_hermes_home / "skills" / "a2a-dm" / "SKILL.md"
    ).exists()


# ── runtime: seed_from_inbox ──────────────────────────────────────


def _fake_task(task_id, sender="laobaigan", text="hi", group_id=None):
    t = MagicMock()
    t.id = task_id
    t.sender_bot_id = sender
    t.message.text = text
    t.is_group_message = bool(group_id)
    t.group_id = group_id
    t.created_at = "2026-07-04T00:00:00+00:00"
    return t


def test_seed_from_inbox_dedupes_seen_tasks():
    from a2a_dm_hermes.runtime import WakeRuntime

    rt = WakeRuntime()  # fresh instance, not the singleton
    fake_client = MagicMock()
    view = MagicMock()
    view.tasks = [_fake_task("t1"), _fake_task("t2")]
    fake_client.dm.inbox.return_value = view
    rt._client = fake_client

    assert rt.seed_from_inbox(force=True) == 2
    assert rt.pending_count() == 2

    # Same tasks again → all deduped.
    assert rt.seed_from_inbox(force=True) == 0

    # Drain doesn't forget seen-ness.
    rt.drain()
    assert rt.seed_from_inbox(force=True) == 0

    # A genuinely new task gets queued.
    view.tasks = [_fake_task("t3")]
    assert rt.seed_from_inbox(force=True) == 1


def test_seed_from_inbox_respects_cache(monkeypatch):
    from a2a_dm_hermes.runtime import WakeRuntime

    rt = WakeRuntime()
    fake_client = MagicMock()
    view = MagicMock()
    view.tasks = [_fake_task("t1")]
    fake_client.dm.inbox.return_value = view
    rt._client = fake_client

    assert rt.seed_from_inbox() == 1          # first call runs
    view.tasks = [_fake_task("t2")]
    assert rt.seed_from_inbox() == 0          # cached — skipped
    assert fake_client.dm.inbox.call_count == 1


def test_seed_from_inbox_no_client():
    from a2a_dm_hermes.runtime import WakeRuntime

    rt = WakeRuntime()
    assert rt.seed_from_inbox(force=True) == 0


def test_on_wake_marks_seen_so_seed_skips(monkeypatch):
    """A DM that arrived via SSE must not be re-queued by the scan."""
    from a2a_dm_hermes.runtime import WakeRuntime

    rt = WakeRuntime()
    rt._autowake = None  # keep the background thread trivial
    monkeypatch.setattr(
        "a2a_dm_hermes.runtime.notify_operator", lambda *_: True
    )

    task = _fake_task("t-sse")
    rt._on_wake(task, None)
    assert rt.pending_count() == 1

    fake_client = MagicMock()
    view = MagicMock()
    view.tasks = [_fake_task("t-sse")]
    fake_client.dm.inbox.return_value = view
    rt._client = fake_client
    assert rt.seed_from_inbox(force=True) == 0
    assert rt.pending_count() == 1


# ── SDK skill source ──────────────────────────────────────────────


def test_sdk_skill_markdown_personalised():
    from a2a_dm.skill import get_skill_markdown

    md = get_skill_markdown(bot_id="bestiedog")
    assert "@bestiedog" in md
    assert "a2a_get_inbox" in md
    assert "a2a_send_group" in md

    md_anon = get_skill_markdown()
    assert "@" not in md_anon.split("\n")[3]  # no identity clause
