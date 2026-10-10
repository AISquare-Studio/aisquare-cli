"""Coding-agent adapters (roadmap 9.1, Doctor half): one "connected?" check, no empty connections.

``claude_code_connected`` (core, with ``services.agents`` its public face) is the one
answer to "is Claude Code connected here?". The doctor's Connect button, ``agents
list`` and ``agents status``, the Accounts page and the Welcome view ask it, and the
plugin route extends it, so the hooks are never offered, or installed, twice.
``agents connect`` refuses an agent aisquare has no hooks for (Codex, Cursor) instead
of recording a connection that installs nothing, and names a file of Claude Code's it
cannot read instead of calling Claude Code not installed. The doctor has a row for
every agent in the registry: Claude Code's with its Connect fix, the others ok,
naming the path they checked and a release only where one is planned, with no
button. Each claim has its negative control in the same test.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core import paths
from aisquare.core.selfcli import CliResult
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import agents as agents_service
from aisquare.services import claude_accounts as claude_accounts_service
from aisquare.services import diagnostics, first_run
from aisquare.services.onboarding import fix_commands
from tests.fsperms import can_deny_reads
from tests.test_no_traceback_on_a_damaged_store import damaged_store  # noqa: F401


@pytest.fixture
def claude_home(isolated_agent_home: Path) -> Path:
    """Claude Code installed (its config directory exists) and never connected."""
    claude = isolated_agent_home / ".claude"
    claude.mkdir(parents=True)
    return claude


def _connect(runner: CliRunner, config_dir: Path | None = None) -> None:
    argv = ["agents", "connect", "claude-code"]
    if config_dir is not None:
        argv += ["--config-dir", str(config_dir)]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output


def _refused_why(config_dir: Path | None = None) -> str | None:
    """Why `agents connect` would refuse ``config_dir``, in its own words, or ``None``."""
    refusal = agents_service.access("claude-code", config_dir).connect
    return None if refusal is None else refusal.why


def _stat_error(path: Path) -> str:
    """The operating system's own words when asked for ``path``, which is not there."""
    try:
        os.stat(path)
    except OSError as exc:
        return str(exc.strerror)
    raise AssertionError(f"{path} is there")


#: The doctor's remedy through CLAUDE_CONFIG_DIR, for the directory sessions from this
#: shell read, set or not: never "unset it", never first.
_REPOINT_FIX = (
    "point CLAUDE_CONFIG_DIR at another directory this user can write, "
    "then start asq or aisquare again from that shell"
)


def _step_two(fix: str | None) -> str:
    """Welcome step 2's remedies for a refusal of the directory this shell reads: the
    doctor's, as it words them (``agents.remedies``), with nothing after the last."""
    said = str(fix or "")
    return f"Connect cannot change that. {said[:1].upper()}{said[1:]}".rstrip()


_DISCONNECT = "aisquare agents disconnect claude-code"


def _repair(refusal: agents_service.Refusal | None, then: str = "then connect again") -> str:
    """The doctor's in-place remedy for ``refusal``: the path that blocks and its fact."""
    assert refusal is not None, "connect refuses"
    return f"repair {refusal.path} ({refusal.fact}), {then}"


# --------------------------------------------------------------------------- the check


def test_connected_follows_the_hooks_connect_installs(runner: CliRunner, claude_home: Path) -> None:
    assert agents_service.claude_code_connected() is False, "installed is not connected"

    _connect(runner)
    assert agents_service.claude_code_connected() is True

    assert runner.invoke(app, ["agents", "disconnect", "claude-code"]).exit_code == 0
    assert agents_service.claude_code_connected() is False, "disconnect takes it back"


