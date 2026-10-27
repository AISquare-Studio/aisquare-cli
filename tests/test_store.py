"""SQLite store: CRUD, pool scoping, soft-delete, prefix lookup and search."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import paths
from aisquare.core import store as store_module
from aisquare.core.ids import new_entry_id
from aisquare.core.store import (
    SCHEMA_VERSION,
    AmbiguousIdError,
    ContextStore,
    GroupSettings,
    is_corrupt_error,
    open_store,
    store_session,
)
from aisquare.models import CheckStatus, ContextEntry, Pool, ProjectInfo

PROJECT = ProjectInfo(id="prj_test", root=Path("/tmp/example-project"), linked_repos=[])


@pytest.fixture
def store() -> Iterator[ContextStore]:
    with store_session() as opened:
        opened.ensure_project(PROJECT)
        yield opened


def _entry(
    text: str = "a fact",
    *,
    pool: Pool = "user",
    project_id: str | None = None,
    tags: list[str] | None = None,
) -> ContextEntry:
    now = datetime.now(tz=UTC)
    return ContextEntry(
        id=new_entry_id(),
        pool=pool,
        project_id=project_id,
        text=text,
        tags=tags or [],
        source="test",
        created_at=now,
        updated_at=now,
    )


def test_add_returns_entry_and_get_round_trips(store: ContextStore) -> None:
    entry = store.add(_entry("prefer tabs", tags=["style", "python"]))
    fetched = store.get(entry.id)
    assert fetched == entry
    assert fetched is not None and fetched.tags == ["style", "python"]


def test_get_resolves_unambiguous_prefix(store: ContextStore) -> None:
    entry = store.add(_entry())
    assert store.get(entry.id[:28]) == entry


def test_get_unknown_returns_none(store: ContextStore) -> None:
    store.add(_entry())
    assert store.get("ctx_doesnotexist") is None


def test_get_ambiguous_prefix_raises(store: ContextStore) -> None:
    store.add(_entry("one"))
    store.add(_entry("two"))
    with pytest.raises(AmbiguousIdError):
        store.get("ctx")  # the shared prefix matches every entry


def test_list_filters_by_pool(store: ContextStore) -> None:
    store.add(_entry("global", pool="user"))
    store.add(_entry("local", pool="project", project_id=PROJECT.id))
    assert [e.text for e in store.entries("user")] == ["global"]
    assert [e.text for e in store.entries("project", project_id=PROJECT.id)] == ["local"]


def test_list_in_scope_is_user_plus_current_project(store: ContextStore) -> None:
    store.add(_entry("global", pool="user"))
    store.add(_entry("local", pool="project", project_id=PROJECT.id))
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[]))
    store.add(_entry("elsewhere", pool="project", project_id="prj_other"))
    in_scope = {e.text for e in store.entries(project_id=PROJECT.id)}
    assert in_scope == {"global", "local"}  # excludes the other project


def test_update_changes_fields_and_bumps_timestamp(store: ContextStore) -> None:
    entry = store.add(_entry("typo heer", tags=["old"]))
    updated = store.update(entry.id, text="typo here", tags=["new"])
    assert updated.text == "typo here"
    assert updated.tags == ["new"]
    assert updated.created_at == entry.created_at
    assert updated.updated_at >= entry.updated_at


def test_update_unknown_raises(store: ContextStore) -> None:
    with pytest.raises(KeyError):
        store.update("ctx_missing", text="x")


def test_delete_is_a_soft_tombstone(store: ContextStore) -> None:
    entry = store.add(_entry("temporary"))
    store.delete(entry.id)
    assert store.get(entry.id) is None
    assert store.entries("user") == []
    # The row survives as a tombstone so the deletion can sync later.
    raw = sqlite3.connect(str(_db_path()))
    try:
        deleted_at = raw.execute(
            "SELECT deleted_at FROM entry WHERE id = ?", (entry.id,)
        ).fetchone()[0]
    finally:
        raw.close()
    assert deleted_at is not None


def test_search_matches_prefix_tokens(store: ContextStore) -> None:
    store.add(_entry("prefer pytest over unittest"))
    store.add(_entry("use ruff for linting"))
    assert [e.text for e in store.search("pytest")] == ["prefer pytest over unittest"]
    assert [e.text for e in store.search("lint")] == ["use ruff for linting"]
    assert store.search("nonexistent") == []
    assert store.search("") == []


def test_search_reads_fts5_keywords_as_plain_words(store: ContextStore) -> None:
    """#269: an upper-case bareword `AND`, `OR` or `NOT` is an FTS5 operator, so `AND*` was
    a syntax error that crashed the search. Each word is a quoted string now: a keyword is
    a word to find like any other, and finds the entries that hold it."""
    store.add(_entry("rock and roll"))
    store.add(_entry("near the shore"))
    assert [e.text for e in store.search("AND")] == ["rock and roll"]
    assert [e.text for e in store.search("NEAR")] == ["near the shore"]
    assert store.search("OR") == [] and store.search("NOT") == []
    # Read as operators this would leave the shore out; as words, each finds its own entry.
    found = {e.text for e in store.search("roll AND NOT shore")}
    assert found == {"rock and roll", "near the shore"}


def test_search_matches_words_in_any_script(store: ContextStore) -> None:
    """#269: the query kept `[A-Za-z0-9]+` only, so a word in another script left nothing to
    match, while the index's `unicode61` tokenizer had stored it. The query is split into
    Unicode letters and digits now, as the index is."""
    store.add(_entry("вход через SSO"))
    store.add(_entry("ログインする手順"))
    store.add(_entry("हिन्दी में लिखा"))
    store.add(_entry("prefer pytest"))
    assert [e.text for e in store.search("вход")] == ["вход через SSO"]
    assert [e.text for e in store.search("ログイン")] == ["ログインする手順"]
    assert [e.text for e in store.search("हिन्दी")] == ["हिन्दी में लिखा"]


def test_search_survives_quotes_and_fts5_syntax(store: ContextStore) -> None:
    """#269: every word goes to FTS5 as a string whose own `"` is doubled, so no punctuation
    a person types is read as query syntax."""
    store.add(_entry("prefer pytest over unittest"))
    for query in ('say "pytest', "pytest*)(", "text:pytest", "^pytest -unittest", "NEAR(pytest"):
        assert [e.text for e in store.search(query)] == ["prefer pytest over unittest"], query
    assert store.search('"') == [] and store.search('"""') == [], "no words, nothing to match"
    assert store_module._fts_match('pytest AND "x') == '"pytest"* OR "AND"* OR "x"*'


def test_search_excludes_deleted(store: ContextStore) -> None:
    entry = store.add(_entry("findable token"))
    assert len(store.search("findable")) == 1
    store.delete(entry.id)
    assert store.search("findable") == []


def test_search_respects_pool(store: ContextStore) -> None:
    store.add(_entry("alpha keyword", pool="user"))
    store.add(_entry("beta keyword", pool="project", project_id=PROJECT.id))
    assert [e.text for e in store.search("keyword", pool="user")] == ["alpha keyword"]
    project_hits = store.search("keyword", pool="project", project_id=PROJECT.id)
    assert [e.text for e in project_hits] == ["beta keyword"]


def test_search_in_scope_is_user_plus_current_project(store: ContextStore) -> None:
    store.add(_entry("alpha keyword", pool="user"))
    store.add(_entry("beta keyword", pool="project", project_id=PROJECT.id))
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[]))
    store.add(_entry("gamma keyword", pool="project", project_id="prj_other"))
    hits = {e.text for e in store.search("keyword", project_id=PROJECT.id)}
    assert hits == {"alpha keyword", "beta keyword"}  # excludes the other project


def test_promote_moves_project_entry_to_user_pool(store: ContextStore) -> None:
    entry = store.add(_entry("ship it", pool="project", project_id=PROJECT.id, tags=["t"]))
    promoted = store.promote(entry.id)
    assert promoted.id == entry.id  # moved in place
    assert promoted.pool == "user"
    assert promoted.project_id is None
    assert promoted.tags == ["t"]
    assert promoted.updated_at >= entry.updated_at
    assert [e.text for e in store.entries("user")] == ["ship it"]
    assert store.entries("project", project_id=PROJECT.id) == []


def test_promote_rejects_user_entry(store: ContextStore) -> None:
    entry = store.add(_entry("already global", pool="user"))
    with pytest.raises(ValueError, match="already in the user pool"):
        store.promote(entry.id)


def test_promote_unknown_raises(store: ContextStore) -> None:
    with pytest.raises(KeyError):
        store.promote("ctx_missing")


def test_schema_rejects_inconsistent_pool(store: ContextStore) -> None:
    bad = _entry("oops", pool="user", project_id=PROJECT.id)
    with pytest.raises(sqlite3.IntegrityError):
        store.add(bad)


def test_list_and_get_projects(store: ContextStore) -> None:
    other = ProjectInfo(id="prj_other", root=Path("/tmp/another-app"), linked_repos=[])
    store.ensure_project(other)  # PROJECT is already registered by the fixture
    # A captured registration is reachable by id and in the full list, but not LISTED
    # until something adds it on purpose (#139).
    assert {project.id for project in store.list_projects(all=True)} == {PROJECT.id, "prj_other"}
    assert "prj_other" not in {project.id for project in store.list_projects()}
    stored = store.get_project("prj_other")
    assert stored is not None and stored.model_copy(update={"onboarded_at": None}) == other
    assert store.get_project("prj_missing") is None
    store.onboard_project(other)
    ids = {project.id for project in store.list_projects()}
    assert ids >= {"prj_other"}


def test_list_projects_hides_a_forgotten_registration_unless_asked(store: ContextStore) -> None:
    """``include_forgotten`` is for the one question a tombstone must not hide: a
    forgotten registration can still hold LIVE ``fleet_agent`` rows, whose panes are
    real processes. ``fleet shutdown`` asks this way so it cannot take a pane down
    while leaving its row live with nothing able to reconcile it.

    It is not ``all`` (#139), which adds the captured rows and still hides a
    tombstone. A forget clears ``onboarded_at``, so ``include_forgotten`` alone
    reads past the onboarded filter too — kept, that filter would drop the very
    tombstone the flag exists to find."""
    store.onboard_project(PROJECT)  # the fixture only captured it
    captured = ProjectInfo(id="prj_captured", root=Path("/tmp/captured-app"), linked_repos=[])
    store.ensure_project(captured)
    gone = ProjectInfo(id="prj_gone", root=Path("/tmp/gone-app"), linked_repos=[])
    store.onboard_project(gone)
    store.forget_project("prj_gone")

    assert [p.id for p in store.list_projects()] == [PROJECT.id], "the default still hides it"
    assert {p.id for p in store.list_projects(all=True)} == {PROJECT.id, "prj_captured"}, (
        "so does all"
    )
    everything = {PROJECT.id, "prj_captured", "prj_gone"}
    assert {p.id for p in store.list_projects(include_forgotten=True)} == everything
    assert {p.id for p in store.list_projects(all=True, include_forgotten=True)} == everything
    assert store.get_project("prj_gone") is None, "every OTHER read keeps the promise"


def test_find_projects_by_name_and_id_prefix(store: ContextStore) -> None:
    assert [p.id for p in store.find_projects("example-project")] == [PROJECT.id]  # by name
    assert [p.id for p in store.find_projects(PROJECT.id[:8])] == [PROJECT.id]  # by id prefix
    assert store.find_projects("nope") == []


def test_a_project_nickname_is_set_read_back_and_cleared(store: ContextStore) -> None:
    named = store.set_project_nickname(PROJECT.id, "Auth rewrite — día 2 🚀")
    assert named.nickname == "Auth rewrite — día 2 🚀"
    assert named.codename is None, "the codename is not touched"
    fetched = store.get_project(PROJECT.id)
    assert fetched is not None and fetched.nickname == "Auth rewrite — día 2 🚀"
    assert [p.nickname for p in store.list_projects(all=True)] == ["Auth rewrite — día 2 🚀"]
    assert store.set_project_nickname(PROJECT.id, None).nickname is None


def test_a_nickname_is_refused_for_a_forgotten_or_unknown_project(store: ContextStore) -> None:
    """As ``update_project_layout`` refuses one: a rename from a stale frame would land
    on the tombstone and come back with the revival."""
    store.ensure_project(ProjectInfo(id="prj_gone", root=Path("/tmp/gone")))
    store.forget_project("prj_gone")
    with pytest.raises(KeyError):
        store.set_project_nickname("prj_gone", "ghost")
    with pytest.raises(KeyError):
        store.set_project_nickname("prj_never", "ghost")
    store.ensure_project(ProjectInfo(id="prj_gone", root=Path("/tmp/gone")))
    revived = store.get_project("prj_gone")
    assert revived is not None and revived.nickname is None


def test_find_projects_matches_a_nickname_only_when_nothing_else_does(
    store: ContextStore,
) -> None:
    """Exactly and case and all, after the id prefix, the folder name and the codename:
    a nickname spelled like another project's codename leaves that project found."""
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other")))
    store.set_codename(PROJECT.id, "amber-otter")
    store.set_project_nickname("prj_other", "amber-otter")
    assert [p.id for p in store.find_projects("amber-otter")] == [PROJECT.id]

    store.set_project_nickname("prj_other", "billing: v2 [wip]")
    assert [p.id for p in store.find_projects("billing: v2 [wip]")] == ["prj_other"]
    assert store.find_projects("Billing: v2 [wip]") == []
    assert store.find_projects("billing") == []

    store.set_project_nickname(PROJECT.id, "billing: v2 [wip]")
    both = store.find_projects("billing: v2 [wip]")
    assert sorted(p.id for p in both) == sorted([PROJECT.id, "prj_other"]), "ambiguous"

    store.forget_project("prj_other")
    assert [p.id for p in store.find_projects("billing: v2 [wip]")] == [PROJECT.id]


def test_a_key_pattern_holds_its_glob_characters_as_themselves(store: ContextStore) -> None:
    """``_glob_escape`` took ``*``, ``?`` and ``[`` out of what it was given, where its
    callers read it as standing for itself: ``x[1`` listed the keys under ``x1``, and a
    purge of an id holding one would have taken the keys of the id without it (review of
    #243, round 11, 8/9). Each stands for itself."""
    for key in ("x[1/a", "x1/b", "x*/c", "xy/d", "signal/prj_a*/1", "signal/prj_ab/1"):
        store.set_meta(key, "v")
    assert list(store.list_meta("x[1")) == ["x[1/a"]
    assert list(store.list_meta("x*")) == ["x*/c"]
    assert list(store.list_meta("x?")) == []
    for project_id in ("prj_a*", "prj_ab"):
        store.ensure_project(ProjectInfo(id=project_id, root=Path(f"/tmp/{project_id}")))
    assert store.purge_project("prj_a*")["team_meta"] == 1
    assert list(store.list_meta("signal/")) == ["signal/prj_ab/1"]


@pytest.mark.parametrize("ref", ["*", "?", "[", "*?[", ""])
def test_a_ref_with_nothing_left_once_its_glob_characters_go_names_nothing(
    store: ContextStore, ref: str
) -> None:
    """``*`` was an id prefix of every id: it named the one project, task or session a
    store had, and was ambiguous with two, a refusal that listed every one of them, the
    directories the hooks only captured included (review of #243, round 10, 7/7)."""
    from aisquare.models import TeamSession, TeamTask

    now = datetime.now(tz=UTC)
    store.ensure_project(ProjectInfo(id="prj_captured", root=Path("/tmp/captured")))
    for n in (1, 2):
        store.upsert_task(
            TeamTask(
                id=f"tsk_{n}", project_id=PROJECT.id, key=f"k{n}", title=f"task {n}",
                created_at=now, updated_at=now,
            )
        )  # fmt: skip
        store.upsert_session(
            TeamSession(
                id=f"ses_{n}", project_id=PROJECT.id, role="coder",
                started_at=now, last_seen_at=now,
            )
        )  # fmt: skip
        store.add(_entry(f"entry {n}"))
    assert store.find_projects(ref) == []
    assert store.get_task(ref) is None, "nor any task by its tail"
    assert store.get_session(ref) is None
    assert store.get(ref) is None
    assert store.get_task("tsk_1") is not None and store.get_task("1") is not None
    assert [p.id for p in store.find_projects("prj_c*")] == ["prj_captured"]


def test_a_task_or_session_lookup_kept_to_some_boards_finds_and_doubts_only_there(
    store: ContextStore,
) -> None:
    """``project_ids`` keeps a lookup to those boards' rows, by id, prefix and tail alike,
    and a prefix is ambiguous only among them: the remote's phone may reach only some boards
    (review of #243, round 10, 5/7)."""
    from aisquare.models import TeamSession, TeamTask

    now = datetime.now(tz=UTC)
    for task_id, board in (("tsk_shared_a1", PROJECT.id), ("tsk_shared_b2", "prj_captured")):
        store.upsert_task(
            TeamTask(
                id=task_id, project_id=board, key=task_id, title=task_id,
                created_at=now, updated_at=now,
            )
        )  # fmt: skip
        store.upsert_session(
            TeamSession(
                id=task_id.replace("tsk", "ses"), project_id=board, role="coder",
                started_at=now, last_seen_at=now,
            )
        )  # fmt: skip
    mine = [PROJECT.id]
    with pytest.raises(AmbiguousIdError):
        store.get_task("tsk_shared")
    task = store.get_task("tsk_shared", project_ids=mine)
    assert task is not None and task.id == "tsk_shared_a1"
    assert store.get_task("tsk_shared_b2", project_ids=mine) is None
    assert store.get_task("b2", project_ids=mine) is None, "nor by its tail"
    assert store.get_task("tsk_shared_b2", project_ids=[]) is None
    with pytest.raises(AmbiguousIdError):
        store.get_session("ses_shared")
    session = store.get_session("ses_shared", project_ids=mine)
    assert session is not None and session.id == "ses_shared_a1"
    assert store.get_session("ses_shared_b2", project_ids=mine) is None


def test_add_linked_repo_is_idempotent(store: ContextStore) -> None:
    updated = store.add_linked_repo(PROJECT.id, "git@github.com:acme/app.git")
    assert updated.linked_repos == ["git@github.com:acme/app.git"]
    again = store.add_linked_repo(PROJECT.id, "git@github.com:acme/app.git")
    assert again.linked_repos == ["git@github.com:acme/app.git"]  # no duplicate


def test_add_linked_repo_unknown_project_raises(store: ContextStore) -> None:
    with pytest.raises(KeyError):
        store.add_linked_repo("prj_missing", "repo")


def test_add_and_list_prompts(store: ContextStore, monkeypatch: pytest.MonkeyPatch) -> None:
    # NO SLEEP. Both prompts are written inside one clock tick on purpose —
    # which is the case that used to sort by coin flip, because the tie-break
    # was an id whose tail is 128 random bits. `recent_prompts` breaks ties on
    # `rowid` now, so "written second" is what "sorts first" means, and this
    # asserts that rather than sleeping until the clock disambiguates it.
    #
    # The tick and the coin are both FIXED here rather than hoped for. On Linux
    # `created_at` has microsecond resolution and two inserts almost never
    # share it, so the tie never happened, and an `id DESC` tie-break passed
    # too. The clock is frozen, and the ids come out in the REVERSE of
    # insertion order, so only `rowid` puts the second prompt first (review of
    # #65, R5).
    frozen = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Frozen:
            return cls.fromtimestamp(frozen.timestamp(), tz)

    ids = iter(["prm_zzz_written_first", "prm_aaa_written_second"])
    monkeypatch.setattr(store_module, "datetime", _Frozen)
    monkeypatch.setattr(store_module, "new_prompt_id", lambda: next(ids))
    first = store.add_prompt("first prompt", PROJECT.id)
    second = store.add_prompt("second prompt", PROJECT.id)
    assert second.id < first.id, "the ids agree with insertion order; `id DESC` would pass too"
    prompts = store.recent_prompts(PROJECT.id)
    assert [p.created_at for p in prompts] == [frozen, frozen], "the tick was not shared"
    assert [p.text for p in prompts] == ["second prompt", "first prompt"]  # newest first


def test_recent_prompts_are_scoped_to_project(store: ContextStore) -> None:
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[]))
    store.add_prompt("here", PROJECT.id)
    store.add_prompt("there", "prj_other")
    assert [p.text for p in store.recent_prompts(PROJECT.id)] == ["here"]


def test_migrations_reach_the_current_schema_version() -> None:
    from aisquare.core.store import SCHEMA_VERSION

    open_store().close()  # creates and migrates the database
    raw = sqlite3.connect(str(_db_path()))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
    finally:
        raw.close()
    # v11 fleet, v12 metric, v13 converges, v14 forgotten_at, v15 the account registry
    # (#145), v16 usage readings and the limited state (#146), v17 onboarded_at (#139),
    # v18 the launch spec and ui_state (#144), v19 the project explainability key (#141),
    # v20 project groups, pins and manual order (#140), v21 project destinations (#142),
    # v22 the revocations owed for keys the CLI minted (#142), v23 the one-time repair of
    # old tombstones (#139, #140), v24 the destination a project's key was attached for (#142),
    # v25 where a fleet agent's server listened and in which boot (round 25 of #257),
    # v26 when that server started, the identity of the server its pane id names (follow-up),
    # v27 a project's nickname, v28 which token took a usage reading (its fingerprint),
    # v29 a task's number on its board, v30 its unique index, v31 the branch of a fleet
    # agent's worktree (review of #259, round 15), v32 the board a fleet agent reads (round 25),
    # v33 the persona a session and a fleet agent run as (#201), v34 how a fleet agent's
    # row ended, stopped or died (#38), v35 session token usage (#346, stamped v34 there),
    # v36 a fleet agent's first message and a session's last prompt (P0-W1a, #122, stamped
    # v34 on rc/p0-reliability), v37 who caused a board event and its details (P1 WS-2,
    # #279, stamped v35 on rc/p1-token-diet), v38 a group's parent manager and a task's
    # origin (P2-W6, #199, stamped v36 on rc/p2-capacity-setup), v39 work briefs
    # (docs/plans/evidence-gate.md, part 2; stamped v34, v36, v37 and v38 on #306's, #341's
    # and #417's branches), v40 local usage stats (the account and repo of a session, the
    # repo profile and the feature counters)
    assert version == SCHEMA_VERSION == 40


