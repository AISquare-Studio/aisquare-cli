"""The Claude Code status line and the sessions it reports (#287, slice 2c).

``asq-statusline`` (``aisquare.statusline``) runs on every assistant message of every session
that carries it, so the first thing pinned is what it IMPORTS: the standard library only.
Then the payload it reads (the documented sample, and one from an API-key session with no
``rate_limits``), the install that never replaces a status line the user set, the uninstall
that takes out only ours, the ingestion that spares the usage endpoint a request, and the
device-wide ``accounts sessions``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare import statusline
from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.services import claude_accounts as service
from aisquare.services import lifecycle
from tests.test_claude_accounts import NOW
from tests.test_usage_aware_accounts import _payload, _slot, _Usage

#: The documented stdin payload (https://code.claude.com/docs/en/statusline), trimmed to
#: what is read, from a Max login after its first answer.
SAMPLE: dict[str, Any] = {
    "hook_event_name": "Status",
    "session_id": "abc123-session",
    "transcript_path": "/home/u/.claude/projects/-work-api/abc123-session.jsonl",
    "cwd": "/work/api",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
    "workspace": {"current_dir": "/work/api", "project_dir": "/work/api"},
    "cost": {"total_cost_usd": 0.42},
    "context_window": {"used_percentage": 34.2},
    "rate_limits": {
        "five_hour": {"used_percentage": 42.0, "resets_at": NOW.timestamp() + 3 * 3600},
        "seven_day": {"used_percentage": 61.4, "resets_at": NOW.timestamp() + 4 * 86400},
    },
}

FORBIDDEN = ("typer", "rich", "textual", "pydantic", "sqlite3")
"""What the status line must never import: each costs every assistant message of every session."""


def _run(
    payload: Any, monkeypatch: pytest.MonkeyPatch, *, env: dict[str, str] | None = None
) -> tuple[int, str]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    code = statusline.main([])
    return code, out.getvalue()


def _record(session_id: str, **fields: Any) -> Path:
    """A record as ``asq-statusline`` writes it, ``fields`` over a fresh five-hour reading."""
    record: dict[str, Any] = {
        "session_id": session_id,
        "cwd": "/work/api",
        "model": "claude-opus-5-5",
        "config_dir": None,
        "fleet_agent": None,
        "five_hour": {"used_percentage": 30.0, "resets_at": NOW.timestamp() + 3600},
        "seven_day": {"used_percentage": 50.0, "resets_at": NOW.timestamp() + 86400},
        "context_percent": 12.0,
        "cost_usd": 0.1,
        "updated_at": NOW.timestamp() - 30,
    }
    record.update(fields)
    return statusline.write_record(statusline.record_dir(), record)


# --------------------------------------------------------------------------- the script


def test_the_status_line_imports_the_standard_library_only() -> None:
    """Pinned like tests/test_import_cost_of_the_integration.py: by which modules load,
    never by milliseconds."""
    probe = (
        "import sys; import aisquare.statusline; "
        f"print([m for m in {FORBIDDEN!r} if m in sys.modules])"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    assert done.stdout.strip() == "[]"


def test_the_documented_payload_writes_one_record_and_prints_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot_dir = core.accounts_root() / "3"
    code, out = _run(
        SAMPLE,
        monkeypatch,
        env={"CLAUDE_CONFIG_DIR": str(slot_dir), "AISQUARE_FLEET_AGENT": "agt_1", "COLUMNS": "120"},
    )
    assert code == 0
    assert out == "acct 3 · 5h 42% · wk 61% · ctx 34%\n"
    [file] = statusline.record_dir().glob("*.json")
    record = json.loads(file.read_text())
    assert file.name == "abc123-session.json"
    assert record["config_dir"] == str(slot_dir)
    assert record["fleet_agent"] == "agt_1"
    assert record["five_hour"]["used_percentage"] == 42.0
    assert record["model"] == "claude-opus-5-5"
    assert "transcript_path" not in record  # what it keeps is listed, nothing else


def test_an_api_key_session_with_no_rate_limits_prints_without_the_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {key: value for key, value in SAMPLE.items() if key != "rate_limits"}
    code, out = _run(payload, monkeypatch, env={"COLUMNS": "80"})
    assert code == 0
    assert out == "claude · ctx 34%\n"


def test_a_payload_it_cannot_read_prints_nothing_and_exits_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _run("not json {", monkeypatch) == (0, "")
    assert _run({"no": "session"}, monkeypatch) == (0, "")
    assert _run({"session_id": "../../etc/passwd"}, monkeypatch) == (0, "")
    assert not list(statusline.record_dir().glob("*.json"))


def test_the_line_names_the_fleet_and_gives_way_from_its_end_to_fit_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = statusline.record_dir()
    directory.mkdir(parents=True)
    (directory / statusline.FLEET_FILE).write_text(
        json.dumps({"percent_left": 58.4, "accounts": 3, "labels": {"": "home"}})
    )
    _code, wide = _run(SAMPLE, monkeypatch, env={"COLUMNS": "120"})
    assert wide == "home · 5h 42% · wk 61% · fleet 58% left (3\N{MULTIPLICATION SIGN}) · ctx 34%\n"
    _code, narrow = _run(SAMPLE, monkeypatch, env={"COLUMNS": "24"})
    assert narrow == "home · 5h 42% · wk 61%\n"
    _code, tiny = _run(SAMPLE, monkeypatch, env={"COLUMNS": "3"})
    assert tiny == "hom\n"


def test_wrap_prints_the_users_own_line_first(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = json.dumps(SAMPLE)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setenv("COLUMNS", "80")
    own = f"{sys.executable} -c \"import sys,json; print('mine', json.load(sys.stdin)['cwd'])\""
    assert statusline.main([statusline.WRAP_FLAG, own]) == 0
    assert out.getvalue().splitlines() == ["mine /work/api", "claude · 5h 42% · wk 61% · ctx 34%"]


# --------------------------------------------------------------------------- install


def _settings(config_dir: Path) -> dict[str, Any]:
    return json.loads((config_dir / "settings.json").read_text())


def test_install_sets_ours_only_where_none_is_set_and_wrap_keeps_the_users(
    tmp_path: Path,
) -> None:
    empty, theirs = tmp_path / "empty", tmp_path / "theirs"
    theirs.mkdir()
    own = {"type": "command", "command": "~/bin/my-line --short", "padding": 2}
    (theirs / "settings.json").write_text(json.dumps({"model": "opus", "statusLine": own}))

    assert agent_core.install_statusline(empty).kind == "ours"
    assert agent_core.install_statusline(empty).kind == "ours"  # again: nothing to do
    command = _settings(empty)["statusLine"]["command"]
    assert agent_core._split_command(command)[-1].endswith("asq-statusline")

    refused = agent_core.install_statusline(theirs)
    assert (refused.kind, refused.theirs) == ("foreign", own["command"])
    assert _settings(theirs)["statusLine"] == own  # never replaced without --wrap

    wrapped = agent_core.install_statusline(theirs, wrap=True)
    assert (wrapped.kind, wrapped.theirs) == ("wrapped", own["command"])
    entry = _settings(theirs)["statusLine"]
    assert entry["padding"] == 2 and _settings(theirs)["model"] == "opus"
    assert agent_core._split_command(entry["command"])[1:] == ["--wrap", own["command"]]


def test_uninstall_takes_out_ours_and_puts_a_wrapped_one_back_as_it_was(tmp_path: Path) -> None:
    ours, wrapped, theirs = tmp_path / "ours", tmp_path / "wrapped", tmp_path / "theirs"
    own = {"type": "command", "command": "my-line", "padding": 1}
    for directory in (wrapped, theirs):
        directory.mkdir()
        (directory / "settings.json").write_text(json.dumps({"statusLine": own}))
    agent_core.install_statusline(ours)
    agent_core.install_statusline(wrapped, wrap=True)

    assert agent_core.remove_statusline(ours) is True
    assert agent_core.remove_statusline(wrapped) is True
    assert agent_core.remove_statusline(theirs) is False
    assert "statusLine" not in _settings(ours)
    assert _settings(wrapped)["statusLine"] == own
    assert _settings(theirs)["statusLine"] == own


def test_accounts_statusline_installs_on_each_account_and_says_so(runner: CliRunner) -> None:
    second = core.create_account()
    own = {"type": "command", "command": "my-line"}
    core.default_config_dir().mkdir(parents=True, exist_ok=True)
    (core.default_config_dir() / "settings.json").write_text(json.dumps({"statusLine": own}))

    result = runner.invoke(app, ["--json", "accounts", "statusline", "--install"])
    assert result.exit_code == 0, result.output
    sites = {site["slot"]: site for site in json.loads(result.stdout)}
    assert sites[core.DEFAULT_SLOT]["state"] == "foreign"
    assert sites[core.DEFAULT_SLOT]["changed"] is False
    assert sites[second.slot]["state"] == "ours" and sites[second.slot]["changed"] is True
    assert statusline.record_dir().is_dir()  # fleet.json is written from now on

    shown = runner.invoke(app, ["accounts", "statusline"])
    assert "your own, left alone" in shown.stdout and "asq-statusline" in shown.stdout

    removed = runner.invoke(app, ["--json", "accounts", "statusline", "--uninstall"])
    states = {site["slot"]: site["state"] for site in json.loads(removed.stdout)}
    assert states == {core.DEFAULT_SLOT: "foreign", second.slot: "none"}


def test_aisquare_uninstall_removes_the_status_line_it_set() -> None:
    second = core.create_account()
    agent_core.install_statusline(second.config_dir)
    plan = lifecycle.uninstall_plan()
    assert second.config_dir in plan.statuslines
    lifecycle.uninstall(plan)
    assert "statusLine" not in _settings(second.config_dir)


# --------------------------------------------------------------------------- the readings


def test_a_status_line_reading_is_served_from_the_cache_and_spares_a_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "_now", lambda: NOW)
    account = _slot("a@example.com", "tok-a")
    _record("s-a", config_dir=str(account.config_dir), updated_at=NOW.timestamp() - 30)
    fetch = _Usage({"tok-a": _payload(90.0)})

    readings = service.cached_usage([account], now=NOW, fetch=fetch)

    assert fetch.calls == []  # the endpoint was not asked
    assert readings[account.slot].session_percent == 30.0
    assert readings[account.slot].week_percent == 50.0
    with store_session() as store:
        [sample] = store.usage_samples(account.slot, since=NOW - timedelta(hours=1))
    assert sample.token_fingerprint is not None


def test_an_old_or_already_recorded_reading_is_not_ingested_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _slot("a@example.com", "tok-a")
    _record("s-a", config_dir=str(account.config_dir), updated_at=NOW.timestamp() - 30)
    assert service.ingest_statusline([account], now=NOW) == 1
    assert service.ingest_statusline([account], now=NOW) == 0
    _record("s-old", config_dir=str(account.config_dir), updated_at=NOW.timestamp() - 3600)
    assert service.ingest_statusline([account], now=NOW, max_age=300) == 0


def test_two_sessions_on_two_slots_are_listed_with_their_slots(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_now", lambda: NOW)
    second = core.create_account()
    _record("plain-1", config_dir=None)
    _record("slot2-1", config_dir=str(second.config_dir), cwd="/elsewhere")
    stale = _record("gone-1", updated_at=NOW.timestamp() - 2 * 86400)

    result = runner.invoke(app, ["--json", "accounts", "sessions", "--since", "15m"])

    assert result.exit_code == 0, result.output
    listed = {row["session_id"]: row for row in json.loads(result.stdout)}
    assert set(listed) == {"plain-1", "slot2-1"}
    assert listed["plain-1"]["slot"] == core.DEFAULT_SLOT
    assert listed["slot2-1"]["slot"] == second.slot
    assert listed["slot2-1"]["account"] == core.label(second)
    assert not stale.exists()  # older than a day: deleted when read
    table = runner.invoke(app, ["accounts", "sessions"])
    assert "slot2-1"[:8] in table.stdout and "/elsewhere" in table.stdout


def test_the_fleet_file_is_written_only_where_the_status_line_is_in_use() -> None:
    account = core.default_account()
    summary = service.fleet_headroom([account], {})
    service.write_statusline_fleet(summary, [account], now=NOW)
    assert not statusline.record_dir().exists()
    statusline.record_dir().mkdir(parents=True)
    service.write_statusline_fleet(summary, [account], now=NOW)
    fleet = json.loads((statusline.record_dir() / statusline.FLEET_FILE).read_text())
    assert fleet["labels"][os.path.realpath(account.config_dir)] == core.label(account)
    assert fleet["labels"][""] == core.label(account)
