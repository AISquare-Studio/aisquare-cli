"""Every process this CLI starts is traced or explicitly excluded.

Tracing identity is process-level and inherited, so an *undecided* spawn is not
neutral — it silently mints a Run under whoever happened to be the parent. The
guard here is the latch on that: it walks the AST of the package and fails when
a call site exists that ``core.spawn.SEAMS`` has not ruled on.

AST rather than grep because grep matches the word ``subprocess.run`` in a
docstring and misses ``subprocess . run``; AST rather than runtime interception
because a seam that is never exercised by the suite would go unnoticed, and the
seams most likely to leak are exactly the rarely-run ones.
"""

from __future__ import annotations

import ast
import os
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import spawn
from aisquare.core.spawn import SEAMS, TRACED, TRACING_ENV_VARS, untraced_env
from aisquare.services import distill as _distill

#: The REAL ``spawn_drain``, captured at import. conftest's autouse
#: ``no_detached_distill`` replaces the module attribute so the suite never
#: launches a detached process — correct for every other test, and fatal for
#: this one, which exists to inspect what that launch would pass. Collection
#: imports this module before any fixture runs, so this binding is the original.
_REAL_SPAWN_DRAIN = _distill.spawn_drain

#: Callables that start a process. Matched on the ``os.``/``subprocess.``
#: attribute form the package actually uses; a bare ``from subprocess import
#: run`` is caught separately, because it would slip past this set.
_SPAWN_CALLS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "system",
        "popen",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "spawnv",
        "spawnve",
        "posix_spawn",
        "posix_spawnp",
    }
)
_SPAWN_MODULES = frozenset({"os", "subprocess"})

