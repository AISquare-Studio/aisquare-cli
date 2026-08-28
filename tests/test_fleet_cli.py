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
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.app import app
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession
from aisquare.services.fleet import (
    FleetError,
    FleetUnavailable,
    NoSuchAgent,
    NoSuchProject,
    ReapReport,
    SpawnReceipt,
    TellResult,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
PROJECT = ProjectInfo(id="prj_01abc", root=Path("/home/me/work/api"), codename="amber-otter")
UNNAMED = ProjectInfo(id="prj_02def", root=Path("/home/me/oss/tool"), codename=None)
SESSION = "asq-amber-otter"


def _plain(text: str) -> str:
    return " ".join(_ANSI.sub("", text).split())


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
    assert set(payload) == {"agent", "asked_label", "tmux_session", "notes"}
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
    assert "aisquare fleet spawn manager" in result.stdout


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
    tell = _install(monkeypatch, "tell", TellResult(delivered=True, how="typed into pane %7"))

    result = runner.invoke(app, ["fleet", "tell", "coder-auth", "ship it", "--as", "mgr"])

    assert result.exit_code == 0, result.output
    assert "✓ coder-auth: typed into pane %7" in _plain(result.stdout)
    assert tell.args == (PROJECT, "coder-auth", "ship it")
    assert tell.kwargs == {"sender": "mgr"}


def test_tell_to_a_busy_agent_is_marked_as_a_note_not_a_delivery(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    tell = _install(
        monkeypatch, "tell", TellResult(delivered=False, how="agent is working; filed a board note")
    )

    result = runner.invoke(app, ["fleet", "tell", "coder-auth", "ship it"])

    assert result.exit_code == 0, result.output
    assert "→ coder-auth: agent is working; filed a board note" in _plain(result.stdout)
    assert "✓" not in result.stdout
    assert tell.kwargs == {"sender": None}  # no --as: the user spoke


def test_tell_json(runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "tell", TellResult(delivered=False, how="filed a board note"))

    result = runner.invoke(app, ["--json", "fleet", "tell", "coder-auth", "ship it"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "label": "coder-auth",
        "delivered": False,
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


def test_stop_confirms_and_passes_force_through(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = _install(monkeypatch, "stop", _agent("coder-auth", ended=True, exit_status=0))

    gentle = runner.invoke(app, ["fleet", "stop", "coder-auth"])
    assert gentle.exit_code == 0, gentle.output
    assert "✓ stopped coder-auth (agt_coderauth)" in _plain(gentle.stdout)
    assert stop.calls[-1] == ((PROJECT, "coder-auth"), {"force": False})

    forced = runner.invoke(app, ["fleet", "stop", "coder-auth", "--force"])
    assert forced.exit_code == 0, forced.output
    assert stop.calls[-1] == ((PROJECT, "coder-auth"), {"force": True})


def test_stop_json_returns_the_agent_row(
    runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "stop", _agent("coder-auth", ended=True, exit_status=0))

    result = runner.invoke(app, ["--json", "fleet", "stop", "coder-auth"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"agent"}
    assert payload["agent"]["label"] == "coder-auth"
    assert payload["agent"]["ended_at"] is not None
    assert payload["agent"]["exit_status"] == 0


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

    result = runner.invoke(app, ["fleet", "attach", "-P", "amber-otter"])

    assert result.exit_code == 0, result.output
    assert exec_spy == [ATTACH_ARGV]
    assert attach_argv.args == (PROJECT,)
    assert resolved.args == ("amber-otter",)


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
        worktrees_removed=[Path("/home/me/work/api/.aisquare-worktrees/coder-auth")],
    )
    reap = _install(monkeypatch, "reap", report)

    result = runner.invoke(app, ["fleet", "reap"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "✓ reaped: 1 ended, 1 lost, 1 worktrees removed" in out
    assert "💤 coder-auth (exit 0)" in out
    assert "✗ tester-py311 pane %9 gone" in out
    assert ".aisquare-worktrees/coder-auth" in out
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


def test_reap_json(runner: CliRunner, resolved: Seen, monkeypatch: pytest.MonkeyPatch) -> None:
    report = ReapReport(
        ended=[_agent("coder-auth", ended=True, exit_status=0)],
        lost=[],
        worktrees_removed=[Path("/home/me/work/api/.aisquare-worktrees/coder-auth")],
    )
    _install(monkeypatch, "reap", report)

    result = runner.invoke(app, ["--json", "fleet", "reap", "--all"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"ended", "lost", "worktrees_removed"}
    assert [a["label"] for a in payload["ended"]] == ["coder-auth"]
    assert payload["lost"] == []
    assert payload["worktrees_removed"] == ["/home/me/work/api/.aisquare-worktrees/coder-auth"]


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
    assert set(payload) == {"project", "name", "codename", "tmux_session"}
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
    pause = _install(monkeypatch, "pause", None)
    resume = _install(monkeypatch, "resume", None)

    paused = runner.invoke(app, ["fleet", "pause", "--as", "mgr"])
    assert paused.exit_code == 0, paused.output
    assert "⏸ fleet paused for api" in _plain(paused.stdout)
    assert pause.args == (PROJECT,) and pause.kwargs == {"session_ref": "mgr"}

    resumed = runner.invoke(app, ["fleet", "resume"])
    assert resumed.exit_code == 0, resumed.output
    assert "▶ fleet resumed for api" in _plain(resumed.stdout)
    assert resume.args == (PROJECT,) and resume.kwargs == {"session_ref": None}


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
        (["fleet", "tell", "coder-auth", "hi"], {"tell": TellResult(True, "typed")}),
        (["fleet", "stop", "coder-auth"], {"stop": _agent()}),
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
    monkeypatch.setattr(ui_app, "run_ui", lambda: calls.append("run_ui"))
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
