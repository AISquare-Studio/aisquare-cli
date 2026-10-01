"""The captain ships behind one switch, OFF by default (card tsk_01m3qvghrpgg).

The owner, 2026-09-29: "feature flag off captain for now, allow experimentally".
``core.experimental.captain_enabled`` is the one reader: ``AISQUARE_EXPERIMENTAL_CAPTAIN``
wins over ``[experimental] captain`` in config.toml either way. OFF, every surface refuses
or hides: the CLI, the fleet UI's insignia, captain row and view, its ui receiver, the
bundled persona, the voice page; doctor says so in one ok row. ON is the captain as it
was: the rest of the suite runs with it on (``tests/conftest.py``, ``captain_on``), and
each surface here is also driven on, as the control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from typer.testing import CliRunner

from aisquare.cli import captain as captain_cli
from aisquare.cli import captain_voice
from aisquare.cli.app import app
from aisquare.cli.ui.views.captain import CaptainView
from aisquare.core import personas as core_personas
from aisquare.core.config import load_config
from aisquare.core.personas import PersonaError
from aisquare.models import CheckStatus, ProjectInfo
from aisquare.services import diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import personas as personas_service
from aisquare.services.captain import actions, brain, voice
from aisquare.services.captain import state as captain_state
from tests import test_ui_receiver as receiver_suite
from tests import test_ui_shell as ui_suite
from tests import test_ui_spawn as spawn_suite
from tests.test_ui_shell import Script, drive, fleet_app, seed, status

# The suites' fixtures, bound here so pytest finds them for this module's tests.
no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script
ui_path = receiver_suite.ui_path
git_project = spawn_suite.git_project
spawns = spawn_suite.spawns

ENV = "AISQUARE_EXPERIMENTAL_CAPTAIN"
OFF_LINE = (
    "the captain is experimental; turn it on with aisquare config set experimental.captain "
    "true, or AISQUARE_EXPERIMENTAL_CAPTAIN=1"
)
"""The card's one line, word for word."""


def _enabled() -> bool:
    # Imported here, not at the top: on a tree without the switch each pin fails alone.
    from aisquare.core import experimental

    return experimental.captain_enabled()


@pytest.fixture
def off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: no variable and no config key. The suite runs with it ON."""
    monkeypatch.delenv(ENV, raising=False)


class Reached(Exception):
    """A captain effect the command got to: raised by the stand-ins below, never run."""


@pytest.fixture(autouse=True)
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every effect a captain command has, replaced by a stand-in that records it and stops.

    Off, a command must reach none of them. On, it must reach its own: that is the
    control, and it runs nothing real. The bare command's ``brain.find`` stands in front
    of ``brain.start``, which spawned a real ``claude`` on the owner's socket once, when a
    routing bite fell through to it (board 13220, ``tests/test_captain_voice_cli.py``).
    """
    seen: list[str] = []

    def stand_in(name: str) -> Any:
        def effect(*args: object, **kwargs: object) -> Any:
            seen.append(name)
            raise Reached(name)

        return effect

    monkeypatch.setattr(brain, "find", stand_in("brain.find"))
    monkeypatch.setattr(brain, "start", stand_in("brain.start"))
    monkeypatch.setattr(brain, "say", stand_in("brain.say"))
    monkeypatch.setattr(fleet_service, "spawn", stand_in("fleet.spawn"))
    monkeypatch.setattr(voice, "serve", stand_in("voice.serve"))
    monkeypatch.setattr(actions, "run_stdio", stand_in("actions.run_stdio"))
    monkeypatch.setattr(actions, "perform", stand_in("actions.perform"))
    monkeypatch.setattr(actions, "perform_read", stand_in("actions.perform_read"))
    monkeypatch.setattr(captain_voice, "voice_dependency_error", lambda: None)
    monkeypatch.setattr(captain_cli, "dependency_error", lambda: None)
    return seen


# --- the switch -------------------------------------------------------------------------------


def test_the_captain_is_off_by_default(off: None) -> None:
    assert load_config().experimental.captain is False
    assert _enabled() is False


def test_aisquare_config_turns_it_on_and_off(off: None, runner: CliRunner) -> None:
    turned = runner.invoke(app, ["config", "set", "experimental.captain", "true"])
    assert turned.exit_code == 0, turned.output
    assert _enabled() is True
    read = runner.invoke(app, ["config", "get", "experimental.captain"])
    assert read.exit_code == 0 and read.output.strip().lower().endswith("true"), read.output
    assert runner.invoke(app, ["config", "set", "experimental.captain", "false"]).exit_code == 0
    assert _enabled() is False


