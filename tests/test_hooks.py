"""The Claude Code hook handlers: prompt capture + session-start injection."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import selfcli
from aisquare.core.store import store_session
from aisquare.core.version import __version__
from aisquare.core.workspace import current_project
from aisquare.services import hooks as hooks_service
from aisquare.services import install_route
from tests import snapshot_repos

_PACK = '<files>\n<file path="a.py">\nprint("hi")\n</file>\n</files>\n'


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _json(output: str) -> Any:
    return json.loads(output)


def _add(work_dir: Path) -> None:
    """Add ``work_dir`` on purpose, as ``project onboard`` does, without the snapshot."""
    with store_session() as store:
        store.onboard_project(current_project(work_dir))


def _prompt(runner: CliRunner, work_dir: Path) -> None:
    payload = json.dumps({"prompt": "add a test for X", "cwd": str(work_dir)})
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


def _logged(runner: CliRunner) -> list[str]:
    return [prompt["text"] for prompt in _json(runner.invoke(app, ["--json", "log"]).stdout)]


def test_user_prompt_submit_captures_in_a_project_added_on_purpose(
    runner: CliRunner, work_dir: Path
) -> None:
    _add(work_dir)
    _prompt(runner, work_dir)
    assert "add a test for X" in _logged(runner)
    with store_session() as store:
        assert len(store.turn_metrics(project_id=current_project(work_dir).id)) == 1


def test_a_prompt_where_nothing_was_added_records_nothing_and_makes_no_project(
    runner: CliRunner, work_dir: Path
) -> None:
    """``[projects] capture`` is off by default: no row, no prompt, no turn (#139)."""
    _prompt(runner, work_dir)
    assert _logged(runner) == []
    with store_session() as store:
        assert store.list_projects(all=True) == []
        assert store.turn_metrics() == []


def test_with_capture_on_a_prompt_captures_the_directory_unlisted(
    runner: CliRunner, work_dir: Path, project_capture: None
) -> None:
    _prompt(runner, work_dir)
    assert "add a test for X" in _logged(runner)
    with store_session() as store:
        assert [p.root for p in store.captured_projects()] == [work_dir.resolve()]
        assert store.list_projects() == []
        assert len(store.turn_metrics()) == 1


def test_capture_is_a_setting_off_by_default_and_on_captures_as_0_8_0_did(
    runner: CliRunner, work_dir: Path, isolated_home: Path
) -> None:
    """Through config.toml, not the tests' patch: the default is in the system layer,
    so only a change is written, and setting it back writes nothing."""
    got = runner.invoke(app, ["--json", "config", "get", "projects.capture"])
    assert _json(got.stdout) == {"projects.capture": "false"}
    assert runner.invoke(app, ["config", "set", "projects.capture", "true"]).exit_code == 0
    assert "[projects]\ncapture = true" in (isolated_home / "config.toml").read_text()
    _prompt(runner, work_dir)
    assert "add a test for X" in _logged(runner)
    assert runner.invoke(app, ["config", "set", "projects.capture", "false"]).exit_code == 0
    assert "[projects]" not in (isolated_home / "config.toml").read_text()


def test_user_prompt_submit_ignores_blank(runner: CliRunner) -> None:
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=json.dumps({"prompt": "  "}))
    assert result.exit_code == 0
    logged = runner.invoke(app, ["--json", "log"])
    assert _json(logged.stdout) == []


def test_user_prompt_submit_survives_garbage_input(runner: CliRunner) -> None:
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input="not json at all")
    assert result.exit_code == 0  # a hook must never break the agent


def test_session_start_injects_curated_context(runner: CliRunner, work_dir: Path) -> None:
    runner.invoke(app, ["context", "add", "prefer tabs", "--user"])
    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "## Your preferences" in result.stdout
    assert "prefer tabs" in result.stdout


