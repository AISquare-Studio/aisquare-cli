"""Workspace credits next to where traces land (#143).

Controls: the API's ``-1`` reads as unlimited and draws no bar; a band the
server computed is shown, never re-derived; a 400 (no workspace context), a
changed shape and an expired session each become a reason on the row and
nothing else; the reading is cached for a minute and the cache is skipped when
asked; a session for another host is not asked about this workspace at all.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.models import TraceDestination
from aisquare.services import credits, iam

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
BALANCE = {
    "period": "2026-09",
    "state": "low",
    "pools": {
        "run_credits": {
            "daily": {
                "used": 380,
                "limit": 500,
                "remaining": 120,
                "resets_at": "2026-09-13T18:00:00Z",
            },
            "monthly": {
                "used": 4000,
                "limit": 10000,
                "remaining": 6000,
                "resets_at": "2026-10-01T00:00:00Z",
            },
            "state": "low",
        },
        "build_credits": {
            "daily": {"used": 3, "limit": -1, "remaining": -1, "resets_at": None},
            "monthly": {"used": 3, "limit": -1, "remaining": -1, "resets_at": None},
            "state": "ok",
        },
    },
}


def _session(api_url: str = "https://stg-api.aisquare.studio") -> iam.Session:
    return iam.Session(api_url=api_url, token="aisq_t", source="env", email="a@b.c")


def _destination(api_url: str = "https://stg-api.aisquare.studio") -> TraceDestination:
    return TraceDestination(
        project_id="prj_x",
        api_url=api_url,
        environment="stg",
        workspace_id=42,
        workspace_uid="ws-uid-42",
        workspace_name="acme",
        studio_id=301,
        studio_name="Frontend",
        set_at=NOW,
    )


def test_the_payload_reads_as_windows_and_minus_one_is_unlimited() -> None:
    reading = credits.parse(BALANCE, workspace_id=42, workspace_name="acme", now=NOW)
    assert reading.available and reading.state == "low" and reading.period == "2026-09"
    daily = reading.window("run", "daily")
    assert daily is not None and (daily.used, daily.limit, daily.remaining) == (380, 500, 120)
    assert daily.percent == 76.0
    assert daily.resets_at == datetime(2026, 9, 13, 18, tzinfo=UTC)
    build = reading.window("build", "daily")
    assert build is not None and build.limit is None and build.percent is None, "-1 = unlimited"
    assert sorted(reading.windows) == ["build.daily", "build.monthly", "run.daily", "run.monthly"]
    assert reading.as_json()["pools"]["run.daily"]["percent"] == 76.0


def test_an_unexpected_shape_is_a_reason_not_an_error() -> None:
    reading = credits.parse(["nope"], workspace_id=42, workspace_name="acme", now=NOW)
    assert not reading.available and reading.reason == "unexpected balance shape"
    partial = credits.parse({"state": "ok"}, workspace_id=42, workspace_name="acme", now=NOW)
    assert partial.available and partial.windows == {}
    assert credits.describe(partial).endswith("— no pools reported")


def test_describe_has_one_voice() -> None:
    reading = credits.parse(BALANCE, workspace_id=42, workspace_name="acme", now=NOW)
    line = credits.describe(reading, now=NOW)
    assert line.startswith("acme [low] — run credits: today 120 of 500 left (24%) · resets in 6h")
    assert "month 6,000 of 10,000 left (60%)" in line
    assert "build credits: today unlimited; month unlimited" in line
    assert line.endswith("· period 2026-09")
    assert credits.describe(None) == "(no destination chosen)"
    gone = credits.WorkspaceCredits(42, "acme", available=False, reason="HTTP 400: no context")
    assert credits.describe(gone) == "acme: credits unavailable — HTTP 400: no context"


def test_for_destination_asks_the_right_host_with_the_workspace_header_and_caches(
    isolated_home: Path, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    calls: list[dict[str, object]] = []

    def request(path: str, **kwargs: object) -> iam.HttpResult:
        calls.append({"path": path, **kwargs})
        return iam.HttpResult(200, dict(BALANCE), {})

    monkeypatch.setattr(iam, "request", request)
    session, destination = _session(), _destination()
    first = credits.for_destination(session, destination, now=NOW)
    assert first is not None and first.available and first.workspace_name == "acme"
    assert calls == [
        {
            "path": "api/v2/credits/balance/",
            "workspace": "ws-uid-42",
            "api_url": "https://stg-api.aisquare.studio",
            "tolerate": (400, 403, 404),
            "timeout": credits.TIMEOUT_SECONDS,
        }
    ]
    # Within the minute: the cache answers, the API is not asked again.
    second = credits.for_destination(session, destination, now=NOW + timedelta(seconds=30))
    assert second is not None and second.windows == first.windows and len(calls) == 1
    # Asked to skip the cache, or after it aged out: a new request.
    credits.for_destination(session, destination, now=NOW, use_cache=False)
    assert len(calls) == 2
    monkeypatch.setattr(credits, "CACHE_SECONDS", 0.0)
    credits.for_destination(session, destination, now=NOW + timedelta(seconds=90))
    assert len(calls) == 3
    # Nothing to ask: no session, no destination, or a session for another host.
    assert credits.for_destination(None, destination) is None
    assert credits.for_destination(session, None) is None
    assert credits.for_destination(_session("https://api.aisquare.studio"), destination) is None
    assert len(calls) == 3


def test_failures_are_reasons_on_the_row(isolated_home: Path, monkeypatch: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    answers: list[object] = [
        iam.HttpResult(
            400, {"error": "Workspace context required. Please select a workspace."}, {}
        ),
        iam.IamError("session_expired", "Your AISquare session has expired or was revoked."),
        iam.HttpResult(200, "not json we know", {}),
    ]

    def request(path: str, **kwargs: object) -> iam.HttpResult:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, iam.HttpResult)
        return answer

    monkeypatch.setattr(iam, "request", request)
    session, destination = _session(), _destination()
    no_context = credits.for_destination(session, destination, now=NOW, use_cache=False)
    assert no_context is not None and not no_context.available
    assert no_context.reason == "HTTP 400: Workspace context required. Please select a workspace."
    expired = credits.for_destination(session, destination, now=NOW, use_cache=False)
    assert expired is not None
    assert expired.reason == "Your AISquare session has expired or was revoked."
    odd = credits.for_destination(session, destination, now=NOW, use_cache=False)
    assert odd is not None and odd.reason == "unexpected balance shape"
    cached = list((paths.cache_dir() / "credits").glob("*.json"))
    assert not cached, "failures are not cached"


def test_a_figure_past_a_floats_range_is_unreadable_not_an_error() -> None:
    """``parse`` never raises: an integer ``float()`` cannot hold is ``OverflowError``,
    not ``ValueError``, and read as no figure at all it costs one number, not the row."""
    huge = {"pools": {"run_credits": {"daily": {"used": 10**400, "limit": 500}}}}
    reading = credits.parse(huge, workspace_id=42, workspace_name="acme", now=NOW)
    daily = reading.window("run", "daily")
    assert reading.available and daily is not None
    assert daily.used == 0.0 and daily.limit == 500


def test_a_damaged_fresh_cache_is_refetched_never_trusted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #173, round 1: a cache file of another shape (an older or newer
    CLI, a hand edit) raised ``KeyError``/``TypeError``/``AttributeError`` out of
    ``for_destination`` — or read back a figure that raised later, when the row
    was drawn. Each is a miss now, as ``iam.discover`` treats its own cache."""
    calls: list[str] = []

    def request(path: str, **kwargs: object) -> iam.HttpResult:
        calls.append(path)
        return iam.HttpResult(200, dict(BALANCE), {})

    monkeypatch.setattr(iam, "request", request)
    session, destination = _session(), _destination()
    path = credits._cache_path(session, destination.workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = {"fetched_at": NOW.isoformat(), "workspace_id": 42, "workspace_name": "acme"}
    window = {"used": 1, "limit": 500, "remaining": 499, "resets_at": None}
    damaged: list[object] = [
        {"fetched_at": NOW.isoformat(), "windows": {}},  # no workspace: KeyError
        fresh | {"windows": ["run.daily"]},  # AttributeError
        fresh | {"windows": {"run.daily": "full"}},  # TypeError
        fresh | {"windows": {"run.daily": window | {"used": "lots"}}},  # drawn: TypeError
        fresh | {"windows": {"run.daily": window | {"limit": {"n": 500}}}},
        fresh | {"workspace_id": "forty-two", "windows": {}},
    ]
    for record in damaged:
        path.write_text(json.dumps(record), encoding="utf-8")
        reading = credits.for_destination(session, destination, now=NOW)
        assert reading is not None and reading.available, record
        run = reading.window("run", "daily")
        assert run is not None and run.limit == 500 and run.used == 380, record
        credits.describe(reading, now=NOW)  # and it draws
    assert len(calls) == len(damaged), "every damaged record was a miss"
    # The record the refetch wrote back is a hit again.
    credits.for_destination(session, destination, now=NOW)
    assert len(calls) == len(damaged)


def test_the_cache_is_per_credential_and_a_plain_file_name(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #173, round 1: the cache key was ``{host}-{workspace}``.

    Keyed by no credential, a second sign-in within the minute — another user,
    or an ``AISQUARE_TOKEN`` for another account — was handed the first one's
    reading, ``cost_true`` included. And the host kept a port's colon
    (``127.0.0.1:8123-42.json``), which on Windows names an NTFS alternate data
    stream rather than a file; ``iam._discovery_cache_path`` spells it ``-8123``.
    """
    calls: list[str] = []

    def request(path: str, **kwargs: object) -> iam.HttpResult:
        calls.append(path)
        return iam.HttpResult(200, dict(BALANCE), {})

    monkeypatch.setattr(iam, "request", request)
    local = "http://127.0.0.1:8123"
    mine = _session(local)
    theirs = iam.Session(api_url=local, token="aisq_someone_else", source="file", email="x@y.z")
    destination = _destination(local)
    assert credits.for_destination(mine, destination, now=NOW) is not None
    written = credits._cache_path(mine, destination.workspace_id)
    assert written.is_file() and ":" not in written.name
    assert written.name.startswith("127.0.0.1-8123-42-")
    assert "aisq_t" not in written.name, "the credential is hashed, never written out"
    assert credits.for_destination(theirs, destination, now=NOW) is not None
    assert len(calls) == 2, "another credential within the minute asks for itself"
    credits.for_destination(mine, destination, now=NOW + timedelta(seconds=30))
    credits.for_destination(theirs, destination, now=NOW + timedelta(seconds=30))
    assert len(calls) == 2, "each is its own cache hit"
    # A port `urlparse` cannot read names no cache file: it costs the cache, not the reading.
    odd = "https://stg-api.aisquare.studio:badport"
    reading = credits.for_destination(_session(odd), _destination(odd), now=NOW)
    assert reading is not None and reading.available and len(calls) == 3
