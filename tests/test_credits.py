"""Workspace credits next to where traces land (#143).

Controls: the API's ``-1`` reads as unlimited and draws no bar; a band the
server computed is shown, never re-derived; a 400 (no workspace context), a
changed shape and an expired session each become a reason on the row and
nothing else; the reading is cached for a minute and the cache is skipped when
asked; a session for another host is not asked about this workspace at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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
