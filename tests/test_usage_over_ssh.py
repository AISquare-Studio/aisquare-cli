"""asq over SSH into the Mac shows the usage the Mac's own asq recorded (task #18).

The owner runs asq on the Mac and, at the same time, over SSH into the same Mac (one home,
one store). The SSH session's Keychain reads say locked, so its asq could fingerprint no
slot's token, was served no reading, and said "accounts: headroom <acc> skipped - login
keychain is locked" while the Mac's asq kept every reading fresh beside it. The owner's
choice (2026-10-30): read what the Mac recorded, never prompt for an unlock.

Each test plays both processes on one store: :class:`TwoAsqs` is the one Keychain, read by
the Mac and locked over SSH, and a switch between them forgets what a process holds in
memory, as a second ``aisquare`` process starts with nothing but the store.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.accounts import account_cells, kept_tooltip, via_tooltip
from aisquare.core import claude_accounts as core
from aisquare.core import paths
from aisquare.models import ClaudeAccount, ClaudeUsage, UsageCause
from aisquare.services import claude_accounts as service
from aisquare.statusline import FLEET_FILE
from tests.test_claude_accounts import NOW, _installed, _sign_in
from tests.test_usage_aware_accounts import _payload, _Usage
from tests.test_usage_backoff import _new_process as _forget_usage_memory

SSH = "192.0.2.7 52144 192.0.2.1 22"
VIA = "via the Mac's asq"
LOCKED_SSH = "the login Keychain is locked in this SSH session"


def _new_process() -> None:
    """Forget what this process holds in memory, as a second ``aisquare`` process starts
    with nothing but the store: the usage cache's, and the logins it said lately."""
    _forget_usage_memory()
    with service._NOTED_LOCK:
        service._NOTED.clear()


def _creds(token: str) -> core.ClaudeCredentials:
    """What a slot's item holds once Claude Code signed in with ``token``."""
    return core.ClaudeCredentials(
        access_token=token,
        expires_at=NOW + timedelta(hours=7),
        subscription_type="max",
        rate_limit_tier="default_claude_max_5x",
    )


class TwoAsqs:
    """The Mac's login Keychain, as ``core.read_keychain`` answers it: each slot's item holds
    the token its Claude Code signed in with; read by the Mac's asq, locked over SSH. A read
    is remembered until one asks ``fresh`` or a new process starts, as the core's memo is
    for ``KEYCHAIN_MEMO_SECONDS``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.items: dict[str, str] = {}
        self.remembered: dict[str, str | None] = {}
        self.locked = False
        self.reads: list[str] = []
        monkeypatch.setattr(core, "keychain_platform", lambda: True)
        monkeypatch.setattr(core, "read_keychain", self.read)

    def login(self, account: ClaudeAccount, token: str) -> None:
        """Claude Code signed ``account`` in: its item holds ``token``."""
        self.items[core.keychain_service(account)] = token

    def read(self, service_name: str, account: str, *, fresh: bool = False) -> core.KeychainRead:
        self.reads.append(service_name)
        if self.locked:
            return core.KeychainRead(found=False, unreadable=core.KEYCHAIN_LOCKED, problem="locked")
        if fresh or service_name not in self.remembered:
            self.remembered[service_name] = self.items.get(service_name)
        token = self.remembered[service_name]
        if token is None:
            return core.KeychainRead(found=False)
        return core.KeychainRead(found=True, credentials=_creds(token))

    def on_the_mac(self) -> None:
        """The asq on the Mac's own screen: its Keychain reads."""
        _new_process()
        self.remembered.clear()
        self.locked = False
        self.monkeypatch.delenv("SSH_CONNECTION", raising=False)

    def over_ssh(self) -> None:
        """An asq in an SSH login into the same Mac: the Keychain says locked."""
        _new_process()
        self.remembered.clear()
        self.locked = True
        self.monkeypatch.setenv("SSH_CONNECTION", SSH)


