"""Codex accounts: slots on the one ladder, signed in by ``codex login``, read offline.

``core.codex_accounts`` is Codex's answers to the accounts slot of its adapter;
``core.claude_accounts`` keeps the slots of both agents; the service resolves,
arranges, reads usage and hands over per agent. Everything here runs against
synthetic homes: an ``auth.json`` whose id token is an UNSIGNED JWT built below,
and rollouts written record by record in the shape Codex 0.154.0 / 0.162.1
records them (``event_msg`` / ``token_count`` / ``rate_limits``). Nothing reads
the developer's own ``~/.codex``, and no test may reach the network: Codex usage
is read from files, and :func:`no_network` fails any fetch that is attempted.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli import accounts as accounts_cli
from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import codex_accounts as codex
from aisquare.core import launch_seal
from aisquare.core.agent_adapters import adapters, get_adapter
from aisquare.core.agent_adapters import get_adapter as _get_adapter
from aisquare.core.config import AccountsSettings, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, FleetAgent, LaunchSpec, ProjectInfo
from aisquare.services import agent_events, hooks
from aisquare.services import claude_accounts as service
from aisquare.services import settings as settings_service
from tests.fakebin import executable_fake, prepend_to_path

CODEX = _get_adapter("codex")

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
_REAL_WHICH = shutil.which


# --------------------------------------------------------------------------- fixtures


def _part(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(email: str, *, plan: str = "plus", account_id: str = "acct-1") -> str:
    """An UNSIGNED id token with the claims Codex's carries (the signature is never checked)."""
    claims = {
        "email": email,
        "name": "Someone",
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan,
            "chatgpt_account_id": account_id,
        },
    }
    return f"{_part({'alg': 'none'})}.{_part(claims)}.not-a-signature"


def _sign_in(home: Path, email: str, *, plan: str = "plus") -> None:
    """What ``codex login`` leaves in a home (ChatGPT mode)."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": _jwt(email, plan=plan),
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "account_id": "acct-1",
                },
                "last_refresh": "2026-10-13T10:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _window(percent: float, minutes: int, resets_at: datetime) -> dict[str, Any]:
    return {
        "used_percent": percent,
        "window_minutes": minutes,
        "resets_at": int(resets_at.timestamp()),
    }


def _token_count(
    when: datetime,
    primary: dict[str, Any] | None,
    secondary: dict[str, Any] | None = None,
    *,
    reached: str | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": None,
            "rate_limits": {
                "limit_id": "codex",
                "limit_name": None,
                "primary": primary,
                "secondary": secondary,
                "credits": {"has_credits": True, "unlimited": False, "balance": None},
                "plan_type": "plus",
                "rate_limit_reached_type": reached,
            },
        },
    }


def _event(kind: str, when: datetime = NOW, **payload: Any) -> dict[str, Any]:
    return {
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {"type": kind, **payload},
    }


def _rollout(
    home: Path, records: list[dict[str, Any]], *, name: str = "a", age: timedelta | None = None
) -> Path:
    """A rollout under ``home`` holding ``records``; ``age`` back-dates its mtime."""
    path = home / "sessions" / "2026" / "10" / "13" / f"rollout-2026-10-13T10-00-00-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": name, "cli_version": "0.162.1"}}
    lines = [json.dumps(meta), *(json.dumps(record) for record in records)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if age is not None:
        stamp = (datetime.now(tz=UTC) - age).timestamp()
        os.utime(path, (stamp, stamp))
    return path


def _installed(monkeypatch: pytest.MonkeyPatch, **found: str | None) -> None:
    """Make each named binary resolve to its path (or to nothing); the rest stay real."""
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: found[name] if name in found else _REAL_WHICH(name, *a, **k),
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Any usage request fails the test: Codex usage is read from files only."""
    fetch = Mock(side_effect=AssertionError("a usage request was made"))
    monkeypatch.setattr(service, "_http_get", fetch)
    return fetch


@pytest.fixture
def home() -> Path:
    """The redirected home ``conftest.isolated_agent_home`` gives the accounts core."""
    redirected = core._home()
    redirected.mkdir(parents=True, exist_ok=True)
    return redirected


def _slot(account: ClaudeAccount | None) -> int:
    assert account is not None
    return account.slot


def _codex_slot(email: str | None = None) -> ClaudeAccount:
    account = core.create_account("codex")
    if email is not None:
        _sign_in(account.config_dir, email)
    return account


# --------------------------------------------------------------------------- core: the slots


def test_codex_slots_share_one_ladder_with_claude_and_keep_its_numbers(home: Path) -> None:
    claude_two = core.create_account()
    first_codex = core.create_account("codex")
    claude_four = core.create_account()

    assert (claude_two.slot, first_codex.slot, claude_four.slot) == (2, 3, 4)
    assert first_codex.config_dir == Path(os.environ["AISQUARE_HOME"]) / "codex-accounts" / "3"
    assert first_codex.tmp_dir is None and first_codex.agent == "codex"
    # The Claude slots are exactly what they were: directory, scratch dir, marker.
    assert claude_two.config_dir.parent.name == "claude-accounts" and claude_two.tmp_dir
    claude_marker = json.loads((claude_two.config_dir / core.MARKER).read_text())
    codex_marker = json.loads((first_codex.config_dir / core.MARKER).read_text())
    assert "agent" not in claude_marker and codex_marker["agent"] == "codex"
    assert [a.slot for a in core.managed_accounts()] == [2, 4]  # Claude's, as before
    assert [a.slot for a in core.managed_accounts("codex")] == [3]
    # A removed Codex slot frees its number for either agent.
    core.remove_account(first_codex)
    assert core.next_free_slot() == 3
    assert core.create_account().slot == 3


def test_the_plain_codex_is_slot_0_and_is_listed_once_its_home_exists(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert [a.slot for a in core.list_accounts()] == [1]  # no ~/.codex: no Codex row
    (home / ".codex").mkdir()
    plain = core.list_accounts()[-1]
    assert (plain.slot, plain.agent, plain.managed) == (0, "codex", False)
    assert plain.config_dir == home / ".codex" and core.label(plain) == "plain codex"
    assert core.slot_name(0) == "plain codex" and core.slot_name(1) == "plain claude"
    # CODEX_HOME moves the plain codex, as it moves a plain `codex` run.
    elsewhere = tmp_path / "other-codex"
    elsewhere.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(elsewhere))
    assert core.default_account(agent="codex").config_dir == elsewhere
    assert core.find_account(0) is not None


