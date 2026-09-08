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

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

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

    def configure(
        *,
        repomix: bool,
        npx: bool,
        node: tuple[int, ...] | None,
        node_on_path: bool = True,
        installed_floor: tuple[int, ...] | None = None,
    ) -> None:
        present = {"repomix": repomix, "npx": npx, "node": node_on_path}
        monkeypatch.setattr(
            "aisquare.services.diagnostics.shutil.which",
            lambda name: f"/usr/bin/{name}" if present.get(name) else None,
        )
        monkeypatch.setattr(snapshot_core, "node_version", lambda: node)
        monkeypatch.setattr(snapshot_core, "installed_repomix_floor", lambda: installed_floor)

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


class TestNodeAbsentIsNotMerelyUnknown:
    """``node_version() is None`` is three facts, and one of them is a broken machine.

    It answers ``None`` for a Node that is absent, one that exits non-zero, and
    one whose output will not parse. Reporting all three as "untested, snapshots
    enabled" put the first back into the shape this whole check was rewritten to
    remove: ``repomix`` and ``npx`` are both ``#!/usr/bin/env node`` scripts
    (verified on this machine), so with no Node on PATH packing cannot run at
    all, and green over that is a lie rather than a gap.

    Reachable without contriving anything: a ``repomix`` shim whose Node came
    from a version manager that a non-interactive shell never sources, or a
    container image where Node was pruned after the global install.
    """

    def test_no_node_on_path_warns_even_though_repomix_is_there(self, tools: Tools) -> None:
        tools(repomix=True, npx=True, node=None, node_on_path=False)
        check = diagnostics._check_repomix()
        assert check.status is CheckStatus.warn
        assert "Node is not on PATH" in check.detail
        assert "cannot run" in check.detail

    def test_the_npx_path_is_no_different(self, tools: Tools) -> None:
        """npx is a Node script too, so it is not a way around a missing Node."""
        tools(repomix=False, npx=True, node=None, node_on_path=False)
        assert diagnostics._check_repomix().status is CheckStatus.warn

    def test_an_unreadable_but_present_node_is_still_only_untested(self, tools: Tools) -> None:
        """The distinction: Node IS installed, it just would not say which version."""
        tools(repomix=True, npx=True, node=None, node_on_path=True)
        check = diagnostics._check_repomix()
        assert check.status is CheckStatus.ok
        assert "not readable" in check.detail

    def test_the_advice_covers_a_version_manager_shim(self, tools: Tools) -> None:
        """The likeliest cause is a Node that exists but is not on THIS PATH."""
        tools(repomix=True, npx=True, node=None, node_on_path=False)
        fix = diagnostics._check_repomix().fix or ""
        assert "on PATH" in fix


