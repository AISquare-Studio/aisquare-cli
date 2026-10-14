"""No shared code decides by an agent's name: every such decision is the adapter's.

``core/agent_adapters/`` is the one place a terminal agent is implemented
(docs/coding-agents.md, "Adding a terminal agent"); a service that compares an agent id
with ``"codex"`` or ``"claude-code"`` is a branch the next agent would have to edit. This
reads every module under ``src/aisquare`` and refuses a comparison, a membership test or a
``match`` case against either literal outside that package. The decision belongs behind
the adapter, as a capability flag or a method.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"
PACKAGE = SRC / "core" / "agent_adapters"
LITERALS = frozenset({"codex", "claude-code"})

#: ``path:line`` → why that comparison is not an agent decision. Empty: keep it so.
ALLOWED: dict[str, str] = {}


def _names_an_agent(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in LITERALS
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_names_an_agent(item) for item in node.elts)
    return False


def _agent_decisions(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            ops = (ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Is, ast.IsNot)
            operands = [node.left, *node.comparators]
            if any(isinstance(op, ops) for op in node.ops) and any(
                _names_an_agent(operand) for operand in operands
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.match_case):
            pattern = node.pattern
            values = [pattern] if not isinstance(pattern, ast.MatchOr) else pattern.patterns
            if any(
                isinstance(value, ast.MatchValue) and _names_an_agent(value.value)
                for value in values
            ):
                lines.append(node.pattern.lineno)
    return lines


def test_no_shared_module_decides_by_an_agent_literal() -> None:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        if PACKAGE in path.parents:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line in _agent_decisions(tree):
            where = f"{path.relative_to(SRC.parent)}:{line}"
            if where not in ALLOWED:
                found.append(where)
    assert not found, (
        "agent-specific branches outside core/agent_adapters (make each a capability or "
        "an adapter method, or list it in ALLOWED with the reason): " + ", ".join(found)
    )


def test_the_check_sees_every_shape_of_the_branch() -> None:
    shapes = [
        'adapter.id == "codex"',
        '"claude-code" != name',
        'name in ("codex", "aider")',
        'agent not in (None, "claude-code")',
        '"codex" in names',
        "match name:\n    case 'codex':\n        pass",
        "match name:\n    case 'aider' | 'claude-code':\n        pass",
    ]
    for source in shapes:
        assert _agent_decisions(ast.parse(source)), source
    for source in ['get_adapter("codex")', 'agent = "claude-code"', 'f(agent="codex")']:
        assert not _agent_decisions(ast.parse(source)), source
