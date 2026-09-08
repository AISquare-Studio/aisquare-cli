"""`repomix` was green whenever `npx` merely existed. Repomix needs Node 22.

Measured for docs/plans/one-line-install.md §1.4, and the numbers are what make
it a live defect rather than a tidiness point:

    repomix@1.18.0  "engines": {"node": ">=22.0.0"}   (registry.npmjs.org)
    Debian 12       ships Node 18
    Ubuntu 22.04    ships Node 12

On both of those distributions `npx` is present, the old check reported `ok`
with the words "repomix available on demand via npx", and the first
`aisquare project onboard` failed at runtime. A green line over a feature that
cannot run — and specifically the ONE way `install.sh` could finish, report a
16/17 doctor, and hand over a machine that cannot pack a snapshot. The
installer's acceptance test is "every check ok except brain", so a check that
lies would have been baked into the definition of success.

WHAT IS ASSERTED HERE, and what is deliberately not. The floor is a MAJOR
version comparison, so the boundary matters more than the middle: 21 warns, 22
is fine, and both are pinned. Unreadable stays `ok` — that is the rule
`_check_tmux` already follows for a version string that does not parse, and the
reason is the same: refusing on a guess would lock a fork or a distro build with
its own banner out of a working feature, while failing open costs one confusing
error later instead of a clear one now. What is NOT asserted is that Repomix's
own floor is still 22 — that would need the network on doctor's primary path,
which `tests/test_no_network_on_the_primary_path.py` exists to prevent.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from aisquare.models import CheckStatus
from aisquare.services import diagnostics
from aisquare.services.diagnostics import MIN_NODE


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Describe a machine by what is on its PATH and what `node --version` says.

    A fixture rather than a monkeypatch per test because `_check_repomix` asks
    `which` three different names and every case has to answer all three — a
    per-test lambda got one wrong twice while this file was being written, in
    both directions, which is exactly the shape of the bug under test.

    `shutil` and `subprocess` are patched on the modules THIS file imports, not
    via `diagnostics.shutil`: they are the same module objects, so the effect is
    identical, and mypy --strict refuses an attribute a module does not export.
    """

    def configure(
        *,
        repomix: bool = False,
        npx: bool = False,
        node: int | None = None,
        node_on_path: bool | None = None,
    ) -> None:
        # `node_on_path` is separate from `node` on purpose: "a node whose
        # version will not parse" is a DIFFERENT machine from "no node at all",
        # the check reports them with different words, and a fixture that
        # derived presence from `node is not None` could not express the first
        # one — it silently tested the second twice. Defaulting to
        # `node is not None` keeps every other case a one-word call.
        present = {
            "repomix": repomix,
            "npx": npx,
            "node": node is not None if node_on_path is None else node_on_path,
        }
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if present.get(name) else None,
        )
        monkeypatch.setattr(diagnostics, "_node_major", lambda _binary: node)

    return configure


@pytest.mark.parametrize("major", [12, 18, 21])
def test_an_old_node_warns_even_though_npx_exists(machine, major: int) -> None:  # type: ignore[no-untyped-def]
    """The exact Debian 12 / Ubuntu 22.04 machine this check was wrong about."""
    machine(npx=True, node=major)
    check = diagnostics._check_repomix()

    assert check.status is CheckStatus.warn, (
        f"Node {major} cannot run Repomix (needs >={MIN_NODE}) yet the check is "
        f"{check.status.value}: {check.detail}"
    )
    assert str(major) in check.detail, check.detail
    assert str(MIN_NODE) in check.detail, (
        "the warning must name the floor — a user told only that their Node is "
        f"'too old' cannot act on it: {check.detail}"
    )
    assert check.fix and str(MIN_NODE) in check.fix, check.fix


def test_an_installed_repomix_is_still_warned_about_on_an_old_node(machine) -> None:  # type: ignore[no-untyped-def]
    """`repomix` on PATH does not bring its own interpreter.

    A global `npm install -g repomix` run on an old Node leaves the launcher
    right there on PATH and still cannot run. Checking `which("repomix")` first
    and returning `ok` — which is what the old code did — misses this.
    """
    machine(repomix=True, node=18)
    check = diagnostics._check_repomix()

    assert check.status is CheckStatus.warn, check.detail
    assert "18" in check.detail and "installed" in check.detail, check.detail


