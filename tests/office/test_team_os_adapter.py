"""The Team OS adapter, against a fake transport and recorded bodies.

**No test here opens a socket.** The peer is stopped on this machine and
starting it was not authorised, so every response comes from
``tests/fixtures/office-team-os/`` through an in-process fake and a stopped peer
is modelled by raising, which is what a refused loopback connect actually does.

Two of these assertions exist because the obvious version of them would be
wrong. A stopped peer is asserted to return *promptly*, not at the three-second
budget: loopback refusal is immediate, and a test that waited out the budget
would only pass against a firewall that drops packets. And an unreachable peer
is asserted to produce ``data=None`` rather than an empty roster, because
``GET /api/roster`` answers 200 with ``[]`` whenever ``CLAUDE.md`` is missing —
so the outage and the empty success are one ``len(rows) == 0`` apart.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.office.adapters.team_os import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_VIEW_ALLOW_LIST,
    KNOWN_VIEW_NAMES,
    META_PATH,
    ROSTER_PATH,
    TOKEN_HEADER,
    VIEW_PATH_PREFIX,
    PeerResponse,
    TeamOSAdapter,
    TeamOSTimeout,
    TeamOSUnreachable,
)
from aisquare.office.config import OfficeConfig
from aisquare.office.models import Page, TeamOSMeta, TeamOSRosterEntry, TeamOSView

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "office-team-os"
HOME = Path("/tmp/office-home")
TOKEN_VARIABLE = "TEAMOS_TOKEN"
FAKE_TOKEN = "synthetic-not-a-real-token"
ENV: Mapping[str, str] = {TOKEN_VARIABLE: FAKE_TOKEN}
BASE = "http://127.0.0.1:4317"


def body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def ok(name: str) -> PeerResponse:
    return PeerResponse(status_code=200, body=body(name))


class FakeClock:
    """Driven, never slept through. ``now`` and ``monotonic`` move together."""

    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        self._monotonic = 1_000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class FakeTransport:
    """Records every request, and answers by path suffix.

    A stopped peer is an ``exc`` that is raised, not a response: the exchange
    never reaches the HTTP layer, so there is no status code to return.
    """

    def __init__(
        self,
        responses: dict[str, PeerResponse] | None = None,
        *,
        exc: Exception | None = None,
    ) -> None:
        # Held by reference, not copied: several tests change what the peer
        # answers between calls, and a defensive copy here would silently make
        # those tests assert against the first answer forever.
        self.responses: dict[str, PeerResponse] = responses if responses is not None else {}
        self.exc = exc
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []
        self.budgets: list[tuple[float, float, int]] = []
        self.entered = threading.Event()
        self.release: threading.Event | None = None

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        connect_timeout_s: float,
        total_timeout_s: float,
        max_bytes: int,
    ) -> PeerResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        self.budgets.append((connect_timeout_s, total_timeout_s, max_bytes))
        self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        if self.exc is not None:
            raise self.exc
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return response
        return PeerResponse(status_code=404, body=body("error_404_unknown_view.json"))

    @property
    def paths(self) -> list[str]:
        return [url[len(BASE) :] for url in self.urls]


def make(
    transport: FakeTransport, clock: FakeClock | None = None, **kwargs: object
) -> TeamOSAdapter:
    config = OfficeConfig(home=HOME, team_os_enabled=True, team_os_token_env=TOKEN_VARIABLE)
    kwargs.setdefault("env", ENV)
    kwargs.setdefault("cache_ttl_s", 0.0)
    kwargs.setdefault("roster_id_salt", b"deterministic-test-salt")
    return TeamOSAdapter(config, clock or FakeClock(), transport=transport, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------


def test_meta_200_is_typed_metadata_with_a_source_label_and_a_timestamp() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})

    result = make(transport).meta()

    assert result.status == "ok"
    assert isinstance(result.data, TeamOSMeta)
    assert result.data.source == "team_os"
    assert result.data.available is True
    assert result.data.peer_revision == "0000000"
    assert result.data.display_name == "example-branch"
    assert result.observed_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert result.last_success_at == result.observed_at
    assert result.error is None


def test_meta_advertises_the_allow_list_and_not_the_peers_six_names() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})

    result = make(transport).meta()

    assert result.data is not None
    assert result.data.views == DEFAULT_VIEW_ALLOW_LIST
    assert "cockpit" not in result.data.views
    assert "cockpit" in KNOWN_VIEW_NAMES


def test_meta_never_carries_synced_at_engine_or_version() -> None:
    """``git.js:39`` hardcodes ``syncedAt`` null; the schema admits five fields."""
    transport = FakeTransport({META_PATH: ok("meta_200.json")})

    result = make(transport).meta()

    assert result.data is not None
    wire = result.data.to_wire()
    assert set(wire) == {"source", "available", "views", "display_name", "peer_revision"}
    assert "syncedAt" not in wire
    assert "synced_at" not in wire
    assert "engine" not in wire
    assert wire["peer_revision"] != "live"


def test_meta_from_a_non_repository_reads_empty_strings_as_no_value() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200_degenerate.json")})

    result = make(transport).meta()

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.available is True
    assert result.data.display_name is None
    assert result.data.peer_revision is None


def test_a_stopped_peer_is_an_ordinary_unavailable_result() -> None:
    transport = FakeTransport(exc=TeamOSUnreachable())

    result = make(transport).meta()

    assert result.status == "unavailable"
    assert result.stale is False
    assert result.error is not None
    assert result.error.code == "service_unavailable"
    assert result.error.retryable is True
    assert isinstance(result.data, TeamOSMeta)
    assert result.data.available is False
    assert result.data.views == ()


def test_a_stopped_peer_answers_promptly_rather_than_at_the_total_budget() -> None:
    """Loopback refusal is immediate. Waiting out 3 s would be the wrong shape."""
    transport = FakeTransport(exc=TeamOSUnreachable())
    adapter = make(transport)

    started = time.monotonic()
    result = adapter.meta()
    elapsed = time.monotonic() - started

    assert result.status == "unavailable"
    assert elapsed < 1.0, f"a refused connect should not be slept through; took {elapsed:.3f}s"
    connect_budget, total_budget, _ = transport.budgets[0]
    assert connect_budget == DEFAULT_CONNECT_TIMEOUT_S
    assert total_budget == 3.0
    assert connect_budget != total_budget


def test_a_timeout_is_unavailable_and_retryable() -> None:
    transport = FakeTransport(exc=TeamOSTimeout())

    result = make(transport).meta()

    assert result.status == "unavailable"
    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retryable is True


def test_the_total_deadline_is_handed_to_the_transport_for_the_whole_read() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})

    make(transport).meta()

    _, total_budget, max_bytes = transport.budgets[0]
    assert total_budget == 3.0
    assert max_bytes > 0


# --------------------------------------------------------------------------
# roster
# --------------------------------------------------------------------------


def test_roster_rows_stay_peer_rows_in_their_own_identifier_namespace() -> None:
    transport = FakeTransport({ROSTER_PATH: ok("roster_200.json")})

    result = make(transport).roster()

    assert result.status == "ok"
    assert isinstance(result.data, Page)
    assert len(result.data.items) == 2
    first = result.data.items[0]
    assert isinstance(first, TeamOSRosterEntry)
    assert first.source == "team_os"
    assert first.label == "Example Person One"
    assert first.peer_local_id.startswith("peer-")
    ids = {row.peer_local_id for row in result.data.items}
    assert len(ids) == 2, "two distinct rows must not collapse to one peer id"


def test_roster_never_carries_an_email_or_a_slack_id_into_office() -> None:
    """The contract admits ``label`` and ``summary``; neither is an address."""
    transport = FakeTransport({ROSTER_PATH: ok("roster_200.json")})

    result = make(transport).roster()

    assert result.data is not None
    for row in result.data.items:
        wire = row.to_wire()
        assert set(wire) == {"source", "peer_local_id", "label", "summary"}
        assert wire["summary"] is None
        rendered = "".join(str(value) for value in wire.values())
        assert "@" not in rendered
        assert "example.invalid" not in rendered
        assert "U00000" not in rendered


def test_an_empty_roster_is_valid_data_and_not_an_outage() -> None:
    transport = FakeTransport({ROSTER_PATH: ok("roster_200_empty.json")})

    result = make(transport).roster()

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.items == ()
    assert result.error is None
    assert result.observed_at is not None


def test_an_unreachable_peer_is_never_flattened_into_an_empty_roster() -> None:
    transport = FakeTransport(exc=TeamOSUnreachable())

    result = make(transport).roster()

    assert result.status == "unavailable"
    assert result.data is None, "an outage must not look like a roster with no rows"
    assert result.error is not None


def test_roster_tolerates_a_row_whose_slack_id_key_is_absent() -> None:
    """``JSON.stringify`` drops undefined values, so the key is missing, not null."""
    transport = FakeTransport({ROSTER_PATH: ok("roster_200_missing_slack.json")})

    result = make(transport).roster()

    assert result.status == "ok"
    assert result.data is not None
    assert len(result.data.items) == 1
    assert result.data.items[0].label == "Example Person Three"


def test_roster_coverage_reports_only_what_the_peer_established() -> None:
    transport = FakeTransport({ROSTER_PATH: ok("roster_200.json")})

    result = make(transport).roster()

    assert result.data is not None
    assert result.data.next_cursor is None
    assert result.data.partial is False
    coverage = result.data.coverage
    assert coverage is not None
    assert coverage.reported_total == 2
    assert coverage.failed_scope_count == 0
    assert coverage.reachable_scope_count == 1


def test_a_roster_that_is_not_a_list_is_a_typed_failure() -> None:
    transport = FakeTransport({ROSTER_PATH: PeerResponse(200, b'{"roster": "nope"}')})

    result = make(transport).roster()

    assert result.status == "unavailable"
    assert result.data is None
    assert result.error is not None
    assert result.error.code == "upstream_invalid"


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------


def test_an_allow_listed_view_normalises_and_keeps_its_named_provenance() -> None:
    transport = FakeTransport({VIEW_PATH_PREFIX + "board": ok("view_board_200.json")})

    result = make(transport).view("board")

    assert result.status == "ok"
    assert isinstance(result.data, TeamOSView)
    assert result.data.source == "team_os"
    assert result.data.name == "board"
    assert result.data.title == "Board"
    assert [section.title for section in result.data.sections] == ["Stats", "Items"]
    assert "EX-001" in result.data.sections[1].text
    assert transport.paths == [VIEW_PATH_PREFIX + "board"]


def test_board_and_team_pending_are_two_names_and_are_never_canonicalised() -> None:
    """One builder (``snapshots.js:138``); the payload echoes what was asked."""
    transport = FakeTransport(
        {
            VIEW_PATH_PREFIX + "board": ok("view_board_200.json"),
            VIEW_PATH_PREFIX + "team-pending": ok("view_team_pending_200.json"),
        }
    )
    adapter = make(transport)

    board = adapter.view("board")
    pending = adapter.view("team-pending")

    assert board.data is not None
    assert pending.data is not None
    assert board.data.name == "board"
    assert pending.data.name == "team-pending"
    assert transport.paths == [
        VIEW_PATH_PREFIX + "board",
        VIEW_PATH_PREFIX + "team-pending",
    ]


def test_a_view_body_echoing_a_different_name_is_refused() -> None:
    transport = FakeTransport({VIEW_PATH_PREFIX + "board": ok("view_team_pending_200.json")})

    result = make(transport).view("board")

    assert result.status == "unavailable"
    assert result.data is None
    assert result.error is not None
    assert result.error.code == "upstream_invalid"


def test_roadmap_keeps_all_four_buckets_even_when_a_bucket_is_empty() -> None:
    transport = FakeTransport({VIEW_PATH_PREFIX + "roadmap": ok("view_roadmap_200.json")})

    result = make(transport).view("roadmap")

    assert result.data is not None
    assert [section.title for section in result.data.sections] == [
        "Now",
        "Next",
        "Later",
        "Recently shipped",
    ]
    assert result.data.sections[0].text == "Example in-flight item"


def test_customers_renders_the_peers_own_dynamic_header_keys() -> None:
    transport = FakeTransport({VIEW_PATH_PREFIX + "customers": ok("view_customers_200.json")})

    result = make(transport).view("customers")

    assert result.data is not None
    directory = result.data.sections[0]
    assert directory.title == "Directory"
    assert "domain(s): example.invalid" in directory.text
    assert "known contacts: Example Contact" in directory.text


def test_a_calendar_event_without_a_status_key_still_decodes() -> None:
    """The hardcoded release milestone omits ``status`` (``snapshots.js:201``)."""
    transport = FakeTransport({VIEW_PATH_PREFIX + "calendar": ok("view_calendar_200.json")})

    result = make(transport).view("calendar")

    assert result.status == "ok"
    assert result.data is not None
    events = result.data.sections[1].text.splitlines()
    assert len(events) == 2
    assert events[0].endswith("(open)")
    assert not events[1].endswith(")")
    assert "2026-06-30" in events[1]


def test_a_board_row_with_a_null_age_and_an_empty_account_still_decodes() -> None:
    transport = FakeTransport({VIEW_PATH_PREFIX + "board": ok("view_board_200.json")})

    result = make(transport).view("board")

    assert result.status == "ok"
    assert result.data is not None
    rows = result.data.sections[1].text.splitlines()
    assert len(rows) == 2
    assert "EX-002" in rows[1]


def test_view_text_is_bounded_however_long_the_peers_notes_are() -> None:
    huge = "x" * 40_000
    payload = (
        '{"view": "board", "generatedAt": "2026-01-01T00:00:00.000Z",'
        '"stats": {"open": 1}, "items": ['
        + ",".join(
            f'{{"id": "EX-{index:03d}", "owner": "o", "item": "i", "notes": "{huge}"}}'
            for index in range(10)
        )
        + "]}"
    )
    transport = FakeTransport({VIEW_PATH_PREFIX + "board": PeerResponse(200, payload.encode())})

    result = make(transport).view("board")

    assert result.status == "ok"
    assert result.data is not None
    for section in result.data.sections:
        assert len(section.text) <= 8000


def test_a_name_outside_the_allow_list_sends_no_request_at_all() -> None:
    transport = FakeTransport()

    result = make(transport).view("graph")

    assert result.status == "unsupported"
    assert result.data is None
    assert result.error is not None
    assert result.error.code == "unsupported_capability"
    assert result.error.retryable is False
    assert transport.urls == [], "an unknown view must never reach the peer"


def test_a_path_shaped_view_name_is_refused_without_a_request() -> None:
    """The peer takes everything after ``/api/views/`` as the name."""
    transport = FakeTransport()

    result = make(transport).view("../../etc/passwd")

    assert result.status == "unsupported"
    assert transport.urls == []


def test_cockpit_is_off_by_default_and_enabling_it_is_an_explicit_choice() -> None:
    transport = FakeTransport({VIEW_PATH_PREFIX + "cockpit": ok("view_cockpit_200.json")})

    default_result = make(transport).view("cockpit")
    assert default_result.status == "unsupported"
    assert transport.urls == []

    enabled = make(transport, allowed_views=("cockpit",)).view("cockpit")
    assert enabled.status == "ok"
    assert enabled.data is not None
    assert [section.title for section in enabled.data.sections] == [
        "Stats",
        "Overdue",
        "I owe",
        "Waiting on",
    ]


def test_a_ui_tab_name_cannot_be_configured_as_a_view() -> None:
    """``graph``, ``files``, ``chat``, ``open-loops`` and ``waiting-on`` are tabs."""
    config = OfficeConfig(home=HOME, team_os_enabled=True, team_os_token_env=TOKEN_VARIABLE)

    with pytest.raises(ValueError, match="not Team OS view names"):
        TeamOSAdapter(config, FakeClock(), transport=FakeTransport(), allowed_views=("graph",))


# --------------------------------------------------------------------------
# authentication and the gates in front of it
# --------------------------------------------------------------------------


def test_401_is_unauthorized_with_no_data_and_no_token_in_the_error() -> None:
    transport = FakeTransport({META_PATH: PeerResponse(401, body("error_401.json"))})

    result = make(transport).meta()

    assert result.status == "unauthorized"
    assert result.data is None, "losing authorisation drops the body, it does not paint it"
    assert result.stale is False
    assert result.error is not None
    assert result.error.code == "unauthorized"
    assert FAKE_TOKEN not in result.error.detail
    assert "missing/invalid token" not in result.error.detail


def test_403_is_forbidden_and_not_retryable_because_it_is_a_client_bug() -> None:
    transport = FakeTransport({META_PATH: PeerResponse(403, body("error_403.json"))})

    result = make(transport).meta()

    assert result.status == "forbidden"
    assert result.data is None
    assert result.error is not None
    assert result.error.code == "forbidden"
    assert result.error.retryable is False


def test_404_is_unsupported_and_never_an_empty_success() -> None:
    transport = FakeTransport(
        {VIEW_PATH_PREFIX + "roadmap": PeerResponse(404, body("error_404_unknown_view.json"))}
    )

    result = make(transport).view("roadmap")

    assert result.status == "unsupported"
    assert result.data is None
    assert result.error is not None
    assert result.error.retryable is False
    assert "example-unknown" not in result.error.detail, "peer error text reflects caller input"


def test_the_token_travels_only_as_a_header_and_never_in_the_url() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})

    make(transport).meta()

    assert transport.headers[0][TOKEN_HEADER] == FAKE_TOKEN
    assert FAKE_TOKEN not in transport.urls[0]
    assert "token=" not in transport.urls[0]


def test_no_origin_header_is_sent_because_the_peer_only_checks_a_present_one() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})

    make(transport).meta()

    sent = {name.lower() for name in transport.headers[0]}
    assert "origin" not in sent
    assert "authorization" not in sent


def test_a_disabled_peer_is_unconfigured_and_sends_no_request() -> None:
    config = OfficeConfig(home=HOME, team_os_enabled=False, team_os_token_env=TOKEN_VARIABLE)
    transport = FakeTransport()

    result = TeamOSAdapter(config, FakeClock(), transport=transport, env=ENV).meta()

    assert result.status == "unconfigured"
    assert result.data is None
    assert transport.urls == []


def test_a_missing_token_value_is_unconfigured_rather_than_an_unauthenticated_call() -> None:
    transport = FakeTransport()

    result = make(transport, env={}).meta()

    assert result.status == "unconfigured"
    assert result.error is not None
    assert result.error.code == "service_unconfigured"
    assert transport.urls == []


# --------------------------------------------------------------------------
# malformed bodies and bounds
# --------------------------------------------------------------------------


def test_a_body_that_is_not_json_is_a_typed_failure_and_not_a_crash() -> None:
    transport = FakeTransport({META_PATH: PeerResponse(200, b"<html>not json</html>")})

    result = make(transport).meta()

    assert result.status == "unavailable"
    assert result.error is not None
    assert result.error.code == "upstream_invalid"
    assert result.error.retryable is False


def test_a_json_body_that_is_not_an_object_is_refused() -> None:
    transport = FakeTransport({META_PATH: PeerResponse(200, b"[1, 2, 3]")})

    result = make(transport).meta()

    assert result.status == "unavailable"
    assert result.error is not None
    assert result.error.code == "upstream_invalid"


def test_an_oversized_body_is_refused_before_it_is_decoded() -> None:
    oversized = b'{"roster": []}' + b" " * 4096
    transport = FakeTransport({ROSTER_PATH: PeerResponse(200, oversized)})

    result = make(transport, max_body_bytes=64).roster()

    assert result.status == "unavailable"
    assert result.data is None
    assert result.error is not None
    assert result.error.code == "upstream_invalid"


def test_an_unexpected_transport_exception_never_escapes_as_an_office_crash() -> None:
    transport = FakeTransport(exc=RuntimeError("peer said /home/someone/secret.md"))

    result = make(transport).meta()

    assert result.status == "unavailable"
    assert result.error is not None
    assert result.error.code == "internal"
    assert "/home/" not in result.error.detail


# --------------------------------------------------------------------------
# cache, invalidation and concurrency
# --------------------------------------------------------------------------


def test_two_callers_arriving_together_make_one_bounded_request() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})
    transport.release = threading.Event()
    adapter = make(transport, cache_ttl_s=30.0)
    results: list[str] = []

    def call() -> None:
        results.append(adapter.meta().status)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    transport.entered.wait(timeout=5)
    transport.release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert results == ["ok", "ok"]
    assert len(transport.urls) == 1, "a second caller must not open a second request"


def test_a_repeat_inside_the_window_is_served_from_cache_and_marked_stale() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})
    clock = FakeClock()
    adapter = make(transport, clock, cache_ttl_s=30.0)

    first = adapter.meta()
    clock.advance(1.0)
    second = adapter.meta()

    assert len(transport.urls) == 1
    assert first.stale is False
    assert second.stale is True
    assert second.status == "ok"
    assert second.observed_at == first.observed_at, "a cached record keeps its own time"


def test_a_changed_token_invalidates_the_cache_and_forces_a_fresh_read() -> None:
    transport = FakeTransport({META_PATH: ok("meta_200.json")})
    env = dict(ENV)
    adapter = make(transport, env=env, cache_ttl_s=30.0)

    adapter.meta()
    env[TOKEN_VARIABLE] = "a-different-synthetic-token"
    adapter.meta()

    assert len(transport.urls) == 2, "a rotated token must not reuse a previous boot's record"
    assert transport.headers[1][TOKEN_HEADER] == "a-different-synthetic-token"


def test_losing_authorisation_drops_the_cached_body() -> None:
    responses = {META_PATH: ok("meta_200.json")}
    transport = FakeTransport(responses)
    clock = FakeClock()
    adapter = make(transport, clock, cache_ttl_s=30.0, stale_max_age_s=3600.0)

    first = adapter.meta()
    assert first.status == "ok"

    responses[META_PATH] = PeerResponse(401, body("error_401.json"))
    clock.advance(60.0)
    denied = adapter.meta()
    assert denied.status == "unauthorized"
    assert denied.data is None

    responses[META_PATH] = PeerResponse(500, b'{"error": "boom"}')
    clock.advance(1.0)
    after = adapter.meta()
    assert after.status == "unavailable"
    assert after.stale is False, "the dropped body must not come back as stale data"


def test_stale_serving_is_off_by_default() -> None:
    responses = {ROSTER_PATH: ok("roster_200.json")}
    transport = FakeTransport(responses)
    clock = FakeClock()
    adapter = make(transport, clock, cache_ttl_s=30.0)

    adapter.roster()
    responses.clear()
    transport.exc = TeamOSUnreachable()
    clock.advance(60.0)
    result = adapter.roster()

    assert result.status == "unavailable"
    assert result.data is None
    assert result.stale is False


def test_opted_in_stale_data_is_marked_and_keeps_its_own_observation_time() -> None:
    responses = {ROSTER_PATH: ok("roster_200.json")}
    transport = FakeTransport(responses)
    clock = FakeClock()
    adapter = make(transport, clock, cache_ttl_s=0.0, stale_max_age_s=300.0)

    first = adapter.roster()
    transport.exc = TeamOSUnreachable()
    clock.advance(60.0)
    served = adapter.roster()

    assert served.status == "partial"
    assert served.stale is True
    assert served.data is not None
    assert served.observed_at == first.observed_at
    assert served.error is not None
    assert served.error.code == "service_unavailable"


def test_stale_data_expires_at_the_configured_bound() -> None:
    responses = {ROSTER_PATH: ok("roster_200.json")}
    transport = FakeTransport(responses)
    clock = FakeClock()
    adapter = make(transport, clock, cache_ttl_s=0.0, stale_max_age_s=300.0)

    adapter.roster()
    transport.exc = TeamOSUnreachable()
    clock.advance(301.0)
    expired = adapter.roster()

    assert expired.status == "unavailable"
    assert expired.data is None
    assert expired.stale is False


# --------------------------------------------------------------------------
# the closed surface
# --------------------------------------------------------------------------


def test_the_adapter_only_ever_requests_the_three_allowed_endpoints() -> None:
    transport = FakeTransport(
        {
            META_PATH: ok("meta_200.json"),
            ROSTER_PATH: ok("roster_200.json"),
            VIEW_PATH_PREFIX + "board": ok("view_board_200.json"),
            VIEW_PATH_PREFIX + "team-pending": ok("view_team_pending_200.json"),
            VIEW_PATH_PREFIX + "calendar": ok("view_calendar_200.json"),
            VIEW_PATH_PREFIX + "customers": ok("view_customers_200.json"),
            VIEW_PATH_PREFIX + "roadmap": ok("view_roadmap_200.json"),
        }
    )
    adapter = make(transport)

    adapter.meta()
    adapter.roster()
    for name in DEFAULT_VIEW_ALLOW_LIST:
        adapter.view(name)

    expected = [META_PATH, ROSTER_PATH] + [
        VIEW_PATH_PREFIX + name for name in DEFAULT_VIEW_ALLOW_LIST
    ]
    assert transport.paths == expected
    forbidden = (
        "/api/commands",
        "/api/run",
        "/api/chat",
        "/api/setup",
        "/api/propose",
        "/api/sync",
        "/api/file",
        "/api/account",
        "/api/graph",
        "/api/identity",
    )
    for path in forbidden:
        assert not any(url.startswith(BASE + path) for url in transport.urls)


def test_every_allow_listed_name_is_a_name_the_peers_builder_answers() -> None:
    assert set(DEFAULT_VIEW_ALLOW_LIST) <= set(KNOWN_VIEW_NAMES)
    assert set(KNOWN_VIEW_NAMES) - set(DEFAULT_VIEW_ALLOW_LIST) == {"cockpit"}
    assert len(KNOWN_VIEW_NAMES) == 6