def test_session_start_is_empty_for_an_unknown_repo(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_session_start_directive_points_at_the_snapshot(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core import snapshot
    from aisquare.core.workspace import current_project

    monkeypatch.setattr(
        snapshot,
        "_run_repomix",
        lambda _root, *, compress, ignore=(): (_PACK, "Total Tokens: 9"),
    )
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: 100)
    snapshot.generate(current_project(work_dir).id, work_dir)

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "packed snapshot" in result.stdout
    assert "pack.repomix.xml" in result.stdout


def test_session_start_directive_points_at_the_skeleton_when_the_full_pack_was_skipped(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over budget even compressed: the agent gets the skeleton and its index, and no full pack.

    This is the case the cap used to leave with NOTHING — and the directive is
    the only reader of the snapshot, so it is where "usable" has to be true.
    """
    from aisquare.core import snapshot
    from aisquare.core.workspace import current_project

    monkeypatch.setattr(
        snapshot,
        "_run_repomix",
        lambda _root, *, compress, ignore=(): (_PACK, "Total Tokens: 9"),
    )
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: 100)
    meta = snapshot.generate(current_project(work_dir).id, work_dir, max_tokens=10)
    assert meta.status == "skeleton_only"

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "index into a skeleton" in result.stdout
    assert str(meta.skeleton_path) in result.stdout
    assert str(meta.index_path) in result.stdout
    assert "pack.repomix.xml" not in result.stdout


def _pack_measuring(monkeypatch: pytest.MonkeyPatch, *, full: int, compressed: int) -> None:
    """Repomix stubbed: the full pack is ``full`` tokens, the compressed one ``compressed``."""
    from aisquare.core import snapshot

    monkeypatch.setattr(
        snapshot,
        "_run_repomix",
        lambda _root, *, compress, ignore=(): (_PACK, "compressed" if compress else "full"),
    )
    monkeypatch.setattr(
        snapshot, "_total_tokens", lambda _text, out: compressed if out == "compressed" else full
    )


def test_an_over_budget_skeleton_is_never_read_first(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#282: a 1.497M-token skeleton against the 150k budget was still "read this FIRST".

    An agent that obeys pages through a file larger than its whole context. It is
    told the size and sent to the index, which names each file's slice.
    """
    from aisquare.core import snapshot

    _pack_measuring(monkeypatch, full=10_990_000, compressed=1_497_000)
    meta = snapshot.generate(current_project(work_dir).id, work_dir)
    assert meta.status == "skeleton_only"

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "read this FIRST" not in result.stdout
    assert "1,497,000 tokens, over the 150,000-token budget" in result.stdout
    assert "never read it from the top" in result.stdout
    index_at = result.stdout.index(str(meta.index_path))
    assert index_at < result.stdout.index(str(meta.skeleton_path))  # the index comes first


@pytest.mark.parametrize(
    ("full", "compressed", "read_first", "named"),
    [
        (120_000, 8_000, True, "skeleton"),  # the skeleton is what is measured, not the pack
        (120_000, 12_000, True, "skeleton"),  # at the limit
        (120_000, 20_000, False, "skeleton"),  # two default Reads of the `between` fixture
        (140_000, 90_000, False, "skeleton"),
        (200_000, 8_000, True, "pack"),  # compressed pack kept, no skeleton beside it
        (200_000, 90_000, False, "pack"),
    ],
)
def test_a_skeleton_is_read_first_only_when_one_read_holds_it(
    runner: CliRunner,
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    full: int,
    compressed: int,
    read_first: bool,
    named: str,
) -> None:
    """Under the budget, "read this FIRST" still cost up to 150k tokens, carried by every
    later request. One default Read of the `between` fixture held 14,163 of the tokens the
    meta counts, so only a file of at most 12,000 is read first."""
    from aisquare.core import snapshot

    _pack_measuring(monkeypatch, full=full, compressed=compressed)
    meta = snapshot.generate(current_project(work_dir).id, work_dir)
    assert meta.status == "ready"
    assert meta.skeleton_path.exists() is (named == "skeleton")

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert str(meta.pack_path) in result.stdout
    assert str(meta.index_path) in result.stdout
    assert ("read this FIRST" in result.stdout) is read_first
    if not read_first:
        assert f"The {named} is {compressed:,} tokens" in result.stdout
        assert "never read it from the top" in result.stdout
    # A compressed pack holds signatures, not every file's contents, and is said to.
    label = "Pack (compressed: structure + signatures" if meta.compressed else "Full pack (every"
    assert f"- {label}" in result.stdout


def test_a_full_pack_whose_skeleton_failed_is_not_called_a_skeleton(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skeleton beside a full pack is best-effort. Without it, the pack read first holds
    every file's contents, and the directive says so instead of calling it the skeleton."""
    from aisquare.core import snapshot

    def repomix(_root: Path, *, compress: bool, ignore: Any = ()) -> tuple[str, str]:
        if compress:
            raise OSError("repomix: killed")
        return _PACK, "full"

    monkeypatch.setattr(snapshot, "_run_repomix", repomix)
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: 8_000)
    meta = snapshot.generate(current_project(work_dir).id, work_dir)
    assert meta.status == "ready" and not meta.compressed and not meta.skeleton_path.exists()

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert (
        f"- Full pack (every file's contents, read this FIRST): {meta.pack_path}" in result.stdout
    )
    assert "Skeleton" not in result.stdout


def test_a_skeleton_left_by_an_earlier_pack_is_not_measured_or_named(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo that grew past the budget keeps its compressed pack and no new skeleton, but the
    last run's skeleton stays on disk with no size on the meta. Measured, it read as 0 tokens:
    "read this FIRST", naming a file that no longer matches the tree."""
    from aisquare.core import snapshot

    project_id = current_project(work_dir).id
    _pack_measuring(monkeypatch, full=120_000, compressed=20_000)
    stale = snapshot.generate(project_id, work_dir).skeleton_path
    _pack_measuring(monkeypatch, full=200_000, compressed=90_000)
    meta = snapshot.generate(project_id, work_dir)
    assert meta.status == "ready" and meta.compressed and stale.exists()

    result = runner.invoke(app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)}))
    assert result.exit_code == 0, result.output
    assert "read this FIRST" not in result.stdout
    assert "The pack is 90,000 tokens" in result.stdout
    assert str(stale) not in result.stdout


@pytest.mark.parametrize("shape", sorted(snapshot_repos.SHAPES))
def test_the_snapshot_fixture_repos_are_the_same_tree_every_time(
    tmp_path: Path, shape: str
) -> None:
    """The handoff's before/after runs compare packs of these repos (§1.7), so a second
    write must give the tree the first did."""
    trees = []
    for run in ("first", "second"):
        root = snapshot_repos.write(tmp_path / run, shape)
        tree = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
            check=True,
        )
        trees.append(tree.stdout.strip())
    assert trees[0] == trees[1]
    modules, _functions = snapshot_repos.SHAPES[shape]
    assert len(list((tmp_path / "first" / "ledger").glob("part_*.py"))) == modules


_REAL_WHICH = shutil.which


def _aisquare_on_path(
    found: str | None, monkeypatch: pytest.MonkeyPatch, *, uvx: str | None = None
) -> None:
    """``shutil.which("aisquare")`` answers ``found`` and ``shutil.which("uvx")`` ``uvx``;
    every other name as it would."""
    answers = {"aisquare": found, "uvx": uvx}

    def which(cmd: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
        return answers[cmd] if cmd in answers else _REAL_WHICH(cmd, mode, path)

    monkeypatch.setattr(shutil, "which", which)


_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
"""The Python this process runs on, as ``uvx --python`` takes it."""


def _uv_cache(root: Path) -> Path:
    """A stand-in for uv's cache at ``root``, tagged as uv tags its own (``CACHEDIR.TAG``):
    only an environment inside uv's cache is a uvx run (review of #257)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "CACHEDIR.TAG").write_text(
        "Signature: 8a477f597d28d172789f06886806bc55", encoding="utf-8"
    )
    return root


def _session_start_after_a_prompt(runner: CliRunner, work_dir: Path) -> str:
    _add(work_dir)
    payload = json.dumps({"prompt": "add a test for X", "cwd": str(work_dir)})
    assert runner.invoke(app, ["hook", "user-prompt-submit"], input=payload).exit_code == 0
    started = runner.invoke(
        app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)})
    )
    assert started.exit_code == 0, started.output
    return started.stdout


