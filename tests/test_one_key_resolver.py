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
from aisquare.services import explainability_ops as ops

SOURCE = Path(service.__file__)
OPS_SOURCE = Path(ops.__file__)

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


#: Allowed to resolve a key. ``resolve_target`` (in ``explainability_ops``, and
#: guarded separately below) is the resolver; ``resolve_api_key`` and
#: ``stored_api_key`` ARE the readers it is built from; ``store_api_key`` is the
#: writer — where a key enters the machine rather than where one is chosen.
#: ``_active_deployment`` stays listed as a projection of the resolver: it no
#: longer reads a key itself, and if it ever does again this list is where the
#: exemption has to be argued for.
#:
#: ``stored_api_key`` was ``_stored_api_key``. It became public when
#: ``resolve_target`` gained the key-file fallback that the operational surfaces
#: were missing — the same file reader, one more legitimate caller.
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
    "stored_api_key",
    "store_api_key",
}


def _key_resolution_offences(node: ast.FunctionDef) -> list[str]:
    """Every way this function resolves a key without going through the resolver.

    A callable predicate rather than a loop body, so it can be CONTROLLED.
    Measured before this existed: adding ``if True: continue`` inside the
    offender loop made the rule examine no function at all and every meta-check
    still passed — the train's version and mine both, 9 green each. They all
    watch THE WALK, and the walk was fine; the rule had stopped consuming it.

    The ops rule in this file was already immune because it was extracted and
    given a positive control, which is how the gap was noticed at all: same
    file, two rules, one protected.
    """
    found: list[str] = []
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id in ("resolve_api_key", "stored_api_key")
        ):
            found.append(f"{node.name} calls {inner.func.id}() directly")
        # READING the file, not NAMING it. An earlier rule flagged any
        # `key_path()` call and named `shipping_state` and `ship_once` — both
        # correct: they use the path only to build the advice string "or write
        # <path>", gated on the target naming the DEFAULT variable. A checker
        # that misdiagnoses correct code is the failure this shift has found
        # three times in its own instructions; the rule is contents, not mention.
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in ("read_text", "read_bytes", "open")
            and isinstance(inner.func.value, ast.Call)
            and isinstance(inner.func.value.func, ast.Name)
            and inner.func.value.func.id == "key_path"
        ):
            found.append(f"{node.name} reads the key file's contents directly")
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "get"
            and any(isinstance(arg, ast.Name) and arg.id == "KEY_ENV_VAR" for arg in inner.args)
        ):
            found.append(f"{node.name} reads ${'{'}KEY_ENV_VAR{'}'} directly")
    return found


#: One synthetic function per shape the rule claims to catch, and one that is
#: correct. Synthetic rather than real, following @8dd460fb: a control anchored
#: to production code stops controlling anything the day that code is cleaned up.
_OFFENDING_BODIES = {
    "calls the resolver": "def f():\n    return resolve_api_key()\n",
    "calls the file reader": "def f():\n    return stored_api_key()\n",
    "reads the key file": "def f():\n    return key_path().read_text()\n",
    "reads the env var": "def f():\n    return os.environ.get(KEY_ENV_VAR)\n",
}
_CORRECT_BODIES = {
    "names the key file in advice": 'def f():\n    return f"or write {key_path()}"\n',
    "reads the resolved key": "def f(target):\n    return target.api_key\n",
    "asks the one resolver": "def f():\n    return _active_deployment()[2]\n",
}


@pytest.mark.parametrize("shape", sorted(_OFFENDING_BODIES))
def test_the_rule_still_fires_on_each_shape_it_claims(shape: str) -> None:
    """The positive control the inline version could not have.

    Without this, ``if True: continue`` in the loop below — or any change that
    stops the rule matching — leaves a guard that reports a clean module while
    inspecting nothing.
    """
    node = ast.parse(_OFFENDING_BODIES[shape]).body[0]
    assert isinstance(node, ast.FunctionDef)

    assert _key_resolution_offences(node), f"the rule no longer catches: {shape}"


@pytest.mark.parametrize("shape", sorted(_CORRECT_BODIES))
def test_the_rule_stays_quiet_on_correct_code(shape: str) -> None:
    """The negative control, so "make the predicate always true" is not a fix.

    Two of these are real functions in the module — naming the key file in
    advice, and reading the resolved key — and a rule that accuses them is the
    too-broad failure this file already committed once.
    """
    node = ast.parse(_CORRECT_BODIES[shape]).body[0]
    assert isinstance(node, ast.FunctionDef)

    assert not _key_resolution_offences(node), f"the rule now accuses correct code: {shape}"


