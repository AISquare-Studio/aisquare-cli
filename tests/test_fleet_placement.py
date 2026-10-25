"""Every tmux server the fleet makes, and every pane, session, socket or path it names to
one, comes from a launch record, tmux's own answer or today's config — by construction.

The store (``context.db``) is writable from an agent's sandbox, so no value read from it
may say where the fleet types, reads, probes or kills: not a row's ``pane_id``,
``tmux_socket``, ``socket_path``, ``agent`` or ``label``, not a project's codename
(``core.launch_seal``). Rounds r5 to r8 of #113 each found one more path that handed one
of them to tmux. So the rule is structural, and this test enumerates it over the fleet's
service and the board's (``services/fleet.py``, ``services/team.py``), the doctor
(``services/diagnostics.py``) and asq's views (``cli/ui``), keyed on the ``TmuxServer``
handle itself (review r8 of #113, finding 11):

1. Every place a ``TmuxServer`` is MADE — ``TmuxServer(…)``, ``.at(path)``, ``server_for``,
   the fleet's ``server()`` factory — is listed in :data:`MADE` with where its socket and
   path come from. ``_server_at`` takes an ``_Address``, which only the mints below make.
2. Every call of a ``TmuxServer`` method that names a pane, a session, a socket or a path
   (read off ``core/tmux.py``: its first parameter is one), of ``run`` (any arguments), of
   ``kill_server``, and of the module's socket probes (``server_may_run``,
   ``socket_answer``), is inside a method of the resolver's handles (``PlacedPane``,
   ``PlacedSession``) or in a site :data:`ACTS` lists with the reason.
3. ``PlacedPane``, ``PlacedSession`` and ``_Address`` are made only by :data:`MINTS`, each
   from the expression named there (a record's place, tmux's own answer, today's config),
   and what turns recorded values into an address (``_address``) or lists sessions by name
   (``_listed_session``) is called only where :data:`CALLERS` says.

A next path that makes a server or names a target to one anywhere else fails here, naming
the file and line. Widening a list is a decision to review, not a fix.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"
TMUX = SRC / "core" / "tmux.py"
FLEET = "services/fleet.py"
SERVICES = (FLEET, "services/team.py")
MODULES = (*SERVICES, "services/diagnostics.py")
UI = "cli/ui"

#: The resolver's handles: the only classes whose methods put a target to tmux unlisted.
HANDLES = frozenset({"PlacedPane", "PlacedSession"})

#: ``module`` → ``site`` → where the servers it makes are pointed.
MADE: dict[str, dict[str, str]] = {
    FLEET: {
        "server": "the factory: the socket today's config names, or one a caller passes",
        "_window_path": (
            "today's socket (the config's), asked only its global PATH, which every window "
            "it opens inherits (review r14 of #113, finding 6); nothing named"
        ),
        "server_for": "a socket's name (`server`): an address's name, or today's to compare",
        "_server_at": "an `_Address`, which only the mints in MINTS make",
        "_address": "the recorded socket's name, to compare its path with this shell's",
        "_require_usable_tmux": "today's socket (the version is asked, nothing named)",
        "spawn": "today's socket, where this spawn starts its window",
        "attach_argv": "today's socket, for `require` before a record's session is asked",
        "_cannot_run": "today's socket, for a socket probe only (ACTS)",
        "_refuse_a_replay_that_cannot_start": (
            "today's socket, asked whether a running server lost its path before a replay "
            "stops its agent (`_refuse_a_freed_socket`); nothing named"
        ),
        "_refuse_a_cross_agent_start": (
            "today's socket, asked as the replay's refusal asks it, before a hand-off onto "
            "another agent stops its agent (task #15); nothing named"
        ),
    },
    "services/diagnostics.py": {
        "_check_tmux": "the default socket: is tmux installed, which version",
        "_check_fleet_terminal": "the fleet's socket from config: its terminal settings",
        "_fleet_server": "the doctor's factory: a socket's name from config or a record",
        "_check_fleet": "today's socket, and each address a launch record places",
    },
    f"{UI}/terminal.py": {"TerminalPane.attach": "today's socket, when a view gave none"},
    f"{UI}/views/accounts.py": {"AccountsView.server": "today's socket: its own sign-in windows"},
    f"{UI}/views/agent.py": {
        "AgentView.__init__": "a listed status's socket and path: placed by the resolver"
    },
    f"{UI}/views/project.py": {
        "ManagerTab.show": "a listed status's socket and path: placed by the resolver"
    },
}

#: ``module`` → ``site`` → why it may name a pane, session, socket or path to tmux without a
#: handle. Each is one the store has no say in.
ACTS: dict[str, dict[str, str]] = {
    FLEET: {
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
        "_server_at": "`.at(path)`: an `_Address`'s path, minted from a record (MINTS)",
        "_unreached": "a socket probe at the path a placed row's launch record holds",
        "_refuse_unless_gone": "a socket probe at the path a placed row's launch record holds",
        "_socket_gone": (
            "a socket probe at the path a placed pane's launch record holds (`tell`'s reason "
            "when that socket is gone)"
        ),
        "_cannot_run": (
            "a socket probe at a row's own `socket_path` for a row no record places, only "
            "ever to END that row: a sandbox that wrote it could end its row itself"
        ),
        "type_then_enter": (
            "types into the pane its caller names: a placed pane's "
            "(`PlacedPane.type_then_enter`), or the pane of an agent a listing placed "
            "(Remote's paste, its row through `placed_row`)"
        ),
        "pane_is_the_agent": (
            "asks the pane its caller names, as `type_then_enter` types into it: the pane of an "
            "agent a listing placed, or of a row put through `placed_row` (the captain's "
            "`press` and `paste`, `services.captain`)"
        ),
        "_live_rows_here": (
            "a socket probe at this shell's path for the live rows recorded there, only ever "
            "to forget an exited server's start (the row is then judged by its record's time, "
            "never placed by it) or to refuse a spawn"
        ),
        "_before_a_server_starts": (
            "today's socket, asked before a server may start there (`open_window`)"
        ),
        "open_window": (
            "a window at today's socket in the session its caller names: a new one "
            "(`_new_session_window`, `create=True`) or the Accounts view's sign-in window"
        ),
        "_refuse_a_replay_that_cannot_start": (
            "today's socket, a freed path probed before a replay stops its agent"
        ),
        "_refuse_a_cross_agent_start": (
            "today's socket, a freed path probed before a hand-off onto another agent "
            "stops its agent"
        ),
    },
    "services/diagnostics.py": {
        "_check_fleet": (
            "each placed row's pane and socket, as `fleet.resolve_place` placed them, read "
            "as written (the doctor takes no census)"
        ),
        "_check_fleet_terminal": "`show-options -gv prefix`: the server's own option",
        "_server_environment": (
            "`show-environment -g`: the server's own environment (the stale-variable and the "
            "pinned-team-variable rows, 0.8.2's `fleet server env`)"
        ),
    },
    f"{UI}/terminal.py": {
        "TerminalPane": (
            "the pane a view attached (`shown_pane`): a listed status's, placed by the "
            "resolver, or a sign-in window the Accounts view made itself; its keys and frames "
            "go down that pane's server's control client (`core.tmux_control`, 0.8.2's #272) "
            "or are polled, for that pane alone"
        )
    },
    f"{UI}/views/accounts.py": {
        "AccountsView._settle_login_quietly": "the sign-in window this view made",
        "AccountsView._poll_login": "the sign-in window this view made",
        "AccountsView._close_login_window": "the sign-in window this view made",
        "AccountsView._login_polled": "the sign-in window this view made",
    },
}

#: ``(site, class made)`` → the source of the pane id or the session target each mint
#: takes, as written there (``None``: ``_address``, whose callers CALLERS restricts).
MINTS: dict[tuple[str, str], str | None] = {
    ("resolve_place", "PlacedPane"): "place.pane_id",
    ("resolve_place", "PlacedSession"): "place.session_id",
    ("_indexed_sessions", "PlacedSession"): "place.session_id",
    ("_legacy_panes", "PlacedPane"): "place.pane_id",
    ("_recordless_pane", "PlacedPane"): "window.pane_id",
    ("PlacedPane.holding_session", "PlacedSession"): "session_id",
    ("_listed_session", "PlacedSession"): "name",
    ("_address", "_Address"): None,
    ("_session_to_join", "_Address"): "config.tmux_socket",
    ("_shutdown_sockets", "_Address"): "config.tmux_socket",
    ("_live_rows_here", "_Address"): "config.tmux_socket",
}

#: ``callee`` → the sites that may call it in ``services/fleet.py``.
CALLERS: dict[str, frozenset[str]] = {
    # Recorded values (a record's place, a session the index holds) to an address.
    "_address": frozenset(
        {
            "resolve_place",
            "_indexed_sessions",
            "_legacy_panes",
            "_running_ids",
            # Remote's handles on an agent a listing placed (`placed_row`): its fields are
            # its record's.
            "server_of",
            "socket_at",
            # The live rows at this shell's path, to forget an exited server's start or
            # refuse a spawn (ACTS).
            "_live_rows_here",
        }
    ),
    # Session names tmux itself listed, for `shutdown --all`'s sweep; or the census holds,
    # whose panes alone are matched in it (`_running_ids`).
    "_listed_session": frozenset({"_swept_sessions", "_running_ids"}),
}

_TARGETS = frozenset({"pane_id", "session", "name", "old", "recorded", "path"})
_PROBES = frozenset({"server_may_run", "socket_answer"})
#: A receiver that is a server, outside the fleet's service (where names are unambiguous).
_SERVER = re.compile(r"(?:\b|_)(?:server|srv|probe|servers\[[^]]*\])\b")
#: Calls that are no tmux server's, whatever their name (`subprocess.run`, `app.run`).
_NOT_TMUX = frozenset({"subprocess", "os", "shutil", "app", "asyncio", "project_service"})


def tmux_target_methods() -> frozenset[str]:
    """The ``TmuxServer`` methods that name a pane, a session, a socket or a path, read off
    ``core/tmux.py`` (their first parameter is one), with ``run`` (any arguments) and
    ``kill_server`` (every session at once)."""
    tree = ast.parse(TMUX.read_text(encoding="utf-8"))
    server = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TmuxServer"
    )
    methods = {"run", "kill_server"}
    for node in server.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("__"):
            continue
        args = node.args.args
        if len(args) > 1 and args[1].arg in _TARGETS:
            methods.add(node.name)
    return frozenset(methods)


def _sites(tree: ast.AST) -> Iterator[tuple[str, str | None, ast.Call]]:
    """``(site, class, call)`` for every call: the site is ``Class.method`` for a method and
    the function's name otherwise, a function nested in one counting as that one (an
    exemption is never had by naming an inner function after it)."""

    def walk(
        node: ast.AST, site: str, klass: str | None
    ) -> Iterator[tuple[str, str | None, ast.Call]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, site, child.name)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                outer = (
                    (f"{klass}.{child.name}" if klass else child.name)
                    if site == "<module>"
                    else site
                )
                yield from walk(child, outer, klass)
            else:
                if isinstance(child, ast.Call):
                    yield site, klass, child
                yield from walk(child, site, klass)

    yield from walk(tree, "<module>", None)


def _called(call: ast.Call) -> tuple[str | None, str | None]:
    """``(name, receiver source)``: ``f`` for ``f(…)``, ``f`` and ``x`` for ``x.f(…)``."""
    if isinstance(call.func, ast.Attribute):
        return call.func.attr, ast.unparse(call.func.value)
    if isinstance(call.func, ast.Name):
        return call.func.id, None
    return None, None


def _allowed(table: dict[str, dict[str, str]], module: str, site: str, klass: str | None) -> bool:
    listed = table.get(module, {})
    return site in listed or (klass is not None and klass in listed)


def violations(source: str, module: str, methods: frozenset[str]) -> list[str]:
    """Every breach of the three rules in ``source`` (the module at ``module``)."""
    found: list[str] = []
    service = module in SERVICES
    for site, klass, call in _sites(ast.parse(source)):
        name, receiver = _called(call)
        if name is None:
            continue
        root = receiver.split(".", 1)[0] if receiver else None
        if root in _NOT_TMUX:
            continue
        in_handle = service and klass in HANDLES
        # 1. A server made.
        made = (
            name in {"TmuxServer", "server_for"}
            or (name == "at" and receiver is not None)
            or (name == "server" and (receiver is None or receiver.endswith("fleet_service")))
        )
        if made and not in_handle and not _allowed(MADE, module, site, klass):
            found.append(f"{call.lineno}: a tmux server made in {site}")
        # 2. A target named to one.
        acts_on_server = receiver is not None and (service or bool(_SERVER.search(receiver)))
        names_target = (name in methods and acts_on_server) or name in _PROBES
        if name == "listener" and call.args:
            names_target = True
        if names_target and not in_handle and not _allowed(ACTS, module, site, klass):
            found.append(f"{call.lineno}: tmux `{name}` in {site} names a target")
        # 3. Mints and callers (the fleet's service).
        if module == FLEET and receiver is None and name in {*HANDLES, "_Address"}:
            mint = (site, name)
            if mint not in MINTS:
                found.append(f"{call.lineno}: {name} made in {site}, not by the resolver")
            elif MINTS[mint] is not None and _source(call) != MINTS[mint]:
                found.append(
                    f"{call.lineno}: {name} made in {site} from {_source(call)!r}, "
                    f"not {MINTS[mint]!r}"
                )
        if module == FLEET and name in CALLERS and site not in CALLERS[name]:
            found.append(f"{call.lineno}: {name} called in {site}")
    return found


def _source(call: ast.Call) -> str | None:
    """The source of a mint's pane id or session target: its second argument (an
    ``_Address``'s first)."""
    if isinstance(call.func, ast.Name) and call.func.id == "_Address":
        return ast.unparse(call.args[0]) if call.args else None
    return ast.unparse(call.args[1]) if len(call.args) >= 2 else None


