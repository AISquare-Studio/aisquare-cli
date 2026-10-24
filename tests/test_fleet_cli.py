"""``aisquare fleet …`` and ``ui``: parse, call the service, render — in both modes.

The CLI is a thin layer over :mod:`aisquare.services.fleet`, and that is exactly
what these tests hold it to. The service is FAKED on the module the CLI reads it
from (``aisquare.cli.fleet.fleet_service``), returning real ``FleetAgent`` /
``FleetAgentStatus`` / ``SpawnReceipt`` models, so every assertion here is about
the CLI's own contract (docs/plans/fleet-tui.md §5.3): which service call a
command line becomes, what the human line says, that ``--json`` is one parseable
object carrying the documented keys, and that every ``FleetError`` reaches the
operator as a ``fail`` payload with the right code — never a traceback.

Two things are NOT faked, on purpose: the project resolver in the ``not_found``
test (so the error path is proven against the real store, not a fake raising
what the test expects), and the ``ui`` predicate in the TTY tests (fake streams
are handed to the real predicate).

Every claim has a control on the other side: the ``asked:`` suffix is asserted
present when the label was changed AND absent when it was honoured; forwarded
agent args are asserted to arrive AND the command's own options are asserted
NOT to leak into them; ``attach`` execs in human mode AND does not under
``--json``.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.app import app
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import install_route
from aisquare.services.fleet import (
    BulkReceipt,
    BulkReport,
    FleetError,
    FleetUnavailable,
    NoSuchAgent,
    NoSuchProject,
    ReapReport,
    RestartReceipt,
    ShutdownPlan,
    ShutdownReport,
    ShutdownRow,
    SpawnReceipt,
    StopReceipt,
    TellResult,
    Typed,
)
from tests.cli_tree import NEEDED_UNLESS, _placeholder, root_command
from tests.rendered import ANSI
from tests.rendered import plain as _plain

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
PROJECT = ProjectInfo(id="prj_01abc", root=Path("/home/me/work/api"), codename="amber-otter")
#: Built once and compared through `str()`/`.name`, never against a hardcoded
#: "/a/b" literal: `str(Path(...))` renders with the platform's separator, so a
#: forward-slash expectation asserts the POSIX half of a contract the CLI keeps
#: on both. Same class as the escaped-path assertions ported earlier.
WORKTREE = Path("/home/me/work/api/.aisquare-worktrees/coder-auth")
UNNAMED = ProjectInfo(id="prj_02def", root=Path("/home/me/oss/tool"), codename=None)
SESSION = "asq-amber-otter"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NO_COLOR", "1")
    return work


def _agent(
    label: str = "coder-auth",
    role: str = "coder",
    *,
    pane: str = "%7",
    worktree: bool = False,
    task_id: str | None = None,
    ended: bool = False,
    exit_status: int | None = None,
    project: ProjectInfo = PROJECT,
) -> FleetAgent:
    return FleetAgent(
        id=f"agt_{label.replace('-', '')}",
        project_id=project.id,
        label=label,
        role=role,
        pane_id=pane,
        session_id="11111111-2222-3333-4444-555555555555",
        cwd=project.root,
        worktree=worktree,
        task_id=task_id,
        spawned_by="user",
        created_at=NOW,
        ended_at=NOW if ended else None,
        exit_status=exit_status,
    )


def _status(
    agent: FleetAgent,
    state: str = "working",
    *,
    detail: str | None = None,
    with_session: bool = False,
) -> FleetAgentStatus:
    session = None
    if with_session:
        session = TeamSession(
            id=agent.session_id or "s",
            project_id=agent.project_id,
            role=agent.role,
            label=agent.label,
            started_at=NOW,
            last_seen_at=NOW,
            state=state,
        )
    return FleetAgentStatus(
        agent=agent,
        state=state,  # the Literal is what the sweep varies; pydantic validates it
        detail=detail,
        session=session,
        tmux_session=SESSION,
    )


class Seen:
    """What the fake service was asked: positional and keyword arguments, per call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @property
    def args(self) -> tuple[Any, ...]:
        assert len(self.calls) == 1, f"expected exactly one call, saw {len(self.calls)}"
        return self.calls[0][0]

    @property
    def kwargs(self) -> dict[str, Any]:
        assert len(self.calls) == 1, f"expected exactly one call, saw {len(self.calls)}"
        return self.calls[0][1]


def _install(monkeypatch: pytest.MonkeyPatch, name: str, outcome: object) -> Seen:
    """Make ``fleet_service.<name>`` return ``outcome`` (or raise it) and record the call."""
    seen = Seen()

    def fake(*args: Any, **kwargs: Any) -> Any:
        seen.calls.append((args, dict(kwargs)))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    # The dotted form resolves ``fleet_service`` on the CLI module — the very
    # binding the commands call through — rather than on the services package.
    monkeypatch.setattr(f"aisquare.cli.fleet.fleet_service.{name}", fake)
    return seen


@pytest.fixture
def resolved(monkeypatch: pytest.MonkeyPatch) -> Seen:
    """Every ``--project`` reference resolves to ``PROJECT``; the ref is recorded."""
    return _install(monkeypatch, "resolve_project", PROJECT)


# ── spawn ────────────────────────────────────────────────────────────────────


