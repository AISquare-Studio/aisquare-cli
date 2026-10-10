"""What a phone's write may hold, and how each refusal reads (review of #243, round 5).

Text that reaches a pane, a board or a terminal is held to one rule per path: what it
would press, reorder or run there is refused before anything is sent or written. A ref
that names nothing says which field named it, and a path the operating system refuses
is a refusal of the request, never ``write_failed`` or an ambiguity.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo, TeamSession
from aisquare.services import project as project_service
from aisquare.services import remote_server
from aisquare.services.remote_server import (
    RequestError,
    Runtime,
    build_app,
    check_note_text,
    check_remote_text,
    live_writes,
)
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock


def test_a_quick_answer_in_words_refuses_a_tab_as_typed_text_does() -> None:
    """A quick answer's words are typed into the pane as send-keys' text is, and a tab
    there is the Tab key (review of #243, round 5)."""
    from aisquare.services.remote_needs import _needs_answer_body

    with pytest.raises(RequestError) as refused:
        _needs_answer_body({"id": "nd_1", "text": "a\tb"})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == (
        "'text' holds the control character U+0009 — send the pad's Tab key instead"
    )


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (
            "hi \x9d52;c;cHduZWQ=\x9c end",
            "the control character U+009D — no key of the pad sends it",
        ),
        ("hi \x9b2J end", "the control character U+009B — no key of the pad sends it"),
        ("approve \u202edeleted\u202c", "the bidi control U+202E — it reorders how the text"),
        ("ok \u2066x\u2069", "the bidi control U+2066 — it reorders how the text"),
    ],
    ids=["c1-osc", "c1-csi", "rlo", "lri"],
)
def test_a_tell_holding_a_c1_or_bidi_control_is_refused(text: str, said: str) -> None:
    """A tell in mode ``auto`` may be filed as a board note, which ``aisquare board`` prints
    as it came, past Rich: its C1 controls and overrides were let through where the
    note's ``to`` refused them (sweep 3 of #243). One rule for every tell."""
    from aisquare.services.remote_actions import action_tell_text

    with pytest.raises(RequestError) as refused:
        action_tell_text({"text": text})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message.startswith(f"'text' holds {said}"), refused.value.message


def test_typed_text_holding_a_c1_control_is_refused() -> None:
    """A C1 control is a control character as an ASCII one is: nothing a phone types needs
    one, and the audit line's ``text=Nch`` could not show it (sweep 3 of #243)."""
    with pytest.raises(RequestError) as refused:
        check_remote_text("yes\x9bA")
    assert refused.value.message == (
        "'text' holds the control character U+009B — no key of the pad sends it"
    )


@pytest.mark.parametrize("char", ["\u202e", "\u2068"], ids=["rlo", "fsi"])
def test_a_switch_reason_holding_a_bidi_control_is_refused(char: str) -> None:
    """The board's ``switched`` event repeats the reason, and ``aisquare board`` prints it as
    it came: an override let through made the line read in another order (sweep 3 of
    #243)."""
    from aisquare.services.remote_actions import action_switch_reason

    with pytest.raises(RequestError) as refused:
        action_switch_reason({"reason": f"limit {char}hit"})
    assert refused.value.message == (
        f"'reason' holds U+{ord(char):04X}, a bidi control — a reason is one line of text"
    )


RIGHT_TO_LEFT_AND_JOINED = (
    "\u05e9\u05dc\u05d5\u05dd \u200f\u05e2\u05d5\u05dc\u05dd, \u0645\u0631\u062d\u0628\u0627, "
    "a\u00a0b, \U0001f469\u200d\U0001f4bb and e\u0301"
)
"""Text in two right-to-left scripts with a right-to-left mark, a no-break space, the joiner
inside an emoji and a combining accent: none of it is a control, and all of it is kept."""


def test_a_note_a_tell_and_a_reason_keep_right_to_left_text_and_what_joins_a_line() -> None:
    from aisquare.services.remote_actions import action_switch_reason, action_tell_text

    check_note_text(RIGHT_TO_LEFT_AND_JOINED, "text")
    check_remote_text(RIGHT_TO_LEFT_AND_JOINED)
    assert action_tell_text({"text": RIGHT_TO_LEFT_AND_JOINED}) == RIGHT_TO_LEFT_AND_JOINED
    assert action_switch_reason({"reason": RIGHT_TO_LEFT_AND_JOINED}) == RIGHT_TO_LEFT_AND_JOINED


