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

SOURCE = Path(service.__file__)


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


def test_there_is_exactly_one_place_that_resolves_a_gateway(prod_machine: None) -> None:
    """A fourth resolver must fail here rather than be found by an operator.

    Structural rather than behavioural, because the behavioural version can only
    catch divergences someone thought to construct — which is how this one
    survived: three configurations were tried and the fourth was the env var.

    The rule: no function in this module other than the resolver itself may read
    `gateway_url` off the loaded config or off the environment.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name in _MAY_RESOLVE:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr == "gateway_url":
                offenders.append(f"{node.name} reads .gateway_url directly")
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and any(
                    isinstance(arg, ast.Name) and arg.id == "GATEWAY_ENV_VAR" for arg in inner.args
                )
            ):
                offenders.append(f"{node.name} reads ${'{'}GATEWAY_ENV_VAR{'}'} directly")

    assert not offenders, (
        "these resolve a gateway without going through the one resolver, which "
        f"is how a machine ends up naming two deployments: {sorted(set(offenders))}"
    )


#: The functions allowed to resolve a gateway from config or environment.
#: `_active_deployment` is the resolver. `configure_shipping` is the WRITER —
#: it takes a URL from the operator or the environment and records it, which is
#: where a gateway enters the machine rather than where one is chosen.
_MAY_RESOLVE = {"_active_deployment", "configure_shipping"}