def _scanned() -> list[tuple[str, Path]]:
    files = [(module, SRC / module) for module in MODULES]
    files += [(path.relative_to(SRC).as_posix(), path) for path in sorted((SRC / UI).rglob("*.py"))]
    return files


def test_the_tmux_methods_that_name_a_target_are_the_ones_the_fleet_acts_with() -> None:
    """What the enumeration keys on is read off ``TmuxServer``, so a method added there is
    covered without editing this test; these are the ones every path above acted with."""
    methods = tmux_target_methods()
    acted = {
        "send_keys", "send_literal", "paste", "kill_window", "kill_session", "pane_facts",
        "pane_facts_or_raise", "pane_pid", "pane_session", "screen_text", "list_windows",
        "windows_or_raise", "has_session", "has_session_or_raise", "rename_session",
        "attach_argv", "spawn_window", "run", "kill_server", "at", "listener", "capture",
    }  # fmt: skip
    assert acted <= methods, sorted(acted - methods)


def test_every_tmux_server_and_target_comes_from_a_record_tmux_or_the_config() -> None:
    methods = tmux_target_methods()
    found = {
        module: hits
        for module, path in _scanned()
        if (hits := violations(path.read_text(encoding="utf-8"), module, methods))
    }
    assert not found, (
        "a tmux server is made, or a pane, session, socket or path named to one, where no "
        "launch record, tmux answer or config says so (`fleet.resolve_place`), or a place "
        f"is made from anything else: {found}"
    )


