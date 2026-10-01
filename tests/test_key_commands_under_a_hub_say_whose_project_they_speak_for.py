"""Under an exported hub, the explainability key commands say whose project they speak for.

Review of #240, finding 12. The key commands default to ``orchestrator.team_project(None)``,
the project a launch from this shell joins: under ``AISQUARE_TEAM_HUB`` that is the hub's
project, and it stays so, since it is still true of a plain ``launch`` from this shell. Since
#230 a fleet window carries its own fleet's root as its hub and names its row, so a fleet seat
spawned from the same shell (``fleet spawn``, or ``fleet spawn -P repoA``) traces as its OWN
fleet's project. ``key set`` and ``register`` then configured the hub's project while the
seats traced with another key, and ``status`` kept reporting the hub (#170's mismatch).

One line on stderr says whose project the command speaks for, whenever a hub is exported and
absolute, the command is not in a fleet window (no fleet row resolves) and no ``--project`` was
given: from the hub's own checkout too, where ``fleet spawn -P repoA`` still gives a seat of
``repoA``. When a ``fleet spawn`` here with no ``-P`` (``services.fleet.resolve_project(None)``)
joins another project than the hub's, the line names it. stdout, ``--json`` and the exit code
are as they were, and a store that is absent, damaged or cannot answer costs that name, never
the line or the command.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner, Result

from aisquare.cli import explainability as explainability_cli
from aisquare.cli.app import app
from aisquare.core import orchestrator, paths
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.workspace import pin_project, project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import explainability as service
from aisquare.services import explainability_ops as ops
from aisquare.services import fleet as fleet_service
from tests import test_a_fleet_window_is_on_its_fleets_board as window_suite
from tests import test_project_explainability_key as key_suite
from tests.test_a_fleet_window_is_on_its_fleets_board import _project, _seat
from tests.test_project_explainability_key import MACHINE_KEY, PROJECT_KEY, _settings

# The suites' fixtures, bound here so pytest finds them for this module's tests: the
# one-shot hub warnings unfired and no inherited pins, and a home with no ambient key.
_quiet = window_suite._quiet
home = key_suite.home

KEY_VAR = "HUB_WORKSPACE_KEY"
SESSION = "5e5510e5-0000-4000-8000-000000000012"
"""A fixed session id, so two runs of ``env`` print the same exports."""
SAID = "the $AISQUARE_TEAM_HUB project. Fleet seats trace as their own project"
"""The words only the line carries, to find it on stderr and to prove stdout has none."""
SPEAKS = "note: this command speaks for hub, the $AISQUARE_TEAM_HUB project. Fleet seats trace "
LINE = SPEAKS + (
    "as their own project: a fleet spawn here joins api, and --project api addresses its key."
)
"""The whole line, for a shell under the hub ``hub`` in the checkout ``api``."""
ANY_SEAT = SPEAKS + (
    "as their own project: --project <name> addresses the key of a seat spawned with -P <name>."
)
"""The whole line where a ``fleet spawn`` here joins the hub's project, or cannot be read."""

KEY_SET = ["explainability", "key", "set", "--from-env", KEY_VAR]
STATUS = ["explainability", "status"]
REGISTER = ["explainability", "register"]
COMMANDS = pytest.mark.parametrize(
    "argv", [KEY_SET, STATUS, REGISTER], ids=["key set", "status", "register"]
)


def _said(stderr: str) -> list[str]:
    """The lines of a command's stderr that are the line."""
    return [line for line in stderr.splitlines() if SAID in line]


@pytest.fixture
def machine(home: Path, monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """A machine with two deployments and its own key; tracing is off, so ``status`` dials
    nothing, and ``register`` reaches no gateway: the keys it was asked to use are returned."""
    config = _settings()
    config.explainability.enabled = False
    save_config(config)
    service.store_api_key(MACHINE_KEY)
    monkeypatch.setenv(KEY_VAR, PROJECT_KEY)
    used: list[str | None] = []

    def register_roster(target: ops.ResolvedTarget, names: tuple[str, ...]) -> ops.HttpVerdict:
        used.append(target.api_key)
        return ops.HttpVerdict(ok=True, status=200, detail="HTTP 200", payload={"agents": []})

    monkeypatch.setattr(ops, "register_roster", register_roster)
    return used


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """An exported, absolute hub: the board a plain launch from this shell joins. A git
    checkout of its own, so a ``fleet spawn`` from inside it joins it."""
    root = (tmp_path / "hub").resolve()
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(root))
    return ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])


