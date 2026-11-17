"""``rlx --help``: rlx's homepage terminal opens on it word for word (owner, 2026-11-17, 0.10.1).

- The experimental captain is not in its Commands panel, on or off. ``rlx captain …`` still
  runs and ``rlx captain --help`` still prints: every verb on and off is pinned in
  ``test_captain_flag``, and every command's help in ``test_help``.
- The ``ui`` line says what the UI is after a colon, not an em dash.

Drawn on one console on every OS, as ``test_release_stage``'s help goldens are: 100 columns,
no colour, a modern terminal's boxes (Windows' legacy console takes a column off, and CI
forces a terminal, which styles the page).
"""

from __future__ import annotations

import re

import pytest
import typer.rich_utils
from rich.console import Console
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import experimental
from tests.cli_tree import root_command
from tests.rendered import plain

UI_LINE = "Open the rlx UI: every project, agent and session in one view."


@pytest.fixture(autouse=True)
def help_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        typer.rich_utils,
        "_get_rich_console",
        lambda stderr=False: Console(
            width=100,
            color_system=None,
            force_terminal=False,
            legacy_windows=False,
            highlight=False,
            stderr=stderr,
        ),
    )


@pytest.fixture(params=["captain on", "captain off"])
def switch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "captain on":
        monkeypatch.setenv(experimental.CAPTAIN_ENV, "1")
    else:
        monkeypatch.delenv(experimental.CAPTAIN_ENV, raising=False)
    return str(request.param)


def _page(*argv: str) -> str:
    result = CliRunner().invoke(app, [*argv, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def _commands(page: str) -> dict[str, str]:
    """The Commands panel: each command's name and its help, a wrapped help joined."""
    panel = page.split("─ Commands ─", 1)[1].split("╰", 1)[0]
    rows: dict[str, str] = {}
    name = ""
    for line in panel.splitlines()[1:]:
        named = re.match(r"│ (\S+) ", line)
        if named:
            name = named.group(1)
            rows[name] = plain(line[named.end() :].rstrip("│"))
        elif name:
            rows[name] += " " + plain(line.strip("│"))
    return rows


@pytest.mark.usefixtures("switch")
def test_the_commands_panel_lists_no_captain() -> None:
    page = _page()
    rows = _commands(page)

    assert {"init", "ui", "fleet", "persona", "explainability"} <= rows.keys(), "read the panel"
    assert "captain" not in rows
    assert "captain" not in page, "the word is nowhere on the page"


def test_the_captain_runs_and_prints_its_own_help(switch: str) -> None:
    """Hidden from the panel only: the group resolves, and its page is its own, verbs listed."""
    captain = root_command().commands.get("captain")
    assert captain is not None and captain.hidden is True

    page = plain(_page("captain"))
    assert "captain [OPTIONS] COMMAND [ARGS]..." in page, page
    assert "The captain: the home-level agent that runs every project's fleet for you." in page
    for verb in ("say", "chat", "attention", "actions", "voice"):
        assert f"│ {verb} " in page, verb

    ran = CliRunner().invoke(app, ["captain", "actions"])
    if switch == "captain on":
        assert ran.exit_code == 0 and "approve_prompt" in ran.stdout, ran.output
    else:  # its own refusal, not click's "No such command"
        assert ran.exit_code == 2 and "the captain is experimental" in ran.stderr, ran.output


def test_the_ui_line_says_what_the_ui_is_after_a_colon() -> None:
    assert _commands(_page())["ui"] == UI_LINE
    assert UI_LINE in plain(_page("ui"))
