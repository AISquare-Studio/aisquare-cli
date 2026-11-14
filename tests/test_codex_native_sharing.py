"""Codex reads what its slot links to the plain home, live, on loopback only (#113's #22).

Run ``AISQUARE_TEST_CODEX=1 pytest tests/test_codex_native_sharing.py``, in the pattern of
``tests/test_codex_native_295_296.py`` (its loopback provider and scratch home): no account,
login, provider credential or model spend.

The parts Codex declares (``agent_adapters/codex_shared.py``) are the ones measured to be read
through a link on 0.164.0 and 0.162.1: ``AGENTS.md`` reaches the model's request and a linked
``skills`` folder lists the same skills as its own, Codex writing its bundled ``.system`` skills
into the folder it finds. ``prompts`` is no part because Codex reads no such folder, linked or
not (``/prompts:name`` is "Unrecognized command").
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import claude_sharing
from aisquare.core.agent_adapters import get_adapter
from aisquare.services import fleet
from tests.test_codex_native_295_296 import TRUSTED, Loopback, loopback  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test"
)


def test_real_codex_reads_the_agents_md_and_the_skills_its_slot_links_to_the_plain_home(
    loopback: Loopback,  # noqa: F811
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain-codex"
    (plain / "skills" / "greet").mkdir(parents=True)
    (plain / "skills" / "greet" / "SKILL.md").write_text(
        "---\nname: greet\ndescription: SHARED-SKILL-MARKER greets people\n---\nsay hi\n",
        encoding="utf-8",
    )
    (plain / "AGENTS.md").write_text("SHARED-AGENTS-MARKER always answer ok\n", encoding="utf-8")
    (plain / "prompts").mkdir()
    (plain / "prompts" / "hi.md").write_text("SHARED-PROMPT-MARKER greet\n", encoding="utf-8")
    parts = get_adapter("codex").accounts.share_parts  # type: ignore[union-attr]
    assert [part.name for part in parts] == ["agents_md", "skills"]
    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    for part in parts:  # the slot is the scratch home Codex runs on
        assert claude_sharing.link_part(loopback.home, part, plain, now=now) is None

    agent = fleet.spawn(
        loopback.project, "coder", agent="codex", worktree=False, prompt="say hello",
        agent_args=TRUSTED,
    ).agent  # fmt: skip
    loopback.until(lambda: loopback.ran("say hello"), 40, "the first prompt ran", agent.pane_id)

    request = json.dumps(loopback.requests[0])
    assert "SHARED-AGENTS-MARKER" in request, "its AGENTS.md was read through the link"
    assert "SHARED-SKILL-MARKER" in request, "its skills were listed through the link"
    assert (loopback.home / "AGENTS.md").is_symlink() and (loopback.home / "skills").is_symlink()
    assert (plain / "skills" / ".system").is_dir(), (
        "Codex's bundled skills landed in the shared one"
    )

    # The folder it does not read, linked or not.
    loopback.type_into(agent.pane_id, "/prompts:hi")
    deadline = time.monotonic() + 10
    while "Unrecognized command" not in loopback.screen(agent.pane_id):
        assert time.monotonic() < deadline, loopback.screen(agent.pane_id)
        time.sleep(0.25)
    assert not any("SHARED-PROMPT-MARKER" in json.dumps(r) for r in loopback.requests)