def test_a_codex_launch_sets_codex_home_and_keeps_the_shells_own(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    slot = _codex_slot()
    shell = {"CODEX_HOME": str(tmp_path / "shell-codex"), "PATH": "/bin"}

    env = core.apply_launch_env(dict(shell), slot, shell=shell)
    assert env["CODEX_HOME"] == str(slot.config_dir)
    assert env["AISQUARE_PLAIN_CODEX_HOME"] == str(tmp_path / "shell-codex")
    assert core.CONFIG_DIR_VAR not in env and core.TMPDIR_VAR not in env  # Claude's untouched

    # Under the slot, the plain codex is still the launching shell's.
    assert core.plain_environment(env, agent="codex") == {"CODEX_HOME": shell["CODEX_HOME"]}
    plain = core.apply_launch_env(dict(env), core.default_account(env, agent="codex"), shell=env)
    assert plain["CODEX_HOME"] == shell["CODEX_HOME"]
    assert "AISQUARE_PLAIN_CODEX_HOME" not in plain


# --------------------------------------------------------------------------- core: identity


def test_identity_is_read_from_the_id_token_claims_and_never_the_tokens(home: Path) -> None:
    slot = _codex_slot()
    assert core.identity(slot) is None and not core.signed_in(slot)
    _sign_in(slot.config_dir, "me@example.com", plan="pro")

    identity = core.identity(slot)
    assert identity is not None and identity.email == "me@example.com"
    assert identity.account_uuid == "acct-1"
    assert core.signed_in(slot) and codex.plan(slot) == "pro"
    described = service.describe(slot)
    assert described.subscription == "pro" and described.token_state == "ok"
    dumped = json.dumps(described.model_dump(mode="json"))
    assert "secret" not in dumped and "not-a-signature" not in dumped

    # An API-key login has no email; a login without tokens is not one Codex can use.
    (slot.config_dir / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-secret", "tokens": None})
    )
    api = core.identity(slot)
    assert api is not None and api.email == codex.API_KEY_IDENTITY and core.signed_in(slot)
    (slot.config_dir / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    assert core.identity(slot) is None and not core.signed_in(slot)
    (slot.config_dir / "auth.json").write_text("{not json")
    assert core.identity(slot) is None and not core.signed_in(slot)


# --------------------------------------------------------------------------- core: usage


def test_usage_is_the_newest_rollouts_token_count_with_windows_by_length(home: Path) -> None:
    slot = _codex_slot("me@example.com")
    assert not service.usage(slot, now=NOW).available  # no session yet
    _rollout(
        slot.config_dir,
        [_token_count(NOW - timedelta(hours=5), _window(90, 300, NOW + timedelta(hours=1)))],
        name="older",
        age=timedelta(hours=5),
    )
    newest = NOW - timedelta(minutes=20)
    _rollout(
        slot.config_dir,
        [
            _token_count(newest - timedelta(minutes=5), _window(10, 300, NOW + timedelta(hours=2))),
            _event("agent_message", message="done"),
            # The secondary is the SHORTER window here: placed by length, not position.
            _token_count(
                newest,
                _window(40, 10080, NOW + timedelta(days=3)),
                _window(25, 300, NOW + timedelta(hours=2)),
            ),
            _event("task_complete"),
        ],
        name="newest",
    )

    reading = service.usage(slot, now=NOW)

    assert reading.available
    assert (reading.session_percent, reading.week_percent) == (25.0, 40.0)
    assert reading.session_resets_at == datetime.fromtimestamp(
        int((NOW + timedelta(hours=2)).timestamp()), tz=UTC
    )
    assert reading.fetched_at == newest  # when the session recorded it
    assert service.headroom_percent(reading) == 40.0


def test_a_weekly_only_plan_and_a_window_that_has_reset_since(home: Path) -> None:
    slot = _codex_slot("me@example.com")
    _rollout(slot.config_dir, [_token_count(NOW, _window(30, 10080, NOW + timedelta(days=2)))])
    weekly = service.usage(slot, now=NOW)
    assert (weekly.session_percent, weekly.week_percent) == (None, 30.0)
    # Its plan has a week alone, which the pick ranks it by (``ClaudeUsage.windows``): only a
    # reading whose shortest window went unread (Claude's endpoint without its five hours)
    # is skipped.
    assert [w.name for w in weekly.windows] == ["week"]
    assert service.headroom_percent(weekly) == 30.0
    assert service.describe_headroom(slot, weekly) == "account 2 week 30% used (70% left)"

    later = NOW + timedelta(days=3)  # the window reset; nobody has used this home since
    reset = service.usage(slot, now=later)
    assert (reset.week_percent, reset.week_resets_at) == (0.0, None)


def test_a_long_session_is_read_from_its_end_past_the_first_chunk(home: Path) -> None:
    slot = _codex_slot("me@example.com")
    filler = [_event("agent_message", message="x" * 2000) for _ in range(300)]  # ~600 KiB
    _rollout(
        slot.config_dir,
        [_token_count(NOW, _window(55, 300, NOW + timedelta(hours=1))), *filler],
    )
    assert (slot.config_dir / "sessions").stat() and service.usage(
        slot, now=NOW
    ).session_percent == 55


# --------------------------------------------------------------------------- core: a limit


@pytest.mark.parametrize(
    ("records", "window", "resets"),
    [
        pytest.param(
            [
                _event("task_started"),
                _token_count(
                    NOW,
                    _window(100, 300, NOW + timedelta(hours=1)),
                    _window(60, 10080, NOW + timedelta(days=2)),
                    reached="rate_limit_reached",
                ),
            ],
            "session",
            NOW + timedelta(hours=1),
            id="rate_limit_reached_type",
        ),
        pytest.param(
            [
                _token_count(
                    NOW - timedelta(minutes=1),
                    _window(70, 300, NOW + timedelta(hours=1)),
                    _window(100, 10080, NOW + timedelta(days=2)),
                ),
                _event("task_started"),
                _event(
                    "error",
                    message="You've hit your usage limit. Try again at 3:15 PM.",
                    codex_error_info="usage_limit_reached",
                ),
            ],
            "weekly",
            NOW + timedelta(days=2),
            id="limit-error-event",
        ),
        pytest.param(
            [
                _event("task_started"),
                _event("error", message="You've hit your usage limit. Upgrade to Pro."),
            ],
            "usage",
            None,
            id="message-only",
        ),
        pytest.param(
            [
                _event("task_started"),
                _token_count(
                    NOW,
                    _window(20, 300, NOW + timedelta(hours=1)),
                    reached="workspace_member_credits_depleted",
                ),
            ],
            "credits",
            None,
            id="credits-depleted",
        ),
    ],
)
def test_a_limit_is_read_off_the_end_of_the_rollout(
    home: Path, records: list[dict[str, Any]], window: str, resets: datetime | None
) -> None:
    path = _rollout(home / "rollouts", records)
    notice = codex.limit_from_transcript(path, now=NOW)
    assert notice is not None and notice.window == window
    expected = datetime.fromtimestamp(int(resets.timestamp()), tz=UTC) if resets else None
    assert notice.resets_at == expected


def test_no_limit_without_one_this_turn_or_once_it_has_lifted(home: Path) -> None:
    healthy = _rollout(
        home / "rollouts",
        [_event("task_started"), _token_count(NOW, _window(40, 300, NOW + timedelta(hours=1)))],
        name="healthy",
    )
    assert codex.limit_from_transcript(healthy, now=NOW) is None
    # A limit error of an EARLIER turn, and a successful turn since: not limited now.
    recovered = _rollout(
        home / "rollouts",
        [
            _event("task_started"),
            _event(
                "error",
                message="You've hit your usage limit.",
                codex_error_info="usage_limit_reached",
            ),
            _event("task_started"),
            _token_count(NOW, _window(5, 300, NOW + timedelta(hours=5))),
        ],
        name="recovered",
    )
    assert codex.limit_from_transcript(recovered, now=NOW) is None
    # A recorded limit whose window has since reset is over.
    lifted = _rollout(
        home / "rollouts",
        [
            _event("task_started"),
            _token_count(
                NOW - timedelta(hours=6),
                _window(100, 300, NOW - timedelta(hours=1)),
                reached="rate_limit_reached",
            ),
        ],
        name="lifted",
    )
    assert codex.limit_from_transcript(lifted, now=NOW) is None
    assert codex.limit_from_transcript(home / "missing.jsonl", now=NOW) is None


SPEND_CAP = "You hit your spend cap set in your workspace. Increase your spend cap to continue."
"""The server's text for a team plan's limit, as 0.162.1 recorded it (SMOKE3)."""


def _at(when: datetime) -> datetime:
    """``when`` as a reading carries it: whole epoch seconds."""
    return datetime.fromtimestamp(int(when.timestamp()), tz=UTC)


def _premium(when: datetime) -> dict[str, Any]:
    """The reading 0.162.1 records at the limit after the account's own: ``limit_id``
    ``premium``, both windows ``null`` (SMOKE3, a team plan at its spend cap)."""
    record = _token_count(when, None, reached="workspace_owner_usage_limit_reached")
    record["payload"]["rate_limits"]["limit_id"] = "premium"
    return record


def _failed(
    info: Any, message: str = SPEND_CAP, when: datetime = NOW, **more: Any
) -> dict[str, Any]:
    """A turn's end as 0.162.1 records a failed one: its ``task_complete`` carries the error."""
    error = {"message": message, "codex_error_info": info}
    return _event("task_complete", when, last_agent_message=None, error=error, **more)


def test_at_the_limit_the_account_reads_the_window_before_the_windowless_premium_record(
    home: Path,
) -> None:
    slot = _codex_slot("full@example.com")
    resets, week = NOW + timedelta(hours=2), NOW + timedelta(days=5)
    path = _rollout(
        slot.config_dir,
        [
            _event("task_started"),
            _token_count(NOW, _window(100, 300, resets), _window(24, 10080, week)),
            _premium(NOW),
            _failed("usage_limit_exceeded", turn_id="turn-9"),
        ],
    )

    reading = service.usage(slot, now=NOW)
    assert reading.available and (reading.session_percent, reading.week_percent) == (100.0, 24.0)
    assert reading.session_resets_at == _at(resets)
    assert codex.limit_from_transcript(path, now=NOW) == core.LimitNotice("session", _at(resets))
    assert codex.failed_turn(path) == core.FailedTurn("turn-9", "rate_limit", SPEND_CAP)


def test_a_session_that_hit_the_limit_at_once_takes_the_window_another_session_recorded(
    home: Path,
) -> None:
    """The coder whose own last reading was 92 % (SMOKE3, account 2): the window's climb to
    100 % is another session's, written before it and before short sessions with none."""
    slot = _codex_slot("full@example.com")
    resets = NOW + timedelta(hours=2)
    drove = NOW - timedelta(minutes=8)
    _rollout(
        slot.config_dir,
        [_event("task_started", drove), _token_count(drove, _window(100, 300, resets))],
        name="drove",
        age=timedelta(minutes=8),
    )
    for n in range(codex.NEWEST_ROLLOUTS // 4):  # a /clear's, with no reading at all
        _rollout(
            slot.config_dir, [_event("task_started")], name=f"clear-{n}", age=timedelta(minutes=5)
        )
    earlier = NOW - timedelta(minutes=14)
    hit = _rollout(
        slot.config_dir,
        [
            _token_count(earlier, _window(92, 300, resets)),
            _event("task_complete", earlier),
            _event("task_started"),
            _premium(NOW),
            _failed("usage_limit_exceeded"),
        ],
        name="hit",
    )

    assert service.usage(slot, now=NOW).session_percent == 100.0
    assert codex.limit_from_transcript(hit, now=NOW) == core.LimitNotice("session", _at(resets))
    # No reading shows a full window: the limit is named, its window and reset are not known.
    third = _codex_slot("copy@example.com")
    alone = _rollout(third.config_dir, [*_records_of(hit)])
    assert codex.limit_from_transcript(alone, now=NOW) == core.LimitNotice("usage", None)
    assert service.usage(third, now=NOW).session_percent == 92.0


def _records_of(path: Path) -> list[dict[str, Any]]:
    """A rollout's records past its ``session_meta``, as :func:`_rollout` took them."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        pytest.param(
            [_event("task_started"), _failed("other", '{"error": "bad"}', turn_id="t")],
            core.FailedTurn("t", "other", '{"error": "bad"}'),
            id="any-other-error",
        ),
        pytest.param(
            [_event("task_started"), _failed({"http_connection_failed": {"status": 502}}, "x")],
            core.FailedTurn(None, "http_connection_failed", "x"),
            id="an-error-with-details",
        ),
        pytest.param(
            [_event("task_started"), _failed("other", "You've hit your usage limit.")],
            core.FailedTurn(None, "rate_limit", "You've hit your usage limit."),
            id="a-limit-by-its-message",
        ),
        pytest.param([_event("task_started"), _event("task_complete")], None, id="completed"),
        pytest.param([_event("task_started"), _event("turn_aborted")], None, id="interrupted"),
        pytest.param(
            [_event("task_started"), _failed("other"), _event("task_started")],
            None,
            id="a-newer-turn-runs",
        ),
        pytest.param([_event("task_started")], None, id="running"),
    ],
)
def test_a_failed_turn_is_the_newest_turns_task_complete_error(
    home: Path, records: list[dict[str, Any]], expected: core.FailedTurn | None
) -> None:
    path = _rollout(home / "rollouts", records)
    assert codex.failed_turn(path) == expected
    assert core.ClaudeCodeAccounts().failed_turn(path) is None  # Claude has StopFailure


def _turn_context(reviewer: str | None) -> dict[str, Any]:
    payload = {"approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"}}
    if reviewer is not None:
        payload["approvals_reviewer"] = reviewer
    return {"timestamp": NOW.isoformat(), "type": "turn_context", "payload": payload}


def test_the_reviewer_is_the_one_the_rollout_names_last(home: Path) -> None:
    """Who answers an approval, as 0.162.1 records it: each turn's ``turn_context``, and a
    ``thread_settings_applied`` event when ``/permissions`` changes it mid-thread."""
    rollouts = home / "rollouts"
    auto = _rollout(rollouts, [_turn_context("auto_review"), _event("task_started")], name="a")
    assert codex.approvals_reviewer(auto) == "auto_review"
    switched = _rollout(
        rollouts,
        [
            _turn_context("auto_review"),
            _event("thread_settings_applied", thread_settings={"approvals_reviewer": "user"}),
        ],
        name="switched",
    )
    assert codex.approvals_reviewer(switched) == "user"
    resumed = _rollout(
        rollouts,
        [
            _turn_context("user"),
            _event("thread_settings_applied", thread_settings={"approvals_reviewer": "user"}),
            _turn_context("auto_review"),
            _event("task_started"),
        ],
        name="resumed",
    )
    assert codex.approvals_reviewer(resumed) == "auto_review"
    assert codex.approvals_reviewer(_rollout(rollouts, [_turn_context(None)], name="n")) is None
    assert codex.approvals_reviewer(_rollout(rollouts, [_event("task_started")], name="e")) is None
    assert codex.approvals_reviewer(home / "missing.jsonl") is None


# --------------------------------------------------------------------------- the adapter slot


def test_every_adapter_declares_its_accounts_slot() -> None:
    claude = get_adapter("claude-code").accounts
    codex_slot = get_adapter("codex").accounts
    assert claude is not None and codex_slot is not None
    assert (claude.home_env, claude.extra_env, claude.plain_slot) == (
        "CLAUDE_CONFIG_DIR",
        ("CLAUDE_CODE_TMPDIR",),
        1,
    )
    assert (codex_slot.home_env, codex_slot.extra_env, codex_slot.plain_slot) == (
        "CODEX_HOME",
        (),
        0,
    )
    assert claude.sign_in_args() == () and codex_slot.sign_in_args() == ("login",)
    assert claude.network_usage and not codex_slot.network_usage
    plain = {a.accounts.plain_slot for a in adapters() if a.accounts is not None}
    assert len(plain) == len([a for a in adapters() if a.accounts is not None])  # never shared
    assert claude.limit_from_transcript(Path("anything")) is None  # Claude has StopFailure


# --------------------------------------------------------------------------- service: resolving


def test_a_launch_takes_only_its_own_agents_account_naming_both_otherwise(home: Path) -> None:
    claude = core.create_account()
    codex_slot = _codex_slot("me@example.com")

    # Third in the one order across agents: Claude Code's plain slot and slot 2 first.
    assert service.choose("3", agent="codex").account == codex_slot.model_copy(
        update={"position": 3}
    )
    with pytest.raises(service.WrongAgent, match=r"Claude Code account.*runs Codex"):
        service.choose(str(claude.slot), agent="codex")
    with pytest.raises(service.WrongAgent, match=r"Codex account.*runs Claude Code"):
        service.choose(str(codex_slot.slot))
    with pytest.raises(service.NoSuchAccount, match="no Codex account in slot 9"):
        service.choose("9", agent="codex")
    # A role binding naming the other agent's account stops the launch with the rung named.
    settings_service.bind_role("coder", account=str(claude.slot))
    with pytest.raises(service.NoSuchAccount, match=r"role binding for 'coder'.*runs Codex"):
        service.choose(None, role="coder", agent="codex")


def test_an_email_signed_in_to_both_agents_resolves_by_the_launching_agent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = core.create_account()
    (claude.config_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}})
    )
    codex_slot = _codex_slot("me@example.com")
    assert service.resolve("me@example.com", agent="codex").slot == codex_slot.slot
    assert service.resolve("me@example.com", agent="claude-code").slot == claude.slot
    with pytest.raises(service.NoSuchAccount, match=r"signed in to Claude Code .* and Codex"):
        service.resolve("me@example.com")


# --------------------------------------------------------------------------- service: arranging


def test_the_default_is_rank_one_and_a_claude_launch_never_sees_codexs(
    home: Path, tmp_path: Path
) -> None:
    claude = core.create_account()
    first = _codex_slot("a@example.com")
    second = _codex_slot("b@example.com")

    # The machine default is rank 1 of the one order, whatever its agent (0.9.0, #406).
    service.set_default(str(second.slot))
    assert _slot(service.machine_default()) == second.slot
    assert _slot(service.machine_default("codex")) == second.slot
    assert service.machine_default("claude-code") is None, "no Claude account is signed in"
    assert service.choose(None).account is None, "a Claude launch never sees Codex's"
    picked = service.choose(None, agent="codex")
    assert (_slot(picked.account), picked.source) == (second.slot, "preferred")

    project = team_project(tmp_path)
    with store_session() as store:
        store.ensure_project(project)
    service.set_default(str(first.slot), project=project)
    assert _slot(service.project_default(project, agent="codex")) == first.slot
    assert service.project_default(project) is None  # Claude's project default is untouched
    assert service.choose(None, project=project, agent="codex").source == "project default"

    with pytest.raises(service.AccountsError, match="cannot be cleared"):
        service.set_default(None, agent="codex")  # rank 1 is always somebody's
    service.set_default(None, project=project, agent="codex")
    assert service.project_default(project, agent="codex") is None
    with pytest.raises(service.WrongAgent):
        service.set_default(str(claude.slot), agent="codex")


def test_alias_disable_order_move_and_remove_work_on_codex_slots(home: Path) -> None:
    claude = core.create_account()
    first = _codex_slot("a@example.com")
    second = _codex_slot("b@example.com")

    service.set_alias(str(claude.slot), "work")
    with pytest.raises(service.AccountsError, match="already taken"):
        service.set_alias(str(first.slot), "work")  # one name across both agents
    named = service.set_alias(str(first.slot), "codex-work")
    assert named.alias == "codex-work" and service.resolve("codex-work").slot == first.slot

    assert service.set_disabled("codex-work", True).disabled
    # The preferred rung skips it: the first enabled, signed-in Codex account is the other.
    assert _slot(service.choose(None, agent="codex", spread=False).account) == second.slot

    order = service.move(str(second.slot), "top")
    codex_order = [a.slot for a in order if a.agent == "codex"]
    assert codex_order == [second.slot, first.slot]
    assert [a.slot for a in order if a.agent == "claude-code"] == [1, claude.slot]
    reordered = service.reorder([str(first.slot)])
    assert [a.slot for a in reordered if a.agent == "codex"] == [first.slot, second.slot]

    service.set_default(str(second.slot))
    moved = service.remove(core.find_account(second.slot) or second)
    assert (
        moved.name.startswith(f"{second.slot}.removed-") and moved.parent.name == "codex-accounts"
    )
    assert service.machine_default("codex") is None  # the one enabled slot went with it
    with pytest.raises(service.AccountsError, match="codex logout"):
        (home / ".codex").mkdir()
        service.remove(core.default_account(agent="codex"))


def test_headroom_picks_among_codex_accounts_offline(home: Path) -> None:
    full = _codex_slot("full@example.com")
    roomy = _codex_slot("roomy@example.com")
    _rollout(full.config_dir, [_token_count(NOW, _window(97, 10080, NOW + timedelta(days=2)))])
    _rollout(roomy.config_dir, [_token_count(NOW, _window(20, 300, NOW + timedelta(hours=2)))])
    core.create_account()  # a Claude slot: never a candidate for a Codex agent

    choice = service.choose_for_handover(
        None, role="coder", project=None, exclude=(full.slot,), automatic=True, agent="codex"
    )

    assert choice.account is not None and choice.account.slot == roomy.slot
    assert choice.source == "headroom"
    with store_session() as store:  # a Codex reading is not a sample: no trend from re-reads
        assert store.usage_samples(roomy.slot, since=NOW - timedelta(days=30)) == []


# --------------------------------------------------------------------------- the commands


@pytest.fixture
def stub_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``codex`` on PATH whose ``login`` writes an auth.json into ``$CODEX_HOME``."""
    helper = tmp_path / "stub-codex.py"
    helper.write_text(
        "import base64, json, os, sys\n"
        "def part(o):\n"
        "    return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b'=').decode()\n"
        "if sys.argv[1:2] == ['login']:\n"
        "    claims = {'email': 'stub@example.com',\n"
        "              'https://api.openai.com/auth': {'chatgpt_plan_type': 'plus'}}\n"
        "    token = part({'alg': 'none'}) + '.' + part(claims) + '.sig'\n"
        "    home = os.environ['CODEX_HOME']\n"
        "    with open(os.path.join(home, 'auth.json'), 'w') as f:\n"
        "        json.dump({'auth_mode': 'chatgpt', 'tokens': {'id_token': token,\n"
        "                   'refresh_token': 'r', 'access_token': 'a'}}, f)\n"
        "    print('signed in')\n",
        encoding="utf-8",
    )
    script = executable_fake(
        tmp_path / "codex-bin",
        "codex",
        posix=f'"{sys.executable}" "{helper}" "$@"',
        windows=f'"{sys.executable}" "{helper}" %*',
    )
    prepend_to_path(script.parent, monkeypatch)
    return script


def test_accounts_add_codex_runs_codex_login_in_the_slot_and_connects_its_hooks(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, stub_codex: Path
) -> None:
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)
    core.create_account()  # Claude's slot 2: the Codex one takes the next number

    result = runner.invoke(app, ["accounts", "add", "--agent", "codex"])

    assert result.exit_code == 0, result.output
    assert "Added Codex account 3: stub@example.com" in result.stderr
    # Naming the account names its agent: no --agent where the coder's binding pins none.
    assert "as a role: aisquare launch coder --account 3" in result.stderr
    [slot] = core.managed_accounts("codex")
    assert core.identity(slot) is not None and service.describe(slot).hooks_installed
    assert (slot.config_dir / "hooks.json").is_file()  # `agents connect codex --config-dir`

    unknown = runner.invoke(app, ["accounts", "add", "--agent", "aider"])
    assert unknown.exit_code == 1 and "choose claude-code, codex" in unknown.stderr


def test_accounts_add_codex_discards_a_slot_nothing_signed_into(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, stub_codex: Path
) -> None:
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)
    monkeypatch.setattr(service, "run_session", lambda account, *a, **k: 1)
    result = runner.invoke(app, ["accounts", "add", "--agent", "codex"])
    assert result.exit_code == 1 and "no sign-in landed in slot 2 (Codex exited" in result.stderr
    assert core.managed_accounts("codex") == []
    _installed(monkeypatch, codex=None)
    missing = runner.invoke(app, ["accounts", "add", "--agent", "codex"])
    assert missing.exit_code == 1 and "npm install -g @openai/codex" in missing.stderr


def test_accounts_list_and_usage_show_both_agents_with_codex_read_offline(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, no_network: Mock
) -> None:
    _installed(monkeypatch, claude="/opt/bin/claude", codex="/opt/bin/codex")
    slot = _codex_slot("me@example.com")
    _rollout(
        slot.config_dir,
        [
            _token_count(
                datetime.now(tz=UTC) - timedelta(hours=3),
                _window(12, 300, datetime.now(tz=UTC) + timedelta(hours=2)),
                _window(48, 10080, datetime.now(tz=UTC) + timedelta(days=2)),
            )
        ],
    )

    listed = runner.invoke(app, ["accounts", "list"])
    assert listed.exit_code == 0, listed.output
    assert "Codex · /opt/bin/codex" in listed.stdout
    row = next(line for line in listed.stdout.splitlines() if "me@example.com" in line)
    assert "Codex" in row and "plus" in row

    usage = runner.invoke(app, ["accounts", "usage", "2"])
    assert usage.exit_code == 0, usage.output
    assert "12%" in usage.stdout and "48%" in usage.stdout and "as of 3h ago" in usage.stdout
    as_json = runner.invoke(app, ["--json", "accounts", "list", "--usage"])
    [codex_row] = [
        r for r in json.loads(as_json.stdout)["accounts"] if r["account"]["agent"] == "codex"
    ]
    assert codex_row["usage"]["session_percent"] == 12.0
    assert no_network.call_count == 0


def test_accounts_run_execs_codex_in_the_slots_home(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, codex="/opt/bin/codex")
    slot = _codex_slot("me@example.com")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        accounts_cli, "_exec", lambda binary, argv, env: captured.update(argv=argv, env=env)
    )

    result = runner.invoke(app, ["accounts", "run", str(slot.slot), "login"])

    assert result.exit_code == 0, result.output
    assert captured["argv"] == ["/opt/bin/codex", "login"]
    assert captured["env"]["CODEX_HOME"] == str(slot.config_dir)
    assert core.CONFIG_DIR_VAR not in captured["env"]
    assert service.sign_in_command(slot)[-3:] == ["run", str(slot.slot), "login"]


