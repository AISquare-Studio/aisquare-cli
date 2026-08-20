"""The CI response cache: hits, expiry, and never raising on the hot path."""

from __future__ import annotations

import json
import os
import time

import pytest

from aisquare.core import paths
from aisquare.services import ci_cache

_SESSION = "ses_01k9q8p3k7zr4m2n6v0c1d8e5f"


def test_a_written_entry_reads_back() -> None:
    ci_cache.write(_SESSION, "K", "payload", ttl_s=900)
    assert ci_cache.read(_SESSION, "K") == "payload"


def test_an_unknown_key_is_a_miss() -> None:
    assert ci_cache.read(_SESSION, "never-written") is None


def test_sessions_do_not_see_each_others_entries() -> None:
    ci_cache.write("ses_a", "K", "a", ttl_s=900)
    ci_cache.write("ses_b", "K", "b", ttl_s=900)
    assert ci_cache.read("ses_a", "K") == "a"
    assert ci_cache.read("ses_b", "K") == "b"


def test_an_expired_entry_is_a_miss() -> None:
    ci_cache.write(_SESSION, "K", "stale", ttl_s=1)
    assert ci_cache.read(_SESSION, "K") == "stale"
    # Rewrite the entry as already-expired rather than sleeping out the TTL.
    path = paths.ci_cache_dir() / f"{_SESSION}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"]["K"]["expires_at"] = time.time() - 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert ci_cache.read(_SESSION, "K") is None


def test_a_non_positive_ttl_means_do_not_cache() -> None:
    """A server turning caching off for one response should not leave a file
    behind that will never be read."""
    ci_cache.write(_SESSION, "K", "payload", ttl_s=0)
    assert ci_cache.read(_SESSION, "K") is None
    ci_cache.write(_SESSION, "K", "payload", ttl_s=-5)
    assert ci_cache.read(_SESSION, "K") is None


def test_later_writes_win() -> None:
    ci_cache.write(_SESSION, "K", "first", ttl_s=900)
    ci_cache.write(_SESSION, "K", "second", ttl_s=900)
    assert ci_cache.read(_SESSION, "K") == "second"


# --- nothing here raises ------------------------------------------------------


def test_a_corrupt_file_is_a_miss_not_a_crash() -> None:
    """The only cost of a miss is one request; an exception here reaches a hook
    wrapping a developer's prompt."""
    ci_cache.write(_SESSION, "K", "payload", ttl_s=900)
    (paths.ci_cache_dir() / f"{_SESSION}.json").write_text("{not json", encoding="utf-8")
    assert ci_cache.read(_SESSION, "K") is None


def test_a_file_holding_the_wrong_shape_is_a_miss() -> None:
    path = paths.ci_cache_dir() / f"{_SESSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in ["[]", "null", '"text"', '{"entries": []}', '{"entries": {"K": 7}}']:
        path.write_text(junk, encoding="utf-8")
        assert ci_cache.read(_SESSION, "K") is None


def test_an_entry_missing_its_expiry_is_a_miss() -> None:
    path = paths.ci_cache_dir() / f"{_SESSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": {"K": {"body": "x"}}}), encoding="utf-8")
    assert ci_cache.read(_SESSION, "K") is None


def test_writing_over_a_corrupt_file_recovers() -> None:
    path = paths.ci_cache_dir() / f"{_SESSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    ci_cache.write(_SESSION, "K", "payload", ttl_s=900)
    assert ci_cache.read(_SESSION, "K") == "payload"


def test_an_unwritable_home_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a directory", encoding="utf-8")
    monkeypatch.setattr(paths, "ci_cache_dir", lambda: blocked / "ci")
    ci_cache.write(_SESSION, "K", "payload", ttl_s=900)
    assert ci_cache.read(_SESSION, "K") is None
    ci_cache.clear(_SESSION)
    assert ci_cache.gc() == 0


# --- the session id is untrusted input ----------------------------------------


@pytest.mark.parametrize(
    "session_id",
    ["../../escape", "a/b/c", "..", "", "ses:with:colons", "x" * 400],
)
def test_a_hostile_session_id_stays_inside_the_cache_directory(session_id: str) -> None:
    """``session_id`` arrives from the agent's hook payload, so it is untrusted
    input being turned into a filename."""
    ci_cache.write(session_id, "K", "payload", ttl_s=900)
    written = list(paths.ci_cache_dir().glob("*.json"))
    for path in written:
        assert path.parent == paths.ci_cache_dir()
    assert ci_cache.read(session_id, "K") == "payload"


# --- housekeeping -------------------------------------------------------------


def test_the_entry_count_is_capped() -> None:
    """This file is read synchronously in front of every prompt — an unbounded
    JSON parse there is the cost the cache exists to avoid."""
    for index in range(ci_cache._MAX_ENTRIES + 50):
        ci_cache.write(_SESSION, f"K{index}", "payload", ttl_s=900 + index)
    stored = json.loads((paths.ci_cache_dir() / f"{_SESSION}.json").read_text(encoding="utf-8"))
    assert len(stored["entries"]) == ci_cache._MAX_ENTRIES


def test_the_cap_keeps_the_newest_entries() -> None:
    for index in range(ci_cache._MAX_ENTRIES + 10):
        ci_cache.write(_SESSION, f"K{index}", "payload", ttl_s=900 + index)
    assert ci_cache.read(_SESSION, f"K{ci_cache._MAX_ENTRIES + 9}") == "payload"
    assert ci_cache.read(_SESSION, "K0") is None


def test_clear_drops_one_session() -> None:
    ci_cache.write(_SESSION, "K", "payload", ttl_s=900)
    ci_cache.write("ses_other", "K", "payload", ttl_s=900)
    ci_cache.clear(_SESSION)
    assert ci_cache.read(_SESSION, "K") is None
    assert ci_cache.read("ses_other", "K") == "payload"


def test_clearing_an_absent_session_is_silent() -> None:
    ci_cache.clear("ses_never_existed")


def test_gc_removes_only_stale_session_files() -> None:
    """Nothing observes a session ending reliably, so without a sweep the
    directory only ever grows."""
    ci_cache.write("ses_old", "K", "payload", ttl_s=900)
    ci_cache.write("ses_new", "K", "payload", ttl_s=900)
    old = paths.ci_cache_dir() / "ses_old.json"
    ancient = time.time() - 200_000
    os.utime(old, (ancient, ancient))
    assert ci_cache.gc(max_age_s=86_400) == 1
    assert not old.exists()
    assert ci_cache.read("ses_new", "K") == "payload"


def test_gc_on_an_absent_directory_is_zero() -> None:
    assert ci_cache.gc() == 0


def test_no_temporary_files_survive_a_write() -> None:
    ci_cache.write(_SESSION, "K", "payload", ttl_s=900)
    assert list(paths.ci_cache_dir().glob("*.tmp")) == []