def test_spawn_prints_the_receipt_with_the_label_the_fleet_used(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = SpawnReceipt(
        agent=_agent("coder-auth"), asked_label="coder-auth", tmux_session=SESSION
    )
    _install(monkeypatch, "spawn", receipt)

    result = runner.invoke(app, ["fleet", "spawn", "coder", "--label", "coder-auth"])

    assert result.exit_code == 0, result.output
    line = _plain(result.stdout)
    assert "✓ spawned coder-auth (agt_coderauth) → asq-amber-otter %7" in line
    # Honoured label: the receipt must NOT claim it was changed.
    assert "asked:" not in line


def test_spawn_says_which_label_was_asked_when_the_fleet_suffixed_it(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = SpawnReceipt(
        agent=_agent("coder-auth-2"),
        asked_label="coder-auth",
        tmux_session=SESSION,
        notes=["label coder-auth is held by a live agent; used coder-auth-2"],
    )
    _install(monkeypatch, "spawn", receipt)

    result = runner.invoke(app, ["fleet", "spawn", "coder", "--label", "coder-auth"])

    assert result.exit_code == 0, result.output
    line = _plain(result.stdout)
    assert "✓ spawned coder-auth-2 (asked: coder-auth)" in line
    assert "⚠ label coder-auth is held by a live agent" in line


def test_spawn_marks_a_share_made_into_its_account_as_done_not_as_a_warning(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final gate NIT 3: '⚠ accounts: <folder> is trusted on plain claude — now on account 2
    too' read as a problem. A share that was made is a ✓ line; a note that something did
    not happen keeps its ⚠."""
    shared = "accounts: /work is trusted on plain claude — now on account 2 too"
    receipt = SpawnReceipt(
        agent=_agent("coder-1"),
        asked_label=None,
        tmux_session=SESSION,
        notes=[shared, "the agent exited before the prompt could be typed"],
        shared=[shared],
    )
    _install(monkeypatch, "spawn", receipt)

    result = runner.invoke(app, ["fleet", "spawn", "coder"])

    assert result.exit_code == 0, result.output
    line = _plain(result.stdout)
    assert f"✓ {shared}" in line and "⚠ accounts:" not in line
    assert "⚠ the agent exited before the prompt could be typed" in line


def test_spawn_with_no_asked_label_prints_no_asked_suffix(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = SpawnReceipt(agent=_agent("coder-1"), asked_label=None, tmux_session=SESSION)
    _install(monkeypatch, "spawn", receipt)

    result = runner.invoke(app, ["fleet", "spawn", "coder"])

    assert result.exit_code == 0, result.output
    assert "✓ spawned coder-1 " in _plain(result.stdout)
    assert "asked" not in result.stdout


def test_spawn_json_carries_the_documented_keys(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = SpawnReceipt(
        agent=_agent("coder-auth-2", task_id="tsk_01k9q8p3abcdef"),
        asked_label="coder-auth",
        tmux_session=SESSION,
        notes=["permission mode auto refused; fell back to acceptEdits"],
    )
    _install(monkeypatch, "spawn", receipt)

    result = runner.invoke(app, ["--json", "fleet", "spawn", "coder", "--label", "coder-auth"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "agent",
        "asked_label",
        "tmux_session",
        "notes",
        # How the first message went (P0-W1a): as restart and switch say it.
        "prompt_typed",
        "prompt_delivery",
        "prompt_acked",
    }
    assert payload["asked_label"] == "coder-auth"
    assert payload["tmux_session"] == SESSION
    assert payload["notes"] == receipt.notes
    agent = payload["agent"]
    assert agent["label"] == "coder-auth-2"
    assert {"id", "role", "pane_id", "session_id", "cwd", "worktree", "task_id"} <= set(agent)
    assert agent["created_at"].startswith("2026-08-28")  # datetimes serialised, not repr'd


def test_spawn_forwards_extra_arguments_to_the_agent(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _install(monkeypatch, "spawn", SpawnReceipt(_agent(), None, SESSION))

    result = runner.invoke(app, ["fleet", "spawn", "coder", "--model", "opus", "-p", "go"])

    assert result.exit_code == 0, result.output
    assert spawn.kwargs["agent_args"] == ["--model", "opus", "-p", "go"]
    assert spawn.args[1] == "coder"


def test_spawn_consumes_its_own_options_and_forwards_nothing_else(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for forwarding: every declared flag lands in its
    keyword, none of them leaks into ``agent_args``."""
    spawn = _install(monkeypatch, "spawn", SpawnReceipt(_agent(), None, SESSION))

    result = runner.invoke(
        app,
        [
            "fleet",
            "spawn",
            "tester",
            "--label",
            "tester-py311",
            "--task",
            "tsk_01k9",
            "--no-worktree",
            "--permission-mode",
            "acceptEdits",
            "--bin",
            "claude-nightly",
            "--prompt",
            "run the suite",
            "-P",
            "amber-otter",
            "--as",
            "mgr-session",
        ],
    )

    assert result.exit_code == 0, result.output
    assert resolved.args == ("amber-otter",)
    assert spawn.args == (PROJECT, "tester")
    assert spawn.kwargs == {
        "label": "tester-py311",
        "task_id": "tsk_01k9",
        "worktree": False,
        "permission_mode": "acceptEdits",
        "binary": "claude-nightly",
        "prompt": "run the suite",
        "agent_args": [],
        "spawned_by": "mgr-session",
        "account": None,
        "persona": None,
        "ack_wait": 45.0,  # [fleet] prompt_ack_seconds: the CLI waits for the first turn
        "board_context": None,
    }


def test_spawn_after_a_double_dash_everything_belongs_to_the_agent(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _install(monkeypatch, "spawn", SpawnReceipt(_agent(), None, SESSION))

    result = runner.invoke(app, ["fleet", "spawn", "coder", "--", "--label", "for-the-agent"])

    assert result.exit_code == 0, result.output
    assert spawn.kwargs["label"] is None
    assert spawn.kwargs["agent_args"] == ["--label", "for-the-agent"]


def test_spawn_every_default_is_a_default(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan §3.10: an omitted flag is ``None`` — "the role's default" — never a
    value the CLI made up; the acting session defaults to the user."""
    spawn = _install(monkeypatch, "spawn", SpawnReceipt(_agent(), None, SESSION))

    result = runner.invoke(app, ["fleet", "spawn", "coder"])

    assert result.exit_code == 0, result.output
    assert spawn.kwargs["worktree"] is None
    assert spawn.kwargs["permission_mode"] is None
    assert spawn.kwargs["binary"] is None
    assert spawn.kwargs["prompt"] is None
    assert spawn.kwargs["label"] is None
    assert spawn.kwargs["task_id"] is None
    assert spawn.kwargs["spawned_by"] == "user"
    assert resolved.args == (None,)  # no -P: the active project


def test_spawn_worktree_flag_is_three_valued(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = _install(monkeypatch, "spawn", SpawnReceipt(_agent(), None, SESSION))

    assert runner.invoke(app, ["fleet", "spawn", "coder", "--worktree"]).exit_code == 0
    assert spawn.calls[-1][1]["worktree"] is True
    assert runner.invoke(app, ["fleet", "spawn", "coder", "--no-worktree"]).exit_code == 0
    assert spawn.calls[-1][1]["worktree"] is False


def test_spawn_json_after_the_role_belongs_to_the_agent(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned, as ``launch`` pins it: on an arg-forwarding command the global
    flags after the role are the AGENT's. Lead with ``--json`` for JSON."""
    spawn = _install(monkeypatch, "spawn", SpawnReceipt(_agent(), None, SESSION))

    result = runner.invoke(app, ["fleet", "spawn", "coder", "--json"])

    assert result.exit_code == 0, result.output
    assert spawn.kwargs["agent_args"] == ["--json"]
    assert "✓ spawned" in result.stdout  # stayed human


def test_spawn_refusal_is_a_fleet_error_with_the_reason(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "spawn", FleetError("fleet is at max_agents_per_project (6 live)"))

    human = runner.invoke(app, ["fleet", "spawn", "coder"])
    assert human.exit_code == 1
    assert human.stdout == ""
    assert "✗ fleet is at max_agents_per_project (6 live)" in _plain(human.stderr)

    machine = runner.invoke(app, ["--json", "fleet", "spawn", "coder"])
    assert machine.exit_code == 1
    payload = json.loads(machine.stdout)
    assert payload == {
        "error": "fleet_error",
        "detail": "fleet is at max_agents_per_project (6 live)",
    }


# ── the error mapping, one code per exception, on every code ─────────────────


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (FleetUnavailable("tmux 3.1 found; the fleet needs 3.3+"), "fleet_unavailable"),
        (NoSuchProject("no project matches 'nope'"), "not_found"),
        (NoSuchAgent("no live agent 'coder-9' in api"), "no_such_agent"),
        (FleetError("label 'Bad.Label' is not valid"), "fleet_error"),
    ],
    ids=["unavailable", "no-project", "no-agent", "generic"],
)
def test_every_fleet_error_maps_to_its_code_with_detail(
    runner: CliRunner,
    resolved: Seen,
    monkeypatch: pytest.MonkeyPatch,
    exc: FleetError,
    code: str,
) -> None:
    _install(monkeypatch, "list_agents", exc)

    machine = runner.invoke(app, ["--json", "fleet", "ls"])
    assert machine.exit_code == 1, machine.output
    payload = json.loads(machine.stdout)
    assert payload["error"] == code
    assert payload["detail"] == str(exc)
    assert str(exc)  # the containment above must not degenerate on ""

    human = runner.invoke(app, ["fleet", "ls"])
    assert human.exit_code == 1
    assert human.stdout == ""
    assert f"✗ {exc}" in _plain(human.stderr)


def test_a_subclass_is_never_reported_as_the_generic_code(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the mapping's ORDER: every subclass is also a FleetError,
    so a mapping that checked the base first would call everything fleet_error."""
    _install(monkeypatch, "list_agents", NoSuchAgent("gone"))

    payload = json.loads(runner.invoke(app, ["--json", "fleet", "ls"]).stdout)

    assert payload["error"] == "no_such_agent"
    assert payload["error"] != "fleet_error"


def test_a_project_reference_that_matches_nothing_is_not_found_against_the_real_store(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one test here that does NOT fake the resolver: an unknown ``-P`` reaches
    the real store in the isolated home, and the real ``NoSuchProject`` is what
    the CLI maps. A fake raising NoSuchProject would only prove the fake."""
    list_agents = _install(monkeypatch, "list_agents", [])

    result = runner.invoke(app, ["--json", "fleet", "ls", "-P", "no-such-project"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_found"
    assert "no-such-project" in payload["detail"]
    assert list_agents.calls == []  # nothing past the resolver ran


def test_a_success_is_never_an_error_payload(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative half of the mapping: a service that answers gets no ``error``."""
    _install(monkeypatch, "list_agents", [_status(_agent())])

    result = runner.invoke(app, ["--json", "fleet", "ls"])

    assert result.exit_code == 0, result.output
    assert "error" not in json.loads(result.stdout)


# ── ls / status ──────────────────────────────────────────────────────────────


def test_ls_prints_the_session_header_and_one_row_per_agent(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = [
        _status(_agent("manager", "manager", pane="%1"), "waiting"),
        _status(_agent("coder-auth", worktree=True, pane="%7"), "working"),
        _status(_agent("tester-py311", "tester", pane="%9"), "attention", detail="permission"),
        _status(_agent("coder-old", ended=True, exit_status=1, pane="%3"), "exited"),
    ]
    _install(monkeypatch, "list_agents", agents)

    result = runner.invoke(app, ["fleet", "ls"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "api · amber-otter · asq-amber-otter" in out
    assert "manager manager ⏸ waiting %1" in out
    assert "coder-auth coder ▶ working (worktree) %7" in out
    assert "tester-py311 tester 🔔 NEEDS YOU permission %9" in out
    assert "coder-old coder 💤 exited(1) %3" in out


def test_ls_with_no_agents_says_how_to_start_one(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "list_agents", [])

    result = runner.invoke(app, ["fleet", "ls"])

    assert result.exit_code == 0, result.output
    assert "(no agents)" in result.stdout
    # Naming the project listed: alone, it spawned in the active one (#259, round 21).
    assert "aisquare fleet spawn --project amber-otter manager" in result.stdout


def test_ls_all_includes_ended_agents_and_plain_ls_does_not(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    list_agents = _install(monkeypatch, "list_agents", [])

    assert runner.invoke(app, ["fleet", "ls"]).exit_code == 0
    assert list_agents.calls[-1][1] == {"live_only": True}
    assert runner.invoke(app, ["fleet", "ls", "--all"]).exit_code == 0
    assert list_agents.calls[-1][1] == {"live_only": False}
    assert runner.invoke(app, ["fleet", "ls", "-a"]).exit_code == 0
    assert list_agents.calls[-1][1] == {"live_only": False}
    assert all(args == (PROJECT,) for args, _ in list_agents.calls)


def test_status_is_ls_of_the_live_agents(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    list_agents = _install(monkeypatch, "list_agents", [_status(_agent(), "working")])

    result = runner.invoke(app, ["fleet", "status", "-P", "amber-otter"])

    assert result.exit_code == 0, result.output
    assert list_agents.args == (PROJECT,) and list_agents.kwargs == {"live_only": True}
    assert resolved.args == ("amber-otter",)
    assert "coder-auth" in result.stdout


def test_ls_json_carries_the_project_and_every_agent_with_its_state(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = [
        _status(_agent("manager", "manager", pane="%1"), "waiting", with_session=True),
        _status(_agent("coder-old", ended=True, exit_status=0), "exited", detail="exit 0"),
    ]
    _install(monkeypatch, "list_agents", agents)

    result = runner.invoke(app, ["--json", "fleet", "ls", "--all"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"project", "name", "codename", "tmux_session", "agents"}
    assert payload["name"] == "api"
    assert payload["codename"] == "amber-otter"
    assert payload["tmux_session"] == SESSION
    assert payload["project"]["id"] == PROJECT.id
    assert [a["agent"]["label"] for a in payload["agents"]] == ["manager", "coder-old"]
    assert [a["state"] for a in payload["agents"]] == ["waiting", "exited"]
    assert payload["agents"][0]["session"]["state"] == "waiting"  # nested model serialised
    assert payload["agents"][1]["detail"] == "exit 0"
    assert payload["agents"][1]["agent"]["exit_status"] == 0


def test_ls_json_names_where_each_agents_server_is_asked(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`socket_at`, which 0.8.0 added to every agent without a CHANGELOG line (0.8.1 has
    it): the path its tmux server is asked at (`tmux -S`), or null where it is asked by
    its socket's name (`tmux -L`). The keys each agent has are pinned with it, so one
    added or dropped is a change the CHANGELOG has to name: `held` since (review of #259,
    round 1)."""
    at = Path("/tmp/tmux-1000/asq")
    agents = [
        _status(_agent("coder-auth"), "working").model_copy(update={"socket_at": at}),
        _status(_agent("manager", "manager", pane="%1"), "waiting"),
    ]
    _install(monkeypatch, "list_agents", agents)

    result = runner.invoke(app, ["--json", "fleet", "ls"])

    assert result.exit_code == 0, result.output
    listed = json.loads(result.stdout)["agents"]
    assert [agent["socket_at"] for agent in listed] == [str(at), None]
    assert all(
        set(agent) == {"agent", "state", "detail", "session", "tmux_session", "socket_at", "held"}
        for agent in listed
    ), listed


def test_a_project_without_a_codename_has_no_session_yet(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codenames are lazy (§5.7): before the first fleet contact there is none,
    and the CLI must say so with ``null`` rather than invent ``asq-None``."""
    _install(monkeypatch, "resolve_project", UNNAMED)
    _install(monkeypatch, "list_agents", [])

    machine = runner.invoke(app, ["--json", "fleet", "ls"])
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert payload["codename"] is None
    assert payload["tmux_session"] is None

    human = runner.invoke(app, ["fleet", "ls"])
    assert human.exit_code == 0, human.output
    first = human.stdout.splitlines()[0]
    assert first.strip() == "tool"
    assert "None" not in human.stdout


# ── tell ─────────────────────────────────────────────────────────────────────


def test_tell_reports_how_the_message_travelled(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    tell = _install(monkeypatch, "tell", TellResult(typed="yes", how="typed into pane %7"))

    result = runner.invoke(app, ["fleet", "tell", "coder-auth", "ship it", "--as", "mgr"])

    assert result.exit_code == 0, result.output
    assert "✓ coder-auth: typed into pane %7" in _plain(result.stdout)
    assert tell.args == (PROJECT, "coder-auth", "ship it")
    assert tell.kwargs == {"sender": "mgr"}


def test_tell_to_a_busy_agent_is_marked_as_a_note_not_a_delivery(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    tell = _install(
        monkeypatch, "tell", TellResult(typed="no", how="agent is working; filed a board note")
    )

    result = runner.invoke(app, ["fleet", "tell", "coder-auth", "ship it"])

    assert result.exit_code == 0, result.output
    assert "→ coder-auth: agent is working; filed a board note" in _plain(result.stdout)
    assert "✓" not in result.stdout
    assert tell.kwargs == {"sender": None}  # no --as: the user spoke


@pytest.mark.parametrize("typed", ["unsent", "maybe"])
def test_tell_neither_typed_nor_noted_is_not_marked_as_a_note(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, typed: Typed
) -> None:
    """Review of #259, round 12, 2/9: text pasted at its prompt with its Enter refused, or one
    tmux did not answer in time with, printed `→`, which says "noted", where no note was
    filed: the line says what is at its prompt."""
    how = "pasted it at its prompt, but tmux could not press Enter (refused)"
    _install(monkeypatch, "tell", TellResult(typed=typed, how=how))

    result = runner.invoke(app, ["fleet", "tell", "coder-auth", "ship it"])

    assert result.exit_code == 0, result.output
    assert f"… coder-auth: {how}" in _plain(result.stdout)
    assert "→" not in result.stdout and "✓" not in result.stdout


@pytest.mark.parametrize("typed", ["yes", "no", "unsent", "maybe"])
def test_tell_json(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, typed: Typed
) -> None:
    """``typed`` is in it: a tell tmux did not answer in time read ``delivered: false`` as a
    refusal does, and a script that retried on it typed the text twice (review of #259,
    round 11, 9/13)."""
    _install(monkeypatch, "tell", TellResult(typed=typed, how="filed a board note"))

    result = runner.invoke(app, ["--json", "fleet", "tell", "coder-auth", "ship it"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "label": "coder-auth",
        "delivered": typed == "yes",
        "typed": typed,
        "how": "filed a board note",
    }


def test_tell_an_unknown_label_is_no_such_agent(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "tell", NoSuchAgent("no live agent 'coder-9' in api"))

    result = runner.invoke(app, ["--json", "fleet", "tell", "coder-9", "hi"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "no_such_agent"


# ── stop ─────────────────────────────────────────────────────────────────────


def _released_task() -> TeamTask:
    """A task a stop returned to the pool, as ``StopReceipt.released`` carries it."""
    now = datetime.now(tz=UTC)
    return TeamTask(
        id="tsk_01x",
        project_id=PROJECT.id,
        key="k",
        title="the task it held",
        status="todo",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("command", ["shutdown", "reap"])
def test_all_and_project_together_are_refused(
    command: str, runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. ``--all`` won silently: ``fleet shutdown --project alpha
    --all --yes``, meant as alpha, took every project's fleet down with nothing
    saying the project flag had been discarded. The two name different scopes;
    both at once is refused before the service is asked anything."""
    service = _install(monkeypatch, command, RuntimeError("the service must not be called"))

    confirm = ["--yes"] if command == "shutdown" else []  # reap asks nothing
    result = runner.invoke(app, ["fleet", command, "--project", "alpha", "--all", *confirm])

    assert result.exit_code != 0
    assert "--all and --project conflict" in _plain(result.output)
    assert service.calls == [], "refused before the service was asked"


def test_shutdown_with_stuck_claims_says_so_in_the_header_and_the_exit_code(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. ``release_failures`` was printed as a ⚠ line under a
    ``✓ fleet shut down`` header and exit 0, so a cutover script gating on the
    code proceeded while two tasks stayed claimed by dead sessions. Every row
    is down — not PARTLY — but the header counts the refusals and the code
    is non-zero."""
    report = ShutdownReport(
        stopped=[_agent("coder-auth", ended=True, exit_status=0)],
        sessions_killed=[f"asq:{SESSION}"],
        release_failures=[
            "coder-auth: could not be released (OperationalError: database is locked)"
        ],
    )
    _install(monkeypatch, "shutdown", report)

    result = runner.invoke(app, ["fleet", "shutdown", "--yes", "--force"])

    assert result.exit_code == 1
    out = _plain(result.stdout)
    assert (
        "⚠ fleet shut down: 1 stopped, 0 recorded lost, 0 left live, 1 claim release(s) refused"
        in out
    )
    assert "PARTLY" not in out, "every row is down; the claims are what is stuck"
    assert (
        "⚠ claims of coder-auth: could not be released (OperationalError: database is locked)"
        in out
    )


def test_an_interrupted_shutdown_prints_how_far_it_got_and_exits_130(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5. Ctrl-C used to propagate past the report to Click's `Aborted!`:
    rows already ended and claims already released, and the operator told
    nothing. The interrupt carries the report; the code says it was not the
    whole fleet."""
    report = ShutdownReport(
        stopped=[_agent("manager", "manager", ended=True, exit_status=0)],
        claims_released=["tsk_01x"],
        interrupted=(
            "interrupted while stopping coder-auth; the rest of the fleet was left as it was"
        ),
    )
    _install(monkeypatch, "shutdown", fleet_service.FleetInterrupted(report))

    result = runner.invoke(app, ["fleet", "shutdown", "--yes", "--force"])

    assert result.exit_code == 130
    out = _plain(result.stdout)
    assert "⚠ fleet PARTLY shut down: 1 stopped" in out
    assert "💤 manager (exit 0)" in out, "the row already down is named"
    assert "🔓 1 claimed task(s) released back to the board" in out
    assert "interrupted while stopping coder-auth" in out
    assert "`aisquare fleet ls --project amber-otter --all` shows what is still running" in out


def test_stop_confirms_and_passes_force_through(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = _install(
        monkeypatch, "stop", StopReceipt(_agent("coder-auth", ended=True, exit_status=0), [])
    )

    gentle = runner.invoke(app, ["fleet", "stop", "coder-auth"])
    assert gentle.exit_code == 0, gentle.output
    assert "✓ stopped coder-auth (agt_coderauth)" in _plain(gentle.stdout)
    assert stop.calls[-1] == ((PROJECT, "coder-auth"), {"force": False, "actor": "user"})

    forced = runner.invoke(app, ["fleet", "stop", "coder-auth", "--force"])
    assert forced.exit_code == 0, forced.output
    assert stop.calls[-1] == ((PROJECT, "coder-auth"), {"force": True, "actor": "user"})


@pytest.mark.parametrize("worktree", [True, False], ids=["worktree", "no-worktree"])
def test_stop_says_reap_removes_the_worktree_of_an_agent_that_had_one(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, worktree: bool
) -> None:
    """A stopped agent's worktree stays until its work is pushed and ``reap`` removes it;
    nothing said so, so trees piled up (one night: 15 of them, 1.2 GB). The reap names the
    stopped agent's project: alone, it walked the active project's rows, so after ``stop
    --project web`` from another project's directory web's tree stayed (review of #259,
    round 21)."""
    agent = _agent("coder-auth", worktree=worktree, ended=True, exit_status=0)
    _install(monkeypatch, "stop", StopReceipt(agent, []))
    said = runner.invoke(app, ["fleet", "stop", "--project", "web", "coder-auth"]).stdout
    hint = (
        "· run `aisquare fleet reap --project amber-otter` to remove its worktree once its "
        "work is pushed"
    )
    assert (hint in " ".join(said.split())) is worktree, said
    assert resolved.calls[-1][0] == ("web",)


def test_stop_json_returns_the_agent_row(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = _released_task()
    _install(
        monkeypatch,
        "stop",
        StopReceipt(_agent("coder-auth", ended=True, exit_status=0), [released]),
    )

    result = runner.invoke(app, ["--json", "fleet", "stop", "coder-auth"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"agent", "claims_released", "release_failed"}
    assert payload["agent"]["label"] == "coder-auth"
    assert payload["agent"]["ended_at"] is not None
    assert payload["agent"]["exit_status"] == 0
    assert payload["claims_released"] == ["tsk_01x"], "what the stop returned to the pool"
    assert payload["release_failed"] is None


def test_stop_names_the_claims_it_released(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work a stop returns to the pool is the one thing the next agent
    inherits from this one, so it is named, not counted (review of #203)."""
    released = _released_task()
    _install(
        monkeypatch,
        "stop",
        StopReceipt(_agent("coder-auth", ended=True, exit_status=0), [released]),
    )

    result = runner.invoke(app, ["fleet", "stop", "coder-auth"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "🔓 1 claimed task(s) released back to the board" in out, (
        "the one line every command uses"
    )
    assert "· the task it held (tsk_01x)" in out, "and the receipt's tasks named under it"


def test_stop_says_when_the_release_was_refused(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is down; the claims that stayed with the ended session are the
    operator's to know about, not a swallowed exception's (review of the fold)."""
    refused = (
        "could not be released (OperationalError: database is locked) — they stay with the "
        "ended session until the lease lapses"
    )
    _install(
        monkeypatch,
        "stop",
        StopReceipt(_agent("coder-auth", ended=True, exit_status=0), [], release_failed=refused),
    )

    result = runner.invoke(app, ["fleet", "stop", "coder-auth"])

    assert result.exit_code == 1, "one contract with shutdown: a stuck claim is not a clean stop"
    out = _plain(result.stdout)
    assert "✓ stopped coder-auth" in out
    assert f"⚠ claims: {refused}" in out

    as_json = runner.invoke(app, ["--json", "fleet", "stop", "coder-auth"])
    assert as_json.exit_code == 1, "and the two output modes agree"
    assert json.loads(as_json.stdout)["release_failed"] == refused


# ── restart --all / stop --all ───────────────────────────────────────────────

OTHER = ProjectInfo(id="prj_03ghi", root=Path("/home/me/work/web"), codename="ruby-fox")


def _bulk(monkeypatch: pytest.MonkeyPatch, report: BulkReport) -> Seen:
    """``fleet_service.bulk`` handing each of ``report``'s receipts to ``on_receipt`` as it
    goes, as the real one does, then returning the report; the call is recorded."""
    seen = Seen()

    def fake(*args: Any, **kwargs: Any) -> BulkReport:
        seen.calls.append((args, dict(kwargs)))
        on_receipt = kwargs.get("on_receipt")
        for receipt in report.receipts:
            if on_receipt is not None:
                on_receipt(receipt)
        return report

    monkeypatch.setattr("aisquare.cli.fleet.fleet_service.bulk", fake)
    return seen


def _lines(stdout: str) -> list[str]:
    """``stdout``'s lines without escape codes, their columns kept (not :func:`_plain`)."""
    return [line.rstrip() for line in ANSI.sub("", stdout).splitlines()]


def _restarted(agent: FleetAgent, project: ProjectInfo = PROJECT) -> BulkReceipt:
    started = agent.model_copy(update={"id": f"{agent.id}2", "pane_id": "%20"})
    receipt = RestartReceipt(
        replaced=agent,
        started=started,
        resumed=True,
        was_running=True,
        tmux_session=SESSION,
        notes=["launch replayed"],
    )
    return BulkReceipt(project, agent, "done", result=receipt)


def _mixed() -> BulkReport:
    """Two projects: a manager restarted and a coder whose task is done in one, a coder
    that failed in the other."""
    web_coder = _agent("coder-web", project=OTHER)
    return BulkReport(
        "restart",
        False,
        (
            _restarted(_agent("manager", "manager")),
            BulkReceipt(PROJECT, _agent("coder-auth"), "skipped", "task is done"),
            BulkReceipt(OTHER, web_coder, "failed", "cannot restart 'coder-web': tmux said no"),
        ),
    )


@pytest.mark.parametrize(
    ("argv", "action", "managers_only", "acting"),
    [
        (["fleet", "restart", "--all"], "restart", False, None),
        (["fleet", "restart", "--all", "--managers", "--as", "sess-1"], "restart", True, "sess-1"),
        (["fleet", "stop", "--all"], "stop", False, None),
        (["fleet", "stop", "--all", "--managers"], "stop", True, None),
    ],
    ids=lambda value: " ".join(value) if isinstance(value, list) else None,
)
def test_restart_and_stop_all_ask_the_one_bulk_service(
    runner: CliRunner,
    resolved: Seen,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    action: str,
    managers_only: bool,
    acting: str | None,
) -> None:
    bulk = _bulk(monkeypatch, BulkReport(action, managers_only, ()))  # type: ignore[arg-type]
    _install(monkeypatch, action, RuntimeError("the one-agent path must not be called"))

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert bulk.args == (action,)
    assert bulk.kwargs["managers_only"] is managers_only
    assert bulk.kwargs["acting_session"] == acting
    assert resolved.calls == [], "every project: no --project is resolved"


def test_all_prints_one_line_per_agent_by_project_then_the_count_and_exits_1_on_a_failure(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bulk(monkeypatch, _mixed())

    result = runner.invoke(app, ["fleet", "restart", "--all"])

    assert result.exit_code == 1, "one agent failed"
    assert _lines(result.stdout) == [
        "api",
        f"  ✓ {'manager':<24} {'manager':<10} stopped and restarted — resumed its session",
        "      · launch replayed",
        f"  · {'coder-auth':<24} {'coder':<10} skipped — task is done",
        "web",
        f"  ✗ {'coder-web':<24} {'coder':<10} failed — cannot restart 'coder-web': tmux said no",
        "restarted 1 · skipped 1 · failed 1",
    ]


def test_all_prints_a_restart_s_notes_as_fleet_restart_does_and_marks_what_failed(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: ``--all`` printed a restart's failures alone, and dropped
    the account ladder's note that one ``fleet restart`` prints."""
    agent = _agent("coder-auth")
    typed = "the agent exited before the prompt could be typed"
    receipt = RestartReceipt(
        replaced=agent,
        started=agent.model_copy(update={"id": f"{agent.id}2"}),
        resumed=False,
        was_running=False,
        tmux_session=SESSION,
        notes=["accounts: slot 2 is at its limit; used slot 3", typed],
        failures=[typed],
    )
    report = BulkReport("restart", False, (BulkReceipt(PROJECT, agent, "done", result=receipt),))
    _bulk(monkeypatch, report)

    result = runner.invoke(app, ["fleet", "restart", "--all"])

    assert _lines(result.stdout)[2:4] == [
        "      · accounts: slot 2 is at its limit; used slot 3",
        f"      ⚠ {typed}",
    ]


def test_all_exits_0_when_nothing_failed_and_says_what_each_stop_left(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each stop's lines say what ``fleet stop`` says for one agent: the reap that removes
    the worktree it left, and the claims it put back, by title. ``stop --all`` dropped
    both, and printed one released count at the end (review of #266, round 3)."""
    ended = _agent("coder-auth", ended=True, exit_status=0, worktree=True)
    stopped = StopReceipt(ended, [_released_task()])
    _bulk(
        monkeypatch,
        BulkReport(
            "stop",
            False,
            (
                BulkReceipt(PROJECT, _agent("coder-auth"), "done", result=stopped),
                BulkReceipt(PROJECT, _agent("manager", "manager"), "skipped", "this session"),
            ),
        ),
    )

    result = runner.invoke(app, ["fleet", "stop", "--all"])

    assert result.exit_code == 0, result.output
    assert _lines(result.stdout) == [
        "api",
        f"  ✓ {'coder-auth':<24} {'coder':<10} stopped",
        "      · run `aisquare fleet reap --project amber-otter` to remove its worktree once"
        " its work is pushed",
        "      🔓 1 claimed task(s) released back to the board",
        "         · the task it held (tsk_01x)",
        f"  · {'manager':<24} {'manager':<10} skipped — this session",
        "stopped 1 · skipped 1 · failed 0",
        "  🔓 1 claimed task(s) released back to the board",
    ]


def test_a_stop_whose_claims_stayed_behind_exits_1_as_fleet_stop_does(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = StopReceipt(_agent("coder-auth", ended=True), [], release_failed="database is locked")
    report = BulkReport("stop", False, (BulkReceipt(PROJECT, _agent(), "done", result=stuck),))
    _bulk(monkeypatch, report)

    result = runner.invoke(app, ["fleet", "stop", "--all"])

    assert result.exit_code == 1
    assert "⚠ claims: database is locked" in _plain(result.stdout)


def test_all_json_gives_every_receipt_and_the_same_exit_status(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk = _bulk(monkeypatch, _mixed())

    result = runner.invoke(app, ["--json", "fleet", "restart", "--all"])

    assert result.exit_code == 1
    assert bulk.kwargs["on_receipt"] is None, "JSON is one object at the end, nothing before"
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "action", "managers_only", "receipts", "unread", "summary", "ok", "interrupted"
    }  # fmt: skip
    assert payload["interrupted"] is None
    assert payload["action"] == "restart" and payload["managers_only"] is False
    assert payload["summary"] == {"done": 1, "skipped": 1, "failed": 1} and not payload["ok"]
    done, skipped, failed = payload["receipts"]
    assert done["project"] == {"id": PROJECT.id, "name": "api", "codename": "amber-otter"}
    assert done["agent"]["id"] == "agt_manager" and done["outcome"] == "done"
    assert done["started"]["id"] == "agt_manager2" and done["how"] == "resumed its session"
    assert done["was_running"] is True and done["notes"] == ["launch replayed"]
    assert (skipped["outcome"], skipped["why"]) == ("skipped", "task is done")
    assert failed["project"]["name"] == "web" and failed["outcome"] == "failed"
    assert failed["why"] == "cannot restart 'coder-web': tmux said no"


@pytest.mark.parametrize("action", ["restart", "stop"])
def test_each_all_receipt_s_json_is_the_one_agent_command_s(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Review of #266, round 3: ``--all`` built each receipt's JSON a second time, and the copy
    had drifted. A restart had no ``replaced``, ``prompt_typed`` or ``tmux_session``, so a
    script written against ``fleet restart --json`` broke on ``--all``."""
    if action == "restart":
        bulk_receipt = _restarted(_agent("manager", "manager"))
    else:
        ended = _agent("coder-auth", ended=True, exit_status=0, worktree=True)
        stopped = StopReceipt(ended, [_released_task()])
        bulk_receipt = BulkReceipt(PROJECT, _agent("coder-auth"), "done", result=stopped)
    _install(monkeypatch, action, bulk_receipt.result)
    _bulk(monkeypatch, BulkReport(action, False, (bulk_receipt,)))  # type: ignore[arg-type]
    label = bulk_receipt.agent.label

    one = json.loads(runner.invoke(app, ["--json", "fleet", action, label]).stdout)
    every = json.loads(runner.invoke(app, ["--json", "fleet", action, "--all"]).stdout)

    [receipt] = every["receipts"]
    assert {key: receipt[key] for key in one} == one
    assert set(receipt) - set(one) == {"project", "outcome", "why"} | (
        {"agent"} if action == "restart" else set()
    )


def _interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fleet_service.bulk`` interrupted on its second agent, the first restarted and told."""
    done = _restarted(_agent("manager", "manager"))
    said = "interrupted while restarting coder-auth (api); the rest were left as they were"
    report = BulkReport("restart", False, (done,), interrupted=said)

    def fake(*args: Any, **kwargs: Any) -> BulkReport:
        if kwargs.get("on_receipt") is not None:
            kwargs["on_receipt"](done)
        raise fleet_service.BulkInterrupted(report)

    monkeypatch.setattr("aisquare.cli.fleet.fleet_service.bulk", fake)


def test_an_interrupted_all_in_json_prints_how_far_it_got_and_exits_130(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #266, round 2: a Ctrl-C part-way through printed nothing under --json, so
    a script had no record of the agents already restarted."""
    _interrupted(monkeypatch)

    result = runner.invoke(app, ["--json", "fleet", "restart", "--all"])

    assert result.exit_code == 130
    payload = json.loads(result.stdout)
    assert [(r["agent"]["label"], r["outcome"]) for r in payload["receipts"]] == [
        ("manager", "done")
    ]
    assert payload["interrupted"].startswith("interrupted while restarting coder-auth (api)")
    assert payload["ok"] is False


def test_an_interrupted_all_says_so_under_the_count_and_exits_130(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _interrupted(monkeypatch)

    result = runner.invoke(app, ["fleet", "restart", "--all"])

    assert result.exit_code == 130
    assert _lines(result.stdout)[1:] == [
        f"  ✓ {'manager':<24} {'manager':<10} stopped and restarted — resumed its session",
        "      · launch replayed",
        "restarted 1 · skipped 0 · failed 0",
        "⚠ interrupted while restarting coder-auth (api); the rest were left as they were",
    ]


def test_a_project_whose_agents_could_not_be_read_is_named_and_exits_1(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bulk(monkeypatch, BulkReport("stop", False, (), unread=((OTHER, "the store is locked"),)))

    result = runner.invoke(app, ["fleet", "stop", "--all"])

    assert result.exit_code == 1
    out = _plain(result.stdout)
    assert "⚠ web: its agents could not be read — the store is locked" in out
    assert "stopped 0 · skipped 0 · failed 0 · 1 project(s) not read" in out


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["fleet", "restart", "--all", "--managers"], "nothing to restart: no manager is running"),
        (["fleet", "stop", "--all"], "nothing to stop: no agent is running in any project"),
    ],
)
def test_all_with_nothing_running_says_so(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, argv: list[str], said: str
) -> None:
    _bulk(monkeypatch, BulkReport(argv[1], "--managers" in argv, ()))  # type: ignore[arg-type]

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert said in _plain(result.stdout)


@pytest.mark.parametrize(
    ("argv", "said"),
    [
        (["fleet", "restart", "--all", "coder-1"], "--all and a label conflict"),
        (["fleet", "stop", "coder-1", "--all"], "--all and a label conflict"),
        (["fleet", "restart", "--all", "--project", "alpha"], "--all and --project conflict"),
        (["fleet", "stop", "--all", "-P", "alpha"], "--all and --project conflict"),
        (["fleet", "restart", "--managers"], "--managers goes with --all"),
        (["fleet", "stop", "coder-1", "--managers"], "--managers goes with --all"),
        (["fleet", "restart", "--all", "--fresh"], "--fresh is for one agent"),
        (["fleet", "restart", "--all", "--permission-mode", "auto"], "--permission-mode is for"),
        (["fleet", "stop", "--all", "--force"], "--force is for one agent"),
        (["fleet", "stop"], "Missing argument 'label'"),
        (["fleet", "restart"], "Missing argument 'label'"),
    ],
    ids=lambda value: " ".join(value) if isinstance(value, list) else None,
)
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_the_usage_errors_of_all_are_refused_before_anything_is_asked(
    runner: CliRunner,
    resolved: Seen,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    said: str,
    as_json: bool,
) -> None:
    services = [
        _install(monkeypatch, name, RuntimeError("the service must not be called"))
        for name in ("bulk", "restart", "stop")
    ]

    result = runner.invoke(app, ["--json", *argv] if as_json else argv)

    assert result.exit_code == 2, result.output
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["error"] == "usage" and said in payload["message"]
    else:
        assert said in " ".join(_plain(result.output).replace("│", " ").split())
    assert all(service.calls == [] for service in services)
    assert resolved.calls == []


def test_click_s_usage_error_is_reached_in_one_place() -> None:
    """Review of #266, round 3: ``cli.fleet`` reached click's ``UsageError`` through the
    bases of ``typer.BadParameter`` a second time, beside ``cli.global_flags``. A change to
    those bases would have had to find both, and the copy missed would raise the wrong
    class: the ``Missing argument 'label'.`` above would then be a traceback."""
    import aisquare

    src = Path(aisquare.__file__).parent
    reaching = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "BadParameter.__bases__" in path.read_text(encoding="utf-8")
    )
    assert reaching == ["cli/common.py"]


@pytest.mark.parametrize("path", sorted(NEEDED_UNLESS), ids=" ".join)
def test_an_argument_needed_unless_a_flag_stands_in_is_refused_without_either(
    runner: CliRunner, path: tuple[str, ...]
) -> None:
    """``tests/cli_tree.NEEDED_UNLESS`` is what lets the sweeps run these commands' own
    code; an entry the command no longer needs would fill a word it reads as something
    else."""
    argument, flag = NEEDED_UNLESS[path]
    command: Any = root_command()
    for word in path:  # a path of any depth (`project group parent`)
        command = command.commands[word]
    param = next(p for p in command.params if p.name == argument)
    assert not param.required and any(flag in p.opts for p in command.params)

    # Every other required argument given (`project group parent <group>`), this one not.
    others = [
        _placeholder(p)
        for p in command.params
        if p.required and p.param_type_name == "argument" and p.name != argument
    ]
    result = runner.invoke(app, ["--json", *path, *others])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["message"] == f"Missing argument '{argument}'."


def test_restart_and_stop_of_one_label_are_as_they_were(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    bulk = _install(monkeypatch, "bulk", RuntimeError("--all was not asked"))
    agent = _agent()
    restart = _install(
        monkeypatch,
        "restart",
        RestartReceipt(
            replaced=agent, started=agent, resumed=True, was_running=False, tmux_session=SESSION
        ),
    )
    stop = _install(monkeypatch, "stop", StopReceipt(_agent(ended=True), []))

    assert runner.invoke(app, ["fleet", "restart", "coder-auth"]).exit_code == 0
    assert runner.invoke(app, ["fleet", "stop", "coder-auth"]).exit_code == 0

    assert restart.calls == [
        (
            (PROJECT, "coder-auth"),
            {"fresh": False, "spawned_by": "user", "permission_mode": None, "ack_wait": 45.0},
        )
    ]
    assert stop.calls == [((PROJECT, "coder-auth"), {"force": False, "actor": "user"})]
    assert bulk.calls == []


# ── shutdown ─────────────────────────────────────────────────────────────────


def _plan(**overrides: object) -> ShutdownPlan:
    base: dict[str, object] = {
        "projects": [PROJECT],
        "agents": [_agent("coder-auth"), _agent("manager", "manager", pane="%9")],
        "sessions": [f"asq:{SESSION}"],
        "absent_sockets": [],
    }
    return ShutdownPlan(**{**base, **overrides})  # type: ignore[arg-type]


def _report(**overrides: object) -> ShutdownReport:
    """A report whose groups can COEXIST — the shape the service actually produces.

    The first draft's fixture could not: it put ``manager`` in ``recorded`` while
    ``servers_killed`` named the very socket that row's ``tmux_socket`` points at,
    so the CLI's "its server was not running" was pinned against a state the
    service cannot reach. Reasons come from the service now, so a fixture cannot
    invent a cause.
    """
    return ShutdownReport(**overrides)  # type: ignore[arg-type]


def test_shutdown_prints_the_plan_and_is_a_dry_run_off_a_terminal(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It ends running work, so it is confirmable the way ``project prune`` is."""
    plan = _install(monkeypatch, "shutdown_plan", _plan())
    shutdown = _install(monkeypatch, "shutdown", _report())

    result = runner.invoke(app, ["fleet", "shutdown"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "about to shut down 2 agent(s) and kill 1 fleet session(s)" in out
    assert "💤 coder-auth · amber-otter — stopped (%7)" in out
    assert f"⌧ tmux session asq:{SESSION}" in out
    assert "dry run: nothing stopped" in out and "--yes" in out
    assert shutdown.calls == [], "the plan must not shut anything down"
    assert plan.args == (PROJECT,)


def test_shutdown_asks_at_a_terminal_and_stops_nothing_when_the_answer_is_no(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "shutdown_plan", _plan())
    shutdown = _install(monkeypatch, "shutdown", _report())
    monkeypatch.setattr("aisquare.cli.fleet._stdin_is_a_terminal", lambda: True)
    asked: list[tuple[str, bool]] = []

    def confirm(text: str, *, default: bool = True, **kwargs: object) -> bool:
        asked.append((text, default))
        return False

    monkeypatch.setattr("aisquare.cli.fleet.typer.confirm", confirm)

    refused = runner.invoke(app, ["fleet", "shutdown"])

    assert refused.exit_code == 0, refused.output
    assert asked == [("Shut down 2 agents and 1 tmux session?", False)], (
        "both numbers, and the default is NOT to do it"
    )
    assert "nothing stopped" in _plain(refused.stdout)
    assert shutdown.calls == []

    monkeypatch.setattr("aisquare.cli.fleet.typer.confirm", lambda *a, **k: True)
    agreed = runner.invoke(app, ["fleet", "shutdown"])

    assert agreed.exit_code == 0, agreed.output
    assert shutdown.calls[-1] == (
        (PROJECT,),
        {"force": False, "forget": False, "actor": "user"},
    )


def test_shutdown_asks_about_the_sessions_when_no_agent_is_live(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 8 of #203. A plan with no live agent and one leftover session — the
    ordinary shape after a clean stop pass — asked "Shut down 0 agents?" while
    the real effect, killing the session, sat only in the lines above. The
    destructive prompt names what the run is about to do."""
    plan = _plan()
    plan = type(plan)(
        projects=plan.projects, agents=[], sessions=["asq:asq-ruby-fox"], absent_sockets=[]
    )
    _install(monkeypatch, "shutdown_plan", plan)
    _install(monkeypatch, "shutdown", _report())
    monkeypatch.setattr("aisquare.cli.fleet._stdin_is_a_terminal", lambda: True)
    asked: list[str] = []

    def confirm(text: str, *, default: bool = True, **kwargs: object) -> bool:
        asked.append(text)
        return False

    monkeypatch.setattr("aisquare.cli.fleet.typer.confirm", confirm)
    runner.invoke(app, ["fleet", "shutdown"])
    assert asked == ["Shut down 1 tmux session?"]


def test_shutdown_asks_about_the_rows_it_ends_not_the_ones_it_leaves_live(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 28 of #257: a held row is printed as left live, but the question still asked
    "Shut down 2 agents", counting it; the run then left it live and exited 1. The plan and
    the question count what the run ends, and say how many it leaves live."""
    held = {"agt_manager": "its server's socket /tmp/tmux-1000/asq is gone"}
    _install(monkeypatch, "shutdown_plan", _plan(held=held))
    _install(monkeypatch, "shutdown", _report())
    monkeypatch.setattr("aisquare.cli.fleet._stdin_is_a_terminal", lambda: True)
    asked: list[str] = []

    def confirm(text: str, *, default: bool = True, **kwargs: object) -> bool:
        asked.append(text)
        return False

    monkeypatch.setattr("aisquare.cli.fleet.typer.confirm", confirm)
    result = runner.invoke(app, ["fleet", "shutdown"])

    out = _plain(result.stdout)
    assert "about to shut down 1 agent(s), leave 1 live and kill 1 fleet session(s)" in out
    assert asked == ["Shut down 1 agent and 1 tmux session, leaving 1 live?"]

    every = {"agt_coderauth": "gone", "agt_manager": "gone"}
    _install(monkeypatch, "shutdown_plan", _plan(held=every, sessions=[]))
    nothing = runner.invoke(app, ["fleet", "shutdown"])

    assert nothing.exit_code == 1 and len(asked) == 1, "nothing to ask: the fleet stays up"
    assert "nothing to shut down: every agent above is left live" in _plain(nothing.stdout)


def test_shutdown_reports_each_row_with_the_reason_the_service_gave(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three groups at once, which is reachable: a row stopped on an answering
    socket, a row recorded because ITS socket did not answer, and a row LEFT LIVE
    because ``stop`` saw its pane alive. The CLI prints the service's reason and
    asserts no cause of its own."""
    report = _report(
        stopped=[_agent("coder-auth", ended=True, exit_status=0)],
        recorded=[
            ShutdownRow(
                _agent("manager", "manager", pane="%9", ended=True),
                "no server answered on socket 'asq-old'",
            )
        ],
        failed=[
            ShutdownRow(
                _agent("tester-py311", "tester", pane="%11"),
                "could not stop 'tester-py311' (send-keys failed) — its pane is still alive",
            )
        ],
        sessions_killed=[f"asq:{SESSION}"],
        sessions_left_up=["asq:asq-ruby-fox"],
        servers_absent=["asq-old"],
        claims_released=["tsk_01x"],
        paused_cleared=["api"],
    )
    shutdown = _install(monkeypatch, "shutdown", report)

    result = runner.invoke(app, ["fleet", "shutdown", "--yes", "--force"])

    assert result.exit_code == 1, "the fleet is NOT down — said in the code as well as the report"
    out = _plain(result.stdout)
    assert "⚠ fleet PARTLY shut down: 1 stopped, 1 recorded lost, 1 left live" in out
    assert f"sessions killed: asq:{SESSION}" in out
    assert "💤 coder-auth (exit 0)" in out
    assert "✗ manager recorded lost — no server answered on socket 'asq-old'" in out
    assert "⚠ tester-py311 LEFT LIVE — could not stop 'tester-py311'" in out
    assert "its pane is still alive" in out
    assert "session asq:asq-ruby-fox left up" in out
    assert "1 claimed task(s) released" in out
    assert "fleet-paused signal on api was cleared" in out
    assert "were NOT ended" in out, "the operator is told which rows they still own"
    assert "was not running" not in out, "the CLI never asserts a cause the service did not give"
    assert shutdown.calls[-1] == ((PROJECT,), {"force": True, "forget": False, "actor": "user"})


def test_shutdown_all_never_resolves_a_project_and_a_clean_run_exits_zero(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report(
        stopped=[_agent("coder-auth", ended=True)],
        sessions_absent=[f"asq:{SESSION}"],
    )
    shutdown = _install(monkeypatch, "shutdown", report)

    result = runner.invoke(app, ["fleet", "shutdown", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "✓ fleet shut down: 1 stopped, 0 recorded lost, 0 left live" in out
    assert "sessions killed: none" in out
    assert f"session asq:{SESSION} was already gone with its last window" in out, (
        "the ordinary shape: killing a session's last window takes the session with it"
    )
    assert "💤 coder-auth" in out and "(exit" not in out, "--force records no exit status"
    assert shutdown.args == (None,) and resolved.calls == []


def test_shutdown_all_names_a_resume_for_each_project_whose_pause_was_not_settled(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 22, 7/9: round 21 named the scoped shutdown's project in the
    pause warning's ``fleet resume``, and under ``--all`` dropped the command: the line
    ended at "re-run once the store answers", with signals left set in several projects
    and no way to clear them but the whole shutdown again. Each project whose signal could
    not be read or cleared gets its own ``resume``; a lookup that failed whole names none."""
    report = _report(
        stopped=[_agent("coder-auth", ended=True)],
        pause_scan_failed="api: could not be read (locked); tool: could not be cleared (locked)",
        pause_unsettled=[PROJECT, UNNAMED],
    )
    _install(monkeypatch, "shutdown", report)

    result = runner.invoke(app, ["fleet", "shutdown", "--all", "--yes"])

    out = " ".join(_plain(result.stdout).split())
    assert (
        "re-run once the store answers, or clear them with "
        "`aisquare fleet resume --project amber-otter`; "
        "`aisquare fleet resume --project prj_02def`"
    ) in out, out
    assert resolved.calls == []

    # Round 23, 7/10: `--json` names them too, as projects a script can resume: the display
    # names `pause_scan_failed` joins are no `--project` reference.
    payload = json.loads(
        runner.invoke(app, ["--json", "fleet", "shutdown", "--all", "--yes"]).stdout
    )
    assert [(p["codename"], p["project"]["id"]) for p in payload["pause_unsettled"]] == [
        ("amber-otter", PROJECT.id),
        (None, UNNAMED.id),
    ]

    _install(monkeypatch, "shutdown", _report(pause_scan_failed="OperationalError: locked"))
    whole = " ".join(
        _plain(runner.invoke(app, ["fleet", "shutdown", "--all", "--yes"]).stdout).split()
    )
    assert "re-run once the store answers" in whole and "fleet resume" not in whole, whole
    payload = json.loads(
        runner.invoke(app, ["--json", "fleet", "shutdown", "--all", "--yes"]).stdout
    )
    assert payload["pause_unsettled"] == [], "a lookup that failed whole asked no project"


def test_shutdown_json_carries_every_group_and_the_reasons(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report(
        stopped=[_agent("coder-auth", ended=True, exit_status=0)],
        recorded=[ShutdownRow(_agent("manager", "manager", ended=True), "no server answered")],
        sessions_absent=[f"asq:{SESSION}"],
    )
    _install(monkeypatch, "shutdown", report)

    result = runner.invoke(app, ["--json", "fleet", "shutdown", "--yes"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "stopped",
        "recorded",
        "failed",
        "sessions_killed",
        "sessions_absent",
        "sessions_failed",
        "sessions_left_up",
        "servers_absent",
        "claims_released",
        "release_failures",
        "interrupted",
        "paused_cleared",
        "paused_kept",
        "incomplete_projects",
        "late_scan_failed",
        "pause_scan_failed",
        "pause_unsettled",
    }
    assert payload["stopped"][0]["label"] == "coder-auth"
    assert payload["recorded"] == [
        {"agent": report.recorded[0].agent.model_dump(mode="json"), "reason": "no server answered"}
    ], "a machine gets the reason too — the causes are not interchangeable"
    assert payload["failed"] == [] and payload["sessions_absent"] == [f"asq:{SESSION}"]


def test_shutdown_json_without_yes_is_the_plan_and_changes_nothing(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "shutdown_plan", _plan(absent_sockets=["asq-old"], sessions=[]))
    shutdown = _install(monkeypatch, "shutdown", _report())

    result = runner.invoke(app, ["--json", "fleet", "shutdown"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert [a["label"] for a in payload["agents"]] == ["coder-auth", "manager"]
    assert payload["absent_sockets"] == ["asq-old"] and payload["sessions"] == []
    assert shutdown.calls == []


def test_shutdown_says_so_when_there_is_nothing_to_shut_down(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "shutdown_plan", ShutdownPlan())
    shutdown = _install(monkeypatch, "shutdown", _report())

    result = runner.invoke(app, ["fleet", "shutdown"])

    assert result.exit_code == 0, result.output
    assert "nothing to shut down" in _plain(result.stdout)
    assert "dry run" not in result.stdout, "nothing to confirm is not a dry run"
    assert shutdown.calls == []


def test_shutdown_forget_shows_every_row_ended_unchecked_and_passes_the_word_on(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 27 of #257: with no usable tmux, ``--forget`` ends the rows on the operator's
    word, so the plan says nothing is stopped, and the run is told so. A row whose socket
    answers is shown left live (round 28)."""
    plan = _install(
        monkeypatch,
        "shutdown_plan",
        _plan(
            sessions=[],
            tmux_unusable="tmux is not installed",
            held={"agt_manager": "a server listens on its socket /tmp/tmux-1000/asq"},
        ),
    )
    shutdown = _install(monkeypatch, "shutdown", _report())

    dry = runner.invoke(app, ["fleet", "shutdown", "--forget"])
    as_json = runner.invoke(app, ["--json", "fleet", "shutdown", "--forget"])
    ran = runner.invoke(app, ["fleet", "shutdown", "--forget", "--yes"])

    assert dry.exit_code == 0, dry.output
    out = _plain(dry.stdout)
    assert "tmux is not usable here (tmux is not installed), so nothing can be stopped" in out
    assert "✗ coder-auth · amber-otter — recorded lost, unchecked" in out
    assert "⚠ manager · amber-otter — left live: a server listens on its socket" in out
    assert "stopped (%7)" not in out
    assert plan.calls[0] == ((PROJECT,), {"forget": True})
    assert json.loads(as_json.stdout)["tmux_unusable"] == "tmux is not installed"
    assert ran.exit_code == 0, ran.output
    assert shutdown.calls == [((PROJECT,), {"force": False, "forget": True, "actor": "user"})]


def test_shutdown_maps_a_refusal_onto_the_fail_contract(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusals are the point of the command: run from inside the fleet's own
    server, or with a tmux that cannot be asked, it must say so — never a traceback,
    and ``--json`` must still emit."""
    _install(monkeypatch, "shutdown_plan", FleetError("fleet shutdown is running INSIDE …"))
    _install(monkeypatch, "shutdown", _report())

    human = runner.invoke(app, ["fleet", "shutdown"])
    assert human.exit_code == 1
    assert "INSIDE" in _plain(human.output)

    _install(monkeypatch, "shutdown", FleetUnavailable("tmux is not installed"))
    as_json = runner.invoke(app, ["--json", "fleet", "shutdown", "--yes"])
    assert as_json.exit_code == 1
    payload = json.loads(as_json.stdout)
    assert payload["error"] == "fleet_unavailable" and "tmux" in payload["detail"]


# ── attach ───────────────────────────────────────────────────────────────────


@pytest.fixture
def exec_spy(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Intercept the exec so the test process is never replaced by tmux."""
    seen: list[list[str]] = []
    monkeypatch.setattr(fleet_cli, "_exec_attach", lambda argv: seen.append(list(argv)))
    return seen


ATTACH_ARGV = ["tmux", "-L", "asq", "attach-session", "-t", "=asq-amber-otter"]


def test_attach_execs_the_argv_the_service_hands_back(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, exec_spy: list[list[str]]
) -> None:
    attach_argv = _install(monkeypatch, "attach_argv", list(ATTACH_ARGV))
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)  # a real terminal

    result = runner.invoke(app, ["fleet", "attach", "-P", "amber-otter"])

    assert result.exit_code == 0, result.output
    assert exec_spy == [ATTACH_ARGV]
    assert attach_argv.args == (PROJECT,)
    assert resolved.args == ("amber-otter",)


def test_attach_without_a_terminal_refuses_before_the_exec(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, exec_spy: list[list[str]]
) -> None:
    """`tmux attach` cannot work without a terminal — and the refusal is what keeps
    the exec unreachable from every non-TTY harness (a test sweep once reached it
    and replaced the pytest process). CliRunner IS the non-TTY case."""
    _install(monkeypatch, "attach_argv", list(ATTACH_ARGV))

    human = runner.invoke(app, ["fleet", "attach"])
    assert human.exit_code == 1, human.output
    assert "interactive terminal" in _plain(human.stderr)
    assert exec_spy == []

    machine = runner.invoke(app, ["--json", "fleet", "attach"])
    assert machine.exit_code == 0  # --json reports the argv instead; never a refusal
    assert json.loads(machine.stdout) == {"argv": ATTACH_ARGV}
    assert exec_spy == []


def test_attach_under_json_prints_the_argv_and_does_not_exec(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, exec_spy: list[list[str]]
) -> None:
    _install(monkeypatch, "attach_argv", list(ATTACH_ARGV))

    result = runner.invoke(app, ["--json", "fleet", "attach"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"argv": ATTACH_ARGV}
    assert exec_spy == []


def test_attach_without_tmux_is_fleet_unavailable(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch, exec_spy: list[list[str]]
) -> None:
    _install(monkeypatch, "attach_argv", FleetUnavailable("tmux is not installed"))

    result = runner.invoke(app, ["--json", "fleet", "attach"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error": "fleet_unavailable",
        "detail": "tmux is not installed",
    }
    assert exec_spy == []


def test_attach_whose_exec_fails_is_legible_not_a_traceback(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmux can vanish between the service's check and the exec (or PATH can
    differ); the OSError must reach the operator as a sentence."""
    _install(monkeypatch, "attach_argv", list(ATTACH_ARGV))

    def broken(argv: list[str]) -> None:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(fleet_cli, "_exec_attach", broken)
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)  # a real terminal

    human = runner.invoke(app, ["fleet", "attach"])
    assert human.exit_code == 1, human.output
    assert human.exception is None or isinstance(human.exception, SystemExit)
    err = _plain(human.stderr)
    assert "could not run tmux" in err
    assert "No such file or directory" in err

    # The control: under --json the argv is REPORTED, never exec'd, so the same
    # broken exec is never reached and the answer is the argv, exit 0.
    machine = runner.invoke(app, ["--json", "fleet", "attach"])
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.stdout) == {"argv": ATTACH_ARGV}


# ── reap ─────────────────────────────────────────────────────────────────────


def test_reap_names_what_it_found(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = ReapReport(
        ended=[_agent("coder-auth", ended=True, exit_status=0)],
        lost=[_agent("tester-py311", "tester", pane="%9", ended=True)],
        worktrees_removed=[WORKTREE],
    )
    reap = _install(monkeypatch, "reap", report)

    result = runner.invoke(app, ["fleet", "reap"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "✓ reaped: 1 ended, 1 lost, 1 worktrees removed" in out
    assert "💤 coder-auth (exit 0)" in out
    assert "✗ tester-py311 pane %9 gone" in out
    assert str(WORKTREE) in out
    assert reap.args == (PROJECT,)


def test_reap_all_sweeps_every_project_without_resolving_one(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    reap = _install(monkeypatch, "reap", ReapReport())

    result = runner.invoke(app, ["fleet", "reap", "--all"])

    assert result.exit_code == 0, result.output
    assert reap.args == (None,)
    assert resolved.calls == []  # --all never asks "which project?"
    assert "✓ reaped: 0 ended, 0 lost, 0 worktrees removed" in _plain(result.stdout)


def test_reap_server_down_reaches_the_service_as_a_keyword(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag doctor prescribes for a silent server must arrive at ``reap`` as
    ``server_down=True`` — and its absence as ``False``, never as a positional."""
    reap = _install(monkeypatch, "reap", ReapReport())

    assert runner.invoke(app, ["fleet", "reap", "--server-down"]).exit_code == 0
    args, kwargs = reap.calls[-1]
    assert args == (PROJECT,) and kwargs == {"server_down": True}
    assert runner.invoke(app, ["fleet", "reap", "--all"]).exit_code == 0
    args, kwargs = reap.calls[-1]
    assert args == (None,) and kwargs == {"server_down": False}


def test_reap_json(runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each branch with its repo (review of #259, round 16): ``--all`` walks several, and a
    bare ``git branch -D <branch>`` did not say where to run it."""
    kept = WORKTREE.parent / "coder-open"
    other = Path("/home/me/oss/my api")
    report = ReapReport(
        ended=[_agent("coder-auth", ended=True, exit_status=0)],
        lost=[],
        worktrees_removed=[WORKTREE],
        worktrees_kept=[(kept, "uncommitted changes")],
        branches_deleted=[(PROJECT.root, "fleet/amber-otter/auth")],
        branches_kept=[(other, "fleet/ruby-fox/billing")],
    )
    _install(monkeypatch, "reap", report)

    result = runner.invoke(app, ["--json", "fleet", "reap", "--all"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "ended",
        "lost",
        "worktrees_removed",
        "worktrees_kept",
        "branches_deleted",
        "branches_kept",
        "claims_released",
        "release_failures",
        "held",
    }
    assert [a["label"] for a in payload["ended"]] == ["coder-auth"]
    assert payload["claims_released"] == []
    assert payload["lost"] == [] and payload["held"] == []
    assert payload["worktrees_removed"] == [str(WORKTREE)]
    assert payload["worktrees_kept"] == [{"path": str(kept), "reason": "uncommitted changes"}]
    assert payload["branches_deleted"] == [
        {"repo": str(PROJECT.root), "branch": "fleet/amber-otter/auth"}
    ]
    assert payload["branches_kept"] == [{"repo": str(other), "branch": "fleet/ruby-fox/billing"}]

    human = runner.invoke(app, ["fleet", "reap", "--all"])
    said = " ".join(human.stdout.split())
    assert f"🗑 {WORKTREE}" in human.stdout
    assert f"🗑 branch fleet/amber-otter/auth in {PROJECT.root}" in said
    assert f"⚠ kept {kept} — uncommitted changes" in human.stdout
    assert f"· kept branch fleet/ruby-fox/billing in {other} — commits the default branch" in said
    drop = install_route.command_line(
        ["git", "-C", str(other), "branch", "-D", "fleet/ruby-fox/billing"]
    )
    assert f"`{drop}` drops it" in said, "and how to drop it, in its repo"


def test_reap_names_a_refused_release_and_exits_1(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5: the same contract as `stop` and `shutdown` for the same fact."""
    report = ReapReport(
        ended=[_agent("coder-auth", ended=True, exit_status=0)],
        release_failures=["coder-auth: could not be released (OperationalError: locked)"],
    )
    _install(monkeypatch, "reap", report)

    result = runner.invoke(app, ["fleet", "reap"])

    assert result.exit_code == 1
    assert "⚠ claims of coder-auth: could not be released (OperationalError: locked)" in _plain(
        result.stdout
    )


# ── rename ───────────────────────────────────────────────────────────────────


def test_rename_reports_the_new_codename_and_session(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    renamed = PROJECT.model_copy(update={"codename": "ruby-fox"})
    rename = _install(monkeypatch, "rename", renamed)

    result = runner.invoke(app, ["fleet", "rename", "ruby-fox", "-P", "amber-otter"])

    assert result.exit_code == 0, result.output
    assert "✓ api is now ruby-fox (asq-ruby-fox)" in _plain(result.stdout)
    assert rename.args == (PROJECT, "ruby-fox")


def test_rename_json_is_the_updated_project(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "rename", PROJECT.model_copy(update={"codename": "ruby-fox"}))

    result = runner.invoke(app, ["--json", "fleet", "rename", "ruby-fox"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # `notes` carries what a swallowed tmux rename cost (empty when it worked).
    assert set(payload) == {"project", "name", "codename", "tmux_session", "notes"}
    assert payload["notes"] == []
    assert payload["codename"] == "ruby-fox"
    assert payload["tmux_session"] == "asq-ruby-fox"
    assert payload["project"]["codename"] == "ruby-fox"


def test_rename_to_a_taken_or_invalid_codename_is_refused_with_the_reason(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "rename", FleetError("codename 'ruby-fox' is already used by ~/oss/api"))

    result = runner.invoke(app, ["fleet", "rename", "ruby-fox"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "already used by ~/oss/api" in _plain(result.stderr)


# ── pause / resume ───────────────────────────────────────────────────────────


def test_pause_and_resume_set_the_signal_as_the_acting_session(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--as`` names who flipped the switch (a manager passes its own session);
    without it the user did, and the service is told ``None`` — never a value
    the CLI invented. Both directions, for both verbs."""
    pause = _install(monkeypatch, "pause", None)
    resume = _install(monkeypatch, "resume", None)

    paused = runner.invoke(app, ["fleet", "pause", "--as", "mgr"])
    assert paused.exit_code == 0, paused.output
    assert "⏸ fleet paused for api" in _plain(paused.stdout)
    assert runner.invoke(app, ["fleet", "pause"]).exit_code == 0

    resumed = runner.invoke(app, ["fleet", "resume"])
    assert resumed.exit_code == 0, resumed.output
    assert "▶ fleet resumed for api" in _plain(resumed.stdout)
    assert runner.invoke(app, ["fleet", "resume", "--as", "mgr"]).exit_code == 0

    # One service call per invocation, the acting session exactly as given.
    as_manager = ((PROJECT,), {"session_ref": "mgr"})
    as_user = ((PROJECT,), {"session_ref": None})
    assert pause.calls == [as_manager, as_user]
    assert resume.calls == [as_user, as_manager]


def test_pause_and_resume_json_say_which_way_the_switch_went(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "pause", None)
    _install(monkeypatch, "resume", None)

    paused = json.loads(runner.invoke(app, ["--json", "fleet", "pause"]).stdout)
    resumed = json.loads(runner.invoke(app, ["--json", "fleet", "resume"]).stdout)

    assert paused["paused"] is True
    assert resumed["paused"] is False
    for payload in (paused, resumed):
        assert set(payload) == {"paused", "project", "name", "codename", "tmux_session"}
        assert payload["codename"] == "amber-otter"


def test_pause_of_an_unknown_project_is_not_found(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "resolve_project", NoSuchProject("'api' matches several projects"))
    pause = _install(monkeypatch, "pause", None)

    result = runner.invoke(app, ["--json", "fleet", "pause", "-P", "api"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "not_found"
    assert pause.calls == []


# ── every fleet command under --json is JSON or nothing ──────────────────────


@pytest.mark.parametrize(
    ("argv", "fakes"),
    [
        (["fleet", "spawn", "coder"], {"spawn": SpawnReceipt(_agent(), None, SESSION)}),
        (["fleet", "ls"], {"list_agents": [_status(_agent())]}),
        (["fleet", "status"], {"list_agents": []}),
        (["fleet", "tell", "coder-auth", "hi"], {"tell": TellResult("yes", "typed")}),
        (["fleet", "stop", "coder-auth"], {"stop": StopReceipt(_agent(), [])}),
        (["fleet", "restart", "--all"], {"bulk": BulkReport("restart", False, ())}),
        (["fleet", "stop", "--all", "--managers"], {"bulk": BulkReport("stop", True, ())}),
        (["fleet", "attach"], {"attach_argv": list(ATTACH_ARGV)}),
        (["fleet", "reap"], {"reap": ReapReport()}),
        (["fleet", "rename", "ruby-fox"], {"rename": PROJECT}),
        (["fleet", "pause"], {"pause": None}),
        (["fleet", "resume"], {"resume": None}),
    ],
    ids=lambda value: " ".join(value) if isinstance(value, list) else "",
)
def test_every_fleet_command_emits_one_json_object_on_success(
    runner: CliRunner,
    resolved: Seen,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    fakes: dict[str, object],
) -> None:
    """The success path of each command, under ``--json``: stdout parses as ONE
    object and nothing human is mixed in. The sweep in
    ``test_json_stdout_is_machine_readable`` reaches only the failure branch of
    these commands (the real service is not wired), so this is the half it
    cannot see."""
    for name, outcome in fakes.items():
        _install(monkeypatch, name, outcome)
    monkeypatch.setattr(fleet_cli, "_exec_attach", lambda argv: None)

    result = runner.invoke(app, ["--json", *argv])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict) and payload
    assert "✓" not in result.stdout and "⏸" not in result.stdout and "▶" not in result.stdout


# ── ui ───────────────────────────────────────────────────────────────────────


class _Stream(io.StringIO):
    """A stream whose ``isatty`` answer is chosen by the test."""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        super().isatty()  # raises ValueError once closed, exactly as a real stream does
        return self._tty


def _streams(monkeypatch: pytest.MonkeyPatch, *, stdin: bool, stdout: bool) -> None:
    monkeypatch.setattr("sys.stdin", _Stream(stdin))
    monkeypatch.setattr("sys.stdout", _Stream(stdout))


def test_interactive_terminal_needs_two_ttys_and_a_real_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The predicate, against fake streams: true only when every condition of
    §3.8 holds, and each failing condition is named."""
    monkeypatch.setenv("TERM", "xterm-256color")
    _streams(monkeypatch, stdin=True, stdout=True)
    assert fleet_cli.interactive_terminal() is True
    assert fleet_cli.not_interactive_reason() is None

    _streams(monkeypatch, stdin=False, stdout=True)
    assert fleet_cli.interactive_terminal() is False
    assert fleet_cli.not_interactive_reason() == "stdin is not a TTY"

    _streams(monkeypatch, stdin=True, stdout=False)
    assert fleet_cli.interactive_terminal() is False
    assert fleet_cli.not_interactive_reason() == "stdout is not a TTY"

    _streams(monkeypatch, stdin=True, stdout=True)
    monkeypatch.setenv("TERM", "dumb")
    assert fleet_cli.interactive_terminal() is False
    assert fleet_cli.not_interactive_reason() == "TERM=dumb"

    monkeypatch.delenv("TERM")
    assert fleet_cli.interactive_terminal() is False
    assert fleet_cli.not_interactive_reason() == "TERM is not set"


def test_interactive_terminal_survives_a_closed_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = _Stream(True)
    closed.close()  # isatty() on a closed StringIO raises ValueError
    monkeypatch.setattr("sys.stdin", closed)
    monkeypatch.setattr("sys.stdout", _Stream(True))
    monkeypatch.setenv("TERM", "xterm")

    assert fleet_cli.interactive_terminal() is False
    assert fleet_cli.not_interactive_reason() == "stdin or stdout is closed"


@pytest.fixture
def ui_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record ``run_ui`` instead of starting Textual."""
    import aisquare.cli.ui.app as ui_app

    calls: list[str] = []
    monkeypatch.setattr(ui_app, "run_ui", lambda **options: calls.append("run_ui"))
    return calls


def test_ui_refuses_without_a_tty_and_says_which_condition_failed(
    runner: CliRunner, ui_spy: list[str]
) -> None:
    """Under CliRunner stdin and stdout are pipes, so the real predicate refuses."""
    result = runner.invoke(app, ["ui"])

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    err = _plain(result.stderr)
    assert "✗ the fleet UI needs an interactive terminal (stdin is not a TTY)" in err
    assert "aisquare fleet ls --json" in err
    assert ui_spy == []


def test_ui_refuses_under_json_with_a_machine_readable_reason(
    runner: CliRunner, ui_spy: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even at a real terminal ``--json`` means "a program is listening"; a
    full-screen app is the wrong answer to it."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)

    result = runner.invoke(app, ["--json", "ui"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_a_tty"
    assert "--json" in payload["detail"]
    assert ui_spy == []


def test_ui_json_without_a_tty_is_still_json(runner: CliRunner, ui_spy: list[str]) -> None:
    result = runner.invoke(app, ["--json", "ui"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "not_a_tty"
    assert payload["detail"]
    assert ui_spy == []


def test_ui_runs_the_app_at_an_interactive_terminal(
    runner: CliRunner, ui_spy: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: with the predicate satisfied, ``ui`` starts the app
    exactly once and prints nothing of its own."""
    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)

    result = runner.invoke(app, ["ui"])

    assert result.exit_code == 0, result.output
    assert ui_spy == ["run_ui"]
    assert result.output == ""


def test_ui_project_and_group_scope_the_app_to_those_projects(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ui --project a --project b --group g`` hands the app those projects' ids (#83)."""
    import aisquare.cli.ui.app as ui_app

    monkeypatch.setattr(fleet_cli, "interactive_terminal", lambda: True)
    scopes: list[object] = []
    monkeypatch.setattr(ui_app, "run_ui", lambda **options: scopes.append(options["scope"]))
    with store_session() as store:
        made = []
        for name in ("alpha", "beta", "gamma", "delta"):
            root = (tmp_path / name).resolve()
            root.mkdir()
            made.append(store.onboard_project(ProjectInfo(id=project_id_for(root), root=root)))
        group = store.create_project_group("ops")
        store.update_project_layout(made[3].id, group_id=group.id)

    result = runner.invoke(app, ["ui", "--project", "alpha", "-P", "beta", "--group", "ops"])
    assert result.exit_code == 0, result.output
    assert scopes == [frozenset({made[0].id, made[1].id, made[3].id})]

    scopes.clear()
    assert runner.invoke(app, ["ui"]).exit_code == 0 and scopes == [None]

    refused = runner.invoke(app, ["ui", "--group", "nope"])
    assert refused.exit_code == 1 and "no project group named 'nope'" in _plain(refused.stderr)
    assert scopes == [None]


def test_ui_help_exists(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ui", "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage" in _plain(result.output)
    assert "fleet UI" in _plain(result.output)


# ── markup in data ───────────────────────────────────────────────────────────


def test_bracketed_data_survives_to_the_screen(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rich reads ``[...]`` as a style tag; a project under ``~/[archive]/`` and a
    note quoting one must print as typed (``tests/test_console_markup.py``)."""
    bracketed = ProjectInfo(id="prj_03", root=Path("/home/me/[archive]/api"), codename="quiet-lynx")
    _install(monkeypatch, "resolve_project", bracketed)
    receipt = SpawnReceipt(
        agent=_agent("coder-auth", project=bracketed),
        asked_label=None,
        tmux_session="asq-quiet-lynx",
        notes=["cwd is /home/me/[archive]/api"],
    )
    _install(monkeypatch, "spawn", receipt)

    result = runner.invoke(app, ["fleet", "spawn", "coder"])

    assert result.exit_code == 0, result.output
    assert "/home/me/[archive]/api" in result.stdout


# ── persona (docs/plans/spawn-personas.md §7 "P2") ───────────────────────────


def test_spawn_persona_reaches_the_service_and_ends_the_receipt(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _agent("coder-auth").model_copy(update={"persona": "skeptic"})
    seen = _install(
        monkeypatch, "spawn", SpawnReceipt(agent=agent, asked_label=None, tmux_session=SESSION)
    )

    result = runner.invoke(app, ["fleet", "spawn", "coder", "--persona", "skeptic"])

    assert result.exit_code == 0, result.output
    assert seen.kwargs["persona"] == "skeptic"
    assert seen.kwargs["agent_args"] == [], "--persona is spawn's own option, not the agent's"
    assert _plain(result.stdout).endswith(
        "✓ spawned coder-auth (agt_coderauth) → asq-amber-otter %7 · persona skeptic"
    )


def test_spawn_without_persona_sends_none_and_the_receipt_says_nothing(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _install(
        monkeypatch,
        "spawn",
        SpawnReceipt(agent=_agent("coder-auth"), asked_label=None, tmux_session=SESSION),
    )

    result = runner.invoke(app, ["fleet", "spawn", "coder"])

    assert result.exit_code == 0, result.output
    assert seen.kwargs["persona"] is None
    assert "persona" not in result.stdout


def test_ls_shows_the_persona_on_its_row_and_json_carries_it(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    skeptic = _agent("coder-auth").model_copy(update={"persona": "skeptic"})
    _install(
        monkeypatch,
        "list_agents",
        [_status(skeptic, "working"), _status(_agent("tester-1", "tester", pane="%9"), "waiting")],
    )

    human = runner.invoke(app, ["fleet", "ls"])
    as_json = runner.invoke(app, ["--json", "fleet", "ls"])

    assert human.exit_code == 0, human.output
    out = _plain(human.stdout)
    assert "coder-auth coder ▶ working · skeptic %7" in out
    assert "tester-1 tester ⏸ waiting %9" in out
    payload = json.loads(as_json.stdout)
    assert [entry["agent"]["persona"] for entry in payload["agents"]] == ["skeptic", None]


# --- capacity and the test-run gate (#283) ---------------------------------------------


def _headroom(monkeypatch: pytest.MonkeyPatch, *answers: int | None) -> list[float]:
    """Each probe answers the next of ``answers`` (the last repeats); time passes only
    when the gate sleeps, so a 15-minute wait is instant."""
    from aisquare.core import memory

    queue = list(answers)
    slept: list[float] = []
    clock = [0.0]

    def probe() -> memory.Headroom:
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        return memory.Headroom(value, 16_384, "normal", None, "test")

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(memory, "headroom", probe)
    monkeypatch.setattr(fleet_service, "_sleep", sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", lambda: clock[0])
    return slept


def test_capacity_json_names_the_headroom_and_every_limit(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _headroom(monkeypatch, 1500)
    result = runner.invoke(app, ["--json", "fleet", "capacity"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["available_mb"] == 1500 and report["min_free_mb"] == 2048
    assert report["admits"] is False and report["reason"] == "1500 MB available, 2048 MB needed"
    assert report["live_total"] == 0 and report["max_agents_per_project"] == 4


def test_gate_at_the_deadline_runs_nothing_and_exits_75(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slept = _headroom(monkeypatch, 900)
    marker = tmp_path / "ran"
    result = runner.invoke(
        app,
        ["fleet", "gate", "--min-free-mb", "2048", "--wait", "12s", "--", "touch", str(marker)],
    )
    assert result.exit_code == 75, result.output
    assert "not run: 900 MB available, 2048 MB needed for 12s" in result.output
    assert not marker.exists()
    assert slept == [5.0, 5.0, 2.0], "every 5 s, and no further than the deadline"


def test_gate_with_headroom_returns_the_commands_exit_code(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    _headroom(monkeypatch, 900, 900, 4000)
    result = runner.invoke(
        app,
        ["--json", "fleet", "gate", "--min-free-mb", "2048", "--wait", "1m", "--",
         sys.executable, "-c", "raise SystemExit(3)"],
    )  # fmt: skip
    assert result.exit_code == 3, result.output
    [line] = result.output.strip().splitlines()
    report = json.loads(line)
    assert report["passed"] is True and report["exit_code"] == 3
    assert report["waited_seconds"] == 10.0


def test_gate_json_at_the_deadline_is_one_object(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _headroom(monkeypatch, 900)
    result = runner.invoke(app, ["--json", "fleet", "gate", "--wait", "1s"])
    assert result.exit_code == 75
    [line] = result.output.strip().splitlines()
    assert json.loads(line) == {
        "passed": False,
        "ran": None,
        "available_mb": 900,
        "needed_mb": 2048,
        "waited_seconds": 1.0,
        "pressure": "normal",
    }


def test_gate_takes_test_gate_mb_over_min_free_mb(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.config import FleetSettings

    monkeypatch.setattr(fleet_service, "settings", lambda: FleetSettings(test_gate_mb=6000))
    _headroom(monkeypatch, 4000)
    result = runner.invoke(app, ["--json", "fleet", "gate", "--wait", "0s"])
    assert json.loads(result.output)["needed_mb"] == 6000


def test_a_capacity_refusal_carries_its_own_json_code(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise fleet_service.CapacityRefused("capacity_memory", "1500 MB available, 2048 MB needed")

    monkeypatch.setattr(fleet_service, "spawn", refuse)
    monkeypatch.setattr(fleet_cli, "_project", lambda _ref: None)
    result = runner.invoke(app, ["--json", "fleet", "spawn", "coder"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "capacity_memory"


def test_gate_json_with_a_command_is_still_one_object_on_stdout(tmp_path: Path) -> None:
    """Review of #323 (minor): the child inherited stdout, so `--json fleet gate -- pytest`
    printed pytest's output and then the object. Under --json the child's stdout goes
    to stderr. A real process: CliRunner cannot see what a child writes to fd 1."""
    import subprocess
    import sys

    from aisquare.core import selfcli

    child = [sys.executable, "-c", "print('the child speaks'); raise SystemExit(4)"]
    argv = selfcli.argv_for(
        ["--json", "fleet", "gate", "--min-free-mb", "0", "--wait", "0s", "--", *child]
    )
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert done.returncode == 4, done.stderr
    [line] = done.stdout.strip().splitlines()
    assert json.loads(line)["exit_code"] == 4
    assert "the child speaks" in done.stderr
