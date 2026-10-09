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

import json
import os
import re
import shutil
import socket
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

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

    reason = f"{gone} does not exist"
    assert f"hooks cannot be written in {gone}: {reason}" in row.detail, row
    assert f"{diagnostics._STALE_HOOKS} in: {kept}" in row.detail, "only kept is missing"
    assert f"forget it: aisquare agents disconnect claude-code --config-dir {gone}" in str(row.fix)
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

    assert f"hooks cannot be written in {loop}: {loop} does not exist" in row.detail, row
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
    assert "point CLAUDE_CONFIG_DIR at a directory on this machine" in str(row.fix), row.fix
    assert welcome.refused == reason, welcome
    assert json.loads(clicked.stdout)["detail"] == reason, clicked.stdout
    assert paths.aisquare_home().exists() == built, "a refusal builds no aisquare home"
    assert list(work.iterdir()) == [], "nothing is made in the cwd"
    # Only the variable changes it, and asq and aisquare read it when they start: Welcome
    # promised "this page notices", and the doctor's fix never said to start again.
    import dataclasses

    from aisquare.cli.ui.views.welcome import claude_text

    found = dataclasses.replace(welcome, binary="/opt/homebrew/bin/claude")
    step_two = claude_text(found, platform="linux").plain
    assert "then start asq again from that shell" in step_two, step_two
    assert "this page notices" not in step_two, step_two
    assert str(row.fix).endswith("then start asq or aisquare again from that shell"), row.fix
    file_refused = dataclasses.replace(found, refused_by_environment=False)
    assert "this page notices" in claude_text(file_refused, platform="linux").plain, "control"


@pytest.mark.parametrize(
    "shape",
    [
        "the default dir, recorded, now a loop",
        "the default dir, a dangling link",
        "a profile, recorded, now a dangling link",
        "a profile under a parent this user may not write",
        "that profile beside a connected ~/.claude",
    ],
)
def test_a_config_dir_connect_cannot_make_is_named_everywhere_and_never_offered_connect(
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """With `claude` on PATH, connect first makes the directory a session from this shell
    reads, and access() did not ask whether it can: a link there that leads nowhere (a
    dotfiles target that moved, a loop) or a parent this user may not write. The doctor
    and Welcome offered Connect, which failed "can't create …"; a default ~/.claude turned
    loop read "can't read its settings.json", with a fix no one can make (review of #257).
    Connect's own first step, in its own words, everywhere."""
    if os.name == "nt":
        pytest.skip("links, loops and mode 555 are POSIX shapes here")
    monkeypatch.setattr(agent_core, "claude_on_path", lambda: "/opt/homebrew/bin/claude")
    claude = isolated_agent_home / ".claude"
    where = claude
    if shape.startswith("the default dir"):
        if "recorded" in shape:
            claude.mkdir(parents=True)
            _connect(runner)
            shutil.rmtree(claude)
            claude.symlink_to(claude.name)
        else:
            isolated_agent_home.mkdir(parents=True)
            claude.symlink_to(isolated_agent_home / "dotfiles" / "claude")
    elif "recorded" in shape:
        where = isolated_agent_home / ".claude-work"
        where.mkdir(parents=True)
        _connect(runner, where)
        shutil.rmtree(where)
        where.symlink_to(isolated_agent_home / "gone" / "work")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(where))
    else:
        if "beside" in shape:
            claude.mkdir(parents=True)
            _connect(runner)
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        if os.access(locked, os.W_OK):
            locked.chmod(0o755)
            pytest.skip("this user can write a mode-555 directory (root)")
        where = locked / "claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(where))
    built = paths.db_path().exists()
    try:
        row = diagnostics._check_claude_code()
        welcome = first_run.probe_claude(sign_in=False, which=lambda _name: None)
        clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
        made = where.is_dir()
    finally:
        if (tmp_path / "locked").exists():
            (tmp_path / "locked").chmod(0o755)

    reason = str(json.loads(clicked.stdout)["detail"])
    assert clicked.exit_code == 1 and reason.startswith(f"can't create {where}: "), reason
    assert f"hooks cannot be written in {where}: {reason}" in row.detail, row
    assert [f.argv for f in fix_commands([row]) if f.argv[:2] == ("agents", "connect")] == []
    remedy = "remove the link at" if os.path.islink(where) else "yourself, or point"
    assert remedy in str(row.fix) and "disconnect" not in str(row.fix), row.fix
    assert welcome.refused == reason, welcome
    assert paths.db_path().exists() == built and not made, "nothing written before it"


