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
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare import statusline
from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as core
from aisquare.core import paths
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
        json.dumps(
            {"percent_left": 58.4, "accounts": 3, "labels": {"": "home"}, "updated_at": time.time()}
        )
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


def _fleet(updated_at: Any = ..., **fields: Any) -> dict[str, Any]:
    """A ``fleet.json`` as ``write_statusline_fleet`` writes it; ``updated_at`` left out
    when not given."""
    data: dict[str, Any] = {"percent_left": 3.4, "accounts": 3, "labels": {"": "home"}, **fields}
    if updated_at is not ...:
        data["updated_at"] = updated_at
    return data


def test_the_fleet_figure_is_as_old_as_the_usage_cache_serves_and_no_older() -> None:
    """Review of #298 (ui half, 2): fleet.json of any age was printed as the fleet's share
    left now. Its bound is the usage cache's own age, which this lean module cannot import."""
    assert statusline.FLEET_MAX_AGE_SECONDS == service.USAGE_MAX_AGE_SECONDS


@pytest.mark.parametrize(
    ("age", "shown"),
    [
        (0.0, True),
        (299.0, True),
        (301.0, False),
        (3 * 86400.0, False),  # the review's case: three days old, still 'fleet 3% left'
        (-60.0, True),  # written a moment after this record's moment: another process's clock
        (-3600.0, False),  # from the future: a clock set back
    ],
)
def test_a_fleet_figure_older_than_the_usage_cache_is_left_out_and_the_account_stays(
    age: float, shown: bool
) -> None:
    record = {"session_id": "s", "config_dir": None, "updated_at": 1_000_000.0}
    fleet = _fleet(updated_at=1_000_000.0 - age)
    said = statusline.line(record, fleet, 120)
    assert ("fleet 3% left" in said) is shown
    assert said.startswith("home")  # the account's name does not age


@pytest.mark.parametrize("updated_at", [None, "yesterday", True, float("nan")])
def test_a_fleet_file_with_no_readable_date_prints_no_figure(updated_at: Any) -> None:
    record = {"session_id": "s", "config_dir": None, "updated_at": 1_000_000.0}
    assert "fleet" not in statusline.line(record, _fleet(updated_at=updated_at), 120)
    assert "fleet" not in statusline.line(record, _fleet(), 120)  # no key at all


def test_a_stale_fleet_file_prints_the_line_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    directory = statusline.record_dir()
    directory.mkdir(parents=True)
    (directory / statusline.FLEET_FILE).write_text(
        json.dumps(_fleet(updated_at=time.time() - 3 * 86400))
    )
    _code, out = _run(SAMPLE, monkeypatch, env={"COLUMNS": "120"})
    assert out == "home · 5h 42% · wk 61% · ctx 34%\n"


