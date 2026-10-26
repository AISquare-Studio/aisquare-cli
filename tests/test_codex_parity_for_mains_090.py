"""Codex parity for main's 0.9.0 features (SCOPE #113 090), on the fleet's fake tmux and Codex's
own hook payloads: the rows that say PARITY, each through the adapter.

- W1a, admission: a Codex spawn counts against ``max_agents_total`` as a Claude Code one does,
  and is refused before any window.
- W1b, auto-stop: Codex's ``Stop`` arms it (``hooks._arm_auto_stop``), and its last check, the
  pane, is read through the pane's own agent (``captain.screen.ready``): an empty Codex
  composer is idle, a draft in it is not.
- W2, ``aisquare watch``: its needs are Remote's scan (``remote_needs.scan_needs_you``), whose
  Codex dialog, limit, crash and stall reads ``test_remote_needs_codex.py`` and
  ``test_remote_dialogs_codex.py`` pin; the watcher classifies nothing a third time.
- W5, the symbol index: Codex's SessionStart refreshes it as Claude Code's does, and Codex's
  MCP config mounts the one server that serves ``code_lookup``.
- #405, the role-less ``agent``: it starts on Codex with none of a role's flags.
- #402, the installer hand-off: ``--agent codex`` goes to the release's own installer only once
  that one has the option, and otherwise this script carries on with its own Codex route.

The accounts rows (#406, #408, Log in again) are in ``test_codex_parity_for_mains_090_accounts``
and the evidence gate's (#417) in ``test_codex_parity_for_mains_090_evidence``: each family has
fixtures of its own that cannot share a module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aisquare.core.config import load_config, save_config
from aisquare.models import FleetAgent, ProjectInfo, TeamTask
from aisquare.services import agent_launch, autostop, delivery, hooks, remote_needs, updates
from aisquare.services import fleet as fleet_service
from aisquare.services import watch as watch_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import (  # noqa: F401 - fixtures, by name
    DRAFT,
    IDLE_PROMPT,
    THREAD_ONE,
    Codex,
    _native,
    _rollout,
    codex_agent,
)
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _command,
    _settings,
    project,
    repo,
    tmux,
)
from tests.test_install_script_handoff import (  # noqa: F401 - fixture, by name
    DRY,
    STAMP,
    Machine,
    machine,
    reporting_release,
)

CODEX = "codex"
#: The flags of a run that ends quickly and changes nothing, and asks for Codex: `--no-agent`
#: would contradict `--agent`.
DRY_CODEX = tuple(flag for flag in DRY if flag != "--no-agent")

# --- W1a: admission -----------------------------------------------------------------------


def test_a_codex_spawn_counts_against_the_fleet_budget_and_is_refused_before_a_window(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings(monkeypatch, max_agents_total=1)
    fleet_service.spawn(project, "coder", agent="codex", worktree=False)
    windows = len(tmux.spawned)

    with pytest.raises(fleet_service.CapacityRefused) as refused:
        fleet_service.spawn(project, "reviewer", agent="codex", worktree=False)

    assert refused.value.code == "capacity_fleet"
    assert len(tmux.spawned) == windows, "no window was opened for the refused spawn"


# --- W1b: auto-stop -----------------------------------------------------------------------


def test_codexs_stop_arms_the_auto_stop_as_claude_codes_does(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    armed: list[str] = []
    monkeypatch.setattr(autostop, "arm", lambda session_id, **_: armed.append(session_id))
    codex, _ = codex_agent
    codex.start(THREAD_ONE)
    codex.hook("UserPromptSubmit", THREAD_ONE, turn_id="turn-1", prompt="start on it")

    codex.hook("Stop", THREAD_ONE, turn_id="turn-1")

    assert armed == [codex.board_id(THREAD_ONE)]


@pytest.mark.parametrize(
    ("screen", "refusal"),
    [(IDLE_PROMPT, None), (DRAFT, "it is not idle at its prompt")],
    ids=["empty composer", "a draft"],
)
def test_a_codex_worker_is_stopped_only_at_an_idle_empty_prompt(
    screen: list[str], refusal: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check before an auto-stop reads the pane by the pane's own agent: Codex's empty
    composer is idle (Claude Code's idle input box is), a draft in it is a human's."""
    server = SimpleNamespace(capture=lambda pane_id: SimpleNamespace(lines=list(screen)))
    monkeypatch.setattr(fleet_service, "server_of", lambda agent: server)
    monkeypatch.setattr(delivery, "draft_guard", lambda *args, **kwargs: None)
    agent = FleetAgent(
        id="agt_codex1",
        project_id="prj_x",
        role="tester",
        label="tester-1",
        binary="codex",
        agent="codex",
        pane_id="%1",
        cwd=Path("/w"),
        created_at=datetime.now(tz=UTC),
    )

    assert autostop.pane_refusal(agent) == refusal


# --- W2: aisquare watch -------------------------------------------------------------------


