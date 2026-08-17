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


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """``from x import save_config as _persist`` — ``_persist`` IS that call.

    Per module, because an alias is only in scope where it was bound. Import
    forms only: an assignment rebinding or a dynamically-built attribute cannot
    be followed statically, and those remain invisible rather than guessed at.
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for imported in node.names:
                if imported.asname:
                    aliases[imported.asname] = imported.name.rsplit(".", 1)[-1]
    return aliases


def _rebinding_aliases(tree: ast.AST, imported: dict[str, str]) -> dict[str, str]:
    """``writer = save_config`` — a call to ``writer`` IS that call.

    The import form was closed first and this one was measured as still open
    rather than assumed closed, which is why it could be shut deliberately.
    Chains are followed (``a = save_config; b = a``) and resolved THROUGH the
    import map, so an aliased import rebound by assignment composes rather than
    merely coexisting.

    The ``seen`` set is not defensive decoration: ``a = b; b = a`` is legal
    Python, and without it this loops forever — a hang rather than a failure,
    which is the worst shape a test helper can take.

    Still invisible: an attribute, a dict entry, a closure, anything built at run
    time. Over-approximating stays the safe direction, so a clean result remains
    trustworthy.

    And it is free here, which is a measurement rather than a hope: resolution
    is per MODULE rather than per scope, so two same-named locals in different
    functions would be conflated — but on this tree, closure size is **16 with
    the resolution and 16 without, adding no spurious members** (@9bbc8ed7,
    2026-08-17). The number lives here rather than in the note that produced it
    because the next person to widen this analysis needs to know what the last
    widening cost, and a note scrolls.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name) and isinstance(value, ast.Name):
                bindings[target.id] = value.id

    resolved: dict[str, str] = {}
    for name in bindings:
        seen = {name}
        current = bindings[name]
        while current in bindings and current not in seen:
            seen.add(current)
            current = bindings[current]
        resolved[name] = imported.get(current, current)
    return resolved


def _call_graph(roots: list[Path] | None = None) -> tuple[dict[str, Path], dict[str, set[str]]]:
    defines: dict[str, Path] = {}
    calls: dict[str, set[str]] = defaultdict(set)
    for module in roots if roots is not None else sorted(SRC.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        imported = _import_aliases(tree)
        aliases = {**imported, **_rebinding_aliases(tree, imported)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            defines[node.name] = module
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
                    if name:
                        calls[node.name].add(aliases.get(name, name))
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


def test_an_aliased_import_of_the_writer_is_still_a_config_write(tmp_path: Path) -> None:
    """``from … import save_config as _persist`` must not hide the edge.

    The graph is keyed by NAME, so a call to ``_persist`` looked like a call to
    something unrelated and the closure lost the edge — measured: a direct
    ``save_config`` in ``services/hooks.py`` failed this file, and the SAME write
    through an alias left it green. That is the difference between a guard that
    catches the change nobody anticipated and one that catches only the obvious
    spelling of it, which is the entire reason this guard exists.

    Resolution is per module and import-only. A rebinding through assignment
    (``writer = save_config``) or a dynamically-built attribute is still
    invisible; static analysis cannot follow those, and over-approximating is
    the safe direction — an extra edge only widens the closure.
    """
    module = tmp_path / "aliased.py"
    module.write_text(
        "from aisquare.core.config import save_config as _persist\n\n"
        "def writes_through_an_alias(config: object) -> None:\n"
        "    _persist(config)\n",
        encoding="utf-8",
    )

    _, calls = _call_graph(roots=[module])

    assert "save_config" in calls["writes_through_an_alias"], (
        "an aliased import hides the edge, so a config write behind a hook "
        "would leave this guard green"
    )


def test_a_rebinding_of_the_writer_is_still_a_config_write(tmp_path: Path) -> None:
    """``writer = save_config; writer(config)`` must not hide the edge either.

    @9bbc8ed7 closed the import half and MEASURED that this half stayed green
    rather than assuming it, which is why it could be closed deliberately later
    instead of discovered. Their argument applies unchanged here and is the
    reason base rate is the wrong lens: this guard's whole job is the change
    nobody anticipated, so a blind spot in it is not an unlikely spelling — it is
    the one spelling that also happens to be invisible.

    Resolution follows a chain (``a = save_config; b = a``) and through an import
    alias (``import save_config as _p; w = _p``), with a cycle guard so a
    pathological ``a = b; b = a`` terminates rather than hanging a test run.

    STILL INVISIBLE, and stated rather than implied: a call through an attribute,
    a dict entry, a closure or anything built at run time. Over-approximating
    remains the safe direction — an extra edge only widens the closure, so a
    clean result stays trustworthy.
    """
    module = tmp_path / "rebound.py"
    module.write_text(
        "from aisquare.core.config import save_config\n\n"
        "writer = save_config\n\n"
        "def writes_through_a_rebinding(config: object) -> None:\n"
        "    writer(config)\n",
        encoding="utf-8",
    )

    _defines, calls = _call_graph([module])

    assert "save_config" in calls["writes_through_a_rebinding"], (
        "a rebinding hid the edge: the closure cannot see a config write reached "
        "through `writer = save_config`"
    )


def test_a_rebinding_chain_and_an_aliased_rebinding_both_resolve(tmp_path: Path) -> None:
    """The two compositions of the two alias forms, since each was closed alone."""
    chained = tmp_path / "chained.py"
    chained.write_text(
        "from aisquare.core.config import save_config\n\n"
        "first = save_config\nsecond = first\n\n"
        "def writes_through_a_chain(config: object) -> None:\n"
        "    second(config)\n",
        encoding="utf-8",
    )
    through_import = tmp_path / "both.py"
    through_import.write_text(
        "from aisquare.core.config import save_config as _persist\n\n"
        "writer = _persist\n\n"
        "def writes_through_both(config: object) -> None:\n"
        "    writer(config)\n",
        encoding="utf-8",
    )

    _d1, chain_calls = _call_graph([chained])
    _d2, both_calls = _call_graph([through_import])

    assert "save_config" in chain_calls["writes_through_a_chain"]
    assert "save_config" in both_calls["writes_through_both"], (
        "an import alias rebound through assignment lost the edge — the two "
        "resolutions must compose, not merely coexist"
    )


def test_a_circular_rebinding_terminates(tmp_path: Path) -> None:
    """`a = b; b = a` is nonsense a human would not write and a parser can meet.

    Asserted because the resolver follows chains: without a cycle guard this
    hangs the whole suite rather than failing, which is the worst failure shape
    a test file can have.
    """
    module = tmp_path / "circular.py"
    module.write_text(
        "a = b\nb = a\n\ndef calls_a(config: object) -> None:\n    a(config)\n",
        encoding="utf-8",
    )

    _defines, calls = _call_graph([module])

    assert "calls_a" in calls