def test_connected_is_answered_per_config_dir(
    runner: CliRunner, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel installs keep their own settings.json, so each directory has its own answer."""
    other = claude_home.parent / ".claude-account1"
    other.mkdir()
    _connect(runner, other)

    assert agents_service.claude_code_connected(other) is True
    assert agents_service.claude_code_connected(claude_home) is False
    assert agents_service.claude_code_connected() is False, "~/.claude was never connected"

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert agents_service.claude_code_connected() is True, (
        "no argument means the directory a claude started from this shell reads"
    )


def test_a_partial_or_foreign_install_is_not_connected(
    runner: CliRunner, claude_home: Path
) -> None:
    """Connect is what completes an older install, so a missing event answers False."""
    _connect(runner)
    settings_path = claude_home / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"].pop("StopFailure")
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    assert agents_service.claude_code_connected() is False, "one event short is not connected"

    foreign = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "webhook stop"}]}]}}
    settings_path.write_text(json.dumps(foreign), encoding="utf-8")
    assert agents_service.claude_code_connected() is False, "another tool's hook is not ours"

    _connect(runner)
    assert agents_service.claude_code_connected() is True, "control: reconnecting completes it"


def test_the_hooks_on_disk_answer_not_the_registry(runner: CliRunner, claude_home: Path) -> None:
    """agents.json records what this home connected; the hooks are what actually runs (#84)."""
    _connect(runner)
    settings_path = claude_home / "settings.json"
    hooked = settings_path.read_text(encoding="utf-8")
    settings_path.write_text("{}", encoding="utf-8")
    assert paths.agents_registry_path().exists(), "the registry still records the connection"
    assert agents_service.claude_code_connected() is False, (
        "a record with no hooks connects nothing"
    )

    settings_path.write_text(hooked, encoding="utf-8")
    shutil.rmtree(paths.aisquare_home())
    assert agents_service.claude_code_connected() is True, (
        "hooks run without any home recording them"
    )


def test_asking_reads_only_and_never_raises(claude_home: Path) -> None:
    """The Welcome view and the doctor ask this on every refresh: no state, no traceback."""
    home = paths.aisquare_home()
    assert not home.exists(), "the fixture starts from a home that does not exist"
    settings_path = claude_home / "settings.json"

    assert agents_service.claude_code_connected(claude_home.parent / ".claude-missing") is False
    assert agents_service.claude_code_connected() is False, "no settings.json yet"
    assert not settings_path.exists(), "asking must not create the file it reads"

    settings_path.write_text("{not json", encoding="utf-8")
    assert agents_service.claude_code_connected() is False
    settings_path.write_bytes(b"\xff\xfe not utf-8")
    assert agents_service.claude_code_connected() is False, "undecodable bytes answer False"
    settings_path.unlink()
    settings_path.mkdir()
    assert agents_service.claude_code_connected() is False, "an unreadable path answers False"

    assert not home.exists(), "asking created the aisquare home"


def _non_list_event(path: Path) -> None:
    path.write_text(json.dumps({"hooks": {"Stop": 5, "SessionEnd": True}}), encoding="utf-8")


def _not_utf8(path: Path) -> None:
    path.write_bytes(b"\xff\xfe")  # a UTF-16 byte-order mark


def _a_directory(path: Path) -> None:
    path.mkdir()


def _mode_000(path: Path) -> None:
    path.write_text("{}", encoding="utf-8")
    path.chmod(0)


#: settings.json shapes that each cost `aisquare --json doctor` its whole report (a
#: traceback, nothing on stdout) before the read-only readers went through `read_json`.
_DAMAGED_SETTINGS = {
    "non-list event": _non_list_event,
    "not UTF-8": _not_utf8,
    "a directory": _a_directory,
    "mode 000": _mode_000,
}
_UNREADABLE_SHAPES = {"not UTF-8", "a directory", "mode 000"}
_NEEDS_DENIED_READS = pytest.mark.skipif(
    not can_deny_reads(), reason="chmod(0) denies nothing here (root, or NTFS where it is advice)"
)


def _shapes(names: set[str] | None = None) -> list[object]:
    return [
        pytest.param(name, marks=_NEEDS_DENIED_READS if name == "mode 000" else (), id=name)
        for name in _DAMAGED_SETTINGS
        if names is None or name in names
    ]


def _cleared(path: Path) -> None:
    """Take a damaged shape away again, leaving nothing at ``path``."""
    if path.is_dir():
        path.rmdir()
    elif path.exists():
        path.chmod(0o600)
        path.unlink()


def _content(path: Path) -> object:
    """What is at ``path``, made readable again: a directory's entries or a file's bytes."""
    if path.is_dir():
        return sorted(child.name for child in path.iterdir())
    path.chmod(0o600)
    return path.read_bytes()


def _row_named(stdout: str, name: str) -> dict[str, object]:
    rows: list[dict[str, object]] = json.loads(stdout)
    return next(row for row in rows if row["name"] == name)


@pytest.mark.parametrize("shape", _shapes())
def test_a_damaged_settings_json_reads_as_no_hooks_not_a_traceback(
    runner: CliRunner, claude_home: Path, shape: str
) -> None:
    """One `agents connect` refuses (it cannot read it) is named, with no Connect: the
    click could only fail (review of #257). One it can rewrite reads as no hooks."""
    settings_path = claude_home / "settings.json"
    _DAMAGED_SETTINGS[shape](settings_path)
    try:
        connected = agents_service.claude_code_connected()
        damaged = runner.invoke(app, ["--json", "doctor"])
        refused = shape in _UNREADABLE_SHAPES
        # The click the row would offer, only where it must fail: one that works records
        # the directory, and the control below compares a never-connected home.
        clicked = runner.invoke(app, ["agents", "connect", "claude-code"]) if refused else None
    finally:
        _cleared(settings_path)
    settings_path.write_text("{}", encoding="utf-8")
    plain = runner.invoke(app, ["--json", "doctor"])

    assert connected is False
    assert damaged.exception is None or isinstance(damaged.exception, SystemExit), repr(
        damaged.exception
    )
    row = _row_named(damaged.stdout, "claude-code")
    assert row["status"] == "warn", row
    if refused:
        assert clicked is not None and clicked.exit_code != 0, "connect refuses it"
        assert f"hooks cannot be written in {claude_home}" in str(row["detail"]), row
        assert str(settings_path) in str(row["detail"]), row
        assert "agents connect" not in str(row["fix"]), row
    else:
        assert "agents connect claude-code" in str(row["fix"]), row
        assert row == _row_named(plain.stdout, "claude-code"), (
            "control: it reads as a file with no hooks"
        )


def test_an_undecodable_sibling_settings_json_costs_doctor_nothing(
    runner: CliRunner, claude_home: Path
) -> None:
    """``_claude_dirs_on_disk`` promises that one unreadable sibling never costs doctor
    its other rows; an undecodable one crashed it."""
    _connect(runner)
    sibling = claude_home.parent / ".claude-account1"
    sibling.mkdir()
    (sibling / "settings.json").write_bytes(b"\xff\xfe")
    skipped = runner.invoke(app, ["--json", "doctor"])
    # Control: the scan does read siblings, so the one above was skipped, not unseen.
    shutil.copyfile(claude_home / "settings.json", sibling / "settings.json")
    seen = runner.invoke(app, ["--json", "doctor"])

    assert skipped.exception is None or isinstance(skipped.exception, SystemExit), repr(
        skipped.exception
    )
    row = _row_named(skipped.stdout, "claude-code")
    assert row["status"] == "ok" and str(sibling) not in str(row["detail"]), row
    assert str(sibling) in str(_row_named(seen.stdout, "claude-code")["detail"])


# --------------------------------------------------------------------------- the refusal


#: The detect-only agents and the release each is planned for. Cursor has none: the
#: release plan schedules Codex (10.1), and a row promising Cursor in 0.10 would be
#: false on 0.10 itself.
_DETECT_ONLY = [("codex", "Codex", "0.10"), ("cursor", "Cursor", None)]


@pytest.mark.parametrize(("name", "label", "planned"), _DETECT_ONLY)
def test_connect_refuses_an_agent_it_has_no_hooks_for(
    runner: CliRunner, isolated_agent_home: Path, name: str, label: str, planned: str | None
) -> None:
    """It exited 0 and wrote the agent into agents.json as connected, installing nothing."""
    agent_dir = isolated_agent_home / f".{name}"
    agent_dir.mkdir(parents=True)

    human = runner.invoke(app, ["agents", "connect", name])
    machine = runner.invoke(app, ["--json", "agents", "connect", name])

    later = f"; support is planned for {planned}" if planned else ""
    assert (human.exit_code, machine.exit_code) == (1, 1)
    assert human.output.strip() == f"✗ aisquare can't connect {label} yet{later}"
    assert ("planned for" in human.output) == (planned is not None), human.output
    assert json.loads(machine.stdout) == {"error": "unsupported_agent", "ref": name}
    assert not paths.aisquare_home().exists(), "a refusal must not build the aisquare home"
    assert list(agent_dir.iterdir()) == [], f"nothing may be written under ~/.{name}"

    (isolated_agent_home / ".claude").mkdir()
    _connect(runner)  # control: the same harness records a connection that is real
    registry = json.loads(paths.agents_registry_path().read_text(encoding="utf-8"))
    assert registry["connected"] == ["claude-code"], registry


def test_an_absent_unsupported_agent_gets_the_same_answer(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saying "not installed" would send someone to install Codex, still unable to connect it."""
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: None)  # absent: not on PATH
    refused = runner.invoke(app, ["agents", "connect", "codex"])
    absent = runner.invoke(app, ["agents", "connect", "claude-code"])

    assert refused.exit_code == 1
    assert "can't connect Codex yet" in refused.output
    assert "not installed" not in refused.output
    assert "not installed" in absent.output, "control: an absent connectable agent says so"


def test_init_reports_the_refusal_as_a_note_and_records_nothing(
    runner: CliRunner, isolated_agent_home: Path
) -> None:
    (isolated_agent_home / ".codex").mkdir(parents=True)

    result = runner.invoke(app, ["--json", "init", "--yes", "--no-onboard", "--agent", "codex"])

    assert result.exit_code == 0, result.output
    notes = json.loads(result.stdout)["notes"]
    assert "Could not connect codex: aisquare can't connect Codex yet" in " ".join(notes), notes
    listed = runner.invoke(app, ["--json", "agents", "list"])
    connected = {agent["name"]: agent["connected"] for agent in json.loads(listed.stdout)}
    assert connected == {"claude-code": False, "cursor": False, "codex": False}, connected


@pytest.mark.parametrize(("name", "label", "planned"), _DETECT_ONLY)
def test_a_record_an_older_aisquare_wrote_is_no_connection(
    runner: CliRunner, claude_home: Path, name: str, label: str, planned: str | None
) -> None:
    """0.7.0's `agents connect codex` exited 0 and recorded the agent in agents.json while
    writing nothing under ~/.codex. On this release `agents list`/`status` and `aisquare
    status` still called it connected, beside a doctor row saying aisquare can't connect it
    and a connect that refuses (review of #257). A record alone is no connection, and
    `agents disconnect` still clears it, with no note about hooks it never had."""
    agent_dir = claude_home.parent / f".{name}"
    agent_dir.mkdir()
    _connect(runner)
    (claude_home / "settings.json").write_text("{}", encoding="utf-8")  # recorded, hooks gone
    registry_path = paths.agents_registry_path()
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["connected"] = sorted([*registry["connected"], name])
    registry["connections"][name] = [str(agent_dir)]  # what 0.7.0's set_connected wrote
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    listed = {
        a["name"]: a for a in json.loads(runner.invoke(app, ["--json", "agents", "list"]).stdout)
    }
    status = json.loads(runner.invoke(app, ["--json", "agents", "status", name]).stdout)[0]
    summary = json.loads(runner.invoke(app, ["--json", "status"]).stdout)
    row = next(row for row in diagnostics._planned_agent_checks() if row.name == name)
    disconnected = runner.invoke(app, ["agents", "disconnect", name])
    after = json.loads(registry_path.read_text(encoding="utf-8"))

    assert (listed[name]["connected"], listed[name]["sites"]) == (False, []), listed[name]
    assert (status["connected"], status["sites"]) == (False, []), status
    assert name not in summary["agents_connected"], summary
    assert f"{label} detected at {agent_dir}, but aisquare can't connect it yet" in row.detail
    claude = listed["claude-code"]
    assert claude["connected"] is True and "claude-code" in summary["agents_connected"], (
        "control: a record of an agent aisquare has hooks for still counts"
    )
    assert claude["sites"][0]["hooks_installed"] is False, "control: and reads as missing"
    assert disconnected.exit_code == 0 and "no aisquare hooks" not in disconnected.output
    assert name not in after["connected"] and not after["connections"].get(name), after
    assert "claude-code" in after["connected"], "disconnecting it leaves claude-code's record"
    assert list(agent_dir.iterdir()) == [], f"nothing is ever written under ~/.{name}"


def test_the_refusal_never_reaches_a_damaged_store(
    runner: CliRunner,
    isolated_agent_home: Path,
    damaged_store: str,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
) -> None:
    """Refused before the store is opened: one JSON object, no traceback, the file untouched."""
    (isolated_agent_home / ".codex").mkdir(parents=True)
    (isolated_agent_home / ".claude").mkdir(parents=True)
    before = paths.db_path().read_bytes()

    refused = runner.invoke(app, ["--json", "agents", "connect", "codex"])

    assert isinstance(refused.exception, SystemExit), repr(refused.exception)
    assert refused.exit_code == 1
    assert json.loads(refused.stdout) == {"error": "unsupported_agent", "ref": "codex"}
    assert paths.db_path().read_bytes() == before

    # Control: a real connect does reach the store. A file that is no database stops
    # it there; a zeroed page its queries never read does not (the shapes differ).
    reached = json.loads(runner.invoke(app, ["--json", "agents", "connect", "claude-code"]).stdout)
    if damaged_store == "at-open":
        assert reached.get("error") == "store_unopenable", reached
    else:
        assert reached.get("name") == "claude-code" and "error" not in reached, reached


def test_connect_names_a_context_file_it_cannot_read(
    runner: CliRunner, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CLAUDE.md saved as Latin-1 was reported as ``not_installed``: an installed Claude
    Code, and asq's Connect button saying it was not, with no file named."""
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: None)  # for "absent"
    claude_md = claude_home / "CLAUDE.md"
    claude_md.write_bytes("# Prefs\ncaf\xe9\n".encode("latin-1"))

    machine = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    human = runner.invoke(app, ["agents", "connect", "claude-code"])
    ingested = paths.aisquare_home().exists()
    shutil.rmtree(claude_home)
    absent = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])

    reason = f"can't read {claude_md}: it is not UTF-8 text"
    assert json.loads(machine.stdout) == {
        "error": "agent_file_unreadable",
        "ref": "claude-code",
        "detail": reason,
    }
    assert human.exit_code == 1 and human.output.strip() == f"✗ {reason}"
    assert not ingested, "refused before anything was ingested or the home built"
    assert json.loads(absent.stdout)["error"] == "not_installed", "control: absent still says so"


def test_a_claude_code_on_path_that_never_started_is_detected_and_offered_connect(
    runner: CliRunner, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agents connect` makes the missing ~/.claude of a Claude Code on PATH (above), but
    detection still keyed on the directory: doctor gave a green "not detected" row with no
    Connect, and `agents status` said detected: false, while Welcome offered Connect
    (review of #257)."""
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    row = diagnostics._check_claude_code()
    listed = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    named = agent_core.detect("claude-code", isolated_agent_home / ".claude-typo")
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: None)
    nowhere = diagnostics._check_claude_code()

    assert row.status is CheckStatus.warn, row
    assert [fix.argv[:3] for fix in fix_commands([row])] == [("agents", "connect", "claude-code")]
    assert listed[0]["detected"] is True, listed
    assert named is not None and not named.detected, "a --config-dir needs its directory"
    assert nowhere.status is CheckStatus.ok, "control: no claude anywhere is not detected"
    assert not (isolated_agent_home / ".claude").exists(), "asking makes nothing"


def test_a_claude_code_on_path_that_never_started_is_connected_not_refused(
    runner: CliRunner, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """npm and Homebrew make ``~/.claude`` only when ``claude`` first runs, and a missing
    directory read as not installed, so `agents connect` and Quickstart 1's `init --agent
    claude-code` refused; only Welcome's own Connect made it (review of #257)."""
    claude = isolated_agent_home / ".claude"
    typo = isolated_agent_home / ".claud"
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: None)
    absent = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    made_without = claude.exists()
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    named = runner.invoke(
        app, ["--json", "agents", "connect", "claude-code", "--config-dir", str(typo)]
    )
    connected = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    hooked = agent_core.hooks_installed("claude-code", claude)
    shutil.rmtree(claude, ignore_errors=True)
    init = runner.invoke(app, ["--json", "init", "--yes", "--no-onboard", "--agent", "claude-code"])

    assert json.loads(absent.stdout)["error"] == "not_installed" and not made_without, "control"
    assert json.loads(named.stdout)["error"] == "not_installed" and not typo.exists(), (
        "a --config-dir is never made: a typo must not get hooks"
    )
    assert connected.exit_code == 0 and hooked, connected.output
    notes = " ".join(json.loads(init.stdout)["notes"])
    assert "Connected claude-code: hooks installed" in notes, notes


def test_a_recorded_claude_dir_that_was_removed_is_made_by_the_doctors_connect(
    runner: CliRunner, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """~/.claude connected and then removed (`rm -rf ~/.claude` resets Claude Code), with
    `claude` still on PATH: the doctor's Connect names the recorded directory with
    --config-dir, and connect refused it as not installed, while the bare Connect, Welcome's,
    made it and connected (review of #257). Named or not, that directory is made."""
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    claude = isolated_agent_home / ".claude"
    claude.mkdir(parents=True)
    _connect(runner)
    shutil.rmtree(claude)

    row = diagnostics._check_claude_code()
    buttons = [fix.argv for fix in fix_commands([row])]
    clicked = runner.invoke(app, ["--json", *buttons[0]]) if buttons else None
    after = diagnostics._check_claude_code()
    other = isolated_agent_home / ".claude-gone"
    elsewhere = runner.invoke(
        app, ["--json", "agents", "connect", "claude-code", "--config-dir", str(other)]
    )

    assert buttons == [("agents", "connect", "claude-code", "--config-dir", str(claude))], row
    assert clicked is not None and clicked.exit_code == 0, clicked and clicked.output
    assert agent_core.hooks_installed("claude-code", claude), "the button made it and connected"
    assert after.status is CheckStatus.ok, after
    assert json.loads(elsewhere.stdout)["error"] == "not_installed" and not other.exists(), (
        "control: a --config-dir naming another directory is never made"
    )


def test_a_config_dir_that_is_a_symlink_loop_is_not_installed_not_a_traceback(
    runner: CliRunner, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connect asks whether a --config-dir is the directory a session from this shell reads
    (above), and pathlib raises RuntimeError for a symlink loop on 3.11 and 3.12: a
    traceback where connect said "not installed" before."""
    if os.name == "nt":
        pytest.skip("a symlink loop is a POSIX shape; NTFS links need a privilege")
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    isolated_agent_home.mkdir(parents=True, exist_ok=True)
    loop = isolated_agent_home / "loop"
    loop.symlink_to(loop)

    result = runner.invoke(
        app, ["--json", "agents", "connect", "claude-code", "--config-dir", str(loop)]
    )

    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert json.loads(result.stdout)["error"] == "not_installed", result.stdout
    assert not (isolated_agent_home / ".claude").exists(), "nothing made for another dir"


@pytest.mark.parametrize("shape", ["another profile", "no claude on PATH"])
def test_a_recorded_dir_connect_cannot_make_is_named_gone_with_the_way_to_forget_it(
    runner: CliRunner, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """A recorded config dir that was removed, and that connect will not make (any but the
    one a session from this shell reads, or ~/.claude with no `claude` on PATH): the row
    graded it "hooks are missing or outdated", which the installer answers with a sign-in,
    with a --config-dir Connect that could only say not installed, and `agents status`
    called it missing (review of #257). Named as gone, with the disconnect that forgets
    it, everywhere."""
    from aisquare.cli.common import _hook_sites

    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    claude = isolated_agent_home / ".claude"
    claude.mkdir(parents=True)
    _connect(runner)
    gone = isolated_agent_home / ".claude-c2" if shape == "another profile" else claude
    if gone != claude:
        gone.mkdir()
        _connect(runner, gone)
    else:
        monkeypatch.setattr(agent_core, "claude_on_path", lambda: None)
    kept = isolated_agent_home / ".claude-c3"  # the control: recorded, and still there
    kept.mkdir()
    _connect(runner, kept)
    (kept / "settings.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(gone)

    row = diagnostics._check_claude_code()
    buttons = [fix.argv for fix in fix_commands([row])]
    status = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    sites = {site["config_dir"]: site for site in status[0]["sites"]}
    cell = _hook_sites(agents_service.status("claude-code")[0])
    clicked = runner.invoke(app, ["agents", "connect", "claude-code", "--config-dir", str(gone)])
    forget = runner.invoke(app, ["agents", "disconnect", "claude-code", "--config-dir", str(gone)])
    after = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)

    reason = f"{gone}: {_stat_error(gone)}"
    assert f"hooks cannot be written in {gone}: {reason}" in row.detail, row
    assert f"{diagnostics._STALE_HOOKS} in: {kept}" in row.detail, "only kept is missing"
    # Forgotten as it is, or, for the directory this shell reads, with the variable moved.
    assert f"aisquare agents disconnect claude-code --config-dir {gone}" in str(row.fix), row.fix
    connect = ("agents", "connect", "claude-code", "--config-dir")
    assert (*connect, str(gone)) not in buttons, buttons
    assert (*connect, str(kept)) in buttons, "control: a recorded dir still there keeps Connect"
    assert (sites[str(gone)]["refused"], sites[str(kept)]["refused"]) == (reason, None), sites
    assert f"cannot be written in {gone}: {reason}" in cell, cell
    assert clicked.exit_code == 1 and clicked.output.strip() == f"✗ {reason}", clicked.output
    assert forget.exit_code == 0 and "no aisquare hooks found" not in forget.output, forget.output
    assert str(gone) not in {site["config_dir"] for site in after[0]["sites"]}, "forgotten"


def test_a_recorded_dir_that_became_a_symlink_loop_is_gone_to_every_reader(
    runner: CliRunner, claude_home: Path
) -> None:
    """A recorded profile replaced by a link to itself: the doctor said it does not exist
    and to forget it with disconnect, while disconnect and uninstall read it as a
    settings.json that could not be read, and disconnect refused the doctor's own fix
    (review of #257). One notion of "nothing there" for every reader."""
    from aisquare.services import lifecycle

    if os.name == "nt":
        pytest.skip("a symlink loop is a POSIX shape; NTFS links need a privilege")
    _connect(runner)
    loop = claude_home.parent / ".claude-c2"
    loop.mkdir()
    _connect(runner, loop)
    shutil.rmtree(loop)
    loop.symlink_to(loop.name)

    row = diagnostics._check_claude_code()
    stuck = [site.config_dir for site in lifecycle.uninstall_plan().unreadable]
    forget = runner.invoke(app, ["agents", "disconnect", "claude-code", "--config-dir", str(loop)])

    reason = f"{loop} is a link to {loop.name}: {_stat_error(loop)}"
    assert f"hooks cannot be written in {loop}: {reason}" in row.detail, row
    fix = f"forget it: aisquare agents disconnect claude-code --config-dir {loop}"
    assert fix in str(row.fix), row.fix
    assert loop not in stuck, "uninstall reads it as the doctor does: nothing there"
    assert forget.exit_code == 0, forget.output
    assert [str(p) for p in agent_core.connected_dirs("claude-code")] == [str(claude_home)]


@pytest.mark.parametrize("shape", ["no other site", "beside a connected ~/.claude"])
def test_an_exported_config_dir_in_a_home_this_machine_lacks_is_never_offered_connect(
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """`CLAUDE_CONFIG_DIR='~olduser/.claude'` exported for a user this machine does not have,
    with `claude` on PATH: the doctor offered the bare Connect for a directory "Claude Code
    has not made yet", Welcome showed Connect, and the click refused only after building
    ~/.aisquare (review of #257). Named everywhere, before anything is written."""
    if os.name == "nt":
        pytest.skip("Windows guesses a ~user's home instead of failing to expand it")
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    if shape == "beside a connected ~/.claude":
        (isolated_agent_home / ".claude").mkdir(parents=True)
        _connect(runner)
    built = paths.aisquare_home().exists()
    homeless = "~aisquare-no-such-user/.claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", homeless)

    row = diagnostics._check_claude_code()
    welcome = first_run.probe_claude(sign_in=False, which=lambda _name: None)
    clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])

    reason = f"can't write {homeless}/settings.json: no such home on this machine"
    assert f"hooks cannot be written in {homeless}: {reason}" in row.detail, row
    assert [f.argv for f in fix_commands([row]) if f.argv[:2] == ("agents", "connect")] == []
    refusal = agents_service.access("claude-code").connect
    assert row.fix == f"{_repair(refusal)}; or {_REPOINT_FIX}", row.fix
    assert welcome.refused == reason, welcome
    assert json.loads(clicked.stdout)["detail"] == reason, clicked.stdout
    assert paths.aisquare_home().exists() == built, "a refusal builds no aisquare home"
    assert list(work.iterdir()) == [], "nothing is made in the cwd"
    import dataclasses

    from aisquare.cli.ui.views.welcome import claude_text

    found = dataclasses.replace(welcome, binary="/opt/homebrew/bin/claude")
    assert refusal is not None
    assert _step_two(row.fix) in claude_text(found, platform="linux").plain
    # The variable's remedy, done as worded in a shell started again, lets connect write,
    # and the row no longer names the home this machine lacks.
    writable = tmp_path / "writable"
    writable.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(writable))
    repointed = runner.invoke(app, ["agents", "connect", "claude-code"])
    after = diagnostics._check_claude_code()
    assert repointed.exit_code == 0, repointed.output
    assert homeless not in after.detail and after.status is CheckStatus.ok, after


#: Config dirs connect cannot make. "the default dir" is ~/.claude with CLAUDE_CONFIG_DIR
#: unset; every other is named by the variable.
_CANNOT_MAKE = [
    "the default dir, recorded, now a loop",
    "the default dir, a dangling link",
    "the default dir, in a HOME this user may not write",
    "a profile, recorded, now a dangling link",
    "a profile under a parent this user may not write",
    "that profile beside a connected ~/.claude",
    "a profile under a link that leads nowhere",
    "a profile under a link to a file",
    "a profile under a link to a folder this user may not write",
    "a profile under a chain of links that leads nowhere",
    "a profile under a file",
    "a profile under a loop",
]


def _cannot_make_shape(shape: str, home: Path, tmp_path: Path, runner: CliRunner) -> Path:
    """Build ``shape`` and return the path that blocks the mkdir."""
    claude = home / ".claude"
    if shape.startswith("the default dir"):
        if "recorded" in shape:
            claude.mkdir(parents=True)
            _connect(runner)
            shutil.rmtree(claude)
            claude.symlink_to(claude.name)
            return claude
        home.mkdir(parents=True)
        if "HOME" in shape:
            home.chmod(0o555)
            return home
        claude.symlink_to(home / "dotfiles" / "claude")
        return claude
    if "recorded" in shape:
        profile = home / ".claude-work"
        profile.mkdir(parents=True)
        _connect(runner, profile)
        shutil.rmtree(profile)
        profile.symlink_to(home / "gone" / "work")
        return profile
    if "may not write" in shape or "beside" in shape:
        if "beside" in shape:
            claude.mkdir(parents=True)
            _connect(runner)
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        if "link" not in shape:
            return locked
        home.mkdir(parents=True)
        (home / "dots").symlink_to(locked)
        return home / "dots"
    home.mkdir(parents=True)
    blocking = home / "dots"
    if "a file" in shape and "link" not in shape:
        blocking.write_text("not a directory", encoding="utf-8")
    elif "link to a file" in shape:
        (home / "afile").write_text("not a directory", encoding="utf-8")
        blocking.symlink_to(home / "afile")
    elif "chain" in shape:
        (home / "hop").symlink_to(home / "missing")
        blocking.symlink_to(home / "hop")
    else:
        blocking.symlink_to(blocking.name if "loop" in shape else home / "mnt" / "dots")
    return blocking


@pytest.mark.parametrize("shape", _CANNOT_MAKE)
def test_a_config_dir_connect_cannot_make_is_named_everywhere_and_never_offered_connect(
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """With `claude` on PATH, connect first makes the directory a session from this shell
    reads. A link there or above it that leads nowhere, a loop, a file in the way, or a
    parent this user may not write failed that mkdir while the doctor and Welcome offered
    Connect; then the refusal named nothing ("no directory it can be made in") and its
    "create it yourself" failed too; a link to a file was said to lead to nothing; and a
    HOME this user may not write was told to become writable, with no other way (review
    of #257). The path that blocks is named with what the operating system says of it,
    and each remedy printed, done as worded, lets connect make it and clears the row:
    repairing that path, or CLAUDE_CONFIG_DIR, with the disconnect that forgets a
    recorded one."""
    if os.name == "nt":
        pytest.skip("links, loops and mode 555 are POSIX shapes here")
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    home = isolated_agent_home
    try:
        blocking = _cannot_make_shape(shape, home, tmp_path, runner)
        if "may not" in shape and os.access(blocking, os.W_OK):
            pytest.skip("this user can write a mode-555 directory (root)")
        named = not shape.startswith("the default dir")
        where = home / ".claude" if not named else blocking / "claude"
        if "recorded" in shape:
            where = blocking
        if named:
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(where))
        built = paths.db_path().exists()
        row = diagnostics._check_claude_code()
        welcome = first_run.probe_claude(sign_in=False, which=lambda _name: "/opt/claude")
        clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
        made = os.path.isdir(where) or paths.db_path().exists() != built
    finally:
        for folder in (home, tmp_path / "locked"):
            if folder.is_dir():
                folder.chmod(0o755)
    from aisquare.cli.ui.views.welcome import claude_text

    step_two = claude_text(welcome, platform="linux").plain
    if os.path.isdir(blocking):  # a directory, or a link to one
        fact = f"this user may not create anything in {blocking}"
    elif os.path.islink(blocking):
        leads_to = os.readlink(blocking)
        fact = (
            f"{blocking} is a link to {leads_to}, which is not a directory"
            if "link to a file" in shape
            else f"{blocking} is a link to {leads_to}: {_stat_error(blocking)}"
        )
    else:
        fact = f"{blocking} is not a directory"
    repoint = _REPOINT_FIX
    if "recorded" in shape:
        repoint += f", and disconnect this one: {_DISCONNECT} --config-dir {where}"

    reason = str(json.loads(clicked.stdout)["detail"])
    assert clicked.exit_code == 1 and reason == f"can't create {where}: {fact}", reason
    assert f"hooks cannot be written in {where}: {reason}" in row.detail, row
    assert [f.argv for f in fix_commands([row]) if f.argv[:2] == ("agents", "connect")] == []
    # A file where a directory must be is never offered a repair, which would destroy it.
    a_file = not (os.path.isdir(blocking) or os.path.islink(blocking))
    repair = None if a_file else f"repair {blocking} ({fact}), then connect again"
    assert row.fix == "; or ".join(filter(None, [repair, repoint])), row.fix
    assert welcome.refused == reason, welcome
    assert _step_two(row.fix) in step_two, "Welcome gives the doctor's remedies"
    assert not made, "nothing written before the refusal"
    # Each remedy, done as worded (the variable's in a shell started again), connects and
    # clears the row.
    named_env = os.environ.get("CLAUDE_CONFIG_DIR")
    writable = tmp_path / "writable"
    writable.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(writable))
    done = {"repoint": [runner.invoke(app, argv) for argv in _commands(repoint)]}
    done["repoint"].append(runner.invoke(app, ["agents", "connect", "claude-code"]))
    rows = {"repoint": diagnostics._check_claude_code()}
    if named_env is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", named_env)
    if os.path.isdir(blocking):  # repaired: one this user may write
        os.chmod(blocking, 0o755)
    elif not a_file:  # repaired: a directory where the link was
        blocking.unlink()
        blocking.mkdir()
    if repair is not None:
        done["repair"] = [runner.invoke(app, ["agents", "connect", "claude-code"])]
        rows["repair"] = diagnostics._check_claude_code()
    else:
        assert blocking.read_text(encoding="utf-8") == "not a directory", "the file is kept"
    codes = {words: [result.exit_code for result in results] for words, results in done.items()}
    assert codes == {words: [0] * len(results) for words, results in done.items()}, {
        words: [result.output for result in results] for words, results in done.items()
    }
    assert all("cannot be written" not in row.detail for row in rows.values()), rows


def _commands(fix: str) -> list[list[str]]:
    """The aisquare commands a remedy names, as argv."""
    return [
        command.split()
        for command in re.findall(r"aisquare (agents \w+ claude-code --config-dir \S+)", fix)
    ]


@pytest.mark.parametrize(
    "folder", ["missing", "read-only", "a loop", "a loop, in a folder this user may not write"]
)
def test_a_settings_json_linked_nowhere_is_refused_first_with_a_remedy_that_works(
    runner: CliRunner,
    claude_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder: str,
) -> None:
    """A settings.json linked into a dotfiles folder that moved: access() let connect
    through, and connect saved CLAUDE.md into the store and built ~/.aisquare before its
    write failed. Then its remedy, "make the folder it points into", could not work where
    that folder is there but read-only, nor for a link in a loop, and a loop in a folder
    this user may not write got one remedy, which could not be done (review of #257).
    Refused first, with what the operating system says of the folder, and each remedy
    printed, done as worded, works: repairing the file, and CLAUDE_CONFIG_DIR for the
    directory sessions from this shell read."""
    if os.name == "nt":
        pytest.skip("links to missing or read-only folders are POSIX shapes here")
    (claude_home / "CLAUDE.md").write_text("# Prefs\nuse tabs\n", encoding="utf-8")
    settings = claude_home / "settings.json"
    target = tmp_path / "dotfiles" / "claude" / "settings.json"
    locked = claude_home if "may not write" in folder else target.parent
    if folder.startswith("a loop"):
        settings.symlink_to(settings.name)
    else:
        settings.symlink_to(target)
    if folder == "read-only" or "may not write" in folder:
        locked.mkdir(parents=True, exist_ok=True)
        locked.chmod(0o555)
    try:
        if os.access(locked, os.W_OK) and locked.exists():
            pytest.skip("this user can write a mode-555 directory (root)")
        refusal = agents_service.access("claude-code").connect
        row = diagnostics._check_claude_code()
        clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
        built = paths.aisquare_home().exists()
    finally:
        if locked.exists():
            locked.chmod(0o755)

    reason = str(json.loads(clicked.stdout)["detail"])
    assert refusal is not None and refusal.why == reason, refusal
    assert f"hooks cannot be written in {claude_home}: {reason}" in row.detail, row
    if folder == "missing":
        fact = f"it is a link to {target}, and {target.parent}: {_stat_error(target.parent)}"
    elif folder == "read-only":
        fact = f"it is a link to {target}, and this user may not create anything in {target.parent}"
    else:
        fact = _stat_error(settings)
    verb = "read" if folder.startswith("a loop") else "write"
    assert reason == f"can't {verb} {settings}: {fact}", reason
    assert row.fix == f"repair {settings} ({fact}), then connect again; or {_REPOINT_FIX}", row.fix
    assert not built, "refused before the store was built or CLAUDE.md saved"
    writable = tmp_path / "writable"
    writable.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(writable))
    done = {"repoint": runner.invoke(app, ["agents", "connect", "claude-code"])}
    rows = {"repoint": diagnostics._check_claude_code()}
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    # Repaired: the read-only folders were made writable again above, after the probe.
    if folder == "missing":  # repaired: the folder it points into is made
        target.parent.mkdir(parents=True)
    elif folder.startswith("a loop"):  # repaired: a JSON object where the loop was
        settings.unlink()
        settings.write_text("{}", encoding="utf-8")
    done["repair"] = runner.invoke(app, ["agents", "connect", "claude-code"])
    rows["repair"] = diagnostics._check_claude_code()
    assert {words: result.exit_code for words, result in done.items()} == dict.fromkeys(done, 0), {
        words: result.output for words, result in done.items()
    }
    assert agent_core.hooks_installed("claude-code"), "written where it was refused"
    assert all("cannot be written" not in row.detail for row in rows.values()), rows


_BESIDE_NO_CLAUDE = "beside a connected ~/.claude, no claude on PATH"
_NOT_A_DIRECTORY_TO_READ = [
    pytest.param("Claude Code's state file", id="state file"),
    pytest.param("Claude Code's state file, beside a connected ~/.claude", id="state file beside"),
    pytest.param(f"Claude Code's state file, {_BESIDE_NO_CLAUDE}", id="state file, no claude"),
    pytest.param("in a folder this user cannot enter", id="unenterable", marks=_NEEDS_DENIED_READS),
    pytest.param(
        f"in a folder this user cannot enter, {_BESIDE_NO_CLAUDE}",
        id="unenterable, no claude",
        marks=_NEEDS_DENIED_READS,
    ),
    pytest.param(
        f"in a folder this user cannot enter, nothing beneath, {_BESIDE_NO_CLAUDE}",
        id="unenterable, empty, no claude",
        marks=_NEEDS_DENIED_READS,
    ),
    pytest.param(f"a link to nothing, {_BESIDE_NO_CLAUDE}", id="dangling, no claude"),
    pytest.param(f"a link loop, {_BESIDE_NO_CLAUDE}", id="loop, no claude"),
    pytest.param(f"under a file, {_BESIDE_NO_CLAUDE}", id="under a file, no claude"),
    pytest.param(f"under a link to nothing, {_BESIDE_NO_CLAUDE}", id="under a link, no claude"),
    pytest.param(
        f"under a chain of links to nothing, {_BESIDE_NO_CLAUDE}", id="under a chain, no claude"
    ),
]


@pytest.mark.parametrize("shape", _NOT_A_DIRECTORY_TO_READ)
def test_a_config_dir_variable_naming_no_directory_to_read_is_named_on_any_path(
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """`CLAUDE_CONFIG_DIR=~/.claude.json`, a slip for ~/.claude that names Claude Code's own
    state file, a directory in a folder this user cannot enter, a link to nothing or in a
    loop, or one under a file: the remedy named the settings.json inside it, which for
    ~/.claude.json meant destroying that file; beside a connected ~/.claude the row was
    green, and with no `claude` on the doctor's PATH it still was; `doctor --json` printed
    nothing and `uninstall --dry-run` ended in a traceback in the folder this user cannot
    enter (review of #257). Named on any PATH, with the first path that blocks and what
    the operating system says of it; a file there is never offered a repair. Each remedy,
    done as worded, lets connect write and clears the row, and the file is left as it was."""
    if os.name == "nt" and "link" in shape:
        pytest.skip("links need a privilege on Windows")
    on_path = None if "no claude" in shape else "/opt/homebrew/bin/claude"
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: on_path)
    home = isolated_agent_home
    home.mkdir(parents=True)
    if "beside" in shape:
        (home / ".claude").mkdir()
        _connect(runner)
    locked = home / "locked"
    if "enter" in shape:
        where, blocking = locked / "claude", locked
        (where if "nothing beneath" not in shape else locked).mkdir(parents=True)
    elif "state file" in shape:
        where = blocking = home / ".claude.json"
        where.write_text('{"numStartups": 7}', encoding="utf-8")
    elif "under a file" in shape:
        blocking = home / "notes"
        blocking.write_text('{"numStartups": 7}', encoding="utf-8")
        where = blocking / "claude"
    elif shape.startswith("under a"):  # a link to a folder that is not there
        blocking = home / "link"
        if "chain" in shape:
            (home / "hop").symlink_to(home / "gone")
        blocking.symlink_to(home / ("hop" if "chain" in shape else "gone"))
        where = blocking / "claude"
    else:
        where = blocking = home / ".claude-work"
        where.symlink_to(where.name if "loop" in shape else home / "gone" / "claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(where))
    if "enter" in shape:
        locked.chmod(0)
    try:
        built = paths.db_path().exists()
        row = diagnostics._check_claude_code()
        welcome = first_run.probe_claude(sign_in=False, which=lambda _name: "/opt/claude")
        clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
        made = paths.db_path().exists() != built
        doctor = runner.invoke(app, ["--json", "doctor"])
        uninstall = runner.invoke(app, ["--json", "uninstall", "--dry-run"])
        # With no `claude` on PATH and nothing Claude Code made there, connect says it is
        # not installed; the doctor names what stands in the way all the same.
        unmade = on_path is None and not agent_core.present(where)
        if "enter" in shape:
            fact = f"this user may not enter {locked}"
        elif os.path.islink(blocking):
            fact = f"{blocking} is a link to {os.readlink(blocking)}: {_stat_error(blocking)}"
        else:
            fact = f"{blocking} is not a directory"
    finally:
        if locked.exists():
            locked.chmod(0o755)
    from aisquare.cli.ui.views.welcome import claude_text

    a_file = os.path.isfile(blocking) and not os.path.islink(blocking)
    # A folder on the way repaired, connect with no `claude` on PATH makes nothing there.
    there = f" so that {where} is there" if blocking != where and on_path is None else ""
    repair = None if a_file else f"repair {blocking} ({fact}){there}, then connect again"
    clicked_said = json.loads(clicked.stdout)
    assert clicked.exit_code == 1, clicked.stdout
    if unmade:
        assert clicked_said["error"] == "not_installed", clicked_said
    else:
        assert clicked_said["detail"] == welcome.refused, (clicked_said, welcome)
    assert f"hooks cannot be written in {where}: {welcome.refused}" in row.detail, row
    assert row.fix == "; or ".join(filter(None, [repair, _REPOINT_FIX])), row.fix
    assert fix_commands([row]) == [], "no Connect: the click could only fail"
    step_two = claude_text(welcome, platform="linux").plain
    assert _step_two(row.fix) in step_two, "Welcome gives the doctor's remedies"
    assert not made, "nothing written before the refusal"
    for result in (doctor, uninstall):
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert result.exception is None or isinstance(result.exception, SystemExit), repr(
            result.exception
        )
        assert len(lines) == 1 and json.loads(lines[0]), result.stdout
    writable = tmp_path / "writable"
    writable.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(writable))
    done = {"repoint": runner.invoke(app, ["agents", "connect", "claude-code"])}
    rows = {"repoint": diagnostics._check_claude_code()}
    if repair is not None:  # repaired as worded: only the path it names
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(where))
        if "enter" in shape:
            locked.chmod(0o755)  # already, above: a folder this user may enter
            where.mkdir(exist_ok=True)  # so that it is there
        elif "loop" in shape:
            blocking.unlink()
            blocking.mkdir()
        else:
            (home / "gone" / "claude").mkdir(parents=True)  # the link leads to it there
        done["repair"] = runner.invoke(app, ["agents", "connect", "claude-code"])
        rows["repair"] = diagnostics._check_claude_code()
    assert {words: result.exit_code for words, result in done.items()} == dict.fromkeys(done, 0), {
        words: result.output for words, result in done.items()
    }
    assert all("cannot be written" not in row.detail for row in rows.values()), rows
    if a_file:
        assert blocking.read_text(encoding="utf-8") == '{"numStartups": 7}', "left as it was"


def test_a_link_loop_windows_reports_as_einval_is_named_in_its_own_words(
    isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows reports a link loop as winerror 1921 with errno EINVAL, not ELOOP, so a loop
    above CLAUDE_CONFIG_DIR read as a link to something "which does not exist" (review of
    #257). Named with what the operating system said, whatever errno it chose."""
    if os.name == "nt":
        pytest.skip("links need a privilege on Windows; its error is played here")
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    home = isolated_agent_home
    home.mkdir(parents=True)
    loop = home / "loop"
    loop.symlink_to(loop.name)
    where = loop / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(where))
    said = "The name of the file cannot be resolved by the system"
    real = os.stat

    def windows_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if os.fspath(path).startswith(str(loop)):
            exc = OSError(errno.EINVAL, said, os.fspath(path))
            setattr(exc, "winerror", 1921)  # noqa: B010 - not an attribute off Windows
            raise exc
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", windows_stat)
    refusal = agents_service.access("claude-code").connect
    row = diagnostics._check_claude_code()

    assert refusal is not None, "connect cannot make it"
    assert refusal.why == f"can't create {where}: {loop} is a link to loop: {said}", refusal
    assert (refusal.path, refusal.fact) == (loop, f"{loop} is a link to loop: {said}"), refusal
    assert row.fix == f"repair {loop} ({refusal.fact}), then connect again; or {_REPOINT_FIX}"


def test_a_settings_json_inside_a_file_is_never_called_writable(tmp_path: Path) -> None:
    """Asked about a settings.json that is not there, the rule asked whether its folder could
    be written, and a folder that is a file (`CLAUDE_CONFIG_DIR=~/.claude.json`) could: on
    Windows, where reading it says only that it is not there, connect was let through to a
    write that failed after the context was saved (review of #257)."""
    state = tmp_path / ".claude.json"
    state.write_text("{}", encoding="utf-8")
    folder = tmp_path / "claude"
    folder.mkdir()

    assert agents_service.settings_unwritable(state / "settings.json") == (
        f"{state} is not a directory"
    )
    assert agents_service.settings_unwritable(folder / "settings.json") is None, "control"
    assert agents_service.settings_unwritable(tmp_path / "gone" / "settings.json") is None, (
        "a folder that is not there is made first"
    )


_THIS_SHELLS_DIR = [
    "the variable names ~/.claude, whose settings.json is not an object",
    "the variable names ~/.claude, in a HOME this user may not write",
    "a recorded ~/.claude, its hooks taken out by hand, its CLAUDE.md not UTF-8",
    "a recorded ~/.claude, its settings.json not valid JSON",
    "the variable names a folder outside ~/.claude*, read-only, its hooks pinning a lost program",
    "the variable names a ~/.claude-work this home never connected, its hooks pinning a lost "
    "program, its CLAUDE.md not UTF-8",
]


@pytest.mark.parametrize("shape", _THIS_SHELLS_DIR)
def test_the_variables_remedy_is_never_unset_and_done_as_worded_clears_the_row(
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """With CLAUDE_CONFIG_DIR naming ~/.claude (Claude Code's reference devcontainer
    exports it), the remedy offered first was "or unset it", which led sessions to the
    same refused directory. For a recorded ~/.claude, pointing the variable elsewhere was
    offered, and done, the row still named it: the doctor grades a recorded directory
    whatever the variable says; and for one outside ~/.claude* holding aisquare's hooks,
    which it grades only as the variable's, it was dropped (review of #257). The
    variable's remedy never says unset and never comes first; for one the doctor grades
    whatever the variable says (recorded, or a ~/.claude* holding aisquare's hooks) it
    comes with the disconnect that takes it out, and only where that disconnect would
    work. Each remedy printed, done as worded, lets connect write and clears the row."""
    if os.name == "nt" and "may not write" in shape:
        pytest.skip("mode 555 denies nothing on NTFS")
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    home = isolated_agent_home
    claude = home / ".claude"
    home.mkdir(parents=True)
    if shape.startswith("the variable"):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    if "not an object" in shape:
        claude.mkdir()
        (claude / "settings.json").write_text("[1]", encoding="utf-8")
    elif "may not write" in shape:
        home.chmod(0o555)
    elif "lost program" in shape:
        claude.mkdir()
        _connect(runner)
        claude = home / ("work/claude" if "outside" in shape else ".claude-work")
        claude.mkdir(parents=True)
        shutil.copy(home / ".claude" / "settings.json", claude / "settings.json")
        _hooks_run(claude / "settings.json", str(tmp_path / "old" / "bin" / "aisquare"))
        if "outside" in shape:
            (claude / "settings.json").chmod(0o444)
        else:
            _utf16(claude / "CLAUDE.md")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    else:
        claude.mkdir()
        _connect(runner)
        if "CLAUDE.md" in shape:
            (claude / "settings.json").write_text("{}", encoding="utf-8")
            _utf16(claude / "CLAUDE.md")
        else:
            text = (claude / "settings.json").read_text(encoding="utf-8")
            (claude / "settings.json").write_text(text.rstrip()[:-1] + ",}", encoding="utf-8")
    try:
        if "may not write" in shape and os.access(home, os.W_OK):
            pytest.skip("this user can write a mode-555 directory (root)")
        refusal = agents_service.access("claude-code").connect
        row = diagnostics._check_claude_code()
    finally:
        home.chmod(0o755)
    if refusal is not None and "may not write" in shape:
        home.chmod(0o555)

    fixes = [_repair(refusal)]
    if "outside" in shape:
        generated = "or point its hooks at this install where that file is generated"
        fixes = [f"{fixes[0]}, {generated}"]
    if "recorded" not in shape and "never connected" not in shape:
        fixes.append(_REPOINT_FIX)
    elif "CLAUDE.md" in shape:  # graded anyway; disconnect would take it out
        fixes.append(
            f"{_REPOINT_FIX}, and disconnect this one: {_DISCONNECT} --config-dir {claude}"
        )
    assert row.fix == "; or ".join(fixes), row.fix
    assert "unset" not in str(row.fix), row.fix
    named = os.environ.get("CLAUDE_CONFIG_DIR")
    done: dict[str, list[Any]] = {}
    rows: dict[str, DoctorCheck] = {}
    if len(fixes) > 1:  # the variable's, in a shell started again
        writable = tmp_path / "writable"
        writable.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(writable))
        done["repoint"] = [runner.invoke(app, argv) for argv in _commands(fixes[1])]
        done["repoint"].append(runner.invoke(app, ["agents", "connect", "claude-code"]))
        rows["repoint"] = diagnostics._check_claude_code()
        if named is None:
            monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        else:
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", named)
    if "may not write" in shape:  # repaired: a HOME this user may write
        home.chmod(0o755)
    elif "outside" in shape:  # repaired: a settings.json this user may write
        (claude / "settings.json").chmod(0o644)
    elif "CLAUDE.md" in shape:
        (claude / "CLAUDE.md").write_text("# Prefs\n", encoding="utf-8")
    else:
        (claude / "settings.json").write_text("{}", encoding="utf-8")
    done["repair"] = [runner.invoke(app, ["agents", "connect", "claude-code"])]
    rows["repair"] = diagnostics._check_claude_code()
    codes = {words: [result.exit_code for result in results] for words, results in done.items()}
    assert codes == {words: [0] * len(results) for words, results in done.items()}, {
        words: [result.output for result in results] for words, results in done.items()
    }
    assert all("cannot be written" not in row.detail for row in rows.values()), rows


def test_a_recorded_profile_under_a_link_to_nothing_is_repaired_as_worded(
    runner: CliRunner, claude_home: Path, tmp_path: Path
) -> None:
    """A profile connected with `--config-dir` through a link into a dotfiles volume that
    is no longer mounted: the doctor said to repair the link and connect again, and done
    as worded (the link leading to a folder again) connect still refused, since it never
    makes a --config-dir (review of #257). The repair says what must be there again, and
    each remedy, done as worded, clears the row."""
    if os.name == "nt":
        pytest.skip("links need a privilege on Windows")
    _connect(runner)
    volume = claude_home.parent / "dotfiles"
    (volume / "claude").mkdir(parents=True)
    link = claude_home.parent / "dots"
    link.symlink_to(volume)
    profile = link / "claude"
    _connect(runner, profile)
    shutil.rmtree(volume)

    row = diagnostics._check_claude_code()
    fact = f"{link} is a link to {volume}: {_stat_error(link)}"
    forget = f"forget it: {_DISCONNECT} --config-dir {profile}"
    assert f"hooks cannot be written in {profile}: " in row.detail, row
    assert row.fix == (
        f"repair {link} ({fact}) so that {profile} is there, then connect again; or {forget}"
    ), row.fix
    forgot = runner.invoke(app, _commands(str(row.fix))[0])
    rows = [diagnostics._check_claude_code()]
    (volume / "claude").mkdir(parents=True)  # repaired as worded: the link leads to it there
    connected = runner.invoke(
        app, ["agents", "connect", "claude-code", "--config-dir", str(profile)]
    )
    rows.append(diagnostics._check_claude_code())
    assert (forgot.exit_code, connected.exit_code) == (0, 0), (forgot.output, connected.output)
    assert all(r.status is CheckStatus.ok for r in rows), rows


@_NEEDS_DENIED_READS
def test_a_recorded_profile_gone_behind_a_folder_this_user_cannot_enter_is_repaired_as_worded(
    runner: CliRunner, claude_home: Path
) -> None:
    """A profile connected with `--config-dir`, then removed, in a folder later made mode
    000: "repair the folder, then connect again" said nothing of the profile, which an
    existence check behind that folder read as there; done as worded, connect still
    refused (delta review 8 of #257). The repair says it must be there again."""
    _connect(runner)
    volume = claude_home.parent / "vol"
    profile = volume / "claude"
    profile.mkdir(parents=True)
    _connect(runner, profile)
    shutil.rmtree(profile)
    volume.chmod(0)
    try:
        row = diagnostics._check_claude_code()
    finally:
        volume.chmod(0o755)
    profile.mkdir()  # repaired as worded: the folder entered, the profile there again
    connected = runner.invoke(
        app, ["agents", "connect", "claude-code", "--config-dir", str(profile)]
    )

    repair = f"repair {volume} (this user may not enter {volume}) so that {profile} is there"
    assert row.fix == f"{repair}, then connect again", row.fix
    assert connected.exit_code == 0, connected.output
    assert diagnostics._check_claude_code().status is CheckStatus.ok


@pytest.mark.parametrize("program", ["this install", "a lost program"])
def test_the_doctor_welcome_and_agents_list_give_one_remedy_list_for_0_7_0_hooks_read_only(
    runner: CliRunner,
    claude_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    program: str,
) -> None:
    """A recorded ~/.claude whose read-only settings.json holds 0.7.0's five hooks (no
    StopFailure): the doctor withholds pointing CLAUDE_CONFIG_DIR elsewhere, since
    disconnect cannot take those hooks out, while Welcome step 2 offered it, and followed,
    step 2 went green while the row never cleared (review of #257). One list for every
    surface (``agents.remedies``); done as worded, it clears both. Pinned at a program
    that is gone, only the doctor's row says so (which program hooks run takes starting
    it to know), and only it adds the remedy for that, where the file is generated."""
    from aisquare.cli.ui.views.welcome import claude_text

    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    _connect(runner)
    settings = claude_home / "settings.json"
    if program == "a lost program":
        _hooks_run(
            settings, str(tmp_path / "nix" / "store" / "aisquare-0.7.0" / "bin" / "aisquare")
        )
    data = json.loads(settings.read_text(encoding="utf-8"))
    del data["hooks"]["StopFailure"]
    settings.write_text(json.dumps(data), encoding="utf-8")
    settings.chmod(0o444)
    try:
        if os.access(settings, os.W_OK):
            pytest.skip("this user can write a read-only file (root)")
        row = diagnostics._check_claude_code()
        welcome = first_run.probe_claude(sign_in=False, which=lambda _name: "/opt/claude")
        listed = json.loads(
            runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout
        )
    finally:
        settings.chmod(0o644)
    step_two = claude_text(welcome, platform="linux").plain
    site = next(s for s in listed[0]["sites"] if s["config_dir"] == str(claude_home))

    generated = ", or point its hooks at this install where that file is generated"
    shared = str(row.fix).replace(generated, "")
    assert row.fix is not None and row.fix.startswith(f"repair {settings} ("), row.fix
    assert "CLAUDE_CONFIG_DIR" not in row.fix, "disconnect could not take the five hooks out"
    assert (generated in row.fix) == (program == "a lost program"), "the doctor's diagnosis"
    assert step_two.endswith(_step_two(shared)), step_two
    assert "; or ".join(site["remedies"]) == shared, site
    connected = runner.invoke(app, ["agents", "connect", "claude-code"])  # repaired: writable
    after = first_run.probe_claude(sign_in=False, which=lambda _name: "/opt/claude")
    assert connected.exit_code == 0, connected.output
    assert diagnostics._check_claude_code().status is CheckStatus.ok and after.connected


def test_welcome_step_two_ends_on_the_command_it_names_as_printed(
    runner: CliRunner, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Welcome step 2 put a period after the doctor's remedies, which can end in a command:
    copied as printed, `--config-dir <dir>.` named another directory, disconnect said ✓,
    and the record and the row stayed (review of #257). Nothing follows the command, and
    run as printed it does what the remedy says."""
    from aisquare.cli.ui.views.welcome import claude_text

    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    _connect(runner)
    (claude_home / "settings.json").write_text("{}", encoding="utf-8")  # hooks taken out
    _utf16(claude_home / "CLAUDE.md")
    welcome = first_run.probe_claude(sign_in=False, which=lambda _name: "/opt/claude")
    step_two = claude_text(welcome, platform="linux").plain
    printed = step_two.rsplit("disconnect this one: aisquare ", 1)[-1].split()
    done = runner.invoke(app, printed)

    assert step_two.endswith(f"--config-dir {claude_home}"), step_two
    assert done.exit_code == 0, done.output
    assert agent_core.connected_dirs("claude-code") == [], "the record is gone, as worded"


@pytest.mark.parametrize("folder", ["Claude Profiles", "claude-$work"], ids=["space", "dollar"])
def test_a_config_dir_with_a_space_or_a_dollar_is_quoted_and_its_button_names_it(
    runner: CliRunner, claude_home: Path, folder: str
) -> None:
    """The doctor, Welcome and `agents list` printed `--config-dir <dir>` bare: pasted, a
    space split the path into extra arguments and a `$` was expanded by the shell (review
    of #257). Quoted for this shell (``install_route.command_line``); a Connect button
    reads it back as the shell would, so it runs on the directory itself."""
    from aisquare.services import install_route

    _connect(runner)
    profile = claude_home.parent / folder / "work"
    profile.mkdir(parents=True)
    _connect(runner, profile)
    (profile / "settings.json").write_text("{}", encoding="utf-8")  # hooks taken out
    row = diagnostics._check_claude_code()
    buttons = [fix.argv for fix in fix_commands([row])]
    pressed = runner.invoke(app, list(buttons[0])) if buttons else None
    shutil.rmtree(profile)  # now gone: forget it, as printed
    gone = diagnostics._check_claude_code()
    printed = str(gone.fix).rsplit("forget it: ", 1)[-1]
    forgot = runner.invoke(app, install_route.split_line(printed)[1:])

    connect = ["agents", "connect", "claude-code", "--config-dir", str(profile)]
    assert install_route.command_line(["aisquare", *connect]) in str(row.fix), row.fix
    assert buttons == [tuple(connect)], buttons
    label = install_route.command_line(["aisquare", *connect])
    assert [fix.label for fix in fix_commands([row])] == [label], "as the fix above it prints it"
    assert pressed is not None and pressed.exit_code == 0, pressed
    assert install_route.split_line(printed) == [
        "aisquare",
        "agents",
        "disconnect",
        "claude-code",
        "--config-dir",
        str(profile),
    ], printed
    assert forgot.exit_code == 0, forgot.output
    assert str(profile) not in [str(p) for p in agent_core.connected_dirs("claude-code")]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_printed_command_reads_back_as_its_arguments(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """What a Connect button runs is the printed fix read back as the shell would read it
    (``install_route.split_line``), on POSIX and on Windows' cmd/PowerShell quoting alike."""
    from aisquare.services import install_route

    monkeypatch.setattr(sys, "platform", platform)  # what install_route asks, at call time
    for directory in (
        "/home/u/Claude Profiles/work",
        "/home/u/claude-$HOME`id`/c",
        r"C:\Users\Me Too\.claude",
        'C:\\a "quoted" dir\\',
        "/plain/path",
    ):
        argv = ["aisquare", "agents", "disconnect", "claude-code", "--config-dir", directory]
        assert install_route.split_line(install_route.command_line(argv)) == argv, directory
    with pytest.raises(ValueError):
        install_route.split_line("--config-dir '/no/closing" if platform == "linux" else '"C:\\x')


def test_a_sibling_that_enables_the_plugin_is_found_with_one_read_of_its_settings(
    claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether a ~/.claude* enables the aisquare plugin was asked twice of its settings.json,
    once directly and once more through `claude_plugin` (review of #257): one read, and
    the install records asked of only where the plugin is enabled."""
    import io

    if not agent_core.plugin_route_supported():
        pytest.skip("the plugin route does not run on native Windows")
    work = claude_home.parent / ".claude-work"
    (work / "plugins").mkdir(parents=True)
    plugin = {agent_core.CLAUDE_PLUGIN_ID: True}
    (work / "settings.json").write_text(json.dumps({"enabledPlugins": plugin}), encoding="utf-8")
    records = {"version": 2, "plugins": {agent_core.CLAUDE_PLUGIN_ID: [{"scope": "user"}]}}
    (work / "plugins" / "installed_plugins.json").write_text(json.dumps(records))
    reads: list[str] = []
    real = io.open

    def counted(file: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, int):
            reads.append(os.fsdecode(file))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", counted)
    found = agent_core.found_on_disk(work)

    assert found, "the plugin is enabled and installed there"
    assert reads.count(str(work / "settings.json")) == 1, reads
    assert reads.count(str(work / "plugins" / "installed_plugins.json")) == 1, reads


def test_a_welcome_tick_reads_settings_json_fewer_times(
    claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Welcome step 2 asks every two seconds on the first-run screen, and each tick read
    ~/.claude/settings.json nine times: `access()` worked out disconnect's half too, which
    step 2 never asks, and a sibling scan read every ~/.claude* (review of #257)."""
    import io

    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    settings = claude_home / "settings.json"
    settings.write_text('{"hooks": {},}', encoding="utf-8")  # one trailing comma: refused
    for sibling in ("2", "-work", "-old"):
        (claude_home.parent / f".claude{sibling}").mkdir()
        (claude_home.parent / f".claude{sibling}" / "settings.json").write_text("{}")
    reads: list[str] = []
    real = io.open

    def counted(file: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, int) and os.fsdecode(file).endswith("settings.json"):
            reads.append(os.fsdecode(file))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", counted)
    state = first_run.probe_claude(sign_in=False, which=lambda _name: "/opt/claude")

    assert state.refused is not None, state
    assert 0 < reads.count(str(settings)) <= 8, reads
    assert len(reads) == reads.count(str(settings)), "no sibling's settings.json is read"


def _hooks_run(settings: Path, program: str) -> None:
    """Point every hook in ``settings`` at ``program``, as a generated config pins one."""
    data = json.loads(settings.read_text(encoding="utf-8"))
    for groups in data["hooks"].values():
        for group in groups:
            for item in group["hooks"]:
                item["command"] = f"{program} hook {item['command'].rsplit(' hook ', 1)[1]}"
    settings.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("profile", ["recorded", "found on disk, not recorded"])
def test_forget_it_is_offered_only_for_a_recorded_profile_disconnect_can_take_out(
    runner: CliRunner, claude_home: Path, tmp_path: Path, profile: str
) -> None:
    """A profile connect refuses, in a folder this user may not write: the doctor's one
    remedy was to make a link lead somewhere, which could not be done there; for one this
    home never recorded, generated read-only with hooks that pin an old program, it was to
    "forget it" with a disconnect that refuses, and the remedy where that file is generated
    was dropped (review of #257). Repair it is always offered, with what generates it where
    that applies, and forget it only where this home recorded it and disconnect would
    work. Each, done as worded, clears the row."""
    if os.name == "nt":
        pytest.skip("a symlink loop in a mode-555 folder is a POSIX shape")
    _connect(runner)
    folder = claude_home.parent / ".claude-work"
    folder.mkdir()
    settings = folder / "settings.json"
    if profile == "recorded":
        _connect(runner, folder)
        settings.unlink()
        settings.symlink_to(settings.name)
    else:
        shutil.copy(claude_home / "settings.json", settings)
        _hooks_run(settings, str(tmp_path / "old-venv" / "bin" / "aisquare"))
        settings.chmod(0o444)
    folder.chmod(0o555)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("this user can write a mode-555 directory (root)")
        refusal = agents_service.access("claude-code", folder).connect
        row = diagnostics._check_claude_code()
        if profile == "recorded":  # forget it, as worded
            done = [runner.invoke(app, _commands(str(row.fix))[0])]
        else:  # what generates it now pins this install
            folder.chmod(0o755)
            settings.chmod(0o644)
            this = json.loads((claude_home / "settings.json").read_text(encoding="utf-8"))
            _hooks_run(settings, this["hooks"]["SessionStart"][0]["hooks"][0]["command"].split()[0])
            settings.chmod(0o444)
            done = []
        rows = [diagnostics._check_claude_code()]
    finally:
        folder.chmod(0o755)
        if settings.exists():
            settings.chmod(0o644)
    if profile == "recorded":  # repaired: a JSON object where the loop was
        settings.unlink()
        settings.write_text("{}", encoding="utf-8")
    done.append(
        runner.invoke(app, ["agents", "connect", "claude-code", "--config-dir", str(folder)])
    )
    rows.append(diagnostics._check_claude_code())

    fix = _repair(refusal)
    if profile == "recorded":
        fix += f"; or forget it: {_DISCONNECT} --config-dir {folder}"
    else:
        fix = fix.replace(
            ", then connect again",
            ", then connect again, or point its hooks at this install where that file is generated",
        )
    assert f"hooks cannot be written in {folder}: " in row.detail, row
    assert row.fix == fix, row.fix
    assert [result.exit_code for result in done] == [0] * len(done), [r.output for r in done]
    assert all(r.status is CheckStatus.ok for r in rows), rows


def test_the_other_readers_of_an_exported_homeless_config_dir_never_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same exported `~olduser/.claude` still raised RuntimeError one line after the
    fixed reader, in the sign-in window's and a fleet spawn's environment
    (carry_environment), and in `team spawn`/`team harness` (account_scope): a bare
    expanduser (review of #257). The window gets it as this shell holds it, never joined
    to the cwd, where its claude would make it."""
    from aisquare.core import harness

    if os.name == "nt":
        pytest.skip("Windows guesses a ~user's home instead of failing to expand it")
    homeless = "~aisquare-no-such-user/.claude"
    _, carried = claude_accounts_service.carry_environment(
        ["claude"], {"CLAUDE_CONFIG_DIR": homeless}, cwd=tmp_path
    )
    _, relative = claude_accounts_service.carry_environment(
        ["claude"], {"CLAUDE_CONFIG_DIR": "profile"}, cwd=tmp_path
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", homeless)
    scope = harness.account_scope()

    assert carried["CLAUDE_CONFIG_DIR"] == homeless, carried
    assert relative["CLAUDE_CONFIG_DIR"] == str(tmp_path / "profile"), "control: resolved"
    assert scope != "default" and len(scope) == 12, scope


@pytest.mark.parametrize("shape", ["new-profile", "never-started"])
def test_a_config_dir_claude_code_has_not_made_is_offered_connect_beside_other_sites(
    runner: CliRunner, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """The directory sessions from this shell read, not made yet (CLAUDE_CONFIG_DIR naming a
    new profile, or an npm/Homebrew Claude Code never started beside a fleet slot): nothing
    graded it, so beside any other site the doctor's row was green with no Connect while
    those sessions ran no hooks, and Welcome said not connected. The test above covers no
    other site (review of #257)."""
    on_path = "/opt/homebrew/bin/claude"
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: on_path)
    if shape == "new-profile":
        _connect(runner)  # makes and connects ~/.claude
        ambient = isolated_agent_home / ".claude-work"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient))
    else:
        slot = isolated_agent_home / ".claude-account1"
        slot.mkdir(parents=True)
        _connect(runner, slot)
        ambient = isolated_agent_home / ".claude"

    row = diagnostics._check_claude_code()
    buttons = [fix.argv for fix in fix_commands([row])]
    welcome = first_run.probe_claude(sign_in=False, which=lambda _name: on_path)
    made_by_asking = ambient.exists()
    with monkeypatch.context() as no_claude_here:
        no_claude_here.setattr(agent_core, "claude_on_path", lambda: None)
        no_claude = diagnostics._check_claude_code()
    clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    after = diagnostics._check_claude_code()

    assert row.status is CheckStatus.warn and f"missing in {ambient}" in row.detail, row
    assert buttons == [("agents", "connect", "claude-code")], "bare: --config-dir makes nothing"
    assert (welcome.found, welcome.connected) == (True, False), "Welcome said so all along"
    assert not made_by_asking, "asking makes nothing"
    assert clicked.exit_code == 0 and after.status is CheckStatus.ok, (clicked.output, after)
    assert no_claude.status is CheckStatus.ok, "control: no claude on PATH, nothing to connect"


@pytest.mark.parametrize("shape", _shapes(_UNREADABLE_SHAPES))
def test_connect_names_a_settings_json_it_cannot_read_and_leaves_it_alone(
    runner: CliRunner, claude_home: Path, tmp_path: Path, shape: str
) -> None:
    """Undecodable was ``not_installed`` after the home was built, a directory a traceback
    after CLAUDE.md was ingested. Now: refused first, the file named, nothing changed."""
    settings_path = claude_home / "settings.json"
    _DAMAGED_SETTINGS[shape](settings_path)
    reference = tmp_path / "reference" / "settings.json"
    reference.parent.mkdir()
    _DAMAGED_SETTINGS[shape](reference)

    refused = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    built = paths.aisquare_home().exists()
    left = _content(settings_path)
    _cleared(settings_path)
    connected = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])

    assert isinstance(refused.exception, SystemExit), repr(refused.exception)
    payload = json.loads(refused.stdout)
    assert (payload["error"], payload["ref"]) == ("agent_file_unreadable", "claude-code"), payload
    assert str(payload["detail"]).startswith(f"can't read {settings_path}: "), payload
    assert not built, "refused before the context was ingested or the home built"
    assert left == _content(reference), "the file is left exactly as it was"
    assert connected.exit_code == 0, "control: the same connect succeeds once it can read"


def test_a_settings_json_this_user_may_not_write_is_refused_before_anything_is_ingested(
    runner: CliRunner, claude_home: Path
) -> None:
    """A mode-444 settings.json (home-manager's, or a link into the read-only Nix store)
    ended `agents connect` in a traceback with no JSON, after CLAUDE.md was ingested, and
    `init --agent` blamed config.toml (review of #257)."""
    settings_path = claude_home / "settings.json"
    settings_path.write_text('{"model": "opus"}', encoding="utf-8")
    (claude_home / "CLAUDE.md").write_text("# Prefs\nuse tabs\n", encoding="utf-8")
    settings_path.chmod(0o444)
    if os.access(settings_path, os.W_OK):
        settings_path.chmod(0o644)
        pytest.skip("this user can write a read-only file (root)")
    try:
        connect = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
        built = paths.aisquare_home().exists()
        refresh = runner.invoke(app, ["--json", "agents", "refresh-hooks", "claude-code"])
        init = runner.invoke(
            app, ["--json", "init", "--yes", "--no-onboard", "--agent", "claude-code"]
        )
    finally:
        settings_path.chmod(0o644)
    left = settings_path.read_text(encoding="utf-8")
    connected = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])

    reason = f"can't write {settings_path}: "
    payload = json.loads(connect.stdout)
    assert (payload["error"], payload["ref"]) == ("agent_file_unreadable", "claude-code"), payload
    assert str(payload["detail"]).startswith(reason), payload
    assert not built, "refused before CLAUDE.md was ingested or the home built"
    assert str(json.loads(refresh.stdout)["detail"]).startswith(reason), refresh.stdout
    notes = " ".join(json.loads(init.stdout)["notes"])
    assert f"Could not connect claude-code: {reason}" in notes, notes
    assert left == '{"model": "opus"}' and connected.exit_code == 0, "control: writable again"


_SETTINGS = {"model": "opus", "permissions": {"allow": ["Bash(git status)"]}, "env": {"FOO": "1"}}


@pytest.mark.parametrize(
    ("text", "why"),
    [
        # The parenthesis holds the json module's own words, which differ by Python
        # version (3.13: "Illegal trailing comma ..."), then the line and column.
        (json.dumps(_SETTINGS)[:-1] + ",}\n", "it is not valid JSON ("),
        (json.dumps([_SETTINGS]), "it is not a JSON object"),
    ],
    ids=["trailing-comma", "an-array"],
)
def test_connect_never_rewrites_a_settings_json_that_is_not_a_json_object(
    runner: CliRunner, claude_home: Path, text: str, why: str
) -> None:
    """Read as ``{}``, then written back as ``{"hooks": …}``: one trailing comma cost the
    user their model, permissions and env (review of #257). Welcome's Connect and the
    doctor's button run this command, and their card shows the file and why."""
    settings_path = claude_home / "settings.json"
    settings_path.write_text(text, encoding="utf-8")

    def run(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        result = runner.invoke(app, list(args))
        return CliResult(
            argv=list(args), returncode=result.exit_code, stdout=result.stdout, stderr=""
        )

    clicked = first_run.connect(run=run)
    refreshed = runner.invoke(app, ["--json", "agents", "refresh-hooks", "claude-code"])
    with pytest.raises(agent_core.SettingsNotAnObjectError):
        agent_core.install_hooks("claude-code")
    left = settings_path.read_text(encoding="utf-8")
    built = paths.aisquare_home().exists()
    settings_path.write_text(json.dumps(_SETTINGS), encoding="utf-8")
    fixed = first_run.connect(run=run)
    kept = json.loads(settings_path.read_text(encoding="utf-8"))
    settings_path.write_text("\n", encoding="utf-8")

    reason = f"can't read {settings_path}: {why}"
    assert not clicked.ok and reason in (clicked.reason or ""), clicked.reason
    assert ("line 1 column" in (clicked.reason or "")) == why.endswith("("), clicked.reason
    assert json.loads(refreshed.stdout)["detail"].startswith(reason), refreshed.stdout
    assert left == text, "the file is left exactly as it was"
    assert not built, "refused before the context was ingested or the home built"
    assert fixed.ok and {k: kept[k] for k in _SETTINGS} == _SETTINGS and "hooks" in kept, kept
    assert first_run.connect(run=run).ok, "an empty file holds nothing to lose"


def test_a_connection_that_installs_nothing_is_refused_not_recorded(
    runner: CliRunner, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug read "no hooks for this agent": `connect codex` exited 0 with it and recorded
    the agent. No connection without hooks may be printed or recorded again."""
    with monkeypatch.context() as patched:
        patched.setattr(agent_core, "install_hooks", lambda name, config_dir=None: False)
        refused = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    recorded = paths.agents_registry_path().exists()
    connected = runner.invoke(app, ["agents", "connect", "claude-code"])

    assert json.loads(refused.stdout) == {"error": "unsupported_agent", "ref": "claude-code"}
    assert not recorded, "nothing was written to agents.json"
    assert connected.output.startswith("✓ connected claude-code — hooks installed"), "control"


# --------------------------------------------------------------------------- the doctor's rows


def _row(name: str) -> DoctorCheck:
    return next(check for check in diagnostics.doctor() if check.name == name)


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_the_doctor_has_a_row_for_every_agent_in_the_registry(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    payload = json.loads(runner.invoke(app, ["--json", "doctor"]).stdout)
    rows = {row["name"]: row for row in payload}

    registry = {spec.name for spec in agent_core.specs()}
    assert {"claude-code", "codex", "cursor"} <= registry
    assert registry <= set(rows), f"agents with no doctor row: {sorted(registry - set(rows))}"
    for name in ("codex", "cursor"):
        assert (rows[name]["status"], rows[name]["fix"]) == ("ok", None), rows[name]


@pytest.mark.parametrize(("name", "label", "planned"), _DETECT_ONLY)
def test_a_detect_only_agent_row_names_the_path_it_checked(
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    label: str,
    planned: str | None,
) -> None:
    """By path, not "on this machine": a Codex kept elsewhere through ``CODEX_HOME`` is
    not looked for there, so a sentence about the machine could be false."""
    monkeypatch.chdir(tmp_path)
    home = isolated_agent_home / f".{name}"
    absent = _row(name)
    home.mkdir(parents=True)
    present = _row(name)

    support = f" (aisquare support is planned for {planned})" if planned else ""
    later = f" (planned for {planned})" if planned else ""
    spec = agent_core.spec(name)
    assert spec is not None and spec.planned == planned, spec
    assert absent.detail == f"{label} not detected at {home}{support}"
    assert present.detail == f"{label} detected at {home}, but aisquare can't connect it yet{later}"
    for row in (absent, present):
        assert (row.status, row.fix) == (CheckStatus.ok, None), row


def _release(version: str) -> tuple[int, ...]:
    """``"0.10"`` → ``(0, 10, 0)``: the release a version names, compared as numbers."""
    found = re.match(r"\d+(?:\.\d+)*", version)
    numbers = [int(part) for part in found.group(0).split(".")][:3] if found else []
    return tuple(numbers + [0] * (3 - len(numbers)))


def _still_ahead(planned: str, running: str) -> bool:
    """Whether a release planned as ``planned`` is still to come for ``running``."""
    return _release(planned) > _release(running)


def test_every_planned_release_is_still_ahead_of_this_one() -> None:
    """A row saying "planned for 0.10" on 0.10 itself is a promise broken in public. So the
    release commit that reaches a planned version fails here until the plan moves or the
    agent can be connected."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    running = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    planned = {spec.name: spec.planned for spec in agent_core.specs() if spec.planned}

    assert planned, "the registry plans no release, so this guards nothing (Codex: 0.10)"
    overdue = {name: when for name, when in planned.items() if not _still_ahead(when, running)}
    assert overdue == {}, f"planned releases already reached by {running}: {overdue}"
    # The rule's controls: by number, not by text, and a release that has arrived is due.
    assert _still_ahead("0.10", "0.9.0") and _still_ahead("1.0", "0.10.3")
    assert not _still_ahead("0.10", "0.10.0") and not _still_ahead("0.9", "0.10.0")


def test_only_the_claude_code_row_offers_connect(
    claude_home: Path, isolated_agent_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The buttons are ``fix_commands`` over the report: Claude Code's Connect, nothing else."""
    for name in (".codex", ".cursor"):
        (isolated_agent_home / name).mkdir()
    monkeypatch.chdir(tmp_path)

    checks = diagnostics.doctor()

    connects = [fix.argv for fix in fix_commands(checks) if fix.argv[:2] == ("agents", "connect")]
    assert connects == [("agents", "connect", "claude-code", "--config-dir", str(claude_home))]
    named = {check.name: check for check in checks}
    assert named["claude-code"].status is CheckStatus.warn, "control: Connect is on offer here"
    assert all(
        (named[name].status, named[name].fix) == (CheckStatus.ok, None)
        for name in ("codex", "cursor")
    ), [named["codex"], named["cursor"]]


def test_every_reader_follows_the_shared_check(
    runner: CliRunner, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin route extends ``claude_code_connected``. Every reader must follow it, or
    one of them goes on calling a directory unconnected and points at a second install of
    the hooks: the doctor's row, ``agents status``, and the Accounts page's slot."""
    _connect(runner)
    (claude_home / "settings.json").write_text("{}", encoding="utf-8")  # recorded, hooks gone

    def readers() -> tuple[DoctorCheck, bool, bool]:
        status = json.loads(
            runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout
        )
        slot = claude_accounts_service.describe(claude_accounts_core.default_account())
        site: bool = status[0]["sites"][0]["hooks_installed"]
        return diagnostics._check_claude_code(), site, slot.hooks_installed

    before = readers()
    monkeypatch.setattr(agent_core, "claude_code_connected", lambda config_dir=None, cwd=None: True)
    after = readers()

    row, site, slot = before
    assert row.status is CheckStatus.warn, "control: unconnected, and every reader says so"
    assert row.fix == f"aisquare agents connect claude-code --config-dir {claude_home}"
    assert (site, slot) == (False, False)
    row, site, slot = after
    assert row.status is CheckStatus.ok and fix_commands([row]) == [], row
    assert (site, slot) == (True, True)


@pytest.mark.parametrize("shape", ["not JSON", "read-only"])
def test_a_settings_json_connect_refuses_is_named_and_never_offered_connect(
    claude_home: Path, shape: str
) -> None:
    """It read as "hooks are missing or outdated (older installs …)", with a Connect that
    could only fail: connect refuses a settings.json that is not a JSON object, or one this
    user may not write, and the read-only one (home-manager's link into the Nix store)
    never cleared (review of #257). Named with connect's reason, and no button."""
    settings_path = claude_home / "settings.json"
    if shape == "not JSON":
        settings_path.write_text('{"model": "opus",}\n', encoding="utf-8")
    else:
        settings_path.write_text('{"model": "opus"}\n', encoding="utf-8")
        settings_path.chmod(0o444)
        if os.access(settings_path, os.W_OK):
            settings_path.chmod(0o644)
            pytest.skip("this user can write a read-only file (root)")
    try:
        row = diagnostics._check_claude_code()
    finally:
        settings_path.chmod(0o644)
    settings_path.write_text("{}\n", encoding="utf-8")
    fixable = diagnostics._check_claude_code()

    assert row.status is CheckStatus.warn, row
    assert f"hooks cannot be written in {claude_home}" in row.detail, row
    assert str(settings_path) in row.detail and "older installs" not in row.detail, row
    assert fix_commands([row]) == [], "no Connect: the click could only fail"
    assert fix_commands([fixable]) != [], "control: a settings.json it can write gets Connect"


def _utf16(path: Path) -> None:
    path.write_bytes("# Prefs\ncafé\n".encode("utf-16"))  # Windows PowerShell 5.1's `>`


def _latin1(path: Path) -> None:
    path.write_bytes("# Prefs\ncafé\n".encode("latin-1"))


#: CLAUDE.md shapes `agents connect` refuses before it writes anything.
_REFUSED_CLAUDE_MD = {
    "UTF-16": _utf16,
    "Latin-1": _latin1,
    "a directory": _a_directory,
    "mode 000": _mode_000,
}


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(name, marks=_NEEDS_DENIED_READS if name == "mode 000" else (), id=name)
        for name in _REFUSED_CLAUDE_MD
    ],
)
def test_a_claude_md_connect_refuses_is_named_and_never_offered_connect(
    runner: CliRunner, claude_home: Path, shape: str
) -> None:
    """`agents connect` reads CLAUDE.md before it writes and refuses one it cannot read, but
    the doctor, Welcome and the installer asked only about settings.json: Connect, the
    default first-run click, was offered and every click failed (review of #257). They ask
    connect's own checks now, all of them, and name the file."""
    claude_md = claude_home / "CLAUDE.md"
    _REFUSED_CLAUDE_MD[shape](claude_md)
    try:
        refusal = _refused_why()
        row = diagnostics._check_claude_code()
        welcome = first_run.probe_claude(sign_in=False, which=lambda _name: None)
        clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    finally:
        _cleared(claude_md)
    claude_md.write_text("# Prefs\ncafé\n", encoding="utf-8")
    readable = _refused_why(), diagnostics._check_claude_code()

    reason = str(json.loads(clicked.stdout)["detail"])
    assert clicked.exit_code == 1 and reason.startswith(f"can't read {claude_md}: "), reason
    assert refusal == reason, "connect's own checks, in its own words"
    assert row.status is CheckStatus.warn, row
    assert f"hooks cannot be written in {claude_home}: {reason}" in row.detail, row
    fact = reason.removeprefix(f"can't read {claude_md}: ")
    assert row.fix == f"repair {claude_md} ({fact}), then connect again; or {_REPOINT_FIX}"
    assert fix_commands([row]) == [], "no Connect: the click could only fail"
    assert (welcome.connected, welcome.refused) == (False, reason), welcome
    refusal, row = readable
    assert refusal is None and fix_commands([row]) != [], "control: a UTF-8 CLAUDE.md is offered"


@_NEEDS_DENIED_READS
@pytest.mark.parametrize("where", ["a recorded profile", "the ambient dir"])
def test_a_claude_md_that_cannot_be_stated_is_a_named_refusal_not_a_traceback(
    runner: CliRunner, claude_home: Path, where: str
) -> None:
    """A CLAUDE.md that is a link into a directory this user cannot search: Path.exists
    raises PermissionError there on 3.11 to 3.13, and `doctor --json`, `agents
    list/status/scan`, connect and init ended in a traceback with nothing on stdout
    (review of #257). Such a file is there to read, and the read names it."""
    from aisquare.cli.common import _hook_sites

    if os.name == "nt":
        pytest.skip("a link into a directory denied to this user is a POSIX shape")
    _connect(runner)
    target = claude_home
    if where == "a recorded profile":
        target = claude_home.parent / ".claude-c2"
        target.mkdir()
        _connect(runner, target)
    (target / "settings.json").write_text("{}", encoding="utf-8")  # hooks gone: asked
    locked = claude_home.parent / "locked"
    locked.mkdir()
    (locked / "CLAUDE.md").write_text("# Prefs\n", encoding="utf-8")
    claude_md = target / "CLAUDE.md"
    claude_md.symlink_to(locked / "CLAUDE.md")
    locked.chmod(0)
    try:
        doctor = runner.invoke(app, ["--json", "doctor"])
        listed = runner.invoke(app, ["--json", "agents", "list"])
        argv = ["--json", "agents", "connect", "claude-code", "--config-dir", str(target)]
        clicked = runner.invoke(app, argv)
        welcome = first_run.probe_claude(sign_in=False, which=lambda _name: None)
        cell = _hook_sites(agents_service.status("claude-code")[0])
    finally:
        locked.chmod(0o755)
    readable = _refused_why(target)

    reason = f"can't read {claude_md}: Permission denied"
    assert doctor.exception is None or isinstance(doctor.exception, SystemExit), repr(
        doctor.exception
    )
    row = _row_named(doctor.stdout, "claude-code")
    assert f"hooks cannot be written in {target}: {reason}" in str(row["detail"]), row
    assert listed.exit_code == 0, listed.output
    sites = {site["config_dir"]: site for site in json.loads(listed.stdout)[0]["sites"]}
    assert sites[str(target)]["refused"] == reason, sites
    assert f"cannot be written in {target}: {reason}" in cell, "the table names the file too"
    assert clicked.exit_code == 1 and json.loads(clicked.stdout)["detail"] == reason, clicked
    if where == "the ambient dir":
        assert (welcome.connected, welcome.refused) == (False, reason), welcome
    assert readable is None, "control: unlocked, the same CLAUDE.md is read"


def _short_of_the_ceiling(settings_path: Path) -> None:
    """No `timeout` on the two context hooks, as in a file declared from the docs' table."""
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for event in ("SessionStart", "UserPromptSubmit"):
        for group in settings["hooks"][event]:
            for item in group["hooks"]:
                item.pop("timeout", None)
    settings_path.write_text(json.dumps(settings), encoding="utf-8")


@pytest.mark.parametrize("refused", ["read-only settings.json", "UTF-16 CLAUDE.md"])
def test_short_timeouts_connect_cannot_raise_are_named_and_never_offered_connect(
    runner: CliRunner, claude_home: Path, refused: str
) -> None:
    """All six hooks, with no `timeout` on the context hooks, in a directory whose files
    connect refuses: the row offered the Connect that raises them, every click failed, and a
    read-only settings.json (home-manager's link into the Nix store) never cleared (review of
    #257). Named with connect's reason, with what to set instead, and no button."""
    _connect(runner)
    settings_path = claude_home / "settings.json"
    _short_of_the_ceiling(settings_path)
    claude_md = claude_home / "CLAUDE.md"
    if refused == "UTF-16 CLAUDE.md":
        _utf16(claude_md)
    else:
        settings_path.chmod(0o444)
    try:
        if os.access(settings_path, os.W_OK) and refused == "read-only settings.json":
            pytest.skip("this user can write a read-only file (root)")
        row = diagnostics._check_claude_code()
        argv = ["--json", "agents", "connect", "claude-code", "--config-dir", str(claude_home)]
        clicked = runner.invoke(app, argv)
    finally:
        settings_path.chmod(0o644)
        claude_md.unlink(missing_ok=True)
    fixable = diagnostics._check_claude_code()

    reason = str(json.loads(clicked.stdout)["detail"])
    assert clicked.exit_code == 1, "connect refuses the directory"
    assert row.status is CheckStatus.warn, row
    assert " connected, but the context hooks allow less than 120 s in: " in row.detail, row
    assert f"hooks cannot be written in {claude_home}: {reason}" in row.detail, row
    assert fix_commands([row]) == [], "no Connect: the click could only fail"
    # Recorded, it is graded whatever CLAUDE_CONFIG_DIR says: the variable's remedy comes
    # with the disconnect that takes it out, and only where that disconnect would work.
    blocked = claude_md if refused == "UTF-16 CLAUDE.md" else settings_path
    fact = reason.split(f"{blocked}: ", 1)[1]
    if refused == "UTF-16 CLAUDE.md":
        fix = f"repair {blocked} ({fact}), then connect again; or {_REPOINT_FIX}, and "
        fix += f"disconnect this one: {_DISCONNECT} --config-dir {claude_home}"
    else:
        fix = (
            f"repair {blocked} ({fact}), then connect again, or give its SessionStart and "
            "UserPromptSubmit hooks a timeout of at least 120 where that file is generated"
        )
    assert row.fix == fix, row.fix
    assert [fix.argv for fix in fix_commands([fixable])] == [(*argv[1:],)], (
        "control: where connect can write, the same row offers it"
    )


def test_agents_status_names_a_directory_connect_refuses_as_the_doctor_does(
    runner: CliRunner, claude_home: Path
) -> None:
    """A connected ~/.claude whose settings.json gained one trailing comma: `agents list` and
    `agents status` read it as no hooks and said "missing", which points at Connect, while
    the doctor and Welcome named connect's refusal (review of #257). They name it too."""
    from aisquare.cli.common import _hook_sites

    _connect(runner)
    settings_path = claude_home / "settings.json"
    hooked = settings_path.read_text(encoding="utf-8").rstrip()
    settings_path.write_text(hooked.removesuffix("}").rstrip() + ",\n}\n", encoding="utf-8")
    damaged = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    cell = _hook_sites(agents_service.status("claude-code")[0])
    refusal = _refused_why()
    row = diagnostics._check_claude_code()
    settings_path.write_text("{}\n", encoding="utf-8")  # readable, and the hooks are gone
    plain = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    missing = _hook_sites(agents_service.status("claude-code")[0])

    assert refusal is not None and "it is not valid JSON" in refusal, refusal
    remedies = damaged[0]["sites"][0]["remedies"]
    assert damaged[0]["sites"] == [
        {
            "config_dir": str(claude_home),
            "hooks_installed": False,
            "hooks_off": None,
            "refused": refusal,
            "remedies": remedies,
        }
    ], damaged
    assert remedies and "; or ".join(remedies) == row.fix, "the doctor's remedies, as it words them"
    assert cell == f"0/1 ok — cannot be written in {claude_home}: {refusal} — {row.fix}", cell
    assert f"hooks cannot be written in {claude_home}: {refusal}" in row.detail, row
    assert plain[0]["sites"][0]["refused"] is None, "control: a file connect can write"
    assert missing == f"0/1 ok — missing in {claude_home}", missing


def _trailing_comma(path: Path) -> None:
    text = path.read_text(encoding="utf-8").rstrip()
    path.write_text(text.removesuffix("}").rstrip() + ",\n}\n", encoding="utf-8")


@pytest.mark.parametrize(
    "shape",
    ["a trailing comma", pytest.param("mode 000", marks=_NEEDS_DENIED_READS), "read-only"],
)
def test_disconnect_refuses_hooks_it_cannot_take_out_and_keeps_the_record(
    runner: CliRunner, claude_home: Path, shape: str
) -> None:
    """With one trailing comma, which Claude Code may read past, disconnect forgot the
    directory and said "✓ disconnected" while all six hooks stayed in the file; mode 000
    ended in a traceback, and so did a read-only file, home-manager's shape (review of
    #257). It refuses before touching anything, with uninstall's own reason, and the
    record stays, as uninstall keeps it."""
    _connect(runner)
    settings_path = claude_home / "settings.json"
    if shape == "a trailing comma":
        _trailing_comma(settings_path)
    before = settings_path.read_bytes()
    registry = paths.agents_registry_path().read_text(encoding="utf-8")
    settings_path.chmod({"mode 000": 0, "read-only": 0o444}.get(shape, 0o644))
    try:
        if shape == "read-only" and os.access(settings_path, os.W_OK):
            pytest.skip("this user can write a read-only file (root)")
        human = runner.invoke(app, ["agents", "disconnect", "claude-code"])
        machine = runner.invoke(app, ["--json", "agents", "disconnect", "claude-code"])
    finally:
        settings_path.chmod(0o644)
    kept = settings_path.read_bytes(), paths.agents_registry_path().read_text(encoding="utf-8")
    settings_path.write_text("{}", encoding="utf-8")  # readable again, hooks gone
    _connect(runner)
    control = runner.invoke(app, ["agents", "disconnect", "claude-code"])

    assert human.exit_code == 1, human.output
    assert human.output.startswith(f"✗ cannot take the hooks out of {claude_home}: its "), human
    assert "disconnect again, or take aisquare's hooks out of it by hand" in human.output
    payload = json.loads(machine.stdout)  # exactly one object
    assert (payload["error"], payload["ref"]) == ("agent_file_unreadable", "claude-code")
    assert str(payload["detail"]).startswith(f"cannot take the hooks out of {claude_home}: ")
    assert kept == (before, registry), "nothing touched: the hooks and their record stay"
    assert control.exit_code == 0 and "✓ disconnected claude-code" in control.output, control
    assert not agent_core.hook_commands("claude-code"), "control: a file it can rewrite"


def test_the_accounts_disconnect_still_forgets_a_slot_beside_an_unparseable_file(
    claude_home: Path,
) -> None:
    """The refusal above is the command's, not the service's: removing an account, whose
    directory leaves either way, calls the service, which forgets the record as before."""
    agents_service.connect("claude-code", claude_home)
    _trailing_comma(claude_home / "settings.json")

    forgot = agents_service.disconnect("claude-code", claude_home)

    assert forgot is True and agent_core.connected_dirs("claude-code") == [], "forgotten"
    text = (claude_home / "settings.json").read_text(encoding="utf-8")
    assert text.count(" hook ") == 6, "and the file is left as it was"


def test_hooks_switched_off_are_not_connected_and_never_offered_connect(
    runner: CliRunner, claude_home: Path
) -> None:
    """``"disableAllHooks": true`` runs none of our hooks however complete they are, and
    Connect leaves the key alone, so it may not be offered: the row says what to change."""
    _connect(runner)
    settings_path = claude_home / "settings.json"
    hooked = json.loads(settings_path.read_text(encoding="utf-8"))
    settings_path.write_text(json.dumps({**hooked, "disableAllHooks": True}), encoding="utf-8")
    switched_off = agents_service.claude_code_connected(), diagnostics._check_claude_code()
    _connect(runner)
    after_connect = agents_service.claude_code_connected()
    settings_path.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
    no_hooks_either = diagnostics._check_claude_code()
    settings_path.write_text(json.dumps(hooked), encoding="utf-8")
    back_on = agents_service.claude_code_connected(), diagnostics._check_claude_code()

    connected, row = switched_off
    assert connected is False and after_connect is False, "Connect cannot clear the switch"
    assert row.status is CheckStatus.warn and '"disableAllHooks": true' in row.detail, row
    assert row.fix == f'Turn hooks back on: remove "disableAllHooks" from {settings_path}'
    assert fix_commands([row]) == [] and fix_commands([no_hooks_either]) == [], "no button"
    connected, row = back_on
    assert connected is True and row.status is CheckStatus.ok, f"control: {row}"


def test_a_switched_off_directory_is_one_clause_and_the_others_keep_their_connect(
    runner: CliRunner, claude_home: Path
) -> None:
    """The switch is per config dir. One left on in a second profile replaced the whole
    row, so the directory sessions read showed no "missing" and no Connect while it had no
    hooks at all (review of #257)."""
    work = claude_home.parent / ".claude-work"
    work.mkdir()
    _connect(runner, work)
    settings_path = work / "settings.json"
    hooked = json.loads(settings_path.read_text(encoding="utf-8"))
    settings_path.write_text(json.dumps({**hooked, "disableAllHooks": True}), encoding="utf-8")

    row = diagnostics._check_claude_code()
    buttons = [" ".join(fix.argv) for fix in fix_commands([row])]

    assert row.status is CheckStatus.warn, row
    assert f'switched off ("disableAllHooks": true) in: {settings_path}' in row.detail, row
    assert f"{diagnostics._STALE_HOOKS} in: {claude_home}" in row.detail, row
    assert f'remove "disableAllHooks" from {settings_path}' in (row.fix or ""), row
    assert buttons == [f"agents connect claude-code --config-dir {claude_home}"], buttons


def test_only_a_literal_true_switches_hooks_off(claude_home: Path) -> None:
    """As Claude Code reads the key; anything else leaves the hooks on."""
    settings_path = claude_home / "settings.json"
    answers = {}
    for value in (True, False, "true", 1, None):
        settings_path.write_text(json.dumps({"disableAllHooks": value}), encoding="utf-8")
        answers[repr(value)] = agent_core.hooks_disabled("claude-code")

    assert answers == {"True": True, "False": False, "'true'": False, "1": False, "None": False}


def test_agents_list_status_connect_and_init_name_hooks_switched_off(
    runner: CliRunner, claude_home: Path
) -> None:
    """`agents list` and `agents status` read a switched-off directory as "missing in", which
    points at Connect; Connect said "✓ connected" and changed nothing, so connect then list
    went round in a loop, and `init` said "Connected" too. Each now names the switch, and the
    hooks are still installed, so they run once the key goes (review of #257)."""
    settings_path = claude_home / "settings.json"
    settings_path.write_text('{"disableAllHooks": true, "model": "opus"}', encoding="utf-8")
    work = claude_home.parent / ".claude-work"
    work.mkdir()
    _connect(runner, work)
    (work / "settings.json").write_text("{}", encoding="utf-8")  # recorded, hooks gone: missing
    wide = {"COLUMNS": "1000"}

    connect = runner.invoke(app, ["agents", "connect", "claude-code"])
    again = json.loads(runner.invoke(app, ["--json", "agents", "connect", "claude-code"]).stdout)
    listed = runner.invoke(app, ["agents", "list"], env=wide).stdout
    status = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    init = runner.invoke(app, ["--json", "init", "--yes", "--no-onboard", "--agent", "claude-code"])
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    del settings["disableAllHooks"]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    back_on = runner.invoke(app, ["agents", "list"], env=wide).stdout

    assert connect.exit_code == 0, connect.output
    assert connect.stdout.startswith("hooks installed for claude-code, but switched off"), connect
    assert f'note: {settings_path} sets "disableAllHooks": true' in connect.stderr, connect.stderr
    assert (again["hooks_installed"], again["hooks_off"]) == (True, str(settings_path)), again
    assert f"0/2 ok — missing in {work}; switched off in {claude_home}" in listed, listed
    sites = {site["config_dir"]: site for site in status[0]["sites"]}
    assert sites[str(claude_home)] == {
        "config_dir": str(claude_home),
        "hooks_installed": False,
        "hooks_off": str(settings_path),
        "refused": None,
        "remedies": [],
    }
    assert (sites[str(work)]["hooks_installed"], sites[str(work)]["hooks_off"]) == (False, None)
    notes = " ".join(json.loads(init.stdout)["notes"])
    assert f'{settings_path} sets "disableAllHooks": true' in notes, notes
    assert "Connected claude-code" not in notes, notes
    assert f"1/2 ok — missing in {work}" in back_on and "switched off" not in back_on, (
        "control: with the key gone, the hooks connect already wrote run"
    )


def test_the_agent_rows_read_paths_only(
    isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No socket, no process, no agents.json, no state: every doctor run in asq computes them."""
    for name in (".codex", ".cursor"):
        (isolated_agent_home / name).mkdir(parents=True)
    before = _tree(isolated_agent_home)
    started: list[str] = []

    class Tripwire(socket.socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            started.append("socket")
            raise ConnectionRefusedError("a doctor agent row opened a socket")

    def no_process(*args: object, **kwargs: object) -> None:
        started.append("process")
        raise OSError("a doctor agent row started a process")

    def no_registry() -> dict[str, object]:
        started.append("registry")
        raise OSError("a doctor agent row read agents.json")

    monkeypatch.setattr(socket, "socket", Tripwire)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(agent_core, "_registry", no_registry)

    rows = diagnostics._planned_agent_checks()

    assert {row.name for row in rows} == {"codex", "cursor"}
    assert started == []
    assert not paths.aisquare_home().exists()
    assert _tree(isolated_agent_home) == before
    with pytest.raises(OSError):  # control: every tripwire is live
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(OSError):
        subprocess.run(["true"], check=False)
    with pytest.raises(OSError):
        agent_core.detect("codex")
    assert started == ["socket", "process", "registry"]


def test_doctor_with_every_agent_on_disk_writes_nothing(
    runner: CliRunner,
    claude_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (".codex", ".cursor"):
        (isolated_agent_home / name).mkdir()
    monkeypatch.chdir(tmp_path)
    before = _tree(isolated_agent_home)

    result = runner.invoke(app, ["--json", "doctor"])

    # A doctor that crashed would write nothing too: the report must have been made,
    # and must have seen the agents put there (doctor exits 1: there is no home).
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    for name, label in (("codex", "Codex"), ("cursor", "Cursor")):
        assert str(_row_named(result.stdout, name)["detail"]).startswith(f"{label} detected at ")
    assert _tree(isolated_agent_home) == before
    assert not paths.aisquare_home().exists()
    _connect(runner)  # control: a real connect writes, and the listing sees it
    assert _tree(isolated_agent_home) != before


def test_the_agent_rows_survive_a_damaged_store(
    runner: CliRunner,
    isolated_agent_home: Path,
    damaged_store: str,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (isolated_agent_home / ".codex").mkdir(parents=True)
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: None)  # no Claude Code anywhere

    result = runner.invoke(app, ["--json", "doctor"])

    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["codex"]["status"] == "ok" and "Codex detected at" in rows["codex"]["detail"]
    assert rows["cursor"]["status"] == "ok" and rows["claude-code"]["status"] == "ok"
    if damaged_store == "at-open":  # control: doctor does see this damage (a zeroed page it
        assert rows["database"]["status"] == "fail", rows["database"]  # never reads, it cannot)
