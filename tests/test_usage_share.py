"""Opt-in anonymous usage sharing: the switch, the batch, the shipment, the privacy of the body.

No test reaches a server: the shipper's transport is a fake (``tests/usage_share_support.Server``),
and the AISquare-Studio-BE endpoint it stands for is a separate PR. The suite forces sharing off
for every test (``AISQUARE_NO_TELEMETRY``); these take that off with ``sharing_allowed``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import feature_use, locking, paths, sharing
from aisquare.core import state_file as state_core
from aisquare.core.store import store_session
from aisquare.models import DailyRollup, DayAccounts, DayTokens, TokenCounts
from aisquare.services import iam, usage_share, usage_stats
from aisquare.services import settings as settings_service
from tests.projects import add_project
from tests.usage_share_support import Server, check_contract, ok, status, unreachable
from tests.usage_stats_support import IST, NOW, Machine, pin_clock, prompt, request, tokens

pytestmark = pytest.mark.usefixtures("sharing_allowed")


@pytest.fixture(autouse=True)
def machine_clock_is_ist() -> Iterator[None]:
    """The machine's own time zone is the one the data is cut in (``IST``, as the stats tests'):
    the sharing path takes the first day shared from the machine's local date, and a test that
    mixed that with IST days passed only where the two dates agree (it failed on a UTC runner
    once the real date had moved on from the pinned ``NOW``, and at UTC-11)."""
    if not hasattr(time, "tzset"):  # Windows: the zone of the runner stays
        yield
        return
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


SECRET_REMOTE = "git@github.com:Acme-Secret/hush-project.git"
SECRET_NAME = "hush-project"
SECRET_NICKNAME = "Hush Nickname"
SECRET_EMAIL = "carla@example.com"
SECRET_ALIAS = "carla-work"
SECRET_ARGUMENT = "do-not-send-this-argument"
SECRET_ROLE = "hush-custom-role"
SECRET_ORG = "Acme-Secret"


def _day(offset: int, hour: int = 10):  # type: ignore[no-untyped-def]
    return NOW.replace(hour=hour, minute=0) + timedelta(days=offset)


def _history(m: Machine) -> Path:
    """Three days of work in a repository whose every name is a secret, on two accounts, with
    fleet activity and a command typed with an argument: everything a body must not carry."""
    repo = m.repo(SECRET_NAME, remote=SECRET_REMOTE)
    project = add_project(repo)
    m.account(1, "team")
    managed = m.account(2, "max")
    (managed / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": SECRET_EMAIL, "organizationName": "Acme"}}),
        encoding="utf-8",
    )
    for offset in (0, -1, -2):
        for slot in (1, 2):
            m.session(
                slot,
                repo,
                f"s{slot}{-offset}",
                prompt(_day(offset, 9), cwd=repo),
                request(_day(offset, 9) + timedelta(minutes=1), f"m{slot}{-offset}", cwd=repo,
                        cache_read=1000 * (slot + 3 * -offset + 1)),
            )  # fmt: skip
    usage_stats.sweep(tz=IST)
    with store_session() as store:
        store.set_project_nickname(project.id, SECRET_NICKNAME)
        store.upsert_claude_account(2, managed)
        store.set_claude_account_alias(2, SECRET_ALIAS)
        store.add_feature_uses(
            {("2026-10-26", "cli.fleet.spawn"): 3, ("2026-10-25", "tui.launch"): 1}
        )
        conn = store._conn  # type: ignore[attr-defined]
        for number, role in enumerate(("coder", "coder", "tester", SECRET_ROLE)):
            conn.execute(
                "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at) "
                "VALUES (?, ?, ?, ?, '%1', ?, ?)",
                (f"a{number}", project.id, f"label-{number}", role, str(repo),
                 _day(0, 11).astimezone(UTC).isoformat()),
            )  # fmt: skip
        for number in range(2):
            conn.execute(
                "INSERT INTO team_event (id, project_id, kind, text, created_at) "
                "VALUES (?, ?, 'task_done', ?, ?)",
                (
                    f"e{number}",
                    project.id,
                    f"task {SECRET_ARGUMENT}",
                    _day(0, 12).astimezone(UTC).isoformat(),
                ),
            )
        conn.commit()
    feature_use.record("cli.task.show", now=_day(0, 9))  # as `aisquare task show <argument>` does
    return repo


def _build(**kwargs: Any) -> usage_share.Batch:
    return usage_share.build_batch(now=NOW, tz=IST, **kwargs)


def _on(days_back: int = 10) -> None:
    """Turn sharing on as of ``days_back`` days before NOW (days since then are shared)."""
    usage_share.enable(now=NOW - timedelta(days=days_back))


# --- off, and forced off ----------------------------------------------------------------------


def test_nothing_is_sent_or_kept_while_sharing_is_off(machine: Machine) -> None:
    _history(machine)
    server = Server()

    report = usage_share.ship(now=NOW, tz=IST, transport=server, force=True)
    spawned = sharing.start_background_ship(NOW)

    assert report.skipped == "sharing is off" and not report.sent and server.calls == []
    assert spawned is False
    assert state_core.read_state().get(sharing.STATE_KEY) is None  # not even an id was minted
    assert usage_share.status().enabled is False and usage_share.status().install_id is None


def test_building_a_preview_stores_nothing_and_uses_placeholders_until_sharing_was_on(
    machine: Machine,
) -> None:
    _history(machine)

    batch = _build()

    check_contract(batch.body)
    assert batch.body["install_id"] == usage_share.PLACEHOLDER_ID
    assert state_core.read_state().get(sharing.STATE_KEY) is None


@pytest.mark.parametrize(
    ("name", "value", "why"),
    [
        ("DO_NOT_TRACK", "1", "DO_NOT_TRACK is set"),
        ("AISQUARE_NO_TELEMETRY", "1", "AISQUARE_NO_TELEMETRY is set"),
        ("CI", "true", "CI is set"),
    ],
)
def test_the_environment_forces_sharing_off_and_the_prompt_away(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, name: str, value: str, why: str
) -> None:
    _history(machine)
    assert usage_share.consent_due() is True  # the question is due until something forbids it
    server = Server()
    monkeypatch.setenv(name, value)

    with pytest.raises(usage_share.SharingForcedOff, match=why):
        usage_share.enable(now=NOW)
    report = usage_share.ship(now=NOW, tz=IST, transport=server, force=True)

    assert usage_share.consent_due() is False
    assert report.skipped is not None and why in report.skipped and server.calls == []
    status = usage_share.status()
    assert status.enabled is False and status.forced_off is not None and why in status.forced_off
    assert state_core.read_state().get(sharing.STATE_KEY) is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_a_variable_that_says_no_does_not_force_anything(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    for name in ("DO_NOT_TRACK", "AISQUARE_NO_TELEMETRY", "CI"):
        monkeypatch.setenv(name, value)
    assert sharing.forced_off_reason() is None


def test_a_switch_turned_on_in_the_config_is_still_forced_off_by_the_environment(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    server = Server()
    monkeypatch.setenv("CI", "true")

    report = usage_share.ship(now=NOW, tz=IST, transport=server, force=True)

    assert sharing.switch_on() is True and sharing.enabled() is False
    assert server.calls == [] and report.skipped is not None
    assert sharing.start_background_ship(NOW) is False


# --- the identity and the hashes --------------------------------------------------------------


def test_the_install_id_is_minted_at_the_first_opt_in_and_kept_across_off_and_on(
    machine: Machine,
) -> None:
    assert usage_share.status().install_id is None
    _on()
    first = usage_share.status().install_id
    usage_share.disable(now=NOW)
    usage_share.enable(now=NOW)
    again = usage_share.status().install_id
    changed = usage_share.reset_identity(now=NOW)[0]

    assert first is not None and again == first
    assert changed != first and usage_share.status().install_id == changed


def test_a_repository_hash_is_stable_for_an_install_and_different_for_another(
    machine: Machine,
) -> None:
    _history(machine)
    _on()
    one = _build().days[-1]["repos"][0]["repo_hash"]
    again = _build().days[-1]["repos"][0]["repo_hash"]
    usage_share.reset_identity(now=NOW - timedelta(days=10))  # another installation: its own salt
    other = _build().days[-1]["repos"][0]["repo_hash"]

    assert one == again and one != other
    salt = "ab" * 32
    assert usage_share.repo_hash(salt, "github.com/org/repo") == usage_share.repo_hash(
        salt, "github.com/org/repo"
    )
    assert usage_share.repo_hash(salt, "github.com/org/repo") != usage_share.repo_hash(
        "cd" * 32, "github.com/org/repo"
    )
    assert len(usage_share.repo_hash(salt, "x")) == 16


# --- the body ---------------------------------------------------------------------------------


def test_the_body_is_the_wire_contracts_and_its_days_are_the_dashboards(machine: Machine) -> None:
    _history(machine)
    _on()

    body = _build().body
    check_contract(body)
    days = {day["date"]: day for day in body["days"]}
    stats_days = {d.date: d for d in usage_stats.stats("7d", now=NOW, tz=IST).days}

    assert list(days) == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert days["2026-10-26"]["tz_offset_minutes"] == 330
    for date, day in days.items():
        assert day["tokens"]["total"] == stats_days[date].tokens.total  # == metrics tokens
        assert day["sessions"] == 2 and day["signed_in"] is False
    today = days["2026-10-26"]
    assert today["features"] == {"cli.fleet.spawn": 3, "cli.task.show": 1}
    assert today["fleet"] == {
        "agents_spawned": {"coder": 2, "other": 1, "tester": 1},
        "tasks_done": 2,
    }
    assert today["accounts"]["by_type"] == {"max": 1, "team": 1}
    assert today["models"] == {
        "claude-opus-5-5": {
            k: today["tokens"][k] for k in ("input", "cache_write", "cache_read", "output")
        }
    }
    (repo,) = today["repos"]
    assert repo["traits"]["host"] == "github" and repo["traits"]["language"] == "unknown"


def test_the_body_carries_no_name_path_remote_email_alias_or_argument(machine: Machine) -> None:
    repo = _history(machine)
    _on()
    with store_session() as store:
        profile = next(iter(store.repo_profiles().values()))
    assert profile.remote == "github.com/acme-secret/hush-project"

    wire = json.dumps(_build().body)

    for secret in (
        SECRET_NAME, SECRET_NICKNAME, SECRET_EMAIL, SECRET_ALIAS, SECRET_ARGUMENT, SECRET_ROLE,
        SECRET_ORG, SECRET_ORG.lower(), "hush", "acme", "github.com/", str(repo),
        str(repo.resolve()), str(machine.root), str(machine.home), "example.com", "tmp/",
        "pytest-of", "/Users", "label-0", "ssh://", "git@",
    ):  # fmt: skip
        assert secret not in wire, secret


def test_plans_and_models_are_a_closed_set_whatever_a_file_said(machine: Machine) -> None:
    """Contract v1.1: ``by_type`` keys are ``pro max team enterprise api unknown other`` and a model
    is a Claude id (``^claude-[a-z0-9.-]{1,60}$``) or ``other``: text from a login or a transcript
    that names a person, a host or a project is summed under ``other`` and goes no further."""
    rollup = DailyRollup(
        date="2026-10-26",
        tz_offset_minutes=-240,
        tokens=DayTokens(input=60, output=6),
        models={
            "claude-opus-5-5": TokenCounts(input=10, output=1),
            "claude opus/5 'quoted' <b>": TokenCounts(input=20, output=2),
            "acme-internal-finetune": TokenCounts(input=30, output=3),
            "claude-" + "x" * 80: TokenCounts(),
        },
        accounts=DayAccounts(
            by_type={
                "team": 1,
                "team alice.secret@acme-internal.example": 2,
                "max": 1,
                "api": 1,
                "weird plan": 1,
            }
        ),
    )

    day = usage_share.day_body(rollup, "ab" * 32, signed_in=False)

    assert day["models"] == {
        "claude-opus-5-5": {"input": 10, "cache_write": 0, "cache_read": 0, "output": 1},
        "other": {"input": 50, "cache_write": 0, "cache_read": 0, "output": 5},
    }
    assert day["accounts"]["by_type"] == {"api": 1, "max": 1, "other": 3, "team": 1}
    assert day["tz_offset_minutes"] == -240  # negative is right for a zone west of UTC
    wire = json.dumps(day)
    for secret in ("alice", "acme", "internal", "secret", "quoted", "weird"):
        assert secret not in wire, secret


def test_the_caps_of_the_contract_hold(machine: Machine) -> None:
    for number in range(30):
        repo = machine.repo(f"r{number:02d}")
        machine.session(
            1,
            repo,
            f"s{number}",
            prompt(_day(0, 9), cwd=repo),
            request(
                _day(0, 9) + timedelta(minutes=1), f"m{number}", cwd=repo, cache_read=1000 + number
            ),
        )
    usage_stats.sweep(tz=IST)
    with store_session() as store:
        store.add_feature_uses({("2026-10-26", f"cli.group{n}.verb"): n + 1 for n in range(250)})
    _on()

    (today,) = _build().days

    check_contract(_build().body)
    assert today["repos_active"] == 30 and len(today["repos"]) == 25
    assert [r["tokens_total"] for r in today["repos"]] == sorted(
        (r["tokens_total"] for r in today["repos"]), reverse=True
    )
    assert len(today["features"]) == 200
    assert "cli.group0.verb" not in today["features"]  # the least used are the ones cut
    assert "cli.group249.verb" in today["features"]


def test_a_batch_holds_at_most_31_days_oldest_first_and_fits_256_kb(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = machine.repo("long")
    for offset in range(0, 40):
        machine.session(
            1, repo, f"s{offset}", prompt(_day(-offset, 9), cwd=repo),
            request(_day(-offset, 9) + timedelta(minutes=1), f"m{offset}", cwd=repo),
        )  # fmt: skip
    usage_stats.sweep(tz=IST)
    _on(days_back=45)  # shared since 45 days ago: all 40 days of activity are to go

    batches = usage_share.build_batches(now=NOW, tz=IST)
    dates = [day["date"] for batch in batches for day in batch.days]

    assert [len(batch.days) for batch in batches] == [31, 9]
    assert dates == sorted(dates) and dates[0] == "2026-09-17" and dates[-1] == "2026-10-26"
    assert usage_share.build_batch(now=NOW, tz=IST).days[0]["date"] == "2026-09-17"
    for batch in batches:
        check_contract(batch.body)

    monkeypatch.setattr(usage_share, "MAX_BODY_BYTES", 6_000)
    cut = usage_share.build_batches(now=NOW, tz=IST)
    assert len(cut) > 2 and all(len(json.dumps(b.body)) <= 6_000 for b in cut)
    flat = [day["date"] for batch in cut for day in batch.days]
    assert flat == dates  # the same days, in more bodies: none dropped
    assert list(cut[-1].digests)[-1] == "2026-10-26"  # today is the last of the last


# --- the shipment -----------------------------------------------------------------------------


def test_what_is_previewed_is_what_is_posted(machine: Machine) -> None:
    _history(machine)
    _on()
    server = Server()

    shown = json.loads(usage_share.preview(now=NOW, tz=IST))
    report = usage_share.ship(now=NOW, tz=IST, transport=server, force=True)

    assert report.sent and len(server.calls) == 1
    assert server.calls[0].body == shown
    check_contract(shown)
    assert server.calls[0].url.endswith("/api/v1/cli/metrics/")
    assert server.calls[0].timeout == 5.0


def test_the_commands_preview_is_the_body_too(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_clock(monkeypatch, usage_share, sharing)
    _history(machine)
    _on()
    runner = CliRunner()

    shown = json.loads(runner.invoke(app, ["metrics", "share", "preview"]).output)

    check_contract(shown)
    assert shown["install_id"] == usage_share.status().install_id


def test_a_shipment_is_acknowledged_per_day_and_only_today_and_changes_are_sent_again(
    machine: Machine,
) -> None:
    repo = _history(machine)
    _on()
    server = Server()

    first = usage_share.ship(now=NOW, tz=IST, transport=server, force=True)
    again = usage_share.ship(now=NOW + timedelta(hours=7), tz=IST, transport=server)
    # a day already sent grows: the sweep finds more of yesterday
    machine.session(
        1, repo, "late", prompt(_day(-1, 20), cwd=repo),
        request(_day(-1, 20) + timedelta(minutes=1), "late", cwd=repo, cache_read=9999),
    )  # fmt: skip
    usage_stats.sweep(tz=IST)
    changed = usage_share.ship(now=NOW + timedelta(hours=14), tz=IST, transport=server)

    assert (first.sent, first.days) == (True, 3)
    assert [d["date"] for d in server.calls[1].body["days"]] == ["2026-10-26"]  # today only
    assert again.days == 1
    # a day already sent that changed goes again; the others (and a past "today") do not
    assert [d["date"] for d in server.calls[2].body["days"]] == ["2026-10-25"]
    assert changed.sent
    state = sharing.read()
    assert set(state.acked) == {"2026-10-24", "2026-10-25", "2026-10-26"}


def test_a_shipment_is_not_due_again_for_six_hours(machine: Machine) -> None:
    _history(machine)
    _on()
    server = Server()

    usage_share.ship(now=NOW, tz=IST, transport=server)
    soon = usage_share.ship(now=NOW + timedelta(hours=5), tz=IST, transport=server)
    later = usage_share.ship(now=NOW + timedelta(hours=6, minutes=1), tz=IST, transport=server)

    assert soon.skipped is not None and soon.skipped.startswith("not due until")
    assert later.sent and len(server.calls) == 2


def test_nothing_to_send_is_not_a_post(machine: Machine) -> None:
    _on()
    server = Server()

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert report.skipped == "nothing to send" and server.calls == []


def test_a_signed_in_user_sends_the_token_and_the_server_links_it(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    monkeypatch.setenv(iam.TOKEN_ENV_VAR, "aisq_secret_token_value")
    server = Server()

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert server.calls[0].token == "aisq_secret_token_value"
    assert report.linked is True and all(d["signed_in"] for d in server.calls[0].body["days"])
    assert "aisq_secret_token_value" not in json.dumps(server.calls[0].body)
    assert usage_share.status().signed_in is True and usage_share.status().linked is True


def test_a_server_that_refuses_the_token_gets_the_batch_again_without_it(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    monkeypatch.setenv(iam.TOKEN_ENV_VAR, "aisq_expired")
    server = Server(status(401, {"detail": "token expired"}))

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert [call.token for call in server.calls] == ["aisq_expired", None]
    assert server.calls[0].body == server.calls[1].body
    assert report.sent and report.retried_anonymously and report.linked is False
    assert sharing.read().failures == 0


def test_a_session_of_another_deployment_is_never_sent_here(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    server = Server()
    monkeypatch.setattr(
        iam,
        "current_session",
        lambda api_url=None: iam.Session(
            api_url="https://other.example.com", token="aisq_other", source="file"
        ),
    )

    usage_share.ship(now=NOW, tz=IST, transport=server)

    assert [call.token for call in server.calls] == [None]


def test_a_network_error_backs_off_and_the_same_days_go_next_time(machine: Machine) -> None:
    _history(machine)
    _on()
    server = Server(unreachable(), unreachable())

    first = usage_share.ship(now=NOW, tz=IST, transport=server)
    too_soon = usage_share.ship(now=NOW + timedelta(minutes=4), tz=IST, transport=server)
    second = usage_share.ship(now=NOW + timedelta(minutes=6), tz=IST, transport=server)
    state = sharing.read()
    third = usage_share.ship(now=NOW + timedelta(minutes=17), tz=IST, transport=server)

    assert first.error is not None and "Could not reach" in first.error and not first.sent
    assert too_soon.skipped is not None  # the backoff holds the next try off
    assert second.error is not None and state.failures == 2
    assert sharing.parse_time(state.next_due_at) == NOW + timedelta(
        minutes=6, seconds=0
    ) + timedelta(minutes=10)
    assert state.acked == {} and state.last_sent_at is None  # nothing is acknowledged
    assert third.sent and len(server.calls) == 3
    assert server.calls[2].body["days"] == server.calls[0].body["days"]  # nothing was lost
    after = sharing.read()
    assert after.failures == 0 and after.last_error is None and len(after.acked) == 3


def test_the_backoff_doubles_to_six_hours(machine: Machine) -> None:
    assert [sharing.backoff(n) for n in range(1, 7)] == [
        timedelta(minutes=5), timedelta(minutes=10), timedelta(minutes=20),
        timedelta(minutes=40), timedelta(minutes=80), timedelta(minutes=160),
    ]  # fmt: skip
    assert sharing.backoff(30) == timedelta(hours=6)
    assert sharing.backoff(100_000) == timedelta(hours=6)  # a server away for years: no overflow


def test_a_throttled_or_rejected_batch_is_kept_for_later_and_says_why(machine: Machine) -> None:
    _history(machine)
    _on()
    server = Server(
        status(429, {"detail": "throttled"}, **{"Retry-After": "7200"}),
        status(400, {"errors": {"days": "date outside [today-400d, today+1d]"}}),
    )

    throttled = usage_share.ship(now=NOW, tz=IST, transport=server)
    waited = sharing.parse_time(sharing.read().next_due_at)
    rejected = usage_share.ship(now=NOW + timedelta(hours=2, minutes=1), tz=IST, transport=server)

    assert throttled.error == "HTTP 429" and waited == NOW + timedelta(hours=2)  # Retry-After
    assert rejected.error is not None and rejected.error.startswith("HTTP 400: ")
    assert "date outside" in (sharing.read().last_error or "")
    assert sharing.read().acked == {}


def test_a_server_error_is_a_failure_and_a_200_without_a_body_is_not_trusted(
    machine: Machine,
) -> None:
    _history(machine)
    _on()
    server = Server(status(503, "unavailable"), status(200, "not json"))

    first = usage_share.ship(now=NOW, tz=IST, transport=server)
    second = usage_share.ship(now=NOW + timedelta(minutes=6), tz=IST, transport=server)

    assert first.error == "HTTP 503" and (second.error or "").startswith("HTTP 200")
    assert sharing.read().acked == {}


def test_two_shippers_at_once_send_one(machine: Machine) -> None:
    _history(machine)
    _on()
    server = Server()

    with locking.holding(sharing.lock_path(), wait_s=0):
        report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert report.skipped == "another shipment is running" and server.calls == []


def test_a_shipment_that_raises_is_a_failed_report_and_not_a_crash(machine: Machine) -> None:
    _history(machine)
    _on()

    def explode(*args: Any) -> Any:
        raise RuntimeError("the transport broke")

    report = usage_share.ship(now=NOW, tz=IST, transport=explode)

    assert report.error is not None and "the transport broke" in report.error and not report.sent


# --- turning it off ---------------------------------------------------------------------------


def test_off_purges_what_was_kept_and_sends_only_the_opt_out(machine: Machine) -> None:
    _history(machine)
    _on()
    server = Server()
    usage_share.ship(now=NOW, tz=IST, transport=server)
    install_id = sharing.read().install_id

    report = usage_share.disable(now=NOW + timedelta(hours=1), transport=server)
    after = sharing.read()
    quiet = usage_share.ship(now=NOW + timedelta(days=1), tz=IST, transport=server, force=True)

    assert report.sent and report.opt_out
    assert len(server.calls) == 2 and quiet.skipped == "sharing is off"
    ping = server.calls[1].body
    assert ping["opt_out"] is True and ping["days"] == [] and ping["install_id"] == install_id
    assert server.calls[1].token is None
    assert (after.acked, after.next_due_at, after.last_sent_at, after.linked) == (
        {},
        None,
        None,
        None,
    )
    assert after.install_id == install_id  # kept: the same installation if it turns it on again
    assert sharing.switch_on() is False and sharing.start_background_ship(NOW) is False


def test_off_sends_no_notice_when_nothing_was_ever_sent(machine: Machine) -> None:
    _history(machine)
    _on()
    server = Server()

    report = usage_share.disable(now=NOW, transport=server)

    assert server.calls == [] and report.opt_out is False and sharing.switch_on() is False


def test_off_while_forced_off_sends_nothing_at_all(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    server = Server()
    usage_share.ship(now=NOW, tz=IST, transport=server)
    monkeypatch.setenv("DO_NOT_TRACK", "1")

    report = usage_share.disable(now=NOW, transport=server)

    assert len(server.calls) == 1 and report.opt_out is False  # the first shipment only
    assert sharing.switch_on() is False


def test_a_failed_opt_out_notice_is_reported_and_costs_nothing(machine: Machine) -> None:
    _history(machine)
    _on()
    usage_share.ship(now=NOW, tz=IST, transport=Server())

    report = usage_share.disable(now=NOW, transport=Server(unreachable()))

    assert report.opt_out and report.error is not None and not report.sent
    assert sharing.switch_on() is False


# --- the consent ------------------------------------------------------------------------------


def test_the_question_is_due_once_and_an_answer_ends_it(machine: Machine) -> None:
    assert usage_share.consent_due() is True

    usage_share.record_consent("declined", now=NOW)

    assert usage_share.consent_due() is False
    assert sharing.switch_on() is False and usage_share.status().consent_answer == "declined"
    assert sharing.read().consent_asked_at is not None


def test_sharing_chosen_at_the_question_turns_it_on(machine: Machine) -> None:
    status = usage_share.record_consent("share", now=NOW)

    assert status.enabled and status.consent_answer == "share" and status.install_id is not None
    assert usage_share.consent_due() is False


def test_no_question_for_a_switch_already_on_in_the_config(machine: Machine) -> None:
    _on()
    assert usage_share.consent_due() is False


# --- the detached shipper ---------------------------------------------------------------------


def test_a_command_starts_one_shipper_when_due_and_none_for_half_an_hour(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    started: list[int] = []
    monkeypatch.setattr(sharing, "_spawn", lambda: started.append(1))

    first = sharing.start_background_ship(NOW)
    second = sharing.start_background_ship(NOW + timedelta(minutes=5))
    third = sharing.start_background_ship(NOW + timedelta(minutes=31))
    usage_share._schedule(NOW + timedelta(minutes=31))  # a shipment went through
    fourth = sharing.start_background_ship(NOW + timedelta(hours=2))

    assert (first, second, third, fourth) == (True, False, True, False)
    assert len(started) == 2


def test_a_command_never_starts_the_shipper_for_itself_or_for_a_hook(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_clock(monkeypatch, usage_share, sharing)
    _history(machine)
    _on()
    started: list[int] = []
    monkeypatch.setattr(sharing, "_spawn", lambda: started.append(1))
    runner = CliRunner()
    monkeypatch.setattr(usage_share, "default_transport", Server())

    runner.invoke(app, ["metrics", "ship", "--force"])
    runner.invoke(app, ["hook", "--help"])
    shipped = len(started)
    sharing.update(lambda s: setattr(s, "next_due_at", None))  # a shipment is due again
    runner.invoke(app, ["config", "list"])  # an ordinary command, with a shipment due

    assert shipped == 0 and len(started) == 1


def test_a_failing_start_costs_the_command_nothing(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()

    def refuse() -> None:
        raise OSError("no processes")

    monkeypatch.setattr(sharing, "_spawn", refuse)

    result = CliRunner().invoke(app, ["config", "list"])

    assert result.exit_code == 0
    assert sharing.start_background_ship(NOW) is False


def test_off_it_costs_a_command_next_to_nothing(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr(
        sharing, "_spawn", lambda: pytest.fail("a shipper started with sharing off")
    )
    sharing.start_background_ship(NOW)  # warm the config cache
    began = time.process_time()  # CPU time: a wall budget measures the machine's load
    for _ in range(200):
        sharing.start_background_ship(NOW)
    per_call = (time.process_time() - began) / 200

    assert per_call < 0.005, f"{per_call * 1e6:.0f} µs of CPU a command"


# --- the commands -----------------------------------------------------------------------------


def test_metrics_share_on_status_preview_off(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_clock(monkeypatch, usage_share, sharing)
    _history(machine)
    monkeypatch.setattr(sharing, "_spawn", lambda: None)
    server = Server()
    monkeypatch.setattr(usage_share, "default_transport", server)
    runner = CliRunner()

    before = json.loads(runner.invoke(app, ["--json", "metrics", "share", "status"]).output)
    on = json.loads(runner.invoke(app, ["--json", "metrics", "share", "on"]).output)
    shipped = runner.invoke(app, ["metrics", "ship", "--force"])
    text = runner.invoke(app, ["metrics", "share", "status"]).output
    off = runner.invoke(app, ["--json", "metrics", "share", "off"])

    assert (before["enabled"], before["install_id"]) == (False, None)
    assert on["enabled"] is True and on["install_id"] and on["consent_answer"] == "share"
    sent = server.calls[0].body
    check_contract(sent)
    assert shipped.exit_code == 0 and f"Sent {len(sent['days'])} day(s)" in shipped.output
    assert sent["install_id"] == on["install_id"]
    # `share on` turns it on as of NOW: the days before it were not agreed to and do not go
    assert [day["date"] for day in sent["days"]] == ["2026-10-26"]
    assert "Sharing is ON" in text and "not signed in: batches are anonymous" in text
    assert json.loads(off.output)["opt_out"] is True and len(server.calls) == 2
    assert server.calls[1].body["opt_out"] is True


def test_metrics_share_on_says_which_repositories_are_counted(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review of #444 (m2): the dialog said it; `share on` and the Settings switch did not."""
    monkeypatch.setattr(sharing, "_spawn", lambda: None)
    runner = CliRunner()

    human = runner.invoke(app, ["metrics", "share", "on"])
    machine_readable = runner.invoke(app, ["--json", "metrics", "share", "on"])

    assert human.exit_code == 0 and usage_share.COVERAGE in human.output
    # its --help is this docstring (the rendered page is boxed and coloured by the terminal)
    from aisquare.cli import metrics as metrics_cli

    assert "including ones you haven't added" in " ".join(
        (metrics_cli.share_on.__doc__ or "").split()
    )
    assert json.loads(machine_readable.output)["enabled"] is True  # still one JSON object


