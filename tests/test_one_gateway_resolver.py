"""Every surface that names a gateway must name the same one.

@dfd9a883 measured the check both documents bill as "the only check that can
detect a split brain" and could not construct a state where it disagrees —
across target stg, target prod, and a hand-pinned stale top-level
``gateway_url``. Their conclusion was careful: "absence of a reachable path is
not a proof."

It is reachable. There is a THIRD resolver. ``_active_deployment`` resolves
through the active target; ``shipping_offer`` reads ``$EXPLAINABILITY_GATEWAY_URL``
FIRST and falls back to the top-level value, consulting no target at all.
Measured with a prod target configured and a staging gateway exported:

    .shipping.gateway        https://PROD.example      (the documented check)
    _active_deployment()     https://PROD.example      (what actually ships)
    shipping_offer()         https://STAGING.example   (what init tells you)

That is operator-visible at the worst moment. ``init`` prints "this machine can
ship … to {offer.gateway_url}" while deciding whether to turn shipping on, and
§5 of the cutover runbook is where the operator exports that very variable. So
the line naming the deployment is read at the exact point the shell state that
falsifies it has been created.

WHY THIS IS NOT THE SPLIT BRAIN THE DOCS DESCRIBE, stated so the fix is not
oversold: the two LANES still agree — model traffic and insights both follow the
target. Nothing routes anywhere wrong. What diverges is a THIRD surface that
only speaks, and it speaks about a deployment the machine will not use.

The fix is that there is one resolver, and this file fails if a fourth appears.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from aisquare.services import explainability as service
from aisquare.services import explainability_ops as ops

SERVICE = Path(service.__file__)
OPS = Path(ops.__file__)


@pytest.fixture
def prod_machine(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prod target configured, and a stale staging gateway in the shell.

    Exactly what §5 of the runbook produces: the operator sources an env file
    for one deployment and later points the CLI at another.
    """
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    CliRunner().invoke(app, ["init", "--yes"], catch_exceptions=False)
    CliRunner().invoke(
        app,
        [
            "explainability",
            "enable",
            "--target",
            "prod",
            "--gateway-url",
            "https://prod.example",
            "--key-env",
            "PROD_KEY",
        ],
        catch_exceptions=False,
    )
    monkeypatch.setenv(service.GATEWAY_ENV_VAR, "https://staging.example")


def test_the_offer_names_the_deployment_that_will_be_used(prod_machine: None) -> None:
    """THE defect: against the current build the offer says staging."""
    offered = service.shipping_offer().gateway_url
    actual = service._active_deployment()[0]

    assert offered == actual, (
        f"init offers to ship to {offered!r} while this machine ships to "
        f"{actual!r} — the operator decides whether to turn shipping on while "
        "reading the wrong deployment"
    )


def test_the_exported_variable_still_works_when_no_target_names_one(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary. The env var is the documented way to configure a machine
    that has no target yet, and `init --explainability` depends on it.

    Routing everything through the target must not make that stop working, or
    the fix trades a misleading offer for a machine that cannot be offered
    anything at all.
    """
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    CliRunner().invoke(app, ["init", "--yes"], catch_exceptions=False)
    monkeypatch.setenv(service.GATEWAY_ENV_VAR, "https://only-source.example")

    assert service.shipping_offer().gateway_url == "https://only-source.example"


def test_only_the_named_functions_resolve_a_gateway() -> None:
    """A fourth resolver must fail here rather than be found by an operator.

    Structural rather than behavioural, because the behavioural version can only
    catch divergences someone thought to construct — which is how the third one
    survived: @dfd9a883 tried three configurations and the fourth was the
    environment variable.

    BOTH MODULES, because the first version of this guard covered one and I
    checked the other BY HAND. A hand-check rots: it was true at the commit I
    read it, nothing re-runs it, and `explainability_ops` is the obvious place a
    fourth resolver would hide since it defines `GATEWAY_ENV_VAR` too.

    THE RULE IS NARROWER THAN "MENTIONS A GATEWAY", and the precision is
    load-bearing. Reading `target.gateway_url` is fine — that IS the resolved
    answer, and the ops module does it a dozen times to build URLs. Naming
    `GATEWAY_ENV_VAR` in an error message is fine. Setting it, as `_init_sdk`
    does to hand the SDK our answer through its own contract, is fine. What is
    forbidden is READING a gateway from config or environment: `settings`,
    `config.explainability`, `load_config().…`, or `.get(GATEWAY_ENV_VAR)`.
    A guard that flagged message strings would be switched off by the next
    person to touch the file.
    """
    offenders = _offenders_in(SERVICE) + _offenders_in(OPS)

    assert not offenders, (
        "these resolve a gateway without going through the one resolver, which "
        f"is how a machine ends up naming two deployments: {sorted(set(offenders))}"
    )


def test_the_guard_is_looking_at_something() -> None:
    """Guard the guard: a rule this narrow could match nothing and pass.

    Both modules must contain SOME allowed reader, or the walk has stopped
    finding functions — a rename, a refactor into a class, a moved file — and
    the assertion above would be green over nothing at all.
    """
    found: set[str] = set()
    for module in (SERVICE, OPS):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef) or node.name not in _MAY_RESOLVE:
                continue
            if any(
                _reads_configured_gateway(i) or _reads_gateway_environment(i)
                for i in ast.walk(node)
            ):
                found.add(f"{module.name}:{node.name}")

    assert len(found) >= 3, (
        f"only {sorted(found)} actually read a gateway — the rule has stopped "
        "matching, so the guard above is asserting over nothing"
    )


#: `settings`-shaped names whose `.gateway_url` is the CONFIGURED value rather
#: than a resolved target's.
_CONFIG_NAMES = {"settings", "config", "resolved_settings"}


def _offenders_in(module: Path, source: str | None = None) -> list[str]:
    """Every function in `module` that resolves a gateway without the resolver.

    EXTRACTED SO A CONTROL CAN REACH IT, which is the whole point. This loop
    used to live inline in the test body, and @9bbc8ed7 found the consequence in
    the sibling guard: one `continue` inside it makes the rule examine nothing,
    report no offender, and be indistinguishable from a clean module. Every
    meta-check in this file passed that sabotage, because they all watched the
    WALK — "the walk found functions", "the allow list names functions that
    exist" — and the walk was fine. THE RULE HAD STOPPED CONSUMING IT.

    A rule inline in a test body cannot be called with known-bad input, so it
    cannot be controlled. Out here it can, and the controls below do.

    `source` overrides the file's contents so controls can pass synthetic
    modules; anchoring a control to production code stops controlling anything
    the day that code is cleaned up.
    """
    text = module.read_text(encoding="utf-8") if source is None else source
    found: list[str] = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.FunctionDef) or node.name in _MAY_RESOLVE:
            continue
        for inner in ast.walk(node):
            if _reads_configured_gateway(inner):
                found.append(f"{module.name}:{node.name} reads a configured gateway_url")
            elif _reads_gateway_environment(inner):
                found.append(f"{module.name}:{node.name} reads $GATEWAY_ENV_VAR")
    return found


def _reads_configured_gateway(node: ast.AST) -> bool:
    """`settings.gateway_url` / `config.explainability.gateway_url` / `load_config()…`.

    Deliberately NOT `target.gateway_url`, which is the resolver's own answer.
    """
    if not (isinstance(node, ast.Attribute) and node.attr == "gateway_url"):
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return base.id in _CONFIG_NAMES
    if isinstance(base, ast.Attribute):
        return base.attr == "explainability"
    return isinstance(base, ast.Call)


def _reads_gateway_environment(node: ast.AST) -> bool:
    """`environ.get(GATEWAY_ENV_VAR, …)` — a READ, not a mention and not a write."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and any(isinstance(a, ast.Name) and a.id == "GATEWAY_ENV_VAR" for a in node.args)
    )


