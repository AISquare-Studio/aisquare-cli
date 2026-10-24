"""``aisquare code``: the CLI, the MCP tool, the doctor row and the onboard line (#285).

Run from inside a real git checkout, as an agent runs them: the project is the one the
working directory is in.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import code_index
from aisquare.core.config import load_config, save_config
from aisquare.services import code as code_service

SOURCE = """\
def parse_duration(text):
    return float(text)


def parse_window(text):
    return parse_duration(text)


class Gate:
    def wait(self, seconds):
        return parse_duration(str(seconds))
"""


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "timing.py").write_text(SOURCE, encoding="utf-8")
    for args in (("init", "-q", "-b", "main"), ("add", "-A")):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    monkeypatch.chdir(root)
    return root


def _json(runner: CliRunner, *args: str) -> dict[str, Any]:
    result = runner.invoke(app, ["--json", "code", *args])
    assert result.exit_code == 0, result.output
    loaded = json.loads(result.output)
    assert isinstance(loaded, dict)
    return loaded


def test_every_query_before_an_index_says_how_to_build_one(
    runner: CliRunner, checkout: Path
) -> None:
    for args in (["find", "x"], ["refs", "x"], ["similar", "x"]):
        result = runner.invoke(app, ["--json", "code", *args])
        assert result.exit_code == 1
        error = json.loads(result.output)
        assert error["error"] == "no_index"
    assert _json(runner, "index", "--status")["exists"] is False


def test_index_then_find_refs_and_similar_answer_in_json(runner: CliRunner, checkout: Path) -> None:
    built = _json(runner, "index")
    assert built["files"] == 1 and built["symbols"] == 4 and built["full"] is True

    found = _json(runner, "find", "parse_duration")
    [symbol] = found["symbols"]
    assert symbol == {
        "name": "parse_duration",
        "qualname": "parse_duration",
        "kind": "function",
        "path": "timing.py",
        "line": 1,
        "end_line": 2,
        "signature": "def parse_duration(text)",
        "parent": None,
        "arity": 1,
    }
    refs = _json(runner, "refs", "parse_duration", "--defs")
    assert [(r["path"], r["line"]) for r in refs["refs"]] == [("timing.py", 6), ("timing.py", 11)]
    assert [d["line"] for d in refs["definitions"]] == [1]

    similar = _json(runner, "similar", "parse_duration")
    assert similar["anchor"]["line"] == 1
    assert [s["name"] for s in similar["similar"]] == ["parse_window"]
    assert similar["similar"][0]["shared"] == ["parse"]

    status = _json(runner, "index", "--status")
    assert status["exists"] and status["symbols"] == 4 and status["stale"] is False


def test_the_human_lines_name_path_line_kind_and_signature(
    runner: CliRunner, checkout: Path
) -> None:
    runner.invoke(app, ["code", "index"])
    result = runner.invoke(app, ["code", "find", "Gate.wait"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "timing.py:10  method   def wait(self, seconds)"
    assert (
        "no definition of 'nothing_like_it'"
        in runner.invoke(app, ["code", "find", "nothing_like_it"]).output
    )


def test_kind_is_checked(runner: CliRunner, checkout: Path) -> None:
    result = runner.invoke(app, ["code", "find", "x", "--kind", "variable"])
    assert result.exit_code == 2


def test_the_mcp_tool_is_there_only_with_an_index(checkout: Path) -> None:
    pytest.importorskip("mcp")
    import anyio

    from aisquare.services.mcp_server import build_server, code_lookup

    names = {tool.name for tool in anyio.run(build_server().list_tools)}
    assert "code_lookup" not in names
    code_service.build_index()
    names = {tool.name for tool in anyio.run(build_server().list_tools)}
    assert "code_lookup" in names
    answer = json.loads(code_lookup("parse_duration", "refs"))
    assert [r["line"] for r in answer["refs"]] == [6, 11]
    assert json.loads(code_lookup("parse_window", "similar"))["similar"][0]["name"] == (
        "parse_duration"
    )


def test_the_doctor_row_is_ok_with_or_without_an_index(checkout: Path) -> None:
    from aisquare.core import paths
    from aisquare.services import diagnostics

    paths.ensure_home()
    row = diagnostics._check_code_index(checkout)
    assert row.status.value == "ok" and "aisquare code index" in row.detail
    code_service.build_index()
    row = diagnostics._check_code_index(checkout)
    assert row.status.value == "ok" and row.detail.startswith("4 symbols in 1 files (ast-")


def test_onboard_builds_the_index_unless_code_is_off(runner: CliRunner, checkout: Path) -> None:
    result = runner.invoke(app, ["--json", "project", "onboard"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["code_index"].startswith("4 symbols in 1 files")
    assert code_index.exists(code_service.project_here().id)

    config = load_config()
    config.code.enabled = False
    save_config(config)
    result = runner.invoke(app, ["--json", "project", "onboard"])
    assert json.loads(result.output)["code_index"] is None


def test_the_suite_never_indexes_its_own_checkout(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that runs ``init`` or ``project onboard`` where it stands onboards this
    repository: a full index of it per test took rc/p2's CI from ~20 to 60-90 minutes.
    ``tests/conftest.py`` builds an index only under pytest's temp dirs."""
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    result = runner.invoke(app, ["--json", "project", "onboard"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["code_index"] is None
    assert not code_index.exists(code_service.project_here().id)
