"""Opt-in anonymous usage sharing: the switch, the batch, the shipment, the privacy of the body.

No test reaches a server: the shipper's transport is a fake (``tests/usage_share_support.Server``),
and the AISquare-Studio-BE endpoint it stands for is a separate PR. The suite forces sharing off
for every test (``AISQUARE_NO_TELEMETRY``); these take that off with ``sharing_allowed``.
"""

from __future__ import annotations

import json
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import feature_use, locking, sharing
from aisquare.core import state_file as state_core
from aisquare.core.store import store_session
from aisquare.models import DailyRollup, DayAccounts, TokenCounts
from aisquare.services import iam, usage_share, usage_stats
from tests.projects import add_project
from tests.usage_share_support import Server, check_contract, status, unreachable
from tests.usage_stats_support import IST, NOW, Machine, pin_clock, prompt, request, tokens

pytestmark = pytest.mark.usefixtures("sharing_allowed")

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


def _on() -> None:
    usage_share.enable(now=NOW)


# --- off, and forced off ----------


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


# --- the identity and the hashes ----------


def test_the_install_id_is_minted_at_the_first_opt_in_and_kept_across_off_and_on(
    machine: Machine,
) -> None:
    assert usage_share.status().install_id is None
    _on()
    first = usage_share.status().install_id
    usage_share.disable(now=NOW)
    usage_share.enable(now=NOW)
    again = usage_share.status().install_id
    changed = usage_share.reset_identity()[0]

    assert first is not None and again == first
    assert changed != first and usage_share.status().install_id == changed


def test_a_repository_hash_is_stable_for_an_install_and_different_for_another(
    machine: Machine,
) -> None:
    _history(machine)
    _on()
    one = _build().days[-1]["repos"][0]["repo_hash"]
    again = _build().days[-1]["repos"][0]["repo_hash"]
    usage_share.reset_identity()  # another installation: its own salt
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


# --- the body ----------


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


def test_a_names_only_field_cannot_carry_text(machine: Machine) -> None:
    """Models and plans are free text in a transcript or a login: they are sent as a short
    token of safe characters, whatever they were."""
    rollup = DailyRollup(
        date="2026-10-26",
        tz_offset_minutes=0,
        models={"claude opus/5 'quoted' <b>": TokenCounts(input=1)},
        accounts=DayAccounts(by_type={"a very\nlong plan name " * 5: 1}),
    )
    day = usage_share.day_body(rollup, "ab" * 32, signed_in=False)

    assert list(day["models"]) == ["claudeopus5quotedb"]
    assert list(day["accounts"]["by_type"]) == [("averylongplanname" * 5)[:32]]


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
    _on()

    days = _build().days
    assert days[0]["date"] == "2026-09-26" and days[-1]["date"] == "2026-10-26"
    assert len(days) == 31

    monkeypatch.setattr(usage_share, "MAX_BODY_BYTES", 6_000)
    cut = _build()
    assert len(json.dumps(cut.body)) <= 6_000 and cut.days[-1]["date"] == "2026-10-26"
    assert len(cut.days) < 31 and list(cut.digests) == [d["date"] for d in cut.days]


# --- the shipment ----------


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

    assert first.error == "HTTP 503" and second.error == "HTTP 200"
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


# --- turning it off ----------


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


# --- the consent ----------


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


# --- the detached shipper ----------


def test_a_command_starts_one_shipper_when_due_and_none_for_half_an_hour(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on()
    started: list[int] = []
    monkeypatch.setattr(sharing, "_spawn", lambda: started.append(1))

    first = sharing.start_background_ship(NOW)
    second = sharing.start_background_ship(NOW + timedelta(minutes=5))
    third = sharing.start_background_ship(NOW + timedelta(minutes=31))
    usage_share._schedule(NOW + timedelta(minutes=31), success=True)  # a shipment went through
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
    began = time.perf_counter()
    for _ in range(200):
        sharing.start_background_ship(NOW)
    per_call = (time.perf_counter() - began) / 200

    assert per_call < 0.005, f"{per_call * 1e6:.0f} µs a command"


# --- the commands ----------


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
    assert len(sent["days"]) >= 3 and sent["install_id"] == on["install_id"]
    assert "Sharing is ON" in text and "not signed in: batches are anonymous" in text
    assert json.loads(off.output)["opt_out"] is True and len(server.calls) == 2
    assert server.calls[1].body["opt_out"] is True


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