def test_every_listed_site_still_exists() -> None:
    """An entry whose site is gone is a stale exemption: removed, not kept."""
    for table in (MADE, ACTS):
        for module, sites in table.items():
            defined = {
                site for site, _, _ in _sites(ast.parse((SRC / module).read_text(encoding="utf-8")))
            }
            classes = {
                node.name
                for node in ast.walk(ast.parse((SRC / module).read_text(encoding="utf-8")))
                if isinstance(node, ast.ClassDef)
            }
            stale = {site for site in sites if site not in defined and site not in classes}
            assert not stale, (module, sorted(stale))
    fleet = {site for site, _, _ in _sites(ast.parse((SRC / FLEET).read_text(encoding="utf-8")))}
    named = {site for site, _ in MINTS} | {s for callers in CALLERS.values() for s in callers}
    assert named <= fleet, sorted(named - fleet)


def test_the_check_sees_every_shape_of_the_breach() -> None:
    methods = tmux_target_methods()
    breaches = [
        # The r5-r8 shapes: a row's pane typed into, killed, asked about; a session named
        # from the codename taken down; a place made from a row; a row's socket probed or
        # made a server of; a server made from a row in a view.
        (FLEET, "def tell(srv, agent):\n    srv.paste(agent.pane_id, 'x')"),
        (FLEET, "def _stop_row(agent):\n    server_for(agent.tmux_socket).kill_window('%1')"),
        (FLEET, "def _pane_alive(srv, agent):\n    return srv.pane_facts(agent.pane_id)"),
        (FLEET, "def shutdown(srv, p):\n    srv.kill_session(session_name(p.codename))"),
        (FLEET, "def resolve_place(row, config):\n    PlacedPane(a, row.pane_id, t, config)"),
        (FLEET, "def _stop_row(row, config):\n    PlacedPane(a, row.pane_id, t, config)"),
        (FLEET, "def tell(row):\n    _Address(row.tmux_socket, row.socket_path)"),
        (FLEET, "def tell(row):\n    _address(row.tmux_socket, row.socket_path)"),
        (FLEET, "def reap(row):\n    server().at(row.socket_path).kill_server()"),
        (FLEET, "def reap(row, srv):\n    tmux_core.server_may_run(srv, row.socket_path, b, c)"),
        (FLEET, "def reap(row, srv):\n    srv.listener(row.socket_path)"),
        (FLEET, "def tell(name):\n    _listed_session(a, name, c)"),
        (
            FLEET,
            "def tell(srv, row):\n    def _kill_unrecorded():\n"
            "        srv.kill_window(row.pane_id)",
        ),
        (
            "services/team.py",
            "def _pane_holds(agent):\n    server_of(agent).pane_pid(agent.pane_id)",
        ),
        ("services/diagnostics.py", "def _check_x(row):\n    TmuxServer(row.tmux_socket)"),
        (f"{UI}/views/x.py", "def show(row):\n    TmuxServer(row.tmux_socket).at(row.socket_path)"),
        (
            f"{UI}/views/x.py",
            "class V:\n    def go(self, row):\n        self.server.send_keys(row.pane_id, 'y')",
        ),
    ]
    for module, source in breaches:
        assert violations(source, module, methods), source
    allowed = [
        (
            FLEET,
            "class PlacedPane:\n    def kill(self):\n"
            "        self.server().kill_window(self.pane_id)",
        ),
        (
            FLEET,
            "def resolve_place(row, place, config):\n    PlacedPane(a, place.pane_id, t, config)",
        ),
        (
            FLEET,
            "def _recordless_pane(window, s):\n    PlacedPane(s.address, window.pane_id, t, c)",
        ),
        (FLEET, "def _new_session_window(srv, name):\n    srv.spawn_window(name, create=True)"),
        (FLEET, "def tell(placed):\n    placed.pane.type_line('x')"),
        (FLEET, "def reap(address, config):\n    _server_at(address, config).reachable()"),
        (FLEET, "def _git(cwd):\n    subprocess.run(['git'])"),
        (f"{UI}/app.py", "def run_ui(app):\n    app.run()"),
    ]
    for module, source in allowed:
        assert not violations(source, module, methods), source


def test_the_control_mode_client_is_made_only_for_a_pane_a_view_attached() -> None:
    """0.8.2's tmux control mode (#272) is a tmux path of its own: one ``tmux -C`` client per
    server (``core.tmux_control.ControlClient``) that carries a pane's keys and frames. It
    acts only on placed panes because it is made in one place, for the server of a
    ``TerminalPane`` on screen (``cli/ui/terminal.py``, ``_Control``), and is asked only
    about that pane and the panes that join it on that server (``watch_pane``): each one a
    view attached (``shown_pane``: a listed status's, placed by the resolver), or a sign-in
    window the Accounts view made itself. A client made anywhere else fails here."""
    made = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for owner in ast.walk(tree):
            if not isinstance(owner, ast.ClassDef | ast.FunctionDef):
                continue
            for node in ast.walk(owner):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ControlClient"
                ):
                    made.append(f"{path.relative_to(SRC).as_posix()}:{owner.name}")
    assert sorted(set(made)) == ["cli/ui/terminal.py:_Control", "cli/ui/terminal.py:__init__"], made
