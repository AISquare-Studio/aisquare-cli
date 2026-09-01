"""$HOME never becomes a project by accident, and the data home is not a marker.

Two related traps, both found setting aisquare up on a real machine:

- ``remember`` from a directory outside any repository fell back to treating
  that directory (often ``$HOME`` itself) as a project, so the entry joined a
  pool no session ever resolves to on purpose.
- ``_ROOT_MARKERS`` includes ``.aisquare``, and the DATA home (``~/.aisquare``)
  matches it — so every non-git directory under ``$HOME`` resolved to ``$HOME``
  as its project, and a docs-only workspace like ``~/SOC2`` could never be its
  own project at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.workspace import find_project_root


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A $HOME whose .aisquare is the DATA home — the real-machine layout."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AISQUARE_HOME", str(home / ".aisquare"))
    (home / ".aisquare").mkdir()
    return home


def test_remember_at_home_refuses_the_project_pool(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fake_home)
    result = runner.invoke(app, ["remember", "oops", "--project"])
    assert result.exit_code == 1, result.output
    assert "refusing to treat" in result.output
    assert "--user" in result.output, "the refusal must say what to do instead"


def test_remember_at_home_still_takes_user_and_stream_entries(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fake_home)
    result = runner.invoke(app, ["remember", "fine globally", "--user"])
    assert result.exit_code == 0, result.output
    runner.invoke(app, ["stream", "new", "soc2"])
    result = runner.invoke(app, ["remember", "fine in a stream", "--stream", "soc2"])
    assert result.exit_code == 0, result.output


def test_stream_add_refuses_a_home_resolved_member(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member that resolves to $HOME is the marker-walk trap, refused loudly.

    ``AISQUARE_HOME`` is pointed elsewhere so ``$HOME/.aisquare`` acts as an
    ordinary marker — the layout where ``stream add ~/docs`` silently enrolled
    ``$HOME`` itself during the first live smoke test of this feature.
    """
    monkeypatch.setenv("AISQUARE_HOME", str(fake_home / "elsewhere"))
    docs = fake_home / "SOC2"
    docs.mkdir()
    runner.invoke(app, ["stream", "new", "soc2"])
    result = runner.invoke(app, ["stream", "add", "soc2", str(docs)])
    assert result.exit_code == 1, result.output
    assert "refusing to treat" in result.output
    assert "mkdir" in result.output, "the refusal must say how to make the dir a project"


def test_the_data_home_is_not_a_project_marker(fake_home: Path) -> None:
    """A non-git dir under $HOME is its own project, not $HOME's."""
    docs_only = fake_home / "SOC2"
    docs_only.mkdir()
    assert find_project_root(docs_only) == docs_only.resolve()


def test_a_real_marker_still_marks(fake_home: Path) -> None:
    """Only the data home is excluded — a project's own .aisquare marker works."""
    project = fake_home / "compliance"
    (project / ".aisquare").mkdir(parents=True)
    inside = project / "evidence"
    inside.mkdir()
    assert find_project_root(inside) == project.resolve()
