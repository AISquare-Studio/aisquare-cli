"""No command may raise in the state the CLI itself tells you to create.

The sibling file, ``test_no_traceback_on_a_damaged_store.py``, holds this
property over a machine whose store is *broken*. Nothing held it over a machine
that is merely *configured*, and that is the state the operator is in at 08:00:
runbook §1 and §2 have been run, the key is not exported yet, nothing is wrong.

That gap cost one instance. ``explainability enable`` writes a target that
overrides nothing, so ``proxy_url``, ``agent_name_template`` and ``roles`` stay
``None``; ``team bind coder --env FOO=bar`` writes a profile with no ``bin``.
``config list`` then rendered the model through ``tomli_w``, which has no null
to write, and exited **1** with a traceback — §2 of the runbook, and then the
obvious next thing a person types to check that §2 worked.

Measured at f10afa9: 97 leaf commands invoked in this state, **zero** raise.
With that one-line fix reverted, the same sweep reports exactly one — so the
sweep discriminates, and ``config list`` was the only instance in the tree.

WHY THIS FILE LOOPS WHERE ITS SIBLING PARAMETRISES. There the reason to
parametrise was that a loop reports the first failure and hides the rest, and
the useful output is *which* commands. Here the loop collects **every**
offender before asserting, which answers the same question — and the state is
built by three real CLI commands, so one test per command would rebuild it 97
times for an answer that does not vary by command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import load_config

# Shared with the damaged-store sweep on purpose. Two copies of "which commands
# are unsafe for a test harness to run" would drift, and the drift would be
# silent in exactly the direction that matters: a command dropped from one list
# is a command nobody sweeps.
from tests.test_no_traceback_on_a_damaged_store import UNINVOKED, _escaped, _leaves

#: Commands that raise in a configured home TODAY. A ratchet, both directions:
#: a command not listed here that starts raising fails, and a listed one that
#: stops raising also fails, naming itself. Empty because the class is closed,
#: not because nobody looked — see the measurement in the module docstring.
STILL_RAISES_WHEN_CONFIGURED: set[str] = set()

#: What the state has to contain for this sweep to mean anything. `config list`
#: broke on an unset optional; if `explainability enable` ever stops leaving one
#: unset, every assertion here would pass while testing a state that cannot
#: reproduce the defect. Asserted, not assumed.
_SETUP: tuple[tuple[str, ...], ...] = (
    ("init",),
    (
        "explainability",
        "enable",
        "--target",
        "tst",
        "--gateway-url",
        "https://gw.invalid",
        "--key-env",
        "TST_KEY_UNSET",
    ),
    ("team", "bind", "coder", "--env", "FOO=bar"),
)


def _nones(value: Any, path: str = "") -> list[str]:
    """Every path in a dumped config whose value is ``None``."""
    if isinstance(value, dict):
        return [p for key, item in value.items() for p in _nones(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [p for i, item in enumerate(value) for p in _nones(item, f"{path}[{i}]")]
    return [path] if value is None else []


@pytest.fixture
def configured_home(isolated_home: Path, runner: CliRunner) -> Path:
    """A machine configured the way runbook §1/§2 configures one.

    Built by running the real commands rather than by writing a config file:
    a hand-written fixture would prove that *this file's idea* of a configured
    machine survives the CLI, which is not the claim. The claim is about the
    state ``aisquare`` itself produces.
    """
    for argv in _SETUP:
        result = runner.invoke(app, list(argv))
        assert result.exit_code == 0, f"setup `aisquare {' '.join(argv)}` failed: {result.output}"

    unset = _nones(load_config().model_dump(mode="json"))
    assert unset, (
        "the configured state contains no unset optional field, so this sweep "
        "can no longer reproduce the defect it exists for — check what "
        "`explainability enable` and `team bind` now write"
    )
    return isolated_home


def _swept() -> list[list[str]]:
    """Every leaf command this sweep runs, required arguments left off.

    ``_leaves()`` yields the command path only. A command with a required
    argument therefore exits 2 on usage, which is a legible exit and not what
    this file is looking for — the point is that nothing reaches the operator
    as a traceback, whichever way the command ends.
    """
    return [chain for chain in _leaves() if " ".join(chain) not in UNINVOKED]


def test_the_sweep_actually_covers_the_tree() -> None:
    """Guard the guard: an empty walk satisfies every assertion below.

    Emptiness is this file's goal *and* its failure symptom, which is the shape
    that let an earlier ratchet report a closed class over one damage shape.
    A floor plus named commands is what separates "nothing to find" from
    "nothing was looked at".
    """
    swept = _swept()

    assert len(swept) >= 90, f"only {len(swept)} commands would be swept"
    names = {" ".join(chain) for chain in swept}
    for required in ("config list", "status", "explainability status", "doctor"):
        assert required in names, f"{required} is not being swept"


def test_the_ratchet_names_only_commands_that_exist() -> None:
    """A renamed command would leave its old name here claiming a dead defect."""
    known = {" ".join(chain) for chain in _leaves()}

    unknown = sorted(name for name in STILL_RAISES_WHEN_CONFIGURED if name not in known)

    assert not unknown, f"the ratchet names commands that do not exist: {unknown}"


def test_no_command_raises_in_a_configured_home(configured_home: Path, runner: CliRunner) -> None:
    """The property, over every command, with the whole offender list reported."""
    raising: dict[str, str] = {}
    for chain in _swept():
        escaped = _escaped(runner.invoke(app, chain, catch_exceptions=True).exception)
        if escaped is not None:
            raising[" ".join(chain)] = f"{type(escaped).__name__}: {escaped}"

    unexpected = {
        name: why for name, why in raising.items() if name not in STILL_RAISES_WHEN_CONFIGURED
    }
    assert not unexpected, (
        "these raised on a machine that is merely configured — the state runbook "
        f"§1/§2 leaves an operator in: {unexpected}"
    )

    fixed = sorted(STILL_RAISES_WHEN_CONFIGURED - set(raising))
    assert not fixed, (
        f"these no longer raise — good: {fixed}. Remove them from "
        "STILL_RAISES_WHEN_CONFIGURED so the list keeps describing the truth; "
        "a ratchet that is not tightened is an allow list."
    )


def test_the_rule_still_recognises_an_escaping_exception() -> None:
    """A control, because zero offenders is also what a blind rule reports.

    The sweep above is measured empty. `_escaped` is shared with the damaged-
    store file and controlled there too; it is controlled again here because
    the import is what this file depends on, and an import that silently
    started returning None would leave every assertion above green.
    """
    assert _escaped(TypeError("Object of type 'NoneType' is not TOML serializable")) is not None
    assert _escaped(SystemExit(2)) is None
    assert _escaped(None) is None