@pytest.mark.parametrize("major", [MIN_NODE, MIN_NODE + 1, 26])
def test_a_node_at_or_above_the_floor_is_ok(machine, major: int) -> None:  # type: ignore[no-untyped-def]
    """The floor is inclusive: `>=22.0.0` means 22 passes.

    Pinned because an off-by-one here is invisible in the common case — every
    developer machine is far above the floor — and would only ever surface on
    the one distribution that ships exactly 22.
    """
    machine(npx=True, node=major)
    check = diagnostics._check_repomix()

    assert check.status is CheckStatus.ok, check.detail
    assert str(major) in check.detail, check.detail


@pytest.mark.parametrize(
    ("node_on_path", "expected"),
    [(False, "no node on PATH"), (True, "Node version not readable")],
    ids=["no-node", "unreadable"],
)
def test_a_node_that_cannot_be_read_reports_rather_than_guesses(  # type: ignore[no-untyped-def]
    machine, node_on_path: bool, expected: str
) -> None:
    """Untested against the floor is `ok` and says which of the two it is.

    Two distinguishable machines, and the words are the whole point: `npx`
    present with no `node` beside it is odd enough to name, and a `node` whose
    `--version` did not parse is a fork or a wrapper. Collapsing them into one
    "could not determine" would lose the only clue either user gets.
    """
    machine(npx=True, node=None, node_on_path=node_on_path)

    check = diagnostics._check_repomix()

    assert check.status is CheckStatus.ok, check.detail
    assert expected in check.detail, check.detail
    assert "untested" in check.detail, (
        f"an unread version must be reported as untested, not implied ok: {check.detail}"
    )


def test_no_launcher_at_all_still_names_the_version_to_install(machine) -> None:  # type: ignore[no-untyped-def]
    """The absent case kept its warning, and gained the number.

    "Install Node.js" was the old fix, and it is what puts Node 18 on a Debian
    box. The floor belongs in the advice.
    """
    machine()
    check = diagnostics._check_repomix()

    assert check.status is CheckStatus.warn, check.detail
    assert check.fix and str(MIN_NODE) in check.fix, check.fix


def test_the_check_survives_a_node_that_explodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Diagnostics must never crash — the rule every sibling check follows."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        diagnostics, "_node_major", lambda _binary: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    check = diagnostics._check_repomix()

    assert check.status is CheckStatus.ok
    assert "not evaluated" in check.detail, check.detail


# --- the parser, against the strings six real tools actually print -----------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("v26.7.0\n", 26),
        ("v22.0.0\n", 22),
        ("v18.20.4\n", 18),
        ("v12.22.9\n", 12),
        # Node has never printed these, but a wrapper or a fork might; the
        # answer must be None rather than a number read out of the wrong place.
        ("node version 26\n", None),
        ("", None),
        ("v\n", None),
    ],
)
def test_node_major_reads_the_version_string(
    monkeypatch: pytest.MonkeyPatch, output: str, expected: int | None
) -> None:
    """`v26.7.0` → 26. Anchored, so `18` inside a banner is not mistaken for one."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, output, ""),
    )
    assert diagnostics._node_major("/usr/bin/node") == expected


@pytest.mark.parametrize(
    "failure",
    [OSError("no such binary"), subprocess.TimeoutExpired("node", 10)],
    ids=["oserror", "timeout"],
)
def test_node_major_answers_none_when_the_process_will_not_run(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """A binary that vanished between `which` and `exec`, and a hung one.

    The timeout case is the one worth having: `doctor` is what you run when the
    machine is behaving oddly, so a version read that can hang forever is a
    diagnostic that hangs exactly when it is needed.
    """

    def explode(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(subprocess, "run", explode)
    assert diagnostics._node_major("/usr/bin/node") is None


def test_a_nonzero_exit_is_not_a_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Something printed on a failed run must not be parsed as a version."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, "v18.0.0", "broken"),
    )
    assert diagnostics._node_major("/usr/bin/node") is None