# --- a ref that names nothing says which field sent it ------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory of the test's own: ``Path.home()`` and ``~`` both point here."""
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    return make_runtime()


def _project(home: Path, where: str) -> ProjectInfo:
    """A repository under ``home``, added on purpose as ``project add`` adds it."""
    root = home / where
    (root / ".git").mkdir(parents=True)
    with store_session() as store:
        return store.onboard_project(
            ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
        )


def _session(project: ProjectInfo, session_id: str) -> None:
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(id=session_id, project_id=project.id, started_at=now, last_seen_at=now)
        )


@pytest.fixture
def phone(
    home: Path, runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """A phone with writes on, over the real board services and store, in project alpha."""
    alpha = _project(home, "code/alpha")
    monkeypatch.chdir(alpha.root)
    sources = remote_server.live_sources()
    app = build_app(runtime, sources=sources, writes=live_writes(), dist_dir=tmp_path)
    client = make_client(app)
    unlock(client, runtime)
    runtime.set_allow_write(True)
    return client


@pytest.mark.parametrize(
    ("route", "body", "message"),
    [
        (
            "note",
            {"text": "hi", "as": "ses_nope"},
            "no session matches 'ses_nope' (the 'as' field)",
        ),
        (
            "note",
            {"text": "hi", "task": "tsk_nope"},
            "no task matches 'tsk_nope' (the 'task' field)",
        ),
        (
            "note",
            {"text": "hi", "as": "xyz", "task": "xyz"},
            "no session matches 'xyz' (the 'as' field)",
        ),
        (
            "note",
            {"text": "hi", "as": "ses_a", "task": "ses_a"},
            "no task matches 'ses_a' (the 'task' field)",
        ),
        ("task/claim", {"ref": "tsk_nope"}, "no task matches 'tsk_nope' (the 'ref' field)"),
        (
            "task/claim",
            {"ref": "{task}", "as": "ses_nope"},
            "no session matches 'ses_nope' (the 'as' field)",
        ),
        ("task/done", {"ref": "tsk_nope"}, "no task matches 'tsk_nope' (the 'ref' field)"),
        (
            "task/done",
            {"ref": "{task}", "as": "ses_nope"},
            "no session matches 'ses_nope' (the 'as' field)",
        ),
    ],
    ids=[
        "note-as",
        "note-task",
        "note-as-and-task-alike-neither",
        "note-as-and-task-alike-a-session",
        "claim-ref",
        "claim-as",
        "done-ref",
        "done-as",
    ],
)
def test_a_ref_that_names_nothing_is_404_in_a_sentence_naming_its_field(
    phone: TestClient,
    runtime: Runtime,
    route: str,
    body: dict[str, str],
    message: str,
) -> None:
    """The board's services raise ``KeyError(ref)``, and the message was the ref in quotes,
    ``"'ses_nope'"``: with ``as`` and ``task`` alike, which one named nothing could not be
    told (sweep 3 of #243). ``ses_a`` is a session and no task."""
    from aisquare.services import team as team_service

    (alpha,) = (p for p in project_service.list_projects() if p.root.name == "alpha")
    _session(alpha, "ses_a")
    task, _added = team_service.add_task("ship it")
    sent = {key: value.format(task=task.id) for key, value in body.items()}
    response = phone.post(f"{base(runtime)}/api/{route}", json=sent)
    assert (response.status_code, response.json()) == (
        404,
        {"error": "not_found", "message": message},
    )


def test_a_note_on_a_task_of_another_projects_board_is_refused_invalid(
    phone: TestClient, runtime: Runtime, home: Path
) -> None:
    """``asq note --task`` refuses a task of another project's board; from the phone that
    refusal fell to 400 ``write_failed``, "the write failed", where nothing had (sweep 3
    of #243, the class of sweep 2's ``project_busy``)."""
    from aisquare.services import team as team_service

    beta = _project(home, "code/beta")
    task, _added = team_service.add_task("beta's task", cwd=beta.root)
    response = phone.post(f"{base(runtime)}/api/note", json={"text": "hi", "task": task.id})
    assert (response.status_code, response.json()) == (
        400,
        {
            "error": "invalid",
            "message": f"--task {task.id}: that task belongs to another project's board",
        },
    )
    _alpha, _sessions, _tasks, events = team_service.board_data()
    assert events == [], "nothing was posted to the note's own board either"
