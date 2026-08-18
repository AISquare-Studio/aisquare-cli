"""``config list`` must be printable for any config the CLI itself writes.

``save_config`` dumps with ``exclude_none=True`` and its docstring says exactly
why: *TOML has no null*, ``tomli_w`` raises ``TypeError`` on ``None`` rather
than writing anything, so one optional field left unset makes the whole file
unwritable. ``emit_config`` renders the same model through the same library and
does **not** pass it. The writer was hardened and the reader-facing printer was
not.

Measured at 5705336, in a throwaway home, both triggers reached by ordinary
commands rather than by a hand-built config:

- ``explainability enable`` writes a target that overrides nothing, so
  ``proxy_url``, ``agent_name_template`` and ``roles`` stay ``None``.
- ``team bind coder --env FOO=bar`` writes a profile with no ``bin``.

Either one makes ``aisquare config list`` exit **1** with a Rich traceback
ending in ``TypeError: Object of type 'NoneType' is not TOML serializable``.
Not a damaged store, not a hostile input: the documented way to configure this
machine, and then the obvious command for looking at what you configured.

WHY THE GATE COULD NOT SEE IT. Both existing tests of this command
(``test_settings.py``) invoke it as ``--json config list``, and JSON has null.
The command was covered; the branch a human sees was not. A test that is
narrower than the property it is named for passes for the same reason the bug
survives — this is the second face of the taxonomy in CONTRIBUTING, and the
reason the first two tests here assert on ``tomllib.loads`` output: that
assertion cannot be satisfied by the JSON branch, so the coverage cannot
silently migrate back.

The guard at the bottom is the class rather than the instance. Two call sites
exist today; the third one is the one nobody will remember this about.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app

_SRC = Path(__file__).resolve().parents[1] / "src" / "aisquare"


def test_config_list_prints_a_home_that_explainability_enable_configured(
    runner: CliRunner,
) -> None:
    """§2 of the cutover runbook, then the obvious next thing a human types."""
    enabled = runner.invoke(
        app,
        [
            "explainability",
            "enable",
            "--target",
            "tst",
            "--gateway-url",
            "https://gw.invalid",
            "--key-env",
            "TST_KEY",
        ],
    )
    # The premise is asserted, not assumed: if `enable` ever stops writing a
    # target, this file would still pass while testing nothing.
    assert enabled.exit_code == 0, enabled.output

    listed = runner.invoke(app, ["config", "list"])
    assert listed.exit_code == 0, listed.output

    parsed = tomllib.loads(listed.stdout)
    assert "tst" in parsed["explainability"]["targets"], listed.stdout


def test_config_list_prints_a_home_that_team_bind_configured(runner: CliRunner) -> None:
    """The same defect with no explainability in the picture at all.

    Kept separate rather than parametrised because it is the evidence that this
    is not an explainability bug: ``RoleLaunchProfile.bin`` is optional too, and
    it arrived with the newest feature on ``main``. One trigger reads as a
    quirk of one subsystem; two independent ones read as the class it is.
    """
    bound = runner.invoke(app, ["team", "bind", "coder", "--env", "FOO=bar"])
    assert bound.exit_code == 0, bound.output

    listed = runner.invoke(app, ["config", "list"])
    assert listed.exit_code == 0, listed.output

    parsed = tomllib.loads(listed.stdout)
    assert parsed["team"]["profiles"]["coder"]["env"] == {"FOO": "bar"}, listed.stdout


def _dumps_calls(source: str, where: str) -> list[tuple[str, int]]:
    """Every ``tomli_w.dumps(...)`` call in ``source``, however it was imported.

    The alias half matters: ``from tomli_w import dumps`` and ``import tomli_w
    as w`` both spell the same hazard, and a guard that only knows the dotted
    form is one import statement away from being decorative.
    """
    tree = ast.parse(source)
    module_aliases = {"tomli_w"}
    bare_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tomli_w" and alias.asname:
                    module_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "tomli_w":
            for alias in node.names:
                if alias.name == "dumps":
                    bare_names.add(alias.asname or alias.name)

    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        hit = (
            isinstance(func, ast.Attribute)
            and func.attr == "dumps"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        ) or (isinstance(func, ast.Name) and func.id in bare_names)
        if hit:
            found.append((where, node.lineno))
    return found


def _excludes_none(source: str, lineno: int) -> bool:
    """Does the call on ``lineno`` hand ``tomli_w`` a None-free mapping?"""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or node.lineno != lineno:
            continue
        for argument in node.args:
            if isinstance(argument, ast.Call) and any(
                keyword.arg == "exclude_none"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in argument.keywords
            ):
                return True
            # A name bound earlier — `payload = tomli_w.dumps(dumped)` — is
            # resolved by the caller, which is why this returns False and the
            # call site is listed in _RESOLVED_ELSEWHERE with its reason.
    return False


#: Call sites whose argument is built somewhere other than the call itself.
#: Listed with the reason, because "the guard cannot see it" and "the guard
#: was told to look away" have to be different things in the file.
_RESOLVED_ELSEWHERE = {
    "core/config.py": (
        "save_config builds `dumped` with exclude_none=True eleven lines up "
        "and merges unknown keys into it; its docstring states the invariant"
    ),
}


def test_every_toml_dump_in_the_cli_is_none_free() -> None:
    """The class, not the instance — a third call site fails here first."""
    seen: list[tuple[str, int]] = []
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        where = path.relative_to(_SRC).as_posix()
        source = path.read_text(encoding="utf-8")
        for _, lineno in _dumps_calls(source, where):
            seen.append((where, lineno))
            if where in _RESOLVED_ELSEWHERE or _excludes_none(source, lineno):
                continue
            offenders.append(f"{where}:{lineno}")

    assert not offenders, (
        "these hand TOML a model dump that can contain None, which raises "
        f"TypeError on the first unset optional field: {offenders}"
    )
    # Emptiness is this test's goal AND its failure symptom. Without a floor,
    # a walk that stopped finding anything would read exactly like a codebase
    # with nothing to find.
    assert len(seen) >= 2, f"the walk found {len(seen)} tomli_w.dumps calls, expected >= 2"


_CAUGHT = """
import tomli_w
def render(config):
    return tomli_w.dumps(config.model_dump(mode="json"))
"""

_ALIASED = """
import tomli_w as w
from tomli_w import dumps as d
def render(config):
    w.dumps(config.model_dump(mode="json"))
    d(config.model_dump(mode="json"))
"""

_ALLOWED = """
import tomli_w
def render(config):
    return tomli_w.dumps(config.model_dump(mode="json", exclude_none=True))
"""


@pytest.mark.parametrize(
    ("source", "expected"),
    [(_CAUGHT, 1), (_ALIASED, 2), (_ALLOWED, 0)],
    ids=["dotted", "aliased-and-bare", "already-safe"],
)
def test_the_rule_sees_what_it_claims_to_see(source: str, expected: int) -> None:
    """A control on synthetic input, because a blind rule reports zero too.

    It does not prove the walk reaches the real files — that is what the floor
    in the test above is for. The two halves are complementary and neither is
    sufficient: this one proves the rule can see an offender, that one proves
    the walk delivers one.
    """
    offending = [
        lineno
        for _, lineno in _dumps_calls(source, "<synthetic>")
        if not _excludes_none(source, lineno)
    ]
    assert len(offending) == expected, offending