def test_a_v14_store_gains_nullable_persona_columns_through_the_persona_step() -> None:
    """A store from before the v15 fork (0.6.0's v14) climbs the whole ladder: rows that
    existed at v14 survive, both tables gain a ``persona`` that reads NULL — v33's products
    (``_PRODUCTS[32]``, #201's step) — and a start that names no persona keeps the one a
    row already records."""
    from datetime import UTC, datetime

    from aisquare.models import TeamSession

    db = _at_version(14)
    raw = sqlite3.connect(str(db))
    try:
        raw.execute(
            "INSERT INTO team_session (id, project_id, started_at, last_seen_at) "
            "VALUES ('sess-old', 'prj_old', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        raw.execute(
            "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at) "
            "VALUES ('agt_old', 'prj_old', 'coder-1', 'coder', '%1', '/tmp', "
            "'2026-01-01T00:00:00+00:00')"
        )
        raw.commit()
    finally:
        raw.close()

    store = open_store()
    try:
        old_session = store.get_session("sess-old")
        old_agent = store.get_fleet_agent("agt_old")
        now = datetime(2026, 9, 15, tzinfo=UTC)
        fresh = TeamSession(id="sess-new", project_id="prj_old", started_at=now, last_seen_at=now)
        store.upsert_session(fresh.model_copy(update={"persona": "skeptic"}))
        restarted = store.upsert_session(fresh)
    finally:
        store.close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        nullable = {
            table: {row[1]: row[3] for row in raw.execute(f"PRAGMA table_info({table})")}
            for table in ("team_session", "fleet_agent")
        }
    finally:
        raw.close()
    assert version == SCHEMA_VERSION
    assert nullable["team_session"]["persona"] == 0 and nullable["fleet_agent"]["persona"] == 0
    assert old_session is not None and old_session.persona is None
    assert old_agent is not None and old_agent.persona is None
    assert restarted.persona == "skeptic"


def test_the_metric_check_constraints_mirror_the_python_vocabularies() -> None:
    """Each closed vocabulary is spelled twice — once in SQL, once in Python —
    because SQLite cannot read an enum. Held equal here so neither can drift:
    a value the model accepts that the CHECK refuses would lose the row
    silently, on every prompt, with nothing raising."""
    import re
    from typing import get_args

    from aisquare.core.store import _SCHEMA_V12
    from aisquare.models import (
        BriefingStatus,
        CacheStatus,
        ClientReason,
        DeliverySource,
        HookAction,
        HookTrigger,
        RunKind,
    )

    def sql_set(column: str, schema: str = _SCHEMA_V12) -> set[str]:
        match = re.search(rf"{column} TEXT[^,]*?IN \(([^)]*)\)", schema, re.DOTALL)
        assert match is not None, column
        return {value.strip().strip("'") for value in match.group(1).split(",")}

    # Against the Python vocabulary itself, never a third copy typed here: a
    # Literal that moves in models.py must move the SQL or fail this test.
    assert sql_set("client_reason") == {reason.value for reason in ClientReason}
    assert sql_set("status") == set(get_args(BriefingStatus))
    assert sql_set("action") == set(get_args(HookAction))
    assert sql_set("trigger") == set(get_args(HookTrigger))
    assert sql_set("cache_status") == set(get_args(CacheStatus))
    assert sql_set("run_kind") == set(get_args(RunKind))
    assert sql_set("delivery_source") == set(get_args(DeliverySource))


def test_the_metric_table_has_no_column_that_could_name_an_arm() -> None:
    from aisquare.core.store import _SCHEMA_V12, _SCHEMA_V13

    for forbidden in ("arm", "flags_hash", "architecture", "CREATE TABLE run"):
        assert forbidden not in _SCHEMA_V12 and forbidden not in _SCHEMA_V13, forbidden


def test_a_populated_v10_database_migrates_to_the_current_version_with_its_rows_intact() -> None:
    """The migration real machines take: every row that existed before the
    metric table survives it, the table arrives with the v2 columns and
    ``delivery_source``, and no ``run`` table comes along."""
    from aisquare.core.store import _MIGRATIONS

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    try:
        for migration in _MIGRATIONS[:10]:
            raw.executescript(migration)
        raw.execute("PRAGMA user_version = 10")
        raw.execute(
            "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES (?, ?, ?, ?, ?)",
            ("prj_old", "/tmp/old", "old", "[]", "2026-01-01T00:00:00+00:00"),
        )
        raw.execute(
            "INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ctx_old",
                "project",
                "prj_old",
                "survives",
                "[]",
                "test",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        raw.commit()
    finally:
        raw.close()

    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            raw.execute("SELECT text FROM entry WHERE id = 'ctx_old'").fetchone()[0] == "survives"
        )
        columns = {row[1] for row in raw.execute("PRAGMA table_info(metric)")}
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        raw.close()
    assert {
        "client_reason",
        "status",
        "action",
        "query_id",
        "opaque_config_id",
        "run_kind",
        "delivery_source",
    } <= columns
    assert "arm" not in columns and "run" not in tables


def _at_version(version: int, *, after: str = "", stamp: int | None = None) -> Path:
    """A database migrated by hand to ``version``, with ``after`` run last and
    ``user_version`` stamped ``stamp`` (default: ``version``). Each step as the
    ladder runs it, so a step from v15 on brings its columns too."""
    from aisquare.core.store import _run_step

    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    try:
        for step in range(version):
            _run_step(raw, step)
        if after:
            raw.executescript(after)
        raw.execute(f"PRAGMA user_version = {stamp if stamp is not None else version}")
        raw.commit()
    finally:
        raw.close()
    return db


# The metric table the branch's v1 contract created (7557751..31956f2), stamped
# user_version 11 like the v2 one that replaced it in place. Column names as
# they were; the arm and flags_hash columns are the reason it must not stay.
CI_V11_METRIC_DDL = """
CREATE TABLE metric (
    trace_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    wall_ms INTEGER,
    run_id TEXT,
    run_kind TEXT CHECK (run_kind IN ('live', 'replay')),
    opaque_config_id TEXT,
    trigger TEXT CHECK (trigger IN ('session_start', 'prompt_submit', 'agent_request')),
    client_reason TEXT NOT NULL DEFAULT 'disabled',
    status TEXT,
    action TEXT,
    query_id TEXT,
    briefing_id TEXT,
    config_fingerprint TEXT,
    input_checkpoint TEXT,
    resolved_scope_version INTEGER,
    round_trip_ms INTEGER,
    server_ms INTEGER,
    deadline_breached INTEGER,
    token_count INTEGER,
    items_count INTEGER,
    cache_status TEXT,
    error_codes TEXT NOT NULL DEFAULT '[]',
    rendered_chars INTEGER,
    injected_chars INTEGER,
    frame_version TEXT,
    instruction_version TEXT,
    redaction_level TEXT,
    snapshot_ref TEXT,
    snapshot_untracked_excluded INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    tool_calls INTEGER
);
CREATE INDEX metric_project_started ON metric (project_id, started_at);
CREATE INDEX metric_open_session ON metric (session_id, started_at) WHERE ended_at IS NULL;
"""
"""The ``metric`` table as THIS BRANCH stamped ``user_version 11`` before the
merge — no ``delivery_source``. It is spelled out here rather than taken from
``_SCHEMA_V11`` because that name now holds the fleet tables v0.6.0 shipped:
two branches claimed 11, which is the whole reason _SCHEMA_V13 exists."""


V1_METRIC_DDL = """
CREATE TABLE metric (
    trace_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    session_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    wall_ms INTEGER,
    ci_action TEXT NOT NULL DEFAULT 'allow',
    degradation_reason TEXT NOT NULL DEFAULT 'disabled',
    cache_hit INTEGER NOT NULL DEFAULT 0,
    server_ms INTEGER,
    round_trip_ms INTEGER,
    budget_breach INTEGER NOT NULL DEFAULT 0,
    injected_chars INTEGER,
    run_id TEXT,
    arm TEXT,
    flags_hash TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    tool_calls INTEGER
);
CREATE INDEX metric_project_started ON metric (project_id, started_at);
CREATE INDEX metric_open_session ON metric (session_id, started_at) WHERE ended_at IS NULL;
CREATE TABLE run (
    id TEXT PRIMARY KEY,
    arm TEXT,
    flags_hash TEXT,
    started_at TEXT NOT NULL,
    note TEXT
);
INSERT INTO metric (trace_id, project_id, started_at, arm) VALUES ('trc_v1', 'prj_old', 't', 'A');
"""


def _metric_columns(db: Path) -> set[str]:
    raw = sqlite3.connect(str(db))
    try:
        return {row[1] for row in raw.execute("PRAGMA table_info(metric)")}
    finally:
        raw.close()


def test_a_v11_database_gains_the_column_and_keeps_its_rows() -> None:
    """The machine that ran the branch's stub smoke: at 11 with a populated
    metric table and no fleet tables. The merged ladder adds the column and the
    fleet tables; the rows stay and read back with a ``None`` source, which is
    the truth about them."""
    db = _at_version(10, after=CI_V11_METRIC_DDL, stamp=11)
    raw = sqlite3.connect(str(db))
    try:
        raw.execute(
            "INSERT INTO metric (trace_id, project_id, started_at, client_reason) "
            "VALUES ('trc_old', 'prj_old', '2026-09-01T00:00:00+00:00', 'disabled')"
        )
        raw.commit()
    finally:
        raw.close()
    store = open_store()
    try:
        (old,) = store.turn_metrics(project_id="prj_old")
    finally:
        store.close()
    assert old.trace_id == "trc_old" and old.delivery_source is None
    assert "delivery_source" in _metric_columns(db)


def test_a_v11_database_whose_metric_table_was_deleted_by_hand_heals() -> None:
    """``user_version`` 11 with no ``metric`` table — two ways to arrive: the
    PR body's own withdrawn advice for the v1-shaped table, and every v0.6.0
    install from PyPI, whose 11 is the fleet tables. The merged ladder creates
    the table before anything alters it, and a write lands afterwards."""
    from datetime import UTC, datetime

    from aisquare.models import TurnMetric

    db = _at_version(11)
    assert _metric_columns(db) == set(), "the precondition: no metric table at all"
    store = open_store()
    try:
        store.open_turn(
            TurnMetric(
                trace_id="trc_healed",
                project_id="prj_x",
                started_at=datetime.now(tz=UTC),
                delivery_source="override",
            )
        )
        (row,) = store.turn_metrics(project_id="prj_x")
    finally:
        store.close()
    assert row.delivery_source == "override"
    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()


def test_a_v11_database_with_the_v1_shaped_metric_table_is_moved_aside_and_rebuilt() -> None:
    """The third real v11 state. Every v12 statement would succeed on the v1
    shape and stamp it 12 — a table that can never take a v2 row and can never
    be migrated by version again. It is renamed (never dropped), its arm-shaped
    sibling with it, and the v2 shape is built in its place."""
    from datetime import UTC, datetime

    from aisquare.core.store import V1_ORPHAN_SUFFIX
    from aisquare.models import TurnMetric

    db = _at_version(10, after=V1_METRIC_DDL, stamp=11)
    assert "arm" in _metric_columns(db), "the precondition: the v1 shape"
    store = open_store()
    try:
        store.open_turn(
            TurnMetric(trace_id="trc_v2", project_id="prj_x", started_at=datetime.now(tz=UTC))
        )
        (row,) = store.turn_metrics(project_id="prj_x")
    finally:
        store.close()
    assert row.trace_id == "trc_v2" and row.delivery_source is None
    columns = _metric_columns(db)
    assert {"run_kind", "delivery_source"} <= columns and "arm" not in columns
    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert f"metric{V1_ORPHAN_SUFFIX}" in tables and f"run{V1_ORPHAN_SUFFIX}" in tables
        assert "run" not in tables, "nothing arm-shaped stays live"
        kept = raw.execute(f"SELECT trace_id, arm FROM metric{V1_ORPHAN_SUFFIX}").fetchall()
        assert kept == [("trc_v1", "A")], "renamed, never dropped"
        indexes = {row[1] for row in raw.execute("PRAGMA index_list(metric)")}
        assert {"metric_project_started", "metric_open_session"} <= indexes, (
            "the indexes follow the live table, not the orphan"
        )
    finally:
        raw.close()


def test_a_v1_shaped_table_is_moved_aside_even_when_an_orphan_already_exists() -> None:
    """The review's wedge: a fixed orphan name that already exists makes the
    rename raise, the transaction roll back and user_version stay 11 — so every
    later open fails identically until someone drops a table by hand."""
    from aisquare.core.store import V1_ORPHAN_SUFFIX

    prior = (
        f"CREATE TABLE metric{V1_ORPHAN_SUFFIX} (x TEXT);"
        f" CREATE TABLE run{V1_ORPHAN_SUFFIX} (x TEXT);"
    )
    db = _at_version(10, after=V1_METRIC_DDL + prior, stamp=11)
    open_store().close()
    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        raw.close()
    assert {f"metric{V1_ORPHAN_SUFFIX}", f"metric{V1_ORPHAN_SUFFIX}_2"} <= tables
    assert {f"run{V1_ORPHAN_SUFFIX}", f"run{V1_ORPHAN_SUFFIX}_2"} <= tables
    assert {"run_kind", "delivery_source"} <= _metric_columns(db)


def test_the_close_turn_compare_and_set_loses_to_a_stop_that_landed_first() -> None:
    """The review found the CAS uncovered: the racing test's second call never
    reached the UPDATE, and removing ``AND ended_at IS NULL`` left 74 tests
    green. Here the other Stop lands between this call's SELECT and its UPDATE,
    on the same connection, so the guard alone decides the outcome."""
    from datetime import UTC, datetime, timedelta

    from aisquare.models import TurnMetric

    store: Any = open_store()  # the concrete store, whose connection we interpose on
    try:
        started = datetime.now(tz=UTC) - timedelta(seconds=5)
        store.open_turn(
            TurnMetric(
                trace_id="trc_cas", project_id="prj_x", session_id="ses_cas", started_at=started
            )
        )
        first_close = (started + timedelta(seconds=1)).isoformat()
        real_conn = store._conn

        class RacingConnection:
            """The other Stop writes the row the instant before our UPDATE."""

            def __getattr__(self, name: str) -> object:
                return getattr(real_conn, name)

            def execute(self, sql: str, *params: object) -> object:
                if sql.lstrip().startswith("UPDATE metric SET ended_at"):
                    real_conn.execute(
                        "UPDATE metric SET ended_at = ?, wall_ms = ? WHERE trace_id = ?",
                        (first_close, 1000, "trc_cas"),
                    )
                return real_conn.execute(sql, *params)

        store._conn = RacingConnection()
        loser = store.close_turn("ses_cas", ended_at=started + timedelta(seconds=4))
        store._conn = real_conn
        (row,) = store.turn_metrics(session_id="ses_cas")
    finally:
        store.close()
    assert loser is None, "the second Stop reports that it closed nothing"
    assert row.ended_at is not None and row.ended_at.isoformat() == first_close
    assert row.wall_ms == 1000, "the first close is never overwritten"


