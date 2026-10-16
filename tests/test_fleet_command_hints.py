"""Every fleet command a person or an agent is told to run names the project it is about.

Without ``--project`` a fleet command acts on the active project, which is the machine-wide
pin before the working directory (``core.workspace.active_project``). A hint written by hand
as ``aisquare fleet reap`` therefore reaped whichever project was pinned in some other shell,
not the one the message was about. Rounds 19 and 20 of the review of #259 fixed it one site at
a time, and round 21 found five more. So the rule is enforced over the source:
``services.project.fleet_command`` builds each one, and no string literal holds a hand-built
``aisquare fleet <verb>``. The exceptions are the sweeps of every project (``reap --all``,
``shutdown --all``) and the few literals in :data:`EXEMPT`: no command to run, or one about
no project in particular.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "aisquare"

_HAND_BUILT = re.compile(r"\baisquare fleet (?!(?:reap|shutdown) --all\b)[a-z]")
"""``aisquare fleet`` and a word: any verb, except the two that walk every project."""

_NO_PROJECT_IN_QUESTION = (
    "the fleet UI's refusal: it covers every project, and the listing it points to is the "
    "JSON form of whichever one the reader asks for"
)
EXEMPT: dict[tuple[str, str], str] = {
    (
        "services/fleet.py",
        "# aisquare fleet worktrees (docs/plans/fleet-tui.md §3.5)\n{…}\n",
    ): "the comment written above the worktree root's line in .git/info/exclude",
    (
        "cli/fleet.py",
        "the fleet UI has no --json form — for machine-readable fleet state use "
        "`aisquare fleet ls --json`",
    ): _NO_PROJECT_IN_QUESTION,
    (
        "cli/fleet.py",
        "the fleet UI needs an interactive terminal ({…}) — run `aisquare` in a terminal, or "
        "use `aisquare fleet ls --json`",
    ): _NO_PROJECT_IN_QUESTION,
}
"""Literals that hold ``aisquare fleet <word>`` and are no hint to name a project in, with
why. Keyed by the literal's text, so a change to one is looked at again."""


