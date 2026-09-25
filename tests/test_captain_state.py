"""The captain's small shared state: the home board, watermarks, busy flag, speech, brake, undo.

Every piece here crosses a process boundary — the MCP server the captain mounts
writes it, and the CLI verbs (T5), the voice page (T3) and the TUI (T4) read
it — so each is pinned through the files it lives in, not through a module
global a second process could never see.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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
