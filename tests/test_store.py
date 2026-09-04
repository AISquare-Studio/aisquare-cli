"""SQLite store: CRUD, pool scoping, soft-delete, prefix lookup and search."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core.ids import new_entry_id
from aisquare.core.store import AmbiguousIdError, ContextStore, open_store, store_session
from aisquare.models import ContextEntry, Pool, ProjectInfo

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
    ids = {project.id for project in store.list_projects()}
    assert ids == {PROJECT.id, "prj_other"}
    assert store.get_project("prj_other") == other
    assert store.get_project("prj_missing") is None


def test_find_projects_by_name_and_id_prefix(store: ContextStore) -> None:
    assert [p.id for p in store.find_projects("example-project")] == [PROJECT.id]  # by name
    assert [p.id for p in store.find_projects(PROJECT.id[:8])] == [PROJECT.id]  # by id prefix
    assert store.find_projects("nope") == []


def test_add_linked_repo_is_idempotent(store: ContextStore) -> None:
    updated = store.add_linked_repo(PROJECT.id, "git@github.com:acme/app.git")
    assert updated.linked_repos == ["git@github.com:acme/app.git"]
    again = store.add_linked_repo(PROJECT.id, "git@github.com:acme/app.git")
    assert again.linked_repos == ["git@github.com:acme/app.git"]  # no duplicate


def test_add_linked_repo_unknown_project_raises(store: ContextStore) -> None:
    with pytest.raises(KeyError):
        store.add_linked_repo("prj_missing", "repo")


def test_add_and_list_prompts(store: ContextStore) -> None:
    store.add_prompt("first prompt", PROJECT.id)
    store.add_prompt("second prompt", PROJECT.id)
    prompts = store.recent_prompts(PROJECT.id)
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
    assert version == SCHEMA_VERSION == 11  # v11: fleet_agent + project.codename


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
