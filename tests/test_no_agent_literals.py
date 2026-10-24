"""No shared code decides by an agent's name or spells an agent's vocabulary: that is the adapter's.

``core/agent_adapters/`` is the one place a terminal agent is implemented
(docs/coding-agents.md, "Adding a terminal agent"); a service that compares an agent id
with ``"codex"`` or ``"claude-code"`` is a branch the next agent would have to edit, and so
is one that compares it with a constant naming one (``AGENT``, ``CODEX``…), asks
``harness.is_default_agent`` of a binary, or spells an agent's binary, variable, config
file or flag itself. This reads every module under ``src/aisquare`` outside that package
(AST nodes only: comments and docstrings are words, not branches) and sorts what it finds
into shapes (:data:`SHAPES`):

- ``decide_literal``: a comparison, membership test or ``match`` case against either id.
  None is allowed, anywhere.
- ``decide_named``: the same against a constant naming an agent (:data:`NAMED`, and every
  module constant whose value is an agent's id or binary, or another such constant).
- ``decide_binary``: a call of ``harness.is_default_agent`` outside ``core/harness.py``.
- ``binary``, ``env``, ``dir``, ``flag``: a string constant that is an agent's binary
  (``claude``), a variable of its own (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME=…``), a file or
  folder of its own (``.claude``, ``hooks.json``) or one of its flags (``--fork-session``).

The last six are a ratchet: :data:`BASELINE` holds, per file and shape, how many lines carry
it today, and the count may only go down. A new one fails with its file and line; one taken
out (made a capability or an adapter method) lowers the baseline, which a later lane then
never raises. Out of the rules, as words and not branches (SCOPE #113 0.8.2, part B):
aisquare's own options spelled like an agent's (``typer.Option("--permission-mode")``),
prose naming a variable (help text "for CLAUDE_CONFIG_DIR"), a model provider's API key,
which is no agent's (``ANTHROPIC_API_KEY``, stripped from a child's environment), and the
agents' names in prose and in the ``Claude*`` model types.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"
PACKAGE = Path("core") / "agent_adapters"
LITERALS = frozenset({"codex", "claude-code"})

Shape = Literal["decide_literal", "decide_named", "decide_binary", "binary", "env", "dir", "flag"]
SHAPES: tuple[Shape, ...] = (
    "decide_literal",
    "decide_named",
    "decide_binary",
    "binary",
    "env",
    "dir",
    "flag",
)

#: Constants that name an agent wherever they are defined; every module constant assigned an
#: agent's id or binary (or one of these) joins them (:func:`agent_constants`).
NAMED = frozenset({"AGENT", "CODEX", "DEFAULT_AGENT", "DEFAULT_AGENT_BINARY"})
BINARIES = frozenset({"claude", "codex", "claude.exe", "codex.exe", "claude.cmd", "codex.cmd"})
#: A variable of an agent's own: the whole constant, or the name of an assignment it starts.
ENV = re.compile(r"(CLAUDE|CLAUDECODE|ANTHROPIC|CODEX|OPENAI)[A-Z0-9_]*")
#: A model provider's key, which no agent owns (the import engine's, the brain's).
PROVIDER_KEYS = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})
DIRS = re.compile(
    r"(^|[/\\])(\.claude|\.codex|\.claude\.json|\.mcp\.json|CLAUDE\.md|AGENTS(\.override)?\.md"
    r"|claude-accounts|codex-accounts|hooks\.json|auth\.json|\.credentials\.json)([/\\]|$)"
)
FLAGS = frozenset(
    {
        "--append-system-prompt",
        "--append-system-prompt-file",
        "--permission-mode",
        "--resume",
        "--fork-session",
        "--session-id",
        "--continue",
        "--mcp-config",
        "--strict-mcp-config",
        "--allowedTools",
        "--allowed-tools",
        "--disallowedTools",
        "--disallowed-tools",
        "--tools",
        "--dangerously-skip-permissions",
        "--chrome",
        "--name",
        "--settings",
        "--setting-sources",
        "--json-schema",
        "--output-format",
        "--max-turns",
        "--no-session-persistence",
        "--effort",
        "--print",
        "--bare",
        "--sandbox",
        "--ask-for-approval",
        "--approve-for-me",
        "--add-dir",
        "--output-schema",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--skip-git-repo-check",
        "--ephemeral",
        "-r",
    }
)
#: Calls whose string arguments declare aisquare's own options (``typer.Option("--name")``).
OPTION_CALLS = frozenset({"Option", "Argument"})

#: Per file (under ``src/aisquare``), per shape: the lines that carry it today. May only go
#: down (:func:`test_no_file_grows_an_agent_specific_shape`), and holds what the tree holds
#: (:func:`test_the_baseline_is_what_the_tree_holds`): lower it in the change that removes one.
BASELINE: dict[str, dict[Shape, int]] = {
    "cli/fleet.py": {"flag": 1},
    "cli/hook.py": {"binary": 3},
    "core/agents.py": {"env": 2, "dir": 9},
    "core/claude_accounts.py": {"binary": 1, "env": 3, "dir": 4},
    "core/claude_sharing.py": {"env": 7},
    "core/codex_accounts.py": {"binary": 1, "env": 1, "dir": 3},
    "core/config.py": {"flag": 3},
    "core/harness.py": {"env": 30, "flag": 5},
    "core/spawn.py": {"env": 1},
    "services/diagnostics.py": {"env": 1, "dir": 3},
    "services/explainability.py": {"env": 4, "flag": 4},
    "services/store_broker.py": {"flag": 1},
}


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _module_constants(tree: ast.Module) -> Iterable[tuple[str, ast.AST]]:
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    yield target.id, statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and statement.value is not None
            and isinstance(statement.target, ast.Name)
        ):
            yield statement.target.id, statement.value


def agent_constants(trees: Iterable[ast.Module]) -> frozenset[str]:
    """:data:`NAMED`, and every module constant (in any module, the adapters' too) whose
    value is an agent's id or binary, or a constant already so named (``HOOK_AGENT =
    DEFAULT_AGENT``)."""
    assigned = [pair for tree in trees for pair in _module_constants(tree)]
    named = set(NAMED)
    grown = True
    while grown:
        grown = False
        for name, value in assigned:
            agentish = (
                isinstance(value, ast.Constant) and value.value in LITERALS | BINARIES
            ) or _name(value) in named
            if agentish and name not in named:
                named.add(name)
                grown = True
    return frozenset(named)


def _names_an_agent(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in LITERALS
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_names_an_agent(item) for item in node.elts)
    return False


def _named(node: ast.AST, named: frozenset[str]) -> bool:
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_named(item, named) for item in node.elts)
    return _name(node) in named


def _docstrings(tree: ast.AST) -> set[int]:
    kinds = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, kinds)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _option_words(tree: ast.AST) -> set[int]:
    """The string arguments of the calls that declare aisquare's own options."""
    return {
        id(arg)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _name(node.func) in OPTION_CALLS
        for arg in node.args
        if isinstance(arg, ast.Constant)
    }


