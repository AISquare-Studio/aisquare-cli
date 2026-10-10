"""No remote function may share a bare name with what the hook path calls (SPEC §0.1, §7.2).

``test_config_writes_stay_in_the_cli.py`` builds its call graph by bare NAME, so a call
to ``flush`` is an edge to every function named ``flush``. This branch alone was green
and #240 alone was green; their merge was red, 31 offenders across ``hooks.py``,
``mcp_server.py`` and ``serve.py``, every chain passing through a remote function whose
name a hook-path function also calls::

    mcp_server._serve_stdio_until_idle → flush[remote_server] → _save → bind_role → save_config

Two rules keep remote code out of that graph whatever the next branch adds:

* **N1.** No function defined in a remote module has a name that non-remote code
  reaches, by name, from a hook, MCP, serve or sweeper entry point (framework
  protocol names aside, which no rename can change).
* **N2.** No remote function shares its name with a non-remote one at all, but for
  framework protocol names (dunders, ``compose``, ``on_*``, ``action_*``, ``watch_*``)
  and the grandfathered few, a set that may only shrink.

This is the branch-local guard. The cross-branch one is the trial merge with #240
(SPEC §8.5), which runs this file on the merged tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.test_config_writes_stay_in_the_cli import NON_CLI_SURFACES, SRC, _call_graph

#: The remote modules, as paths under ``src/aisquare``.
REMOTE = (
    "services/remote_",
    "services/transcript.py",
    "services/ngrok_tunnel.py",
    "cli/remote.py",
    "cli/ui/remote_control.py",
    "cli/ui/views/remote.py",
)

#: Remote names that may stay shared: ``Runtime.token``, which no hook-path code
#: reaches, and renaming it churns about sixty test lines. This set may only shrink:
#: ``build_app`` left it when #240's own ``build_app`` turned out to be reached by name
#: from the hook path, and the remote def became ``build_remote_app``.
GRANDFATHERED = frozenset({"token"})


def _is_remote(module: Path) -> bool:
    return module.relative_to(SRC).as_posix().startswith(REMOTE)


def _is_framework(name: str) -> bool:
    """A name a framework calls by protocol, which no rename could change."""
    dunder = name.startswith("__") and name.endswith("__")
    return dunder or name == "compose" or name.startswith(("on_", "action_", "watch_"))


def _defined(modules: list[Path]) -> dict[str, set[str]]:
    """Every function and method name defined in ``modules``, with where."""
    names: dict[str, set[str]] = {}
    for module in modules:
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.setdefault(node.name, set()).add(module.name)
    return names


def _reached_from_the_surfaces(non_remote: list[Path]) -> set[str]:
    """Every name non-remote code reaches by name from a non-CLI entry point."""
    _defines, calls = _call_graph(non_remote)
    surfaces = [module for module in non_remote if module.name in NON_CLI_SURFACES]
    seen = set(_defined(surfaces))
    stack = list(seen)
    while stack:
        for callee in calls.get(stack.pop(), ()):
            if callee not in seen:
                seen.add(callee)
                stack.append(callee)
    return seen


def _modules() -> tuple[list[Path], list[Path]]:
    modules = sorted(SRC.rglob("*.py"))
    return [m for m in modules if _is_remote(m)], [m for m in modules if not _is_remote(m)]


def _n1_offenders(remote: list[Path], non_remote: list[Path]) -> dict[str, set[str]]:
    """Remote names the hook path reaches. A framework name is exempt here as in N2: no
    rename can change ``__init__``, and that the hook path reaching a remote one reaches
    no config write is for the whole-graph guard to prove, which it does."""
    reached = _reached_from_the_surfaces(non_remote)
    return {
        name: where
        for name, where in _defined(remote).items()
        if name in reached and not _is_framework(name)
    }


def _n2_offenders(remote: list[Path], non_remote: list[Path]) -> dict[str, set[str]]:
    others = _defined(non_remote)
    return {
        name: where
        for name, where in _defined(remote).items()
        if name in others and not _is_framework(name) and name not in GRANDFATHERED
    }


def test_no_remote_function_is_reached_by_name_from_the_hook_path() -> None:
    remote, non_remote = _modules()
    offenders = _n1_offenders(remote, non_remote)
    assert not offenders, (
        f"hook-path code calls these names, and remote modules define them: {offenders}. "
        "Prefix the remote function with its area (kit_, needs_, push_, action_, ledger_, "
        "remote_, page_), SPEC §0.1."
    )


def test_no_remote_function_shares_a_name_with_non_remote_code() -> None:
    remote, non_remote = _modules()
    offenders = _n2_offenders(remote, non_remote)
    assert not offenders, (
        f"remote and non-remote code both define: {offenders}. A shared name is one call "
        "away from an N1 entry point on the next merge; prefix the remote one (SPEC §0.1)."
    )


def test_the_walk_reaches_the_hook_path_and_the_rules_can_fail(tmp_path: Path) -> None:
    """The control: an empty answer from a walk that reached nothing proves nothing."""
    remote, non_remote = _modules()
    assert len(_reached_from_the_surfaces(non_remote)) > 200
    assert len(remote) >= 9, [m.name for m in remote]
    synthetic = tmp_path / "remote_synthetic.py"
    synthetic.write_text(
        "def flush() -> None: ...\n"  # the PR's own Runtime.flush, before the rename
        "def save_config() -> None: ...\n"
        "def on_mount() -> None: ...\n"
        "def compose() -> None: ...\n",
        encoding="utf-8",
    )
    assert "flush" in _n1_offenders([synthetic], non_remote)
    flagged = _n2_offenders([synthetic], non_remote)
    assert {"flush", "save_config"} <= set(flagged)
    assert not {"on_mount", "compose"} & set(flagged), "framework names stay shared"