def test_a_bad_delivery_source_is_refused_at_the_row() -> None:
    from datetime import UTC, datetime
    from typing import cast

    from aisquare.models import DeliverySource, TurnMetric

    store = open_store()
    try:
        # model_construct skips validation, so the CHECK is the only thing in
        # the way; the cast keeps mypy from refusing the value first.
        bad = TurnMetric.model_construct(
            trace_id="trc_bad",
            project_id="prj_x",
            started_at=datetime.now(tz=UTC),
            delivery_source=cast(DeliverySource, "server"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.open_turn(bad)
    finally:
        store.close()


def test_data_persists_across_reopen() -> None:
    first = open_store()
    first.ensure_project(PROJECT)
    entry = first.add(_entry("durable", pool="project", project_id=PROJECT.id))
    first.close()

    second = open_store()
    try:
        assert second.get(entry.id) == entry
    finally:
        second.close()


def _db_path() -> Path:
    from aisquare.core.paths import db_path

    return db_path()


# Every shape `user_version` 11 or 12 has in the wild, and what each must reach.
# Two branches claimed 11 at once — v0.6.0 shipped the fleet tables under it
# (and is on PyPI), this branch had the metric table — so the merged ladder is
# built to CONVERGE rather than to count, and this is the test that says so.
# Renumbering alone cannot pass this table: put the fleet first and every CI
# machine wedges on `table metric already exists`; put the metric first and
# every v0.6.0 install wedges on `table fleet_agent already exists`.
_COHORTS: tuple[tuple[str, str, int], ...] = (
    ("a fresh install", "", 10),
    ("v0.6.0 from PyPI: fleet at 11, no metric", "FLEET", 11),
    ("this branch at 11: metric, no fleet", "CI11", 11),
    ("this branch at 12: metric with the column", "CI12", 12),
    ("11 with no metric table at all", "", 11),
    ("11 with the v1-contract metric table", "V1", 11),
    ("11 with a v1 table and an orphan already aside", "V1ORPHAN", 11),
)


@pytest.mark.parametrize(("label", "shape", "stamp"), _COHORTS, ids=[c[0] for c in _COHORTS])
def test_every_shape_of_user_version_11_converges_on_one_schema(
    label: str, shape: str, stamp: int
) -> None:
    """No cohort is wedged, and none is left silently short of a table.

    "Silently short" is the half that is easy to miss: a store that opens but
    has no ``fleet_agent`` breaks the fleet UI at the first query, and one with
    no ``metric`` loses every CI row with nothing raising. So this asserts the
    end state by WRITING to both tables, not by reading the version.
    """
    from datetime import UTC, datetime

    from aisquare.core.store import _SCHEMA_V11
    from aisquare.models import TurnMetric

    after = {
        "": "",
        "FLEET": _SCHEMA_V11,
        "CI11": CI_V11_METRIC_DDL,
        "CI12": CI_V11_METRIC_DDL + "\nALTER TABLE metric ADD COLUMN delivery_source TEXT;",
        "V1": V1_METRIC_DDL,
        "V1ORPHAN": V1_METRIC_DDL + "\nCREATE TABLE metric_v1_orphaned (trace_id TEXT);",
    }[shape]
    db = _at_version(10, after=after, stamp=stamp)

    store = open_store()  # migrates on open; a wedge raises out of here
    try:
        store.open_turn(
            TurnMetric(
                trace_id="trc_cohort",
                project_id="prj_x",
                started_at=datetime.now(tz=UTC),
                delivery_source="descriptor",
            )
        )
        (row,) = store.turn_metrics(project_id="prj_x")
    finally:
        store.close()
    assert row.delivery_source == "descriptor", label

    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION, label
        # the fleet half must be there and usable too, whichever way in
        raw.execute(
            "INSERT INTO project (id, name, root, linked_repos, created_at, codename) "
            "VALUES ('prj_f', 'f', '/tmp/f', '[]', '2026-01-01T00:00:00+00:00', 'kestrel')"
        )
        raw.execute(
            "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at) "
            "VALUES ('agt_1', 'prj_f', 'a', 'dev', '%1', '/tmp/f', '2026-01-01T00:00:00+00:00')"
        )
        raw.commit()
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()
    if shape.startswith("V1"):
        # the v1 rows are the developer's; renamed aside, never dropped
        assert any(t.startswith("metric_v1_orphaned") for t in tables), label
    if shape == "V1ORPHAN":
        assert "metric_v1_orphaned_2" in tables, "a taken orphan name must not wedge the rename"


# --- stores another line stamped 15 or 17 (the note above _SCHEMA_V15) ----------------------
#
# v15-v17 were claimed by other lines of development while the accounts stack held
# them. What each line's own steps above v14 left in its stores, verbatim from its
# branch: #136 (codex/native-personas-workflow), #201 (rc/hackathon-v1) and #113
# (feat/coding-agent-adapters), with a row in what each added.
WORK_BRIEF_AT_15 = """
CREATE TABLE IF NOT EXISTS work_brief (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_brief_project ON work_brief(project_id);
INSERT INTO work_brief (id, project_id, revision, data) VALUES ('wb_1', 'prj_used', 3, '{}');
"""
PERSONAS_AT_15 = """
ALTER TABLE team_session ADD COLUMN persona TEXT;
ALTER TABLE fleet_agent ADD COLUMN persona TEXT;
INSERT INTO team_session (id, project_id, started_at, last_seen_at, persona)
    VALUES ('ses_1', 'prj_used', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00',
            'architect');
"""
CODING_AGENTS_AT_17 = """
ALTER TABLE team_session ADD COLUMN agent TEXT;
ALTER TABLE team_session ADD COLUMN native_session_id TEXT;
ALTER TABLE fleet_agent ADD COLUMN agent TEXT;
UPDATE team_session SET agent = 'claude-code', native_session_id = id
 WHERE account IS NOT NULL AND transcript_path LIKE '%/projects/%.jsonl';
UPDATE fleet_agent SET agent = 'claude-code' WHERE binary = 'claude';
ALTER TABLE team_meta ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;
UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER);
CREATE TRIGGER team_meta_insert_time AFTER INSERT ON team_meta BEGIN
 UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER) WHERE key = NEW.key;
END;
CREATE TRIGGER team_meta_update_time AFTER UPDATE OF value ON team_meta BEGIN
 UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER) WHERE key = NEW.key;
END;
UPDATE team_meta SET updated_at = CAST(strftime('%s', 'now') AS INTEGER)
 WHERE updated_at = 0;
INSERT INTO team_session (id, project_id, started_at, last_seen_at, agent, native_session_id)
    VALUES ('ses_1', 'prj_used', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00',
            'codex', 'nat_1');
"""

# Two projects every cohort's store holds: one used on purpose (a context entry), which
# the v17 backfill adopts, and one only ever captured (a prompt), which it leaves hidden.
# The used one has a live fleet agent: a fleet read of it is what raised "no such
# column: account_slot" on a store that skipped v15 (review of #203).
_TWO_PROJECTS = """
INSERT INTO project (id, root, name, linked_repos, created_at) VALUES
    ('prj_used', '/w/used', 'used', '[]', '2026-09-01T00:00:00+00:00'),
    ('prj_seen', '/w/seen', 'seen', '[]', '2026-09-01T00:00:00+00:00');
INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at)
    VALUES ('ent_1', 'project', 'prj_used', 'a fact', '[]', 'cli',
            '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
INSERT INTO prompt (id, project_id, text, source, created_at)
    VALUES ('prm_1', 'prj_seen', 'hello', 'claude-code', '2026-09-01T00:00:00+00:00');
INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at)
    VALUES ('agt_1', 'prj_used', 'manager', 'manager', '%0', '/w/used',
            '2026-09-01T00:00:00+00:00');
"""

# (label, what the line's steps left, the stamp, a query over it, what it must still read)
_FOREIGN_COHORTS: tuple[tuple[str, str, int, str, list[tuple[object, ...]]], ...] = (
    (
        "#136 at 15: work_brief",
        WORK_BRIEF_AT_15,
        15,
        "SELECT id, revision FROM work_brief",
        [("wb_1", 3)],
    ),
    (
        "#201 at 15: persona columns",
        PERSONAS_AT_15,
        15,
        "SELECT persona FROM team_session",
        [("architect",)],
    ),
    # The maintainer's own store is this cohort: a backup of it (31 projects) holds
    # exactly these tables, indexes, triggers and columns, object for object.
    (
        "#113 at 17: coding-agent columns",
        CODING_AGENTS_AT_17,
        17,
        "SELECT session.agent, session.native_session_id, agent.agent"
        " FROM team_session AS session, fleet_agent AS agent",
        [("codex", "nat_1", "claude-code")],
    ),
)


def _shape(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Every table, index and trigger by name, and every column as ``table.column``.

    SQLite's automatic indexes are left out: they come and go with their table."""
    shape: set[tuple[str, str]] = set()
    for kind, name in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall():
        shape.add((kind, name))
        if kind == "table":
            columns = conn.execute(f"PRAGMA table_info({name})").fetchall()
            shape |= {("column", f"{name}.{column[1]}") for column in columns}
    return shape


def _built(steps: int, after: str = "") -> set[tuple[str, str]]:
    """The shape of a database built in memory by the first ``steps`` steps, then ``after``."""
    from aisquare.core.store import _run_step

    conn = sqlite3.connect(":memory:")
    try:
        for step in range(steps):
            _run_step(conn, step)
        if after:
            conn.executescript(after)
        return _shape(conn)
    finally:
        conn.close()


def _contents(db: Path) -> tuple[list[tuple[object, ...]], dict[str, list[tuple[object, ...]]]]:
    """``db``'s whole schema, SQL included, and every row of every table, with its version."""
    raw = sqlite3.connect(str(db))
    try:
        schema = raw.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
        rows = {
            name: sorted(raw.execute(f"SELECT * FROM {name}").fetchall(), key=repr)
            for kind, name, _, _ in schema
            if kind == "table"
        }
        rows["PRAGMA user_version"] = raw.execute("PRAGMA user_version").fetchall()
        return sorted(schema, key=repr), rows
    finally:
        raw.close()


def _open_a_foreign_cohort(db: Path, query: str) -> dict[str, object]:
    """Open ``db`` with this build, write through the store to the tables whose absence
    was "no such table", read from the ones a fleet read and ``accounts list`` failed
    on, open it once more, and read back what :func:`_converged` says it must hold."""
    store = open_store()  # a wedge raises out of here
    try:
        store.upsert_claude_account(1, Path("/h/.claude"))
        store.set_project_setting("prj_used", "claude_account", "1")
        read: dict[str, object] = {
            "listed": [p.id for p in store.list_projects()],
            "listed with --all": sorted(p.id for p in store.list_projects(all=True)),
            "account setting": store.project_setting("prj_used", "claude_account"),
            "accounts": [(r.slot, r.config_dir) for r in store.claude_accounts()],
            "fleet": [(a.id, a.account_slot) for a in store.fleet_agents("prj_used")],
            "live fleet": [a.id for a in store.fleet_agents("prj_used", live_only=True)],
            "missing": store.missing_schema(),
        }
    finally:
        store.close()
    schema, rows = _contents(db)
    open_store().close()
    schema_again, rows_again = _contents(db)
    read["a second open changed"] = sorted(
        name for name in rows.keys() | rows_again.keys() if rows.get(name) != rows_again.get(name)
    ) + (["the schema"] if schema_again != schema else [])
    raw = sqlite3.connect(str(db))
    try:
        read["version"] = raw.execute("PRAGMA user_version").fetchone()[0]
        read["shape"] = _shape(raw)
        read["their rows"] = raw.execute(query).fetchall()
    finally:
        raw.close()
    return read


def _converged(ddl: str, their_rows: list[tuple[object, ...]]) -> dict[str, object]:
    """What a foreign store reads once this build has opened it: the project used on
    purpose listed (the v17 backfill ran) and the captured one not, the account and the
    fleet agent read back, nothing ``doctor`` would report missing, a second open that
    changes no row and no schema, every table, index and column of this build and of
    the other line and nothing else, and the other line's rows as it left them."""
    theirs = _built(14, _TWO_PROJECTS + ddl) - _built(14, _TWO_PROJECTS)
    return {
        "listed": ["prj_used"],
        "listed with --all": ["prj_seen", "prj_used"],
        "account setting": "1",
        "accounts": [(1, Path("/h/.claude"))],
        "fleet": [("agt_1", None)],
        "live fleet": ["agt_1"],
        "missing": [],
        "a second open changed": [],
        "version": SCHEMA_VERSION,
        "shape": _built(SCHEMA_VERSION) | theirs,
        "their rows": their_rows,
    }


@pytest.mark.parametrize(
    ("label", "ddl", "stamp", "query", "expected"),
    _FOREIGN_COHORTS,
    ids=[c[0] for c in _FOREIGN_COHORTS],
)
def test_a_store_another_line_stamped_converges_on_this_schema_and_keeps_its_own(
    label: str, ddl: str, stamp: int, query: str, expected: list[tuple[object, ...]]
) -> None:
    """A store stamped 15 or 17 by another line never ran this line's steps below its
    stamp. Counted positionally, it had no ``claude_account``, so every accounts
    command failed with "no such table". Stamped 17, it had no ``onboarded_at`` either:
    v23 failed on that column and the store stopped opening. It converges instead:
    every table and column this build makes, the v17 backfill (so its projects stay
    listed), and the other line's tables, columns, triggers and rows left alone."""
    db = _at_version(14, after=_TWO_PROJECTS + ddl, stamp=stamp)

    assert _open_a_foreign_cohort(db, query) == _converged(ddl, expected), label


@pytest.mark.parametrize(
    ("label", "ddl", "stamp", "query", "expected"),
    _FOREIGN_COHORTS,
    ids=[c[0] for c in _FOREIGN_COHORTS],
)
def test_a_foreign_store_a_build_without_the_pass_carried_on_converges_too(
    label: str, ddl: str, stamp: int, query: str, expected: list[tuple[object, ...]]
) -> None:
    """A foreign store that a build without the presence pass has already opened ran
    this line's steps from its stamp on and none below it. Stamped 15, it reached the
    current version without v15's tables, and the ladder never runs for it again, so
    only the pass after the ladder can give them back. Stamped 17, it stopped at 22:
    v23 failed on the missing ``onboarded_at``, and the pass before v23 adds it."""
    from aisquare.core.store import _run_step

    carried_to = SCHEMA_VERSION if stamp == 15 else 22  # v23 is the step it failed on
    db = _at_version(14, after=_TWO_PROJECTS + ddl, stamp=stamp)
    raw = sqlite3.connect(str(db))
    try:
        for step in range(stamp, carried_to):
            _run_step(raw, step)
        raw.execute(f"PRAGMA user_version = {carried_to}")
        raw.commit()
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        raw.close()
    assert "claude_account" not in tables, "the fixture is the store that build left"

    assert _open_a_foreign_cohort(db, query) == _converged(ddl, expected), label


# --- one schema from the shapes this RC meets: its own v21 and v24, main's v24 and v32, fresh --
#
# rc/captain-v1 stamped 21 with the v15 fork converged in place: the account registry AND the
# persona columns. Main's 0.7.0 stamps 24, with the presence pass and no persona column, and
# the RC took it whole (#233): its own v24 is main's with the persona columns. Main's 0.8.2
# stamps 32. #201's v33 adds the two persona columns where they are absent, so the RC's
# stores keep theirs and main's gain them. A fresh store runs this ladder. Each starting shape
# is replayed from a FROZEN dump of its schema (tests/fixtures/store_schemas), never built by
# this ladder, so no pin over it can pass by construction. The owner's two stores were the
# RC's v21, by two histories, and are its v24 since #233 was installed on them.

_SCHEMAS = Path(__file__).parent / "fixtures" / "store_schemas"
_RC_V21_SCHEMAS = ("rc_v21_owner_home.sql", "rc_v21_owner_asqui.sql", "rc_v21_fresh.sql")
_PERSONA_COLUMNS = {("column", "team_session.persona"), ("column", "fleet_agent.persona")}
_ENDED_BY_COLUMN = {("column", "fleet_agent.ended_by")}
"""v34's product (#38), one step past the persona step on every shape."""
#: v35 (token metrics, #346): the two usage tables and their indexes, on top of v34.
_USAGE_STEP = {
    ("table", "usage_session"),
    ("index", "usage_session_project"),
    ("table", "usage_turn"),
    ("index", "usage_turn_session"),
    *(
        ("column", f"usage_session.{column}")
        for column in (
            "session_id",
            "project_id",
            "transcript_path",
            "state",
            "usage",
            "turns",
            "total_tokens",
            "weighted",
            "cost_usd",
            "started_at",
            "updated_at",
            "ended_at",
        )
    ),
    *(
        ("column", f"usage_turn.{column}")
        for column in (
            "id",
            "session_id",
            "project_id",
            "trace_id",
            "seq",
            "trigger",
            "recorded_at",
            "usage",
            "total_tokens",
            "weighted",
            "cost_usd",
            "scan_ms",
        )
    ),
}
_FIRST_PROMPT_COLUMNS = {
    ("column", "team_session.prompted_at"),
    ("column", "fleet_agent.first_prompt"),
    ("column", "fleet_agent.first_prompt_at"),
}
"""v36's products (P0-W1a): no dump of an older store holds them, every store reaches them."""
#: P1 WS-2's step (#279, v37; v35 on rc/p1-token-diet): who caused a board event, and its
#: details.
_EVENT_ACTOR_COLUMNS = {("column", "team_event.actor"), ("column", "team_event.meta")}
#: P2-W6's step (#199, v38; v36 on rc/p2-capacity-setup): a group's parent, rules and cap;
#: a task's origin.
_PARENT_COLUMNS = {
    ("column", "project_group.parent_project_id"),
    ("column", "project_group.route"),
    ("column", "project_group.max_agents"),
    ("column", "team_task.origin_task_id"),
    ("column", "team_task.origin_project_id"),
}
#: v39, work briefs (v34, then v36, on #306's and #341's branches, v37 and v38 on #417's): the
#: ``brief`` table and its per-board number index.
_BRIEF_STEP = {
    ("table", "brief"),
    ("index", "brief_number"),
    *(
        ("column", f"brief.{column}")
        for column in ("id", "project_id", "number", "revision", "data", "created_at", "updated_at")
    ),
}
#: v40, local usage stats: the account and repository a session ran under, the origin of a
#: turn row, the repository profile and the feature counters (``_PRODUCTS[39]``).
_USAGE_STATS_STEP = {
    ("table", "repo_profile"),
    ("table", "feature_use"),
    ("index", "usage_turn_recorded"),
    *(
        ("column", f"repo_profile.{column}")
        for column in (
            "repo_id", "name", "root", "remote", "host", "language", "size_bucket", "monorepo",
            "files", "refreshed_at",
        )
    ),
    *(("column", f"feature_use.{column}") for column in ("day", "name", "count")),
    ("column", "usage_session.account_slot"),
    ("column", "usage_session.account_type"),
    ("column", "usage_session.repo_id"),
    ("column", "usage_turn.source"),
}  # fmt: skip
# Main's v22 to v24 by name: SQLite's automatic index for the primary key is left out.
_MAINS_STEPS_PAST_21 = {
    ("table", "pending_revocation"),
    *(
        ("column", f"pending_revocation.{column}")
        for column in (
            "key_uid",
            "api_url",
            "workspace_id",
            "workspace_name",
            "project_id",
            "project_name",
            "detached_at",
            "last_error",
        )
    ),
    ("column", "project_explainability.api_url"),
}
_AT = "'2026-09-01T00:00:00+00:00'"
# A row in every table both lines share, among them what each step past 21 reads: a
# forgotten project that holds no mark (v23 leaves it as it is), and a destination with a
# key bound to its deployment (v24's backfill binds it).
_EVERY_TABLE = f"""
INSERT INTO project_group (id, name, created_at) VALUES ('grp_1', 'work', {_AT});
INSERT INTO project (id, root, name, linked_repos, created_at, codename, onboarded_at,
                     group_id, position)
    VALUES ('prj_live', '/w/live', 'live', '[]', {_AT}, 'kestrel', {_AT}, 'grp_1', 0);
INSERT INTO project (id, root, name, linked_repos, created_at, forgotten_at)
    VALUES ('prj_gone', '/w/gone', 'gone', '[]', {_AT}, {_AT});
INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at)
    VALUES ('ent_1', 'project', 'prj_live', 'a fact', '[]', 'cli', {_AT}, {_AT});
INSERT INTO prompt (id, project_id, text, source, created_at)
    VALUES ('prm_1', 'prj_live', 'hello', 'claude-code', {_AT});
INSERT INTO team_session (id, project_id, role, started_at, last_seen_at, account)
    VALUES ('ses_1', 'prj_live', 'coder', {_AT}, {_AT}, '1');
INSERT INTO team_event (id, project_id, session_id, kind, text, created_at)
    VALUES ('evt_1', 'prj_live', 'ses_1', 'note', 'on the board', {_AT});
INSERT INTO team_task (id, project_id, key, title, created_at, updated_at)
    VALUES ('tsk_1', 'prj_live', 'k1', 'a task', {_AT}, {_AT});
INSERT INTO team_meta (key, value) VALUES ('note', 'kept');
INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at, account_slot)
    VALUES ('agt_1', 'prj_live', 'coder-1', 'coder', '%1', '/w/live', {_AT}, 1);
INSERT INTO metric (trace_id, project_id, started_at) VALUES ('trc_1', 'prj_live', {_AT});
INSERT INTO claude_account (slot, config_dir, position, is_default, created_at)
    VALUES (1, '/h/.claude', 0, 1, {_AT});
INSERT INTO claude_usage (slot, fetched_at, session_percent) VALUES (1, {_AT}, 12.5);
INSERT INTO project_setting (project_id, key, value, set_at)
    VALUES ('prj_live', 'claude_account', '1', {_AT});
INSERT INTO ui_state (key, value, updated_at) VALUES ('fleet.selected', '"agt_1"', {_AT});
INSERT INTO project_destination (project_id, api_url, environment, workspace_id,
                                 workspace_name, set_at)
    VALUES ('prj_live', 'https://stg-api.example', 'stg', 7, 'the workspace', {_AT});
INSERT INTO project_explainability (project_id, target, key_path, set_at)
    VALUES ('prj_live', 'stg', '/h/stg.key', {_AT});
"""
# What only the RC's v21 holds: a persona on the session and on the agent.
_THE_RCS_OWN_ROWS = """
UPDATE team_session SET persona = 'architect';
UPDATE fleet_agent SET persona = 'architect';
"""
# What only main's v24 holds: a revocation still owed, and the key's deployment recorded.
_MAINS_OWN_ROWS = f"""
INSERT INTO pending_revocation (key_uid, api_url, workspace_id, workspace_name, project_id,
                                project_name, detached_at)
    VALUES ('key_1', 'https://api.example', 7, 'the workspace', 'prj_live', 'live', {_AT});
UPDATE project_explainability SET api_url = 'https://stg-api.example';
"""
# What main's v32 holds besides: its task numbered on its board, as 0.8.2 numbers every task.
_MAINS_V32_OWN_ROWS = "UPDATE team_task SET number = 1;"


def _replay(schema: str, rows: str = "", db: Path | None = None) -> Path:
    """The store ``schema`` froze, stamped with its version, holding ``rows``."""
    text = (_SCHEMAS / schema).read_text(encoding="utf-8")
    version = int(text.split("\n", 1)[0].removeprefix("-- user_version "))
    if db is None:
        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    try:
        raw.executescript(text + rows)
        raw.execute(f"PRAGMA user_version = {version}")
        raw.commit()
    finally:
        raw.close()
    return db


def _replayed_shape(schema: str) -> set[tuple[str, str]]:
    """The shape of the store ``schema`` froze (:func:`_shape`), built in memory."""
    conn = sqlite3.connect(":memory:")
    try:
        text = (_SCHEMAS / schema).read_text(encoding="utf-8")
        conn.executescript(text)
        return _shape(conn)
    finally:
        conn.close()


def _mains_steps_past_24() -> set[tuple[str, str]]:
    """Main's v25 to v32 by name (0.8.0 to 0.8.2): what its frozen v32 holds and its frozen v24
    does not."""
    return _replayed_shape("main_v32_fresh.sql") - _replayed_shape("main_v24_fresh.sql")


Columns = dict[str, list[str]]


def _rows(db: Path, columns: Columns | None = None) -> tuple[dict[str, object], Columns]:
    """Every row of every table, read on ``columns`` (default: the columns each has now)."""
    raw = sqlite3.connect(str(db))
    try:
        if columns is None:
            tables = raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            columns = {
                table: [row[1] for row in raw.execute(f'PRAGMA table_info("{table}")')]
                for (table,) in tables
            }
        rows: dict[str, object] = {}
        for table, names in columns.items():
            quoted = ", ".join(f'"{name}"' for name in names)
            rows[table] = sorted(
                raw.execute(f'SELECT {quoted} FROM "{table}"').fetchall(), key=repr
            )
        return rows, columns
    finally:
        raw.close()


@pytest.mark.parametrize("schema", _RC_V21_SCHEMAS)
def test_the_rcs_v21_store_reaches_this_schema_with_every_row_it_held(schema: str) -> None:
    """Both of the owner's stores were the RC's v21: the account registry and the persona
    columns in place, none of main's v22 to v32. This build runs v22
    (``pending_revocation``), v23 (a repair no row here needs), v24
    (``project_explainability.api_url``, whose backfill binds the key to its destination's
    deployment), main's v25 to v32 and #201's v33, whose persona columns it holds already.
    The store ends in exactly the schema a fresh store has, every row it held reads back
    unchanged on every column it had, and a second open changes nothing."""
    db = _replay(schema, _EVERY_TABLE + _THE_RCS_OWN_ROWS)
    before, columns = _rows(db)

    with store_session() as store:
        missing = store.missing_schema()
        agent = store.get_fleet_agent("agt_1")
        session = store.get_session("ses_1")
    after, _ = _rows(db, columns)
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
        bound = raw.execute("SELECT api_url FROM project_explainability").fetchall()
    finally:
        raw.close()
    assert (version, missing) == (SCHEMA_VERSION, [])
    assert shape == _built(SCHEMA_VERSION), "not the schema a fresh store has"
    assert after == before, "a row the store held changed on a column it had"
    assert bound == [("https://stg-api.example",)], "v24's backfill did not bind the key"
    assert agent is not None and (agent.persona, agent.account_slot) == ("architect", 1)
    assert session is not None and session.persona == "architect"
    assert _contents(db) == once, "a second open changed the store"


def test_mains_v24_store_gains_the_persona_columns_and_keeps_every_row() -> None:
    """A store main's 0.7.0 made: stamped 24, with no persona column. The ladder runs main's
    v25 to v32 and #201's v33, which adds ``team_session.persona`` and
    ``fleet_agent.persona``; they read NULL (no persona). It ends in main's v32 and the two
    persona columns, no row changes on a column it had, and a second open changes
    nothing."""
    db = _replay("main_v24_fresh.sql", _EVERY_TABLE + _MAINS_OWN_ROWS)
    before, columns = _rows(db)

    with store_session() as store:
        missing = store.missing_schema()
        agent = store.get_fleet_agent("agt_1")
        session = store.get_session("ses_1")
    after, _ = _rows(db, columns)
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
    finally:
        raw.close()
    assert (version, missing) == (SCHEMA_VERSION, [])
    assert shape == (
        _replayed_shape("main_v32_fresh.sql")
        | _PERSONA_COLUMNS
        | _ENDED_BY_COLUMN
        | _USAGE_STEP
        | _FIRST_PROMPT_COLUMNS
        | _EVENT_ACTOR_COLUMNS
        | _PARENT_COLUMNS
        | _BRIEF_STEP
        | _USAGE_STATS_STEP
    )
    assert shape == _built(SCHEMA_VERSION), "not the schema a fresh store has"
    assert after == before, "a row the store held changed on a column it had"
    assert agent is not None and (agent.persona, agent.account_slot) == (None, 1)
    assert session is not None and session.persona is None
    assert _contents(db) == once, "a second open changed the store"


@pytest.mark.parametrize(
    ("schema", "rows", "persona"),
    [
        pytest.param(
            "rc_v24_fresh.sql", _EVERY_TABLE + _MAINS_OWN_ROWS + _THE_RCS_OWN_ROWS, "architect",
            id="the RC's v24, the owner's stores since #233",
        ),
        pytest.param(
            "main_v32_fresh.sql",
            _EVERY_TABLE + _MAINS_OWN_ROWS + _MAINS_V32_OWN_ROWS,
            None,
            id="main 0.8.2's v32",
        ),
    ],
)  # fmt: skip
def test_a_v24_rc_store_and_a_v32_main_store_reach_this_schema_with_every_row(
    schema: str, rows: str, persona: str | None
) -> None:
    """The two shapes the 0.8.x merge adds. The RC's own v24, which both of the owner's
    stores are since #233: it runs main's v25 to v32 and #201's v33, and keeps its persona
    columns and what they hold. Main 0.8.2's v32: the ladder runs v33 alone, which adds the
    two persona columns, and they read NULL. Each ends in the schema a fresh store has,
    every row it held reads back unchanged on every column it had, and a second open
    changes nothing."""
    db = _replay(schema, rows)
    before, columns = _rows(db)

    with store_session() as store:
        missing = store.missing_schema()
        agent = store.get_fleet_agent("agt_1")
        session = store.get_session("ses_1")
    after, _ = _rows(db, columns)
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
    finally:
        raw.close()
    assert (version, missing) == (SCHEMA_VERSION, [])
    assert shape == _built(SCHEMA_VERSION), "not the schema a fresh store has"
    assert after == before, "a row the store held changed on a column it had"
    assert agent is not None and (agent.persona, agent.account_slot) == (persona, 1)
    assert session is not None and session.persona == persona
    assert _contents(db) == once, "a second open changed the store"


def test_one_schema_is_mains_v32_with_the_persona_columns_and_the_rcs_v21_with_mains_steps() -> (
    None
):
    """The schema every shape reaches, by name. A fresh store holds main's v32 and the two
    persona columns, and nothing else. That is also the RC's v21 (each of the three dumps)
    plus main's v22 to v32, and the RC's v24 plus main's v25 to v32, and nothing else. The
    persona columns are #201's v33, one step on main's v32 with an empty script and the two
    columns as its products, so a store that holds them already (the RC's) is not changed
    by it. v34 adds ``fleet_agent.ended_by`` (#38) on top, v35 the two token-usage tables
    (#346), v36 the first message's three columns (P0-W1a), v37 a board event's actor and
    details (P1 WS-2), v38 a group's parent manager and a task's origin (P2-W6), v39 the
    work-brief table, and nothing else."""
    fresh = _built(SCHEMA_VERSION)
    persona = _built(33)

    assert SCHEMA_VERSION == 40
    assert fresh == _built(39) | _USAGE_STATS_STEP
    assert _built(39) == _built(38) | _BRIEF_STEP
    assert _built(38) == _built(37) | _PARENT_COLUMNS
    assert _built(37) == _built(36) | _EVENT_ACTOR_COLUMNS
    assert _built(36) == persona | _ENDED_BY_COLUMN | _USAGE_STEP | _FIRST_PROMPT_COLUMNS
    assert _built(35) == persona | _ENDED_BY_COLUMN | _USAGE_STEP
    assert _built(34) == persona | _ENDED_BY_COLUMN
    assert persona == _replayed_shape("main_v32_fresh.sql") | _PERSONA_COLUMNS
    for schema in _RC_V21_SCHEMAS:
        assert persona == _replayed_shape(schema) | _MAINS_STEPS_PAST_21 | _mains_steps_past_24()
    assert persona == _replayed_shape("rc_v24_fresh.sql") | _mains_steps_past_24()
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        assert _shape(raw) == fresh, "a store opened fresh is not the ladder's schema"
    finally:
        raw.close()


# --- the persona step (v33, #201): every store in the wild reaches it --------------------
#
# The persona line stamped its two columns as its own v15 (PERSONAS_AT_15); the captain
# RC carried them as products of main's v15 up to its v24 (#240, "The store"); main's own
# stores are at v32 without them; and a fresh store has nothing. Each opens with this
# build to the same shape at the same version, with the rows it held. Take v33 out
# (its empty script and its ``_PRODUCTS[32]`` entry) and every case goes red: the
# version, and where no line put them, the columns the store reads.
PERSONA_COLUMNS = """
ALTER TABLE team_session ADD COLUMN persona TEXT;
ALTER TABLE fleet_agent ADD COLUMN persona TEXT;
"""
_A_SESSION_AND_AN_AGENT = """
INSERT INTO team_session (id, project_id, started_at, last_seen_at)
    VALUES ('ses_old', 'prj_used', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at)
    VALUES ('agt_old', 'prj_used', 'coder-1', 'coder', '%1', '/w/used',
            '2026-09-01T00:00:00+00:00');
"""
_PERSONA_COHORTS: tuple[tuple[str, int, str, int, str | None], ...] = (
    # (label, steps run, what ran after them, the stamp, the persona the old row reads)
    ("main v32", 32, _A_SESSION_AND_AN_AGENT, 32, None),
    (
        "persona-15",
        14,
        _A_SESSION_AND_AN_AGENT
        + PERSONA_COLUMNS
        + "UPDATE team_session SET persona = 'architect';"
        + "UPDATE fleet_agent SET persona = 'architect';",
        15,
        "architect",
    ),
    (
        "captain RC v24",
        24,
        _A_SESSION_AND_AN_AGENT
        + PERSONA_COLUMNS
        + "UPDATE team_session SET persona = 'skeptic';"
        + "UPDATE fleet_agent SET persona = 'skeptic';",
        24,
        "skeptic",
    ),
)


def _persona_shape(db: Path) -> tuple[int, set[tuple[str, str]], dict[str, int]]:
    """``db``'s version, its shape, and whether each persona column is NOT NULL."""
    raw = sqlite3.connect(str(db))
    try:
        notnull = {
            table: {row[1]: row[3] for row in raw.execute(f"PRAGMA table_info({table})")}["persona"]
            for table in ("team_session", "fleet_agent")
        }
        return raw.execute("PRAGMA user_version").fetchone()[0], _shape(raw), notnull
    finally:
        raw.close()


@pytest.mark.parametrize(
    ("label", "steps", "after", "stamp", "persona"),
    _PERSONA_COHORTS,
    ids=[c[0] for c in _PERSONA_COHORTS],
)
def test_every_store_in_the_wild_reaches_the_persona_step_with_its_rows(
    label: str, steps: int, after: str, stamp: int, persona: str | None
) -> None:
    """Opened with this build: every table, index and column of the current schema and
    nothing else, ``user_version == SCHEMA_VERSION``, both persona columns nullable, the
    old rows read back with the persona their line gave them (NULL where none did), and
    the store writes and reads a persona on each table. A second open changes nothing."""
    from datetime import UTC, datetime

    from aisquare.models import TeamSession

    db = _at_version(steps, after=_TWO_PROJECTS + after, stamp=stamp)

    store = open_store()
    try:
        old_session = store.get_session("ses_old")
        old_agent = store.get_fleet_agent("agt_old")
        attached = store.set_fleet_agent_persona("agt_old", "mentor")
        now = datetime(2026, 10, 20, tzinfo=UTC)
        fresh = TeamSession(id="ses_new", project_id="prj_used", started_at=now, last_seen_at=now)
        store.upsert_session(fresh.model_copy(update={"persona": "careful"}))
        restarted = store.upsert_session(fresh)  # a start naming none keeps the recorded one
    finally:
        store.close()
    _schema, rows = _contents(db)
    open_store().close()
    _schema_again, rows_again = _contents(db)
    version, shape, notnull = _persona_shape(db)

    assert version == SCHEMA_VERSION == 40, label
    assert shape == _built(SCHEMA_VERSION), label
    assert notnull == {"team_session": 0, "fleet_agent": 0}, label
    assert old_session is not None and old_session.persona == persona, label
    assert old_agent is not None and old_agent.persona == persona, label
    assert attached.persona == "mentor", label
    assert restarted.persona == "careful", label
    assert rows_again == rows, f"{label}: a second open changed a row"


def test_a_fresh_store_is_built_with_the_persona_step() -> None:
    """No file at all: v1 to v38, the same shape as every cohort above reaches."""
    open_store().close()
    version, shape, notnull = _persona_shape(_db_path())

    assert version == SCHEMA_VERSION == 40
    assert shape == _built(SCHEMA_VERSION)
    assert {("column", "team_session.persona"), ("column", "fleet_agent.persona")} <= shape
    assert notnull == {"team_session": 0, "fleet_agent": 0}


def test_the_persona_step_is_the_columns_alone_on_top_of_main_s_ladder() -> None:
    """v33 is one step with an empty script, its two columns in ``_PRODUCTS[32]`` — the
    v29/v31/v32 pattern — so the presence pass gives them to any store that lacks them,
    whatever ``user_version`` says, and #208's ``_PREPARE[15]`` converge is not needed."""
    from aisquare.core.store import _MIGRATIONS, _PREPARE, _PRODUCTS, _Products

    assert _MIGRATIONS[32] == ""
    assert _PRODUCTS[32] == _Products(
        columns=(("team_session", "persona", "TEXT"), ("fleet_agent", "persona", "TEXT"))
    )
    assert 15 not in _PREPARE and 32 not in _PREPARE
    assert not any(
        column == "persona" for step, products in _PRODUCTS.items() if step != 32
        for _table, column, _ in products.columns
    )  # fmt: skip


# --- two branches stamped v34 (#42): #38's ended_by and #346's usage tables ---------------
#
# #274 and #346 each added a step v34: #38's ``fleet_agent.ended_by``, and #346's two token
# usage tables. Merged, #38's stays v34 and #346's is v35. A store that ran either branch reads
# v34 and holds only its own step's products, and must reach v35 with both. #346's step is
# replayed from its DDL as that branch shipped it (827da39f), frozen here, never this ladder's.

#: #346's v34 as its branch shipped it, the store a run of that branch left.
_USAGE_V34_OF_346 = """
CREATE TABLE IF NOT EXISTS usage_session (
    session_id      TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    transcript_path TEXT,
    state           TEXT NOT NULL DEFAULT '{}',
    usage           TEXT NOT NULL DEFAULT '{}',
    turns           INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    weighted        REAL NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    ended_at        TEXT
);
CREATE INDEX IF NOT EXISTS usage_session_project ON usage_session (project_id, updated_at);

CREATE TABLE IF NOT EXISTS usage_turn (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    trace_id     TEXT,
    seq          INTEGER NOT NULL,
    trigger      TEXT NOT NULL CHECK (trigger IN ('stop', 'stop_failure', 'session_end')),
    recorded_at  TEXT NOT NULL,
    usage        TEXT NOT NULL,
    total_tokens INTEGER NOT NULL,
    weighted     REAL NOT NULL,
    cost_usd     REAL NOT NULL,
    scan_ms      REAL
);
CREATE INDEX IF NOT EXISTS usage_turn_session ON usage_turn (session_id, seq);
INSERT INTO usage_session (session_id, project_id, state, usage, turns, total_tokens,
                           weighted, cost_usd, started_at, updated_at)
    VALUES ('ses_old', 'prj_used', '{"offset": 812}', '{}', 2, 4096, 1.5, 0.25,
            '2026-10-20T00:00:00+00:00', '2026-10-20T00:05:00+00:00');
INSERT INTO usage_turn (id, session_id, project_id, seq, trigger, recorded_at, usage,
                        total_tokens, weighted, cost_usd)
    VALUES ('utn_old', 'ses_old', 'prj_used', 1, 'stop', '2026-10-20T00:05:00+00:00', '{}',
            4096, 1.5, 0.25);
"""

#: What a run of #274's v34 left: an agent row that ended on aisquare's word.
_ENDED_BY_V34_OF_274 = (
    "UPDATE fleet_agent SET ended_at = '2026-10-20T00:09:00+00:00', "
    "exit_status = 0, ended_by = 'stopped' WHERE id = 'agt_old';"
)


def test_the_two_v34_steps_are_v34_and_v35_each_idempotent() -> None:
    """#38's step stays v34 (an empty script, the column in ``_PRODUCTS[33]``); #346's is
    v35 (``CREATE ... IF NOT EXISTS`` only, its tables and indexes in ``_PRODUCTS[34]``).
    Run twice on one store, each leaves what it left once."""
    from aisquare.core.store import _MIGRATIONS, _PRODUCTS, _Products, _run_step

    assert SCHEMA_VERSION == len(_MIGRATIONS) == 40
    assert _MIGRATIONS[33] == ""
    assert _PRODUCTS[33] == _Products(columns=(("fleet_agent", "ended_by", "TEXT"),))
    assert _PRODUCTS[34] == _Products(
        ("usage_session", "usage_session_project", "usage_turn", "usage_turn_session")
    )
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            _run_step(conn, step)
        once = _shape(conn)
        _run_step(conn, 33)
        _run_step(conn, 34)
        assert _shape(conn) == once
    finally:
        conn.close()
    assert once == (
        _built(33)
        | _ENDED_BY_COLUMN
        | _USAGE_STEP
        | _FIRST_PROMPT_COLUMNS
        | _EVENT_ACTOR_COLUMNS
        | _PARENT_COLUMNS
        | _BRIEF_STEP
        | _USAGE_STATS_STEP
    )


@pytest.mark.parametrize(
    ("steps", "after", "label"),
    [
        pytest.param(
            33, _A_SESSION_AND_AN_AGENT + _USAGE_V34_OF_346, "#346", id="#346's v34: usage tables"
        ),
        pytest.param(
            34, _A_SESSION_AND_AN_AGENT + _ENDED_BY_V34_OF_274, "#38", id="#38's v34: ended_by"
        ),
    ],
)  # fmt: skip
def test_a_store_from_either_branchs_v34_reaches_v35_with_both_steps(
    steps: int, after: str, label: str
) -> None:
    """Stamped v34 by either branch, a store opens at v35 with ``fleet_agent.ended_by`` AND
    the two usage tables and their indexes: the schema a fresh store has, nothing missing.
    Its rows read back unchanged on every column they had; the store then writes and reads
    both steps' data, and a second open changes nothing."""
    from aisquare.models import SessionUsage, TurnUsage

    db = _at_version(steps, after=_TWO_PROJECTS + after, stamp=34)
    before, columns = _rows(db)
    raw = sqlite3.connect(str(db))
    try:
        stamped = _shape(raw)
    finally:
        raw.close()
    assert (("column", "fleet_agent.ended_by") in stamped) == (label == "#38"), "the fixture"
    assert (("table", "usage_session") in stamped) == (label == "#346"), "the fixture"

    with store_session() as store:
        missing = store.missing_schema()
        old_agent = store.get_fleet_agent("agt_old")
        old_usage = store.session_usage("ses_old")
    after_rows, _ = _rows(db, columns)

    now = datetime(2026, 10, 24, tzinfo=UTC)
    with store_session() as store:
        ended = store.end_fleet_agent("agt_1", exit_status=143, ended_by="died")
        wrote = store.record_usage(
            SessionUsage(
                session_id="ses_new", project_id="prj_used", started_at=now, updated_at=now
            ),
            TurnUsage(
                id="utn_new",
                session_id="ses_new",
                project_id="prj_used",
                seq=1,
                trigger="stop",
                recorded_at=now,
                total_tokens=7,
            ),
            previous_state=None,
        )
        turns = store.turn_usages("ses_new")
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
    finally:
        raw.close()
    assert (version, missing) == (SCHEMA_VERSION, []), label
    assert shape == _built(SCHEMA_VERSION), f"{label}: not the schema a fresh store has"
    assert shape >= _ENDED_BY_COLUMN | _USAGE_STEP, label
    assert after_rows == before, f"{label}: a row the store held changed on a column it had"
    assert old_agent is not None
    if label == "#38":
        assert old_agent.ended_by == "stopped", "#38's own row keeps how it ended"
        assert old_usage is None
    else:
        assert old_agent.ended_by is None, "a row from before v34 reads NULL"
        assert old_usage is not None and (old_usage.turns, old_usage.total_tokens) == (2, 4096)
        assert old_usage.state == '{"offset": 812}', "#346's scan resumes where it stopped"
    assert ended.ended_by == "died"
    assert wrote and [turn.total_tokens for turn in turns] == [7]
    assert _contents(db) == once, f"{label}: a second open changed the store"


# --- the trains' own steps on main's v35 (sync of main 8eb18a8a, 2026-10-24) -------------------
#
# Main owns v34 (#38's ``ended_by``) and v35 (#346's usage tables). Each release train stamped
# its own step at its branch's top, and moves past main's on the sync, in train order:
# rc/p0-reliability's v34 (P0-W1a's first-message columns) is v36 here, and
# rc/p1-token-diet's v35 (P1 WS-2's event columns, on top of its own v34 = P0-W1a's) is v37,
# and rc/p2-capacity-setup's v36 (P2-W6's parent columns, on top of v34 and v35) is v38.
# A store that ran a
# lineage reads that lineage's top version with only that lineage's products, and must reach
# head with every product of every lineage. A train's step is replayed from its DDL as the
# branch shipped it, frozen here, never this ladder's; a later train adds its own case.

#: rc/p0-reliability's v34 as it shipped (P0-W1a, f1de966a): an empty script, the three
#: columns added by the ladder, with what a run of it wrote on a session and an agent.
_FIRST_PROMPT_V34_OF_RC_P0 = """
ALTER TABLE team_session ADD COLUMN prompted_at TEXT;
ALTER TABLE fleet_agent ADD COLUMN first_prompt TEXT;
ALTER TABLE fleet_agent ADD COLUMN first_prompt_at TEXT;
UPDATE team_session SET prompted_at = '2026-10-23T00:01:00+00:00' WHERE id = 'ses_old';
UPDATE fleet_agent SET first_prompt = 'acked', first_prompt_at = '2026-10-23T00:01:00+00:00'
    WHERE id = 'agt_old';
"""

#: rc/p1-token-diet's v35 as it shipped (P1 WS-2, b2ecee3f), on top of its v34 (rc/p0's
#: above): an empty script, the two columns added by the ladder, with an event a run of it
#: wrote carrying both.
_EVENT_ACTOR_V35_OF_RC_P1 = (
    _FIRST_PROMPT_V34_OF_RC_P0
    + """
ALTER TABLE team_event ADD COLUMN actor TEXT;
ALTER TABLE team_event ADD COLUMN meta TEXT;
INSERT INTO team_event (id, project_id, session_id, kind, text, created_at, actor, meta)
    VALUES ('evt_old', 'prj_used', 'ses_old', 'agent_exited', 'coder-1 exited (1)',
            '2026-10-23T00:02:00+00:00', 'user', '{"status": 1}');
"""
)

#: rc/p2-capacity-setup's v36 as it shipped (P2-W6, #199), on top of its v34 and v35 (rc/p1's
#: above): an empty script, the five columns added by the ladder, with a group a run of it
#: gave a parent, rules and a cap.
_PARENT_V36_OF_RC_P2 = (
    _EVENT_ACTOR_V35_OF_RC_P1
    + """
ALTER TABLE project_group ADD COLUMN parent_project_id TEXT;
ALTER TABLE project_group ADD COLUMN route TEXT;
ALTER TABLE project_group ADD COLUMN max_agents INTEGER;
ALTER TABLE team_task ADD COLUMN origin_task_id TEXT;
ALTER TABLE team_task ADD COLUMN origin_project_id TEXT;
INSERT INTO project_group (id, name, created_at, parent_project_id, route, max_agents)
    VALUES ('grp_old', 'app', '2026-10-23T00:03:00+00:00', 'prj_seen', '{"off": true}', 4);
"""
)

#: What a run of main's v35 left: #38's stopped row and #346's scan state and turn.
_MAINS_V35_ROWS = (
    _ENDED_BY_V34_OF_274
    + """
INSERT INTO usage_session (session_id, project_id, state, usage, turns, total_tokens,
                           weighted, cost_usd, started_at, updated_at)
    VALUES ('ses_old', 'prj_used', '{"offset": 812}', '{}', 2, 4096, 1.5, 0.25,
            '2026-10-20T00:00:00+00:00', '2026-10-20T00:05:00+00:00');
INSERT INTO usage_turn (id, session_id, project_id, seq, trigger, recorded_at, usage,
                        total_tokens, weighted, cost_usd)
    VALUES ('utn_old', 'ses_old', 'prj_used', 1, 'stop', '2026-10-20T00:05:00+00:00', '{}',
            4096, 1.5, 0.25);
"""
)


def test_the_first_prompt_step_is_v36_past_mains_two_and_idempotent() -> None:
    """rc/p0's step moved from v34 to v36 on the sync: an empty script, its three columns in
    ``_PRODUCTS[35]``, main's v34 and v35 under it as main has them. Run twice, it leaves what
    it left once."""
    from aisquare.core.store import _MIGRATIONS, _PRODUCTS, _Products, _run_step

    assert SCHEMA_VERSION == len(_MIGRATIONS) == 40
    assert _MIGRATIONS[35] == ""
    assert _PRODUCTS[33] == _Products(columns=(("fleet_agent", "ended_by", "TEXT"),))
    assert _PRODUCTS[35] == _Products(
        columns=(
            ("team_session", "prompted_at", "TEXT"),
            ("fleet_agent", "first_prompt", "TEXT"),
            ("fleet_agent", "first_prompt_at", "TEXT"),
        )
    )
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            _run_step(conn, step)
        once = _shape(conn)
        _run_step(conn, 35)
        assert _shape(conn) == once
    finally:
        conn.close()
    assert once == (
        _built(35)
        | _FIRST_PROMPT_COLUMNS
        | _EVENT_ACTOR_COLUMNS
        | _PARENT_COLUMNS
        | _BRIEF_STEP
        | _USAGE_STATS_STEP
    )


def test_the_event_actor_step_is_v37_past_the_first_prompt_and_idempotent() -> None:
    """rc/p1's step moved from v35 to v37 on the sync: an empty script, its two columns in
    ``_PRODUCTS[36]``, past main's v34 and v35 and P0-W1a's v36. Run twice, it leaves what it
    left once."""
    from aisquare.core.store import _MIGRATIONS, _PRODUCTS, _Products, _run_step

    assert SCHEMA_VERSION == len(_MIGRATIONS) == 40
    assert _MIGRATIONS[36] == ""
    assert _PRODUCTS[36] == _Products(
        columns=(("team_event", "actor", "TEXT"), ("team_event", "meta", "TEXT"))
    )
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            _run_step(conn, step)
        once = _shape(conn)
        _run_step(conn, 36)
        assert _shape(conn) == once
    finally:
        conn.close()
    assert once == (
        _built(36) | _EVENT_ACTOR_COLUMNS | _PARENT_COLUMNS | _BRIEF_STEP | _USAGE_STATS_STEP
    )


def test_the_parent_step_is_v38_past_the_event_actor_and_idempotent() -> None:
    """rc/p2's step moved from v36 to v38 on the sync: an empty script, its five columns in
    ``_PRODUCTS[37]``, past main's v34 and v35, P0-W1a's v36 and P1 WS-2's v37. Run twice, it
    leaves what it left once."""
    from aisquare.core.store import _MIGRATIONS, _PRODUCTS, _Products, _run_step

    assert SCHEMA_VERSION == len(_MIGRATIONS) == 40
    assert _MIGRATIONS[37] == ""
    assert _PRODUCTS[37] == _Products(
        columns=(
            ("project_group", "parent_project_id", "TEXT"),
            ("project_group", "route", "TEXT"),
            ("project_group", "max_agents", "INTEGER"),
            ("team_task", "origin_task_id", "TEXT"),
            ("team_task", "origin_project_id", "TEXT"),
        )
    )
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            _run_step(conn, step)
        once = _shape(conn)
        _run_step(conn, 37)
        assert _shape(conn) == once
    finally:
        conn.close()
    assert once == _built(37) | _PARENT_COLUMNS | _BRIEF_STEP | _USAGE_STATS_STEP


@pytest.mark.parametrize(
    ("steps", "after", "stamp", "label"),
    [
        pytest.param(
            35, _A_SESSION_AND_AN_AGENT + _MAINS_V35_ROWS, 35, "main",
            id="main's v35: ended_by and the usage tables",
        ),
        pytest.param(
            33, _A_SESSION_AND_AN_AGENT + _FIRST_PROMPT_V34_OF_RC_P0, 34, "rc/p0",
            id="rc/p0's v34: the first-message columns",
        ),
        pytest.param(
            33, _A_SESSION_AND_AN_AGENT + _EVENT_ACTOR_V35_OF_RC_P1, 35, "rc/p1",
            id="rc/p1's v35: the first-message and event-actor columns",
        ),
        pytest.param(
            33, _A_SESSION_AND_AN_AGENT + _PARENT_V36_OF_RC_P2, 36, "rc/p2",
            id="rc/p2's v36: the first-message, event-actor and parent columns",
        ),
    ],
)  # fmt: skip
def test_a_store_from_main_or_a_train_reaches_head_with_every_lineages_steps(
    steps: int, after: str, stamp: int, label: str
) -> None:
    """Stamped by main's v35, rc/p0's v34, rc/p1's v35 or rc/p2's v36, a store opens at head
    with ``fleet_agent.ended_by``, the usage tables and their indexes, the first message's
    three columns, a board event's actor and details AND a group's parent, rules and cap and
    a task's origin: the schema a fresh store has, nothing missing. Its rows read back
    unchanged on every column they had; the store then writes and reads every lineage's
    data, and a second open changes nothing.

    Bites: with the presence pass off, the rc/p0 store fails on ``ended_by``; with v36 out
    of the ladder, main's store has no ``first_prompt``; with v35's CREATE not ``IF NOT
    EXISTS``, a store that holds the tables fails to open; with v37 out of the ladder, main's
    and rc/p0's stores have no ``team_event.actor``; with v38 out of it, no store but rc/p2's
    has ``project_group.parent_project_id``; with the presence pass off, rc/p2's store fails
    on ``ended_by`` too."""
    from aisquare.models import FleetAgent, SessionUsage, TeamEvent

    db = _at_version(steps, after=_TWO_PROJECTS + after, stamp=stamp)
    before, columns = _rows(db)
    raw = sqlite3.connect(str(db))
    try:
        stamped = _shape(raw)
    finally:
        raw.close()
    assert (("column", "fleet_agent.ended_by") in stamped) == (label == "main"), "the fixture"
    assert (("table", "usage_session") in stamped) == (label == "main"), "the fixture"
    assert (("column", "fleet_agent.first_prompt") in stamped) == (label != "main"), "fixture"
    assert (("column", "team_event.actor") in stamped) == (label in ("rc/p1", "rc/p2")), "fixture"
    assert (("column", "project_group.route") in stamped) == (label == "rc/p2"), "the fixture"

    with store_session() as store:
        missing = store.missing_schema()
        old_agent = store.get_fleet_agent("agt_old")
        old_session = store.get_session("ses_old")
        old_usage = store.session_usage("ses_old")
        old_event = store.get_event("evt_old")
        old_group = store.group_settings("grp_old")
    after_rows, _ = _rows(db, columns)

    now = datetime(2026, 10, 24, tzinfo=UTC)
    with store_session() as store:
        posted = store.add_team_event(
            TeamEvent(
                id="evt_new",
                project_id="prj_used",
                kind="agent_exited",
                text="coder-2 exited (0)",
                created_at=now,
                actor="user",
                meta={"status": 0},
            )
        )
        given = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_new",
                project_id="prj_used",
                label="coder-2",
                role="coder",
                pane_id="%2",
                session_id="ses_new",
                cwd=Path("/w/used"),
                created_at=now,
                first_prompt="pending",
                first_prompt_at=now,
            )
        )
        taken = store.mark_prompted("ses_new", now, agent_id="agt_new")
        ended = store.end_fleet_agent("agt_1", exit_status=143, ended_by="died")
        wrote = store.record_usage(
            SessionUsage(
                session_id="ses_new", project_id="prj_used", started_at=now, updated_at=now
            ),
            None,
            previous_state=None,
        )
        acked = store.get_fleet_agent("agt_new")
        read_back = store.get_event_by_seq(posted.seq)
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
    finally:
        raw.close()
    assert (version, missing) == (SCHEMA_VERSION, []), label
    assert shape == _built(SCHEMA_VERSION), f"{label}: not the schema a fresh store has"
    assert shape >= (
        _ENDED_BY_COLUMN
        | _USAGE_STEP
        | _FIRST_PROMPT_COLUMNS
        | _EVENT_ACTOR_COLUMNS
        | _PARENT_COLUMNS
    ), label
    assert after_rows == before, f"{label}: a row the store held changed on a column it had"
    assert old_agent is not None and old_session is not None
    if label in ("rc/p1", "rc/p2"):
        assert old_event is not None
        assert (old_event.actor, old_event.meta) == ("user", {"status": 1}), "rc/p1's own event"
    else:
        assert old_event is None
    if label == "rc/p2":
        assert old_group == GroupSettings(
            group_id="grp_old", parent_project_id="prj_seen", route={"off": True}, max_agents=4
        ), "rc/p2's own group keeps its parent, rules and cap"
    else:
        assert old_group == GroupSettings(group_id="grp_old"), "no group, no settings"
    assert read_back is not None and (read_back.actor, read_back.meta) == ("user", {"status": 0})
    if label == "main":
        assert old_agent.ended_by == "stopped", "#38's own row keeps how it ended"
        assert old_usage is not None and old_usage.state == '{"offset": 812}'
        assert (old_agent.first_prompt, old_session.prompted_at) == (None, None), "not tracked"
    else:
        assert (old_agent.first_prompt, old_agent.ended_by, old_usage) == ("acked", None, None)
        assert old_session.prompted_at == datetime(2026, 10, 23, 0, 1, tzinfo=UTC)
    assert given.first_prompt == "pending" and taken
    assert acked is not None and (acked.first_prompt, acked.first_prompt_at) == ("acked", now)
    assert ended.ended_by == "died" and wrote
    assert _contents(db) == once, f"{label}: a second open changed the store"


# --- the stack's step: work briefs, stamped v34, v36 and v37 before it was v38 -------------
#
# #306 and #341 stamped work briefs v34 on their branches, and v36 once they had merged main's
# v34 and v35. #417 stamped it v37, after rc/p0's v36 (P0-W1a). Merged after rc/p1's v37
# (P1 WS-2), the step is v38. A store that ran those branches reads v34 with ``brief`` and no
# ``fleet_agent.ended_by``, usage tables, first-message or event-actor columns; v36 with
# ``brief``, the column and the tables but neither set of columns; or v37 with ``brief`` and
# the first-message columns but no event-actor columns. Each must reach v38 with every step.
# The briefs step is replayed from its DDL as those branches shipped it (1f669088, unchanged
# since), frozen here.

#: The stack's briefs step as its branches shipped it, with one brief a run of them left.
_BRIEF_V34_OF_THE_STACK = """
CREATE TABLE IF NOT EXISTS brief (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS brief_number ON brief (project_id, number);
INSERT INTO brief (id, project_id, number, revision, data, created_at, updated_at)
    VALUES ('brf_old', 'prj_used', 1, 3, '{"title": "Login"}',
            '2026-10-23T00:00:00+00:00', '2026-10-23T00:05:00+00:00');
"""


def test_the_brief_step_is_v39_and_idempotent() -> None:
    """The briefs step is v39, past P0-W1a's v36, P1 WS-2's v37 and P2-W6's v38: #417 took
    v38 and main's P2 took v38 in the same release, and P2 kept it (``CREATE ... IF NOT
    EXISTS`` only, its table and index in ``_PRODUCTS[38]``). Run twice on one store, it
    leaves what it left once."""
    from aisquare.core.store import _MIGRATIONS, _PRODUCTS, _Products, _run_step

    assert SCHEMA_VERSION == len(_MIGRATIONS) == 40
    assert _PRODUCTS[38] == _Products(("brief", "brief_number"))
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            _run_step(conn, step)
        once = _shape(conn)
        _run_step(conn, 38)
        assert _shape(conn) == once
    finally:
        conn.close()
    assert once == _built(38) | _BRIEF_STEP | _USAGE_STATS_STEP


@pytest.mark.parametrize(
    ("steps", "stamp"),
    [
        pytest.param(33, 34, id="the stack's v34: brief, before main's v34 and v35"),
        pytest.param(35, 36, id="the stack's v36: brief on main's v35"),
        pytest.param(36, 37, id="#417's v37: brief on P0-W1a's v36"),
        pytest.param(37, 38, id="#417's v38: brief on P1 WS-2's v37, without P2-W6's v38"),
    ],
)  # fmt: skip
def test_a_store_from_the_stacks_v34_to_v38_reaches_v39_with_every_step(
    steps: int, stamp: int
) -> None:
    """Stamped v34 by the stack, with ``brief`` and neither ``fleet_agent.ended_by``, the
    usage tables nor the first message's or a board event's actor columns; v36, with
    ``brief``, the column and the tables but neither set of columns; v37 by #417, with
    ``brief`` and the first message's columns but not the actor's; or v38 by #417 as pushed
    (ce1530ae), with everything to P1's v37 and ``brief`` but not P2-W6's v38 columns, which
    its stamp claims: a store opens at v39 with all of them, the schema a fresh store has.
    The presence pass adds what the stamp claims and the store lacks, the ladder runs the
    steps above the stamp, and v39 finds ``brief`` in place. Its brief and every other row
    read back unchanged, and a second open changes nothing.

    Bites: with the presence pass off, each of the four fails: the v34 store has no
    ``ended_by``, the v36 store no ``first_prompt``, the v37 store no ``team_event.actor``,
    and the v38 store no ``project_group.parent_project_id`` (as with P2's columns taken
    out of ``_PRODUCTS``)."""
    db = _at_version(
        steps,
        after=_TWO_PROJECTS + _A_SESSION_AND_AN_AGENT + _BRIEF_V34_OF_THE_STACK,
        stamp=stamp,
    )
    before, columns = _rows(db)
    raw = sqlite3.connect(str(db))
    try:
        stamped = _shape(raw)
    finally:
        raw.close()
    assert stamped >= _BRIEF_STEP, "the fixture"
    assert not _PARENT_COLUMNS & stamped, "the fixture: P2-W6's columns are what it lacks"
    if stamp == 38:
        assert stamped >= _EVENT_ACTOR_COLUMNS, "the fixture"
    else:
        assert not _EVENT_ACTOR_COLUMNS & stamped, "the fixture"
    if stamp in (37, 38):
        assert stamped >= _FIRST_PROMPT_COLUMNS, "the fixture"
    else:
        assert not _FIRST_PROMPT_COLUMNS & stamped, "the fixture"
    if stamp >= 36:
        assert stamped >= _ENDED_BY_COLUMN | _USAGE_STEP, "the fixture"
    else:
        assert not (_ENDED_BY_COLUMN | _USAGE_STEP) & stamped, "the fixture"

    with store_session() as store:
        missing = store.missing_schema()
        old_brief = store.get_brief("prj_used", "1")
        old_agent = store.get_fleet_agent("agt_old")
    after_rows, _ = _rows(db, columns)
    with store_session() as store:
        ended = store.end_fleet_agent("agt_1", exit_status=143, ended_by="died")
        usage = store.session_usage("ses_none")
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
    finally:
        raw.close()
    label = f"the v{stamp} store"
    every = (
        _ENDED_BY_COLUMN
        | _USAGE_STEP
        | _FIRST_PROMPT_COLUMNS
        | _EVENT_ACTOR_COLUMNS
        | _PARENT_COLUMNS
    )
    assert (version, missing) == (SCHEMA_VERSION, []), label
    assert shape == _built(SCHEMA_VERSION), f"{label}: not the schema a fresh store has"
    assert shape >= every | _BRIEF_STEP, label
    assert after_rows == before, f"{label}: a row the store held changed on a column it had"
    assert old_brief is not None and (old_brief.id, old_brief.revision) == ("brf_old", 3)
    assert old_agent is not None, label
    assert (old_agent.ended_by, old_agent.first_prompt) == (None, None), "rows from before"
    assert ended.ended_by == "died" and usage is None
    assert _contents(db) == once, f"{label}: a second open changed the store"


def test_a_store_main_stamped_v38_takes_the_briefs_step() -> None:
    """Main's P2-W6 took v38 too, so a store a build of main (8425a08d) opened reads v38 with
    the parent columns and no ``brief``: it opens at v39 with the table, the schema a fresh
    store has, its rows unchanged, and a second open changes nothing."""
    db = _at_version(38, after=_TWO_PROJECTS + _A_SESSION_AND_AN_AGENT)
    before, columns = _rows(db)
    raw = sqlite3.connect(str(db))
    try:
        stamped = _shape(raw)
    finally:
        raw.close()
    assert stamped >= _PARENT_COLUMNS and not _BRIEF_STEP & stamped, "the fixture"

    with store_session() as store:
        missing = store.missing_schema()
    after_rows, _ = _rows(db, columns)
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
    finally:
        raw.close()
    assert (version, missing) == (SCHEMA_VERSION, [])
    assert shape == _built(SCHEMA_VERSION) and shape >= _BRIEF_STEP
    assert after_rows == before
    assert _contents(db) == once, "a second open changed the store"


def test_the_usage_stats_step_is_v40_and_idempotent() -> None:
    """The usage-stats step is v40: an ``IF NOT EXISTS`` script for ``repo_profile``,
    ``feature_use`` and the range index, and four nullable columns in ``_PRODUCTS[39]``. Run
    twice on one store, it leaves what it left once; a fresh store has all of it."""
    from aisquare.core.store import _MIGRATIONS, _PRODUCTS, _Products, _run_step

    assert SCHEMA_VERSION == len(_MIGRATIONS) == 40
    assert _PRODUCTS[39] == _Products(
        ("repo_profile", "feature_use", "usage_turn_recorded"),
        (
            ("usage_session", "account_slot", "INTEGER"),
            ("usage_session", "account_type", "TEXT"),
            ("usage_session", "repo_id", "TEXT"),
            ("usage_turn", "source", "TEXT"),
        ),
    )
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            _run_step(conn, step)
        once = _shape(conn)
        _run_step(conn, 39)
        assert _shape(conn) == once
    finally:
        conn.close()
    assert once == _built(39) | _USAGE_STATS_STEP
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        assert _shape(raw) == once, "a fresh store is not the schema the ladder builds"
    finally:
        raw.close()


def test_a_store_at_the_v39_schema_gains_the_usage_stats_step_and_keeps_every_row() -> None:
    """A store as rc/0.9.1 left it (v39) holds usage rows that know no account, repository
    or origin: it opens at v40, each of those reads NULL, the rows are unchanged and a second
    open changes nothing. The new tables take writes at once."""
    db = _at_version(39)
    raw = sqlite3.connect(str(db))
    try:
        raw.execute(
            "INSERT INTO usage_session (session_id, project_id, transcript_path, state, usage, "
            "turns, total_tokens, weighted, cost_usd, started_at, updated_at) VALUES "
            "('old', 'prj_a', '/t/old.jsonl', '{}', '{}', 1, 9, 0, 0, '2026-10-01T00:00:00+00:00', "
            "'2026-10-01T00:00:00+00:00')"
        )
        raw.execute(
            "INSERT INTO usage_turn (id, session_id, project_id, seq, trigger, recorded_at, "
            "usage, total_tokens, weighted, cost_usd) VALUES "
            "('t1', 'old', 'prj_a', 1, 'stop', '2026-10-01T00:00:00+00:00', '{}', 9, 0, 0)"
        )
        raw.commit()
        assert ("column", "usage_session.account_slot") not in _shape(raw), "the fixture"
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
        session = store.session_usage("old")
        turns = store.turn_usages("old")
        store.add_feature_uses({("2026-10-26", "cli.fleet.spawn"): 2})
    once = _contents(db)
    open_store().close()

    raw = sqlite3.connect(str(db))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        shape = _shape(raw)
    finally:
        raw.close()
    assert (version, missing) == (SCHEMA_VERSION, [])
    assert shape == _built(SCHEMA_VERSION) and shape >= _USAGE_STATS_STEP
    assert session is not None and session.total_tokens == 9
    assert (session.account_slot, session.account_type, session.repo_id) == (None, None, None)
    assert [(t.total_tokens, t.source) for t in turns] == [(9, None)]
    assert _contents(db) == once, "a second open changed the store"


def test_a_store_another_line_stamped_v40_gains_the_usage_stats_objects() -> None:
    """Another rc line has stamped a v40 of its own before: a store claiming v40 without
    this step's tables and columns gets them by presence at its next open."""
    db = _at_version(39, stamp=40)
    raw = sqlite3.connect(str(db))
    try:
        assert ("table", "feature_use") not in _shape(raw), "the fixture"
    finally:
        raw.close()
    with store_session() as store:
        assert store.missing_schema() == []
        store.add_feature_uses({("2026-10-26", "tui.view.usage_stats"): 1})
    raw = sqlite3.connect(str(db))
    try:
        assert _shape(raw) >= _USAGE_STATS_STEP
    finally:
        raw.close()


# --- doctor's database row reads the schema, not only the file (review of #203) ---------------


def test_doctor_names_what_a_store_lacks_instead_of_calling_it_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured on #203 by the crew: a store #201 stamped 15, opened by a build that
    trusted the stamp, reached the current version with no ``claude_account`` and no
    ``fleet_agent.account_slot``. Every fleet read failed on the column, and doctor's
    database row said "context.db is readable". That build is this one with the
    presence pass taken out. The row fails and names each thing missing once (the
    table, not its indexes as well). Its remedy is not the corrupt-store move: the
    history in the file is intact."""
    from aisquare.services import diagnostics

    _at_version(14, after=_TWO_PROJECTS + PERSONAS_AT_15, stamp=15)
    monkeypatch.setattr(store_module, "_converge_by_presence", lambda connection, below: None)
    with store_session() as store, pytest.raises(sqlite3.OperationalError, match="account_slot"):
        store.fleet_agents("prj_used")

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert (
        "schema: column fleet_agent.account_slot, table claude_account, table project_setting;"
        in row.detail
    ), row.detail
    assert "claude_account_alias" not in row.detail, "an index of a missing table is noise"
    assert "persona" not in row.detail, "another line's columns are not this build's to report"
    assert row.fix is not None and f"cp {_db_path()}" in row.fix, row.fix
    assert "mv " not in row.fix, "the corrupt-store move would drop an intact history"
    assert "github.com/AISquare-Studio/aisquare-cli/issues" in row.fix, "report it where?"


def test_doctor_fails_on_what_no_step_of_this_build_puts_back() -> None:
    """The open restores only what a step from v15 on makes. A table or an index from
    before that, gone from a store another build or a hand edit changed, stays gone
    whatever the open does, and doctor's database row is where it shows. It names six
    and counts the rest, so the row stays one line an operator can read. It says what
    each kind the store lacks costs, and it still counts the notes, whose table is
    whole."""
    from aisquare.services import diagnostics

    indexes = (
        "entry_pool_project",
        "metric_open_session",
        "metric_project_started",
        "team_event_project_seq",
        "team_session_project",
        "team_task_project_status",
    )
    with store_session() as store:
        store.add(_entry())
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript("DROP TABLE prompt;" + "".join(f"DROP INDEX {i};" for i in indexes))
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
    row = diagnostics._check_database()

    assert missing[0] == "table prompt", "a missing table is named before any index"
    assert sorted(missing[1:]) == [f"index {i}" for i in indexes], missing
    assert row.status is CheckStatus.fail, row
    assert f"schema: {', '.join(missing[:6])} and 1 more;" in row.detail, row.detail
    assert row.detail.startswith("context.db opens (1 user entries) but"), row.detail
    assert row.detail.endswith(
        "; a command that reads a missing table or column fails with 'no such table' or "
        "'no such column'; a missing index that is not unique only slows the reads it served"
    ), row.detail


def test_a_store_without_its_full_text_index_lacks_one_table_not_five() -> None:
    """``entry_fts`` is an FTS5 table, and SQLite keeps what it indexes in shadow tables
    the module makes and drops with it (``entry_fts_data``, ``entry_fts_idx`` …). Read as
    this build's tables, a store without ``entry_fts`` lacked five, and they took five of
    the six names doctor's database row shows, crowding out what else the store lacked
    (review of the #203 side merges, F7). It lacks one table."""
    from aisquare.services import diagnostics

    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript("DROP TABLE entry_fts; DROP INDEX team_session_project;")
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
    row = diagnostics._check_database()

    assert missing == ["table entry_fts", "index team_session_project"], missing
    assert row.status is CheckStatus.fail, row
    assert f"schema: {', '.join(missing)};" in row.detail, row.detail


@pytest.mark.parametrize(
    "shadow", ["entry_fts_data", "entry_fts_idx", "entry_fts_docsize", "entry_fts_config"]
)
def test_doctor_fails_on_a_full_text_index_that_lost_a_shadow_table(shadow: str) -> None:
    """A shadow table gone with ``entry_fts`` is that table's absence, named once. Gone
    alone, the index is damaged: no note can be added, and SQLite's answer reads as a
    damaged store, or without ``_config`` ends in a traceback. Left out of this build's
    schema whatever else was there, a store without ``entry_fts_data`` lacked nothing and
    doctor's row said "context.db is readable"; without ``entry_fts_config`` the row's
    own column read raised and called the store unreadable, with the corrupt-store move
    (review of the #203 side merges, R2-F1). The row fails naming the shadow table, says
    what it costs, and still counts the notes, which are intact."""
    from aisquare.services import diagnostics

    with store_session() as store:
        store.add(_entry())
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(f"DROP TABLE {shadow};")
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
        with pytest.raises(sqlite3.DatabaseError) as raised:
            store.add(_entry())
    row = diagnostics._check_database()

    if shadow == "entry_fts_config":
        # Not damage to the CLI, so a traceback: the row's sentence says so, where it
        # said every shadow table's error reads as a damaged store (review of #203,
        # round 3, F9).
        assert "vtable constructor failed" in str(raised.value), raised.value
        assert not is_corrupt_error(raised.value), raised.value
    assert missing == [f"shadow table {shadow}"], missing
    assert row.status is CheckStatus.fail, row
    assert row.detail == (
        "context.db opens (1 user entries) but lacks part of this build's schema: shadow "
        f"table {shadow}; without a shadow table the notes' full-text index can be neither "
        "written nor searched: adding a note and `aisquare context search` fail, with an "
        "error that reads as a damaged store or, without entry_fts_config, a traceback "
        "ending 'vtable constructor failed', though the notes are intact"
    ), row.detail


@pytest.mark.parametrize(
    "script",
    ["DELETE FROM entry_fts_config;", "UPDATE entry_fts_config SET v = 99 WHERE k = 'version';"],
)
def test_doctor_names_a_full_text_index_fts5_cannot_open_instead_of_calling_the_store_unreadable(
    script: str,
) -> None:
    """With every shadow table there, FTS5 still cannot open ``entry_fts`` when its
    ``_config`` lost the version row or holds one the module does not read. The row's
    own column read opened it and raised "invalid fts5 file format", and the row called
    the store unreadable, with the corrupt-store move for its remedy, while every note
    still read (review of #203, round 3, F1). It names the index, says what that costs,
    counts the notes and keeps the file."""
    from aisquare.services import diagnostics

    with store_session() as store:
        store.add(_entry())
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(script)
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()
        with pytest.raises(sqlite3.OperationalError, match="invalid fts5 file format") as raised:
            store.add(_entry())
    row = diagnostics._check_database()

    assert not is_corrupt_error(raised.value), raised.value
    assert missing == ["unreadable table entry_fts"], missing
    assert row.status is CheckStatus.fail, row
    assert row.detail == (
        "context.db opens (1 user entries) but lacks part of this build's schema: "
        "unreadable table entry_fts; FTS5 cannot open an unreadable table, the notes' "
        "full-text index, so it can be neither written nor searched: adding a note and "
        "`aisquare context search` fail with a traceback ending in SQLite's error, though "
        "the notes are intact"
    ), row.detail
    assert row.fix is not None and "mv " not in row.fix, row.fix


def test_the_copy_to_keep_of_a_store_short_of_schema_is_quoted_for_a_home_with_a_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database row's "keep a copy (cp <db> <db>.bak)" embedded the store's path bare:
    under an AISQUARE_HOME with a space, pasted, cp got four arguments (review of #257).
    Quoted for this shell, it reads back as the two paths."""
    from aisquare.services import diagnostics, install_route

    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / "aisquare home"))
    with store_session() as store:
        store.add(_entry())
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript("DROP TABLE entry_fts_data;")
    finally:
        raw.close()
    row = diagnostics._check_database()

    database = _db_path()
    keep = install_route.command_line(["cp", str(database), f"{database}.bak"])
    assert " " in str(database) and f"keep a copy ({keep})" in str(row.fix), row.fix
    assert install_route.split_line(keep)[1:] == [str(database), f"{database}.bak"]


def test_the_acknowledgement_of_a_truncated_store_is_quoted_for_a_home_with_a_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database row's "Acknowledge it with: rm <marker>" embedded the marker's path
    bare: under an AISQUARE_HOME with a space, pasted, rm got two arguments and the marker
    stayed (review of #257). Quoted for this shell, it reads back as the one path."""
    from aisquare.core import paths
    from aisquare.services import diagnostics, install_route

    monkeypatch.setenv("AISQUARE_HOME", str(tmp_path / "aisquare home"))
    with store_session() as store:
        store.add(_entry())
    marker = paths.truncation_marker_path()
    marker.write_text("2026-10-10T00:00:00Z\n", encoding="utf-8")
    row = diagnostics._check_database()

    remove = install_route.command_line(["rm", str(marker)])
    assert " " in str(marker) and f"Acknowledge it with: {remove}" in str(row.fix), row.fix
    assert install_route.split_line(remove) == ["rm", str(marker)]


def test_a_lock_on_the_full_text_index_is_not_taken_for_an_unreadable_one() -> None:
    """Only a read the module refuses names ``entry_fts`` unreadable. "database is
    locked" says nothing about the file, so it raises as it did before the read was
    guarded, and is not reported as a gap in the schema."""
    from typing import cast

    class LockedIndex:
        """A connection whose read of ``entry_fts``'s columns meets a writer's lock."""

        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real

        def execute(self, sql: str) -> sqlite3.Cursor:
            if sql == "PRAGMA table_info(entry_fts)":
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql)

    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store_module._missing_from(
                cast(sqlite3.Connection, LockedIndex(raw)), store_module._ladder_schema()
            )
    finally:
        raw.close()


def test_a_table_named_after_the_full_text_index_is_not_taken_for_its_shadow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shadow tables are told apart by FTS5's suffixes (``_data``, ``_idx`` …). Told apart
    by the prefix ``entry_fts_`` alone, a table of this build's named that way was left
    out of its schema and never reported missing (review of the #203 side merges,
    R2-F3). A step that makes ``entry_fts_meta`` stands in for one."""
    ladder = store_module._MIGRATIONS
    monkeypatch.setattr(
        store_module,
        "_MIGRATIONS",
        (*ladder[:-1], ladder[-1] + "CREATE TABLE entry_fts_meta (note TEXT);"),
    )
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript("DROP TABLE entry_fts; DROP TABLE entry_fts_meta;")
    finally:
        raw.close()

    with store_session() as store:
        missing = store.missing_schema()

    assert missing == ["table entry_fts", "table entry_fts_meta"], missing


def test_doctor_says_what_a_trigger_it_only_counts_costs() -> None:
    """The row names six missing objects and counts the rest, but says what each kind
    the store lacks costs, a counted one too: what it costs is what the operator will
    meet. A trigger's sentence names the trigger, so a note trigger that falls into
    "and 1 more" still comes with its warning, and says which it is."""
    from aisquare.services import diagnostics

    tables = ("prompt", "team_event", "team_task", "team_meta", "metric")
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(
            "ALTER TABLE project DROP COLUMN linked_repos;"
            + "".join(f"DROP TABLE {table};" for table in tables)
            + "DROP TRIGGER entry_ai;"
        )
    finally:
        raw.close()

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert row.detail.startswith(
        "context.db opens (0 user entries) but lacks part of this build's schema: column "
        f"project.linked_repos, {', '.join(f'table {table}' for table in tables)} and 1 "
        "more; a command that reads a missing table or column fails"
    ), row.detail
    assert row.detail.endswith(f"; {diagnostics._TRIGGER_COSTS['entry_ai']}"), row.detail


@pytest.mark.parametrize(
    ("named", "script"),
    [
        ("table entry", "DROP TABLE entry;"),
        (
            "column entry.deleted_at",
            "DROP INDEX entry_pool_project; ALTER TABLE entry DROP COLUMN deleted_at;",
        ),
    ],
)
def test_doctor_names_a_store_without_its_notes_table_instead_of_calling_it_unreadable(
    named: str, script: str
) -> None:
    """The row counted the notes before it read the schema, and the count reads
    ``entry``. A store without that table, or a column of it, raised "no such table:
    entry" there and read as unreadable, with the corrupt-store move for its remedy: an
    intact history moved aside over a table the file merely lacks. The schema is read
    first, the count is left out when ``entry`` is what is missing, and the row names
    the gap like any other."""
    from aisquare.services import diagnostics

    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(script)
    finally:
        raw.close()

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert row.detail.startswith(
        f"context.db opens but lacks part of this build's schema: {named}"
    ), row.detail
    assert row.fix is not None and "mv " not in row.fix, row.fix


@pytest.mark.parametrize(
    ("script", "said", "not_said"),
    [
        (
            "DROP TRIGGER entry_ai;",
            "schema: trigger entry_ai; without trigger entry_ai a new note is not indexed: "
            "`aisquare context search` misses it, and editing or removing it, or purging its "
            "project, fails as 'database disk image is malformed', which the CLI calls a "
            "damaged store though the notes are intact",
            ("stays indexed", "duplicates", "slows"),
        ),
        (
            "DROP TRIGGER entry_ad;",
            "schema: trigger entry_ad; without trigger entry_ad a note purged with its "
            "project stays indexed, and `aisquare context search` can match a later note on "
            "the purged one's words",
            ("malformed", "old text", "duplicates", "slows"),
        ),
        (
            "DROP TRIGGER entry_au;",
            "schema: trigger entry_au; without trigger entry_au an edited note stays indexed "
            "under its old text, so `aisquare context search` matches what it said, not what "
            "it says",
            ("malformed", "purged", "duplicates", "slows"),
        ),
        (
            "DROP INDEX fleet_agent_live_label;",
            "schema: unique index fleet_agent_live_label; a missing unique index raises "
            "nothing and lets in the duplicates it refused",
            ("context search", "slows"),
        ),
        (
            "DROP INDEX prompt_project;",
            "schema: index prompt_project; a missing index that is not unique only slows the "
            "reads it served",
            ("context search", "duplicates"),
        ),
    ],
    ids=["entry_ai", "entry_ad", "entry_au", "unique index", "index"],
)
def test_doctor_says_what_a_missing_index_or_trigger_costs(
    script: str, said: str, not_said: tuple[str, ...]
) -> None:
    """A missing table or column fails its readers with "no such table" or "no such
    column"; nothing raises on a missing index or trigger, so the row says what each
    costs, and only of the kinds the store lacks. It once said a missing index or
    trigger "fails nothing", and then gave every trigger the cost of `entry_ai`: a note
    it did not index hands FTS5 a 'delete' for text it never held when that note is
    edited, removed or purged, and SQLite answers "database disk image is malformed",
    which the CLI calls a damaged store and answers with the corrupt-store move. Without
    `entry_ad` or `entry_au` nothing fails, and search goes stale in a way of its own,
    so each trigger gets its own sentence (the tests after this one measure each)."""
    from aisquare.services import diagnostics

    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.executescript(script)
    finally:
        raw.close()

    row = diagnostics._check_database()

    assert row.status is CheckStatus.fail, row
    assert row.detail.endswith(said), row.detail
    assert "fails nothing" not in row.detail and "no such" not in row.detail, row.detail
    assert not [cost for cost in not_said if cost in row.detail], row.detail


def test_doctor_reports_a_schema_gap_where_the_package_says_issues_go() -> None:
    """The database row's remedy says where to report a gap it cannot close. The address
    is a copy of pyproject's ``[project.urls] Issues``, so a tracker that moves there
    must move here too."""
    import tomllib

    from aisquare.services import diagnostics

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    urls = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["urls"]

    assert urls["Issues"] == diagnostics._ISSUES_URL


def _drop_trigger(name: str) -> None:
    """A store whose ``name`` trigger a hand edit or another build dropped."""
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.execute(f"DROP TRIGGER {name}")
        raw.commit()
    finally:
        raw.close()


def test_without_entry_ai_a_new_note_is_unsearchable_and_changing_it_is_malformed() -> None:
    """What doctor's row says of a missing `entry_ai`, measured: the new note never
    reaches the search index, and each later change hands FTS5 a 'delete' for text it
    never held, which SQLite answers as a corrupt file though nothing in it is."""
    _drop_trigger("entry_ai")
    with store_session() as store:
        store.ensure_project(PROJECT)
        note = store.add(_entry("alpha beta", pool="project", project_id=PROJECT.id))

        assert store.search("alpha", project_id=PROJECT.id) == []
        for change in (
            lambda: store.update(note.id, text="gamma delta"),
            lambda: store.delete(note.id),
            lambda: store.purge_project(PROJECT.id),
        ):
            with pytest.raises(sqlite3.DatabaseError, match="database disk image is malformed"):
                change()


def test_without_entry_ad_a_later_note_matches_a_purged_notes_words() -> None:
    """What doctor's row says of a missing `entry_ad`, measured: a purge deletes the
    project's notes for real, their text stays in the search index, and a note that
    takes the freed rowid is found by words it does not hold. Nothing fails."""
    _drop_trigger("entry_ad")
    with store_session() as store:
        store.ensure_project(PROJECT)
        store.add(_entry("alpha beta", pool="project", project_id=PROJECT.id))
        store.purge_project(PROJECT.id)
        later = store.add(_entry("gamma delta"))

        assert [entry.id for entry in store.search("alpha")] == [later.id]


def test_without_entry_au_an_edited_note_is_found_by_its_old_text() -> None:
    """What doctor's row says of a missing `entry_au`, measured: an edit leaves the
    search index on the text the note had. Nothing fails, a removal included."""
    _drop_trigger("entry_au")
    with store_session() as store:
        note = store.add(_entry("alpha beta"))
        store.update(note.id, text="gamma delta")

        assert [entry.text for entry in store.search("alpha")] == ["gamma delta"]
        assert store.search("gamma") == []
        store.delete(note.id)


def test_doctor_has_a_cost_for_every_trigger_of_this_build() -> None:
    """Doctor's database row says what a missing trigger costs from a sentence per
    trigger, in the ladder's order. A trigger the ladder makes without one would be
    named in the row with no word of what its absence does, so a new trigger fails
    here until it has its sentence."""
    from aisquare.services import diagnostics

    connection = sqlite3.connect(":memory:")
    try:
        store_module._migrate(connection)
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()

    assert [name for (name,) in triggers] == list(diagnostics._TRIGGER_COSTS)


def test_each_step_from_v15_on_declares_what_it_builds_and_builds_nothing_twice() -> None:
    """``_PRODUCTS`` is what the presence pass looks for: a step whose entry drifts from
    its script is a table the pass never restores, or one it restores on every open.
    Checked by building: each step's new tables, indexes and columns are exactly its
    entry, and applying the step again, as the pass does, changes nothing."""
    from aisquare.core.store import _PRODUCTS, _Products, _run_step

    assert all(14 <= step < SCHEMA_VERSION for step in _PRODUCTS)
    conn = sqlite3.connect(":memory:")
    try:
        for step in range(SCHEMA_VERSION):
            before = _shape(conn)
            _run_step(conn, step)
            after = _shape(conn)
            if step < 14:
                continue  # v1-v14 run once, below every stamp another line made
            new = after - before
            objects = {name for kind, name in new if kind != "column"}
            columns = {
                column
                for kind, column in new
                if kind == "column" and column.split(".")[0] not in objects
            }
            declared = _PRODUCTS.get(step, _Products())
            assert objects == set(declared.objects), f"v{step + 1}"
            assert columns == {f"{t}.{c}" for t, c, _ in declared.columns}, f"v{step + 1}"
            _run_step(conn, step)
            assert _shape(conn) == after, f"v{step + 1} applied twice"
    finally:
        conn.close()


def test_the_ladder_from_v15_run_again_over_a_current_store_changes_nothing() -> None:
    """Every step from v15 on is idempotent, so a store that meets them twice (stamped
    back to 14 here) opens with the same schema and the same rows. That includes the
    rows the v17 backfill reads: a captured project with a context entry stays
    captured, because the backfill runs only when it adds the column."""
    with store_session() as store:
        store.onboard_project(PROJECT)
        store.ensure_project(ProjectInfo(id="prj_captured", root=Path("/w/captured")))
        store.add(_entry("a fact", pool="project", project_id="prj_captured"))
        store.upsert_claude_account(1, Path("/h/.claude"))
        store.set_project_setting(PROJECT.id, "claude_account", "1")
        assert [p.id for p in store.list_projects()] == [PROJECT.id]

    def dump() -> tuple[list[tuple[object, ...]], dict[str, list[tuple[object, ...]]]]:
        raw = sqlite3.connect(str(_db_path()))
        try:
            schema = raw.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
            rows = {
                name: sorted(raw.execute(f"SELECT * FROM {name}").fetchall(), key=repr)
                for kind, name, _, _ in schema
                if kind == "table"
            }
            return sorted(schema, key=repr), rows
        finally:
            raw.close()

    before = dump()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.execute("PRAGMA user_version = 14")
        raw.commit()
    finally:
        raw.close()

    with store_session() as store:
        assert [p.id for p in store.list_projects()] == [PROJECT.id]
    raw = sqlite3.connect(str(_db_path()))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()
    assert dump() == before


# --- the Claude account registry and per-project settings (v15, #145) ----------------------


def test_claude_account_registry_keeps_unique_aliases_and_a_dense_order(
    store: ContextStore,
) -> None:
    """The alias invariant is the schema's: a reused alias is refused by an index, not by a
    caller remembering to check. The machine default is rank 1 of the order (#8), so there
    is no flag left to keep single."""
    first = store.upsert_claude_account(1, Path("/h/.claude"))
    second = store.upsert_claude_account(2, Path("/h/.aisquare/claude-accounts/2"))
    assert (first.position, second.position) == (1, 2)  # queued at the end, in order
    assert not first.is_default and not second.is_default and first.alias is None
    assert not hasattr(store, "set_claude_account_default"), (
        "the flag is retired (#406): read once, then promoted"
    )

    store.set_claude_account_alias(2, "work")
    with pytest.raises(sqlite3.IntegrityError):
        store.set_claude_account_alias(1, "work")
    assert [r.alias for r in store.claude_accounts()] == [None, "work"]
    store.set_claude_account_alias(2, None)
    store.set_claude_account_alias(1, "work")  # free again once released
    assert [r.alias for r in store.claude_accounts()] == ["work", None]

    store.set_claude_account_disabled(2, True)
    assert [r.disabled for r in store.claude_accounts()] == [False, True]

    # An upsert of a known slot refreshes the directory and touches nothing else.
    refreshed = store.upsert_claude_account(1, Path("/elsewhere/.claude"))
    assert refreshed.config_dir == Path("/elsewhere/.claude")
    assert refreshed.alias == "work" and refreshed.position == 1

    store.upsert_claude_account(3, Path("/h/.aisquare/claude-accounts/3"))
    store.order_claude_accounts([3, 9])  # 9 does not exist and is ignored
    assert [(r.slot, r.position) for r in store.claude_accounts()] == [(3, 1), (1, 2), (2, 3)]
    store.order_claude_accounts([2, 2, 3])  # a repeated reference left position 1 unused
    assert [(r.slot, r.position) for r in store.claude_accounts()] == [(2, 1), (3, 2), (1, 3)]
    store.order_claude_accounts([3, 1, 2])  # back to the order the rest of the test reads
    assert store.delete_claude_account(1) is True
    assert store.delete_claude_account(1) is False
    assert [(r.slot, r.position) for r in store.claude_accounts()] == [(3, 1), (2, 2)]  # dense


_FLAGGED_AT_RANK_3 = """
INSERT INTO claude_account (slot, config_dir, position, is_default, created_at)
VALUES (1, '/h/.claude', 1, 0, '2026-09-01T00:00:00+00:00'),
       (2, '/h/.aisquare/claude-accounts/2', 2, 0, '2026-09-01T00:00:00+00:00'),
       (3, '/h/.aisquare/claude-accounts/3', 3, 1, '2026-09-01T00:00:00+00:00'),
       (4, '/h/.aisquare/claude-accounts/4', 4, 0, '2026-09-01T00:00:00+00:00');
"""


@pytest.mark.parametrize("stamp", [None, 40], ids=["current", "stamped-past-it"])
def test_the_retired_default_flag_moves_its_slot_to_rank_1_once_whatever_the_version(
    stamp: int | None,
) -> None:
    """Rank 1 is the machine default (#8 on the board). A slot an older build flagged
    ``is_default`` goes to rank 1, the others behind it in their order, and the flag is
    cleared, so no launch changes account. Keyed on the flag, not on a schema step: a
    store another line stamped past every version this build knows (a real copy at v40
    kept its default at rank 2 under a version-keyed step) is promoted all the same."""
    _at_version(SCHEMA_VERSION, after=_FLAGGED_AT_RANK_3, stamp=stamp)
    store = open_store()
    try:
        assert store.promote_retired_claude_account_default() == 3
        assert [(r.slot, r.position) for r in store.claude_accounts()] == [
            (3, 1),
            (1, 2),
            (2, 3),
            (4, 4),
        ]
        assert not any(r.is_default for r in store.claude_accounts())  # cleared
        # Idempotent: nothing flagged is left, so a later reorder stands.
        store.order_claude_accounts([4])
        assert store.promote_retired_claude_account_default() is None
        assert [r.slot for r in store.claude_accounts()] == [4, 3, 1, 2]
    finally:
        store.close()


def test_a_promote_that_lost_the_race_leaves_a_concurrent_reorder_standing() -> None:
    """The reviewer's interleave (review of #406, the lost update): B promotes and then
    moves slot 3 to the top between A's flag check and A's write. A read the order before
    taking the write lock, and wrote its stale order over B's move. Now A clears the flag
    under the lock first, finds it gone, and writes nothing."""
    _at_version(
        SCHEMA_VERSION,
        after="""
        INSERT INTO claude_account (slot, config_dir, position, is_default, created_at)
        VALUES (4, '/h/.aisquare/claude-accounts/4', 1, 0, '2026-09-01T00:00:00+00:00'),
               (2, '/h/.aisquare/claude-accounts/2', 2, 1, '2026-09-01T00:00:00+00:00'),
               (3, '/h/.aisquare/claude-accounts/3', 3, 0, '2026-09-01T00:00:00+00:00'),
               (1, '/h/.claude', 4, 0, '2026-09-01T00:00:00+00:00');
        """,
    )
    a, b = open_store(), open_store()

    class Interleaved:
        """A's connection: B runs right after A's flag check, as in the repro."""

        def __init__(self, real: sqlite3.Connection) -> None:
            self.real = real

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            cursor = self.real.execute(sql, *args)
            if "WHERE is_default = 1 LIMIT 1" in sql:
                assert b.promote_retired_claude_account_default() == 2
                b.order_claude_accounts([3])  # B moved 3 to the top
                assert [(r.slot, r.position) for r in b.claude_accounts()] == [
                    (3, 1),
                    (2, 2),
                    (4, 3),
                    (1, 4),
                ]
            return cursor

        def __getattr__(self, name: str) -> object:
            return getattr(self.real, name)

    try:
        a._conn = Interleaved(a._conn)  # type: ignore[attr-defined]
        assert a.promote_retired_claude_account_default() is None  # lost the race: no write
        a._conn = a._conn.real  # type: ignore[attr-defined]
        assert [(r.slot, r.position) for r in a.claude_accounts()] == [
            (3, 1),
            (2, 2),
            (4, 3),
            (1, 4),
        ], "B's move of slot 3 stands"
    finally:
        a.close()
        b.close()


def test_with_no_flag_set_the_order_is_left_as_it_was(store: ContextStore) -> None:
    store.upsert_claude_account(2, Path("/h/.aisquare/claude-accounts/2"))
    store.upsert_claude_account(1, Path("/h/.claude"))
    assert store.promote_retired_claude_account_default() is None
    assert [(r.slot, r.position) for r in store.claude_accounts()] == [(2, 1), (1, 2)]


def test_project_settings_round_trip_per_project_and_clear(store: ContextStore) -> None:
    other = ProjectInfo(id="prj_other", root=Path("/tmp/other"), linked_repos=[])
    store.ensure_project(other)

    assert store.project_setting(PROJECT.id, "claude_account") is None
    store.set_project_setting(PROJECT.id, "claude_account", "2")
    store.set_project_setting(other.id, "claude_account", "3")
    store.set_project_setting(PROJECT.id, "claude_account", "4")  # an update, not a second row
    assert store.project_setting(PROJECT.id, "claude_account") == "4"
    assert store.project_setting(other.id, "claude_account") == "3"
    assert store.project_settings("claude_account") == {PROJECT.id: "4", other.id: "3"}
    assert store.project_setting(PROJECT.id, "something_else") is None

    assert store.clear_project_setting(PROJECT.id, "claude_account") is True
    assert store.clear_project_setting(PROJECT.id, "claude_account") is False
    assert store.project_settings("claude_account") == {other.id: "3"}
    with pytest.raises(sqlite3.IntegrityError):  # a foreign key: no setting for a ghost project
        store.set_project_setting("prj_ghost", "claude_account", "1")


# --- captured versus onboarded (#139) ------------------------------------------------------


def test_ensure_project_captures_and_only_onboard_project_shows() -> None:
    from aisquare.models import ProjectInfo

    store = open_store()
    try:
        quiet = ProjectInfo(id="prj_quiet", root=Path("/w/quiet"))
        store.ensure_project(quiet)  # what a hook does
        assert store.list_projects() == []  # captured, not shown
        [captured] = store.list_projects(all=True)
        assert captured.id == "prj_quiet" and captured.onboarded_at is None
        assert [p.id for p in store.captured_projects()] == ["prj_quiet"]
        assert store.get_project("prj_quiet") is not None  # reachable by id, as before
        store.ensure_project(quiet)  # again: still nothing changes
        assert store.list_projects() == []

        shown = store.onboard_project(quiet)  # what init / onboard / link / + / team on do
        assert shown.onboarded_at is not None
        assert [p.id for p in store.list_projects()] == ["prj_quiet"]
        assert store.captured_projects() == []
        first = shown.onboarded_at
        assert store.onboard_project(quiet).onboarded_at == first  # set once, kept

        # forget clears the mark and hides; a hook's capture afterwards brings the row
        # back CAPTURED — reachable, with its history, but not listed…
        store.forget_project("prj_quiet")
        assert store.list_projects(all=True) == [] and store.get_project("prj_quiet") is None
        store.ensure_project(quiet)
        assert store.list_projects() == [], "forget sticks against a capture"
        [back] = store.list_projects(all=True)
        assert back.id == "prj_quiet" and back.onboarded_at is None
        assert store.get_project("prj_quiet") is not None, "not a tombstone prompts vanish into"
        # …and a deliberate add lists it again, with a fresh mark.
        again = store.onboard_project(quiet)
        assert again.onboarded_at is not None and again.onboarded_at >= first
        assert [p.id for p in store.list_projects()] == ["prj_quiet"]
    finally:
        store.close()


def test_v23_repairs_the_tombstones_older_cuts_left_holding_a_mark_or_a_place() -> None:
    """The first cut of the v17 backfill (c716094) had no ``forgotten_at`` guard, so a
    store migrated by it holds forgotten rows stamped onboarded, and a forget written
    before #171's first round left the group, the position and the pin on its tombstone.
    Revived as they were, the next prompt there put the project back on the list, pinned
    and grouped (the bug #139 is about), and an onboarding kept the stale mark. Both were
    answered by a CASE on every capture; v23 clears them once, as a forget does now (review
    of #168 at the fold). A live row keeps its mark and its place."""
    legacy = "'2026-09-01T00:00:00+00:00'"
    _at_version(
        22,
        after=f"""
        INSERT INTO project_group (id, name, position, created_at)
            VALUES ('grp_1', 'tools', 0, {legacy});
        INSERT INTO project (id, root, name, linked_repos, created_at, onboarded_at,
                             forgotten_at, group_id, position, pinned_at)
        VALUES
            ('prj_old', '/w/old', 'old', '[]', {legacy}, {legacy},
             '2026-09-02T00:00:00+00:00', 'grp_1', 0, {legacy}),
            ('prj_gone', '/w/gone', 'gone', '[]', {legacy}, {legacy},
             '2026-09-02T00:00:00+00:00', NULL, NULL, {legacy}),
            ('prj_live', '/w/live', 'live', '[]', {legacy}, {legacy}, NULL, 'grp_1', 1, {legacy});
    """,
    )

    store = open_store()
    try:
        raw = sqlite3.connect(str(_db_path()))
        try:
            tombstones = raw.execute(
                "SELECT onboarded_at, group_id, position, pinned_at FROM project "
                "WHERE forgotten_at IS NOT NULL"
            ).fetchall()
        finally:
            raw.close()
        assert tombstones == [(None, None, None, None)] * 2, "repaired as a forget clears them"

        store.ensure_project(ProjectInfo(id="prj_old", root=Path("/w/old")))  # a prompt there
        store.ensure_project(ProjectInfo(id="prj_live", root=Path("/w/live")))  # one here
        onboarded = store.onboard_project(ProjectInfo(id="prj_gone", root=Path("/w/gone")))

        assert {p.id for p in store.list_projects()} == {"prj_live", "prj_gone"}
        revived = store.get_project("prj_old")
        assert revived is not None, "captured, not a tombstone"
        assert (revived.onboarded_at, revived.group_id, revived.position) == (None, None, None)
        assert revived.pinned_at is None
        assert onboarded.onboarded_at is not None
        assert onboarded.onboarded_at.isoformat() > "2026-09-02", "marked now, not the old mark"
        kept = store.get_project("prj_live")
        assert kept is not None and kept.onboarded_at is not None
        assert (kept.group_id, kept.position) == ("grp_1", 1) and kept.pinned_at is not None
    finally:
        store.close()


def test_v27_gives_a_project_from_before_it_no_nickname_and_room_for_one() -> None:
    _at_version(
        26,
        after="""
        INSERT INTO project (id, root, name, linked_repos, created_at, codename)
        VALUES ('prj_old', '/w/old', 'old', '[]', '2026-09-01T00:00:00+00:00', 'amber-otter');
    """,
    )

    with store_session() as store:
        before = store.get_project("prj_old")
        assert before is not None
        assert (before.nickname, before.codename) == (None, "amber-otter")
        assert store.set_project_nickname("prj_old", "the old one").nickname == "the old one"
    raw = sqlite3.connect(str(_db_path()))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()


def test_v24_marks_the_keys_bound_to_their_destinations_deployment() -> None:
    """A binding named its deployment by target name alone, and the destination's ``stg``
    and the machine's ``stg`` can be two deployments (review of #203). v24 records the
    destination's API on a key bound to the deployment its destination names, as ``use``
    and ``key set`` bind it; a key bound to another target stays one of the machine's."""
    legacy = "'2026-09-01T00:00:00+00:00'"
    _at_version(
        23,
        after=f"""
        INSERT INTO project (id, root, name, linked_repos, created_at) VALUES
            ('prj_dest', '/w/dest', 'dest', '[]', {legacy}),
            ('prj_elsewhere', '/w/elsewhere', 'elsewhere', '[]', {legacy}),
            ('prj_machine', '/w/machine', 'machine', '[]', {legacy});
        INSERT INTO project_destination (project_id, api_url, environment, workspace_id,
                                         workspace_name, set_at)
        VALUES
            ('prj_dest', 'https://stg-api.aisquare.studio', 'stg', 42, 'acme', {legacy}),
            ('prj_elsewhere', 'https://api.aisquare.studio', 'prod', 42, 'acme', {legacy});
        INSERT INTO project_explainability (project_id, target, key_path, set_at) VALUES
            ('prj_dest', 'stg', '/k/dest', {legacy}),
            ('prj_elsewhere', 'stg', '/k/elsewhere', {legacy}),
            ('prj_machine', 'stg', '/k/machine', {legacy});
    """,
    )

    with store_session() as store:
        marked = {row.project_id: row.api_url for row in store.project_explainability_all()}
    assert marked == {
        "prj_dest": "https://stg-api.aisquare.studio",
        "prj_elsewhere": None,
        "prj_machine": None,
    }


def test_the_v17_migration_adopts_the_rows_already_used_on_purpose(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows with context entries, a codename, linked repos, board activity, a fleet
    agent or a snapshot on disk become onboarded; captured-only rows stay hidden."""
    rows = [
        ("prj_entries", "/w/entries"),
        ("prj_named", "/w/named"),
        ("prj_linked", "/w/linked"),
        ("prj_board", "/w/board"),
        ("prj_agent", "/w/agent"),
        ("prj_snap", "/w/snap"),
        ("prj_quiet", "/w/quiet"),
        ("prj_gone", "/w/gone"),
        ("prj_gone_used", "/w/gone-used"),
    ]

    def insert(pid: str, root: str) -> str:
        repos = '["git@x:y.git"]' if pid == "prj_linked" else "[]"
        name = root.rsplit("/", 1)[1]
        return (
            "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES "
            f"('{pid}', '{root}', '{name}', '{repos}', '2026-09-01T00:00:00+00:00');\n"
        )

    inserts = "".join(insert(pid, root) for pid, root in rows)
    after = (
        inserts
        + """
        UPDATE project SET codename = 'amber-otter' WHERE id = 'prj_named';
        UPDATE project SET forgotten_at = '2026-09-02T00:00:00+00:00'
            WHERE id IN ('prj_gone', 'prj_gone_used');
        INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at)
            VALUES ('ent_1', 'project', 'prj_entries', 'a fact', '[]', 'cli',
                    '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
        INSERT INTO entry (id, pool, project_id, text, tags, source, created_at, updated_at)
            VALUES ('ent_2', 'project', 'prj_gone_used', 'kept by forget', '[]', 'cli',
                    '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
        INSERT INTO team_event (id, project_id, session_id, kind, text, created_at)
            VALUES ('evt_1', 'prj_board', NULL, 'activate', 'on', '2026-09-01T00:00:00+00:00');
        INSERT INTO fleet_agent (id, project_id, label, role, tmux_socket, pane_id, cwd,
                                 worktree, created_at)
            VALUES ('agt_1', 'prj_agent', 'coder-1', 'coder', 'asq', '%1', '/w/agent', 0,
                    '2026-09-01T00:00:00+00:00');
        INSERT INTO prompt (id, project_id, text, source, created_at)
            VALUES ('prm_1', 'prj_quiet', 'hello', 'claude-code', '2026-09-01T00:00:00+00:00');
    """
    )
    _at_version(16, after=after)
    for with_snapshot in ("prj_snap", "prj_gone_used"):  # forget keeps the snapshot
        _old_pack(with_snapshot)

    store = open_store()
    try:
        shown = {p.id for p in store.list_projects()}
        everything = {p.id for p in store.list_projects(all=True)}
        # The next prompt in a directory forgotten before v17 captures it; it must
        # not be re-listed by the entries and snapshot its forget left behind.
        store.ensure_project(ProjectInfo(id="prj_gone_used", root=Path("/w/gone-used")))
        revived = store.get_project("prj_gone_used")
        listed_after_prompt = {p.id for p in store.list_projects()}
    finally:
        store.close()
    assert shown == {"prj_entries", "prj_named", "prj_linked", "prj_board", "prj_agent", "prj_snap"}
    assert everything == shown | {"prj_quiet"}, "the prompt-only row is captured, not shown"
    assert "prj_gone" not in everything, "forgotten stays forgotten"
    assert "prj_gone_used" not in everything
    assert revived is not None and revived.onboarded_at is None, "a forgotten row is not adopted"
    assert "prj_gone_used" not in listed_after_prompt


def test_an_unreadable_snapshot_is_no_evidence_and_the_store_still_opens(
    isolated_home: Path,
) -> None:
    """The v17 backfill looks for each captured row's snapshot on disk, and ``Path.exists``
    raises for a directory this user cannot search. The ``PermissionError`` escaped the
    migration, which rolls back only on ``sqlite3.Error``, so the open failed with a raw
    traceback and the write transaction left open, on every command until the directory
    was readable again (review of #203). It is no evidence: the row stays captured."""
    after = "".join(
        "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES "
        f"('{pid}', '/w/{pid}', '{pid}', '[]', '2026-09-01T00:00:00+00:00');\n"
        for pid in ("prj_locked", "prj_snap")
    )
    _at_version(16, after=after)
    for pid in ("prj_locked", "prj_snap"):
        _old_pack(pid)
    locked = paths.project_data_dir("prj_locked") / "snapshot"
    locked.chmod(0o000)
    try:
        try:
            (locked / "snapshot.json").exists()
        except PermissionError:
            pass
        else:
            pytest.skip("this user can search a directory with no permissions (root, or no modes)")
        store = open_store()
        try:
            shown = {p.id for p in store.list_projects()}
            everything = {p.id for p in store.list_projects(all=True)}
        finally:
            store.close()
    finally:
        locked.chmod(0o700)
    assert shown == {"prj_snap"}, "a readable snapshot still adopts its row"
    assert everything == {"prj_snap", "prj_locked"}, "the unreadable one stays captured"


def _old_pack(project_id: str) -> Path:
    """A codebase pack as a build up to 0.8.2 left one for ``project_id``: its files, and the
    ``snapshot.json`` the v17 backfill reads as evidence."""
    pack = paths.project_data_dir(project_id) / "snapshot"
    pack.mkdir(parents=True, exist_ok=True)
    for name in ("pack.xml", "skeleton.xml", "index.json"):
        (pack / name).write_text("", encoding="utf-8")
    (pack / "snapshot.json").write_text("{}", encoding="utf-8")
    return pack


def test_a_pack_is_the_evidence_first_and_deleted_after(isolated_home: Path) -> None:
    """A store from 0.6 or older, upgraded straight to this build: the only mark of a
    project the owner added is its pack. The v17 backfill must read it before the open
    deletes it, or the project vanishes from ``project list`` and the fleet. Then the
    pack goes, and the ``brain/`` beside it stays."""
    after = (
        "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES "
        "('prj_packed', '/w/packed', 'packed', '[]', '2026-09-01T00:00:00+00:00');\n"
    )
    _at_version(16, after=after)
    pack = _old_pack("prj_packed")
    brain = paths.project_data_dir("prj_packed") / "brain"
    brain.mkdir()
    (brain / "page.md").write_text("kept", encoding="utf-8")

    with store_session() as store:
        shown = {p.id for p in store.list_projects()}
    assert shown == {"prj_packed"}, "the pack adopted its row before it was deleted"
    assert not pack.exists()
    assert (brain / "page.md").read_text(encoding="utf-8") == "kept"


def test_the_homes_old_pack_does_not_list_the_home(isolated_home: Path) -> None:
    """Builds before D5 packed the aisquare home as well. That pack is not the owner adding
    it, as main's ``snapshot.exists()`` held, so the v17 backfill passes over the home's
    row and the pack still goes (review of #399, m10)."""
    from aisquare.core.workspace import project_id_for

    home = paths.aisquare_home().resolve()
    home_id = project_id_for(home)
    after = (
        "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES "
        f"('{home_id}', '{home}', '.aisquare', '[]', '2026-09-01T00:00:00+00:00');\n"
    )
    _at_version(16, after=after)
    pack = _old_pack(home_id)

    with store_session() as store:
        shown = {p.id for p in store.list_projects()}
    raw = sqlite3.connect(str(_db_path()))
    try:
        onboarded = raw.execute(
            "SELECT onboarded_at FROM project WHERE id = ?", (home_id,)
        ).fetchone()
    finally:
        raw.close()
    assert onboarded == (None,), "the home's pack adopted nothing"
    assert home_id not in shown
    assert not pack.exists()


def test_the_backfill_resolves_no_root_for_a_row_without_a_pack(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking whether a row is the home resolves its root, which can hang on a dead mount,
    and most captured rows have no pack. So the stat comes first, and only a row with a
    pack is asked (review of #399, n11)."""
    after = (
        "INSERT INTO project (id, root, name, linked_repos, created_at) VALUES "
        "('prj_bare', '/gone/mount/bare', 'bare', '[]', '2026-09-01T00:00:00+00:00'),"
        " ('prj_packed', '/w/packed', 'packed', '[]', '2026-09-01T00:00:00+00:00');\n"
    )
    _at_version(16, after=after)
    _old_pack("prj_packed")
    asked: list[str] = []
    is_the_home = store_module.is_the_home

    def spy(root: Path) -> bool:
        asked.append(root.as_posix())
        return is_the_home(root)

    monkeypatch.setattr(store_module, "is_the_home", spy)
    with store_session():
        pass

    assert "/w/packed" in asked, "control: a row with a pack is asked"
    assert "/gone/mount/bare" not in asked


def test_old_packs_go_once_and_nothing_outside_projects_is_followed(
    isolated_home: Path, tmp_path: Path
) -> None:
    """Every project's ``snapshot/`` goes on the first open, the aisquare home's own among
    them (D5: its pack copied the home's login data), and only that: not a file of that
    name, not a symlink's target, not a pack reached through a project directory that is a
    symlink out of the home. A pack written after that open stays: the cleanup ran, and
    records it."""
    from aisquare.core.workspace import project_id_for

    first = _old_pack("prj_a")
    second = _old_pack("prj_b")
    homes = _old_pack(project_id_for(paths.aisquare_home().resolve()))
    (paths.project_data_dir("prj_b") / "explainability-key").write_text("k", encoding="utf-8")
    (paths.project_data_dir("prj_file")).mkdir(parents=True)
    (paths.project_data_dir("prj_file") / "snapshot").write_text("not ours", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    (outside / "snapshot").mkdir(parents=True)
    (outside / "snapshot" / "keep.txt").write_text("keep", encoding="utf-8")
    try:
        paths.project_data_dir("prj_link").mkdir(parents=True)
        (paths.project_data_dir("prj_link") / "snapshot").symlink_to(outside / "snapshot")
        paths.project_data_dir("prj_out").symlink_to(outside)
    except OSError:
        pytest.skip("this filesystem cannot make symlinks")

    with store_session():
        pass
    assert not first.exists() and not second.exists() and not homes.exists()
    assert (paths.project_data_dir("prj_b") / "explainability-key").exists()
    assert (paths.project_data_dir("prj_file") / "snapshot").read_text(encoding="utf-8")
    assert (outside / "snapshot" / "keep.txt").exists(), "never followed out of the home"
    assert (paths.project_data_dir("prj_link") / "snapshot").is_symlink()

    again = _old_pack("prj_a")
    with store_session():
        pass
    assert again.exists(), "it ran once"


def test_a_home_with_no_projects_dir_has_nothing_to_clean_and_says_so(
    isolated_home: Path,
) -> None:
    """A new home has no ``projects/`` and so no pack: done on the first open, so later
    opens do not look again."""
    with store_session():
        pass
    assert not (paths.aisquare_home() / "projects").exists(), "the premise: nothing there"
    assert (paths.cache_dir() / "old-packs-removed").exists()


@pytest.mark.parametrize("damage", ["looping", "unreadable", "resolve-raises"])
def test_a_projects_dir_the_cleanup_cannot_read_never_breaks_the_open(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """The cleanup is best effort, so ``projects/`` in any state it cannot read leaves the
    open working: a symlink to itself (Python 3.11 and 3.12 raise ``RuntimeError`` from
    ``resolve()``, not ``OSError``; review of #399), a directory this user cannot list,
    and, on every Python, a ``resolve()`` that raises ``RuntimeError``. Nothing is
    recorded as done, so a later open tries again."""
    projects = paths.aisquare_home() / "projects"
    if damage == "looping":
        projects.parent.mkdir(parents=True, exist_ok=True)
        try:
            projects.symlink_to(projects)
        except OSError:
            pytest.skip("this filesystem cannot make symlinks")
    elif damage == "unreadable":
        if sys.platform == "win32" or os.geteuid() == 0:  # mypy narrows on sys.platform
            pytest.skip("needs POSIX permissions that bind this user")
        _old_pack("prj_a")
        projects.chmod(0)
    else:
        _old_pack("prj_a")
        resolve = Path.resolve

        def loop(self: Path, strict: bool = False) -> Path:
            if self.name == "projects":
                raise RuntimeError(f"Symlink loop from {str(self)!r}")
            return resolve(self, strict)

        monkeypatch.setattr(Path, "resolve", loop)
    try:
        with store_session() as store:
            store.list_projects()
    finally:
        if damage == "unreadable":
            projects.chmod(0o700)
    assert not (paths.cache_dir() / "old-packs-removed").exists()


# --- the reads a poller makes every few seconds (remote's needs scan) -------------------------


def test_the_rows_that_ended_since_are_chosen_by_the_query(store: ContextStore) -> None:
    """Every spawn, restart and switch leaves a row that is never deleted, so a reader that
    wants the last day's endings must not build all of them to keep a few."""
    from datetime import timedelta, timezone

    from aisquare.models import FleetAgent

    now = datetime.now(tz=UTC)
    for n, ended in ((1, now - timedelta(days=2)), (2, now - timedelta(hours=1)), (3, None)):
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{n}", project_id=PROJECT.id, label=f"coder-{n}", role="coder",
                pane_id=f"%{n}", cwd=Path("/w"), created_at=now - timedelta(days=3),
                ended_at=ended, exit_status=None if ended is None else 1,
            )
        )  # fmt: skip
    since = now - timedelta(days=1)
    assert [row.id for row in store.fleet_agents_ended_since(PROJECT.id, since)] == ["agt_2"]
    toronto = since.astimezone(timezone(timedelta(hours=-4)))
    assert [row.id for row in store.fleet_agents_ended_since(PROJECT.id, toronto)] == ["agt_2"]
    assert store.fleet_agents_ended_since("prj_other", since) == []


def test_the_sessions_seen_since_and_the_ones_named_are_chosen_by_the_query(
    store: ContextStore,
) -> None:
    """Every Claude Code start leaves a session that is never deleted: the reader names the
    old ones it needs (the authors of open board questions) and gets the recent ones."""
    from datetime import timedelta

    from aisquare.models import TeamSession

    now = datetime.now(tz=UTC)
    for n, seen in ((1, now - timedelta(days=2)), (2, now - timedelta(minutes=5)), (3, now)):
        store.upsert_session(
            TeamSession(
                id=f"ses_{n}", project_id=PROJECT.id, role="coder",
                started_at=seen, last_seen_at=seen,
            )
        )  # fmt: skip
    recent = now - timedelta(minutes=30)
    assert [s.id for s in store.team_sessions_seen_since(PROJECT.id, recent)] == ["ses_3", "ses_2"]
    named = store.team_sessions_seen_since(PROJECT.id, recent, ids=["ses_1", "ses_2", "ses_9"])
    assert [s.id for s in named] == ["ses_3", "ses_2", "ses_1"], "newest seen first, once each"
    assert store.team_sessions_seen_since("prj_other", recent, ids=["ses_1"]) == []
    alone = store.team_sessions_seen_since(PROJECT.id, None, ids=["ses_1", "ses_3", "ses_9"])
    assert [s.id for s in alone] == ["ses_3", "ses_1"], "no window: the ones named, and only them"
    assert store.team_sessions_seen_since(PROJECT.id, None) == []


def _board(store: ContextStore, project_id: str, *events: tuple[str, str | None, datetime]) -> None:
    """``(kind, session id, written at)`` events on ``project_id``'s board, in that order."""
    from aisquare.core.ids import new_event_id
    from aisquare.models import TeamEvent

    for kind, session_id, at in events:
        store.add_team_event(
            TeamEvent(
                id=new_event_id(), project_id=project_id, session_id=session_id, kind=kind,
                text=kind, created_at=at,
            )
        )  # fmt: skip


def test_the_events_since_a_time_are_the_kinds_asked_for_from_that_time_on(
    store: ContextStore,
) -> None:
    """A board's last day, for a reader that asks every few seconds (remote's needs scan):
    the agents' events of ``kinds``, the human's (no session) of ``human_kinds`` too, and
    nothing written before ``since`` or on another board."""
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    store.ensure_project(ProjectInfo(id="prj_other", root=Path("/tmp/other")))
    _board(
        store,
        PROJECT.id,
        ("question", "ses_1", now - timedelta(days=2)),
        ("question", "ses_1", now - timedelta(hours=3)),
        ("note", "ses_1", now - timedelta(hours=2)),
        ("note", None, now - timedelta(hours=2)),
        ("decision", None, now - timedelta(hours=1)),
        ("attention", None, now - timedelta(hours=1)),
        ("result", "ses_2", now),
    )
    _board(store, "prj_other", ("question", "ses_9", now))
    since = now - timedelta(days=1)
    read = store.team_events_since(
        PROJECT.id, since, kinds=["question", "result"], human_kinds=["note", "decision"]
    )
    assert [(e.kind, e.session_id) for e in read] == [
        ("question", "ses_1"),
        ("note", None),
        ("decision", None),
        ("result", "ses_2"),
    ], "oldest first; an agent's note is not the human's"
    assert all(event.created_at >= since for event in read)
    assert store.team_events_since(PROJECT.id, now + timedelta(seconds=1), kinds=["result"]) == []
    assert store.team_events_since(PROJECT.id, since, kinds=[]) == []


def test_the_events_since_a_time_can_be_one_sessions_kinds_alone(store: ContextStore) -> None:
    """Review of #259, round 28, 8/10: of a board remote's needs scan does not read whole, it
    takes the posts of its own rows' sessions, the human's replies and the fleet's kinds. It
    read the board's whole day for them and dropped the rest itself; the store reads only
    those (``session_kinds`` of ``sessions``)."""
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    _board(
        store,
        PROJECT.id,
        ("question", "ses_1", now - timedelta(hours=3)),
        ("question", "ses_2", now - timedelta(hours=3)),
        ("note", "ses_1", now - timedelta(hours=2)),
        ("note", None, now - timedelta(hours=2)),
        ("agent_exited", "ses_3", now - timedelta(hours=1)),
        ("result", "ses_1", now),
    )
    since = now - timedelta(days=1)

    read = store.team_events_since(
        PROJECT.id,
        since,
        kinds=["agent_exited"],
        human_kinds=["note"],
        session_kinds=["question", "result"],
        sessions=["ses_1"],
    )

    assert [(e.kind, e.session_id) for e in read] == [
        ("question", "ses_1"),
        ("note", None),
        ("agent_exited", "ses_3"),
        ("result", "ses_1"),
    ], "ses_2's question and ses_1's own note are not asked for"
    no_session = store.team_events_since(
        PROJECT.id, since, kinds=[], session_kinds=["question"], sessions=[]
    )
    assert no_session == []


def test_the_events_since_a_time_do_not_take_seq_order_for_stamp_order(
    store: ContextStore,
) -> None:
    """Review of #259, round 6, 6/10: both reads started from, or stopped at, the newest event
    stamped before ``since``, as if ``seq`` order were stamp order. A clock stepped back an
    hour (NTP, a VM's snapshot) writes seq 11 at 11:00 after seq 10 at 12:00, and asked from
    11:30 the read began past seq 11 and never saw seq 10, a question for the human and an
    agent's last attention among them, for as long as the window asked held it."""
    from datetime import timedelta

    noon = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    _board(
        store,
        PROJECT.id,
        ("question", "ses_1", noon - timedelta(hours=3)),
        ("question", "ses_1", noon),  # seq 2, stamped before the clock stepped back
        ("attention", "ses_2", noon + timedelta(minutes=1)),
        ("note", "ses_1", noon - timedelta(hours=1)),  # the clock stepped back an hour
    )
    since = noon - timedelta(minutes=30)
    read = store.team_events_since(PROJECT.id, since, kinds=["question", "attention"])
    assert [(e.kind, e.created_at) for e in read] == [
        ("question", noon),
        ("attention", noon + timedelta(minutes=1)),
    ]
    found = store.newest_session_event(PROJECT.id, "ses_2", "attention", since=since)
    assert found is not None and found.created_at == noon + timedelta(minutes=1)


def test_a_store_already_at_v26_without_the_board_stamps_index_gets_it_at_its_next_open() -> None:
    """An earlier 0.8.1 candidate stamped v26 without ``team_event_project_created``: the
    presence pass adds it, and the reads since a time are narrowed by it from then on."""
    open_store().close()
    raw = sqlite3.connect(str(_db_path()))
    try:
        raw.execute("DROP INDEX team_event_project_created")
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()
    with store_session() as store:
        assert store.missing_schema() == []
    raw = sqlite3.connect(str(_db_path()))
    try:
        plan = raw.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM team_event WHERE project_id = 'p' "
            "AND created_at >= 'x' ORDER BY seq"
        ).fetchall()
    finally:
        raw.close()
    assert "team_event_project_created" in str(plan), plan


def _short_and_long_boards(store: ContextStore, now: datetime) -> int:
    """``prj_short`` and ``prj_long``: the same last half hour, a question a minute, over 20
    and 2 000 older notes. Returns how many questions that half hour holds."""
    from datetime import timedelta

    day = [("question", "ses_1", now - timedelta(minutes=n)) for n in range(30, 0, -1)]
    for project_id, history in (("prj_short", 20), ("prj_long", 2_000)):
        store.ensure_project(ProjectInfo(id=project_id, root=Path(f"/tmp/{project_id}")))
        old = now - timedelta(days=30)
        _board(
            store,
            project_id,
            *[("note", "ses_1", old + timedelta(seconds=n)) for n in range(history)],
        )
        _board(store, project_id, *day)
    return len(day)


def _steps(store: ContextStore, read: Callable[[], object]) -> int:
    """How many SQLite VM steps ``read`` took: what its queries cost, on any machine."""
    connection = store._conn  # type: ignore[attr-defined]
    counted = [0]

    def step() -> int:
        counted[0] += 1
        return 0

    connection.set_progress_handler(step, 1)
    try:
        read()
    finally:
        connection.set_progress_handler(None, 1)
    return counted[0]


def test_the_events_since_a_time_cost_that_time_not_the_boards_history(
    store: ContextStore,
) -> None:
    """Filtered on ``created_at`` with no index on it, a read walks every event a board ever
    had, which is never pruned. The ``(project_id, created_at)`` index narrows it to the
    events since ``since``: a board with a hundred times the history costs the same."""
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    asked = _short_and_long_boards(store, now)
    since = now - timedelta(days=1)
    for project_id in ("prj_short", "prj_long"):
        assert len(store.team_events_since(project_id, since, kinds=["question"])) == asked
    short = _steps(store, lambda: store.team_events_since("prj_short", since, kinds=["question"]))
    long = _steps(store, lambda: store.team_events_since("prj_long", since, kinds=["question"]))
    assert long < short + 500, (short, long)


def test_a_sessions_newest_event_of_a_kind_is_the_one_since_a_time(store: ContextStore) -> None:
    """The session's newest event of the kind, when it was written at or after ``since``."""
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    _board(
        store,
        PROJECT.id,
        ("attention", "ses_1", now - timedelta(hours=3)),
        ("attention", "ses_1", now - timedelta(hours=1)),
        ("attention", "ses_2", now - timedelta(minutes=30)),
        ("limited", "ses_1", now - timedelta(minutes=20)),
        ("note", None, now - timedelta(minutes=10)),
    )
    found = store.newest_session_event(
        PROJECT.id, "ses_1", "attention", since=now - timedelta(hours=2)
    )
    assert found is not None and found.created_at == now - timedelta(hours=1)
    later = now - timedelta(minutes=50)
    assert store.newest_session_event(PROJECT.id, "ses_1", "attention", since=later) is None
    assert store.newest_session_event(PROJECT.id, "ses_3", "attention", since=later) is None
    assert store.newest_session_event("prj_other", "ses_1", "limited", since=later) is None


def test_a_session_with_no_such_event_costs_the_time_asked_not_the_boards_history(
    store: ContextStore,
) -> None:
    """Asked by the session and the kind alone, a session that has no such event walked
    every event its board ever had to say so, every few seconds. The ``(project_id,
    created_at)`` index narrows it to the events since ``since``: a hundred times the
    history costs the same."""
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    _short_and_long_boards(store, now)
    since = now - timedelta(days=1)
    short = _steps(
        store, lambda: store.newest_session_event("prj_short", "ses_9", "x", since=since)
    )
    long = _steps(store, lambda: store.newest_session_event("prj_long", "ses_9", "x", since=since))
    assert long < short + 500, (short, long)


# --- the launch spec and ui_state (#144) ----------------------------------------------------


def test_a_fleet_agents_launch_spec_round_trips_and_an_old_row_has_none(
    store: ContextStore,
) -> None:
    from aisquare.models import FleetAgent, LaunchSpec

    spec = LaunchSpec(
        binary="claude",
        permission_mode="auto",
        extra_args=["--chrome", "--effort", "high"],
        account_slot=2,
        worktree=True,
        command=["python", "-P", "-m", "aisquare", "launch", "coder"],
    )
    row = FleetAgent(
        id="agt_spec", project_id=PROJECT.id, label="coder-1", role="coder", pane_id="%1",
        cwd=Path("/w"), created_at=datetime.now(tz=UTC), launch_spec=spec,
    )  # fmt: skip
    stored = store.upsert_fleet_agent(row)
    assert stored.launch_spec == spec
    bare = store.upsert_fleet_agent(
        row.model_copy(update={"id": "agt_old", "label": "coder-2", "launch_spec": None})
    )
    assert bare.launch_spec is None
    # A spec that no longer parses (a future field renamed) costs the replay, not the row.
    with sqlite3.connect(str(_db_path())) as raw:
        raw.execute("UPDATE fleet_agent SET launch_spec = '{not json' WHERE id = 'agt_spec'")
    broken = store.get_fleet_agent("agt_spec")
    assert broken is not None and broken.launch_spec is None


def _fleet_history(store: ContextStore, rows: int, *, projects: int = 1, labels: int = 7) -> None:
    """``rows`` fleet rows over ``projects`` projects and ``labels`` labels each, a minute
    apart, the newest of each label live: a fleet's spawns, restarts and switches, which
    are never deleted."""
    from datetime import timedelta

    start = datetime(2026, 1, 1, tzinfo=UTC)
    newest = {(f"prj_{i % projects}", f"coder-{i % labels}"): i for i in range(rows)}
    store._conn.executemany(  # type: ignore[attr-defined]
        "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at, "
        "ended_at) VALUES (?, ?, ?, 'coder', '%1', '/w', ?, ?)",
        [
            (
                f"agt_{i:06d}",
                f"prj_{i % projects}",
                f"coder-{i % labels}",
                (start + timedelta(minutes=i)).isoformat(),
                None
                if newest[(f"prj_{i % projects}", f"coder-{i % labels}")] == i
                else (start + timedelta(minutes=i, seconds=30)).isoformat(),
            )
            for i in range(rows)
        ],
    )


def test_the_label_heads_are_every_live_row_and_each_labels_newest(store: ContextStore) -> None:
    """``fleet_label_heads``: every live row, and the newest of each label, live or ended,
    in every project, oldest first. A live row with a newer ended one of its label (a spawn
    that failed after it) is both's, and so is a label all of whose rows ended."""
    from datetime import timedelta

    _fleet_history(store, 60, projects=3)
    late = datetime(2026, 2, 1, tzinfo=UTC)
    store._conn.executemany(  # type: ignore[attr-defined]
        "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at, "
        "ended_at) VALUES (?, ?, ?, 'coder', '%1', '/w', ?, ?)",
        [
            ("agt_failed", "prj_0", "coder-0", late.isoformat(), late.isoformat()),
            ("agt_gone_1", "prj_1", "stopped", late.isoformat(), late.isoformat()),
            ("agt_gone_2", "prj_1", "stopped", (late + timedelta(1)).isoformat(), late.isoformat()),
        ],
    )
    every = store._conn.execute(  # type: ignore[attr-defined]
        "SELECT id, project_id, label, created_at, ended_at FROM fleet_agent"
    ).fetchall()
    newest: dict[tuple[str, str], tuple[str, str]] = {}
    for agent_id, project_id, label, created_at, _ended in every:
        newest[(project_id, label)] = max(
            newest.get((project_id, label), ("", "")), (created_at, agent_id)
        )
    expected = sorted(
        (created_at, agent_id)
        for agent_id, project_id, label, created_at, ended_at in every
        if ended_at is None or newest[(project_id, label)] == (created_at, agent_id)
    )

    heads = store.fleet_label_heads()

    assert [head.id for head in heads] == [agent_id for _created, agent_id in expected]
    assert {"agt_failed", "agt_gone_2"} <= {head.id for head in heads}
    assert "agt_gone_1" not in {head.id for head in heads}


def test_forgotten_among_answers_for_ids_read_or_not(store: ContextStore) -> None:
    """Review of #259, round 42, 11/11: a plain prune asks ``forgotten_among`` of candidates it
    could not read at all (a read that leaves tombstones out), to tell a forgotten row from a
    purged one. It answers for any ids: the tombstones among them, and neither a live row
    nor one the store no longer holds."""
    for project_id in ("prj_forgotten", "prj_purged"):
        store.onboard_project(ProjectInfo(id=project_id, root=Path(f"/tmp/{project_id}")))
        store.forget_project(project_id)
    store.purge_project("prj_purged")
    asked = [PROJECT.id, "prj_forgotten", "prj_purged", "prj_never"]

    assert all(store.get_project(project_id) is None for project_id in asked[1:]), "unread"
    assert store.forgotten_among(asked) == {"prj_forgotten"}


def test_a_label_lookup_and_the_label_heads_pick_one_row_of_two_stamped_alike(
    store: ContextStore,
) -> None:
    """Review of #259, round 41, 4/6: the heads break a tie on ``created_at`` with the greater
    id, and ``fleet_agent_by_label``, the row a ``fleet stop`` or a ``restart`` by label acts
    on, broke none: SQLite gave whichever row its plan reached first, and the holds could
    judge one row while a restart acted on the other. Both give the greater id, whichever
    was written first."""
    stamped = datetime(2026, 2, 1, tzinfo=UTC).isoformat()
    store._conn.executemany(  # type: ignore[attr-defined]
        "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at, "
        "ended_at) VALUES (?, 'prj_0', 'coder-1', 'coder', '%1', '/w', ?, ?)",
        [("agt_b", stamped, stamped), ("agt_a", stamped, stamped)],
    )

    [head] = store.fleet_label_heads()
    found = store.fleet_agent_by_label("prj_0", "coder-1", live_only=False)

    assert found is not None
    assert (head.id, found.id) == ("agt_b", "agt_b")


def test_a_session_lookup_picks_the_greater_id_of_two_live_rows_stamped_alike(
    store: ContextStore,
) -> None:
    """Review of #259, round 42, 7/11: ``fleet_agent_for_session``, which the session-start
    hook, ``task next`` and the limit hand-over read, broke no tie on ``created_at``: for two
    live rows on one session stamped alike, SQLite gave whichever its plan reached first. It
    gives the greater id, as the label lookup and the label heads do, whichever was first."""
    stamped = datetime(2026, 2, 1, tzinfo=UTC).isoformat()
    store._conn.executemany(  # type: ignore[attr-defined]
        "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at, "
        "session_id) VALUES (?, 'prj_0', ?, 'coder', '%1', '/w', ?, 'ses_1')",
        [("agt_b", "coder-2", stamped), ("agt_a", "coder-1", stamped)],
    )

    found = store.fleet_agent_for_session("ses_1")

    assert found is not None and found.id == "agt_b"


def test_the_label_heads_cost_grows_with_the_rows_not_their_square(store: ContextStore) -> None:
    """Review of #259, round 37, 4/7: each label's newest was a correlated ``NOT EXISTS`` per
    row, over the ``(project_id, created_at)`` index, which has no ``label``: quadratic in a
    project's rows, 4.6 s at 20 000, on every ``project forget`` and ``prune`` and doctor's
    capture-off line at each ``asq`` start. A window over the table is one pass and a
    sort: four times the rows, a label for every five, cost about four times the steps,
    not sixteen."""
    _fleet_history(store, 500, labels=100)
    small = _steps(store, store.fleet_label_heads)
    store._conn.execute("DELETE FROM fleet_agent")  # type: ignore[attr-defined]
    _fleet_history(store, 2000, labels=400)
    large = _steps(store, store.fleet_label_heads)
    assert large < small * 6, (small, large)


def test_ui_state_is_a_key_value_memory(store: ContextStore) -> None:
    assert store.ui_state("fleet.selected") is None
    store.set_ui_state("fleet.selected", "project:prj_test")
    assert store.ui_state("fleet.selected") == "project:prj_test"
    store.set_ui_state("fleet.selected", "agent:prj_test/agt_1")  # every change is the save
    assert store.ui_state("fleet.selected") == "agent:prj_test/agt_1"
    store.set_ui_state("fleet.selected", None)
    assert store.ui_state("fleet.selected") is None


# #201's v15 as its ladder writes it, copied line for line from _SCHEMA_V15 at
# its head 87526ceff8d9 (2026-09-24): the two persona columns and nothing else.
PERSONA_V15_DDL = """
ALTER TABLE team_session ADD COLUMN persona TEXT;
ALTER TABLE fleet_agent ADD COLUMN persona TEXT;
"""


@pytest.mark.parametrize(
    ("label", "cohort"),
    [
        ("main's v14, passing through this branch's v15", "V14"),
        ("the hackathon branch's persona v15 (#201)", "PERSONA15"),
        ("this branch's own v15, stamped before v16 existed", "ACCOUNTS15"),
        ("the persona v15 run through v16 by a build without the step (this box)", "THISBOX16"),
        ("the persona v15 run to 21 by a build without the step", "RANTO21"),
    ],
)
def test_every_shape_of_user_version_15_converges_on_one_schema(label: str, cohort: str) -> None:
    """The v15 fork (#201 persona against #203 accounts): whichever route a store
    took to 15, it reaches the current version with the account registry, the
    account slot AND the persona columns present.

    As with the v11 cohorts, the end state is asserted by WRITING to the halves a
    cohort could silently lack: a persona-15 store opened by this ladder without
    the converge step stamps 21 with no ``claude_account`` and no
    ``fleet_agent.account_slot``, and nothing raises until the first fleet read.
    The last two cohorts are stores that ALREADY went past 15 that way, stamped
    16 (the crew's own board store, 2026-09-24) and 21: no ladder step will ever
    run on them again, so only the presence pass after the ladder can reach them.
    Each history is built as the build that made it ran it: a step past 15 with the
    columns it added (``_run_step``), and this branch's own v15 as it was, the
    registry and ``account_slot`` with no persona column.
    """
    from pathlib import Path

    from aisquare.core.store import _SCHEMA_V15, _run_step

    if cohort == "V14":
        db = _at_version(14)
    elif cohort == "ACCOUNTS15":
        accounts = _SCHEMA_V15 + "ALTER TABLE fleet_agent ADD COLUMN account_slot INTEGER;\n"
        db = _at_version(14, after=accounts, stamp=15)
    else:
        carried_to = {"PERSONA15": 15, "THISBOX16": 16, "RANTO21": 21}[cohort]
        db = _at_version(14, after=PERSONA_V15_DDL, stamp=15)
        raw = sqlite3.connect(str(db))
        try:
            for step in range(15, carried_to):
                _run_step(raw, step)
            raw.execute(f"PRAGMA user_version = {carried_to}")
            raw.commit()
        finally:
            raw.close()

    store = open_store()  # migrates on open; a wedge raises out of here
    try:
        account = store.upsert_claude_account(1, Path("/tmp/claude-accounts/1"))
        assert account.slot == 1, label
    finally:
        store.close()

    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION, label
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"claude_account", "project_setting", "claude_usage"} <= tables, label
        fleet = {r[1] for r in raw.execute("PRAGMA table_info(fleet_agent)")}
        session = {r[1] for r in raw.execute("PRAGMA table_info(team_session)")}
        assert {"account_slot", "launch_spec", "persona"} <= fleet, label
        assert {"limit_resets_at", "persona"} <= session, label
        # and both halves take a row, whichever way in
        raw.execute(
            "INSERT INTO project (id, name, root, linked_repos, created_at, codename) "
            "VALUES ('prj_f', 'f', '/tmp/f', '[]', '2026-01-01T00:00:00+00:00', 'kestrel')"
        )
        raw.execute(
            "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at, "
            "account_slot, persona) VALUES ('agt_1', 'prj_f', 'a', 'dev', '%1', '/tmp/f', "
            "'2026-01-01T00:00:00+00:00', 1, 'skeptic')"
        )
        raw.commit()
        (slot, persona) = raw.execute(
            "SELECT account_slot, persona FROM fleet_agent WHERE id = 'agt_1'"
        ).fetchone()
        assert (slot, persona) == (1, "skeptic"), label
    finally:
        raw.close()


def test_a_fleet_row_is_read_as_the_columns_it_is_written_with_whatever_follows_board(
    store: ContextStore,
) -> None:
    """Review of #259, round 27, 7/7: the read list was the written one's text with
    ``board`` cut off its end, so a column appended after it made every fleet read invalid
    SQL. Both lists come from one tuple, and a read names each column as it is written."""
    later = (*store_module._FLEET_AGENT_FIELDS, "later")
    read = store_module._read_list(later, {"board": "COALESCE(board, project_id)"})
    # `persona` (v33), `ended_by` (v34) and the first message's two (v36) are the columns
    # that came after it, and are read as written.
    assert read.endswith(
        ", COALESCE(board, project_id) AS board, persona, ended_by, first_prompt, "
        "first_prompt_at, later"
    ), read

    cursor = store._conn.execute(  # type: ignore[attr-defined]
        f"SELECT {store_module._FLEET_AGENT_READ} FROM fleet_agent LIMIT 0"
    )
    named = tuple(column[0] for column in cursor.description)
    assert named == store_module._FLEET_AGENT_FIELDS, named
    assert ", ".join(named) == store_module._FLEET_AGENT_COLUMNS


def test_a_store_without_team_session_still_reads_its_fleet_rows() -> None:
    """Review of #259, round 27, 5/7: a row that recorded no board reads its session's, a
    subquery on ``team_session``, and it was in every fleet read. On a store without that
    table (doctor's damaged-store case, which the v32 backfill tolerates) every fleet read
    failed, the live-only ones doctor's own checks make included. Such a store reads a row
    with no board as its project's, as the backfill gives it there."""
    from aisquare.models import FleetAgent

    with store_session() as store:
        store.ensure_project(PROJECT)
        for n, board in ((1, None), (2, "prj_hub")):
            store.upsert_fleet_agent(
                FleetAgent(
                    id=f"agt_{n}", project_id=PROJECT.id, label=f"coder-{n}", role="coder",
                    pane_id=f"%{n}", cwd=Path("/w"), created_at=datetime.now(tz=UTC),
                    board=board,
                )
            )  # fmt: skip
    with sqlite3.connect(str(_db_path())) as raw:
        raw.execute("UPDATE fleet_agent SET board = NULL WHERE id = 'agt_1'")  # 0.8.0's
    raw.close()
    with sqlite3.connect(str(_db_path())) as raw:
        raw.execute("DROP TABLE team_session")
    raw.close()

    with store_session() as store:
        assert "table team_session" in store.missing_schema()
        live = {row.id: row.board for row in store.fleet_agents(PROJECT.id, live_only=True)}
        assert live == {"agt_1": PROJECT.id, "agt_2": "prj_hub"}, live
        assert [row.id for row in store.live_fleet_agents()] == ["agt_1", "agt_2"]
        one = store.get_fleet_agent("agt_1")
        assert one is not None and one.board == PROJECT.id
        assert store.fleet_agent_boards(PROJECT.id) == sorted([PROJECT.id, "prj_hub"])
        assert store.fleet_agent_by_label(PROJECT.id, "coder-2") is not None


def test_fleet_board_is_a_live_rows_a_live_managers_first() -> None:
    """Review of #259, round 30, 1/8: an ended row's board was the answer once no row was
    live, so Remote kept a project's tabs on its last spawn's hub for as long as it ran no
    agent. Only a live row's board is."""
    from datetime import timedelta

    from aisquare.models import FleetAgent

    start = datetime.now(tz=UTC)

    def row(n: int, role: str, board: str, *, ended: bool = False) -> FleetAgent:
        return FleetAgent(
            id=f"agt_{n}", project_id=PROJECT.id, label=f"{role}-{n}", role=role,
            pane_id=f"%{n}", cwd=Path("/w"), created_at=start + timedelta(minutes=n),
            board=board, ended_at=start + timedelta(hours=1) if ended else None,
        )  # fmt: skip

    with store_session() as store:
        store.ensure_project(PROJECT)
        assert store.fleet_board(PROJECT.id) is None, "before its first spawn"
        store.upsert_fleet_agent(row(1, "manager", "prj_hub", ended=True))
        assert store.fleet_board(PROJECT.id) is None, "an ended manager's hub is not it"
        store.upsert_fleet_agent(row(2, "coder", PROJECT.id))
        store.upsert_fleet_agent(row(3, "coder", "prj_b"))
        store.upsert_fleet_agent(row(4, "coder", "prj_c", ended=True))
        assert store.fleet_board(PROJECT.id) == "prj_b", "the newest live row's"
        store.upsert_fleet_agent(row(0, "manager", "prj_m"))
        assert store.fleet_board(PROJECT.id) == "prj_m", "a live manager's first"


def test_fleet_manager_board_is_the_newest_manager_rows_a_live_one_first() -> None:
    """Review of #259, round 32, 2/9: the board a pause is set on besides its homes is the one
    a manager of the project reads, or the one a restart of its last one hands on. Only a
    manager's: a coder never reads the pause."""
    from datetime import timedelta

    from aisquare.models import FleetAgent

    start = datetime.now(tz=UTC)

    def row(n: int, role: str, board: str, *, ended: bool = False) -> FleetAgent:
        return FleetAgent(
            id=f"agt_{n}", project_id=PROJECT.id, label=f"{role}-{n}", role=role,
            pane_id=f"%{n}", cwd=Path("/w"), created_at=start + timedelta(minutes=n),
            board=board, ended_at=start + timedelta(hours=1) if ended else None,
        )  # fmt: skip

    with store_session() as store:
        store.ensure_project(PROJECT)
        store.upsert_fleet_agent(row(9, "coder", "prj_c"))
        assert store.fleet_manager_board(PROJECT.id) is None, "before its first manager"
        store.upsert_fleet_agent(row(1, "manager", "prj_old", ended=True))
        store.upsert_fleet_agent(row(2, "manager", "prj_hub", ended=True))
        assert store.fleet_manager_board(PROJECT.id) == "prj_hub", "the newest, stopped"
        store.upsert_fleet_agent(row(0, "manager", "prj_m"))
        assert store.fleet_manager_board(PROJECT.id) == "prj_m", "a live one first"
