"""A relative ``AISQUARE_TEAM_HUB`` turns the shared board into "wherever I am".

The setting exists so an execution spanning several repositories can pin every
session to ONE hub directory. ``core.orchestrator.team_project`` reads it first
and overrides everything — and ``Path('./').resolve()`` is the process cwd, so a
relative value inverts the feature into its opposite: the board follows the
caller instead of being fixed.

Nobody can mean that. "The hub is wherever I am standing" is what NOT setting
the variable does, and the resolver already does it correctly — ``git_common_root``
reads ``--git-common-dir`` specifically so a linked worktree resolves to its
principal repo. That line sits directly AFTER the override that never lets it
run. So a relative hub is always a mistake, never a preference, which is what
makes ignoring it safe.

MEASURED COST, this shift: it caused both board incidents. A `note list` typed
in a worktree addressed a different project, and a documented recovery command
returned nothing from a worktree — which was nearly published as "the recovery
returns nothing" and would have removed a working recovery from the morning
handoff. Every session on this team works from a worktree by standing
instruction, so while this value is set, every board read is cwd-dependent.

The override also ignores the ``cwd`` ARGUMENT, not just the caller's location —
that is what the first test pins, because it is the sharpest statement of the
defect: you can ask this function about a specific directory and, with a
relative hub, get an answer about somewhere else entirely.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aisquare.core import orchestrator


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git checkout with a subdirectory to ask about."""
    root = tmp_path / "repo"
    (root / "deep" / "nested").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the one-shot warning unfired."""
    # raising=False so this fixture works BEFORE the attribute exists: without
    # it every test errors on the fixture and the test-first run says nothing
    # about whether the assertions would have failed.
    monkeypatch.setattr(orchestrator, "_WARNED_HUBS", set(), raising=False)


def test_a_relative_hub_does_not_change_the_answer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property, stated as an equality against the correct resolution."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    correct = orchestrator.team_project(repo / "deep" / "nested")

    monkeypatch.setenv("AISQUARE_TEAM_HUB", "./")
    with_relative = orchestrator.team_project(repo / "deep" / "nested")

    assert with_relative.root == correct.root
    assert with_relative.id == correct.id


def test_the_override_ignores_even_the_directory_it_was_asked_about(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sharpest form: the answer did not depend on the question.

    Under the defect the hub resolves against the PROCESS cwd, so asking about
    ``repo/deep/nested`` returns wherever the test process happens to be — a
    directory the caller never mentioned.
    """
    monkeypatch.setenv("AISQUARE_TEAM_HUB", "./")
    monkeypatch.chdir(tmp_path)

    answer = orchestrator.team_project(repo / "deep" / "nested")

    assert answer.root != tmp_path.resolve(), (
        "the resolver answered about the process cwd rather than the directory it was asked about"
    )
    assert answer.root == repo.resolve()


def test_it_says_which_variable_and_why(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail open WITH THE REASON: silence here is how it survived thirty hours."""
    monkeypatch.setenv("AISQUARE_TEAM_HUB", "./")

    orchestrator.team_project(repo)

    err = capsys.readouterr().err
    assert "AISQUARE_TEAM_HUB" in err, err
    assert "./" in err, err
    assert "relative" in err.lower(), err


def test_an_absolute_hub_is_still_honoured(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control in the other direction — this must not become "hubs are ignored".

    Without it the safe-looking fix is to drop the override entirely, and the
    feature it exists for — several repositories, one board — goes with it.
    """
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))

    answer = orchestrator.team_project(repo)

    assert answer.root == hub.resolve()
    assert capsys.readouterr().err == ""


def test_a_tilde_hub_is_absolute_after_expansion(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``~/hub`` is not a relative path once expanded, and must not be refused."""
    monkeypatch.setenv("AISQUARE_TEAM_HUB", "~")

    answer = orchestrator.team_project(repo)

    assert answer.root == Path("~").expanduser().resolve()
    assert capsys.readouterr().err == ""


def test_an_unset_hub_says_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The quiet case, asserted — a warning on every invocation is noise."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)

    orchestrator.team_project(repo)

    assert capsys.readouterr().err == ""


def test_the_warning_fires_once_not_once_per_lookup(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Several call sites resolve the board within one command."""
    monkeypatch.setenv("AISQUARE_TEAM_HUB", "./")

    for _ in range(3):
        orchestrator.team_project(repo)

    assert capsys.readouterr().err.lower().count("relative") == 1


def test_a_relative_path_resolves_to_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One line, because this single fact is the whole defect.

    Left unwritten, the next reader has to rediscover why a hub that looks like
    a directory behaves like a variable.
    """
    monkeypatch.chdir(tmp_path)

    assert Path("./").resolve() == tmp_path.resolve()


def test_the_warning_never_reaches_stdout_or_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-open through the CLI, not just through the resolver.

    A warning printed on the wrong stream turns every `--json` consumer into a
    parse error, which would make this fix worse than the defect it repairs —
    the defect at least produced valid JSON about the wrong board.
    """
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", "./")

    result = CliRunner().invoke(app, ["--json", "team", "status"])

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)
    assert "AISQUARE_TEAM_HUB" not in result.stdout, result.stdout