def test_accounts_default_shows_rank_one_and_refuses_to_clear_it(
    home: Path, runner: CliRunner
) -> None:
    slot = _codex_slot("me@example.com")
    assert runner.invoke(app, ["accounts", "default", str(slot.slot)]).exit_code == 0
    shown = runner.invoke(app, ["accounts", "default"])
    assert f"machine default: slot {slot.slot}" in shown.stdout
    assert "Codex" in shown.stdout and "rank 1" in shown.stdout  # whatever its agent
    cleared = runner.invoke(app, ["accounts", "default", "--clear", "--agent", "codex"])
    assert cleared.exit_code != 0 and "cannot be cleared" in cleared.output
    assert service.machine_default("codex") is not None


# --------------------------------------------------------------------------- launch and spawn


def test_launch_and_spawn_run_a_codex_role_with_codex_home_on_its_slot(
    home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from importlib import import_module

    from aisquare.services import agent_launch, fleet

    slot = _codex_slot("me@example.com")
    claude = core.create_account()
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/opt/bin/codex")

    launched = runner.invoke(
        app, ["launch", "coder", "--agent", "codex", "--account", str(slot.slot)]
    )

    assert launched.exit_code == 0, launched.output
    env = execute.call_args.args[2]
    assert env["CODEX_HOME"] == str(slot.config_dir)
    assert core.CONFIG_DIR_VAR not in env or env[core.CONFIG_DIR_VAR] != str(claude.config_dir)

    server = Mock()
    monkeypatch.setattr(fleet, "server", lambda config: server)
    monkeypatch.setattr(fleet, "_require_tmux", lambda server: None)
    with pytest.raises(fleet.FleetError, match=r"Claude Code account.*runs Codex"):
        fleet.spawn(
            team_project(tmp_path), "coder", agent="codex", account=str(claude.slot), worktree=False
        )
    server.spawn_window.assert_not_called()


# --------------------------------------------------------------------------- the limit hook


@pytest.fixture
def team_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_capture: None) -> None:
    """The board as a fleet window has it (the environment tests/test_coding_agents.py uses),
    in a directory its hooks capture (``project_capture``: off is 0.8.1's default)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")


def _codex_fleet_row(
    tmp_path: Path, directory: Path, transcript: Path | None = None
) -> tuple[str, ProjectInfo]:
    """A Codex session started by the fleet under ``directory`` (its account's home), its
    start naming ``transcript`` as Codex's does (it fires with the first prompt)."""
    payload = {"session_id": "native-one", "cwd": str(tmp_path), "hook_event_name": "SessionStart"}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    agent_events.handle(CODEX, payload, directory)
    session_id = agent_events.session_key(CODEX, directory, "native-one")
    project = team_project(tmp_path)
    created_at = datetime.now(UTC) - timedelta(hours=1)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="fleet-one",
                project_id=project.id,
                role="coder",
                label="coder-1",
                binary="codex",
                agent="codex",
                tmux_socket="test",
                pane_id="%1",
                cwd=tmp_path,
                created_at=created_at,
                account_slot=core.managed_slot(directory, "codex"),
                session_id=session_id,
            )
        )
    # What its spawn and its own start hook would have written (`core.launch_seal`).
    window = launch_seal.WindowPlace(tmux_socket="test", pane_id="%1", created_at=created_at)
    assert launch_seal.placed("fleet-one", window) is None
    launch_seal.write(
        launch_seal.LaunchRecord(
            id="fleet-one",
            project_id=project.id,
            project_root=str(project.root),
            role="coder",
            agent="codex",
            binary="codex",
            cwd=str(project.root),
            account_slot=core.managed_slot(directory, "codex"),
            account_home=str(directory),
            spec=LaunchSpec(binary="codex"),
            session=launch_seal.SessionFacts(
                id=session_id, agent="codex", native_id="native-one", home=str(directory)
            ),
        )
    )
    return session_id, project