_PACKAGE = Path(spawn.__file__).parent.parent  # …/src/aisquare
_SRC = _PACKAGE.parent


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class _Sites(ast.NodeVisitor):
    """Collect ``<module>::<enclosing function>`` for every spawn call."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.found: set[str] = set()
        self.imported_spawns: set[str] = set()
        self._scope: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module in _SPAWN_MODULES:
            for alias in node.names:
                if alias.name in _SPAWN_CALLS:
                    self.imported_spawns.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func) if isinstance(node.func, ast.Attribute) else None
        bits = name.split(".") if name else []
        direct = isinstance(node.func, ast.Name) and node.func.id in self.imported_spawns
        attribute = len(bits) >= 2 and bits[0] in _SPAWN_MODULES and bits[-1] in _SPAWN_CALLS
        if direct or attribute:
            where = self._scope[-1] if self._scope else "<module>"
            self.found.add(f"{self.module}::{where}")
        self.generic_visit(node)


def _spawn_sites() -> set[str]:
    sites: set[str] = set()
    for path in sorted(_PACKAGE.rglob("*.py")):
        visitor = _Sites(str(path.relative_to(_SRC)))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        sites |= visitor.found
    return sites


# ── the guard ────────────────────────────────────────────────────────────────


def _undecided(sites: set[str], rulings: Iterable[str]) -> set[str]:
    """Spawn sites with no entry in the inventory.

    A CALLABLE rule rather than a set-difference inline in a test, so a control
    can drive it with known-bad input. Measured before it was extracted:
    replacing the expression with ``set()`` left ALL TWELVE TESTS PASSING — the
    guard reported that every spawn site has a ruling while comparing nothing.

    The scanner was already controlled, and that did not help: the control
    proves the SCANNER sees seams, not that the RULE consults it.
    """
    return sites - set(rulings)


def _stale(sites: set[str], rulings: Iterable[str]) -> set[str]:
    """Inventory entries whose call site is gone. Same shape, same reason."""
    return set(rulings) - sites


def test_every_spawn_site_has_a_written_ruling() -> None:
    """A new ``subprocess.run`` may not land without a tracing decision.

    If this fails you added a way to start a process. Decide what it is and add
    it to ``core.spawn.SEAMS``: an agent that should carry an identity is
    ``TRACED``; anything else is ``EXCLUDED``, and if it can reach a model it
    also takes ``untraced_env`` so it cannot inherit one.
    """
    undecided = _undecided(_spawn_sites(), SEAMS)
    assert not undecided, (
        "process-spawn site(s) with no tracing decision: "
        f"{sorted(undecided)} — add each to core.spawn.SEAMS"
    )


def test_the_inventory_describes_nothing_that_is_gone() -> None:
    """A registry that outlives its call sites stops being an inventory and
    starts being folklore."""
    stale = _stale(_spawn_sites(), SEAMS)
    assert not stale, f"core.spawn.SEAMS names call site(s) that no longer exist: {sorted(stale)}"


def test_the_undecided_rule_reports_a_site_with_no_ruling() -> None:
    """Positive control. Synthetic, so it keeps controlling when SEAMS changes."""
    assert _undecided({"mod.py::spawns"}, {"other.py::ruled"}) == {"mod.py::spawns"}


def test_the_stale_rule_reports_a_ruling_with_no_site() -> None:
    """Positive control, other direction, named separately so a failure says which."""
    assert _stale({"mod.py::spawns"}, {"gone.py::vanished"}) == {"gone.py::vanished"}


def test_neither_rule_reports_a_site_that_is_ruled() -> None:
    """Negative control: "report everything" is not a fix for either rule.

    Without this the cheapest way to satisfy both positives is a rule that
    always fires, and a guard that accuses every seam gets deleted rather than
    fixed.
    """
    both = {"mod.py::spawns"}

    assert _undecided(both, both) == set()
    assert _stale(both, both) == set()


def test_the_scanner_would_actually_catch_a_new_seam(tmp_path: Path) -> None:
    """The guard is only worth having if it fires. Proven on both shapes it
    claims to cover — the attribute call the package uses, and the bare
    ``from subprocess import run`` that would otherwise slip past."""
    for source in (
        "import subprocess\ndef leak():\n    subprocess.run(['x'])\n",
        "from subprocess import run\ndef leak():\n    run(['x'])\n",
    ):
        visitor = _Sites("probe.py")
        visitor.visit(ast.parse(source))
        assert visitor.found == {"probe.py::leak"}, source

    quiet = _Sites("probe.py")
    quiet.visit(ast.parse('"""subprocess.run is mentioned here."""\nX = 1\n'))
    assert quiet.found == set(), "a docstring mention is not a spawn"


def test_every_ruling_is_one_of_the_two() -> None:
    for key, seam in SEAMS.items():
        assert seam.decision in (TRACED, spawn.EXCLUDED), key
        assert seam.reason, f"{key} has a decision but no reason"
        assert not (seam.decision == TRACED and seam.strips_identity), key


def test_tracing_vars_agree_with_the_wiring_that_sets_them() -> None:
    """The two lists are separate only because ``core`` must not import
    ``services``. They describe the same thing, so they may not drift: a third
    identity variable added to the wiring must reach the strippers too."""
    from aisquare.services import explainability

    # Looked up by name, and under both spellings: the correlation-spine lane
    # makes this tuple public, so the test must survive either fold order
    # rather than pin the branch that happens to land first.
    reserved = getattr(explainability, "RESERVED_ENV_VARS", None) or getattr(
        explainability, "_RESERVED_ENV_VARS", ()
    )
    assert reserved, "neither spelling found — the wiring's reserved tuple was renamed again"

    # Compared as SEQUENCES, not sets. The set form was verified to bite on a
    # CONTENT difference — adding a third name to one list fails this test — but
    # it accepted a REORDER silently, measured.
    #
    # And order turned out NOT to be cosmetic, which I only learned by breaking
    # it: reordering this tuple also fails
    # test_harness.py::test_spawn_print_enabled_composes_a_fresh_eval, because
    # cli/team.py joins these names into `unset …` inside a shell snippet the CLI
    # PRINTS for a human to eval. The order is user-visible text, not just an
    # iteration order, so two lists that disagree about it disagree about output.
    assert tuple(TRACING_ENV_VARS) == tuple(reserved), (
        "core.spawn.TRACING_ENV_VARS and services.explainability.RESERVED_ENV_VARS "
        f"have drifted: {tuple(TRACING_ENV_VARS)} vs {tuple(reserved)}. They are the "
        "same list kept twice because core must not import services — a name in one "
        "and not the other means a seam stands down on a variable nothing strips, or "
        "a subprocess inherits one nothing stands down for."
    )


# ── the strip itself ─────────────────────────────────────────────────────────


@pytest.fixture
def traced_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment that looks like a live traced agent session."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9190")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS", "X-Agent-Name: aisquare-coder\nX-Pipeline-Id: run-1"
    )


def test_untraced_env_drops_the_identity_and_nothing_else(
    traced_parent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "kept")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = untraced_env()

    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_CUSTOM_HEADERS" not in env
    assert env["ANTHROPIC_API_KEY"] == "kept", "credentials are how the child authenticates"
    assert env["PATH"] == "/usr/bin"


def test_untraced_env_does_not_mutate_what_it_was_given() -> None:
    base = {"ANTHROPIC_BASE_URL": "http://x", "KEEP": "1"}
    assert untraced_env(base) == {"KEEP": "1"}
    assert base == {"ANTHROPIC_BASE_URL": "http://x", "KEEP": "1"}


def test_the_model_probe_never_inherits_a_role_identity(traced_parent: None) -> None:
    """THE leak this sweep was opened for.

    ``probe_model`` runs a real ``claude -p``. Inherited, the parent's identity
    makes every probe a Run under that role — junk in the dataset the morning
    experiments measure, attributed to a teammate who did not do it. The SDK
    has its own junk-run suppression; this must not depend on it, because the
    fix is to not emit the traffic at all.
    """
    from aisquare.core import harness

    env = harness._probe_env()

    for name in TRACING_ENV_VARS:
        assert name not in env, f"{name} reaches the probe child"


def test_the_probe_still_gets_what_it_needs_to_run(traced_parent: None) -> None:
    """The strip must stay surgical: the probe authenticates as this account
    and resolves its own binary, so credentials and PATH still travel."""
    from aisquare.core import harness

    env = harness._probe_env()

    assert "PATH" in env
    assert env["AISQUARE_TEAM"] == "0"
    assert env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] == "1"


def test_the_gbrain_worker_never_inherits_a_role_identity(
    traced_parent: None, tmp_path: Path
) -> None:
    """gbrain is not an agent session. Its own env builder already guards the
    Anthropic KEY, which is the tell that an Anthropic path exists — so an
    inherited base URL would route it through our proxy under the parent."""
    from aisquare.core import brain

    env = brain._env(tmp_path)

    for name in TRACING_ENV_VARS:
        assert name not in env, f"{name} reaches gbrain"


def test_the_detached_distiller_never_inherits_a_role_identity(
    traced_parent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background worker of ours is not an agent session — and this one
    OUTLIVES the process that started it, so a live pipeline id would attach
    its work to a Run that may already have ended."""
    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        seen.update(argv=argv, env=kwargs.get("env"))
        return None

    monkeypatch.setattr("aisquare.core.brain.brain_enabled", lambda: True)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/gbrain")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    _REAL_SPAWN_DRAIN(root=tmp_path)

    env = seen["env"]
    assert env is not None, "the drain must pass an environment, not inherit one wholesale"
    for name in TRACING_ENV_VARS:
        assert name not in env, f"{name} reaches the detached distiller"
    assert env.get("PATH") == os.environ.get("PATH")


def _traced_seams() -> Iterator[str]:
    yield from (key for key, seam in SEAMS.items() if seam.decision == TRACED)


def test_only_the_two_launch_seams_are_traced() -> None:
    """Being traced means "this process becomes the agent". Anything else
    wearing an identity is a leak, so the traced set is small and pinned."""
    assert set(_traced_seams()) == {
        "aisquare/cli/launch.py::_exec",
        "aisquare/cli/team.py::spawn",
    }