@pytest.fixture
def api(machine: list[str | None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """This checkout: a repository registered as its own project, where the commands run."""
    project = _project(tmp_path, "api")
    monkeypatch.chdir(project.root)
    return project


def _key_project(runner: CliRunner, *argv: str) -> str | None:
    """The project ``status`` resolved the key FOR, off its ``--json``."""
    result = runner.invoke(app, ["--json", *STATUS, *argv])
    assert result.exit_code == 0, result.output
    answer: str | None = json.loads(result.stdout)["key_project"]
    return answer


def _unsaid(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> Result:
    """The same command with the line held back: the control for what else it changed.

    Not ``--project hub``, the default said aloud: ``key set`` echoes the option back in
    the ``register`` it suggests next, so its stdout differs for a reason of its own.
    """
    with monkeypatch.context() as quiet:
        quiet.setattr(explainability_cli, "_hub_note", lambda project: None, raising=False)
        return runner.invoke(app, argv)


def _traced_by_a_seat_of(project: ProjectInfo, monkeypatch: pytest.MonkeyPatch) -> str | None:
    """The key a seat of ``project`` traces with, resolved inside its window's environment.

    What ``fleet spawn -P <project>`` puts on the window (``services.fleet.spawn``): the
    fleet's own root as the hub, and the row's id. One seat per project, made on first ask.
    """
    with store_session() as store:
        rows = store.fleet_agents(project.id)
    seat = rows[0] if rows else _seat(project, "coder-1")
    with monkeypatch.context() as window:
        window.setenv("AISQUARE_TEAM_HUB", str(project.root))
        window.setenv("AISQUARE_FLEET_AGENT", seat.id)
        joined = orchestrator.team_project()
    assert joined.id == project.id, "the seat joins its own fleet's project"
    return ops.resolve_target(load_config().explainability, None, project_id=joined.id).api_key


# --- the line, in exactly its situation ---------------------------------------------------------


@COMMANDS
def test_under_a_hub_a_key_command_says_in_one_line_whose_project_it_speaks_for(
    argv: list[str], hub: ProjectInfo, api: ProjectInfo, runner: CliRunner
) -> None:
    """A shell under a hub, in a checkout whose fleet seats trace as ``api``."""
    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == [LINE], result.stderr
    assert SAID not in result.stdout, "stderr only"


@COMMANDS
def test_from_the_hubs_own_checkout_the_line_is_said_too(
    argv: list[str],
    hub: ProjectInfo,
    api: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``fleet spawn`` from here joins the hub, but ``fleet spawn -P api`` from here does
    not: the review's repro names ``-P repoA``. So the line is owed here as well, naming no
    project, since none is known."""
    monkeypatch.chdir(hub.root)

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == [ANY_SEAT], result.stderr


def test_the_repro_key_set_configures_the_hub_and_a_seat_spawned_with_p_traces_with_another_key(
    machine: list[str | None],
    hub: ProjectInfo,
    api: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review's repro, end to end, from the hub's own checkout: ``key set`` and
    ``register`` bind the hub's project, and a seat of ``api`` (``fleet spawn -P api``)
    traces with the machine's key. The line said so at both commands, and
    ``--project api``, which it points at, gives that seat the key."""
    monkeypatch.chdir(hub.root)

    attached = runner.invoke(app, KEY_SET)
    registered = runner.invoke(app, REGISTER)

    assert (attached.exit_code, registered.exit_code) == (0, 0), attached.output
    assert _said(attached.stderr + registered.stderr) == [ANY_SEAT, ANY_SEAT]
    assert machine == [PROJECT_KEY], "registered under the hub project's key, just attached"
    assert _traced_by_a_seat_of(api, monkeypatch) == MACHINE_KEY, "the mismatch the line names"

    named = runner.invoke(app, [*KEY_SET, "--project", "api"])

    assert named.exit_code == 0, named.output
    assert _said(named.stderr) == []
    assert _traced_by_a_seat_of(api, monkeypatch) == PROJECT_KEY


def test_the_default_project_under_a_hub_is_still_the_hubs(
    machine: list[str | None], hub: ProjectInfo, api: ProjectInfo, runner: CliRunner
) -> None:
    """The default stays ``team_project(None)``, which is still true of a plain ``launch``
    from this shell. Only the line is new."""
    assert _key_project(runner) == hub.id

    assert runner.invoke(app, KEY_SET).exit_code == 0
    with store_session() as store:
        assert store.project_explainability(hub.id) is not None
        assert store.project_explainability(api.id) is None

    assert runner.invoke(app, REGISTER).exit_code == 0
    assert machine == [PROJECT_KEY], "registered under the hub project's key, just attached"


@COMMANDS
def test_the_line_changes_neither_stdout_nor_the_exit_code(
    argv: list[str],
    hub: ProjectInfo,
    api: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against the same command with the line held back: the same stdout to the byte and
    the same exit code."""
    noted = runner.invoke(app, argv)
    plain = _unsaid(runner, monkeypatch, argv)

    assert (len(_said(noted.stderr)), _said(plain.stderr)) == (1, [])
    assert noted.stdout == plain.stdout
    assert noted.exit_code == plain.exit_code == 0


@pytest.mark.parametrize("argv", [STATUS, REGISTER], ids=["status", "register"])
def test_json_output_is_the_same_document_with_the_line_on_stderr(
    argv: list[str],
    hub: ProjectInfo,
    api: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A script reads stdout: no new field, and no line of prose in front of the document.
    The line is still said, on stderr, as ``team_project`` says its own warnings."""
    noted = runner.invoke(app, ["--json", *argv])
    plain = _unsaid(runner, monkeypatch, ["--json", *argv])

    assert noted.exit_code == plain.exit_code == 0, noted.output
    assert json.loads(noted.stdout) == json.loads(plain.stdout)
    assert noted.stdout == plain.stdout, "stdout is the document and nothing else"
    assert (_said(noted.stderr), _said(plain.stderr)) == ([LINE], [])


def test_a_red_status_keeps_its_exit_code_and_its_line(
    hub: ProjectInfo, api: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status`` exits 1 for one thing, tracing on and the proxy lane red. The line is
    not a second meaning for it."""
    _settings()  # tracing on
    monkeypatch.setattr(
        ops, "probe_proxy", lambda url, timeout=1.5: service.ProxyProbe(False, "refused (fake)")
    )

    noted = runner.invoke(app, STATUS)
    plain = _unsaid(runner, monkeypatch, STATUS)

    assert noted.exit_code == plain.exit_code == 1
    assert noted.stdout == plain.stdout
    assert (_said(noted.stderr), _said(plain.stderr)) == ([LINE], [])


def test_env_prints_only_its_exports_on_stdout_and_the_line_on_stderr(
    hub: ProjectInfo, api: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env``'s stdout is evaled: an eval would execute anything else that landed there."""
    _settings()  # tracing on
    monkeypatch.setattr(service, "probe_proxy", lambda url: service.ProxyProbe(True, "healthy"))
    argv = ["explainability", "env", "coder", "--session-id", SESSION]

    noted = runner.invoke(app, argv)
    plain = _unsaid(runner, monkeypatch, argv)

    assert noted.exit_code == plain.exit_code == 0, noted.output
    assert noted.stdout.startswith("export ") and noted.stdout == plain.stdout
    assert (_said(noted.stderr), _said(plain.stderr)) == ([LINE], [])


# --- and in no other -----------------------------------------------------------------------------


@COMMANDS
def test_with_no_hub_nothing_is_said(
    argv: list[str], api: ProjectInfo, tmp_path: Path, runner: CliRunner
) -> None:
    """Even with a ``project switch`` pin elsewhere, which ``fleet spawn`` follows and a
    launch ignores: the line is about a hub, and there is none."""
    pin_project(_project(tmp_path, "web").id)

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == []
    assert _key_project(runner) == api.id, "this checkout, as before"


@pytest.mark.parametrize("named", ["api", "hub"])
@COMMANDS
def test_with_a_project_named_nothing_is_said(
    argv: list[str], named: str, hub: ProjectInfo, api: ProjectInfo, runner: CliRunner
) -> None:
    """``--project`` is the answer the line points at. Named, there is nothing to add: not
    for the fleet's project, and not for the hub's own, which is the default said aloud."""
    with store_session() as store:
        store.onboard_project(hub)  # registered, so that it can be named
    expected = {"api": api.id, "hub": hub.id}[named]

    result = runner.invoke(app, [*argv, "--project", named])

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == []
    assert _key_project(runner, "--project", named) == expected


@pytest.mark.parametrize("own_root", [True, False], ids=["its-fleets-root", "an-inherited-hub"])
@COMMANDS
def test_inside_a_fleet_window_nothing_is_said(
    argv: list[str],
    own_root: bool,
    hub: ProjectInfo,
    api: ProjectInfo,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seat's command speaks for the seat's own project already (the fleet row's, #230),
    whether its window carries its fleet's own root as the hub, as ``fleet spawn`` sets it,
    or one its tmux server inherited. The pin elsewhere would make the line name ``web``."""
    if own_root:
        monkeypatch.setenv("AISQUARE_TEAM_HUB", str(api.root))
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _seat(api, "coder-1").id)
    pin_project(_project(tmp_path, "web").id)

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == []
    assert _key_project(runner) == api.id, "the seat's own project"


def test_a_relative_hub_is_no_hub_and_nothing_is_said(
    api: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``team_project`` ignores a relative hub, with its own warning, and answers this
    checkout: the command does not speak for a hub."""
    monkeypatch.setenv("AISQUARE_TEAM_HUB", "hub")

    result = runner.invoke(app, STATUS)

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == []
    assert _key_project(runner) == api.id


@COMMANDS
def test_when_the_hub_is_this_checkouts_own_project_the_line_names_no_other(
    argv: list[str], api: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn here with no ``-P`` joins the hub, so no other project is named; one spawned
    with ``-P`` still traces as its own, so whose project this is is still said."""
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(api.root))

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == [ANY_SEAT.replace("speaks for hub", "speaks for api")]
    assert _key_project(runner) == api.id


def test_a_fleet_id_that_names_no_row_is_not_a_fleet_window(
    hub: ProjectInfo, api: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ``AISQUARE_FLEET_AGENT`` resolves no row, so the hub answers
    (``team_project``) and the command speaks for the hub: the line is owed."""
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_no_such_row")

    result = runner.invoke(app, STATUS)

    assert result.exit_code == 0, result.output
    assert _said(result.stderr) == [LINE]
    assert _key_project(runner) == hub.id


# --- the line never costs the command ------------------------------------------------------------


def test_on_a_machine_with_no_store_none_is_created_and_the_line_names_no_project(
    isolated_home: Path,
    hub: ProjectInfo,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking which project a fleet spawn here would use opens the store, and opening one
    creates it. ``status`` and ``env`` are reads that create nothing (review of #170)."""
    _settings()
    healthy = service.ProxyProbe(True, "healthy", gateway="https://stg.example")
    monkeypatch.setattr(service, "probe_proxy", lambda url: healthy)
    monkeypatch.setattr(ops, "probe_proxy", lambda url, timeout=1.5: healthy)
    work = tmp_path / "api"
    work.mkdir()
    monkeypatch.chdir(work)

    for argv in (["--json", *STATUS], ["explainability", "env", "coder"]):
        result = runner.invoke(app, argv)
        assert _said(result.stderr) == [ANY_SEAT], argv
        assert not paths.db_path().exists(), argv
    assert _key_project(runner) == hub.id


def test_a_damaged_store_costs_the_name_and_never_the_command(
    hub: ProjectInfo, api: ProjectInfo, runner: CliRunner
) -> None:
    """The bar ``status`` and ``env`` already hold (review of #170): a ``context.db`` that
    cannot be read costs the project's key, never the command. It costs the line the name
    of the project a spawn here joins, and nothing else."""
    assert _said(runner.invoke(app, STATUS).stderr) == [LINE], "the control: named while it reads"
    paths.db_path().write_bytes(b"this is not a SQLite database, and never was one")

    result = runner.invoke(app, ["--json", *STATUS])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert (payload["key_source"], payload["key_project"]) == ("file", hub.id)
    assert _said(result.stderr) == [ANY_SEAT] and "Traceback" not in result.output


def test_a_fleet_project_that_cannot_be_resolved_costs_the_name_and_not_the_project(
    hub: ProjectInfo, api: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status`` and ``register`` read their project inside a guard that answers "no
    project" for any failure. A failure of this lookup must not reach it: the command
    would then resolve the machine's key in place of the hub project's."""

    def unreadable(ref: str | None = None, *, cwd: Path | None = None) -> ProjectInfo:
        raise OSError("state.json could not be read")

    monkeypatch.setattr(fleet_service, "resolve_project", unreadable)

    result = runner.invoke(app, ["--json", *STATUS])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["key_project"] == hub.id
    assert _said(result.stderr) == [ANY_SEAT] and "Traceback" not in result.output


def test_a_line_that_cannot_be_worked_out_costs_the_line_and_not_the_project(
    hub: ProjectInfo, api: ProjectInfo, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line itself is decoration too: a failure while it is worked out, past the name's
    own guard, is not a "no project" for ``status`` either."""

    def broken() -> Path | None:
        raise OSError("the environment could not be read")

    # ``team_project`` reads the variable itself, so only the line's own question breaks.
    monkeypatch.setattr(orchestrator, "team_hub", broken)

    result = runner.invoke(app, ["--json", *STATUS])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["key_project"] == hub.id
    assert _said(result.stderr) == [] and "Traceback" not in result.output


# --- the same sentence where the option is described ----------------------------------------------


def _options(*path: str) -> list[Any]:
    """The click parameters of one command, off the built tree (no help renderer in the loop)."""
    node: Any = get_command(app)
    for name in path:
        node = node.commands[name]
    return list(node.params)


@pytest.mark.parametrize(
    "path",
    [("key", "set"), ("key", "show"), ("key", "clear"), ("status",), ("register",), ("env",)],
    ids=" ".join,
)
def test_the_project_options_help_says_what_the_line_says(path: tuple[str, ...]) -> None:
    [option] = [p for p in _options("explainability", *path) if "--project" in p.opts]

    assert "the one a launch here joins" in option.help, "the default, as before"
    assert "fleet seats trace as their own project" in option.help