@pytest.mark.parametrize("event", ["Stop", "Interrupt"])
@pytest.mark.parametrize("shape", ["reached-type", "error-event"])
def test_a_codex_turn_that_hit_its_limit_parks_the_row_and_hands_over(
    home: Path,
    team_on: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    shape: str,
) -> None:
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet-one")
    limited = _codex_slot("full@example.com")
    _codex_slot("room@example.com")  # somewhere to go: one account waits (0.8.1)
    session_id, _project = _codex_fleet_row(tmp_path, limited.config_dir)
    resets = datetime.now(tz=UTC) + timedelta(hours=3)
    records: list[dict[str, Any]] = [_event("task_started")]
    if shape == "reached-type":
        records.append(
            _token_count(
                datetime.now(tz=UTC), _window(100, 300, resets), reached="rate_limit_reached"
            )
        )
    else:
        records += [
            _token_count(datetime.now(tz=UTC), _window(100, 300, resets)),
            _event(
                "error",
                message="You've hit your usage limit.",
                codex_error_info="usage_limit_reached",
            ),
        ]
    transcript = _rollout(limited.config_dir, records)
    config = load_config()
    config.accounts = AccountsSettings(on_limit="switch")
    save_config(config)
    detached: list[list[str]] = []
    monkeypatch.setattr(hooks, "_detach", detached.append)
    stopped = Mock(return_value=None)
    monkeypatch.setattr(hooks, "turn_stopped", stopped)

    payload = {
        "session_id": "native-one",
        "cwd": str(tmp_path),
        "hook_event_name": event,
        "transcript_path": str(transcript),
        "turn_id": "turn-1",
    }
    assert agent_events.handle(CODEX, payload, limited.config_dir) == ""

    with store_session() as store:
        session = store.get_session(session_id)
        kinds = [
            e.kind for e in store.recent_events(session.project_id if session else "", limit=5)
        ]
    assert session is not None and session.state == "limited"
    assert session.limit_resets_at == datetime.fromtimestamp(int(resets.timestamp()), tz=UTC)
    # The hand-over goes (another Codex slot has room), so the line says so and wakes no
    # manager: one switch per limit (#295's W3), Codex's as Claude Code's.
    assert "limited_handing_over" in kinds and "limited" not in kinds
    stopped.assert_not_called()  # the limit takes the Stop's place, as StopFailure does
    [argv] = detached
    assert argv[-4:-2] == ["hand-over", session_id] and argv[-1] == "session limit"


