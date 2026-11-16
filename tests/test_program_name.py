"""``core.program``: the word every printed command starts with (#135, RLX R3; the manager's
ruling on Q2, brief 180), and the doctor's buttons read back from hints that print it.

The suite pins the word to ``rlx`` (conftest's ``hints_print_rlx``); the tests of the choice
itself put the real :func:`program.ours_on_path` back and build the PATH it reads.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as core
from aisquare.core import program
from aisquare.core.program import ours_on_path as real_ours_on_path
from aisquare.core.shell_line import command_line
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics, onboarding

SUFFIX = ".exe" if sys.platform == "win32" else ""


def _script(directory: Path, name: str) -> Path:
    """A program ``shutil.which`` finds as ``name``: ``name.exe`` on Windows, as uv writes
    a console script there, and executable elsewhere."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}{SUFFIX}"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _path(monkeypatch: pytest.MonkeyPatch, *directories: Path) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(str(directory) for directory in directories))
    program.command.cache_clear()


@pytest.fixture
def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This install's scripts directory, as a uv tool's: ``aisquare`` and ``rlx`` side by
    side (``agents.current_install`` names the first), and the real PATH test back."""
    scripts = tmp_path / "tools" / "aisquare-cli" / "bin"
    _script(scripts, "aisquare")
    _script(scripts, "rlx")
    monkeypatch.setattr(agent_core, "current_install", lambda: scripts / f"aisquare{SUFFIX}")
    monkeypatch.setattr(program, "ours_on_path", real_ours_on_path)
    program.command.cache_clear()
    return scripts


# --------------------------------------------------------------------------- the word


def test_hints_say_rlx_while_the_rlx_on_path_is_this_installs_own(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path(monkeypatch, install)

    assert program.command() == "rlx"
    assert program.line("accounts", "add") == "rlx accounts add"


@pytest.mark.skipif(sys.platform == "win32", reason="uv links ~/.local/bin on POSIX only")
def test_a_link_to_it_as_uv_makes_in_local_bin_is_ours(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv tool install`` puts a link in ``~/.local/bin`` to the tool's own script: the
    file a link leads to is what is compared."""
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "rlx").symlink_to(install / "rlx")
    _path(monkeypatch, local)

    assert program.command() == "rlx"


