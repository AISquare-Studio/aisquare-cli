"""Every tmux act of the fleet is put to a place a launch record holds — by construction.

The store (``context.db``) is writable from an agent's sandbox, so no value read from it
may say where the fleet types, reads or kills: not a row's ``pane_id``, ``tmux_socket``,
``socket_path`` or ``agent``, not a project's codename (``core.launch_seal``). Rounds r5,
r6 and r7 of #113 each found one more path that handed one of them to tmux — typing,
``/exit`` and the kill, a restart's running check, the late panes, ``shutdown``'s sessions
— and each fix closed that path alone. So the rule is structural, and this test enumerates
it over ``services/fleet.py`` and ``services/team.py``:

1. A ``TmuxServer`` method that names a pane or a session (read off ``core/tmux.py``: its
   first parameter is a pane id or a session) is called only inside a method of
   ``PlacedPane`` or ``PlacedSession``, the resolver's handles, or in a function
   :data:`ALLOWED` names with the reason.
2. ``PlacedPane``, ``PlacedSession`` and ``_Address`` are made only by the functions
   :data:`MINTS` names, each from the expression it names: a launch record's
   ``WindowPlace``, or what tmux itself listed.
3. What turns a row's columns or a socket's name into a server (``_address``,
   ``server_for``) and the sweep's listed sessions (``_listed_session``) are called only
   where :data:`CALLERS` says.

A next path that puts a pane or a session to tmux from anywhere else, or mints a place from
a row, fails here, naming the line. Widening a list is a decision to review, not a fix.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"
TMUX = SRC / "core" / "tmux.py"
MODULES = (SRC / "services" / "fleet.py", SRC / "services" / "team.py")

#: The resolver's handles: the only classes whose methods put a pane or a session to tmux.
HANDLES = frozenset({"PlacedPane", "PlacedSession"})

#: ``function`` → why it may name a pane or a session to tmux without a handle. Each is one
#: the store has no say in.
ALLOWED: dict[str, str] = {
    "_new_session_window": (
        "makes a NEW session (`spawn_window(create=True)` never joins one); a name is "
        "only probed for being free, so no name read from the store chooses a session a "
        "window lands in (review r7 of #113, finding 2)"
    ),
    "_kill_unrecorded": (
        "kills the window this spawn's own `spawn_window` just made, by the pane tmux "
        "answered, when its place or its row could not be recorded"
    ),
    "_activity_times": "`list-panes -a`: every pane of a server, no pane or session named",
}

#: ``(function, class made)`` → the source of the pane id or the session target each mint
#: takes, as written there. ``_Address`` is made from a row's columns only in ``_address``,
#: which only the resolver calls (:data:`CALLERS`), else from today's config.
MINTS: dict[tuple[str, str], str | None] = {
    ("resolve_place", "PlacedPane"): "place.pane_id",
    ("resolve_place", "PlacedSession"): "place.session_id",
    ("_recordless_pane", "PlacedPane"): "window.pane_id",
    ("_listed_session", "PlacedSession"): "name",
    ("_address", "_Address"): None,
    ("_session_to_join", "_Address"): "config.tmux_socket",
    ("_shutdown_sockets", "_Address"): "config.tmux_socket",
}

#: ``callee`` → the functions that may call it.
CALLERS: dict[str, frozenset[str]] = {
    # A row's columns (placed by the resolver) to the server it is asked at.
    "_address": frozenset({"resolve_place"}),
    # A socket's name to a server: today's socket compared, or an address asked.
    "server_for": frozenset({"_address", "_server_at"}),
    # Session names tmux itself listed, for `shutdown --all`'s sweep.
    "_listed_session": frozenset({"_swept_sessions"}),
}

_TARGETS = frozenset({"pane_id", "session", "name", "old"})


def tmux_target_methods() -> frozenset[str]:
    """The ``TmuxServer`` methods that name a pane or a session, read off ``core/tmux.py``:
    whose first parameter after ``self`` is a pane id or a session — and ``run``, which
    takes any arguments at all."""
    tree = ast.parse(TMUX.read_text(encoding="utf-8"))
    server = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TmuxServer"
    )
    methods = {"run"}
    for node in server.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        args = node.args.args
        if len(args) > 1 and args[1].arg in _TARGETS:
            methods.add(node.name)
    return frozenset(methods)


def _scopes(tree: ast.AST) -> Iterator[tuple[str, str | None, ast.AST]]:
    """``(function, class, node)`` for every node, with the top-level function or the
    method it is in (a function nested in one counts as that one: an exemption is never
    had by naming an inner function after it) and its class."""

    def walk(
        node: ast.AST, function: str, klass: str | None
    ) -> Iterator[tuple[str, str | None, ast.AST]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, function, child.name)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                outer = child.name if function == "<module>" else function
                yield outer, klass, child
                yield from walk(child, outer, klass)
            else:
                yield function, klass, child
                yield from walk(child, function, klass)

    yield from walk(tree, "<module>", None)


def _called(node: ast.AST) -> str | None:
    """The name a call calls: ``f`` for ``f(…)`` and ``x.f(…)`` alike."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _of_subprocess(func: ast.expr) -> bool:
    """``subprocess.run(…)``: git and a native queue, no tmux."""
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _target_source(call: ast.Call) -> str | None:
    """The source of a mint's pane id or session target: its second argument."""
    if len(call.args) >= 2:
        return ast.unparse(call.args[1])
    if call.args and isinstance(call.func, ast.Name) and call.func.id == "_Address":
        return ast.unparse(call.args[0])
    return None


