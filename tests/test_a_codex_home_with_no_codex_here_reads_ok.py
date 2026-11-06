"""With no Codex installed, the doctor's codex row does not warn (review r40 of #113, finding 3).

An exported ``CODEX_HOME``, or 0.7.0's hookless ``agents connect codex`` record (a record of
``~/.codex`` with nothing installed there), had the head's doctor grade that home: ``warn
codex | Codex: not_configured in … — aisquare's hooks are not installed there``, on a
machine with no Codex at all. 0.9.2 read ``ok`` there, and install.sh counts any amber codex
row but "unverified" as unexpected, so a Claude Code user's re-install exited 2 and never
short-circuited. Now the home the shell names is graded where Codex is the selected agent,
and a home only a record names where Codex is installed; one with aisquare's hooks in it
wherever it is.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.config import load_config, save_config
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics

CODEX = "codex"


@pytest.fixture
def codex_on_path(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Whether Codex's binary is installed here, as the doctor asks (``agents.on_path``)."""
    real = agent_core.on_path

    def installed(yes: bool) -> None:
        monkeypatch.setattr(
            agent_core,
            "on_path",
            lambda name: ("/usr/local/bin/codex" if yes else None) if name == CODEX else real(name),
        )

    installed(False)
    return installed


def _exported_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))


def _hookless_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = accounts_core.default_account(agent=CODEX).config_dir
    plain.mkdir(parents=True, exist_ok=True)
    agent_core.set_connected(CODEX, True, plain)  # what 0.7.0's `agents connect codex` wrote
    assert agent_core.connected_dirs(CODEX) and not agent_core.hooks_installed(CODEX, plain)


_SETUPS = [_exported_codex_home, _hookless_record]
_IDS = ["exported-CODEX_HOME", "0.7.0-hookless-record"]


def _codex_rows() -> list[DoctorCheck]:
    rows = [row for row in diagnostics._check_other_agents() if row.name == CODEX]
    assert rows, "the doctor has a codex row"
    return rows


def _install_sh_amber(rows: list[DoctorCheck]) -> list[str]:
    """What install.sh's ``doctor_amber`` lists for a doctor payload of ``rows`` (an ok row
    beside them), which ``summary`` then counts as expected, actionable or unexpected. A POSIX
    script: where its tests skip themselves (Windows), so does this half, the doctor's rows
    asserted before it."""
    if sys.platform == "win32":
        pytest.skip("install.sh is a POSIX shell script (its own tests skip here too)")
    from tests.test_install_script_functions import sh

    payload = json.dumps(
        [
            {"name": "home", "status": "ok", "detail": "x", "fix": None},
            *(
                {"name": row.name, "status": row.status.value, "detail": "d", "fix": row.fix}
                for row in rows
            ),
        ]
    )
    result = sh('doctor_amber "$PAYLOAD"; echo', env={"PAYLOAD": payload})
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.split("\n") if line]


@pytest.mark.parametrize("setup", _SETUPS, ids=_IDS)
def test_with_no_codex_installed_the_codex_row_is_ok_and_install_sh_has_nothing_amber(
    setup: Callable[[Path, pytest.MonkeyPatch], None],
    codex_on_path: Callable[[bool], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(tmp_path, monkeypatch)

    rows = _codex_rows()

    assert [row.status for row in rows] == [CheckStatus.ok] * len(rows), rows
    assert _install_sh_amber(rows) == [], "install.sh exits as 0.9.2 did: nothing amber"


def test_a_record_is_still_graded_where_codex_is_installed(
    codex_on_path: Callable[[bool], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hookless_record(tmp_path, monkeypatch)
    codex_on_path(True)

    rows = _codex_rows()

    assert [row.status for row in rows] == [CheckStatus.warn], rows
    assert "aisquare's hooks are not installed there" in rows[0].detail
    assert _install_sh_amber(rows) == [CODEX]


def test_the_home_the_shell_names_is_graded_where_codex_is_the_selected_agent(
    codex_on_path: Callable[[bool], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exported_codex_home(tmp_path, monkeypatch)
    config = load_config()
    config.agents.default = CODEX  # `agents use codex`
    save_config(config)

    rows = _codex_rows()

    assert CheckStatus.warn in [row.status for row in rows], rows


def test_the_home_the_shell_names_is_not_graded_where_codex_is_installed_but_not_selected(
    codex_on_path: Callable[[bool], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed is not chosen: a Claude Code user with Codex on PATH who exported CODEX_HOME
    reads ok, as on 0.9.2, and install.sh has nothing amber (#96, a mutant 11240 saw survive:
    the shell's home graded wherever Codex is installed)."""
    _exported_codex_home(tmp_path, monkeypatch)
    codex_on_path(True)

    rows = _codex_rows()

    assert [row.status for row in rows] == [CheckStatus.ok] * len(rows), rows
    assert _install_sh_amber(rows) == []


def test_an_exported_empty_codex_home_with_no_codex_reads_not_detected(
    codex_on_path: Callable[[bool], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r41 of #113, nit 3: the row said "Codex detected" for the folder CODEX_HOME names when
    it was empty and Codex was not installed; 0.9.2 said "not detected". A home holding Codex's
    files, or Codex on PATH, is still detected."""
    _exported_codex_home(tmp_path, monkeypatch)

    assert [row.detail for row in _codex_rows()] == ["Codex not detected on this machine"]

    (tmp_path / "codex-home" / "auth.json").write_text("{}", encoding="utf-8")
    assert [row.detail for row in _codex_rows()] == [
        "Codex detected; AISquare integration not selected"
    ], "a home with Codex's files in it"
    (tmp_path / "codex-home" / "auth.json").unlink()
    codex_on_path(True)
    assert [row.detail for row in _codex_rows()] == [
        "Codex detected; AISquare integration not selected"
    ], "Codex installed"
