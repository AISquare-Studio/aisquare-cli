"""A command a person is told to paste is quoted for their shell, never by ``shlex`` alone.

``shlex.quote`` wraps every Windows path, whose ``\\`` it counts unsafe, in single quotes,
which cmd.exe has no syntax for and passes on as part of the argument: the onboard
``NotAdded`` named failed on Windows (CI gate of #259), and the same quoting stood in
``project rename``'s refusal, the Remove dialog and its toast, the trust hints, and reap's
``git branch -D`` (review of #259, round 20). ``shell_line.command_line``, which
``install_route`` re-exports, is the one quoting for a printed command: bare where cmd.exe
and PowerShell both take a word as it is, double quotes on Windows otherwise, ``shlex.join``
elsewhere.

What ``shlex`` still quotes is listed below, each with why it is no paste-this command for
a Windows shell; a new use fails here until it is one or the other.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from aisquare.cli import project as project_cli
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import install_route
from aisquare.services import project as project_service

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"

#: (module, function) → (how many ``shlex.quote``/``shlex.join`` calls, and why each is not
#: a command a person on Windows pastes).
SHLEX_KEPT: dict[tuple[str, str], tuple[int, str]] = {
    ("core/shell_line.py", "command_line"): (1, "it is the quoting: shlex.join off Windows"),
    ("core/agents.py", "_quote"): (1, "its POSIX branch; Windows has its own"),
    ("services/remote_server.py", "_remote_shell_word"): (1, "its POSIX branch; Windows its own"),
    ("services/remote_server.py", "_page_open_refusal"): (2, "chmod, which only POSIX has"),
    ("services/remote_server.py", "_page_open_to_others"): (1, "chmod, which only POSIX has"),
    ("cli/team.py", "spawn"): (1, "one sh line: `aisquare launch`, in `_reading_env_files`' $(…)"),
    ("cli/team.py", "_reading_env_files"): (2, "the same sh line: $(…) && export"),
    ("cli/team.py", "_describe"): (1, "a binding's args as shown for editing, not a command"),
    ("cli/explainability.py", "env"): (1, "export lines for sh's eval"),
    ("cli/team.py", "harness_status"): (1, "a role's agent arguments as shown, not a command"),
    ("cli/ui/views/app_settings.py", "_append_flags"): (1, "a role's flags as shown, not run"),
    ("services/diagnostics.py", "_trust_fix"): (1, "its POSIX branch; Windows its own"),
}


def _shlex_calls() -> Counter[tuple[str, str]]:
    """Each ``shlex.quote``/``shlex.join`` call in ``src``, by module and enclosing function."""
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node: ast.AST, scope: list[str], module: str = module) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    walk(child, [*scope, child.name])
                    continue
                if isinstance(child, ast.ImportFrom) and child.module == "shlex":
                    found[(module, "from shlex import")] += 1
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "shlex"
                    and child.func.attr in ("quote", "join")
                ):
                    found[(module, scope[-1] if scope else "<module>")] += 1
                walk(child, scope)

        walk(tree, [])
    return found


def test_shlex_quotes_only_what_no_windows_shell_is_told_to_run() -> None:
    kept = {where: count for where, (count, _why) in SHLEX_KEPT.items()}

    found = dict(_shlex_calls())

    assert found == kept, (
        "a command a person is told to paste is quoted with shell_line.command_line; "
        "a shlex use that is not one goes in SHLEX_KEPT, with why"
    )


@pytest.mark.parametrize(
    ("said", "argv"),
    [
        pytest.param(
            project_service.onboard_command,
            ["aisquare", "project", "onboard", "{root}"],
            id="onboard",
        ),
        pytest.param(
            accounts_service.trust_command, ["aisquare", "accounts", "trust", "{root}"], id="trust"
        ),
        pytest.param(
            lambda root: fleet_service.trust_prompt_detail(root).split("`")[1],
            ["aisquare", "accounts", "trust", "{root}"],
            id="trust-prompt",
        ),
        pytest.param(
            lambda root: project_cli._positional(f"-{root}"), ["--", "-{root}"], id="group-name"
        ),
    ],
)
def test_a_command_to_paste_reads_back_on_windows_as_the_one_it_names(
    monkeypatch: pytest.MonkeyPatch, said: Callable[[Path], str], argv: list[str]
) -> None:
    """With ``sys.platform`` "win32", each reads back through ``split_line`` as the command
    it names: a path with spaces is one argument, with no single quote in it."""
    monkeypatch.setattr(sys, "platform", "win32")
    root = Path("C:/Users/me/My Repo")

    line = said(root)

    assert "'" not in line
    assert install_route.split_line(line) == [word.format(root=root) for word in argv]
