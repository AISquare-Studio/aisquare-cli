"""`doctor` should say which SOURCE the installed build came from.

``aisquare --version`` reports 0.4.0rc1 for a build made from this checkout and
for one made from a sibling worktree. That is how a stale install survived a
whole shift on this machine while five separate mechanisms were blamed for
"which build am I running": PATH resolving to an old install, the pyenv shim
behind a non-existent .venv prefix, stale bytecode from a same-size edit, a lane
venv pinned to the commit it was built from, and a train that moved forward
between a fetch and a measurement.

pip already records the answer in ``direct_url.json`` for anything installed
from a path. Nothing read it, so the answer existed and was invisible. On this
machine it says the running build came from a worktree named ``main-install``
that no longer exists — which is exactly the state an operator cannot otherwise
detect, and the state the runbook's step 1 exists to leave.

A DETECTOR: it reports so a human decides. The single warning case is a source
directory that is GONE, because that build cannot be verified against its source,
cannot be reinstalled from it, and is by definition not the tree anyone works in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aisquare.models import CheckStatus
from aisquare.services import diagnostics


def _record(monkeypatch: pytest.MonkeyPatch, payload: str | None) -> None:
    """Stand in for the installed distribution's direct_url.json."""

    class _Dist:
        def read_text(self, name: str) -> str | None:
            return payload

    # String target: patching a module attribute through another module is an
    # attr-defined error under strict mypy, and the string form is checked at
    # run time by monkeypatch itself rather than by the type checker.
    monkeypatch.setattr("aisquare.services.diagnostics.metadata.distribution", lambda name: _Dist())


def test_a_path_install_names_its_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _record(monkeypatch, json.dumps({"dir_info": {}, "url": f"file://{tmp_path}"}))

    check = diagnostics._check_provenance()

    assert check.status is CheckStatus.ok
    assert str(tmp_path) in check.detail
    assert "non-editable" in check.detail


def test_an_editable_install_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The distinction matters: §5 installs an extra that bricks an editable checkout."""
    _record(monkeypatch, json.dumps({"dir_info": {"editable": True}, "url": f"file://{tmp_path}"}))

    assert "editable" in diagnostics._check_provenance().detail


def test_a_vanished_source_is_the_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live state on this machine, and the one an operator cannot otherwise see."""
    _record(monkeypatch, json.dumps({"dir_info": {}, "url": "file:///gone/main-install"}))

    check = diagnostics._check_provenance()

    assert check.status is CheckStatus.warn
    # Rendered the way THIS platform writes a path: the check builds a `Path`
    # from the URL, and `str(Path("/gone/main-install"))` is backslash-separated
    # on Windows. Asserting the POSIX spelling would pin the separator rather
    # than the property, which is that the operator is told which directory
    # vanished.
    assert str(Path("/gone/main-install")) in check.detail
    assert "NO LONGER EXISTS" in check.detail
    assert "absolute" in (check.fix or "") or "/path/to" in (check.fix or ""), (
        "the fix must show the absolute-path install form — a bare `pip install .` "
        "resolves against the current directory, which is how 30 sibling worktrees "
        "passed the old verification"
    )


def test_an_index_install_is_reported_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    """No direct_url.json means pip fetched it; that is normal, not a finding."""
    _record(monkeypatch, None)

    check = diagnostics._check_provenance()

    assert check.status is CheckStatus.ok
    assert "index" in check.detail


def test_unreadable_metadata_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A diagnostic must never be the thing that breaks a machine."""

    def _boom(name: str) -> Any:
        raise ValueError("no such distribution")

    monkeypatch.setattr("aisquare.services.diagnostics.metadata.distribution", _boom)

    assert diagnostics._check_provenance().status is CheckStatus.ok


def test_malformed_json_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch, "{not json at all")

    assert diagnostics._check_provenance().status is CheckStatus.ok


def test_it_never_fails_the_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Where a build came from must never be why `doctor` reports broken."""
    for payload in (
        None,
        "{bad",
        json.dumps({"dir_info": {}, "url": f"file://{tmp_path}"}),
        json.dumps({"dir_info": {}, "url": "file:///gone"}),
        json.dumps({"url": "https://example.invalid/x.whl"}),
    ):
        _record(monkeypatch, payload)
        assert diagnostics._check_provenance().status is not CheckStatus.fail


def test_the_check_runs_beside_the_install_line() -> None:
    """A check nobody runs is a function, and this one answers `install`'s question."""
    names = [check.name for check in diagnostics.doctor()]

    assert "provenance" in names
    assert names.index("provenance") == names.index("install") + 1