def test_only_the_agents_own_accounts_are_somewhere_to_hand_over(home: Path) -> None:
    limited = _codex_slot("full@example.com")
    claude = core.create_account()  # signed in: a Claude Code login, no Codex one
    (claude.config_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "me@example.com"}}), encoding="utf-8"
    )
    (claude.config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "test-token"}}), encoding="utf-8"
    )
    assert core.signed_in(claude)
    # The hook asks with the row's agent: a Claude Code account is no room for Codex.
    assert not service.somewhere_to_hand_over(limited.slot, agent=CODEX.id)
    assert service.somewhere_to_hand_over(limited.slot)  # every agent's, as main asks
    _codex_slot("room@example.com")
    assert service.somewhere_to_hand_over(limited.slot, agent=CODEX.id)


def test_a_codex_session_ending_on_its_limit_records_it_without_a_hand_over(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet-one")
    limited = _codex_slot("full@example.com")
    session_id, _project = _codex_fleet_row(tmp_path, limited.config_dir)
    transcript = _rollout(
        limited.config_dir,
        [_event("task_started"), _event("error", message="You've hit your usage limit.")],
    )
    config = load_config()
    config.accounts = AccountsSettings(on_limit="switch")
    save_config(config)
    detached: list[list[str]] = []
    monkeypatch.setattr(hooks, "_detach", detached.append)

    payload = {
        "session_id": "native-one",
        "cwd": str(tmp_path),
        "hook_event_name": "SessionEnd",
        "transcript_path": str(transcript),
    }
    agent_events.handle(CODEX, payload, limited.config_dir)

    with store_session() as store:
        session = store.get_session(session_id)
        kinds = [
            e.kind for e in store.recent_events(session.project_id if session else "", limit=5)
        ]
    assert session is not None and session.ended_at is not None and "limited" in kinds
    assert detached == []


def test_a_healthy_codex_stop_is_handled_as_it_always_was(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = _codex_slot("me@example.com")
    session_id, _project = _codex_fleet_row(tmp_path, slot.config_dir)
    transcript = _rollout(
        slot.config_dir,
        [_event("task_started"), _token_count(NOW, _window(40, 300, NOW + timedelta(hours=1)))],
    )
    failed = Mock()
    monkeypatch.setattr(hooks, "turn_failed", failed)
    payload = {
        "session_id": "native-one",
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }
    agent_events.handle(CODEX, payload, slot.config_dir)
    failed.assert_not_called()
    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None and session.state == "waiting"


@pytest.mark.parametrize(
    ("info", "message", "state", "kind"),
    [
        # Handed over (another Codex slot has room): one switch per limit, said once and
        # waking no manager (#295's W3).
        pytest.param(
            "usage_limit_exceeded", SPEND_CAP, "limited", "limited_handing_over", id="limit"
        ),
        pytest.param("other", "fixture says no", "waiting", "turn_failed", id="other-error"),
    ],
)
def test_a_codex_turn_that_failed_with_no_stop_reaches_the_board_on_the_fleets_read(
    home: Path,
    team_on: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    info: str,
    message: str,
    state: str,
    kind: str,
) -> None:
    """SMOKE3: Codex 0.162.1 fires only the prompt's hook for a turn that fails, so the row
    read working for good. The fleet's read of the row finds the failure at the end of
    its rollout and records it as Claude Code's StopFailure is, once."""
    from aisquare.services import fleet

    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet-one")
    slot = _codex_slot("full@example.com")
    _codex_slot("room@example.com")  # somewhere to go: one account waits (0.8.1)
    resets = datetime.now(tz=UTC) + timedelta(hours=3)
    ended = datetime.now(tz=UTC) + timedelta(minutes=1)  # a minute after its prompt, below
    transcript = _rollout(
        slot.config_dir,
        [
            _event("task_started", turn_id="turn-1"),
            _token_count(datetime.now(tz=UTC), _window(100, 300, resets)),
            _premium(datetime.now(tz=UTC)),
            _failed(info, message, ended, turn_id="turn-1"),
        ],
    )
    session_id, project = _codex_fleet_row(tmp_path, slot.config_dir, transcript)
    prompt = {
        "session_id": "native-one",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Reply with exactly: LIMIT-PROBE",
        "transcript_path": str(transcript),
        "turn_id": "turn-1",
    }
    agent_events.handle(CODEX, prompt, slot.config_dir)  # the one hook the turn fires
    config = load_config()
    config.accounts = AccountsSettings(on_limit="switch")
    save_config(config)
    detached: list[list[str]] = []
    monkeypatch.setattr(hooks, "_detach", detached.append)
    _running_on_screen(monkeypatch)
    with store_session() as store:
        assert (row := store.get_session(session_id)) is not None and row.state == "working"
        agent = store.get_fleet_agent("fleet-one")
    assert agent is not None
    assert fleet.status_of(agent).state == state
    assert [status.state for status in fleet.list_agents(project)] == [state]

    with store_session() as store:
        session = store.get_session(session_id)
        kinds = [e.kind for e in store.recent_events(project.id, limit=10)]
    assert session is not None and session.state == state
    assert kinds.count(kind) == 1, kinds  # told once, though read twice
    if state == "limited":
        assert session.limit_resets_at == _at(resets)
        [argv] = detached
        assert argv[-4:-2] == ["hand-over", session_id] and argv[-1] == "session limit"
    else:
        assert detached == []
    # A reader holding the row as it was before (another listing at the same moment)
    # claims the turn too late and records nothing.
    assert row is not None and not agent_events.failed_turn("fleet-one", row)


def _running_on_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fleet's tmux answers for every row, its agent running in its pane: the reader is
    outside any sandbox (a sandboxed one is refused tmux)."""
    from aisquare.services import fleet

    def observe(
        placements: Any, also: Any = (), reads: Any = None, tick: Any = None
    ) -> dict[str, Any]:
        return {
            placement.row.id: fleet._PaneView(False, None, "codex", None)
            for placement in placements
        }

    monkeypatch.setattr(fleet, "_observe", observe)


def test_a_codex_turn_still_running_is_not_read_as_failed(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import fleet

    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet-one")
    slot = _codex_slot("me@example.com")
    # The previous turn failed; the one the prompt below began is running.
    transcript = _rollout(
        slot.config_dir,
        [_event("task_started"), _failed("usage_limit_exceeded"), _event("task_started")],
    )
    _session_id, project = _codex_fleet_row(tmp_path, slot.config_dir, transcript)
    prompt = {
        "session_id": "native-one",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "carry on",
        "transcript_path": str(transcript),
        "turn_id": "turn-2",
    }
    agent_events.handle(CODEX, prompt, slot.config_dir)
    failed = Mock()
    monkeypatch.setattr(hooks, "turn_failed", failed)
    _running_on_screen(monkeypatch)

    [status] = fleet.list_agents(project)

    failed.assert_not_called()
    assert status.session is not None and status.session.state == "working"


def _working_codex_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, records: list[dict[str, Any]]
) -> tuple[str, ProjectInfo, Path, list[list[str]]]:
    """A fleet Codex row whose prompt began a turn, its rollout holding ``records`` after
    that turn's start; ``on_limit = "switch"``, the hand-overs it detaches collected."""
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet-one")
    slot = _codex_slot("full@example.com")
    _codex_slot("room@example.com")  # somewhere to go: one account waits (0.8.1)
    transcript = _rollout(slot.config_dir, [_event("task_started", turn_id="turn-1"), *records])
    session_id, project = _codex_fleet_row(tmp_path, slot.config_dir, transcript)
    prompt = {
        "session_id": "native-one",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "carry on",
        "transcript_path": str(transcript),
        "turn_id": "turn-1",
    }
    agent_events.handle(CODEX, prompt, slot.config_dir)
    config = load_config()
    config.accounts = AccountsSettings(on_limit="switch")
    save_config(config)
    detached: list[list[str]] = []
    monkeypatch.setattr(hooks, "_detach", detached.append)
    return session_id, project, transcript, detached


def _planted_limit(tmp_path: Path) -> Path:
    """A rollout a sandboxed agent wrote: its last turn failed on the usage limit."""
    planted = tmp_path / "planted" / "rollout-planted.jsonl"
    planted.parent.mkdir()
    ended = datetime.now(tz=UTC) + timedelta(minutes=1)
    records = [_event("task_started"), _failed("usage_limit_exceeded", when=ended)]
    planted.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return planted


def test_a_transcript_the_store_names_never_parks_a_row_limited(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r5 of #113, finding 2: the fleet's read took the transcript it acts on from
    the board session's row, which a sandbox writes: pointed at a rollout of its making
    that ends in a limit, the next listing parked the row limited, told the manager and
    detached a hand-over. It reads the transcript the agent's own hook reported."""
    from aisquare.services import fleet

    session_id, project, _, detached = _working_codex_row(tmp_path, monkeypatch, [])
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE team_session SET transcript_path = ?, agent = 'codex' WHERE id = ?",
            (str(_planted_limit(tmp_path)), session_id),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    _running_on_screen(monkeypatch)

    assert [status.state for status in fleet.list_agents(project)] == ["working"]

    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=10)]
    assert "limited" not in kinds and "turn_failed" not in kinds and detached == []


def test_a_hooks_limit_reads_the_records_transcript_not_the_stores(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r5 finding 2's other half: a stop whose payload names no transcript read the board
    session's ``transcript_path`` for its limit. It reads the launch record's."""
    session_id, _project, transcript, detached = _working_codex_row(tmp_path, monkeypatch, [])
    planted = _planted_limit(tmp_path)
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE team_session SET transcript_path = ? WHERE id = ?", (str(planted), session_id)
        )
        store._conn.commit()  # type: ignore[attr-defined]
    record = launch_seal.read("fleet-one")
    assert isinstance(record, launch_seal.LaunchRecord) and record.session is not None
    assert record.session.transcript == str(transcript)
    slot_home = transcript.parents[4]
    stop = {"session_id": "native-one", "cwd": str(tmp_path), "hook_event_name": "Stop"}

    agent_events.handle(CODEX, stop, slot_home)

    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None and session.state == "waiting" and detached == []
    # The record's own transcript is still read: one that ends in a limit parks the row.
    transcript.write_text(planted.read_text(encoding="utf-8"), encoding="utf-8")
    agent_events.handle(CODEX, {**stop, "turn_id": "turn-2"}, slot_home)
    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None and session.state == "limited"


def test_a_failed_turn_is_left_to_a_reader_outside_the_sandbox(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r5 of #113, finding 3: a Codex manager's ``fleet ls`` or ``tell`` runs in its
    sandbox, where tmux is refused, and the first reader claimed the failed turn: its
    hand-over ran in that sandbox, ended "not switched", and nothing retried it. A reader
    tmux did not answer records nothing; the next one outside a sandbox does, once."""
    from aisquare.services import fleet

    ended = datetime.now(tz=UTC) + timedelta(minutes=1)
    failed = [_failed("usage_limit_exceeded", when=ended, turn_id="turn-1")]
    session_id, project, _, detached = _working_codex_row(tmp_path, monkeypatch, failed)
    monkeypatch.setattr(fleet, "_observe", lambda *a, **k: None)  # tmux refused: a sandbox
    with store_session() as store:
        agent = store.get_fleet_agent("fleet-one")
    assert agent is not None

    assert fleet.status_of(agent).state != "limited"
    assert [status.state for status in fleet.list_agents(project)] != ["limited"]
    with store_session() as store:
        session = store.get_session(session_id)
        claimed = store.get_meta(f"agent-event:{session_id}:turn_failed:turn-1")
    assert session is not None and session.state == "working" and claimed is None
    assert detached == []

    _running_on_screen(monkeypatch)  # the app's tick, a human's `fleet ls`
    assert [status.state for status in fleet.list_agents(project)] == ["limited"]
    [argv] = detached
    assert argv[-4:-2] == ["hand-over", session_id]


def test_a_failure_recorded_before_the_running_turns_prompt_is_not_its_end(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r5 of #113, finding 7: until Codex writes the new turn's ``task_started``,
    the rollout ends in the previous turn's failure; a listing in that gap recorded it
    against the turn now running, flipped the working row, and on a limit handed a
    working agent over. A failure older than the session's last hook is not recorded."""
    from aisquare.services import fleet

    before = datetime.now(tz=UTC) - timedelta(minutes=5)  # the previous turn's end
    failed = [_failed("usage_limit_exceeded", when=before, turn_id="turn-0")]
    _, project, _, detached = _working_codex_row(tmp_path, monkeypatch, failed)
    _running_on_screen(monkeypatch)

    assert [status.state for status in fleet.list_agents(project)] == ["working"]

    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=10)]
    assert "limited" not in kinds and detached == []


def test_the_homes_search_reads_each_rollouts_end_and_only_once_until_one_is_written(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r5 of #113, finding 6: an account with no windowed reading (an API key, the
    limit's windowless reading alone) read twenty rollouts back to 32 MB each on every
    ``usage()``: the Accounts page, Welcome's look, the hand-over's headroom, and the
    app's tick through a failed turn's limit. Each is read back a bounded span, and the
    home is not read again until one of its rollouts is written."""
    slot = _codex_slot("key@example.com")
    for n in range(codex.NEWEST_ROLLOUTS + 5):
        _rollout(slot.config_dir, [_premium(NOW)], name=f"r{n}", age=timedelta(minutes=n))
    spans: list[int] = []
    real = codex._records_from_end

    def reading(path: Path, *, limit: int = codex.MAX_SCAN_BYTES) -> Any:
        spans.append(limit)
        return real(path, limit=limit)

    monkeypatch.setattr(codex, "_records_from_end", reading)

    assert codex.usage(slot).available is False
    assert len(spans) == codex.NEWEST_ROLLOUTS and set(spans) == {codex.HOME_SCAN_BYTES}
    assert codex.HOME_SCAN_BYTES < codex.MAX_SCAN_BYTES
    spans.clear()
    assert codex.usage(slot).available is False
    assert spans == [], "nothing under the home was written since"
    _rollout(slot.config_dir, [_premium(NOW), _token_count(NOW, _window(40, 300, NOW))], name="r0")
    assert codex.usage(slot).available is True
    assert spans, "a rollout written since is read"


# --------------------------------------------------------------------------- the hand-over


def test_a_codex_switch_goes_fresh_to_the_codex_account_with_room(
    home: Path, team_on: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import fleet

    full = _codex_slot("full@example.com")
    roomy = _codex_slot("roomy@example.com")
    core.create_account()  # Claude's: never the target of a Codex agent
    _rollout(roomy.config_dir, [_token_count(NOW, _window(10, 300, NOW + timedelta(hours=4)))])
    _session_id, project = _codex_fleet_row(tmp_path, full.config_dir)
    with store_session() as store:
        agent = store.get_fleet_agent("fleet-one")
    assert agent is not None
    monkeypatch.setattr(fleet, "_live_agent", lambda store, project, label, agent_id=None: agent)
    monkeypatch.setattr(fleet, "_require_role", lambda role: None)
    monkeypatch.setattr(
        fleet,
        "_refuse_a_replay_that_cannot_start",
        lambda project, record, home=None, permission_mode=None, branch=None: None,
    )
    monkeypatch.setattr(fleet, "_mark_handing_over", lambda *a, **k: None)
    monkeypatch.setattr(fleet, "_unmark_handing_over", lambda session: None)
    monkeypatch.setattr(fleet, "stop", lambda *a, **k: Mock(agent=agent, withheld=False))
    respawned: dict[str, Any] = {}

    def respawn(project: ProjectInfo, stopped: FleetAgent, *a: Any, **k: Any) -> Any:
        respawned.update(k)
        receipt = Mock(failures=[], prompt_typed="yes", tmux_session="asq", agent=stopped)
        return receipt, False, []

    monkeypatch.setattr(fleet, "_respawn", respawn)

    receipt = fleet.switch(project, "coder-1", reason="session limit", automatic=True)

    assert (receipt.from_slot, receipt.to_slot) == (full.slot, roomy.slot)
    assert respawned["account"] == str(roomy.slot) and respawned["fresh"] is True
    assert "under another Codex account" in respawned["reason"]
    assert any("cannot resume a thread" in note for note in receipt.notes)


def test_team_bind_checks_the_account_against_the_roles_agent(
    home: Path, runner: CliRunner
) -> None:
    claude = core.create_account()
    slot = _codex_slot("me@example.com")
    refused = runner.invoke(
        app, ["team", "bind", "coder", "--agent", "codex", "--account", str(claude.slot)]
    )
    assert refused.exit_code == 1
    assert "Claude Code account" in refused.stderr and "runs Codex" in refused.stderr
    bound = runner.invoke(
        app, ["team", "bind", "coder", "--agent", "codex", "--account", str(slot.slot)]
    )
    assert bound.exit_code == 0, bound.output
    assert _slot(service.choose(None, role="coder", agent="codex").account) == slot.slot
