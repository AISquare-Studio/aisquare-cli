"""A launch's Claude account is decided in ONE place — `services.claude_accounts.choose`.

#145 put a ladder under every launch: the ``--account`` flag, the role's
binding, the project's default, the machine's default. ``aisquare launch`` and
``aisquare fleet spawn`` both walk it, and the only reason a hand-typed launch
and a fleet window can be trusted to land on the same account is that neither
walks it itself. The moment one of them reads ``is_default`` on its own, or
calls ``resolve`` directly, or reaches for ``core.default_account()``, the two
surfaces can disagree — and "it ran on the wrong login" is exactly the silent
failure the default exists to end.

Same shape as ``tests/test_one_key_resolver.py``, for the same reason: a
behavioural test catches only the divergence someone thought to construct. The
rule here is structural — in the two launching modules, no function may decide
an account by any means but ``choose`` — and it is CONTROLLED: one synthetic
offender per shape it claims to catch, one correct body it must not accuse, and
a check that the walk finds the real functions and that ``choose`` is really
called by them (an allow list that excuses nothing, a rule that matches
nothing, both pass silently otherwise — CONTRIBUTING, "Writing a guard that
still guards").
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from aisquare.cli import launch as launch_cli
from aisquare.services import fleet as fleet_service

LAUNCHERS = {
    "cli/launch.py": Path(launch_cli.__file__),
    "services/fleet.py": Path(fleet_service.__file__),
}

#: Ways a function could decide an account WITHOUT the resolver. Each is a call
#: shape: a bare name (``resolve(...)``) or an attribute on a module alias
#: (``claude_accounts_service.resolve(...)``, ``core.default_account()``).
_DECIDERS = frozenset(
    {
        "resolve",
        "default_account",
        "find_account",
        "list_accounts",
        "managed_accounts",
        "machine_default",
        "project_default",
    }
)
#: Reading the default flag is deciding too: ``[a for a in accounts if a.is_default]``
#: is a resolver with no name.
_DECIDING_ATTRIBUTES = frozenset({"is_default", "DEFAULT_SLOT"})

#: Functions a launching module may call to reach an account: the resolver, and
#: the two helpers that ACT on an already-chosen account (setting its variables,
#: carrying the caller's view of slot 1 into a window). Neither chooses.
_ALLOWED = frozenset({"choose", "apply_launch_env", "carry_environment", "label"})


def _account_decision_offences(node: ast.FunctionDef) -> list[str]:
    """Every way ``node`` decides an account other than by asking ``choose``.

    A callable predicate rather than an inline loop body, so the positive and
    negative controls below can aim at it directly (the inline form was found
    blind in this repo once: a ``continue`` in the offender loop, every
    meta-check green).
    """
    found: list[str] = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            name: str | None = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute) and _on_an_accounts_module(func):
                # `path.resolve()` is a Path; `claude_accounts_service.resolve()` is a
                # decision. The receiver tells them apart — both launching modules
                # reach the accounts code through an alias that says so.
                name = func.attr
            if name in _ALLOWED:
                continue  # the resolver itself, and the helpers that act on its answer
            if name in _DECIDERS:
                found.append(f"{node.name} calls {name}()")
        if isinstance(inner, ast.Attribute) and inner.attr in _DECIDING_ATTRIBUTES:
            found.append(f"{node.name} reads .{inner.attr}")
    return found


def test_the_allow_list_shields_the_resolver_and_nothing_a_decider_could_hide_behind() -> None:
    """``_ALLOWED`` is consulted by the rule (it was defined and read by nobody — review of
    #205, second round): an allowed name is never an offence, the two sets are disjoint so
    an allowance can never mask a decider, and every allowed name is one the launching
    modules really call — a list of names nobody uses would be documentation, not a guard."""
    assert _ALLOWED.isdisjoint(_DECIDERS)
    shielded = ast.parse(
        "def f(account, role, project):\n"
        "    choice = claude_accounts_service.choose(account, role=role, project=project)\n"
        "    return claude_accounts_core.label(choice.account)\n"
    ).body[0]
    assert isinstance(shielded, ast.FunctionDef) and _account_decision_offences(shielded) == []
    caught = ast.parse("def f(ref):\n    return claude_accounts_service.resolve(ref)\n").body[0]
    assert isinstance(caught, ast.FunctionDef) and _account_decision_offences(caught)
    called: set[str] = set()
    for path in LAUNCHERS.values():
        for inner in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(inner, ast.Call):
                func = inner.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)
    unused = sorted(name for name in _ALLOWED if name not in called)
    assert unused == [], f"allowed but never called by a launching module: {unused}"


def _on_an_accounts_module(func: ast.Attribute) -> bool:
    return isinstance(func.value, ast.Name) and "account" in func.value.id


def _calls_choose(node: ast.FunctionDef) -> bool:
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            if (isinstance(func, ast.Attribute) and func.attr == "choose") or (
                isinstance(func, ast.Name) and func.id == "choose"
            ):
                return True
    return False


#: One synthetic body per shape the rule claims to catch.
_OFFENDING_BODIES = {
    "resolves a reference itself": "def f(ref):\n    return claude_accounts_service.resolve(ref)\n",
    "reaches for the plain default": (
        "def f():\n    return claude_accounts_core.default_account()\n"
    ),
    "looks a slot up": "def f():\n    return claude_accounts_core.find_account(2)\n",
    "lists and picks": "def f():\n    return claude_accounts_service.list_accounts()[0]\n",
    "reads the machine default": "def f():\n    return claude_accounts_service.machine_default()\n",
    "reads the project default": (
        "def f(p):\n    return claude_accounts_service.project_default(p)\n"
    ),
    "reads the default flag": (
        "def f(accounts):\n    return [a for a in accounts if a.is_default]\n"
    ),
    "hard-codes slot 1": "def f():\n    return core.DEFAULT_SLOT\n",
}
#: Correct bodies the rule must leave alone — the two real launchers are
#: shaped like these.
_CORRECT_BODIES = {
    "asks the resolver": (
        "def f(account, role, project):\n"
        "    choice = claude_accounts_service.choose(account, role=role, project=project)\n"
        "    return choice.account\n"
    ),
    "acts on a chosen account": (
        "def f(env, choice):\n"
        "    if choice.account is not None:\n"
        "        claude_accounts_core.apply_launch_env(env, choice.account)\n"
        "    return env\n"
    ),
    "carries the caller's view into a window": (
        "def f(command):\n    return claude_accounts_service.carry_environment(command)\n"
    ),
    "names the chosen account": "def f(choice):\n    return choice.describe()\n",
    "resolves a PATH, not an account": "def f(path):\n    return path.resolve().parent\n",
}


@pytest.mark.parametrize("shape", sorted(_OFFENDING_BODIES))
def test_the_rule_still_fires_on_each_shape_it_claims(shape: str) -> None:
    node = ast.parse(_OFFENDING_BODIES[shape]).body[0]
    assert isinstance(node, ast.FunctionDef)

    assert _account_decision_offences(node), f"the rule no longer catches: {shape}"


@pytest.mark.parametrize("shape", sorted(_CORRECT_BODIES))
def test_the_rule_stays_quiet_on_correct_code(shape: str) -> None:
    node = ast.parse(_CORRECT_BODIES[shape]).body[0]
    assert isinstance(node, ast.FunctionDef)

    assert not _account_decision_offences(node), f"the rule now accuses correct code: {shape}"


@pytest.mark.parametrize("module", sorted(LAUNCHERS))
def test_a_launching_module_decides_an_account_only_through_choose(module: str) -> None:
    tree = ast.parse(LAUNCHERS[module].read_text(encoding="utf-8"))

    offenders = [
        offence
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for offence in _account_decision_offences(node)
    ]

    assert not offenders, (
        f"{module} decides a Claude account without the one resolver, so `launch` and "
        f"`fleet spawn` can disagree about which login an agent runs on: {sorted(set(offenders))}"
    )


@pytest.mark.parametrize(
    ("module", "function"), [("cli/launch.py", "launch"), ("services/fleet.py", "spawn")]
)
def test_the_launcher_really_asks_the_resolver(module: str, function: str) -> None:
    """Guard the guard: a module that never chooses at all passes the rule above for free.

    The property is not "nothing decides" but "the launcher decides THROUGH
    choose" — so the real launching function must be found AND must call it.
    """
    tree = ast.parse(LAUNCHERS[module].read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    assert function in functions, (
        f"{module} no longer has {function}(); this test describes a ghost"
    )
    assert _calls_choose(functions[function]), (
        f"{module}::{function} no longer asks claude_accounts_service.choose — either the "
        "ladder moved (update this test) or the launcher stopped honouring defaults"
    )