def _literals(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Each string literal's line and text, f-strings with their fields left out — every
    one but a docstring, or any other string standing as a statement of its own."""
    standing = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant | ast.JoinedStr)
    }
    parts: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts.update(id(value) for value in node.values)
    for node in ast.walk(tree):
        if id(node) in standing or id(node) in parts:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                str(value.value) if isinstance(value, ast.Constant) else "{…}"
                for value in node.values
            )
            yield node.lineno, text


def _hand_built() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        where = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, text in _literals(tree):
            if _HAND_BUILT.search(text) and (where, text) not in EXEMPT:
                found.append(f"{where}:{line}: {text!r}")
    return found


def test_no_fleet_command_hint_is_built_by_hand() -> None:
    assert _hand_built() == []


def test_every_exemption_is_still_in_the_source() -> None:
    """An exemption whose literal is gone or changed is dropped, not left to cover another."""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        where = path.relative_to(SRC).as_posix()
        found.update((where, text) for _line, text in _literals(ast.parse(path.read_text("utf-8"))))
    assert set(EXEMPT) - found == set()


def test_the_check_finds_a_hand_built_hint_and_passes_what_it_should() -> None:
    """The check itself: a bare verb, a placeholder and an f-string are each found; a sweep
    of every project, a docstring and an attribute's docstring are not."""
    module = '''
"""aisquare fleet reap in a docstring."""
A = "run `aisquare fleet reap` to remove it"
B = f"`aisquare fleet restart {label}`"
C = "aisquare fleet " "tell <label> …"
D = "`aisquare fleet reap --all` and `aisquare fleet shutdown --all --yes`"
E = 1
"""aisquare fleet stop, an attribute's docstring."""
F = "`aisquare fleet ls --all` is this project's, ended rows included"
'''
    hits = [text for _line, text in _literals(ast.parse(module)) if _HAND_BUILT.search(text)]
    assert hits == [
        "run `aisquare fleet reap` to remove it",
        "`aisquare fleet restart {…}`",
        "aisquare fleet tell <label> …",
        "`aisquare fleet ls --all` is this project's, ended rows included",
    ]


_TARGET = "the project the command was run against (``--project``, else the active one)"
_FLEET_OP = "the project the fleet operation acts on, whose own row the message is about"
_SHOWN = "the project the fleet UI shows, whose rows it lists"
_WALKED = "each project its rows are read from, project by project"
_ROW_OF_SESSION = (
    "the project of the row the store records against the session, in whichever project "
    "(``fleet_agent_for_session``): under a hub, not the session's board's"
)
PROJECT_OF: dict[tuple[str, str], str] = {
    ("cli/fleet.py", "_emit_agents"): _TARGET,
    ("cli/fleet.py", "stop"): _TARGET,
    ("cli/fleet.py", "_emit_shutdown"): _TARGET,
    ("cli/fleet.py", "attach"): _TARGET,
    ("cli/ui/app.py", "FleetApp.on_spawn_agent"): _SHOWN,
    ("cli/ui/views/project.py", "no_manager_text"): _SHOWN,
    ("cli/ui/views/welcome.py", "WelcomeView._paint_fleet"): _SHOWN,
    ("services/auto_mode.py", "_restart_off_auto"): (
        "its callers': the running rows' own projects, or " + _ROW_OF_SESSION
    ),
    ("services/diagnostics.py", "_check_resumable_agents"): _WALKED,
    ("services/diagnostics.py", "_left_from_before_capture"): "each captured row itself",
    ("services/diagnostics.py", "_check_dead_managers"): _WALKED,
    ("services/diagnostics.py", "_claude_account_limit_checks"): _ROW_OF_SESSION,
    ("services/fleet.py", "_live_agent"): _FLEET_OP,
    ("services/fleet.py", "_replaced"): _FLEET_OP,
    ("services/fleet.py", "_in_hand_over"): _FLEET_OP,
    ("services/fleet.py", "spawn"): _FLEET_OP,
    ("services/fleet.py", "_in_place_of_recorded"): _FLEET_OP,
    ("services/fleet.py", "_exited_note"): "each exited row's own ``project_id``",
    ("services/fleet.py", "_tell"): _FLEET_OP,
    ("services/fleet.py", "_send_it_later"): _FLEET_OP,
    ("services/fleet.py", "_past_trust_prompt"): _FLEET_OP,
    ("services/fleet.py", "_forget"): (
        _FLEET_OP + ", or each stranded row's own (``_refuse_a_freed_socket``)"
    ),
    ("services/fleet.py", "switch"): _FLEET_OP,
    ("services/fleet.py", "restart"): _FLEET_OP,
    ("services/fleet.py", "rename"): "the project renamed, with its new codename",
    ("services/fleet.py", "attach_argv"): _FLEET_OP,
    ("services/project.py", "ProjectBusyError.__init__"): "the project forget refused",
    ("services/remote_server.py", "_remote_turn_pinned"): "the project the Remote request names",
    ("services/team.py", "fleet_switches"): _ROW_OF_SESSION,
    ("services/team.py", "_limited_text"): _ROW_OF_SESSION,
    ("services/team.py", "_role_cycle"): (
        "``_cycle_project``: the row ``AISQUARE_FLEET_AGENT`` names, any project; with no "
        "row, the board's only when no hub is set"
    ),
}
"""Where each caller of ``fleet_command`` takes the project it names, by module and enclosing
function. Never the board's: under ``AISQUARE_TEAM_HUB`` the board is the hub's, while each
agent's row is in its own project, so the role cycle and the board's ``limited`` chip told
a manager to spawn in the hub and to stop, tell and switch agents that are not there (review
of #259, round 22). A new caller states its source here, and the test below fails until it
does."""


def _fleet_command_callers() -> set[tuple[str, str]]:
    """Each module and enclosing function (``Class.method``) that calls ``fleet_command``."""
    found: set[tuple[str, str]] = set()

    def visit(node: ast.AST, where: str, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, where, (*scope, child.name))
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "fleet_command":
                    found.add((where, ".".join(scope)))
            visit(child, where, scope)

    for path in sorted(SRC.rglob("*.py")):
        visit(ast.parse(path.read_text(encoding="utf-8")), path.relative_to(SRC).as_posix(), ())
    return found


def test_every_fleet_command_caller_says_where_its_project_comes_from() -> None:
    callers = _fleet_command_callers()
    assert callers - set(PROJECT_OF) == set(), "a new caller: say where its project comes from"
    assert set(PROJECT_OF) - callers == set(), "a caller that is gone: drop its entry"
