"""Shared fixtures — isolate every test from the real ~/.hermes.

v0.1.2 made the plugin write files under HERMES_HOME (webhook route
subscriptions, HMAC secret, bundled SKILL.md). Tests must never touch
the developer's real gateway config, so HERMES_HOME is pointed at a
tmp dir for every test, and auto-wake networking is disabled unless a
test opts back in.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Default-off in tests: no gateway is listening, so auto-wake and
    # the notification ladder would only add latency + log noise.
    monkeypatch.setenv("A2A_AUTO_WAKE", "0")
    monkeypatch.delenv("A2A_WAKE_TG_TOKEN", raising=False)
    monkeypatch.delenv("A2A_WAKE_TG_CHAT_ID", raising=False)
    monkeypatch.delenv("A2A_WAKE_HOME", raising=False)
    monkeypatch.delenv("A2A_WAKE_WEBHOOK_URL", raising=False)
    return home
