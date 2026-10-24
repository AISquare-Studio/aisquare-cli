"""``aisquare code``: the CLI, the MCP tool, the doctor row and the onboard line (#285).

Run from inside a real git checkout, as an agent runs them: the project is the one the
working directory is in.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import code_index, locking
from aisquare.core.config import load_config, save_config
from aisquare.services import code as code_service
from aisquare.services import hooks as hooks_service
from tests.conftest import REAL_START_CODE_INDEX

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


def test_a_query_before_an_index_builds_one_unless_code_is_off(
    runner: CliRunner, checkout: Path
) -> None:
    """Onboard's build is detached, so the first query may come before it: it builds the
    index then. With ``[code] enabled`` off, every query says how to build one."""
    config = load_config()
    config.code.enabled = False
    save_config(config)
    for args in (["find", "x"], ["refs", "x"], ["similar", "x"]):
        result = runner.invoke(app, ["--json", "code", *args])
        assert result.exit_code == 1
        error = json.loads(result.output)
        assert error["error"] == "no_index"
    assert _json(runner, "index", "--status")["building"] is False
    assert _json(runner, "index", "--status")["exists"] is False

    config.code.enabled = True
    save_config(config)
    found = _json(runner, "find", "parse_window")
    assert [s["name"] for s in found["symbols"]] == ["parse_window"]
    assert _json(runner, "index", "--status")["exists"] is True


def test_a_query_while_the_first_build_runs_says_so(runner: CliRunner, checkout: Path) -> None:
    """A first build writes in one transaction: until it commits, the file is there and
    empty. A query then is not answered from it ("no definition"), and doctor says why."""
    from aisquare.core import paths
    from aisquare.services import diagnostics

    paths.ensure_home()
    project = code_service.project_here()
    with code_index._connect(project.id, create=True):
        pass  # the file as a first build leaves it before its commit
    with locking.holding(code_index.code_dir(project.id) / "build.lock", wait_s=0):
        result = runner.invoke(app, ["--json", "code", "find", "parse_window"])
        row = diagnostics._check_code_index(checkout)
        status = runner.invoke(app, ["code", "index", "--status"])
        as_json = _json(runner, "index", "--status")
        human_query = runner.invoke(app, ["code", "find", "parse_window"])
    assert "building in the background" in status.output
    assert as_json["building"] is True
    assert "building it first" not in human_query.output, "it builds nothing: index_busy"
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "index_busy"
    assert row.detail.startswith("building in the background")
    assert not code_index.ready(project.id)

    found = _json(runner, "find", "parse_window")
    assert [s["name"] for s in found["symbols"]] == ["parse_window"], "built once it is free"
    assert code_index.ready(project.id)


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
    lock = code_index.code_dir(code_service.project_here().id) / "build.lock"
    lock.parent.mkdir(parents=True)
    with locking.holding(lock, wait_s=0):
        row = diagnostics._check_code_index(checkout)
    assert row.status.value == "ok" and row.detail.startswith("building in the background")
    code_service.build_index()
    row = diagnostics._check_code_index(checkout)
    assert row.status.value == "ok" and row.detail.startswith("4 symbols in 1 files (ast-")
    assert "refresh is running" not in row.detail
    with locking.holding(lock, wait_s=0):
        row = diagnostics._check_code_index(checkout)
    assert row.detail.endswith("; a refresh is running")


SLOW_BUILD_S = 6.0


def test_onboard_returns_while_a_slow_index_build_runs(
    runner: CliRunner, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#327 built the index inline: an onboard (and ``init``) of a large checkout waited
    for a full index, and every test that onboarded paid for one. The build is started
    detached, as the session-start refresh is, and the onboard returns at once."""
    monkeypatch.setattr(hooks_service, "start_code_index", REAL_START_CODE_INDEX)
    started: list[tuple[list[str], Path | None]] = []
    monkeypatch.setattr(
        hooks_service, "_detach", lambda argv, *, cwd=None: started.append((argv, cwd))
    )

    built: list[bool] = []

    def slow_build(*_args: object, **_kwargs: object) -> code_index.BuildReport:
        built.append(True)
        time.sleep(SLOW_BUILD_S)
        raise AssertionError("onboard built the index inline")

    monkeypatch.setattr(code_index, "build", slow_build)

    began = time.monotonic()
    result = runner.invoke(app, ["--json", "project", "onboard"])
    took = time.monotonic() - began

    assert result.exit_code == 0, result.output
    # The build was never called inline, which no runner's speed changes. No wall-clock
    # bound: the rest of an onboard took 3.1 s on a loaded linux runner and 6.6 s on
    # windows (rc/p2 push run 40087282716), so any bound measured the runner.
    assert built == [], f"onboard built the index inline ({took:.1f}s)"
    assert json.loads(result.output)["code_index"].startswith("building in the background")
    [(argv, cwd)] = started
    assert argv[-3:] == ["--quiet", "code", "index"] and cwd == checkout.resolve()


def test_onboard_starts_the_index_unless_code_is_off(
    runner: CliRunner, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(
        hooks_service, "start_code_index", lambda _project, *, full=False: started.append(full)
    )
    result = runner.invoke(app, ["--json", "project", "onboard"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["code_index"].startswith("building in the background")
    runner.invoke(app, ["--json", "project", "onboard", "--refresh"])
    assert started == [False, True], "--refresh rebuilds from nothing"

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
