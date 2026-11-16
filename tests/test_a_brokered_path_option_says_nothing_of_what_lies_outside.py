"""A path a sandboxed agent names to its broker is refused alike whatever lies there, when it
is not one the command reads (review r38 of #113, finding 1, reopened: e).

The broker runs the command outside the sandbox, as the user. Typer's own check of a path
option ``stat``\\ s and ``access``\\ es it before the command sees it: "is not readable" told the
sandbox that a file outside its tree it may not read is there. The commands told more:
``--artifact`` resolved the path first ("cannot be read" for nothing there), ``watch show``
said "no such watch detail file", and ``brief check --source-root`` said "is not a directory"
and printed the path resolved, where an outside link leads. Through the real broker
(``Broker.answer``), each of ``--detail-file``'s siblings now says one thing of a file, nothing,
a directory, a file the user may not read and a link, outside the tree: the same words but for
the path, the same exit. Unbrokered, each path is checked as it always was.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import store_broker
from tests.test_a_sandboxed_codex_records_evidence import _linked
from tests.test_store_broker import (  # noqa: F401 - the fixture, by name
    MINE,
    _answer,
    _broker,
    _request,
    work,
)

if sys.platform == "win32":  # the broker is POSIX's (FIFOs, descriptors): nothing below runs
    pytest.skip("the broker is POSIX's", allow_module_level=True)

FOREIGN = "what only the user outside the sandbox may read"
#: What the user's own checks, or the commands' old ones, said of what lies at a path.
TELLS = ("readable", "a directory", "No such file", "no such", "cannot be read", "far-away")
#: Each option, the argv that names a path with it, and the refusal it gives every one outside.
OPTIONS: dict[str, tuple[list[str], str]] = {
    "evidence --artifact": (
        ["brief", "evidence", "{brief}", "R1", "--task", "{task}", "--verdict", "pass",
         "--summary", "looked", "--artifact", "{path}", "--as", MINE[:8]],
        "is outside this agent's tree",
    ),
    "finding --artifact": (
        ["brief", "finding", "{brief}", "R1", "--task", "{task}", "--summary", "broken",
         "--artifact", "{path}", "--as", MINE[:8]],
        "is outside this agent's tree",
    ),
    "check --source-root": (
        ["brief", "check", "{brief}", "--source-root", "{path}"],
        "is not a checkout of this brief's project",
    ),
    "watch show": (["watch", "show", "{path}"], "is not a watch detail file of this agent's"),
}  # fmt: skip


def _outside(tmp_path: Path) -> dict[str, Path]:
    """Beside the launch's tree: a file, nothing, a directory, a file the user may not read,
    and a link to a directory whose name the refusal must not show."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "auth.json").write_text(FOREIGN, encoding="utf-8")
    (outside / "dir").mkdir()
    (outside / "locked.json").write_text(FOREIGN, encoding="utf-8")
    (outside / "locked.json").chmod(0)
    (tmp_path / "far-away").mkdir()
    (outside / "link").symlink_to(tmp_path / "far-away")
    return {
        "file": outside / "auth.json",
        "nothing": outside / "not-there.json",
        "directory": outside / "dir",
        "unreadable": outside / "locked.json",
        "link": outside / "link",
    }


def _argv(option: str, brief: str, task: str, path: Path) -> list[str]:
    template, _ = OPTIONS[option]
    return [part.format(brief=brief, task=task, path=path) for part in template]


@pytest.mark.parametrize("option", list(OPTIONS))
def test_a_path_outside_the_tree_is_refused_alike_whatever_lies_there(
    work: Path,  # noqa: F811
    tmp_path: Path,
    option: str,
) -> None:
    brief, task = _linked()
    broker = _broker(tmp_path)
    _, why = OPTIONS[option]
    said: dict[str, tuple[object, str]] = {}
    for n, (kind, path) in enumerate(_outside(tmp_path).items()):
        answer = _answer(broker, _request(_argv(option, brief, task, path), work, rid=f"{n:032x}"))
        text = f"{answer['stdout']}{answer['stderr']}"
        assert why in text, (kind, text)
        assert not any(tell in text for tell in TELLS) and FOREIGN not in text, (kind, text)
        said[kind] = (answer["code"], " ".join(text.replace(str(path), "<path>").split()))
    assert len(set(said.values())) == 1, said
    [(code, _)] = set(said.values())
    assert code not in (0, None), said


@pytest.mark.parametrize("option", list(OPTIONS))
def test_unbrokered_the_path_is_checked_as_it_always_was(
    work: Path,  # noqa: F811
    tmp_path: Path,
    option: str,
) -> None:
    """Typer's own check, as the user: a file the user may not read is a usage error."""
    if os.geteuid() == 0:
        pytest.skip("root reads a file of mode 0")
    assert not store_broker.brokered_call()
    brief, task = _linked()
    unreadable = _outside(tmp_path)["unreadable"]
    result = CliRunner().invoke(app, _argv(option, brief, task, unreadable))
    assert result.exit_code == 2, result.output
    assert "is not readable" in " ".join(result.output.split()), result.output


@pytest.mark.parametrize(
    "option", ["evidence --artifact", "finding --artifact", "check --source-root"]
)
def test_a_link_out_and_back_in_says_nothing_of_what_it_names(
    work: Path,  # noqa: F811
    tmp_path: Path,
    option: str,
) -> None:
    """``<tree>/l/../../<tree's name>[/f.md]``, ``l`` a link to ``X`` outside the tree: refused
    the same whatever ``X`` is (review r38 of #113, finding 1, round 2). Resolved whole, the way
    followed ``l`` and climbed back: recorded, or checked, when ``X`` was there, and "cannot be
    read" or "not a checkout" when it was not."""
    brief, task = _linked()
    broker = _broker(tmp_path)
    out = tmp_path / "outside"
    out.mkdir()
    (out / "auth.json").write_text(FOREIGN, encoding="utf-8")
    (out / "adir").mkdir()
    (work / "f.md").write_text("in tree", encoding="utf-8")
    tail = f"{work.name}/f.md" if "artifact" in option else work.name
    _, why = OPTIONS[option]
    seen: dict[str, tuple[object, str]] = {}
    for n, target in enumerate((out / "auth.json", out / "adir", out / "nothing")):
        link = work / f"l{n}"
        link.symlink_to(target)
        named = Path(f"{link}/../../{tail}")
        argv = _argv(option, brief, task, named)
        answer = _answer(broker, _request(argv, work, rid=f"{n + 100:032x}"))
        text = f"{answer['stdout']}{answer['stderr']}".replace(str(named), "<path>")
        seen[target.name] = (answer["code"], " ".join(text.split()))
    assert len(set(seen.values())) == 1, seen
    [(code, text)] = set(seen.values())
    assert code not in (0, None) and why in text, seen