def test_a_settings_json_linked_into_a_missing_folder_is_refused_before_anything_is_written(
    runner: CliRunner, claude_home: Path, tmp_path: Path
) -> None:
    """A settings.json linked into a dotfiles folder that moved: access() let connect
    through, and connect saved CLAUDE.md into the store and built ~/.aisquare before its
    write failed (review of #257). Refused first, in the same words everywhere."""
    if os.name == "nt":
        pytest.skip("a link to a missing folder is a POSIX shape here")
    (claude_home / "CLAUDE.md").write_text("# Prefs\nuse tabs\n", encoding="utf-8")
    settings = claude_home / "settings.json"
    target = tmp_path / "dotfiles" / "claude" / "settings.json"
    settings.symlink_to(target)

    refusal = agents_service.access("claude-code").connect
    row = diagnostics._check_claude_code()
    clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    built = paths.aisquare_home().exists()
    target.parent.mkdir(parents=True)
    linked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])

    reason = str(json.loads(clicked.stdout)["detail"])
    assert reason.startswith(f"can't write {settings}: it is a link to {target}"), reason
    assert refusal is not None and refusal.why == reason, refusal
    assert f"hooks cannot be written in {claude_home}: {reason}" in row.detail, row
    assert f"remove the link at {settings}" in str(row.fix), row.fix
    assert not built, "refused before the store was built or CLAUDE.md saved"
    assert linked.exit_code == 0 and agent_core.hooks_installed("claude-code"), linked.output
    assert target.is_file(), "control: once its folder is there, the link is written through"


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
        refusal = agents_service.connect_refusal("claude-code")
        row = diagnostics._check_claude_code()
        welcome = first_run.probe_claude(sign_in=False, which=lambda _name: None)
        clicked = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    finally:
        _cleared(claude_md)
    claude_md.write_text("# Prefs\ncafé\n", encoding="utf-8")
    readable = agents_service.connect_refusal("claude-code"), diagnostics._check_claude_code()

    reason = str(json.loads(clicked.stdout)["detail"])
    assert clicked.exit_code == 1 and reason.startswith(f"can't read {claude_md}: "), reason
    assert refusal == reason, "connect's own checks, in its own words"
    assert row.status is CheckStatus.warn, row
    assert f"hooks cannot be written in {claude_home}: {reason}" in row.detail, row
    assert row.fix == f"make {claude_md} UTF-8 text this user can read, then connect again"
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
    readable = agents_service.connect_refusal("claude-code", target)

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
    if refused == "UTF-16 CLAUDE.md":
        assert row.fix == f"make {claude_md} UTF-8 text this user can read, then connect again"
    else:
        assert row.fix == (
            f"make {settings_path} a JSON object this user can write, then connect again, or "
            "give its SessionStart and UserPromptSubmit hooks a timeout of at least 120 where "
            "that file is generated"
        )
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
    refusal = agents_service.connect_refusal("claude-code")
    row = diagnostics._check_claude_code()
    settings_path.write_text("{}\n", encoding="utf-8")  # readable, and the hooks are gone
    plain = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    missing = _hook_sites(agents_service.status("claude-code")[0])

    assert refusal is not None and "it is not valid JSON" in refusal, refusal
    assert damaged[0]["sites"] == [
        {
            "config_dir": str(claude_home),
            "hooks_installed": False,
            "hooks_off": None,
            "refused": refusal,
        }
    ], damaged
    assert cell == f"0/1 ok — cannot be written in {claude_home}: {refusal}", cell
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