def test_there_is_exactly_one_place_that_resolves_a_key() -> None:
    """The half @8dd460fb's guard does not cover, in the same structural shape.

    Behavioural tests only catch divergences someone thought to construct —
    which is how the gateway one survived three configurations. The rule: no
    other function in this module may read ``$EXPLAINABILITY_API_KEY``, call
    ``resolve_api_key``, or read the key file.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))

    offenders = [
        offence
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name not in _MAY_RESOLVE_KEY
        for offence in _key_resolution_offences(node)
    ]

    assert not offenders, (
        "these resolve a key without going through the one resolver, which is "
        f"how an unlabelled file satisfies a named deployment: {sorted(set(offenders))}"
    )


def test_the_guard_actually_inspects_something() -> None:
    """Guard the guard: an AST walk that matches nothing passes silently."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    # No typed floor here on purpose. A broken walk yields an empty set, which
    # cannot be a superset of a non-empty allow list, so the assertion below
    # already fails on it — VERIFIED by neutralising a `>= 30` floor AND breaking
    # the walk together: this test still failed. A number would be the
    # constant-that-can-be-lowered category (@8dd460fb), earning nothing.
    assert {n.name for n in functions} >= _MAY_RESOLVE_KEY, (
        "the allow list names functions that no longer exist, so it excuses nothing"
    )


#: Allowed to resolve a key IN ``explainability_ops``. ``resolve_target`` is the
#: resolver; nothing else may read the variable a target names.
#:
#: Reading the RESOLVED ``target.api_key`` is fine and must not be flagged —
#: ``probe_ingest``, ``register_roster`` and ``_check_config`` all do it and are
#: correct. Accusing them would be the too-broad rule this file already
#: committed once, when it conflated NAMING the key file with READING it.
_OPS_MAY_RESOLVE_KEY = {"resolve_target"}


def _resolves_from_a_named_variable(node: ast.FunctionDef) -> bool:
    """True when this function reads ``environ.get(<something>.api_key_env)``.

    An ATTRIBUTE argument, not a Name — which is the whole reason this rule is
    written separately rather than copied. ``explainability.py`` resolves via
    ``os.environ.get(KEY_ENV_VAR)``, a module constant; ops resolves via the
    variable the TARGET names. A guard copied across unchanged would walk ops,
    match nothing, and pass forever while inspecting nothing.
    """
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "get"
            and any(
                isinstance(arg, ast.Attribute) and arg.attr == "api_key_env" for arg in inner.args
            )
        ):
            return True
    return False


def test_ops_has_exactly_one_place_that_resolves_a_key() -> None:
    """The half the gateway guard covers and this one did not.

    @8dd460fb's gateway guard walks both modules because ops is "the obvious
    place a second resolver would appear". The key guard walked one. Both
    divergences found tonight lived in a READER that never joined the resolver,
    so the asymmetry is the gap that matters.
    """
    tree = ast.parse(OPS_SOURCE.read_text(encoding="utf-8"))

    offenders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name not in _OPS_MAY_RESOLVE_KEY
        and _resolves_from_a_named_variable(node)
    ]

    assert not offenders, (
        "these resolve a key from a target-named variable without being the "
        f"resolver: {sorted(offenders)}"
    )


def test_reading_the_resolved_key_is_not_an_offence() -> None:
    """The rule must not accuse correct code.

    ``probe_ingest`` and ``register_roster`` read ``target.api_key`` — the
    resolver's OUTPUT — and are exactly what the one-resolver design wants.
    Measured: they touch a key and must still pass.
    """
    tree = ast.parse(OPS_SOURCE.read_text(encoding="utf-8"))
    by_name = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    for name in ("probe_ingest", "register_roster", "_check_config"):
        assert name in by_name, f"{name} no longer exists; this test is describing a ghost"
        assert not _resolves_from_a_named_variable(by_name[name]), (
            f"{name} reads the resolved key and must not be flagged as a resolver"
        )


def test_the_ops_walk_inspects_something_and_the_rule_can_match() -> None:
    """Guard the guard, both halves.

    A walk that finds no functions passes; so does a rule that can never match.
    The second is the one that bites — the ops rule is deliberately DIFFERENT
    from the one above it, so "it compiles and passes" says nothing.
    """
    tree = ast.parse(OPS_SOURCE.read_text(encoding="utf-8"))
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    # No typed floor, same reason as above, and here the rule-still-matches
    # assertion at the end of this function is a second cover: a broken walk
    # makes its `next(...)` raise rather than pass.
    assert {n.name for n in functions} >= _OPS_MAY_RESOLVE_KEY, (
        "the ops allow list names functions that no longer exist"
    )
    # The rule matches the one function that really does resolve, so a rule that
    # matches nothing at all cannot masquerade as a clean module.
    assert _resolves_from_a_named_variable(
        next(n for n in functions if n.name == "resolve_target")
    ), "the ops rule no longer matches the resolver itself, so it matches nothing"