def test_the_watcher_takes_a_codex_agents_needs_from_the_phones_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Watcher.needs`` is ``scan_needs_you`` and nothing else: what the scan reads off a
    Codex pane or rollout (its approval dialog, a usage limit, a crash, a stall) is the
    scan's, pinned by ``test_remote_needs_codex.py``."""
    item = SimpleNamespace(
        id="agt_codex1:permission",
        project_id="prj_x",
        project_name="repo",
        kind="permission",
        agent="coder-1",
        reason="Codex asks to run a command",
        excerpt="rm -rf build",
        since=datetime.now(tz=UTC),
        actions=("approve", "deny"),
    )
    seen: list[Any] = []

    def scan(sources: Any, **kwargs: Any) -> list[Any]:
        seen.append(sources)
        return [item]

    monkeypatch.setattr(remote_needs, "scan_needs_you", scan)
    monkeypatch.setattr(remote_needs, "load_needs_dismissals", lambda: set())
    watcher = watch_service.Watcher(needs_sources=lambda: "the phone's sources", stale=False)

    [found] = watcher.needs(datetime.now(tz=UTC))

    assert seen == ["the phone's sources"]
    assert (found.id, found.kind) == ("needs:agt_codex1:permission", "permission")
    assert found.summary == "coder-1: Codex asks to run a command"


# --- W5: the symbol index -----------------------------------------------------------------


def test_codexs_session_start_refreshes_the_symbol_index(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresh is the shared session-start path's (``hooks.session_start_context``, which
    ``agent_events`` takes every agent's start to); the suite stubs its detached worker, so
    this holds the call: made with the project, for Codex's start."""
    started: list[Path] = []
    monkeypatch.setattr(hooks, "_refresh_code_index", lambda found: started.append(found.root))
    codex, _ = codex_agent

    codex.start(THREAD_ONE)

    assert started == [project.root]


def test_codexs_mcp_config_mounts_the_server_that_serves_code_lookup(
    codex_on_path: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``code_lookup`` is a tool of the one aisquare MCP server (``mcp_server.build_server``,
    there only with an index: ``test_code_cli.test_the_mcp_tool_is_there_only_with_an_index``).
    Codex launches that same server through its own ``-c mcp_servers.aisquare.*`` flags."""
    config = load_config()
    config.agents.mcp = True
    save_config(config)

    mounted = agent_launch.mcp_args(agent_launch.resolve(agent=CODEX)).args

    assert any(arg.startswith("mcp_servers.aisquare.command=") for arg in mounted), mounted
    assert any("serve" in arg and "--stdio" in arg for arg in mounted), mounted


# --- #301: Restart & update reattaches agents ---------------------------------------------


def test_the_update_reopen_restarts_a_codex_agent_whose_pane_is_gone_on_its_thread(
    codex_agent: tuple[Codex, TeamTask],  # noqa: F811
    tmux: FakeTmux,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reopen's restart is ``fleet.restart``, pinned to the recorded row: a Codex agent is
    brought back as every restart brings one, ``codex <options> resume <thread>`` through its
    launch record, and keeps its claim."""
    codex, _ = codex_agent
    _rollout(codex, THREAD_ONE)
    codex.start(THREAD_ONE)
    monkeypatch.setattr(updates, "__version__", "0.9.0")
    alive = updates.agents_alive_now({codex.project.id})
    updates.record_press(alive)
    monkeypatch.setattr(updates, "__version__", "0.9.1")
    tmux.vanish(codex.agent.pane_id)  # the pane did not survive
    windows = len(tmux.spawned)

    outcome = updates.reattach_after_update()

    assert [agent.label for agent in alive] == [codex.agent.label]
    assert outcome is not None and [label for label, _ in outcome.restarted] == [codex.agent.label]
    assert len(tmux.spawned) == windows + 1, "one window for the one agent"
    native = _native(_command(tmux))
    at = native.index("resume")
    assert native[at : at + 2] == ["resume", THREAD_ONE]
    assert "--resume" not in _command(tmux), "no Claude Code flag"


# --- #405: the role-less agent ------------------------------------------------------------


def test_a_role_less_agent_starts_on_codex_with_none_of_a_roles_flags(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    receipt = fleet_service.spawn(project, fleet_service.PLAIN_ROLE, agent=CODEX, worktree=False)

    assert (receipt.agent.agent, receipt.agent.role) == (CODEX, fleet_service.PLAIN_ROLE)
    command = " ".join(_command(tmux))
    assert "--model" not in command and "--effort" not in command, command


# --- #402: the installer hand-off ---------------------------------------------------------


def test_a_codex_install_stays_on_this_installer_until_the_release_has_the_option(
    machine: Machine,  # noqa: F811
) -> None:
    machine.pypi(STAMP)
    machine.serve_latest(reporting_release())  # a release that does not parse --agent

    result = machine.run(*DRY_CODEX, "--agent", "codex")

    assert "using this installer (the v7.7.7 installer has no --agent option)" in result.stdout
    assert "RELEASE RAN" not in result.stdout, "the release's installer must not run"
    assert "Codex" in result.stdout, "this script's own Codex route carried on"


def test_a_codex_install_is_handed_to_a_release_that_has_the_option(
    machine: Machine,  # noqa: F811
) -> None:
    machine.pypi(STAMP)
    machine.serve_latest(
        reporting_release(
            options=(
                "--yes",
                "--no-agent",
                "--no-system-deps",
                "--no-project",
                "--project",
                "--version",
                "--version=*",
                "--dry-run",
                "--agent",
            )
        )
    )

    result = machine.run(*DRY_CODEX, "--agent", "codex")

    assert "using the v7.7.7 installer" in result.stdout
    assert "ARG[--agent]" in result.stdout and "ARG[codex]" in result.stdout, result.stdout
