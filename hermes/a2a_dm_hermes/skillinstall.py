"""Install the bundled a2a-dm skill into Hermes (v0.1.2).

The skill text lives in the SDK (:mod:`a2a_dm.skill`) as the single
source shared by every runtime integration. This module adapts it to
Hermes two ways:

  1. ``ctx.register_skill(...)`` when the running Hermes exposes that
     API (newer builds).
  2. A file drop into ``~/.hermes/skills/a2a-dm/SKILL.md`` — the
     stable path every Hermes version scans, and the one the webhook
     wake route's ``skills: ["a2a-dm"]`` lookup resolves against.

Both are attempted; both are best-effort. A version marker in the
file lets us refresh content on plugin upgrades without clobbering
user edits made on top of a *current* version.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from a2a_dm.skill import SKILL_NAME, SKILL_VERSION, get_skill_markdown

logger = logging.getLogger(__name__)

_MARKER = f"<!-- a2a-dm-skill-version: {SKILL_VERSION} -->"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _skill_file_content(bot_id: str | None) -> str:
    return f"{_MARKER}\n{get_skill_markdown(bot_id=bot_id)}"


def install_skill_file(bot_id: str | None = None) -> bool:
    """Write ``~/.hermes/skills/a2a-dm/SKILL.md`` if missing or stale.

    Stale = our version marker is present but older, or the file was
    written by us and content changed. A file *without* our marker is
    treated as user-owned and left alone.
    """
    skill_dir = _hermes_home() / "skills" / SKILL_NAME
    skill_path = skill_dir / "SKILL.md"
    try:
        desired = _skill_file_content(bot_id)
        if skill_path.exists():
            current = skill_path.read_text(encoding="utf-8")
            if "a2a-dm-skill-version:" not in current:
                logger.debug(
                    "a2a-dm: %s exists without our marker — user-owned, "
                    "not touching it", skill_path,
                )
                return True
            if current == desired:
                return True
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(desired, encoding="utf-8")
        logger.info("a2a-dm: installed skill file %s", skill_path)
        return True
    except OSError:
        logger.warning(
            "a2a-dm: could not install skill file at %s", skill_path,
            exc_info=True,
        )
        return False


def register_skill(ctx, bot_id: str | None = None) -> None:
    """Best-effort dual registration (API + file). Never raises."""
    md = get_skill_markdown(bot_id=bot_id)
    try:
        register = getattr(ctx, "register_skill", None)
        if callable(register):
            register(SKILL_NAME, md)
            logger.info("a2a-dm: skill registered via ctx.register_skill")
    except Exception:  # noqa: BLE001
        logger.debug("a2a-dm: ctx.register_skill failed", exc_info=True)
    install_skill_file(bot_id)