def test_session_start_names_a_log_command_the_uvx_route_can_run(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's uvx route leaves no ``aisquare`` on the agent's PATH (review of #257).

    uvx runs the hook from an environment in uv's cache and puts that environment's
    ``bin`` on the hook's PATH only; the launcher takes the route only where no
    aisquare is installed. "run `aisquare log`" was command not found for the agent,
    and it was often all that session start said there.
    """
    cached = _uv_cache(tmp_path / ".cache" / "uv") / "archive-v0" / "NPat_ypOvcy3YMzz"
    monkeypatch.setattr(sys, "prefix", str(cached))
    _aisquare_on_path(str(cached / "bin" / "aisquare"), monkeypatch, uvx="/usr/bin/uvx")
    monkeypatch.setenv(hooks_service.PLUGIN_UVX_ENV, "/usr/bin/uvx")

    said = _session_start_after_a_prompt(runner, work_dir)

    pin = f"aisquare-cli=={__version__}"
    assert f"run `uvx --python {_PYTHON} --from {pin} aisquare log` to see" in said, said
    assert "run `aisquare log`" not in said


@pytest.mark.parametrize("on_path", [None, "/usr/bin/uvx"], ids=["no-uvx", "another-uvx"])
def test_on_the_uvx_route_the_log_command_names_the_uvx_and_the_python_the_hook_ran_on(
    runner: CliRunner,
    work_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    on_path: str | None,
) -> None:
    """Round 20 of #257: the launcher finds uvx in ~/.local/bin where PATH has none (a
    session started from the desktop app), and runs it with the tested range. The agent
    was told a bare `uvx`, command not found there, and no `--python`, so uv resolved on
    its default Python, which may be outside the range: another environment built, on an
    untested Python, or none. Named by the launcher's path, with this process's Python."""
    cached = _uv_cache(tmp_path / ".cache" / "uv") / "archive-v0" / "NPat_ypOvcy3YMzz"
    monkeypatch.setattr(sys, "prefix", str(cached))
    _aisquare_on_path(str(cached / "bin" / "aisquare"), monkeypatch, uvx=on_path)
    ran = str(tmp_path / "home" / ".local" / "bin" / "uvx")
    monkeypatch.setenv(hooks_service.PLUGIN_UVX_ENV, ran)

    said = _session_start_after_a_prompt(runner, work_dir)

    argv = [ran, "--python", _PYTHON, "--from", f"aisquare-cli=={__version__}", "aisquare", "log"]
    assert f"run `{install_route.command_line(argv)}` to see" in said, said


def test_on_the_uvx_route_the_log_command_names_the_build_the_launcher_was_given(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 21 of #257: the launcher runs `--from "$AISQUARE_PLUGIN_FROM"` when it is set,
    to check a wheel before the release is on PyPI, and session start still named
    `aisquare-cli==<version>`: not on PyPI then, and another build than the hooks' after."""
    cached = _uv_cache(tmp_path / ".cache" / "uv") / "archive-v0" / "NPat_ypOvcy3YMzz"
    monkeypatch.setattr(sys, "prefix", str(cached))
    _aisquare_on_path(str(cached / "bin" / "aisquare"), monkeypatch, uvx="/usr/bin/uvx")
    monkeypatch.setenv(hooks_service.PLUGIN_UVX_ENV, "/usr/bin/uvx")
    wheel = str(tmp_path / "dist" / f"aisquare_cli-{__version__}-py3-none-any.whl")
    monkeypatch.setenv(hooks_service.PLUGIN_FROM_ENV, wheel)

    said = _session_start_after_a_prompt(runner, work_dir)

    argv = ["uvx", "--python", _PYTHON, "--from", wheel, "aisquare", "log"]
    assert f"run `{install_route.command_line(argv)}` to see" in said, said
    assert f"aisquare-cli=={__version__}" not in said


def test_session_start_names_plain_aisquare_log_where_path_has_it(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: an installed CLI on PATH is named as people type it."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "tools" / "aisquare-cli"))
    _aisquare_on_path("/home/me/.local/bin/aisquare", monkeypatch)

    said = _session_start_after_a_prompt(runner, work_dir)

    assert "run `aisquare log` to see how the user tends to ask" in said, said


def test_session_start_names_this_interpreter_where_path_has_no_aisquare(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hooks that name a virtualenv not on PATH: the bare name would not run either."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "project" / ".venv"))
    _aisquare_on_path(None, monkeypatch)

    said = _session_start_after_a_prompt(runner, work_dir)

    assert f"run `{install_route.command_line(selfcli.argv_for(['log']))}` to see" in said, said
    assert "run `aisquare log`" not in said


def test_the_plugin_page_names_the_log_command_the_uvx_route_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/claude-code-plugin.md said a uv-only machine "gets memory with nothing else
    installed", where session start's one line named a command that is not there
    (review of #257). It now says what that route gives, with the command it prints."""
    cache = _uv_cache(tmp_path / ".cache" / "uv")
    monkeypatch.setattr(sys, "prefix", str(cache / "archive-v0" / "x"))
    printed = hooks_service._log_command()
    page = Path(__file__).resolve().parents[1] / "docs" / "claude-code-plugin.md"
    text = " ".join(page.read_text(encoding="utf-8").split())
    documented = printed.replace(__version__, "<version>").replace(_PYTHON, "<python>")

    assert printed.startswith(f"uvx --python {_PYTHON} --from aisquare-cli=="), printed
    assert f"`{documented}`" in text
    assert "gets memory with nothing else installed" not in text


# --- the symbol index at session start (#285) -------------------------------------------


def _index(work_dir: Path) -> str:
    from aisquare.core import code_index

    (work_dir / "mod.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    project_id = current_project(work_dir).id
    code_index.build(project_id, work_dir)
    return project_id


def test_the_directive_names_the_code_commands_only_where_there_is_an_index(
    runner: CliRunner, work_dir: Path
) -> None:
    def started() -> str:
        result = runner.invoke(
            app, ["hook", "session-start"], input=json.dumps({"cwd": str(work_dir)})
        )
        assert result.exit_code == 0, result.output
        return result.stdout

    assert "aisquare code" not in started()
    _index(work_dir)
    said = started()
    assert "`aisquare code find <name>`" in said and "`aisquare code similar <name>`" in said
    lines = [line for line in said.splitlines() if "aisquare code" in line]
    assert len(lines) == 2, "two lines at most"


def test_a_session_start_refreshes_the_index_detached_and_at_most_once_a_minute(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import REAL_REFRESH_CODE_INDEX

    started: list[tuple[list[str], Path | None]] = []
    monkeypatch.setattr(
        hooks_service, "_detach", lambda argv, *, cwd=None: started.append((argv, cwd))
    )
    project = current_project(work_dir)

    REAL_REFRESH_CODE_INDEX(project)
    assert started == [], "no index: building one is onboard's, not a session start's"

    _index(work_dir)
    REAL_REFRESH_CODE_INDEX(project)
    REAL_REFRESH_CODE_INDEX(project)
    [(argv, cwd)] = started
    assert argv[-3:] == ["--quiet", "code", "index"] and cwd == project.root

    from aisquare.core import code_index

    stamp = code_index.code_dir(project.id) / "refreshed"
    os.utime(stamp, (stamp.stat().st_mtime - 61, stamp.stat().st_mtime - 61))
    REAL_REFRESH_CODE_INDEX(project)
    assert len(started) == 2, "a minute on, the next session start refreshes again"