def _env(value: str) -> bool:
    name, assigns, _ = value.partition("=")
    return bool(ENV.fullmatch(name)) and (assigns or name == value) and name not in PROVIDER_KEYS


def shapes(tree: ast.AST, named: frozenset[str], *, harness: bool = False) -> dict[Shape, set[int]]:
    """The lines of ``tree`` that carry each shape (:data:`SHAPES`). ``harness``: the module
    is ``core/harness.py``, whose own ``is_default_agent`` calls are its definition's."""
    found: dict[Shape, set[int]] = {shape: set() for shape in SHAPES}
    docs, options = _docstrings(tree), _option_words(tree)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_names_an_agent(operand) for operand in operands):
                found["decide_literal"].add(line)
            elif any(_named(operand, named) for operand in operands):
                found["decide_named"].add(line)
        elif isinstance(node, ast.match_case):
            pattern = node.pattern
            cases = pattern.patterns if isinstance(pattern, ast.MatchOr) else [pattern]
            for case in cases:
                if isinstance(case, ast.MatchValue) and _names_an_agent(case.value):
                    found["decide_literal"].add(pattern.lineno)
                elif isinstance(case, ast.MatchValue) and _named(case.value, named):
                    found["decide_named"].add(pattern.lineno)
        elif isinstance(node, ast.Call) and _name(node.func) == "is_default_agent":
            if not harness:
                found["decide_binary"].add(line)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if id(node) in docs or id(node) in options:
                continue
            if value in BINARIES:
                found["binary"].add(line)
            if _env(value):
                found["env"].add(line)
            if DIRS.search(value):
                found["dir"].add(line)
            if value in FLAGS or value.split("=", 1)[0] in FLAGS - {"-r"}:
                found["flag"].add(line)
    return found


