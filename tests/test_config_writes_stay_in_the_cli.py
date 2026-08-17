"""Nothing outside the CLI may reach a config write.

@9bbc8ed7's write guard asserts that every CLI call to a config-writing service
sits inside ``expected_config_write_errors()``, so an expected failure prints one
``✗`` line instead of a traceback. It inspects the CLI layer, and they named the
limit themselves: "a config write reached from somewhere else that eventually
surfaces through a command — a hook, the serve daemon, the sweeper — is outside
what it inspects".

THAT LIMIT IS SAFE ONLY WHILE THE PRECONDITION HOLDS, and the precondition is
what this file pins: no hook, MCP/serve, or sweeper entry point can reach
``save_config`` at all. Surveyed on the train — 34 such functions, every one
outside the closure — so their guard covers every path that exists. If a config
write is ever added behind one of those surfaces, their guard stays green while
an unwrapped traceback path exists, and this test is what says so.

The call graph is built by NAME rather than by resolved import, which
over-approximates: an unrelated function sharing a name only ever ADDS edges, so
the closure is a superset and a clean result is trustworthy. It could
under-approximate a call made through an alias or a dynamically-built attribute,
which is stated rather than papered over.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"

#: Modules that are entered by something other than a person typing a command.
NON_CLI_SURFACES = ("hooks.py", "mcp_server.py", "serve.py", "sweeper.py")

#: Commands known to reach a config write. The control: if the closure stops
#: finding these, it has stopped working and its empty answer means nothing.
KNOWN_WRITERS = ("set_", "redaction", "enable", "disable", "bind", "init")


def _call_graph() -> tuple[dict[str, Path], dict[str, set[str]]]:
    defines: dict[str, Path] = {}
    calls: dict[str, set[str]] = defaultdict(set)
    for module in sorted(SRC.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            defines[node.name] = module
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
                    if name:
                        calls[node.name].add(name)
    return defines, calls


def _reaches_a_config_write(calls: dict[str, set[str]]) -> set[str]:
    reaching = {fn for fn, targets in calls.items() if "save_config" in targets}
    changed = True
    while changed:
        changed = False
        for fn, targets in calls.items():
            if fn not in reaching and targets & reaching:
                reaching.add(fn)
                changed = True
    return reaching


def test_the_closure_finds_the_commands_that_do_write() -> None:
    """Guard the guard: an empty answer from a broken walk proves nothing.

    Two guards this shift certified blind checkers because a walk that visited
    nothing looked exactly like a walk over a clean tree.
    """
    defines, calls = _call_graph()
    reaching = _reaches_a_config_write(calls)

    assert len(defines) > 200, f"only {len(defines)} functions parsed — the walk broke"
    for command in KNOWN_WRITERS:
        assert command in reaching, (
            f"`{command}` no longer reaches save_config in this analysis, so a "
            "clean result below would mean the closure stopped working"
        )


def test_no_hook_or_daemon_surface_can_reach_a_config_write() -> None:
    """The precondition behind the CLI-only write guard.

    A config write behind one of these would surface a raw traceback to whoever
    triggered it — an agent's hook, an MCP client — with no command to wrap it,
    and @9bbc8ed7's guard would not see it.
    """
    defines, calls = _call_graph()
    reaching = _reaches_a_config_write(calls)

    offenders = sorted(
        f"{defines[fn].name}::{fn}"
        for fn in reaching
        if fn in defines and defines[fn].name in NON_CLI_SURFACES
    )

    assert not offenders, (
        f"these non-CLI surfaces can now reach a config write: {offenders}. "
        "The CLI write guard cannot cover them — either route the write through "
        "a command that wraps it, or make that surface handle the failure itself."
    )