def _script(
    tmp_path: Path, payload: bytes, *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """``python -m aisquare.statusline`` over real pipes, as Claude Code runs it: its bytes
    in, its bytes out, in the code page ``env`` gives Python (``io.StringIO`` has none)."""
    clean = {k: v for k, v in os.environ.items() if k not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
    clean.update({"COLUMNS": "120", **env})
    return subprocess.run(
        [sys.executable, "-m", "aisquare.statusline", *args],
        input=payload,
        capture_output=True,
        env=clean,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )


def _utf8(done: subprocess.CompletedProcess[bytes]) -> str:
    assert done.returncode == 0, done.stderr.decode("utf-8", errors="replace")
    return done.stdout.decode("utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize("code_page", ["cp932", "cp874", "cp1251", "cp1252"])
def test_the_status_line_speaks_utf_8_whatever_the_code_page_of_its_pipes(
    tmp_path: Path, code_page: str
) -> None:
    """Review of #298 (ui half, 1): a Windows ANSI code page cannot encode the separator
    (cp932, cp874) or the fleet's count sign (cp1251, cp874), so it raised on every message,
    against 'never fails, always exits 0'; and on cp1252 it wrote 0xB7 for the separator."""
    directory = statusline.record_dir()
    directory.mkdir(parents=True)
    (directory / statusline.FLEET_FILE).write_text(
        json.dumps(_fleet(updated_at=time.time(), percent_left=58.4))
    )
    done = _script(
        tmp_path,
        json.dumps(SAMPLE).encode("utf-8"),
        env={"PYTHONIOENCODING": code_page},
    )
    assert _utf8(done) == (
        "home \N{MIDDLE DOT} 5h 42% \N{MIDDLE DOT} wk 61% \N{MIDDLE DOT} "
        "fleet 58% left (3\N{MULTIPLICATION SIGN}) \N{MIDDLE DOT} ctx 34%\n"
    )
    assert b"\xc2\xb7" in done.stdout  # the separator as UTF-8, never the code page's 0xB7


@pytest.mark.parametrize("code_page", ["cp1252", "cp932"])
def test_a_non_ascii_cwd_and_label_go_through_whatever_the_code_page(
    tmp_path: Path, code_page: str
) -> None:
    """Review of #298 (ui half, 1): a cwd with a character outside the code page died in
    the decode (UnicodeDecodeError on cp1252), and a label outside it in the encode, which
    also stopped the record and so the usage ingest."""
    directory = statusline.record_dir()
    directory.mkdir(parents=True)
    (directory / statusline.FLEET_FILE).write_text(
        json.dumps(_fleet(updated_at=time.time(), labels={"": "\u5de5\u4f5c"}), ensure_ascii=False),
        encoding="utf-8",
    )
    payload = dict(SAMPLE, cwd="C:\\Users\\\u601d\\p", workspace={})
    done = _script(
        tmp_path,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        env={"PYTHONIOENCODING": code_page},
    )
    assert _utf8(done).startswith("\u5de5\u4f5c \N{MIDDLE DOT} 5h 42%")
    [file] = directory.glob("abc123-session.json")
    assert json.loads(file.read_text(encoding="utf-8"))["cwd"] == "C:\\Users\\\u601d\\p"


def test_wrap_reads_the_users_line_as_utf_8_whatever_the_locale(tmp_path: Path) -> None:
    """Review of #298 (ui half, 1): the wrapped command's output was decoded with the
    locale's encoding (text=True), so a status line printing a bullet died in
    UnicodeDecodeError under a non-UTF-8 locale, and ours with it."""
    emit = tmp_path / "emit.py"
    emit.write_text(
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.buffer.write('\u25cf mine\\n'.encode('utf-8'))\n",
        encoding="utf-8",
    )
    own = f"{sys.executable} {agent_core._quote(str(emit))}"
    done = _script(
        tmp_path,
        json.dumps(SAMPLE).encode("utf-8"),
        statusline.WRAP_FLAG,
        own,
        env={"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"},
    )
    assert _utf8(done).splitlines() == [
        "\u25cf mine",
        "claude \N{MIDDLE DOT} 5h 42% \N{MIDDLE DOT} wk 61% \N{MIDDLE DOT} ctx 34%",
    ]


# --------------------------------------------------------------------------- install


def _settings(config_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((config_dir / "settings.json").read_text())
    return data


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
    program = agent_core._split_command(command)[-1].lower()
    assert program.removesuffix(".exe").endswith("asq-statusline")  # Windows: …\asq-statusline.EXE

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
    assert not (ours / "settings.json").exists(), "ours made the file; it goes with ours"
    assert _settings(wrapped)["statusLine"] == own
    assert _settings(theirs)["statusLine"] == own


def test_uninstall_gives_back_the_file_byte_for_byte_and_no_file_where_there_was_none(
    tmp_path: Path,
) -> None:
    """Review of #362's walk: --uninstall re-wrote the file (an inline object came back as
    an indented block), and where there had been no settings.json it left '{}'."""
    none, compact, wrapped = tmp_path / "none", tmp_path / "compact", tmp_path / "wrapped"
    for directory, body in (
        (compact, '{"model":"opus",  "theme": "dark"}'),
        (wrapped, '{"statusLine": {"type": "command", "command": "my-line", "padding": 2}}\n'),
    ):
        directory.mkdir()
        (directory / "settings.json").write_text(body)
    originals = {d: (d / "settings.json").read_bytes() for d in (compact, wrapped)}

    agent_core.install_statusline(none)
    agent_core.install_statusline(compact)
    agent_core.install_statusline(wrapped, wrap=True)
    for directory in (none, compact, wrapped):
        assert agent_core.remove_statusline(directory) is True

    assert not (none / "settings.json").exists()
    for directory, original in originals.items():
        assert (directory / "settings.json").read_bytes() == original
    assert not list(tmp_path.rglob("*" + agent_core.STATUSLINE_KEPT)), "the copies go too"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.parametrize(("source", "copy"), [(0o600, 0o600), (0o644, 0o600), (0o640, 0o600)])
def test_the_kept_copy_is_never_readable_by_more_than_its_owner(
    tmp_path: Path, source: int, copy: int
) -> None:
    """Review of #362: the copy was made with the umask's mode, so a 0600 settings.json
    holding a key got a world-readable 0644 copy. The file's own bits, at most the owner's."""
    settings = tmp_path / "settings.json"
    settings.write_text('{"env": {"ANTHROPIC_API_KEY": "sk-dummy"}}')
    settings.chmod(source)
    agent_core.install_statusline(tmp_path)
    kept = tmp_path / ("settings.json" + agent_core.STATUSLINE_KEPT)
    assert kept.stat().st_mode & 0o777 == copy


def test_a_file_changed_since_the_install_keeps_its_change(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text('{"model": "opus"}')
    agent_core.install_statusline(tmp_path)
    settings = _settings(tmp_path)
    settings["theme"] = "dark"  # the user's edit, after ours went in
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    agent_core.remove_statusline(tmp_path)
    assert _settings(tmp_path) == {"model": "opus", "theme": "dark"}


class _Platform:
    """``core.agents``' own view of ``sys`` on another platform: only that module sees it, so
    the rest of the process (pytest, pathlib, the temp dir) stays what it is."""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def __getattr__(self, name: str) -> Any:
        return getattr(sys, name)


def _on(platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``core.agents`` as ``platform`` (the reviewer's repro of #298: ``agents.sys`` and the
    DACL probe stubbed, there being no Windows host to run on)."""
    monkeypatch.setattr(agent_core, "sys", _Platform(platform))
    monkeypatch.setattr(paths, "dacl_sddl", lambda path: "D:P(A;;FA;;;OW)")


#: The shape of a Windows user's command: a quoted path under Program Files.
QUOTED = '"C:\\Program Files\\nodejs\\node.exe" C:\\Users\\me\\sl.js'


def _own(config_dir: Path, command: str, **keys: Any) -> bytes:
    """A settings.json holding the user's own status line ``command``; its bytes."""
    config_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"statusLine": {"type": "command", "command": command, **keys}}, indent=1)
    (config_dir / "settings.json").write_text(body, encoding="utf-8")
    return (config_dir / "settings.json").read_bytes()


def test_on_windows_a_quoted_command_is_not_wrapped_and_nothing_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #298 (core half): --wrap put the quoted path inside double quotes it could not
    nest (``asq-statusline --wrap ""C:\\Program Files\\…" sl.js"``), read it back as '' and
    --uninstall wrote that over the user's line and deleted the copy. A command that cannot be
    read back exactly is left as it is, and the answer says why."""
    _on("win32", monkeypatch)
    original = _own(tmp_path, QUOTED, padding=2)

    entry = agent_core.install_statusline(tmp_path, wrap=True)

    assert (entry.kind, entry.theirs) == ("foreign", QUOTED)
    assert entry.note is not None and "double quote" in entry.note
    assert (tmp_path / "settings.json").read_bytes() == original
    assert not (tmp_path / ("settings.json" + agent_core.STATUSLINE_KEPT)).exists()
    assert agent_core.statusline_entry(tmp_path).kind == "foreign"
    assert agent_core.remove_statusline(tmp_path) is False
    assert (tmp_path / "settings.json").read_bytes() == original


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize(
    "command",
    [
        "my-line",
        "~/bin/my-line --short",
        "C:\\tools\\sl.cmd",
        "C:\\Program Files\\x\\sl.cmd",  # spaces, no quote: wrapped in ours as one argument
        "node 'a b.js' --x",
        "echo $HOME && date",
        "sl.cmd \\",  # ends in a backslash
        "sl.cmd\t--tabbed",
        "\u5de5\u4f5c.cmd \u601d",
        QUOTED,
        'node -e "console.log(1)"',
        "it's",
        "",
    ],
)
def test_a_command_is_wrapped_only_when_it_goes_back_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, command: str
) -> None:
    """Whatever the command: either it is wrapped and read back as exactly itself and the
    uninstall gives the file back byte for byte, or it is left alone with a reason."""
    _on(platform, monkeypatch)
    original = _own(tmp_path, command, padding=2)

    entry = agent_core.install_statusline(tmp_path, wrap=True)

    if entry.kind == "wrapped":
        assert entry.note is None
        assert agent_core.statusline_entry(tmp_path).theirs == command
        assert agent_core.remove_statusline(tmp_path) is True
    else:
        assert (entry.kind, entry.theirs) == ("foreign", command)
        assert entry.note
        assert agent_core.remove_statusline(tmp_path) is False
    assert (tmp_path / "settings.json").read_bytes() == original
    assert not (tmp_path / ("settings.json" + agent_core.STATUSLINE_KEPT)).exists()


@pytest.mark.parametrize(("platform", "wraps"), [("linux", True), ("win32", False)])
def test_a_command_with_a_double_quote_is_wrapped_where_posix_quoting_inverts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, wraps: bool
) -> None:
    """Pins which side of the line the quoted command is on: POSIX quoting inverts (the
    review found it byte-identical), Windows' cannot nest a ``"``."""
    _on(platform, monkeypatch)
    _own(tmp_path, QUOTED)
    assert (agent_core.install_statusline(tmp_path, wrap=True).kind == "wrapped") is wraps


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_uninstall_gives_the_users_line_back_from_the_copy_whatever_the_parsing_made_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """What an older build wrote (and what a hand edit can write): a wrapped command whose
    quoting reads back as something else, over a copy that holds the user's own. The copy's
    command is the one that goes back, and the file is the copy's bytes."""
    _on(platform, monkeypatch)
    original = _own(tmp_path, QUOTED, padding=2)
    kept = tmp_path / ("settings.json" + agent_core.STATUSLINE_KEPT)
    kept.write_bytes(original)
    garbled = f'asq-statusline --wrap "{QUOTED}"'  # the old build's: an unescaped quote inside
    body = json.loads(original)
    body["statusLine"]["command"] = garbled
    (tmp_path / "settings.json").write_text(json.dumps(body), encoding="utf-8")
    assert agent_core.statusline_entry(tmp_path).theirs != QUOTED, "the premise: it is misread"

    assert agent_core.remove_statusline(tmp_path) is True

    assert (tmp_path / "settings.json").read_bytes() == original
    assert not kept.exists()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_uninstall_after_a_change_keeps_the_change_and_still_gives_the_command_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    _on(platform, monkeypatch)
    original = _own(tmp_path, QUOTED, padding=2)
    (tmp_path / ("settings.json" + agent_core.STATUSLINE_KEPT)).write_bytes(original)
    body = json.loads(original)
    body["statusLine"]["command"] = f'asq-statusline --wrap "{QUOTED}"'
    body["theme"] = "dark"  # the user's edit, after ours went in
    (tmp_path / "settings.json").write_text(json.dumps(body), encoding="utf-8")

    assert agent_core.remove_statusline(tmp_path) is True

    assert _settings(tmp_path) == {
        "statusLine": {"type": "command", "command": QUOTED, "padding": 2},
        "theme": "dark",
    }


@pytest.mark.parametrize("copy", [None, b"{not json", b"[]", b'{"model": "opus"}'])
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_without_a_usable_copy_the_uninstall_gives_back_what_the_wrapped_string_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, copy: bytes | None
) -> None:
    """The old path stays where the copy is missing, unparseable, not an object, or holds no
    status line of the user's: the command read out of ours, the other keys as they are."""
    _on(platform, monkeypatch)
    _own(tmp_path, "my-line --short", padding=2)
    assert agent_core.install_statusline(tmp_path, wrap=True).kind == "wrapped"
    kept = tmp_path / ("settings.json" + agent_core.STATUSLINE_KEPT)
    kept.unlink()
    if copy is not None:
        kept.write_bytes(copy)

    assert agent_core.remove_statusline(tmp_path) is True

    assert _settings(tmp_path)["statusLine"] == {
        "type": "command",
        "command": "my-line --short",
        "padding": 2,
    }


def test_the_accounts_command_says_why_a_quoted_command_was_not_wrapped(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on("win32", monkeypatch)
    original = _own(core.default_config_dir(), QUOTED)

    result = runner.invoke(app, ["--json", "accounts", "statusline", "--install", "--wrap"])

    assert result.exit_code == 0, result.output
    [site] = [s for s in json.loads(result.stdout) if s["slot"] == core.DEFAULT_SLOT]
    assert (site["state"], site["changed"], site["theirs"]) == ("foreign", False, QUOTED)
    assert "double quote" in site["note"]
    assert (core.default_config_dir() / "settings.json").read_bytes() == original

    shown = runner.invoke(app, ["accounts", "statusline", "--install", "--wrap"])
    assert "not wrapped" in shown.stdout and "left as it is" in shown.stdout
    assert "--install --wrap keeps it" not in shown.stdout, "it was asked, and refused"


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
    assert not (second.config_dir / "settings.json").exists(), "ours made it; it goes"


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


def test_a_reading_without_its_week_never_shadows_the_endpoints_spent_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #362: a record with no seven_day was ingested as a sample with no week, so
    the cache served an account whose week the endpoint reads spent as one with room
    (headroom 100 -> 10, no fetch). Both windows, or the endpoint is asked."""
    monkeypatch.setattr(service, "_now", lambda: NOW)
    account = _slot("a@example.com", "tok-a")
    _record("s-a", config_dir=str(account.config_dir), seven_day=None)
    fetch = _Usage({"tok-a": _payload(10.0, week=100.0)})

    readings = service.cached_usage([account], now=NOW, fetch=fetch)

    assert fetch.calls == ["tok-a"], "the endpoint was asked"
    assert readings[account.slot].week_percent == 100.0
    assert service.ingest_statusline([account], now=NOW) == 0


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