@pytest.fixture
def asqs(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> TwoAsqs:
    two = TwoAsqs(monkeypatch)
    two.on_the_mac()
    return two


def _slots(asqs: TwoAsqs, count: int = 2) -> list[ClaudeAccount]:
    """``count`` managed slots, each signed in on the Mac with ``tok-<slot>``."""
    made = []
    for _ in range(count):
        account = core.create_account()
        _sign_in(account, f"slot{account.slot}@example.com", credentials=False)
        asqs.login(account, f"tok-{account.slot}")
        made.append(account)
    return made


class NoFetch(_Usage):
    """The endpoint an SSH session must never reach: it has no token to send."""

    def __init__(self) -> None:
        super().__init__({})

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        raise AssertionError(f"a locked slot was fetched with {headers['Authorization']!r}")


def _mac_reads(
    asqs: TwoAsqs, accounts: list[ClaudeAccount], at: datetime, **percents: float
) -> dict[int, ClaudeUsage]:
    """The Mac's asq reads ``accounts`` at ``at``, each by the token its item holds now: slot
    ``n`` at ``percents['s<n>']``."""
    asqs.on_the_mac()
    fetch = _Usage(
        {
            asqs.items[core.keychain_service(account)]: _payload(percents[f"s{account.slot}"])
            for account in accounts
        }
    )
    readings = service.cached_usage(accounts, now=at, fetch=fetch)
    assert all(reading.available for reading in readings.values())
    assert not any(reading.recorded_elsewhere for reading in readings.values()), (
        "a reading the Mac took itself is its own"
    )
    return readings


def _store_rows() -> dict[str, list[tuple[Any, ...]]]:
    """Every usage sample and every ui_state row, as the store holds them."""
    with sqlite3.connect(paths.db_path()) as db:
        return {
            "claude_usage": sorted(db.execute("SELECT * FROM claude_usage").fetchall()),
            "ui_state": sorted(db.execute("SELECT * FROM ui_state").fetchall()),
        }


# --- 1. a fresh reading the Mac recorded is what every surface over SSH shows --------------


def test_an_ssh_session_is_served_what_the_macs_asq_recorded_and_says_so(asqs: TwoAsqs) -> None:
    two, three = _slots(asqs)
    _mac_reads(asqs, [two, three], NOW - timedelta(minutes=2), s2=40.0, s3=70.0)

    asqs.over_ssh()
    fetch = NoFetch()
    readings = service.cached_usage([two, three], now=NOW, fetch=fetch)

    assert {slot: r.session_percent for slot, r in readings.items()} == {2: 40.0, 3: 70.0}
    for reading in readings.values():
        assert reading.available and reading.recorded_elsewhere and not reading.kept
        assert reading.fetched_at == NOW - timedelta(minutes=2)
        assert service.describe_via(reading, now=NOW) == f"{VIA} · 2m ago"
    # ↻ Refresh usage over SSH: a fresh look at what the Mac recorded, never a request.
    forced = service.cached_usage([two, three], now=NOW, force=True, fetch=fetch)
    assert {slot: r.session_percent for slot, r in forced.items()} == {2: 40.0, 3: 70.0}
    assert all(r.recorded_elsewhere and not r.kept for r in forced.values())


def test_the_accounts_page_marks_the_macs_reading_and_says_whose_it_is_on_hover(
    asqs: TwoAsqs,
) -> None:
    [two] = _slots(asqs, 1)
    _mac_reads(asqs, [two], NOW - timedelta(minutes=2), s2=40.0)

    asqs.over_ssh()
    reading = service.cached_usage([two], now=NOW, fetch=NoFetch())[two.slot]
    status = service.describe(two)

    assert status.token_state == "unreadable"  # the row still knows its Keychain is locked
    assert service.usage_problem(status, reading) is None, "a figure, not the '!'"
    session = account_cells(status, reading, now=NOW)["session"].plain
    assert session == "▮▮▯▯▯ 40% · Mac"
    assert via_tooltip(reading, now=NOW) == f"{VIA} · 2m ago — {LOCKED_SSH}"
    assert kept_tooltip(reading, now=NOW) is None


def test_the_status_lines_headroom_over_ssh_is_the_macs(asqs: TwoAsqs) -> None:
    two, three = _slots(asqs)
    _mac_reads(asqs, [two, three], NOW - timedelta(minutes=2), s2=40.0, s3=70.0)

    asqs.over_ssh()
    summary = service.read_fleet_headroom([two, three], now=NOW, fetch=NoFetch())

    assert summary.level != "empty" and summary.usable == 2
    assert summary.session_by_slot == {2: 40.0, 3: 70.0}


def test_accounts_usage_over_ssh_prints_the_macs_figures_with_their_age(
    asqs: TwoAsqs, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    two, three = _slots(asqs)
    _mac_reads(asqs, [two, three], NOW - timedelta(minutes=2), s2=40.0, s3=70.0)

    asqs.over_ssh()
    monkeypatch.setattr(service, "_http_get", NoFetch())
    result = runner.invoke(app, ["accounts", "usage", "2"])
    assert result.exit_code == 0, result.output
    assert "40%" in result.output
    assert f"account 2: {VIA} · 2m ago ({LOCKED_SSH})" in result.output
    assert "skipped" not in result.output

    listed = runner.invoke(app, ["--json", "accounts", "usage"])
    assert listed.exit_code == 0, listed.output
    rows = {row["account"]["slot"]: row["usage"] for row in json.loads(listed.stdout)}
    assert {slot: rows[slot]["session_percent"] for slot in (2, 3)} == {2: 40.0, 3: 70.0}
    assert all(rows[slot]["recorded_elsewhere"] for slot in (2, 3))


def test_a_launch_over_ssh_picks_by_the_macs_reading_inside_the_picks_minute(
    asqs: TwoAsqs,
) -> None:
    two, three = _slots(asqs)
    _mac_reads(asqs, [two, three], NOW - timedelta(seconds=30), s2=90.0, s3=10.0)

    asqs.over_ssh()
    picked, notes = service.headroom_choice(
        [two, three],
        switch_at=80,
        max_age=service.PICK_MAX_AGE_SECONDS,
        now=NOW,
        fetch=NoFetch(),
    )

    assert picked is not None and picked.slot == three.slot, notes
    assert f"headroom: account 2 read {VIA} · just now" in notes
    assert not any("skipped" in note or "locked" in note for note in notes), notes


# --- 2. nothing in the window: the aged last good figure, and the SSH fix ----------------


def test_with_nothing_fresh_the_last_good_reading_is_kept_and_the_fix_is_the_macs_asq(
    asqs: TwoAsqs,
) -> None:
    [two] = _slots(asqs, 1)
    _mac_reads(asqs, [two], NOW - timedelta(minutes=20), s2=40.0)

    asqs.over_ssh()
    fetch = NoFetch()
    reading = service.cached_usage([two], now=NOW, fetch=fetch)[two.slot]

    assert reading.available and reading.kept and reading.recorded_elsewhere
    assert reading.session_percent == 40.0
    assert reading.fetched_at == NOW - timedelta(minutes=20)
    assert reading.last_failed_reason == LOCKED_SSH
    assert reading.last_failed_cause is UsageCause.keychain_locked_ssh
    fix = service.kept_fix_line(reading, slot=two.slot)
    assert fix is not None
    assert "open asq on the Mac itself" in fix and "security unlock-keychain" in fix
    assert kept_tooltip(reading, now=NOW) == (f"reading from 20m ago, {VIA} · {LOCKED_SSH}\n{fix}")
    # An aged figure is not the fleet's room (review of #408, MAJ-1), here as anywhere.
    assert service.fleet_headroom([two], {two.slot: reading}, now=NOW).usable == 0


def test_with_nothing_on_record_the_row_says_the_keychain_is_locked_over_ssh(
    asqs: TwoAsqs,
) -> None:
    [two] = _slots(asqs, 1)

    asqs.over_ssh()
    reading = service.cached_usage([two], now=NOW, fetch=NoFetch())[two.slot]

    assert not reading.available and not reading.recorded_elsewhere
    assert (reading.cause, reading.reason) == (UsageCause.keychain_locked_ssh, LOCKED_SSH)
    assert reading.fix == (
        "open asq on the Mac itself, which records usage for this session too; "
        "or run security unlock-keychain here, then ↻ Refresh usage (or --refresh)"
    )
    assert not reading.needs_login, "a log-in would not unlock this session's Keychain"


def test_a_locked_keychain_on_the_macs_own_screen_keeps_its_own_words(asqs: TwoAsqs) -> None:
    [two] = _slots(asqs, 1)
    asqs.locked = True  # locked by hand on the Mac itself: not an SSH session

    reading = service.cached_usage([two], now=NOW, fetch=NoFetch())[two.slot]

    assert reading.cause is UsageCause.keychain_locked
    assert reading.reason == core.KEYCHAIN_LOCKED_REASON


def test_a_launch_over_ssh_never_picks_by_a_reading_older_than_the_picks_minute(
    asqs: TwoAsqs,
) -> None:
    """The kept rule of today (review of #408, MAJ-1): a launch picks only by a reading
    inside its own minute, PICK_MAX_AGE_SECONDS. Over SSH nothing can fetch a newer one,
    so an older reading of the Mac's is skipped with its age, and the default decides."""
    two, three = _slots(asqs)
    _mac_reads(asqs, [two, three], NOW - timedelta(minutes=2), s2=90.0, s3=10.0)

    asqs.over_ssh()
    picked, notes = service.headroom_choice(
        [two, three],
        switch_at=80,
        max_age=service.PICK_MAX_AGE_SECONDS,
        now=NOW,
        fetch=NoFetch(),
    )

    assert picked is None, notes
    assert f"headroom: account 3 skipped — {LOCKED_SSH}; its last reading is from 2m ago, " in (
        "\n".join(notes)
    )
    assert notes[-1] == "headroom: no account's usage could be read — the default decides"


# --- 3. a locked process writes nothing another process is served ------------------------


def test_an_ssh_session_writes_nothing_that_could_hide_the_macs_reading(
    asqs: TwoAsqs, monkeypatch: pytest.MonkeyPatch
) -> None:
    two, three = _slots(asqs)
    service.statusline_dir().mkdir(parents=True, exist_ok=True)
    _mac_reads(asqs, [two, three], NOW - timedelta(minutes=2), s2=40.0, s3=70.0)
    service.read_fleet_headroom([two, three], now=NOW - timedelta(minutes=2))
    fleet = (service.statusline_dir() / FLEET_FILE).read_text()
    before = _store_rows()

    asqs.over_ssh()
    later = NOW + timedelta(minutes=20)  # nothing in the window: every read here fails
    for force in (False, True):
        service.cached_usage([two, three], now=later, force=force, fetch=NoFetch())
    service.read_fleet_headroom([two, three], now=later, fetch=NoFetch())
    service.headroom_choice(
        [two, three],
        switch_at=80,
        max_age=service.PICK_MAX_AGE_SECONDS,
        now=later,
        fetch=NoFetch(),
    )
    assert service._UNANSWERED == {}, "a locked slot's failure is remembered nowhere"

    assert _store_rows() == before, "no sample, back-off or login row written over SSH"
    assert (service.statusline_dir() / FLEET_FILE).read_text() == fleet, (
        "the SSH session's blind figure is not put over the Mac's fleet.json"
    )
    # The Mac, a minute later, is still served its own reading: nothing hid it.
    asqs.on_the_mac()
    endpoint = _Usage({"tok-2": _payload(99.0), "tok-3": _payload(99.0)})
    again = service.cached_usage([two, three], now=NOW - timedelta(minutes=1), fetch=endpoint)
    assert {slot: r.session_percent for slot, r in again.items()} == {2: 40.0, 3: 70.0}
    assert endpoint.calls == []


def test_an_ssh_session_with_a_fresh_reading_writes_the_fleets_figure(asqs: TwoAsqs) -> None:
    two, three = _slots(asqs)
    service.statusline_dir().mkdir(parents=True, exist_ok=True)
    _mac_reads(asqs, [two, three], NOW - timedelta(minutes=2), s2=40.0, s3=70.0)

    asqs.over_ssh()
    service.read_fleet_headroom([two, three], now=NOW, fetch=NoFetch())

    written = json.loads((service.statusline_dir() / FLEET_FILE).read_text())
    assert written["percent_left"] == pytest.approx(45.0) and written["accounts"] == 2


# --- 5. a login the Mac saw replaced is never shown the last one's figure ----------------


def test_a_slot_the_mac_saw_signed_into_another_login_is_not_shown_the_last_ones_figure(
    asqs: TwoAsqs,
) -> None:
    [two] = _slots(asqs, 1)
    _mac_reads(asqs, [two], NOW - timedelta(minutes=3), s2=40.0)

    # Signed into another login on the Mac; its first read there fails (the endpoint is
    # down), so the store holds no reading of the new login, only the old one's.
    asqs.on_the_mac()
    asqs.login(two, "tok-2-other")
    down = service.cached_usage(
        [two], now=NOW - timedelta(minutes=1), fetch=_Usage({"tok-2-other": 500})
    )
    assert not down[two.slot].available

    asqs.over_ssh()
    reading = service.cached_usage([two], now=NOW, fetch=NoFetch())[two.slot]
    assert not reading.available, "the last login's 40% is not this login's"
    assert reading.cause is UsageCause.keychain_locked_ssh

    # Once the Mac reads the new login, the SSH session shows that.
    _mac_reads(asqs, [two], NOW + timedelta(minutes=1), s2=5.0)
    asqs.over_ssh()
    later = service.cached_usage([two], now=NOW + timedelta(minutes=2), fetch=NoFetch())
    assert later[two.slot].session_percent == 5.0 and later[two.slot].recorded_elsewhere


def test_a_slot_the_mac_saw_signed_out_shows_nothing_over_ssh(asqs: TwoAsqs) -> None:
    [two] = _slots(asqs, 1)
    _mac_reads(asqs, [two], NOW - timedelta(minutes=3), s2=40.0)
    asqs.on_the_mac()
    del asqs.items[core.keychain_service(two)]  # /logout: the item holds no login
    service.cached_usage([two], now=NOW - timedelta(minutes=1), fetch=_Usage({}))

    asqs.over_ssh()
    reading = service.cached_usage([two], now=NOW, fetch=NoFetch())[two.slot]
    assert not reading.available and not reading.recorded_elsewhere


def test_the_login_row_is_written_only_when_the_mac_sees_another_login(asqs: TwoAsqs) -> None:
    [two] = _slots(asqs, 1)
    key = f"usage-login:{two.slot}"
    _mac_reads(asqs, [two], NOW - timedelta(minutes=10), s2=40.0)
    first = _store_rows()["ui_state"]
    row = next(value for name, value, *_ in first if name == key)
    assert json.loads(row)["token"] is not None

    _mac_reads(asqs, [two], NOW, s2=45.0)  # the same login, read again
    assert [r for r in _store_rows()["ui_state"] if r[0] == key] == [
        r for r in first if r[0] == key
    ], "the same login is not written again"

    asqs.login(two, "tok-2-other")
    asqs.on_the_mac()
    service.cached_usage([two], now=NOW + timedelta(minutes=1), fetch=_Usage({"tok-2-other": 500}))
    changed = next(v for name, v, *_ in _store_rows()["ui_state"] if name == key)
    assert json.loads(changed)["token"] != json.loads(row)["token"]


def test_a_login_row_this_version_cannot_read_serves_nothing() -> None:
    assert service.SeenLogin.load("{truncated") is None
    assert service.SeenLogin.load(json.dumps({"token": 7, "seen_at": NOW.isoformat()})) is None
    seen = service.SeenLogin(token="fp", seen_at=NOW)
    assert service.SeenLogin.load(seen.dump()) == seen


def test_over_ssh_is_read_from_the_logins_environment() -> None:
    assert core.over_ssh({"SSH_CONNECTION": SSH})
    assert core.over_ssh({"SSH_TTY": "/dev/ttys004"})
    assert not core.over_ssh({"TERM": "xterm-256color"})
    assert not core.over_ssh({"SSH_CONNECTION": ""})


# --- 5 (reopened): a /logout the Mac sees is a logout over SSH, on every surface ----------


def _logged_out_on_the_mac(
    asqs: TwoAsqs, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> tuple[ClaudeAccount, ClaudeAccount]:
    """The tester's repro of the reopen (seq 5074), through the CLI as the owner runs it: the
    Mac reads slots 2 (90 %) and 3 (10 %), then slot 3 is logged out (``/logout`` keeps
    ``.claude.json``'s account and drops the token), and the Mac's ``accounts usage
    --refresh`` lists every slot, 3 among them, though only signed-in ones are read."""
    _installed(monkeypatch)
    two, three = _slots(asqs)
    monkeypatch.setattr(
        service, "_http_get", _Usage({"tok-2": _payload(90.0), "tok-3": _payload(10.0)})
    )
    assert runner.invoke(app, ["accounts", "usage"]).exit_code == 0
    del asqs.items[core.keychain_service(three)]
    asqs.on_the_mac()
    mac = runner.invoke(app, ["--json", "accounts", "usage", "--refresh"])
    assert mac.exit_code == 0, mac.output
    assert {row["account"]["slot"]: row["signed_in"] for row in json.loads(mac.stdout)}[3] is False
    asqs.over_ssh()
    monkeypatch.setattr(service, "_http_get", NoFetch())
    return two, three


def test_a_logout_the_mac_saw_is_a_logout_over_ssh_in_accounts_usage(
    asqs: TwoAsqs, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _logged_out_on_the_mac(asqs, runner, monkeypatch)

    listed = runner.invoke(app, ["--json", "accounts", "usage"])
    assert listed.exit_code == 0, listed.output
    rows = {row["account"]["slot"]: row for row in json.loads(listed.stdout)}
    assert rows[3]["signed_in"] is False and rows[3]["token_state"] == "missing"
    assert rows[3]["usage"]["available"] is False and rows[3]["usage"]["session_percent"] is None
    assert rows[3]["usage"]["cause"] == UsageCause.token_missing.value
    assert rows[2]["usage"]["session_percent"] == 90.0 and rows[2]["usage"]["recorded_elsewhere"]

    table = runner.invoke(app, ["accounts", "usage"])
    assert table.exit_code == 0, table.output
    [row] = [line for line in table.output.splitlines() if line.startswith("3 ")]
    assert "10%" not in row and "no login in the" in row, row  # the reason wraps on
    assert f"account 3: {VIA}" not in table.output


def test_a_logout_the_mac_saw_is_a_logout_over_ssh_for_the_page_line_and_pick(
    asqs: TwoAsqs, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    two, three = _logged_out_on_the_mac(asqs, runner, monkeypatch)
    accounts = service.list_accounts()

    # The Accounts page reads its rows from the overview, and asks usage of signed-in ones.
    rows = {status.account.slot: status for status in service.overview().accounts}
    assert not rows[3].signed_in and rows[3].token_state == "missing"
    assert rows[2].signed_in and rows[2].token_state == "unreadable"
    problem = service.usage_problem(rows[3], None)
    assert problem is not None and problem.cause is UsageCause.token_missing
    # The status line's headroom.
    summary = service.read_fleet_headroom(fetch=NoFetch())
    assert set(summary.session_by_slot) == {two.slot}
    # The launch pick, and the default rung under it.
    picked, notes = service.headroom_choice(
        accounts, switch_at=85, max_age=service.PICK_MAX_AGE_SECONDS, fetch=NoFetch()
    )
    assert picked is not None and picked.slot == two.slot, notes
    preferred = service.preferred_account(accounts)
    assert preferred is not None and preferred.slot == two.slot
    assert not service.is_signed_in(three) and service.is_signed_in(two)
    # A hook's look, and the room on record for a hand-over off slot 2.
    assert service.line_reading(three.slot, switch_at=85, on_record=True) is None
    assert not service.room_on_record(two.slot, switch_at=85)


def test_signing_back_in_on_the_mac_is_signed_in_over_ssh_again(
    asqs: TwoAsqs, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two, three = _logged_out_on_the_mac(asqs, runner, monkeypatch)
    asqs.login(three, "tok-3-again")
    asqs.on_the_mac()
    assert service.overview().accounts  # the Mac's asq looks at every slot on its tick

    asqs.over_ssh()
    assert service.is_signed_in(three)
    status = next(s for s in service.overview().accounts if s.account.slot == three.slot)
    assert status.signed_in and status.token_state == "unreadable"


def test_forgetting_a_slot_forgets_the_login_seen_there(asqs: TwoAsqs) -> None:
    [two] = _slots(asqs, 1)
    _mac_reads(asqs, [two], NOW - timedelta(minutes=2), s2=40.0)
    key = f"usage-login:{two.slot}"
    assert any(row[0] == key for row in _store_rows()["ui_state"])

    service.forget_arrangement(two.slot)

    assert not any(row[0] == key for row in _store_rows()["ui_state"])


def test_a_login_remembered_from_before_is_not_written_over_a_newer_one(
    asqs: TwoAsqs,
) -> None:
    """Process A read slot 2's login a few seconds ago and still remembers it (the core's
    ten-second memo); slot 2 was signed into another login since, which process B read and
    said in the row. A, told nothing, must not write its older login back: over SSH that
    would serve the last login's figure for the new one."""
    [two] = _slots(asqs, 1)
    _mac_reads(asqs, [two], NOW - timedelta(minutes=2), s2=40.0)  # A remembers tok-2
    key = f"usage-login:{two.slot}"
    asqs.login(two, "tok-2-new")
    newer = service.SeenLogin(token=service._login_digest(_creds("tok-2-new")), seen_at=NOW)
    from aisquare.core.store import store_session

    with store_session() as store:  # B's read of the new login
        store.set_ui_state(key, newer.dump())
    with service._NOTED_LOCK:
        service._NOTED.clear()  # A's minute is up: it looks at the row again

    service.cached_usage([two], now=NOW, fetch=_Usage({"tok-2": _payload(40.0)}))

    held = next(value for name, value, *_ in _store_rows()["ui_state"] if name == key)
    assert service.SeenLogin.load(held) == newer, "A's older login was written back"


def test_the_mac_says_each_login_once_a_minute_not_on_every_two_second_tick(
    asqs: TwoAsqs, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    _slots(asqs)
    service.overview()  # the first look says every slot's login
    opens: list[int] = []
    from aisquare.core.store import store_session as real

    @contextlib.contextmanager
    def counted() -> Any:
        opens.append(1)
        with real() as store:
            yield store

    accounts = service.list_accounts()  # the registry's own read opens it, as it always has
    monkeypatch.setattr("aisquare.services.claude_accounts.store_session", counted)
    for _ in range(5):
        service.signed_in_slots(accounts)
    assert opens == [], "the same logins, said again, opened the store"
