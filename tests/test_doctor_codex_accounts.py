"""The doctor's account rows, per agent: Codex slots get rows of their own, read offline.

Claude Code's rows (``claude-accounts``, ``claude-account-default``,
``claude-account-headroom``, ``claude-account-limits``) are pinned by
``tests/test_account_defaults.py`` and ``tests/test_usage_aware_accounts.py``;
this file holds their Codex counterparts and the control beside each: a Codex
slot never reaches a Claude row, and no Codex row ever sends a request — its
usage is the newest ``token_count`` its own sessions recorded.

Every test runs in a redirected home (``fake_home`` from tests/conftest.py): the
Codex homes below are slots under the test's own ``AISQUARE_HOME``, written
with a synthetic ``auth.json`` (an unsigned id token carrying claims only) and a
synthetic rollout. Nothing reads the developer's ``~/.codex``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo, TeamSession
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics
from aisquare.services import settings as settings_service
from tests.test_claude_accounts import _sign_in

# `fake_home`, the redirected home, is tests/conftest.py's since 0.8.1.

CODEX = "codex"


# --------------------------------------------------------------------------- helpers


def _b64(payload: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _codex_sign_in(account: ClaudeAccount, email: str, *, plan: str = "plus") -> None:
    """What ``codex login`` leaves in a home: ``auth.json`` with an (unsigned) id token."""
    claims = {
        "email": email,
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan,
            "chatgpt_account_id": f"acct-{account.slot}",
        },
    }
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig",
            "access_token": "not-a-real-token",
            "refresh_token": "x",
            "account_id": f"acct-{account.slot}",
        },
        "last_refresh": "2026-10-13T09:00:00Z",
    }
    (account.config_dir / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def _window(percent: float, minutes: int, *, hours: float = 3.0) -> dict[str, Any]:
    resets = datetime.now(tz=UTC) + timedelta(hours=hours)
    return {
        "used_percent": percent,
        "window_minutes": minutes,
        "resets_at": int(resets.timestamp()),
    }


def _rollout(
    account: ClaudeAccount,
    primary: dict[str, Any] | None,
    secondary: dict[str, Any] | None = None,
) -> Path:
    """A session that recorded ``rate_limits`` once, as Codex writes a ``token_count``."""
    folder = account.config_dir / "sessions" / "2026" / "10" / "13"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "rollout-2026-10-13T10-00-00-0199e0a0-0000-7000-8000-000000000001.jsonl"
    stamp = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    records = [
        {"timestamp": stamp, "type": "session_meta", "payload": {"id": "s", "cli_version": "x"}},
        {"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_started"}},
        {
            "timestamp": stamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": None,
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": primary,
                    "secondary": secondary,
                    "credits": {"has_credits": True, "unlimited": False, "balance": None},
                    "plan_type": "plus",
                    "rate_limit_reached_type": None,
                },
            },
        },
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _codex_slot(email: str | None, *, primary: dict[str, Any] | None = None) -> ClaudeAccount:
    account = core.create_account(CODEX)
    if email is not None:
        _codex_sign_in(account, email)
    if primary is not None:
        _rollout(account, primary)
    return account


def _store() -> None:
    """Make ``context.db``: every account row is gated on it, as a doctor run creates nothing."""
    with store_session():
        pass


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every request the accounts service would send; a Codex row must send none."""
    calls: list[str] = []

    def refuse(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        calls.append(url)
        raise AssertionError(f"a Codex doctor row reached the network: {url}")

    monkeypatch.setattr(service, "_http_get", refuse)
    return calls


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    project = team_project(root)
    with store_session() as store:
        store.ensure_project(project)
    return project


# --------------------------------------------------------------------------- the slots row


def test_codex_slots_get_a_row_of_their_own_with_identity_and_the_codex_sign_in(
    fake_home: Path, no_network: list[str]
) -> None:
    signed = _codex_slot("dev@example.com")
    unsigned = _codex_slot(None)
    _store()

    checks = {check.name: check for check in diagnostics._other_accounts_checks()}

    row = checks["codex-accounts"]
    assert row.status.value == "warn"
    assert "2 added beside the plain codex" in row.detail
    assert f"{signed.slot} dev@example.com" in row.detail
    assert f"{unsigned.slot} not signed in" in row.detail
    assert row.fix is not None and f"aisquare accounts run {unsigned.slot} login" in row.fix
    # A Codex slot is not one of Claude Code's: its row is unchanged (absent: none added).
    assert "claude-accounts" not in {c.name for c in diagnostics._claude_accounts_checks()}

    _codex_sign_in(unsigned, "second@example.com")
    [ok] = [c for c in diagnostics._other_accounts_checks() if c.name == "codex-accounts"]
    assert ok.status.value == "ok" and "second@example.com" in ok.detail
    assert no_network == []


def test_a_machine_without_codex_slots_keeps_the_claude_rows_it_had(
    fake_home: Path, no_network: list[str]
) -> None:
    """The control: Claude slots only — the Codex rows are absent, the Claude row as before."""
    _sign_in(core.create_account(), "two@example.com")
    _store()

    assert diagnostics._other_accounts_checks() == []
    assert diagnostics._other_accounts_headroom_checks() == []
    [claude] = diagnostics._claude_accounts_checks()
    assert claude.name == "claude-accounts" and claude.status.value == "ok"
    assert claude.detail == "1 added beside the plain claude: 2 two@example.com"


# --------------------------------------------------------------------------- headroom


def test_codex_headroom_is_read_offline_on_a_plain_doctor_run(
    fake_home: Path, no_network: list[str]
) -> None:
    """No ``--live`` and no request: the windows come from each slot's own rollout. A plan with
    ONE weekly window (measured on a business plan) is measured on it, not skipped."""
    weekly = _codex_slot("weekly@example.com", primary=_window(30, 10080, hours=72))
    both = _codex_slot("both@example.com")
    _rollout(both, _window(95, 300), _window(50, 10080, hours=72))
    _store()

    check = diagnostics._agent_account_headroom_check(CODEX)

    assert check is not None and check.name == "codex-account-headroom"
    assert check.status.value == "ok"
    assert "percent USED (85% used is the line)" in check.detail, "as the Claude row says it"
    assert f"account {weekly.slot} week 30% used (70% left)" in check.detail
    assert f"account {both.slot} 95%" in check.detail
    names = {c.name for c in diagnostics.doctor()}
    assert "codex-account-headroom" in names
    assert "claude-account-headroom" not in names  # Claude Code's still waits for --live
    assert no_network == []


def test_codex_headroom_warns_when_every_codex_account_is_over_the_line(
    fake_home: Path, no_network: list[str]
) -> None:
    _codex_slot("a@example.com", primary=_window(90, 300))
    _codex_slot("b@example.com", primary=_window(99, 10080, hours=48))
    _store()

    check = diagnostics._agent_account_headroom_check(CODEX)

    assert check is not None and check.status.value == "warn"
    assert "every Codex account has used 85% or more of a window" in check.detail
    assert "at or over" not in check.detail, "USED, as the Claude row says it"
    assert check.fix is not None and "aisquare accounts add --agent codex" in check.fix
    assert no_network == []


def test_codex_headroom_says_so_when_no_session_recorded_limits_and_is_absent_without_logins(
    fake_home: Path, no_network: list[str]
) -> None:
    _store()
    _codex_slot(None)  # never signed in: nothing to measure, no row
    assert diagnostics._agent_account_headroom_check(CODEX) is None

    _codex_slot("fresh@example.com")  # signed in, no session yet
    check = diagnostics._agent_account_headroom_check(CODEX)
    assert check is not None and check.status.value == "warn"
    assert "no Codex account's usage has been recorded yet" in check.detail
    assert no_network == []


def test_codex_headroom_creates_no_store(fake_home: Path, no_network: list[str]) -> None:
    from aisquare.core import paths

    _codex_slot("a@example.com", primary=_window(10, 300))
    assert diagnostics._agent_account_headroom_check(CODEX) is None
    assert not paths.db_path().exists()


# --------------------------------------------------------------------------- defaults and bindings


def test_codex_defaults_are_checked_apart_from_claude_codes(
    fake_home: Path, work: ProjectInfo, no_network: list[str]
) -> None:
    codex = _codex_slot("dev@example.com")
    unsigned = _codex_slot(None)
    names = {check.name for check in diagnostics._other_accounts_checks()}
    assert names == {"codex-accounts"}  # nothing arranged: no default line

    service.set_default(str(codex.slot))
    checks = {check.name: check for check in diagnostics._other_accounts_checks()}
    default = checks["codex-account-default"]
    assert default.status.value == "ok"
    assert default.detail == f"Codex machine default: slot {codex.slot} (account {codex.slot})"
    # Claude Code has no default of its own, and a Codex one is not it.
    assert diagnostics._claude_accounts_checks() == []

    service.set_default(str(unsigned.slot))
    checks = {check.name: check for check in diagnostics._other_accounts_checks()}
    default = checks["codex-account-default"]
    assert default.status.value == "warn" and "not signed in" in default.detail
    assert default.fix is not None and f"aisquare accounts run {unsigned.slot} login" in default.fix

    service.set_default(str(codex.slot))
    service.set_disabled(str(codex.slot), True)
    checks = {check.name: check for check in diagnostics._other_accounts_checks()}
    assert checks["codex-account-default"].status.value == "warn"
    assert "disabled" in checks["codex-account-default"].detail


def test_a_codex_roles_binding_and_project_default_are_checked_against_codex_accounts(
    fake_home: Path, work: ProjectInfo, no_network: list[str]
) -> None:
    claude = core.create_account()
    _sign_in(claude, "two@example.com")
    codex = _codex_slot("dev@example.com")
    settings_service.bind_role("coder", agent=CODEX, account=str(claude.slot))  # another agent's
    settings_service.bind_role("reviewer", agent=CODEX, account="99")  # no such slot
    service.set_default(str(codex.slot), project=work)
    with store_session() as store:
        store.set_project_setting(work.id, service.project_account_key(CODEX), "98")

    checks = {check.name: check for check in diagnostics._other_accounts_checks()}

    dangling = checks["codex-account-bindings"]
    assert dangling.status.value == "warn"
    assert f"role coder → {claude.slot}" in dangling.detail
    assert "role reviewer → 99" in dangling.detail
    assert f"project {work.root.name} → slot 98" in dangling.detail
    assert dangling.fix is not None and "--agent codex" in dangling.fix
    # The Claude rows do not check a Codex role's binding, and see nothing dangling.
    assert "claude-account-bindings" not in {c.name for c in diagnostics._claude_accounts_checks()}


# --------------------------------------------------------------------------- limits


def _limited(project: ProjectInfo, session_id: str, label: str, agent: str | None) -> None:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role="coder",
                agent=agent,
                started_at=now,
                last_seen_at=now,
                transcript_path=f"/tmp/{session_id}.jsonl",
            )
        )
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{session_id}",
                project_id=project.id,
                label=label,
                role="coder",
                pane_id="%2",
                session_id=session_id,
                cwd=project.root,
                created_at=now,
            )
        )
        store.mark_limited(session_id, now + timedelta(hours=2))


def test_a_limited_codex_agent_is_named_on_a_codex_row_not_the_claude_one(
    fake_home: Path, work: ProjectInfo, no_network: list[str]
) -> None:
    _limited(work, "sess-codex", "coder-codex", CODEX)

    checks = {check.name: check for check in diagnostics._other_accounts_checks()}
    limits = checks["codex-account-limits"]
    assert limits.status.value == "warn"
    assert "parked on a Codex usage limit" in limits.detail and "coder-codex" in limits.detail
    # The command for the row, as the Claude row names its own (0.8.1: `fleet_command`).
    assert limits.fix is not None and "aisquare fleet switch --project" in limits.fix
    assert "coder-codex" in limits.fix
    assert "claude-account-limits" not in {c.name for c in diagnostics._claude_accounts_checks()}

    _limited(work, "sess-claude", "coder-claude", None)  # a row that names no agent is Claude's
    [claude] = [
        c for c in diagnostics._claude_accounts_checks() if c.name == "claude-account-limits"
    ]
    assert "parked on a Claude usage limit" in claude.detail
    assert "coder-claude" in claude.detail and "coder-codex" not in claude.detail