@pytest.mark.parametrize(
    ("configured", "variable", "on"),
    [
        (False, "1", True),
        (False, "true", True),
        (False, "yes", True),
        (False, "on", True),
        (True, "0", False),
        (True, "false", False),
        (True, "ture", False),  # a typo lands in the safe state
        (True, "", True),  # empty defers to the config
        (False, "", False),
    ],
)
def test_the_variable_wins_over_the_config_either_way(
    off: None,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    variable: str,
    on: bool,
) -> None:
    written = runner.invoke(app, ["config", "set", "experimental.captain", str(configured)])
    assert written.exit_code == 0, written.output
    monkeypatch.setenv(ENV, variable)
    assert _enabled() is on


def test_an_unreadable_config_opts_into_nothing(off: None, isolated_home: Path) -> None:
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "config.toml").write_text("[experimental\ncaptain = true\n")
    assert _enabled() is False


# --- the CLI ----------------------------------------------------------------------------------

EVERY_CAPTAIN_COMMAND = [
    [],
    ["what is up"],
    ["say", "what is up"],
    ["chat"],
    ["serve", "--stdio"],
    ["voice"],
    ["--voice"],
    ["voice", "--show-token"],
    ["attention"],
    ["next"],
    ["resolve", "q_1"],
    ["snooze", "q_1"],
    ["since", "coder-1"],
    ["log"],
    ["uav"],
    ["wololo"],
    ["bt"],
    ["actions"],
    ["say", "--help"],
]


@pytest.mark.parametrize("argv", EVERY_CAPTAIN_COMMAND, ids=lambda argv: " ".join(argv) or "bare")
def test_every_captain_command_exits_2_with_one_line_when_off(
    off: None, runner: CliRunner, reached: list[str], argv: list[str]
) -> None:
    result = runner.invoke(app, ["captain", *argv])
    assert (result.exit_code, result.stdout, result.stderr) == (2, "", f"✗ {OFF_LINE}\n")
    assert reached == [], "off, no captain effect runs: no start, no say, no server, no page"


def test_under_json_the_refusal_is_one_object(off: None, runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "captain", "attention"])
    assert result.exit_code == 2
    assert json.loads(result.stdout) == {"error": "captain_off", "hint": OFF_LINE}


@pytest.mark.parametrize(
    ("argv", "effect"),
    [
        ([], "brain.find"),
        (["say", "what is up"], "brain.say"),
        (["serve", "--stdio"], "actions.run_stdio"),
        (["voice"], "voice.serve"),
        (["attention"], "actions.perform"),
    ],
)
def test_on_each_command_reaches_its_own_effect(
    runner: CliRunner, reached: list[str], argv: list[str], effect: str
) -> None:
    result = runner.invoke(app, ["captain", *argv])
    assert isinstance(result.exception, Reached), (result.exit_code, result.output)
    assert reached == [effect]


# --- the fleet UI -----------------------------------------------------------------------------


def _a_home_with_a_captain(tmp_path: Path, script: Script) -> str:
    seed(tmp_path, ("prj_aaa", "alpha", "amber-otter"))
    home = captain_state.home_project()
    captain = status(home.id, "captain", "captain", "waiting")
    script[home.id] = [captain]
    script["prj_aaa"] = [status("prj_aaa", "coder-1", "coder", "working")]
    return captain.agent.id