def test_another_programs_rlx_first_on_path_makes_hints_say_aisquare(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyPI's ``relax`` is another project, and a user's own ``rlx`` may come first on PATH:
    a hint that said ``rlx …`` would send them to it."""
    other = tmp_path / "other" / "bin"
    _script(other, "rlx")
    _path(monkeypatch, other, install)
    foreign = (program.command(), program.line("doctor"))
    _path(monkeypatch, install, other)
    ours_first = program.command()

    assert foreign == ("aisquare", "aisquare doctor")
    assert ours_first == "rlx", "control: ours first on PATH is ours"


def test_no_rlx_on_path_makes_hints_say_aisquare(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout run as ``python -m aisquare``, or a PATH without ``~/.local/bin``: nothing
    answers to ``rlx``, so a hint names the command this install has always answered to."""
    _path(monkeypatch, tmp_path / "empty")

    assert program.command() == "aisquare"


def test_the_word_is_decided_once_per_process(
    install: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path(monkeypatch, install)
    asked: list[str] = []
    which = shutil.which

    def counting(name: str, *args: object, **kwargs: object) -> str | None:
        asked.append(name)
        return which(name)

    monkeypatch.setattr(shutil, "which", counting)
    words = {program.command() for _ in range(5)}

    assert words == {"rlx"} and asked == ["rlx"]


def test_a_line_is_quoted_for_the_shell_it_is_pasted_into(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _path(monkeypatch, install)
    folder = str(tmp_path / "a folder")

    assert program.line("accounts", "trust", folder) == command_line(
        ["rlx", "accounts", "trust", folder]
    )


# ------------------------------------------------------------------ the doctor's buttons

#: One hint's command for each of ``onboarding.KNOWN_FIXES`` (after the program's word), and
#: the button it makes. A new entry in that table needs one here.
_SAMPLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "agents connect": (
        "agents connect claude-code --config-dir /tmp/c",
        ("agents", "connect", "claude-code", "--config-dir", "/tmp/c"),
    ),
    "project onboard": ("project onboard --refresh", ("project", "onboard", "--refresh")),
    "doctor --fix": ("doctor --fix --yes", ("doctor", "--fix", "--yes")),
    "accounts run": ("accounts run 3 login --device-auth", ("accounts", "run", "3")),
}


def _warn(fix: str) -> DoctorCheck:
    return DoctorCheck(name="row", status=CheckStatus.warn, detail="something", fix=fix)


def test_every_known_fix_makes_its_button_from_either_word_a_hint_prints() -> None:
    """A hint starts its command with ``rlx`` or, with another program's ``rlx`` first on
    PATH, ``aisquare`` (``program.PRINTED``): either makes the same button. Before #135 the
    parser read only ``aisquare``, so a hint reworded to ``rlx`` lost its button while the
    tests pinning the old words stayed green."""
    assert set(_SAMPLES) == {known.mention for known in onboarding.KNOWN_FIXES}
    for word in program.PRINTED:
        for mention, (command, argv) in _SAMPLES.items():
            fixes = onboarding.fix_commands([_warn(f"Fix it: {word} {command}")])
            assert [fix.argv for fix in fixes] == [argv], (word, mention)
    for word in ("asq", "relax", "uvx"):  # control: no hint prints these, and none is read
        assert onboarding.fix_commands([_warn(f"{word} doctor --fix --yes")]) == [], word


def test_a_buttons_label_names_the_command_with_the_word_hints_print(
    install: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [fix] = onboarding.fix_commands([_warn("aisquare accounts run 3")])
    _path(monkeypatch, install)
    ours = fix.label
    other = tmp_path / "other"
    _script(other, "rlx")
    _path(monkeypatch, other, install)
    foreign = fix.label

    assert (ours, foreign) == ("rlx accounts run 3", "aisquare accounts run 3")


def _buttons_in_each_word(
    rows: Callable[[], list[DoctorCheck]], monkeypatch: pytest.MonkeyPatch
) -> dict[str, list[tuple[str, tuple[str, ...]]]]:
    """The buttons the doctor makes of ``rows()``, with the hints printing ``rlx`` and again
    printing ``aisquare`` (another program's ``rlx`` first on PATH), keyed by that word; each
    button's label starts with the word its hint printed."""
    made = {}
    for ours in (True, False):
        monkeypatch.setattr(program, "ours_on_path", lambda name, ours=ours: ours)
        program.command.cache_clear()
        word = program.command()
        fixes = onboarding.fix_commands(rows())
        assert all(fix.label.startswith(f"{word} ") for fix in fixes), (word, fixes)
        made[word] = sorted((fix.check, fix.argv) for fix in fixes)
    return made


def test_the_doctors_own_hints_make_the_same_buttons_whichever_word_they_print(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every button the doctor makes today, from its real rows in each state that makes one
    (#135's button proof, in a test's home), with the hints printing either word: the same
    commands. Claude Code on PATH and never connected, a Claude slot and a Codex slot that
    never signed in; then Codex chosen (`agents use codex`) and not connected."""
    agents = tmp_path / "agents-on-path"
    found = {"claude-code": str(agents / "claude"), "codex": str(agents / "codex")}
    monkeypatch.setattr(agent_core, "on_path", found.get)
    core.create_account("claude-code")
    core.create_account("codex")
    first = _buttons_in_each_word(
        lambda: [
            diagnostics._check_claude_code(),
            *diagnostics._claude_accounts_checks(),
            *diagnostics._agent_accounts_checks("codex"),
        ],
        monkeypatch,
    )
    used = runner.invoke(app, ["--json", "agents", "use", "codex"])
    assert used.exit_code == 0, used.output
    then = _buttons_in_each_word(diagnostics._check_other_agents, monkeypatch)

    assert first["rlx"] == first["aisquare"] and then["rlx"] == then["aisquare"]
    assert first["rlx"] == [
        ("claude-accounts", ("accounts", "run", "2")),
        ("claude-code", ("agents", "connect", "claude-code")),
        ("codex-accounts", ("accounts", "run", "3")),
    ]
    assert [(check, argv[:4]) for check, argv in then["rlx"]] == [
        ("codex", ("agents", "connect", "codex", "--config-dir"))
    ]
