"""``GET /v1/me``: the run a signed-in developer asks against.

The descriptor said *how* to deliver for a run and never *which* run — that was
an environment variable a controller handed out per cohort. These tests cover
the client half of closing that gap: the fetch and its bounds, the two caches,
and the precedence that keeps the harness working unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.core import workspace as workspace_core
from aisquare.core.config import AppConfig, save_config
from aisquare.models import ClientReason
from aisquare.services import ci_augment, ci_client, ci_me
from aisquare.services.ci_contract import MeDocument
from tests.ci_schemas import fixture, fixture_text
from tests.ci_support import RUN, wire
from tests.stub_ci_server import StubCI, serve

KEY = "k"


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


def _me(stub: StubCI, key: str = KEY) -> ci_me.MeResult:
    return ci_me.current(base=stub.url, key=key)


def _project() -> str:
    return workspace_core.current_project().id


def _bind(workspace: str) -> None:
    """Bind THIS checkout to a workspace, the way `aisquare ci bind-workspace` does."""
    config = AppConfig()
    config.experiment.enabled = True
    config.experiment.bindings[_project()] = workspace
    save_config(config)
    ci_client.reset_cache()


# --- the document ------------------------------------------------------------


def test_the_servers_own_answer_is_understood(stub: StubCI, isolated_home: Path) -> None:
    result = _me(stub)

    assert result.reason is ClientReason.none
    assert result.me is not None
    assert result.me.principal_id == fixture("me.v1.valid")["principal_id"]
    assert [m.workspace_id for m in result.me.workspaces] == [
        "ws_kernel01",
        "ws_9a8b7c6d5e4f30211203948576abcdef",
    ]


def test_an_arm_shaped_field_is_refused_like_it_is_on_the_descriptor(
    stub: StubCI, isolated_home: Path
) -> None:
    """The blinding argument rests on the descriptor being the only run document
    that can say anything about a configuration. This is the second document the
    client fetches, so it is closed for the same reason."""
    stub.me_body = fixture_text("me.v1.invalid")

    result = _me(stub)

    assert result.me is None
    assert "arm_kind" in result.detail


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("not json at all", "me is not JSON"),
        ("[]", "me is list"),
        ('{"contract_version": 2}', "contract_version 2"),
    ],
)
def test_every_malformed_answer_gets_its_own_detail(
    stub: StubCI, isolated_home: Path, body: str, expected: str
) -> None:
    """A distinct detail per failure, because "unavailable" with no reason is
    what makes an operator guess."""
    stub.me_body = body

    result = _me(stub)

    assert result.me is None
    assert expected in result.detail


def test_a_401_says_the_token_was_rejected(stub: StubCI, isolated_home: Path) -> None:
    stub.me_status = 401

    result = _me(stub)

    assert result.me is None and result.status == 401
    assert "token rejected (401)" in result.detail


# --- the two caches ----------------------------------------------------------


def test_a_second_call_does_not_reach_the_wire(stub: StubCI, isolated_home: Path) -> None:
    """This call sits in front of the descriptor fetch on the session-start
    path, so "cached" has to mean no round trip and not merely no parse."""
    assert _me(stub).me is not None
    assert stub.me_fetches == 1

    again = _me(stub)

    assert again.me is not None and again.from_cache
    assert stub.me_fetches == 1, "the second call went to the server"


def test_a_refusal_is_cached_so_a_dead_server_costs_one_probe(
    stub: StubCI, isolated_home: Path
) -> None:
    """Without this, every prompt pays a fresh probe — the defect the descriptor
    fetch had before its own refusal cache."""
    stub.me_status = 500
    assert _me(stub).me is None
    assert stub.me_fetches == 1

    again = _me(stub)

    assert again.me is None and again.from_cache
    assert stub.me_fetches == 1


def test_the_cache_is_keyed_by_bearer_not_by_user(stub: StubCI, isolated_home: Path) -> None:
    """Signing in as somebody else must not serve the previous routing."""
    assert _me(stub, "first-token").me is not None
    assert stub.me_fetches == 1

    assert _me(stub, "second-token").me is not None

    assert stub.me_fetches == 2, "a different bearer must start cold"


def test_an_answer_disproves_a_cached_refusal(stub: StubCI, isolated_home: Path) -> None:
    """`doctor` fetches uncached. A refusal it has just disproved has to go, or
    the diagnostic prints a healthy identity while every hook reads the stale
    negative for the rest of the window."""
    stub.me_status = 503
    assert _me(stub).me is None
    assert ci_me._refusal_path(KEY).exists()

    stub.me_status = 200
    fresh = ci_me.fetch(base=stub.url, key=KEY, cache=False)

    assert fresh.me is not None
    assert not ci_me._refusal_path(KEY).exists()
    assert not ci_me._cache_path(KEY).exists(), "cache=False still leaves no document behind"


def test_an_expired_cache_is_refetched(stub: StubCI, isolated_home: Path) -> None:
    assert _me(stub).me is not None

    later = datetime.now(tz=UTC) + timedelta(seconds=ci_me.CACHE_TTL_SECONDS + 1)
    result = ci_me.current(base=stub.url, key=KEY, now=later)

    assert result.me is not None and not result.from_cache
    assert stub.me_fetches == 2


def test_a_damaged_cache_file_is_a_refetch_not_a_crash(stub: StubCI, isolated_home: Path) -> None:
    assert _me(stub).me is not None
    paths.ci_me_path(ci_me._key_digest(KEY)).write_text("{ not json", encoding="utf-8")

    assert _me(stub).me is not None
    assert stub.me_fetches == 2


def test_a_refusal_against_one_endpoint_does_not_silence_another(
    stub: StubCI, isolated_home: Path
) -> None:
    stub.me_status = 401
    assert _me(stub).me is None

    stub.me_status = 200
    other = stub.url.replace("127.0.0.1", "localhost")

    result = ci_me.current(base=other, key=KEY)

    assert result.me is not None and not result.from_cache


# --- which run ---------------------------------------------------------------


def _doc(**changes: object) -> MeDocument:
    raw = fixture("me.v1.valid")
    raw.update(changes)
    return MeDocument.model_validate(raw)


def test_the_bound_workspaces_run_is_the_one_used() -> None:
    choice = ci_me.run_for(_doc(), "ws_kernel01")

    assert choice.run_id == "run_kernel0001"
    assert choice.reason is ci_me.RunReason.resolved
    assert "ws_kernel01" in choice.detail


def test_a_workspace_with_no_run_is_a_reason_not_a_run() -> None:
    choice = ci_me.run_for(_doc(), "ws_9a8b7c6d5e4f30211203948576abcdef")

    assert choice.run_id is None
    assert choice.reason is ci_me.RunReason.no_run
    assert "no run published" in choice.detail


def test_several_workspaces_and_none_bound_refuses_to_guess() -> None:
    """Guessing would bind a project to whichever the server listed first, and
    every row afterwards would name the wrong tenant."""
    choice = ci_me.run_for(_doc(), None)

    assert choice.run_id is None
    assert choice.reason is ci_me.RunReason.unbound
    assert "none is bound" in choice.detail and "bind-workspace" in choice.detail


def test_a_single_workspace_needs_no_binding() -> None:
    """No choice to make, so asking the developer to configure one is ceremony."""
    only = fixture("me.v1.valid")
    only["workspaces"] = only["workspaces"][:1]

    choice = ci_me.run_for(MeDocument.model_validate(only), None)

    assert choice.run_id == "run_kernel0001"


def test_membership_of_nothing_is_signed_in_with_nowhere_to_ask() -> None:
    choice = ci_me.run_for(_doc(workspaces=[]), None)

    assert choice.run_id is None
    assert choice.reason is ci_me.RunReason.no_workspaces
    assert "member of no workspace" in choice.detail


def test_a_workspace_the_user_is_not_in_says_so() -> None:
    choice = ci_me.run_for(_doc(), "ws_somebody_elses")

    assert choice.run_id is None
    assert choice.reason is ci_me.RunReason.not_a_member
    assert "not a member of ws_somebody_elses" in choice.detail


# --- the gate ----------------------------------------------------------------


def test_an_exported_run_still_wins_and_costs_no_me_call(
    stub: StubCI, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness, the joint smoke and every CITEST_* identity name a run from
    one shell variable. A signed-in fallback that could override it would make
    the harness depend on whoever happened to be logged in."""
    wire(monkeypatch, stub)

    opened = ci_augment.gate(_project())

    assert opened.open and opened.run_id == RUN
    assert stub.me_fetches == 0, "GET /v1/me must not be asked when the run is exported"


