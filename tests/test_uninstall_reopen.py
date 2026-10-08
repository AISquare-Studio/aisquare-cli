"""``aisquare uninstall --reopen``: asq's Uninstall button brings asq back unless something went.

Uninstall sits beside Update in asq's Doctor, and its question defaults to No, so a
mis-click or a "no" must not cost the asq session. Here, at a terminal with Enter
pressed, asq reopens after a "no", after "nothing to remove", and after a refusal it
has said in full; it never reopens after a removal, whose last step is the package.
Built on #253's own fixtures, every installer seam closed (``no_real_installer``).
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli import install as install_cli
from aisquare.cli.app import app
from aisquare.core import selfcli
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import
from tests.test_lifecycle_uninstall import (  # noqa: F401 — `tool` and `world` are fixtures
    Tool,
    World,
    _hooked,
    _initialised,
    _live_agent,
    tool,
    world,
)

_ENTER = "Press Enter to go back to asq "

#: Taken at import, before ``no_real_installer`` closes it, and never called.
_REAL_EXEC_SELF = selfcli.exec_self


@pytest.fixture
def reopened(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """At a terminal, Enter pressed: each Enter prompt and each exec, in order."""
    seen: list[tuple[str, list[str]]] = []

    def enter(prompt: str = "") -> str:
        seen.append(("prompt", [prompt]))
        return ""

    monkeypatch.setattr(install_cli, "_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(builtins, "input", enter)
    monkeypatch.setattr(selfcli, "exec_self", lambda args: seen.append(("exec", list(args))))
    return seen


def test_a_no_brings_asq_back_and_removes_nothing(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    world: World,  # noqa: F811
    isolated_agent_home: Path,
    reopened: list[tuple[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hooked(isolated_agent_home / ".claude", tool.script)
    monkeypatch.setattr("aisquare.cli.install.typer.confirm", lambda *_a, **_k: False)

    result = runner.invoke(app, ["uninstall", "--reopen"])

    assert result.exit_code == 0 and "nothing removed" in result.stdout, result.output
    assert reopened == [("prompt", [_ENTER]), ("exec", ["ui"])], "Enter first, then asq"
    assert world.events == []


def test_nothing_to_remove_brings_asq_back(
    runner: CliRunner,
    world: World,  # noqa: F811
    reopened: list[tuple[str, list[str]]],
) -> None:
    """The suite's own install is editable, which aisquare does not remove itself: with no
    hooks either, there is nothing to ask about."""
    result = runner.invoke(app, ["uninstall", "--reopen"])

    assert "nothing for aisquare to remove here" in result.stdout, result.output
    assert reopened == [("prompt", [_ENTER]), ("exec", ["ui"])]


def test_a_refusal_is_read_and_then_asq_comes_back(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    world: World,  # noqa: F811
    isolated_agent_home: Path,
    tmp_path: Path,
    reopened: list[tuple[str, list[str]]],
) -> None:
    """Live fleet agents refuse the uninstall; the plan says why and how to stop them."""
    _initialised(runner, tmp_path)
    _live_agent(tmp_path / "repo")
    _hooked(isolated_agent_home / ".claude", tool.script)

    result = runner.invoke(app, ["uninstall", "--reopen"])

    assert result.exit_code == 1
    assert "aisquare fleet shutdown --all --yes" in result.output
    assert reopened == [("prompt", [_ENTER]), ("exec", ["ui"])] and world.events == []


def test_a_removal_never_comes_back(
    runner: CliRunner,
    tool: Tool,  # noqa: F811
    world: World,  # noqa: F811
    isolated_agent_home: Path,
    reopened: list[tuple[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: a "yes" removes the hooks and then the package, the last step, so there is
    no asq to reopen; without --reopen a "no" stays in the shell too."""
    _hooked(isolated_agent_home / ".claude", tool.script)
    monkeypatch.setattr("aisquare.cli.install.typer.confirm", lambda *_a, **_k: False)
    plain_no = runner.invoke(app, ["uninstall"])
    monkeypatch.setattr("aisquare.cli.install.typer.confirm", lambda *_a, **_k: True)
    removed = runner.invoke(app, ["uninstall", "--reopen"])

    assert plain_no.exit_code == 0 and removed.exit_code == 0, removed.output
    assert world.events[-1][0] == "package", world.events
    assert reopened == []


def test_the_installer_fixture_closes_the_exec_that_reopens_asq(
    no_real_installer: list[str],  # noqa: F811 — the fixture's record of what was reached
) -> None:
    """A test that forgot to stub it would replace the pytest process with the UI, so the
    real one is never called here: the check that it is closed comes first."""
    assert selfcli.exec_self is not _REAL_EXEC_SELF, "the real exec is open to every test"
    with pytest.raises(AssertionError):
        selfcli.exec_self(["ui"])

    assert no_real_installer == ["selfcli.exec_self"]
    no_real_installer.clear()  # reached on purpose: the fixture's teardown checks the record
