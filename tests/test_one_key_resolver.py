"""The offer says this machine has a key; `status` and `ship` say it has none.

@8dd460fb closed the env-first GATEWAY read in ``shipping_offer``. The same
function has an env-first KEY read with the same shape, and the guard built to
stop a fourth gateway resolver names ``gateway_url`` seven times and the key
zero times — so a future reader who trusts it gets no protection here.

MEASURED at 4f21ee8: target names a custom ``api_key_env = "MY_KEY_VAR"``, that
variable is UNSET, and a key file exists at ``~/.aisquare/explainability-key``:

    resolve_api_key()        -> a key   (fixed EXPLAINABILITY_API_KEY, then THE FILE)
    status --json .key_set   -> False   (through the TARGET's variable, no file)
    explainability ship      -> "no workspace key — set $MY_KEY_VAR"

So ``init`` can print that this machine can ship — because an unlabelled file
exists — while everything that actually ships says there is no key. Same shape
as the gateway defect: a surface that only SPEAKS, about something the machine
will not do, read at the moment the operator decides.

WHICH HALF IS WRONG, AND WHY IT IS THIS ONE. Not ``resolve_api_key``: its
environment-wins rule is argued and correct for the machine that named no
target. Not ``_active_deployment`` either, and that is the load-bearing part —
it refuses the key file when a target names its own variable ON PURPOSE, because
the file holds ONE UNLABELLED key. The incident is recorded in its own comment:
follow the CLI's "or write <key file>" advice on staging, switch to prod, and
the STAGING KEY WENT TO THE PROD GATEWAY. Teaching the target path to read the
file would re-open exactly that, and ``test_key_never_crosses_deployments.py``
exists because of it.

So the offer asks the target-aware resolver, which is what @8dd460fb did for the
gateway one line above. The no-target case is unaffected: ``_active_deployment``
already falls back to the key file when the target names the DEFAULT variable,
which is the machine ``init --explainability`` produces.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.services import explainability as service

SOURCE = Path(service.__file__)

_FAKE_FILE_KEY = "-".join(["not", "a", "real", "file", "key"])
_CUSTOM_VAR = "MY_KEY_VAR"


@pytest.fixture
def target_names_its_own_key_var(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """A target with a custom key variable UNSET, and an unlabelled key file.

    The file is what the CLI's own "or write <key file>" advice produces, so
    this is a machine an operator can reach by following instructions.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    runner.invoke(
        app,
        [
            "explainability",
            "enable",
            "--target",
            "prod",
            "--gateway-url",
            "https://prod.example",
            "--key-env",
            _CUSTOM_VAR,
        ],
        catch_exceptions=False,
    )
    monkeypatch.delenv(_CUSTOM_VAR, raising=False)
    monkeypatch.delenv(service.KEY_ENV_VAR, raising=False)
    service.store_api_key(_FAKE_FILE_KEY)


def test_the_offer_agrees_with_what_will_actually_ship(
    target_names_its_own_key_var: None,
) -> None:
    """THE defect: the offer says yes, the shipping state says no."""
    offered = service.shipping_offer().has_key
    actually = service.shipping_state().has_key

    assert offered == actually, (
        f"the offer says has_key={offered} while shipping says {actually} — "
        "init would tell the operator this machine can ship with a key that "
        "the target's variable does not name"
    )


def test_the_offer_does_not_claim_the_unlabelled_file(
    target_names_its_own_key_var: None,
) -> None:
    """Stated as the direction that matters, so a fix cannot satisfy it by
    teaching the shipping side to accept the file instead."""
    assert service.shipping_offer().has_key is False


def test_a_machine_that_named_no_key_variable_still_works(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The boundary inherited from the gateway fix, and the one that could brick.

    ``init --explainability`` produces a machine with no custom variable. If
    routing the offer through the target stopped the key FILE and the default
    variable from counting, that machine could never be offered anything.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    monkeypatch.delenv(service.KEY_ENV_VAR, raising=False)
    service.store_api_key(_FAKE_FILE_KEY)

    assert service.shipping_offer().has_key is True, (
        "the key file stopped counting on a machine that named no variable"
    )


def test_the_default_variable_still_counts_when_no_target_names_another(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The other half of that boundary: the exported default variable."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    monkeypatch.setenv(service.KEY_ENV_VAR, "-".join(["not", "a", "real", "env", "key"]))

    assert service.shipping_offer().has_key is True


#: Allowed to resolve a key. ``_active_deployment`` is the resolver;
#: ``resolve_api_key`` and ``_stored_api_key`` ARE the readers it is built from;
#: ``store_api_key`` is the writer — where a key enters the machine rather than
#: where one is chosen.
#:
#: ``configure_shipping`` was here and is NOT any more. It decided the `ship`
#: flag from ``resolve_api_key``, which broke its own documented invariant:
#: ship=True on a machine whose target names a key variable it does not have,
#: satisfied by an unlabelled key file, buffering forever. It now asks
#: ``_active_deployment`` like everything else, so the exemption it needed is
#: gone — and a shrinking allow list is the only direction this list should
#: ever move.
_MAY_RESOLVE_KEY = {
    "_active_deployment",
    "resolve_api_key",
    "_stored_api_key",
    "store_api_key",
}


def test_there_is_exactly_one_place_that_resolves_a_key() -> None:
    """The half @8dd460fb's guard does not cover, in the same structural shape.

    Behavioural tests only catch divergences someone thought to construct —
    which is how the gateway one survived three configurations. The rule: no
    other function in this module may read ``$EXPLAINABILITY_API_KEY``, call
    ``resolve_api_key``, or read the key file.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name in _MAY_RESOLVE_KEY:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id in ("resolve_api_key", "_stored_api_key")
            ):
                offenders.append(f"{node.name} calls {inner.func.id}() directly")
            # READING the file, not NAMING it. My first rule flagged any
            # `key_path()` call and named `shipping_state` and `ship_once` —
            # both correct: they use the path only to build the advice string
            # "or write <path>", and both already gate it on the target naming
            # the DEFAULT variable. A checker that misdiagnoses correct code is
            # the failure this shift has now found three times in its own
            # instructions; the rule is contents, not mention.
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr in ("read_text", "read_bytes", "open")
                and isinstance(inner.func.value, ast.Call)
                and isinstance(inner.func.value.func, ast.Name)
                and inner.func.value.func.id == "key_path"
            ):
                offenders.append(f"{node.name} reads the key file's contents directly")
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and any(isinstance(arg, ast.Name) and arg.id == "KEY_ENV_VAR" for arg in inner.args)
            ):
                offenders.append(f"{node.name} reads ${'{'}KEY_ENV_VAR{'}'} directly")

    assert not offenders, (
        "these resolve a key without going through the one resolver, which is "
        f"how an unlabelled file satisfies a named deployment: {sorted(set(offenders))}"
    )


def test_the_guard_actually_inspects_something() -> None:
    """Guard the guard: an AST walk that matches nothing passes silently."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    assert len(functions) >= 30, f"only {len(functions)} functions parsed from {SOURCE}"
    assert {n.name for n in functions} >= _MAY_RESOLVE_KEY, (
        "the allow list names functions that no longer exist, so it excuses nothing"
    )