def violations(source: str, methods: frozenset[str]) -> list[str]:
    """Every breach of the three rules in ``source``, as ``line: what``."""
    found: list[str] = []
    tree = ast.parse(source)
    for function, klass, node in _scopes(tree):
        name = _called(node)
        if name is None:
            continue
        assert isinstance(node, ast.Call)
        is_attribute = isinstance(node.func, ast.Attribute)
        if _of_subprocess(node.func):
            continue
        if is_attribute and name in methods and klass not in HANDLES and function not in ALLOWED:
            found.append(f"{node.lineno}: tmux `{name}` in {function} names a pane or session")
        if not is_attribute and name in {*HANDLES, "_Address"}:
            mint = (function, name)
            if mint not in MINTS:
                found.append(f"{node.lineno}: {name} made in {function}, not by the resolver")
            elif MINTS[mint] is not None and _target_source(node) != MINTS[mint]:
                found.append(
                    f"{node.lineno}: {name} made in {function} from "
                    f"{_target_source(node)!r}, not {MINTS[mint]!r}"
                )
        if name in CALLERS and function not in CALLERS[name]:
            found.append(f"{node.lineno}: {name} called in {function}")
    return found


def test_the_tmux_methods_that_name_a_target_are_the_ones_the_fleet_acts_with() -> None:
    """What the enumeration keys on is read off ``TmuxServer``, so a method added there is
    covered without editing this test; these are the ones every path above acted with."""
    methods = tmux_target_methods()
    acted = {
        "send_keys", "send_literal", "paste", "kill_window", "kill_session", "pane_facts",
        "pane_facts_or_raise", "pane_pid", "screen_text", "list_windows", "windows_or_raise",
        "has_session", "has_session_or_raise", "rename_session", "attach_argv",
        "spawn_window", "run",
    }  # fmt: skip
    assert acted <= methods, sorted(acted - methods)


def test_every_tmux_act_of_the_fleet_goes_through_a_place_a_launch_record_holds() -> None:
    methods = tmux_target_methods()
    found = {
        path.relative_to(SRC.parent): violations(path.read_text(encoding="utf-8"), methods)
        for path in MODULES
    }
    assert not any(found.values()), (
        "a tmux call names a pane or a session the resolver (`fleet.resolve_place`) did not "
        "place, or a place is made from something other than a launch record or tmux's own "
        f"listing: {found}"
    )


def test_every_allowed_site_and_mint_still_exists() -> None:
    """An entry whose function is gone is a stale exemption: removed, not kept."""
    defined: set[str] = set()
    for path in MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.ClassDef):
                defined.add(node.name)
    named = {*ALLOWED, *(function for function, _ in MINTS), *CALLERS, *HANDLES}
    for callers in CALLERS.values():
        named |= callers
    assert named <= defined, sorted(named - defined)


def test_the_check_sees_every_shape_of_the_breach() -> None:
    methods = tmux_target_methods()
    breaches = [
        # The r5-r7 shapes: a row's pane typed into, killed, asked about; a session named
        # from the codename taken down; a place made from a row.
        "def tell(srv, agent):\n    srv.paste(agent.pane_id, 'x')",
        "def _stop_row(agent):\n    server_of(agent).kill_window(agent.pane_id)",
        "def _pane_alive(srv, agent):\n    return srv.pane_facts(agent.pane_id)",
        "def shutdown(srv, project):\n    srv.kill_session(session_name(project.codename))",
        "def restart(srv, name):\n    srv.has_session_or_raise(name)",
        "def resolve_place(row, config):\n    PlacedPane(a, row.pane_id, row.created_at, config)",
        "def _stop_row(row, config):\n    PlacedPane(a, row.pane_id, row.created_at, config)",
        "def tell(row):\n    _Address(row.tmux_socket, row.socket_path)",
        "def tell(row):\n    _address(row)",
        "def tell(row):\n    server_for(row.tmux_socket)",
        "def tell(name):\n    _listed_session(a, name, c)",
        "class Other:\n    def m(self, row):\n        self.srv.send_keys(row.pane_id, 'Enter')",
        "def tell(srv, row):\n    def _kill_unrecorded():\n        srv.kill_window(row.pane_id)",
    ]
    for source in breaches:
        assert violations(source, methods), source
    allowed = [
        "class PlacedPane:\n    def kill(self):\n        self.server().kill_window(self.pane_id)",
        "def resolve_place(row, place, config):\n    PlacedPane(a, place.pane_id, t, config)",
        "def _recordless_pane(window, s):\n    PlacedPane(s.address, window.pane_id, t, c)",
        "def _new_session_window(srv, name):\n    srv.spawn_window(name, create=True)",
        "def tell(placed):\n    placed.pane.type_line('x')",
        "def anything(config):\n    server(config).reachable()",
    ]
    for source in allowed:
        assert not violations(source, methods), source