def scan(src: Path) -> dict[str, dict[Shape, set[int]]]:
    """Every module under ``src`` outside :data:`PACKAGE`, by path relative to ``src``: the
    lines of each shape it carries (none listed for a module that carries none)."""
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(src.rglob("*.py"))
    }
    named = agent_constants(trees.values())
    found: dict[str, dict[Shape, set[int]]] = {}
    for path, tree in trees.items():
        relative = path.relative_to(src)
        if PACKAGE in relative.parents:
            continue
        by_shape = shapes(tree, named, harness=relative == Path("core") / "harness.py")
        kept = {shape: lines for shape, lines in by_shape.items() if lines}
        if kept:
            found[relative.as_posix()] = kept
    return found


def grown(
    found: Mapping[str, Mapping[Shape, set[int]]], baseline: Mapping[str, Mapping[Shape, int]]
) -> list[str]:
    """Each file and shape that carries more lines than ``baseline`` allows, with every one
    of its lines (which of them is new, the diff says): ``path:line`` each."""
    over: list[str] = []
    for path, by_shape in sorted(found.items()):
        for shape, lines in by_shape.items():
            allowed = baseline.get(path, {}).get(shape, 0)
            if len(lines) > allowed:
                where = ", ".join(f"{path}:{line}" for line in sorted(lines))
                over.append(f"{shape} {len(lines)} > {allowed}: {where}")
    return over


def test_no_shared_module_decides_by_an_agent_literal() -> None:
    found = scan(SRC)
    literal = sorted(
        f"src/aisquare/{path}:{line}"
        for path, by_shape in found.items()
        for line in by_shape.get("decide_literal", ())
    )
    assert not literal, (
        "agent-specific branches outside core/agent_adapters (make each a capability or "
        "an adapter method): " + ", ".join(literal)
    )


def test_no_file_grows_an_agent_specific_shape() -> None:
    over = grown(scan(SRC), BASELINE)
    assert not over, (
        "agent-specific code outside core/agent_adapters, past tests/test_no_agent_literals.py's "
        "BASELINE (make it a capability or an adapter method; prose and aisquare's own "
        "options are not counted):\n  " + "\n  ".join(over)
    )


def test_the_baseline_is_what_the_tree_holds() -> None:
    """A shape taken out lowers its count here, in the same change: a baseline above the tree
    would let the next one back in unseen."""
    found = scan(SRC)
    held = {
        path: {shape: len(lines) for shape, lines in by_shape.items()}
        for path, by_shape in found.items()
    }
    loose = sorted(
        f"{path} {shape}: {count} -> {held.get(path, {}).get(shape, 0)}"
        for path, by_shape in BASELINE.items()
        for shape, count in by_shape.items()
        if held.get(path, {}).get(shape, 0) < count
    )
    assert not loose, "lower BASELINE (the tree holds fewer): " + ", ".join(loose)


def _shapes_of(source: str, named: frozenset[str] = NAMED) -> set[Shape]:
    return {shape for shape, lines in shapes(ast.parse(source), named).items() if lines}