def _captain_surfaces(tmp_path: Path, script: Script) -> tuple[int, int, bool, int, bool]:
    """The insignia, the captain section, the home board, captain views, alpha's card."""
    _a_home_with_a_captain(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[int, int, bool, int, bool]:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        home = app.snapshot.home if app.snapshot is not None else None
        return (
            len(app.query("#captain-button")),
            len(app.query("#captain-section")),
            home is not None,
            len(app.query(CaptainView)),
            bool(app.query("#card-prj_aaa")),
        )

    return drive(body)


def test_the_fleet_ui_has_no_insignia_captain_row_or_home_when_off(
    off: None, tmp_path: Path, script: Script
) -> None:
    button, section, home, views, alpha = _captain_surfaces(tmp_path, script)
    assert (button, section, home, views) == (0, 0, False, 0)
    assert alpha, "the projects are listed as ever"


def test_the_fleet_ui_shows_the_insignia_and_the_captain_when_on(
    tmp_path: Path, script: Script
) -> None:
    button, section, home, _views, alpha = _captain_surfaces(tmp_path, script)
    assert (button, section, home, alpha) == (1, 1, True, True)


def test_a_remembered_captain_opens_no_captain_view_when_off(
    off: None, tmp_path: Path, script: Script
) -> None:
    captain_id = _a_home_with_a_captain(tmp_path, script)
    home = captain_state.home_project()
    from aisquare.core.store import store_session

    with store_session() as store:
        store.set_ui_state("fleet.selected", f"agent:{home.id}/{captain_id}")

    async def body(pilot: Pilot[None]) -> int:
        app = fleet_app(pilot)
        app.refresh_data()
        await pilot.pause()
        await pilot.pause()
        return len(app.query(CaptainView))

    assert drive(body) == 0


@receiver_suite.UNIX_SOCKETS
def test_the_ui_receiver_refuses_every_captain_action_when_off(
    off: None, tmp_path: Path, script: Script, ui_path: Path
) -> None:
    receiver_suite.fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> tuple[list[dict[str, Any]], str | None]:
        app = fleet_app(pilot)
        replies = [
            await receiver_suite.send(pilot, ui_path, "select_project", "alpha"),
            await receiver_suite.send(pilot, ui_path, "open_spawn", "alpha"),
            await receiver_suite.send(pilot, ui_path, "copy_row", "alpha"),
        ]
        return replies, app.sidebar.selected_key

    replies, selected = drive(body)
    assert replies == [{"ok": False, "said": OFF_LINE}] * 3
    assert selected != "project:prj_aaa", "nothing the actions asked for ran"


@receiver_suite.UNIX_SOCKETS
def test_the_ui_receiver_runs_a_captain_action_when_on(
    tmp_path: Path, script: Script, ui_path: Path
) -> None:
    receiver_suite.fleet(tmp_path, script)

    async def body(pilot: Pilot[None]) -> dict[str, Any]:
        fleet_app(pilot)
        return await receiver_suite.send(pilot, ui_path, "select_project", "alpha")

    assert drive(body) == {"ok": True, "said": "selected alpha"}


# --- the persona ------------------------------------------------------------------------------


def test_the_bundled_captain_persona_is_absent_when_off(off: None, runner: CliRunner) -> None:
    names = [persona.name for persona in core_personas.catalogue(None)[0]]
    assert "captain" not in names and names, "the other bundled personas stay"
    with pytest.raises(PersonaError) as refused:
        core_personas.resolve("captain")
    assert refused.value.code == "unknown_persona"
    with pytest.raises(PersonaError):
        personas_service.locate("captain", None)
    listed = runner.invoke(app, ["persona", "list"])
    assert listed.exit_code == 0 and "captain" not in listed.output, listed.output


def test_the_bundled_captain_persona_is_there_when_on() -> None:
    assert "captain" in [persona.name for persona in core_personas.catalogue(None)[0]]
    assert core_personas.resolve("captain").layer == "bundled"


def test_a_captain_persona_of_the_users_own_is_never_hidden(
    off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    personas_service.new("captain", layer="user", root=None)
    persona = core_personas.resolve("captain")
    assert persona.layer == "user"
    assert personas_service.shadows(persona, None) == [], "off, no bundled captain beneath it"
    monkeypatch.setenv(ENV, "1")
    assert personas_service.shadows(persona, None) == ["bundled"]


def _persona_options(project: ProjectInfo) -> list[str]:
    async def scenario(pilot: Pilot[None], host: Any, dialog: Any) -> list[str]:
        field = spawn_suite.select(dialog, "persona")
        return [str(value) for _, value in field._options]

    return spawn_suite.drive(project, scenario)


def test_the_spawn_dialogs_persona_picker_has_no_captain_when_off(
    off: None, git_project: ProjectInfo
) -> None:
    options = _persona_options(git_project)
    assert "captain" not in options and "skeptic" in options, options


def test_the_spawn_dialogs_persona_picker_has_the_captain_when_on(
    git_project: ProjectInfo,
) -> None:
    assert "captain" in _persona_options(git_project)


# --- doctor -----------------------------------------------------------------------------------


def test_doctor_shows_one_ok_captain_row_when_off(off: None) -> None:
    rows = [check for check in diagnostics.doctor() if check.name == "captain"]
    assert len(rows) == 1, rows
    (row,) = rows
    assert row.status is CheckStatus.ok
    assert row.detail.startswith("off (experimental)"), row.detail
    assert "aisquare config set experimental.captain true" in row.detail


def test_doctor_has_no_captain_row_when_on() -> None:
    assert [check for check in diagnostics.doctor() if check.name == "captain"] == []


# --- the docs ---------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("page", ["CHANGELOG.md", "docs/captain.md", "README.md"])
def test_the_docs_say_it_is_experimental_and_how_to_turn_it_on(page: str) -> None:
    text = (REPO / page).read_text(encoding="utf-8")
    assert "experimental" in text
    assert "aisquare config set experimental.captain true" in text
    assert ENV in text
