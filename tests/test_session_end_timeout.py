"""RLX R20 (#154): a session's end reaches the board on a slow Mac.

Claude Code gives a ``SessionEnd`` hook 1.5 s unless the hook names a ``timeout`` (2.1.318
and 2.1.326 alike: the largest one, capped at 60 s, or
``CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS``). The interpreter and its imports are inside that
budget, and it cancels the hook past it. Then the session's end never reaches the board: the
row reads running, and its claims stay held until their lease runs out. 0.10.0 installed the
hook with no ``timeout``. It now names :data:`SESSION_END_TIMEOUT_SECONDS`.

An install from 0.10.0 reads as short, in ``doctor`` and in what install.sh's short-circuit
reads, and the upgrade's own rewrite (``agents refresh-hooks``, which ``aisquare upgrade`` and
asq's *Update* run from the new install for every site it connected) gives it the timeout. It
writes one entry, and leaves other tools' hooks as they were.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import store
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.claude_code import SESSION_END_TIMEOUT_SECONDS
from aisquare.models import CheckStatus
from aisquare.services import diagnostics, lifecycle

#: Another tool's session-end hook, after ours, and another's Stop hook, before ours: what an
#: operator's settings.json holds beside aisquare's.
FOREIGN_END = {"hooks": [{"type": "command", "command": "/opt/notes/bin/flush --quiet"}]}
FOREIGN_STOP = {"matcher": "", "hooks": [{"type": "command", "command": "say done", "timeout": 5}]}


def _spec(event: str) -> Any:
    (spec,) = [s for s in get_adapter("claude-code").capabilities.hooks if s.event == event]
    return spec


def _ours(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for group in groups
        for item in group["hooks"]
        if agent_core._is_aisquare_hook_command(str(item.get("command", "")))
    ]


#: A session end short of its timeout: none, as 0.10.0's ``agents connect`` wrote it
#: (``HookSpec("SessionEnd", "session-end")`` in v0.10.0), or a shorter one an operator set.
SHORT = pytest.mark.parametrize(
    "short", [None, SESSION_END_TIMEOUT_SECONDS - 1], ids=["0.10.0", "9"]
)


def _with_session_end(runner: CliRunner, claude: Path, timeout: int | None) -> Path:
    """``claude``'s settings.json as ``agents connect`` writes it, our session end's
    ``timeout`` then ``timeout`` (``None``: none, 0.10.0's), and an operator's key and two
    other tools' hooks beside ours.

    Claude Code installed, as ``fake_home`` makes it: its config dir is there. The suite's
    stand-in ``claude`` alone does not make it so on Windows, where ``shutil.which`` does not
    see it (no ``PATHEXT`` extension), and connect then refused "claude-code is not installed
    on this machine" (#521's Windows shards, tester 12781)."""
    claude.mkdir(parents=True, exist_ok=True)
    connected = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert connected.exit_code == 0, connected.output
    path = claude / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    (ours,) = _ours(settings["hooks"]["SessionEnd"])
    if timeout is None:
        del ours["timeout"]
    else:
        ours["timeout"] = timeout
    settings["hooks"]["SessionEnd"].append(FOREIGN_END)
    settings["hooks"]["Stop"].insert(0, FOREIGN_STOP)
    settings["theme"] = "dark"
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return path


def test_the_session_end_hook_names_a_timeout_that_outlasts_a_busy_store() -> None:
    """Its own timeout, which Claude Code honours up to 60 s. It must cover the store's busy
    wait, since a session end behind another writer waits for it (measured: a lock held 3 s,
    a session end of 3.1 s, which reached the board)."""
    assert _spec("SessionEnd").timeout == SESSION_END_TIMEOUT_SECONDS == 10
    assert SESSION_END_TIMEOUT_SECONDS * 1000 > store._DEFAULT_BUSY_MS
    assert SESSION_END_TIMEOUT_SECONDS <= 60, "Claude Code's cap: more would buy nothing"
    assert agent_core.required_hook_timeout(_spec("SessionEnd")) == SESSION_END_TIMEOUT_SECONDS


@SHORT
def test_a_short_session_end_reads_as_short_and_the_upgrades_rewrite_gives_it_its_timeout(
    short: int | None, runner: CliRunner, isolated_agent_home: Path
) -> None:
    """0.10.0's session end, with no timeout, and one an operator set shorter (9 s): each reads
    as short, in ``hook_timeout_shortfall`` and doctor's row, and the upgrade raises it to 10."""
    claude = isolated_agent_home / ".claude"
    path = _with_session_end(runner, claude, short)
    foreign_end, foreign_stop = json.dumps(FOREIGN_END), json.dumps(FOREIGN_STOP)

    # Short: installed and firing, and its own line in doctor, which offers connect.
    assert agent_core.hooks_installed("claude-code") is True, "the hooks are there"
    assert agent_core.hook_timeout_shortfall("claude-code") == ["SessionEnd"]
    row = diagnostics._check_claude_code()
    assert row.status is CheckStatus.warn, row
    # The sentence, not the directory's spelling, which Windows may give otherwise.
    assert "connected, but the session-end hook has too little time in: " in row.detail, row
    assert "(SessionEnd)" in row.detail and "(SessionStart" not in row.detail, row
    assert "context hooks" not in row.detail, "the context hooks are not short"
    assert "agents connect claude-code" in (row.fix or ""), row.fix

    # The upgrade rewrites every site this home connected, by the new install's command.
    candidates = {site.resolve() for site in lifecycle._refresh_candidates("claude-code")}
    assert claude.resolve() in candidates
    argv = [*lifecycle.refresh_hooks_argv("claude-code"), "--config-dir", str(claude)]
    refreshed = runner.invoke(app, argv)
    assert refreshed.exit_code == 0, refreshed.output

    settings = json.loads(path.read_text(encoding="utf-8"))
    (ours,) = _ours(settings["hooks"]["SessionEnd"])  # one entry: no duplicate
    assert ours["timeout"] == SESSION_END_TIMEOUT_SECONDS
    assert [json.dumps(g) for g in settings["hooks"]["SessionEnd"]].count(foreign_end) == 1
    assert json.dumps(settings["hooks"]["Stop"][0]) == foreign_stop, "theirs, where it was"
    assert len(_ours(settings["hooks"]["Stop"])) == 1
    assert settings["theme"] == "dark"
    assert agent_core.hook_timeout_shortfall("claude-code") == []
    assert "too little time" not in diagnostics._check_claude_code().detail

    # Current now: the next upgrade writes nothing.
    before = path.read_bytes()
    again = runner.invoke(app, argv)
    assert again.exit_code == 0, again.output
    assert path.read_bytes() == before


@SHORT
def test_doctors_fix_connect_gives_a_short_session_end_its_timeout_once(
    short: int | None, runner: CliRunner, isolated_agent_home: Path
) -> None:
    """The row's own remedy, ``agents connect``, mends it the same way."""
    claude = isolated_agent_home / ".claude"
    path = _with_session_end(runner, claude, short)
    assert runner.invoke(app, ["agents", "connect", "claude-code"]).exit_code == 0
    settings = json.loads(path.read_text(encoding="utf-8"))
    (ours,) = _ours(settings["hooks"]["SessionEnd"])
    assert ours["timeout"] == SESSION_END_TIMEOUT_SECONDS
    assert json.dumps(settings["hooks"]["SessionEnd"][-1]) == json.dumps(FOREIGN_END)
    assert agent_core.hook_timeout_shortfall("claude-code") == []


def test_a_longer_session_end_timeout_is_enough() -> None:
    """ "Sufficient", as for the context hooks: an operator's longer value is no shortfall."""
    group = {"hooks": [{"type": "command", "command": "aisquare hook session-end", "timeout": 30}]}
    assert agent_core._is_current_aisquare_group(group, SESSION_END_TIMEOUT_SECONDS)
    group["hooks"][0]["timeout"] = SESSION_END_TIMEOUT_SECONDS - 1
    assert not agent_core._is_current_aisquare_group(group, SESSION_END_TIMEOUT_SECONDS)
    group["hooks"][0]["timeout"] = True
    assert not agent_core._is_current_aisquare_group(group, SESSION_END_TIMEOUT_SECONDS)


def test_a_longer_session_end_timeout_an_operator_set_is_kept_by_the_upgrade_and_connect(
    runner: CliRunner, isolated_agent_home: Path
) -> None:
    """The manager's ruling (a) on #521: the writer agrees with drift, "sufficient, not equal".
    An operator's 30 s session end is no shortfall, and neither the upgrade's
    ``refresh-hooks`` nor ``connect`` cuts it to 10. Once the file is current, neither writes
    it at all."""
    claude = isolated_agent_home / ".claude"
    path = _with_session_end(runner, claude, 30)
    assert agent_core.hook_timeout_shortfall("claude-code") == []
    argv = [*lifecycle.refresh_hooks_argv("claude-code"), "--config-dir", str(claude)]
    assert runner.invoke(app, argv).exit_code == 0  # its own format, the 30 kept
    (ours,) = _ours(json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"])
    assert ours["timeout"] == 30
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    assert runner.invoke(app, argv).exit_code == 0
    assert runner.invoke(app, ["agents", "connect", "claude-code"]).exit_code == 0
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp, "untouched"
    assert "too little time" not in diagnostics._check_claude_code().detail


def test_codex_clamps_its_session_end_so_a_reconnect_writes_the_spec_s(tmp_path: Path) -> None:
    """Codex declares the events it clamps at the spec's own value
    (``AgentCapabilities.clamped_hook_events``): its ``SessionEnd`` and ``Interrupt``, at 3 s.
    There a longer timeout buys nothing, and a reconnect writes 3 over an operator's 5. Claude
    Code declares none, so its longer session end is kept."""
    codex = get_adapter("codex")
    assert codex.capabilities.clamped_hook_events == {"SessionEnd", "Interrupt"}
    assert get_adapter("claude-code").capabilities.clamped_hook_events == frozenset()
    agent_core.install_hooks("codex", tmp_path)
    path = tmp_path / codex.settings_name
    hooks = json.loads(path.read_text(encoding="utf-8"))
    (ours,) = _ours(hooks["hooks"]["SessionEnd"])
    ours["timeout"] = 5
    path.write_text(json.dumps(hooks, indent=2), encoding="utf-8")
    agent_core.install_hooks("codex", tmp_path)
    (ours,) = _ours(json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionEnd"])
    assert ours["timeout"] == 3