@pytest.mark.parametrize(
    ("source", "shape"),
    [
        ('adapter.id == "codex"', "decide_literal"),
        ('"claude-code" != name', "decide_literal"),
        ('name in ("codex", "aider")', "decide_literal"),
        ('agent not in (None, "claude-code")', "decide_literal"),
        ('"codex" in names', "decide_literal"),
        ("match name:\n    case 'codex':\n        pass", "decide_literal"),
        ("match name:\n    case 'aider' | 'claude-code':\n        pass", "decide_literal"),
        ("account.agent == AGENT", "decide_named"),
        ("kind != core.CODEX", "decide_named"),
        ("agent in (DEFAULT_AGENT, other)", "decide_named"),
        ("match name:\n    case core.CODEX:\n        pass", "decide_named"),
        ("harness.is_default_agent(b)", "decide_binary"),
        ('which("claude")', "binary"),
        ('env.pop("CODEX_THREAD_ID")', "env"),
        ('f"CLAUDE_CONFIG_DIR={home} claude"', "env"),
        ('home / ".claude"', "dir"),
        ('path.name == "hooks.json"', "dir"),
        ('args.append("--fork-session")', "flag"),
        ('["--resume=" + path]', "flag"),
    ],
)
def test_the_check_sees_every_shape_of_the_branch(source: str, shape: Shape) -> None:
    assert shape in _shapes_of(source), source


@pytest.mark.parametrize(
    "source",
    ['get_adapter("codex")', 'agent = "claude-code"', 'f(agent="codex")', "get_adapter(AGENT)"],
)
def test_naming_an_agent_decides_nothing(source: str) -> None:
    """A name passed on decides nothing: no ``decide_*`` shape (Codex's id, being its
    binary's name too, is a ``binary`` all the same)."""
    assert not {shape for shape in _shapes_of(source) if shape.startswith("decide")}, source


@pytest.mark.parametrize(
    "source",
    [
        "get_adapter(AGENT)",
        'help = "the config dir for CLAUDE_CONFIG_DIR, when it is set"',
        'name: str = typer.Option(None, "--name", "-n")',
        'env.pop("ANTHROPIC_API_KEY", None)',
        'f"run it with CLAUDE_CONFIG_DIR unset"',
        'label = "Claude Code"',
        "def f():\n    '''Reads ~/.claude and its hooks.json, then --fork-session.'''",
    ],
)
def test_words_and_aisquares_own_options_are_no_branch(source: str) -> None:
    assert not _shapes_of(source), source


def test_a_constant_naming_an_agent_is_named_wherever_it_is_defined() -> None:
    trees = [
        ast.parse('LEAD = "claude-code"\nTOOL = "codex"\nFIRST = LEAD\nPATH = "/usr/bin"'),
        ast.parse("SECOND = module.FIRST"),
    ]
    named = agent_constants(trees)
    assert {"LEAD", "TOOL", "FIRST", "SECOND"} <= named and "PATH" not in named
    assert _shapes_of("if agent == SECOND:\n    pass", named) == {"decide_named"}


@pytest.mark.parametrize(
    ("planted", "shape"),
    [
        ("def pick(agent):\n    if agent == AGENT:\n        return 1\n", "decide_named"),
        ("def pick(b):\n    return harness.is_default_agent(b)\n", "decide_binary"),
        ('def fork(args):\n    return [*args, "--fork-session"]\n', "flag"),
    ],
)
def test_a_planted_branch_in_a_shared_module_fails_with_its_file_and_line(
    tmp_path: Path, planted: str, shape: Shape
) -> None:
    """Acceptance 1 of L5: each planted shape fails the ratchet naming where it is."""
    src = tmp_path / "aisquare"
    (src / "services").mkdir(parents=True)
    (src / "core" / "agent_adapters").mkdir(parents=True)
    (src / "services" / "planted.py").write_text(planted, encoding="utf-8")
    # The same shape inside the adapters' package is theirs to have.
    (src / "core" / "agent_adapters" / "mine.py").write_text(planted, encoding="utf-8")

    over = grown(scan(src), BASELINE)

    line = 2
    assert over == [f"{shape} 1 > 0: services/planted.py:{line}"], over
