"""The repomix check must not be green on a Node that cannot run repomix.

THE DEFECT THIS PINS. ``_check_repomix`` reported ``ok`` when ``npx`` merely
EXISTED. Repomix 1.18.0's own package metadata declares ``"node": ">=22.0.0"``;
Debian 12 ships Node 18 and Ubuntu 22.04 ships 12. On those machines ``npx`` is
present, the check was green, and the first ``aisquare project onboard`` failed
at run time — a green line over a feature that cannot run, which is the one
shape a diagnostic must never have. It is also the specific way an installer
could report success and hand over a machine that cannot pack a snapshot.

WHAT IS DELIBERATELY *NOT* A FAILURE, and why each is a considered ruling
rather than leniency:

- **Unreadable is unreadable, not too old.** ``node_version()`` answers ``None``
  for a Node that will not run, prints something unparseable, or is absent from
  PATH while ``repomix`` is not. Guessing "too old" there would send someone to
  reinstall a working toolchain; the line says untested instead and stays ``ok``.
- **Too old is a WARNING, never a fail.** ``home`` is the only check that fails,
  and snapshots are one feature. A machine that runs every other command is not
  unhealthy — the same ruling ``_check_tmux`` already makes for the fleet.
- **The floor is read from ``snapshot_core.MIN_NODE``**, not written here. A
  test that hardcoded 22 would keep passing while the code moved, and the
  version this project supports is the code's fact, not the test's.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import pytest

from aisquare.core import snapshot as snapshot_core
from aisquare.models import CheckStatus
from aisquare.services import diagnostics

_TOO_OLD = (snapshot_core.MIN_NODE[0] - 1, 9, 9)
_AT_FLOOR = snapshot_core.MIN_NODE
_NEWER = (snapshot_core.MIN_NODE[0] + 4, 7, 0)


#: ``(*, repomix, npx, node) -> None`` — what the check will find.
Tools = Callable[..., None]


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> Tools:
    """Control what is on PATH and what Node reports, without touching either."""

    def configure(*, repomix: bool, npx: bool, node: tuple[int, ...] | None) -> None:
        present = {"repomix": repomix, "npx": npx}
        monkeypatch.setattr(
            "aisquare.services.diagnostics.shutil.which",
            lambda name: f"/usr/bin/{name}" if present.get(name) else None,
        )
        monkeypatch.setattr(snapshot_core, "node_version", lambda: node)

    return configure


def test_npx_present_but_node_too_old_is_a_warning_not_a_green_line(tools: Tools) -> None:
    """The bug, stated as the test that would have caught it."""
    tools(repomix=False, npx=True, node=_TOO_OLD)
    check = diagnostics._check_repomix()
    assert check.status is CheckStatus.warn
    assert "older than repomix needs" in check.detail
    assert "21.9.9" in check.detail
    assert check.fix and "nodejs.org" in check.fix


def test_a_direct_repomix_binary_is_gated_too(tools: Tools) -> None:
    """repomix IS a Node CLI, so an old Node breaks the direct path identically.

    Not a duplicate of the test above: the two paths produce different details,
    and gating only the `npx` one would leave `npm install -g repomix` machines
    green and broken.
    """
    tools(repomix=True, npx=True, node=_TOO_OLD)
    check = diagnostics._check_repomix()
    assert check.status is CheckStatus.warn
    assert "repomix found" in check.detail


def test_the_floor_itself_passes(tools: Tools) -> None:
    """`>=22` means 22 is fine — an off-by-one here fails every current machine."""
    tools(repomix=False, npx=True, node=_AT_FLOOR)
    assert diagnostics._check_repomix().status is CheckStatus.ok


def test_a_new_enough_node_is_ok_and_says_which(tools: Tools) -> None:
    tools(repomix=True, npx=True, node=_NEWER)
    check = diagnostics._check_repomix()
    assert check.status is CheckStatus.ok
    assert "26.7.0" in check.detail
    assert check.fix is None


def test_an_unreadable_node_is_reported_as_untested_not_as_too_old(tools: Tools) -> None:
    """Failing open: the verdict is lost, nobody is sent to fix a working Node."""
    tools(repomix=False, npx=True, node=None)
    check = diagnostics._check_repomix()
    assert check.status is CheckStatus.ok
    assert "not readable" in check.detail
    assert "untested" in check.detail


def test_no_repomix_and_no_npx_still_warns_and_now_names_the_floor(tools: Tools) -> None:
    tools(repomix=False, npx=False, node=None)
    check = diagnostics._check_repomix()
    assert check.status is CheckStatus.warn
    assert "repomix not found" in check.detail
    floor = ".".join(str(part) for part in snapshot_core.MIN_NODE)
    assert check.fix and f"Node.js {floor}+" in check.fix


def test_the_upgrade_advice_does_not_send_anyone_to_their_package_manager(tools: Tools) -> None:
    """On the distributions that ship an old Node, `apt install nodejs` IS it.

    So the advice deliberately does not use `install_hint()` the way the tmux
    check does. This pins the reasoning, because "be consistent with tmux" is
    the obvious later edit and it would make the advice circular.
    """
    tools(repomix=False, npx=True, node=_TOO_OLD)
    fix = diagnostics._check_repomix().fix or ""
    assert "apt install nodejs" not in fix
    assert "dnf install nodejs" not in fix
    assert "fnm" in fix or "nvm" in fix


class TestNodeVersionReader:
    """``snapshot_core.node_version`` — the parser, without a real Node."""

    def test_reads_the_v_prefixed_form_node_actually_prints(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub(monkeypatch, stdout="v26.7.0\n")
        assert snapshot_core.node_version() == (26, 7, 0)

    def test_a_prerelease_suffix_is_ignored_rather_than_parsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`v23.0.0-nightly2024abc` — the numeric run is the answer."""
        self._stub(monkeypatch, stdout="v23.0.0-nightly2024abc\n")
        assert snapshot_core.node_version() == (23, 0, 0)

    def test_returns_a_tuple_so_comparison_is_not_textual(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason it is not a string: "9" > "22" as text, and 9 < 22 is the truth."""
        self._stub(monkeypatch, stdout="v9.0.0\n")
        version = snapshot_core.node_version()
        assert version is not None and version < snapshot_core.MIN_NODE

    def test_absent_node_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aisquare.core.snapshot.shutil.which", lambda _n: None)
        assert snapshot_core.node_version() is None

    def test_a_nonzero_exit_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub(monkeypatch, stdout="v26.7.0\n", returncode=1)
        assert snapshot_core.node_version() is None

    def test_unparseable_output_is_none_not_a_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._stub(monkeypatch, stdout="not a version\n")
        assert snapshot_core.node_version() is None

    def test_a_node_that_will_not_run_is_none_not_a_traceback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Diagnostics must never crash — an OSError from the spawn is `None`."""
        monkeypatch.setattr("aisquare.core.snapshot.shutil.which", lambda _n: "/usr/bin/node")

        def explode(*_args: object, **_kwargs: object) -> object:
            raise OSError("Exec format error")

        monkeypatch.setattr("aisquare.core.snapshot.subprocess.run", explode)
        assert snapshot_core.node_version() is None

    def test_a_timeout_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aisquare.core.snapshot.shutil.which", lambda _n: "/usr/bin/node")

        def hang(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd="node", timeout=10)

        monkeypatch.setattr("aisquare.core.snapshot.subprocess.run", hang)
        assert snapshot_core.node_version() is None

    @staticmethod
    def _stub(monkeypatch: pytest.MonkeyPatch, *, stdout: str, returncode: int = 0) -> None:
        monkeypatch.setattr("aisquare.core.snapshot.shutil.which", lambda _n: "/usr/bin/node")

        class _Result:
            def __init__(self) -> None:
                self.stdout = stdout
                self.returncode = returncode

        monkeypatch.setattr("aisquare.core.snapshot.subprocess.run", lambda *_a, **_k: _Result())