def test_metrics_share_on_is_refused_where_something_forces_it_off(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")

    result = CliRunner().invoke(app, ["metrics", "share", "on"])

    assert result.exit_code == 1 and "CI is set" in result.output
    assert sharing.switch_on() is False


def test_the_shipper_command_reads_the_transcripts_first_and_prints_what_it_did(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_clock(monkeypatch, usage_share, sharing)
    repo = machine.repo("fresh")
    machine.session(
        1, repo, "s", prompt(_day(0, 9), cwd=repo),
        request(_day(0, 9) + timedelta(minutes=1), "m", cwd=repo),
    )  # fmt: skip
    _on()
    server = Server()
    monkeypatch.setattr(usage_share, "default_transport", server)

    result = CliRunner().invoke(app, ["--json", "metrics", "ship", "--force"])

    report = json.loads(result.output)
    assert (
        report["sent"] is True and server.calls[0].body["days"][-1]["tokens"]["total"] == tokens()
    )


# --- failing silently, retrying forever, losing nothing (the owner's decision) ----------------

BACKLOG_DAYS = 70


def _backlog(m: Machine) -> None:
    """70 days of activity, to be shared since 80 days ago: more than one batch's worth."""
    repo = m.repo("backlog")
    for offset in range(BACKLOG_DAYS):
        m.session(
            1, repo, f"b{offset}", prompt(_day(-offset, 9), cwd=repo),
            request(_day(-offset, 9) + timedelta(minutes=1), f"bm{offset}", cwd=repo),
        )  # fmt: skip
    usage_stats.sweep(tz=IST)
    _on(days_back=80)


def _dates(server: Server) -> list[list[str]]:
    return [[day["date"] for day in call.body["days"]] for call in server.calls]


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(status(404, {"detail": "Not Found"}), id="the endpoint is not deployed (404)"),
        pytest.param(unreachable(), id="connection refused"),
        pytest.param(status(502, "bad gateway"), id="5xx"),
        pytest.param(status(400, {"errors": {"days": "no"}}), id="4xx"),
        pytest.param(status(429, None, **{"Retry-After": "60"}), id="throttled"),
        pytest.param(status(200, "not json at all"), id="a reply that is not json"),
        pytest.param(iam.IamError("unreachable", "TLS handshake failed"), id="TLS / DNS"),
    ],
)
def test_a_shipment_that_fails_makes_no_sound_and_leaves_the_days_queued(
    machine: Machine,
    answer: Any,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _history(machine)
    _on()

    with caplog.at_level("DEBUG", logger="aisquare.usage_share"):
        report = usage_share.ship(now=NOW, tz=IST, transport=Server(answer), force=True)
    out = capsys.readouterr()

    assert report.sent is False and report.error is not None
    assert (out.out, out.err) == ("", "")  # no stdout, no stderr
    assert any(r.levelname == "DEBUG" and "not sent" in r.getMessage() for r in caplog.records)
    assert all(r.levelno <= 10 for r in caplog.records if r.name == "aisquare.usage_share")
    state = sharing.read()
    assert state.acked == {} and state.settled_through is None  # nothing is marked as sent
    assert state.failures == 1
    # and what was queued is all still there to go
    assert len(_build().days) == 3


def test_the_commands_say_nothing_of_a_failed_shipment(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_clock(monkeypatch, usage_share, sharing)
    _history(machine)
    _on()
    monkeypatch.setattr(usage_share, "default_transport", Server(status(404), unreachable()))
    runner = CliRunner()

    plain = runner.invoke(app, ["metrics", "ship", "--force"])
    as_json = runner.invoke(app, ["--json", "metrics", "ship", "--force"])
    shown = runner.invoke(app, ["metrics", "share", "status"])
    debug = json.loads(runner.invoke(app, ["--json", "metrics", "share", "status"]).output)

    assert (plain.exit_code, plain.output) == (0, "")
    assert json.loads(as_json.output)["error"] is not None  # --json is the debug view
    text = shown.output.lower()
    for word in ("fail", "error", "retry", "404", "unreach", "could not", "refused"):
        assert word not in text, word
    assert "sharing is on" in text and "last sent      never" in text
    assert debug["debug"]["failures"] == 2 and debug["debug"]["last_error"]
    assert {"enabled", "switch", "last_sent_at", "linked", "debug"} <= set(debug)


def test_the_retry_goes_on_for_ever_with_a_capped_backoff_and_never_gives_up(
    machine: Machine,
) -> None:
    _history(machine)
    _on()
    server = Server(*([status(404)] * 40))
    moment = NOW
    waits = []

    for _ in range(40):
        usage_share.ship(now=moment, tz=IST, transport=server)
        state = sharing.read()
        waits.append(sharing.parse_time(state.next_due_at) - moment)  # type: ignore[operator]
        moment = sharing.parse_time(state.next_due_at)  # type: ignore[assignment]

    assert len(server.calls) == 40  # not one skipped: it is tried at every due time
    assert waits[0] == timedelta(minutes=5) and max(waits) == timedelta(hours=6)
    assert waits[-1] == timedelta(hours=6) and all(w <= timedelta(hours=6) for w in waits)
    assert sharing.read().failures == 40 and sharing.read().acked == {}  # no dead-letter either


def test_a_later_200_delivers_the_whole_backlog_oldest_first_in_batches_of_31(
    machine: Machine,
) -> None:
    _backlog(machine)
    server = Server(status(502), unreachable(), status(503))
    moment = NOW
    for _ in range(3):
        usage_share.ship(now=moment, tz=IST, transport=server)  # the endpoint is not there yet
        moment = sharing.parse_time(sharing.read().next_due_at)  # type: ignore[assignment]
    assert sharing.read().acked == {} and sharing.read().failures == 3

    delivered = usage_share.ship(now=moment, tz=IST, transport=server)  # it is deployed now

    sent = _dates(server)[3:]
    flat = [date for batch in sent for date in batch]
    assert delivered.sent and delivered.days == BACKLOG_DAYS
    assert [len(batch) for batch in sent] == [31, 31, 8]  # oldest first, 31 days at most
    assert flat == sorted(flat) and len(flat) == len(set(flat)) == BACKLOG_DAYS
    assert flat[-1] == "2026-10-26"  # the last batch ends with today
    for call in server.calls[3:]:
        check_contract(call.body)
        assert call.url.endswith("/api/v1/cli/metrics/")
    after = sharing.read()
    assert after.failures == 0 and after.last_error is None and after.last_sent_at is not None
    # none of it is sent again, and the file that tracks it stays small
    again = usage_share.ship(now=moment + timedelta(hours=7), tz=IST, transport=server)
    assert (again.sent and _dates(server)[-1] == []) or _dates(server)[-1] == ["2026-10-26"]
    assert len(after.acked) <= 61 and after.settled_through is not None


def test_a_failure_in_the_middle_of_a_backlog_keeps_what_went_and_resumes_where_it_stopped(
    machine: Machine,
) -> None:
    _backlog(machine)
    server = Server(ok(31), status(503))

    first = usage_share.ship(now=NOW, tz=IST, transport=server)
    resumed = usage_share.ship(now=NOW + timedelta(hours=7), tz=IST, transport=server)

    sent = _dates(server)
    assert first.sent and first.days == 31 and first.error is not None  # 31 went, then 503
    assert len(sent) == 4 and sent[0][0] < sent[1][0]  # first batch, the failed one, then retried
    assert sent[2] == sent[1]  # the batch that failed is the one that is sent again
    assert resumed.sent and sum(len(batch) for batch in (sent[0], *sent[2:])) == BACKLOG_DAYS


def test_a_month_of_downtime_loses_no_day_even_beyond_the_old_31_day_window(
    machine: Machine,
) -> None:
    _backlog(machine)  # 70 days of activity, shared since 80 days ago
    moment = NOW
    outage = Server(*([status(503)] * 30))
    for _ in range(30):
        usage_share.ship(now=moment, tz=IST, transport=outage)
        moment = sharing.parse_time(sharing.read().next_due_at)  # type: ignore[assignment]
    working = Server()

    usage_share.ship(now=moment, tz=IST, transport=working)

    dates = {date for batch in _dates(working) for date in batch}
    assert len(dates) == BACKLOG_DAYS and "2026-08-18" in dates  # 69 days before today is sent


def test_nothing_older_than_the_servers_400_days_is_attempted(machine: Machine) -> None:
    repo = machine.repo("ancient")
    machine.session(
        1, repo, "old", prompt(_day(-450, 9), cwd=repo),
        request(_day(-450, 9) + timedelta(minutes=1), "old", cwd=repo),
    )  # fmt: skip
    usage_stats.sweep(tz=IST)
    _on(days_back=500)

    assert usage_share.build_batches(now=NOW, tz=IST) == []  # the server would answer 400


# --- only what happens from the moment it is agreed to ----------------------------------------


def test_the_days_before_sharing_was_turned_on_are_never_sent(machine: Machine) -> None:
    _history(machine)  # activity on Oct 24, 25 and 26
    usage_share.enable(now=NOW - timedelta(days=1))  # turned on on the 25th
    server = Server()

    usage_share.ship(now=NOW, tz=IST, transport=server)

    assert _dates(server) == [["2026-10-25", "2026-10-26"]]  # not the 24th
    usage_share.disable(now=NOW, transport=server)
    usage_share.enable(now=NOW + timedelta(days=3))  # off for days, then on again
    state = sharing.read()
    assert state.since == "2026-10-29" and state.acked == {}  # the gap in between is not shared


def test_while_sharing_is_off_the_preview_is_a_sample_of_the_last_31_days(
    machine: Machine,
) -> None:
    _history(machine)

    sample = usage_share.build_batch(now=NOW, tz=IST)
    _on(days_back=0)  # turned on today
    real = usage_share.build_batch(now=NOW, tz=IST)

    assert [d["date"] for d in sample.days] == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert [d["date"] for d in real.days] == ["2026-10-26"]  # the body that would be posted
    assert sample.body["install_id"] == usage_share.PLACEHOLDER_ID
    assert real.body["install_id"] == sharing.read().install_id
    check_contract(sample.body)


def test_a_switch_turned_on_by_hand_in_the_config_shares_from_its_first_shipment(
    machine: Machine,
) -> None:
    _history(machine)
    settings_service.set_value("sharing.enabled", "true")  # no `share on`: no `since` yet
    server = Server()

    usage_share.ship(now=NOW, tz=IST, transport=server)

    assert _dates(server) == [["2026-10-26"]] and sharing.read().since == "2026-10-26"


# --- turning it off stops a shipment in flight (review of #5, F1) -----------------------------


def _wait_for(condition: Any, seconds: float = 10.0) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_turning_it_off_while_a_shipment_is_in_flight_stops_it_and_it_leaves_no_state_behind(
    machine: Machine,
) -> None:
    """The tester's repro: 75 days, a slow server, `share off` while the first POST is out. The
    shipment finishes that POST, posts nothing after the opt-out notice, records nothing, and what
    `off` purged stays purged."""
    import threading

    _backlog(machine)  # 70 days: three batches
    order: list[str] = []
    in_post, release = threading.Event(), threading.Event()
    done: dict[str, Any] = {}

    def slow(url: str, body: dict[str, Any], token: str | None, timeout: float) -> iam.HttpResult:
        order.append(f"batch:{len(body['days'])}")
        in_post.set()
        release.wait(timeout=60)  # the POST is out: the user turns sharing off now
        return ok(len(body["days"]))

    def ping(url: str, body: dict[str, Any], token: str | None, timeout: float) -> iam.HttpResult:
        order.append("opt_out" if body["opt_out"] else "BATCH AFTER OFF")
        return ok(1)

    shipper = threading.Thread(
        target=lambda: done.update(ship=usage_share.ship(now=NOW, tz=IST, transport=slow))
    )
    off = threading.Thread(
        target=lambda: done.update(off=usage_share.disable(now=NOW, transport=ping))
    )
    shipper.start()
    assert in_post.wait(10)
    off.start()
    assert _wait_for(lambda: not sharing.switch_on()), "the switch is turned off at once"
    release.set()
    shipper.join(15)
    off.join(15)

    assert order == ["batch:31", "opt_out"], order  # one POST went; then only the notice
    assert done["ship"].skipped == "sharing was turned off" and not done["ship"].sent
    assert done["off"].opt_out and done["off"].sent  # a shipment was in flight: the notice goes
    after = sharing.read()
    assert (after.acked, after.last_sent_at, after.next_due_at, after.since) == (
        {},
        None,
        None,
        None,
    )
    assert (after.linked, after.failures, after.settled_through) == (None, 0, None)
    assert usage_share.status().last_sent_at is None  # off says nothing of a "last sent"


def test_the_switch_is_looked_at_again_before_every_post(machine: Machine) -> None:
    _backlog(machine)
    calls: list[int] = []

    def turns_it_off(
        url: str, body: dict[str, Any], token: str | None, timeout: float
    ) -> iam.HttpResult:
        calls.append(len(body["days"]))
        settings_service.set_value("sharing.enabled", "false")  # as `share off` does, first
        return ok(len(body["days"]))

    report = usage_share.ship(now=NOW, tz=IST, transport=turns_it_off)

    assert calls == [31]  # not a second batch
    assert report.skipped == "sharing was turned off" and report.sent is False
    state = sharing.read()
    assert state.acked == {} and state.last_sent_at is None and state.next_due_at is None


def test_a_failure_after_it_went_off_does_not_write_back_either(machine: Machine) -> None:
    _history(machine)
    _on()

    def turns_it_off_then_fails(*args: Any) -> iam.HttpResult:
        settings_service.set_value("sharing.enabled", "false")
        return status(503)

    usage_share.ship(now=NOW, tz=IST, transport=turns_it_off_then_fails)

    assert (sharing.read().failures, sharing.read().last_error, sharing.read().next_due_at) == (
        0, None, None,
    )  # fmt: skip


def test_off_waits_for_a_stuck_shipment_only_a_little_and_then_purges_all_the_same(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    usage_share.ship(now=NOW, tz=IST, transport=Server())
    monkeypatch.setattr(usage_share, "DISABLE_WAIT_S", 0.2)

    with locking.holding(sharing.lock_path(), wait_s=0):  # a shipment that never ends
        server = Server()
        report = usage_share.disable(now=NOW, transport=server)

    assert report.opt_out and report.sent and len(server.calls) == 1
    assert server.calls[0].body["opt_out"] is True
    assert sharing.read().last_sent_at is None and sharing.switch_on() is False


# --- only the contract's 200 is an acknowledgement (F2) ---------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="an empty object"),
        pytest.param({"error": "Not Found"}, id="an error with a 200"),
        pytest.param({"status": "ok"}, id="a gateway's ok"),
        pytest.param({"accepted_days": "x", "linked": False}, id="accepted_days a string"),
        pytest.param({"accepted_days": 0, "linked": False}, id="accepted_days 0"),
        pytest.param({"accepted_days": True, "linked": False}, id="accepted_days a bool"),
        pytest.param({"accepted_days": 2.5, "linked": False}, id="accepted_days a float"),
        pytest.param({"accepted_days": 3}, id="linked missing"),
        pytest.param({"accepted_days": 3, "linked": "yes"}, id="linked a string"),
        pytest.param({"linked": True}, id="accepted_days missing"),
        pytest.param([], id="a list"),
        pytest.param("<html>200</html>", id="html"),
        pytest.param(None, id="no body"),
    ],
)
def test_a_200_that_is_not_the_contracts_reply_is_not_an_acknowledgement(
    machine: Machine, body: Any
) -> None:
    _history(machine)
    _on()
    server = Server(status(200, body))

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert report.sent is False and (report.error or "").startswith("HTTP 200")
    state = sharing.read()
    assert state.acked == {} and state.last_sent_at is None and state.failures == 1
    assert len(_build().days) == 3  # every day is still queued, and goes next time
    retry = usage_share.ship(now=NOW + timedelta(minutes=6), tz=IST, transport=Server())
    assert retry.sent and retry.days == 3


def test_a_reply_with_the_contracts_two_keys_is_taken_whatever_else_it_carries(
    machine: Machine,
) -> None:
    _history(machine)
    _on()
    server = Server(status(200, {"accepted_days": 3, "linked": True, "request_id": "abc"}))

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert report.sent and report.days == 3 and report.linked is True
    assert len(sharing.read().acked) == 3


# --- no redirect is followed (F3) -------------------------------------------------------------


class _Site:
    """A loopback HTTP server that records what it is sent and answers as told."""

    def __init__(self, answer: Callable[[Any], tuple[int, dict[str, str], bytes]]) -> None:
        import http.server
        import threading

        outer = self
        self.requests: list[tuple[str, str, str | None]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.requests.append((self.command, self.path, self.headers.get("Authorization")))
                code, headers, payload = answer(self)
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

            def log_message(self, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_is_never_followed_and_the_bearer_goes_to_one_origin_only(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    other = _Site(lambda h: (200, {}, b'{"accepted_days": 9, "linked": true}'))
    home = _Site(lambda h: (code, {"Location": f"{other.url}/stolen"}, b""))
    try:
        _history(machine)
        _on()
        monkeypatch.setenv("AISQUARE_API_URL", home.url)
        monkeypatch.setenv(iam.TOKEN_ENV_VAR, "aisq_secret_bearer")

        report = usage_share.ship(now=NOW, tz=IST)  # the real transport, over real sockets

        assert report.sent is False and report.error == f"HTTP {code}"
        assert [(m, p) for m, p, _ in home.requests] == [("POST", "/api/v1/cli/metrics/")]
        assert home.requests[0][2] == "Bearer aisq_secret_bearer"
        assert other.requests == []  # the redirect target was never contacted
        state = sharing.read()
        assert state.acked == {} and state.failures == 1  # a retryable failure, silent
    finally:
        home.close()
        other.close()


def test_a_post_that_is_answered_directly_goes_through_the_real_transport(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = _Site(
        lambda h: (
            200,
            {"Content-Type": "application/json"},
            b'{"accepted_days": 3, "linked": false}',
        )
    )
    try:
        _history(machine)
        _on()
        monkeypatch.setenv("AISQUARE_API_URL", site.url)

        report = usage_share.ship(now=NOW, tz=IST)

        assert report.sent and report.days == 3
        assert site.requests == [("POST", "/api/v1/cli/metrics/", None)]  # anonymous: no header
    finally:
        site.close()


# --- the preview is built the way a shipment is (F4) ------------------------------------------


def test_the_preview_reads_the_transcripts_first_so_it_is_the_body_that_is_posted(
    machine: Machine,
) -> None:
    repo = _history(machine)
    _on()
    machine.session(
        1, repo, "unswept", prompt(_day(0, 11), cwd=repo),
        request(_day(0, 11) + timedelta(minutes=1), "unswept", cwd=repo, cache_read=77_000),
    )  # fmt: skip
    with store_session() as store:
        store.delete_meta("usage.sweep.at")  # the ten-minute throttle is open

    shown = json.loads(usage_share.preview(now=NOW, tz=IST))
    with store_session() as store:
        store.delete_meta("usage.sweep.at")
    server = Server()
    usage_share.ship(now=NOW, tz=IST, transport=server, force=True)

    assert server.calls[0].body == shown
    assert shown["days"][-1]["tokens"]["cache_read"] >= 77_000  # the unswept session is in it


def test_the_preview_command_reads_the_transcripts_first_too(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_clock(monkeypatch, usage_share, sharing)
    swept: list[int] = []
    monkeypatch.setattr(usage_stats, "maybe_sweep", lambda **kw: swept.append(1))

    CliRunner().invoke(app, ["metrics", "share", "preview"])
    CliRunner().invoke(app, ["metrics", "ship", "--force"])

    assert swept == [1]  # `preview` swept; `ship` was not due to run (sharing off): it did not


# --- the nits ---------------------------------------------------------------------------------


def test_an_explicit_off_is_the_answer_to_the_one_time_question(machine: Machine) -> None:
    assert usage_share.consent_due() is True

    usage_share.disable(now=NOW)

    assert usage_share.consent_due() is False
    assert usage_share.status().consent_answer == "declined"
    assert sharing.read().consent_asked_at is not None


def test_off_after_a_yes_keeps_the_yes_as_the_answer(machine: Machine) -> None:
    usage_share.record_consent("share", now=NOW)
    usage_share.disable(now=NOW)

    assert usage_share.status().consent_answer == "share"


def test_the_preview_command_says_it_is_a_sample_while_sharing_is_off(machine: Machine) -> None:
    _history(machine)
    runner = CliRunner()

    off = runner.invoke(app, ["metrics", "share", "preview"])
    _on(days_back=0)
    on = runner.invoke(app, ["metrics", "share", "preview"])

    assert "SAMPLE" in off.stderr and "placeholder ids" in off.stderr
    assert "once sharing is on only days from then on are sent" in off.stderr
    assert "SAMPLE" not in on.stderr and "sharing is off" not in on.stderr
    json.loads(off.stdout)  # the body alone on stdout, both ways
    json.loads(on.stdout)


# --- a damaged state file never prints a traceback (d) ----------------------------------------


@pytest.mark.parametrize(
    "damage",
    [
        {"salt": "not-hex"},
        {"salt": "ab" * 31},
        {"salt": ""},
        {"install_id": "not-a-uuid"},
        {"salt": "zz" * 32, "install_id": "x"},
    ],
)
def test_a_damaged_identity_never_prints_a_traceback_and_is_minted_again(
    machine: Machine, damage: dict[str, str]
) -> None:
    _history(machine)
    _on()

    def damage_it(state: sharing.SharingState) -> None:
        for key, value in damage.items():
            setattr(state, key, value)

    sharing.update(damage_it)
    runner = CliRunner()

    preview = runner.invoke(app, ["metrics", "share", "preview"])
    server = Server()
    report = usage_share.ship(now=NOW, tz=IST, transport=server, force=True)

    assert preview.exit_code == 0 and "Traceback" not in preview.output + preview.stderr
    check_contract(json.loads(preview.stdout))
    assert report.sent and len(server.calls) == 1
    check_contract(server.calls[0].body)
    state = sharing.read()
    assert usage_share._valid_id(state.install_id) and usage_share._valid_salt(state.salt)


def test_a_state_file_that_is_not_json_is_met_quietly(machine: Machine) -> None:
    _history(machine)
    paths_state = paths.state_path()
    paths_state.parent.mkdir(parents=True, exist_ok=True)
    paths_state.write_text("{not json", encoding="utf-8")

    result = CliRunner().invoke(app, ["metrics", "share", "preview"])

    assert "Traceback" not in result.output + result.stderr
    assert result.exit_code in (0, 1)


# --- turning it on when it is on keeps the backlog (review of #5, F5) -------------------------


def test_turning_it_on_again_keeps_the_first_day_the_backlog_and_the_schedule(
    machine: Machine,
) -> None:
    """A dotfile or a person that says `share on` again while the server is down must not throw
    the days still waiting away: ten days of silent failures are ten days to send."""
    _history(machine)
    _on(days_back=10)
    failed = usage_share.ship(
        now=NOW, tz=IST, transport=Server(status(503, "unavailable"))
    )  # silent: the days stay unsent
    before = sharing.read()
    queued = [day["date"] for day in _build().days]

    usage_share.enable(now=NOW)
    usage_share.enable(now=NOW + timedelta(days=3))

    after = sharing.read()
    assert failed.error == "HTTP 503" and len(queued) == 3
    assert after.since == before.since == (NOW - timedelta(days=10)).astimezone().date().isoformat()
    assert [day["date"] for day in _build().days] == queued  # the days to send are unchanged
    assert (after.acked, after.settled_through) == (before.acked, before.settled_through)
    assert (after.failures, after.last_error, after.next_due_at) == (
        before.failures, before.last_error, before.next_due_at,
    )  # fmt: skip
    assert (after.install_id, after.salt, after.consent_answer) == (
        before.install_id, before.salt, before.consent_answer,
    )  # fmt: skip


def test_turning_it_on_again_after_a_delivery_keeps_what_was_acknowledged(machine: Machine) -> None:
    _history(machine)
    _on(days_back=10)
    usage_share.ship(now=NOW, tz=IST, transport=Server())
    before = sharing.read()

    usage_share.enable(now=NOW + timedelta(hours=1))

    after = sharing.read()
    assert before.acked and after.acked == before.acked
    assert (after.since, after.last_sent_at, after.next_due_at) == (
        before.since, before.last_sent_at, before.next_due_at,
    )  # fmt: skip
    server = Server()
    usage_share.ship(now=NOW + timedelta(hours=1), tz=IST, transport=server, force=True)
    assert [len(call.body["days"]) for call in server.calls] == [1]  # today only: nothing resent


def test_off_then_on_starts_afresh_from_the_day_it_is_turned_on(machine: Machine) -> None:
    _history(machine)
    _on(days_back=10)
    usage_share.ship(now=NOW, tz=IST, transport=Server())

    usage_share.disable(now=NOW, transport=Server())
    usage_share.enable(now=NOW + timedelta(days=1))

    after = sharing.read()
    assert after.since == (NOW + timedelta(days=1)).astimezone().date().isoformat()
    assert after.acked == {} and after.last_sent_at is None


def test_a_switch_turned_on_by_hand_is_started_by_the_first_turning_on(machine: Machine) -> None:
    """`config set sharing.enabled true` leaves no first day; `share on` sets it."""
    settings_service.set_value("sharing.enabled", "true")
    assert sharing.read().since is None

    usage_share.enable(now=NOW)

    assert sharing.read().since == NOW.astimezone().date().isoformat()


# --- a preview is a sample unless a shipment would send exactly it (e), (f) -------------------


def test_a_switch_turned_on_by_hand_with_no_identity_previews_as_a_sample(
    machine: Machine,
) -> None:
    _history(machine)
    settings_service.set_value("sharing.enabled", "true")  # as `aisquare config set` does
    runner = CliRunner()

    first = runner.invoke(app, ["metrics", "share", "preview"])
    second = runner.invoke(app, ["metrics", "share", "preview"])

    assert usage_share.is_sample() is True and "no id yet" in (usage_share.sample_reason() or "")
    assert "SAMPLE" in first.stderr and "placeholder ids" in first.stderr
    one, two = json.loads(first.stdout), json.loads(second.stdout)
    assert one["install_id"] == usage_share.PLACEHOLDER_ID
    hashes = [r["repo_hash"] for day in one["days"] for r in day["repos"]]
    assert hashes and hashes == [r["repo_hash"] for day in two["days"] for r in day["repos"]]
    assert sharing.read().install_id is None  # looking stored nothing


def test_once_the_first_shipment_minted_the_identity_the_preview_is_that_shipment(
    machine: Machine,
) -> None:
    _history(machine)
    settings_service.set_value("sharing.enabled", "true")
    server = Server()
    usage_share.ship(now=NOW, tz=IST, transport=server, force=True)

    assert usage_share.is_sample() is False and usage_share.sample_reason() is None
    shown = json.loads(usage_share.preview(now=NOW, tz=IST))
    again = Server()
    usage_share.ship(now=NOW, tz=IST, transport=again, force=True)
    assert again.calls[0].body["install_id"] == shown["install_id"] != usage_share.PLACEHOLDER_ID
    assert again.calls[0].body["days"] == shown["days"]


def test_a_forced_off_preview_is_a_sample_too(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    monkeypatch.setenv("DO_NOT_TRACK", "1")

    assert "DO_NOT_TRACK" in (usage_share.sample_reason() or "")


def test_under_json_the_preview_says_whether_it_is_a_sample(machine: Machine) -> None:
    _history(machine)
    runner = CliRunner()

    off = json.loads(runner.invoke(app, ["--json", "metrics", "share", "preview"]).stdout)
    _on(days_back=0)
    on = json.loads(runner.invoke(app, ["--json", "metrics", "share", "preview"]).stdout)

    assert off["sample"] is True and off["sample_reason"] == "sharing is off"
    assert on["sample"] is False and on["sample_reason"] is None
    for envelope in (off, on):
        assert set(envelope) == {"sample", "sample_reason", "body"}
        check_contract(envelope["body"])


# --- a state file that cannot be updated ends `share on` cleanly (g) --------------------------


def test_share_on_with_a_state_file_that_is_not_json_exits_cleanly_and_stays_off(
    machine: Machine,
) -> None:
    state = paths.state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json", encoding="utf-8")
    runner = CliRunner()

    human = runner.invoke(app, ["metrics", "share", "on"])
    machine_readable = runner.invoke(app, ["--json", "metrics", "share", "on"])
    reset = runner.invoke(app, ["metrics", "share", "reset-id"])

    for result in (human, machine_readable, reset):
        assert result.exit_code == 1, result.output
        assert "Traceback" not in result.output + result.stderr
        assert result.exception is None or isinstance(result.exception, SystemExit)
    assert f"{paths.state_path().name} cannot be updated" in human.stderr
    assert "repair or remove" in human.stderr  # the way out is in the message, not only the hint
    assert json.loads(machine_readable.stdout)["error"] == "sharing_state_unwritable"
    assert sharing.switch_on() is False  # failed closed: it did not turn on
    assert state.read_text(encoding="utf-8") == "{not json"  # and the file was left as it was


# --- defence in depth: a field that could carry text is a shape or a closed set (review of #444) --


def test_a_language_a_profile_cannot_name_is_not_sent(machine: Machine) -> None:
    from aisquare.models import DayRepo, RepoTraits

    rollup = DailyRollup(
        date="2026-10-26",
        tz_offset_minutes=330,
        repos=[
            DayRepo(
                repo_id="r1", key="github.com/acme/hush", tokens_total=1, sessions=1, turns=1,
                traits=RepoTraits(host="github", language="alice-secret-lang", size_bucket="s"),
            ),
            DayRepo(
                repo_id="r2", key="github.com/acme/ok", tokens_total=1, sessions=1, turns=1,
                traits=RepoTraits(host="github", language="python", size_bucket="s"),
            ),
            DayRepo(
                repo_id="r3", key="github.com/acme/none", tokens_total=1, sessions=1, turns=1,
                traits=RepoTraits(host="github", language=None, size_bucket="s"),
            ),
        ],
    )  # fmt: skip

    day = usage_share.day_body(rollup, "ab" * 32, signed_in=False)

    assert sorted(r["traits"]["language"] for r in day["repos"]) == ["python", "unknown", "unknown"]
    assert "alice" not in json.dumps(day)


@pytest.mark.parametrize(
    ("release", "sent"),
    [
        ("27.0.0", "27.0"),
        ("6.8.0-45-generic", "6.8"),
        ("6.8-alice-laptop", "unknown"),
        ("10", "10"),
        ("5.15.alice-pc.x", "5.15"),  # only the first two fields are kept
        ("", "unknown"),
    ],
)
def test_the_os_version_is_a_version_or_unknown(
    monkeypatch: pytest.MonkeyPatch, release: str, sent: str
) -> None:
    import platform

    monkeypatch.setattr(platform, "release", lambda: release)

    assert usage_share.client_info()["os_version"] == sent


def test_the_platform_names_in_client_are_shapes_or_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Alice's OS")
    monkeypatch.setattr(platform, "machine", lambda: "arm64 (alice)")
    monkeypatch.setattr(platform, "python_version", lambda: "alice-fork")

    client = usage_share.client_info()

    assert (client["os"], client["arch"], client["python"]) == ("unknown",) * 3


def test_a_feature_name_outside_the_contracts_shape_is_not_sent(machine: Machine) -> None:
    rollup = DailyRollup(
        date="2026-10-26",
        tz_offset_minutes=0,
        features={"cli.fleet.spawn": 3, "tui.launch": 2, "x" * 65: 1, "Has Space": 1, "a/b": 1},
    )

    day = usage_share.day_body(rollup, "ab" * 32, signed_in=False)

    assert day["features"] == {"cli.fleet.spawn": 3, "tui.launch": 2}


# --- a batch the server refuses for good does not block the newer ones (review of #444, 3) ----


def test_a_refused_batch_does_not_starve_the_newer_batches_and_today(machine: Machine) -> None:
    _backlog(machine)  # 70 days: batches of 31, 31 and 8
    server = Server(status(400, {"errors": {"days": ["no"]}}))

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    sent = _dates(server)
    assert [len(batch) for batch in sent] == [31, 31, 8]  # all three were asked
    assert report.sent and report.error is None and report.rejected_days == 31
    assert report.days == 39
    state = sharing.read()
    assert set(state.acked) == set(sent[1]) | set(sent[2])  # the refused days are not acked
    assert state.failures == 0 and "refused" in (state.last_error or "")
    # and they are asked again, with today, at the next shipment
    again = Server()
    usage_share.ship(now=NOW + timedelta(hours=7), tz=IST, transport=again)
    assert _dates(again)[0] == sent[0] and len(again.calls) == 2  # the oldest 31, then today
    after = sharing.read()
    assert all(day in after.acked or day <= (after.settled_through or "") for day in sent[0])


@pytest.mark.parametrize("code", [400, 404, 409, 413, 422])
def test_every_4xx_that_is_about_the_batch_is_skipped_ahead_of(machine: Machine, code: int) -> None:
    _backlog(machine)
    server = Server(status(code, "no"))

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert len(server.calls) == 3 and report.sent and report.rejected_days == 31


@pytest.mark.parametrize("code", [429, 408, 500, 502, 503])
def test_a_throttle_a_timeout_or_a_server_error_still_stops_the_shipment(
    machine: Machine, code: int
) -> None:
    _backlog(machine)
    server = Server(status(code, "later"))

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert len(server.calls) == 1 and not report.sent and report.error == f"HTTP {code}"
    assert sharing.read().failures == 1


def test_a_server_that_refuses_everything_is_asked_a_few_times_not_fourteen(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _backlog(machine)
    monkeypatch.setattr(usage_share, "REJECTED_IN_A_ROW", 2)
    server = Server(*[status(404, "not found")] * 5)

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    assert len(server.calls) == 3  # two in a row, then only the newest (8 days, with today)
    assert [len(batch) for batch in _dates(server)] == [31, 31, 8]
    assert not report.sent and report.error == "HTTP 404" and report.rejected_days == 70
    state = sharing.read()
    assert state.failures == 1 and state.acked == {} and state.next_due_at is not None


def test_after_three_refusals_the_newest_batch_with_today_is_still_tried(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review of #444 (nit): the three oldest batches refused kept today from going."""
    monkeypatch.setattr(usage_share, "WINDOW_DAYS", 14)  # 70 days: five batches
    _backlog(machine)
    server = Server(*[status(400, {"errors": {"days": ["no"]}})] * 3)

    report = usage_share.ship(now=NOW, tz=IST, transport=server)

    sent = _dates(server)
    assert [len(batch) for batch in sent] == [14, 14, 14, 14]  # the three oldest, then the newest
    assert sent[-1][-1] == NOW.astimezone(IST).date().isoformat()  # today
    assert report.sent and report.error is None and report.rejected_days == 42
    state = sharing.read()
    assert set(state.acked) == set(sent[-1])  # only what the server took
    assert state.settled_through is None  # and nothing is settled past a refused day
    again = Server()
    usage_share.ship(now=NOW + timedelta(hours=7), tz=IST, transport=again)
    assert _dates(again)[0] == sent[0]  # the refused days are asked again, oldest first


def test_a_day_the_server_refused_is_never_settled_past(machine: Machine) -> None:
    """Days older than the keep window are forgotten into `settled_through` once the server has
    them; one it has refused must stay to be asked again."""
    old = "2026-01-15"
    _on()
    sharing.update(lambda s: s.acked.update({old: "abc123abc123"}))
    batch = usage_share.Batch({}, {"2026-01-20": "d" * 64})

    usage_share._acknowledge(NOW, batch, False, held_from="2026-01-10")
    kept = sharing.read()
    usage_share._acknowledge(NOW, batch, False, held_from=None)
    settled = sharing.read()

    assert old in kept.acked and kept.settled_through is None
    assert old not in settled.acked and settled.settled_through == "2026-01-20"


# --- days on which sharing was forced off are never sent (review of #444, 4) ------------------


def test_a_day_spent_forced_off_is_never_sent_when_the_force_is_lifted(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    monkeypatch.setenv("DO_NOT_TRACK", "1")

    forced = usage_share.ship(now=NOW, tz=IST, transport=Server())
    monkeypatch.delenv("DO_NOT_TRACK")
    server = Server()
    usage_share.ship(now=NOW + timedelta(days=1), tz=IST, transport=server, force=True)

    assert forced.skipped == "sharing is off: DO_NOT_TRACK is set"
    withheld = sharing.read().withheld
    assert withheld == [NOW.astimezone(IST).date().isoformat()]
    sent = [day for batch in _dates(server) for day in batch]
    assert withheld[0] not in sent  # that day never goes
    assert sent == ["2026-10-24", "2026-10-25"]  # the other two days of the history


@pytest.mark.parametrize("variable", ["DO_NOT_TRACK", "AISQUARE_NO_TELEMETRY", "CI"])
def test_every_command_under_a_forced_off_run_withholds_its_day_once(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    _history(machine)
    _on()
    monkeypatch.setenv(variable, "1")

    first = sharing.start_background_ship(NOW)
    second = sharing.start_background_ship(NOW + timedelta(hours=1))

    assert first is False and second is False
    assert sharing.read().withheld == [NOW.astimezone().date().isoformat()]


def test_only_someone_who_turned_sharing_on_has_days_withheld(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", "1")

    sharing.start_background_ship(NOW)
    usage_share.ship(now=NOW, tz=IST, transport=Server())

    assert sharing.read().withheld == []
    assert state_core.read_state().get(sharing.STATE_KEY) is None  # nothing was written at all


def test_turning_sharing_off_forgets_the_withheld_days(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(machine)
    _on()
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    sharing.start_background_ship(NOW)
    monkeypatch.delenv("DO_NOT_TRACK")

    usage_share.disable(now=NOW, transport=Server())

    assert sharing.read().withheld == []