class TestTheFloorIsPerPath:
    """``MIN_NODE`` is the LATEST repomix's floor, and only the npx path runs that.

    ``npx --yes repomix`` fetches the newest release, so the constant is right
    there. An installed ``repomix`` is whatever was pinned — ``npm install -g
    repomix@0.2`` on Node 18 may pack perfectly well — and judging it by the
    latest release's floor is the same false positive this check's docstring
    rules out ("would send someone to reinstall a working toolchain"), just from
    the other direction. So where a repomix is installed, its own
    ``engines.node`` is the authority.
    """

    def test_a_pinned_old_repomix_with_a_lower_floor_is_not_warned_about(
        self, tools: Tools
    ) -> None:
        """The false positive: Node 18, repomix@0.2 declaring >=16. It works."""
        tools(repomix=True, npx=True, node=(18, 19, 1), installed_floor=(16,))
        check = diagnostics._check_repomix()
        assert check.status is CheckStatus.ok
        assert "18.19.1" in check.detail

    def test_the_installed_floor_still_bites_when_it_is_not_met(self, tools: Tools) -> None:
        tools(repomix=True, npx=True, node=(18, 19, 1), installed_floor=(22, 0, 0))
        check = diagnostics._check_repomix()
        assert check.status is CheckStatus.warn
        assert "the installed repomix needs" in check.detail
        assert "22.0.0+" in check.detail

    def test_a_higher_installed_floor_is_honoured_so_the_constant_cannot_under_warn(
        self, tools: Tools
    ) -> None:
        """When repomix RAISES its floor, a hardcoded 22 would pass a broken machine."""
        tools(repomix=True, npx=True, node=(22, 1, 0), installed_floor=(24,))
        assert diagnostics._check_repomix().status is CheckStatus.warn

    def test_an_unreadable_engines_falls_back_to_the_constant(self, tools: Tools) -> None:
        """Unknown is not treated as satisfied — the documented floor still applies."""
        tools(repomix=True, npx=True, node=(18, 19, 1), installed_floor=None)
        check = diagnostics._check_repomix()
        assert check.status is CheckStatus.warn
        assert "repomix needs" in check.detail

    def test_the_npx_path_ignores_any_installed_floor(self, tools: Tools) -> None:
        """`npx --yes` fetches the latest, so a stale local floor must not apply.

        Without this, a machine with an ancient global repomix AND npx would be
        judged by the ancient one while `_repomix_base()` prefers... the direct
        binary. The gate only reads the floor on the path that will actually run.
        """
        tools(repomix=False, npx=True, node=(18, 19, 1), installed_floor=(16,))
        assert diagnostics._check_repomix().status is CheckStatus.warn


class TestInstalledRepomixFloor:
    """``snapshot_core.installed_repomix_floor`` — reading npm metadata off disk."""

    def _package(self, tmp_path: Path, spec: str | None, *, nested: bool = False) -> Path:
        root = tmp_path / "repomix"
        binary = root / "bin" / "repomix.cjs"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/env node\n")
        payload: dict[str, object] = {"name": "repomix", "version": "1.18.0"}
        if spec is not None:
            payload["engines"] = {"node": spec}
        target = binary.parent if nested else root
        (target / "package.json").write_text(json.dumps(payload))
        return binary

    def test_reads_the_range_repomix_actually_declares(self, tmp_path: Path) -> None:
        binary = self._package(tmp_path, ">=22.0.0")
        assert snapshot_core.installed_repomix_floor(str(binary)) == (22, 0, 0)

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (">=22.0.0", (22, 0, 0)),
            (">= 22", (22,)),
            (">22.1", (22, 1)),
            ("^22.0.0", (22, 0, 0)),
            ("~18.4.0", (18, 4, 0)),
            (">=v20.1.0", (20, 1, 0)),
        ],
    )
    def test_the_shapes_an_engines_range_comes_in(
        self, tmp_path: Path, spec: str, expected: tuple[int, ...]
    ) -> None:
        binary = self._package(tmp_path, spec)
        assert snapshot_core.installed_repomix_floor(str(binary)) == expected

    def test_a_package_json_beside_the_bin_is_found_too(self, tmp_path: Path) -> None:
        """pnpm and yarn nest differently from npm; both parents are tried."""
        binary = self._package(tmp_path, ">=22.0.0", nested=True)
        assert snapshot_core.installed_repomix_floor(str(binary)) == (22, 0, 0)

    def test_no_engines_field_is_none_not_a_guess(self, tmp_path: Path) -> None:
        binary = self._package(tmp_path, None)
        assert snapshot_core.installed_repomix_floor(str(binary)) is None

    def test_an_unparseable_range_is_none(self, tmp_path: Path) -> None:
        binary = self._package(tmp_path, "*")
        assert snapshot_core.installed_repomix_floor(str(binary)) is None

    def test_malformed_json_is_none_not_a_traceback(self, tmp_path: Path) -> None:
        root = tmp_path / "repomix"
        binary = root / "bin" / "repomix.cjs"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/usr/bin/env node\n")
        (root / "package.json").write_text("{ not json")
        assert snapshot_core.installed_repomix_floor(str(binary)) is None

    def test_no_repomix_on_path_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("aisquare.core.snapshot.shutil.which", lambda _n: None)
        assert snapshot_core.installed_repomix_floor() is None
