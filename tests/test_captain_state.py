"""The captain's small shared state: the home board, watermarks, busy flag, speech, brake, undo.

Every piece here crosses a process boundary — the MCP server the captain mounts
writes it, and the CLI verbs (T5), the voice page (T3) and the TUI (T4) read
it — so each is pinned through the files it lives in, not through a module
global a second process could never see.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core import paths, state_file
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.services.captain import state as captain_state


def _state_json() -> dict[str, object]:
    data = json.loads(paths.state_path().read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# --- state_file.modify_state: the read-modify-write the captain's keys need ------------


def test_modify_state_hands_the_current_value_to_the_change_and_keeps_other_keys() -> None:
    state_file.update_state("sidebar_width", 40)
    seen: list[object] = []

    def bump(current: object) -> object:
        seen.append(current)
        return {"n": 1} if current is None else {"n": 2}

    assert state_file.modify_state("captain_test", bump) == {"n": 1}
    assert state_file.modify_state("captain_test", bump) == {"n": 2}
    assert seen == [None, {"n": 1}]
    assert _state_json() == {"sidebar_width": 40, "captain_test": {"n": 2}}


def test_modify_state_drops_the_key_when_the_change_returns_none() -> None:
    state_file.update_state("captain_test", [1, 2])
    assert state_file.modify_state("captain_test", lambda current: None) is None
    assert "captain_test" not in state_file.read_state()


def test_modify_state_refuses_a_file_that_is_not_an_object_and_leaves_it() -> None:
    paths.ensure_home()
    paths.state_path().write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(state_file.StateUnwritableError, match="not a JSON object"):
        state_file.modify_state("captain_test", lambda current: 1)
    assert paths.state_path().read_text(encoding="utf-8") == "[1, 2]"


# --- the home board and the captain's per-board session ---------------------------------


def test_the_home_board_is_the_captured_project_row_for_the_aisquare_home() -> None:
    home = captain_state.home_project()
    root = paths.aisquare_home().resolve()
    assert home.root == root
    assert home.id == project_id_for(root)
    with store_session() as store:
        row = store.get_project(home.id)
        listed = [project.id for project in store.list_projects()]
    assert row is not None, "the home board needs a project row to hold events"
    assert home.id not in listed, "captured, never onboarded: it must not join the sidebar"


def test_the_captain_acts_as_one_virtual_session_per_board() -> None:
    home = captain_state.home_project()
    session_id = captain_state.ensure_session(home)
    assert session_id == captain_state.session_id_for(home.id)
    assert session_id == f"captain:{home.id.removeprefix('prj_')[:12]}"
    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None and session.role == "captain"
    assert session.project_id == home.id


# --- watermarks --------------------------------------------------------------------------


def test_a_watermark_is_kept_per_project_and_per_agent() -> None:
    assert captain_state.watermark("prj_a", None) is None
    captain_state.set_watermark("prj_a", None, 10)
    captain_state.set_watermark("prj_a", "coder-1", 25)
    captain_state.set_watermark("prj_b", None, 3)
    assert captain_state.watermark("prj_a", None) == 10
    assert captain_state.watermark("prj_a", "coder-1") == 25
    assert captain_state.watermark("prj_b", None) == 3
    assert captain_state.watermark("prj_b", "coder-1") is None
    assert _state_json()["captain_watermarks"] == {
        "prj_a": {"*": 10, "coder-1": 25},
        "prj_b": {"*": 3},
    }


# --- the busy flag -----------------------------------------------------------------------


def test_the_busy_flag_is_a_since_stamp_when_on_and_absent_when_off() -> None:
    assert captain_state.busy_since() is None
    before = datetime.now(tz=UTC)
    captain_state.set_busy(True)
    since = captain_state.busy_since()
    assert since is not None and before - timedelta(seconds=1) <= since <= datetime.now(tz=UTC)
    stored = _state_json()["captain_busy"]
    assert isinstance(stored, dict) and set(stored) == {"since"}
    captain_state.set_busy(False)
    assert captain_state.busy_since() is None
    assert "captain_busy" not in _state_json()


# --- the speech spool --------------------------------------------------------------------


def test_speech_is_spooled_oldest_first_taken_once_and_cleared() -> None:
    first = captain_state.enqueue_speech("item one")
    second = captain_state.enqueue_speech("item two")
    captain_state.enqueue_speech("item three")
    assert [item.text for item in captain_state.pending_speech()] == [
        "item one",
        "item two",
        "item three",
    ]
    taken = captain_state.take_speech()
    assert taken is not None and (taken.id, taken.text) == (first, "item one")
    assert captain_state.pending_speech()[0].id == second
    assert captain_state.clear_speech() == 2
    assert captain_state.pending_speech() == []
    assert captain_state.take_speech() is None
    assert list((paths.aisquare_home() / "captain" / "speech").iterdir()) == []


# --- the brake ---------------------------------------------------------------------------


def test_the_brake_stops_only_what_started_before_it() -> None:
    started = datetime.now(tz=UTC) - timedelta(seconds=5)
    assert not captain_state.brake_pulled_after(started)
    pulled = captain_state.pull_brake()
    assert captain_state.brake_pulled_after(started)
    assert not captain_state.brake_pulled_after(pulled + timedelta(seconds=1))


# --- the undo log ------------------------------------------------------------------------


def test_the_undo_log_pops_the_last_reversible_action_and_keeps_twenty() -> None:
    for n in range(25):
        captain_state.record_undo("claim", f"tsk_{n:02d}", "prj_a")
    last = captain_state.pop_undo()
    assert last is not None and (last.kind, last.task_id, last.project_id) == (
        "claim",
        "tsk_24",
        "prj_a",
    )
    remaining = _state_json()["captain_undo"]
    assert isinstance(remaining, list) and len(remaining) == 19
    for _ in range(19):
        assert captain_state.pop_undo() is not None
    assert captain_state.pop_undo() is None


# --- the ui socket path: one helper, always inside AF_UNIX's limit (13042 item 6) -------------


def test_the_ui_socket_lives_in_the_home_when_the_path_fits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "h"
    monkeypatch.setenv("AISQUARE_HOME", str(home))
    assert captain_state.ui_socket_path() == home.resolve() / "captain" / "ui.sock"


def test_a_home_too_long_for_a_unix_socket_gets_a_short_path_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / ("a" * 70) / ("b" * 40)
    second = tmp_path / ("a" * 70) / ("c" * 40)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)  # computed only: nothing is created
    monkeypatch.setenv("AISQUARE_HOME", str(first))
    one = captain_state.ui_socket_path()
    assert len(os.fsencode(str(one))) <= captain_state.UI_SOCKET_MAX
    assert one == captain_state.ui_socket_path(), "the same home, the same path — both sides agree"
    monkeypatch.setenv("AISQUARE_HOME", str(second))
    two = captain_state.ui_socket_path()
    assert len(os.fsencode(str(two))) <= captain_state.UI_SOCKET_MAX
    assert one != two, "two homes never share a receiver"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_the_short_path_s_folder_is_private_and_a_shared_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Path(tempfile.mkdtemp(prefix="asq", dir="/tmp"))  # short, and the test's own
    try:
        monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / ("a" * 70) / ("b" * 40)))
        monkeypatch.setattr(captain_state, "_short_root", lambda: runtime)
        path = captain_state.ui_socket_path(create=True)
        folder = path.parent
        assert folder == runtime / f"aisquare-{captain_state._user_tag()}"
        assert folder.is_dir() and (folder.stat().st_mode & 0o077) == 0
        os.chmod(folder, 0o777)
        with pytest.raises(OSError, match="not a private folder"):
            captain_state.ui_socket_path(create=True)
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


# --- no silent fail-soft (13038 item 3) ---------------------------------------------------


def test_a_spooled_line_that_cannot_be_read_is_logged_not_dropped_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = captain_state.enqueue_speech("item one")
    bad = captain_state.enqueue_speech("item two")
    real = captain_state._read_line

    def unreadable(path: Path) -> str:
        if path.stem == bad:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(captain_state, "_read_line", unreadable)
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.state"):
        pending = captain_state.pending_speech()
    assert [item.id for item in pending] == [good]
    assert bad in caplog.text and "Permission denied" in caplog.text


def test_the_short_path_does_not_follow_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binder (asq) and the dialer (the captain's server) run in different
    environments — tmux and cron do not carry XDG_RUNTIME_DIR or TMPDIR — so neither may
    move the path, or ui says "not running" while asq listens (fix-round review)."""
    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / ("a" * 70) / ("b" * 40)))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    plain = captain_state.ui_socket_path()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "t"))
    monkeypatch.setattr(tempfile, "tempdir", None)  # re-read TMPDIR
    assert captain_state.ui_socket_path() == plain


def test_a_malformed_watermark_is_logged_not_silently_unset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from aisquare.core import state_file

    state_file.update_state("captain_watermarks", {"prj_a": {"*": "twelve"}})
    with caplog.at_level(logging.WARNING, logger="aisquare.services.captain.state"):
        assert captain_state.watermark("prj_a", None) is None
    assert "captain_watermarks.prj_a.*" in caplog.text and "twelve" in caplog.text


def test_an_unreadable_state_file_is_an_error_not_an_empty_one() -> None:
    paths.ensure_home()
    paths.state_path().mkdir()  # a directory where the file should be: exists, cannot be read
    with pytest.raises(OSError):
        captain_state.watermark("prj_a", None)
