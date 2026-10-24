"""The per-project symbol index behind ``aisquare code`` (#285, phase A of #91).

A Python and a TypeScript fixture in a real git checkout: definitions, references,
incremental rebuilds, deletion, the fallbacks (no extra, a syntax error, a binary, a
large file), and the queries' order.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import code_index

PYTHON = '''\
import time


def _now():
    return time.time()


def parse_duration(text, default=None):
    return float(text)


class FleetApp:
    """The app."""

    def spawn_agent(self, role, label=None):
        return _now()

    async def stop(self):
        self.spawn_agent("x")
'''

TYPESCRIPT = """\
export function spawnAgent(role: string, label?: string): string {
  return helperFor(role);
}

export class Board {
  openTask(id: number) { return this.close(id); }
}

interface Shape { area(): number }

const parseDuration = (text: string) => Number(text);
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "pkg" / "app.py").write_text(PYTHON, encoding="utf-8")
    (root / "web" / "board.ts").write_text(TYPESCRIPT, encoding="utf-8")
    (root / "tool.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.py").write_text("def vendored():\n    pass\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return root


def _ts_installed() -> bool:
    return code_index.tree_sitter() is not None


needs_tree_sitter = pytest.mark.skipif(
    not _ts_installed(), reason="the [code] extra (tree-sitter) is not installed"
)


def test_python_definitions_with_kinds_qualnames_signatures_and_arity(repo: Path) -> None:
    report = code_index.build("p", repo)
    assert report.full and report.parsed >= 1
    [method] = code_index.find("p", "spawn_agent")
    assert (method.kind, method.qualname, method.parent) == (
        "method",
        "FleetApp.spawn_agent",
        "FleetApp",
    )
    assert method.signature == "def spawn_agent(self, role, label=None)"
    assert method.arity == 2, "self does not count"
    assert (method.path, method.line, method.end_line) == ("pkg/app.py", 15, 16)
    [klass] = code_index.find("p", "FleetApp", kind="class")
    assert (klass.line, klass.end_line) == (12, 19)
    [stop] = code_index.find("p", "FleetApp.stop")
    assert stop.signature == "async def stop(self)"


def test_refs_list_every_use_by_path_and_line(repo: Path) -> None:
    code_index.build("p", repo)
    uses = [(r.path, r.line) for r in code_index.refs("p", "_now")]
    assert uses == [("pkg/app.py", 16)]
    assert ("pkg/app.py", 19) in [(r.path, r.line) for r in code_index.refs("p", "spawn_agent")]


def test_find_goes_exact_then_case_then_prefix_then_subtokens(repo: Path) -> None:
    code_index.build("p", repo)
    assert [s.name for s in code_index.find("p", "fleetapp")] == ["FleetApp"]
    assert [s.name for s in code_index.find("p", "parse_dur")] == ["parse_duration"]
    by_token = {s.name for s in code_index.find("p", "agent_spawning_thing")}
    assert "spawn_agent" not in by_token, "every sub-token must match"
    assert "spawn_agent" in {s.name for s in code_index.find("p", "agent_spawn")}


def test_similar_finds_like_named_helpers_across_languages(repo: Path) -> None:
    code_index.build("p", repo)
    anchor, found = code_index.similar("p", "parse_duration")
    assert anchor is not None and anchor.path == "pkg/app.py"
    names = [s.symbol.name for s in found]
    assert "parse_duration" not in names, "the target itself is left out"
    if _ts_installed():
        assert names[0] == "parseDuration" and found[0].shared == ("duration", "parse")
    _, by_place = code_index.similar("p", "pkg/app.py:16")  # inside spawn_agent
    assert "spawnAgent" in [s.symbol.name for s in by_place] or not _ts_installed()


@needs_tree_sitter
def test_typescript_through_the_tags_queries(repo: Path) -> None:
    code_index.build("p", repo)
    [fn] = code_index.find("p", "spawnAgent")
    assert (fn.kind, fn.path, fn.line, fn.arity) == ("function", "web/board.ts", 1, 2)
    [method] = code_index.find("p", "openTask")
    assert (method.kind, method.qualname) == ("method", "Board.openTask")
    assert code_index.find("p", "Shape")[0].kind == "interface"
    assert [r.line for r in code_index.refs("p", "helperFor")] == [2]
    assert [r.line for r in code_index.refs("p", "close")] == [6]


def test_without_the_extra_typescript_is_reported_not_indexed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(code_index, "tree_sitter", lambda: None)
    report = code_index.build("p", repo)
    assert report.not_indexed == {"typescript": 1, "go": 1}
    assert all(s.path != "web/board.ts" for s in code_index.find("p", "spawnAgent"))
    assert code_index.status("p").not_indexed == {"typescript": 1, "go": 1}


def test_ignored_trees_are_never_read(repo: Path) -> None:
    """node_modules is gitignored nowhere here; the snapshot's DEFAULT_IGNORE leaves it out."""
    code_index.build("p", repo)
    assert code_index.find("p", "vendored") == []


def test_an_unchanged_tree_is_not_read_again_and_one_edit_is(repo: Path) -> None:
    code_index.build("p", repo)
    again = code_index.build("p", repo)
    assert (again.full, again.parsed) == (False, 0)
    app = repo / "pkg" / "app.py"
    app.write_text(PYTHON + "\n\ndef added_helper():\n    pass\n", encoding="utf-8")
    edited = code_index.build("p", repo)
    assert edited.parsed == 1
    assert code_index.find("p", "added_helper")[0].line == 22
    # Touched but the same bytes: the hash says unchanged, the mtime is refreshed.
    app.write_text(PYTHON + "\n\ndef added_helper():\n    pass\n", encoding="utf-8")
    os.utime(app, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert code_index.build("p", repo).parsed == 0


def test_a_deleted_file_takes_its_symbols_and_refs_with_it(repo: Path) -> None:
    code_index.build("p", repo)
    (repo / "pkg" / "app.py").unlink()
    report = code_index.build("p", repo)
    assert report.removed == 1
    assert all(s.path != "pkg/app.py" for s in code_index.find("p", "spawn_agent"))
    assert code_index.refs("p", "_now") == []
    assert all(s.symbol.path != "pkg/app.py" for s in code_index.similar("p", "parse_duration")[1])


def test_a_new_parser_version_rebuilds_everything(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code_index.build("p", repo)
    monkeypatch.setattr(code_index, "parser_versions", lambda: {"python": "ast-next"})
    report = code_index.build("p", repo)
    assert report.full and report.parsed == 1


def test_a_syntax_error_is_reported_and_its_old_rows_dropped(repo: Path) -> None:
    code_index.build("p", repo)
    (repo / "pkg" / "app.py").write_text("def broken(:\n", encoding="utf-8")
    report = code_index.build("p", repo)
    assert "pkg/app.py" in report.failed
    assert all(s.path != "pkg/app.py" for s in code_index.find("p", "spawn_agent"))


def test_binaries_and_large_files_are_skipped(repo: Path) -> None:
    (repo / "pkg" / "blob.py").write_bytes(b"\x00\x01binary")
    (repo / "pkg" / "big.py").write_text("x = 1\n" * 400, encoding="utf-8")
    report = code_index.build("p", repo, max_file_kb=1)
    assert report.skipped_binary == 1 and report.skipped_large == 1


def test_outside_git_the_root_is_walked(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "a.py").write_text("def lonely():\n    pass\n", encoding="utf-8")
    code_index.build("q", tmp_path / "plain")
    assert code_index.find("q", "lonely")[0].path == "a.py"


def test_a_second_builder_is_refused_not_queued(repo: Path) -> None:
    from aisquare.core import locking

    code_index.code_dir("p").mkdir(parents=True, exist_ok=True)
    lock = code_index.code_dir("p") / "build.lock"
    with locking.holding(lock, wait_s=0), pytest.raises(code_index.IndexBusy):
        code_index.build("p", repo)


def test_an_unreadable_index_is_rebuilt_not_trusted(repo: Path) -> None:
    code_index.index_path("p").parent.mkdir(parents=True, exist_ok=True)
    code_index.index_path("p").write_bytes(b"not a database")
    assert not code_index.status("p").exists
    code_index.build("p", repo)
    assert code_index.status("p").symbols > 0


def test_status_says_what_it_holds_and_whether_it_is_behind(repo: Path) -> None:
    assert not code_index.status("p").exists
    code_index.build("p", repo, head="abc")
    found = code_index.status("p", current_head="def")
    assert found.exists and found.stale and found.files >= 1
    assert code_index.status("p", current_head="abc").stale is False


@pytest.mark.parametrize(
    ("name", "tokens"),
    [
        ("spawnAgent", ["spawn", "agent"]),
        ("_verify_cap", ["verify", "cap"]),
        ("HTTPServer", ["http", "server"]),
        ("parse2Dates", ["parse2", "dates"]),
        ("__init__", ["init"]),
    ],
)
def test_names_split_into_snake_and_camel_subtokens(name: str, tokens: list[str]) -> None:
    assert code_index.subtokens(name) == tokens


def test_one_file_a_parser_cannot_handle_costs_that_file_not_the_index(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #327 (major): a 1,200-term expression passes ast.parse and then raised
    RecursionError out of the build, which rolled every later build back, so the index
    froze silently. One file's failure is that file's, in ``failed``, with its type."""
    deep = "x = " + " + ".join(["1"] * 1200) + "\n"
    (repo / "pkg" / "gen_big.py").write_text(deep, encoding="utf-8")
    (repo / "pkg" / "new_helper.py").write_text("def other_now():\n    pass\n", encoding="utf-8")
    report = code_index.build("p", repo)
    assert code_index.find("p", "other_now")[0].path == "pkg/new_helper.py"
    assert "pkg/gen_big.py" not in report.failed, "a deep expression is not an error at all"

    def broken(path: str, source: str) -> tuple[list[code_index.Symbol], list[code_index.Ref]]:
        if path.endswith("app.py"):
            raise MemoryError("a parser blew up")
        return [], []

    monkeypatch.setattr(code_index, "python_symbols", broken)
    (repo / "pkg" / "app.py").write_text(PYTHON + "\n# touched\n", encoding="utf-8")
    report = code_index.build("p", repo)
    assert report.failed["pkg/app.py"].startswith("MemoryError: a parser blew up")


def test_a_walk_outside_git_never_enters_an_ignored_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #327 (nit): node_modules was walked whole and filtered afterwards."""
    root = tmp_path / "plain"
    (root / "node_modules" / "deep").mkdir(parents=True)
    (root / "node_modules" / "deep" / "x.py").write_text("def vendored():\n    pass\n")
    (root / "a.py").write_text("def mine():\n    pass\n", encoding="utf-8")
    entered: list[str] = []
    real_walk = os.walk

    def spy(top: Path, **kwargs: Any) -> Iterator[tuple[str, list[str], list[str]]]:
        for current, dirs, files in real_walk(top, **kwargs):
            entered.append(Path(current).name)
            yield current, dirs, files

    monkeypatch.setattr(os, "walk", spy)
    assert code_index.listed_files(root) == ["a.py"]
    assert "node_modules" not in entered and "deep" not in entered