def test_with_no_exported_run_the_gate_asks_who_it_is(
    stub: StubCI, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire(monkeypatch, stub)
    monkeypatch.delenv(ci_client.RUN_ENV_VAR, raising=False)
    _bind("ws_kernel01")

    opened = ci_augment.gate(_project())

    assert stub.me_fetches == 1
    assert opened.run_id == "run_kernel0001"
    assert opened.open, opened.detail


def test_no_bearer_means_no_run_and_no_round_trip(
    stub: StubCI, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a bearer the honest answer is still "no run", and asking would
    spend a session-start round trip to be told 401."""
    wire(monkeypatch, stub)
    monkeypatch.delenv(ci_client.RUN_ENV_VAR, raising=False)
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)

    opened = ci_augment.gate(_project())

    assert not opened.open and opened.reason is ClientReason.no_run
    assert "no bearer" in opened.detail
    assert stub.me_fetches == 0


def test_a_refused_me_is_no_run_with_the_servers_reason(
    stub: StubCI, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire(monkeypatch, stub)
    monkeypatch.delenv(ci_client.RUN_ENV_VAR, raising=False)
    stub.me_status = 401

    opened = ci_augment.gate(_project())

    assert not opened.open and opened.reason is ClientReason.no_run
    assert "token rejected (401)" in opened.detail


def test_the_answer_is_a_valid_me_v1_by_the_servers_own_schema(
    stub: StubCI, isolated_home: Path
) -> None:
    """Validated against the vendored schema rather than a second reading of it."""
    from tests.ci_schemas import assert_valid

    assert_valid("me.v1", json.loads(stub.me_body))
    assert _me(stub).me is not None


# --- the cache answers for one server only ----------------------------------


def test_a_document_from_one_server_is_not_served_for_another(
    stub: StubCI, isolated_home: Path
) -> None:
    """Repointing AISQUARE_CI_URL within the TTL must not route every hook to the
    previous server's run: the refusal cache already refused to cross servers,
    and the document it protects carries the ids that routing depends on."""
    assert _me(stub).me is not None
    other = stub.url.replace("127.0.0.1", "localhost")

    result = ci_me.current(base=other, key=KEY)

    assert result.me is not None and not result.from_cache
    assert stub.me_fetches == 2, "the other server must be asked, not answered from A's cache"


def test_a_cache_file_with_a_naive_expiry_is_a_miss_not_a_crash(
    stub: StubCI, isolated_home: Path
) -> None:
    """`fromisoformat` accepts an offset-less stamp and returns a naive datetime;
    comparing it with an aware `now` raises TypeError. That comparison used to
    sit outside the try, on the synchronous hook path, in a function documented
    never to raise."""
    assert _me(stub).me is not None
    path = ci_me._cache_path(KEY)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["until"] = "2999-01-01T00:00:00"
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert _me(stub).me is not None
    assert stub.me_fetches == 2

    refusal = ci_me._refusal_path(KEY)
    refusal.write_text(
        json.dumps({"detail": "x", "until": "2999-01-01T00:00:00", "endpoint": stub.url}),
        encoding="utf-8",
    )
    assert ci_me._read_refusal(KEY, datetime.now(tz=UTC), stub.url) is None


def test_bindings_are_per_project_not_per_machine(isolated_home: Path, tmp_path: Path) -> None:
    """Binding repo A must leave repo B alone: the docstring's own promise."""
    a = workspace_core.current_project(tmp_path / "a").id
    b = workspace_core.current_project(tmp_path / "b").id
    config = AppConfig()
    config.experiment.bindings[a] = "ws_team"
    save_config(config)
    ci_client.reset_cache()

    assert ci_client.workspace_id(a) == "ws_team"
    assert ci_client.workspace_id(b) == ""
    assert ci_client.workspace_id(None) == ""