#: The functions allowed to read a gateway from config or environment.
#: `_active_deployment` is the resolver and `resolve_target` is its ops-side
#: half, which consults the variable only AFTER the target — the ordering that
#: makes the target authoritative. `configure_shipping` is the WRITER: it takes
#: a URL from the operator or the environment and records it, which is where a
#: gateway ENTERS the machine rather than where one is chosen.
_MAY_RESOLVE = {"_active_deployment", "resolve_target", "configure_shipping"}


#: Synthetic modules, one per shape the rule claims to catch, plus correct code
#: it must NOT accuse. Synthetic rather than real functions on purpose: a
#: control anchored to production code stops controlling anything the day that
#: code is cleaned up, and two of these shapes have no live instance to point at.
_CAUGHT = {
    "settings attribute": "def f():\n    return settings.gateway_url\n",
    "config attribute": "def f():\n    return config.explainability.gateway_url\n",
    "loaded config": "def f():\n    return load_config().explainability.gateway_url\n",
    "environment read": 'def f():\n    return os.environ.get(GATEWAY_ENV_VAR, "")\n',
}
_ALLOWED = {
    "a resolved target": "def f():\n    return target.gateway_url\n",
    "naming it in a message": 'def f():\n    return f"set {GATEWAY_ENV_VAR}"\n',
    "setting it for the SDK": "def f():\n    os.environ[GATEWAY_ENV_VAR] = url\n",
    "the resolver itself": "def _active_deployment():\n    return settings.gateway_url\n",
    "the writer": "def configure_shipping():\n    return os.environ.get(GATEWAY_ENV_VAR)\n",
}


def test_the_rule_still_catches_every_shape_it_claims() -> None:
    """POSITIVE controls, and the reason this file needed them.

    @9bbc8ed7 found that one `continue` inside the offender loop of the sibling
    guard made it examine nothing while every meta-check stayed green — because
    they all watched the WALK, and the walk was fine. Reproduced here before
    fixing: the same line made all four of this file's tests pass.

    A rule is only controlled by being CALLED WITH KNOWN-BAD INPUT. Each shape
    is asserted separately so a failure says which one stopped being caught,
    rather than "the rule is broken".
    """
    missed = [name for name, source in _CAUGHT.items() if not _offenders_in(SERVICE, source)]

    assert not missed, (
        f"the rule no longer catches: {missed}. It cannot report a clean module "
        "if it cannot recognise a dirty one — a guard that examines nothing is "
        "indistinguishable from a guard that finds nothing."
    )


def test_the_rule_still_permits_the_shapes_that_are_correct() -> None:
    """NEGATIVE controls, so "make the predicate always true" is not a fix.

    Without these, the cheapest way to pass the positive controls above is a
    rule that accuses everything — which would then flag `target.gateway_url`,
    read a dozen times in the ops module, and the guard would be deleted rather
    than fixed. Two of these shapes are real code in the module today.
    """
    accused = {name: _offenders_in(SERVICE, source) for name, source in _ALLOWED.items()}

    wrong = {name: hits for name, hits in accused.items() if hits}
    assert not wrong, (
        f"the rule now accuses correct code: {wrong}. Reading a RESOLVED target, "
        "naming the variable in a message, and setting it for the SDK are all "
        "fine; only reading a gateway from config or environment is not."
    )
