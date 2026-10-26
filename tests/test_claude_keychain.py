"""Claude Code's macOS Keychain, read for every account, so usage works on a Mac.

The human (2026-10-15): macOS users could not see any account's usage, every row saying the
credentials were in the Keychain, which the CLI did not read, and "macos users also want to
see usage across all accounts". On macOS Claude Code keeps each config dir's login in
one item of the login Keychain; ``core.claude_accounts.stored_login`` reads it through
``/usr/bin/security``, and falls back to ``.credentials.json`` when there is no item.

Run on Linux CI: the platform is switched on per test and ``security`` is a FAKE, a script
that answers from a table and logs its arguments (POSIX only: the item's names are checked
on every platform). The service names are Claude Code's own, computed by its code, as
transcribed from the 2.1.300 bundle into Node (the board's research note):

    Ee  = (env.CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")).normalize("NFC")
    n   = env.CLAUDE_SECURESTORAGE_CONFIG_DIR
    Ome = "Claude Code" + "" + "-credentials"
          + ((n !== undefined ? !n : !env.CLAUDE_CONFIG_DIR) ? ""
             : "-" + sha256(n !== undefined ? n.normalize("NFC") : Ee).hex.slice(0, 8))

and the expected names below are what Node printed for it, not this module recomputed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import types
import unicodedata
from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.models import ClaudeAccount, ClaudeAccountStatus
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import NOW, _installed, _sign_in
from tests.test_usage_aware_accounts import _payload, _Usage

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake `security` is a POSIX script, as the real one is"
)

NOT_FOUND = (
    "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n"
)

# What Node printed for Claude Code's Ome("-credentials") over each environment.
NODE_VECTORS: list[tuple[dict[str, str], str]] = [
    ({}, "Claude Code-credentials"),
    ({"CLAUDE_CONFIG_DIR": ""}, "Claude Code-credentials"),
    ({"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work/"}, "Claude Code-credentials-d3ebdfac"),
    ({"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"}, "Claude Code-credentials-1e91dd84"),
    ({"CLAUDE_CONFIG_DIR": "~/.claude-work"}, "Claude Code-credentials-250d1b22"),
    ({"CLAUDE_CONFIG_DIR": "/Users/me/Caf\u00e9/claude"}, "Claude Code-credentials-5d9899dd"),
    ({"CLAUDE_CONFIG_DIR": "/Users/me/Cafe\u0301/claude"}, "Claude Code-credentials-5d9899dd"),
    (
        {
            "CLAUDE_CONFIG_DIR": "/Users/me/.claude-work",
            "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/Users/me/shared",
        },
        "Claude Code-credentials-f4c6087d",
    ),
    (
        {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work", "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""},
        "Claude Code-credentials",
    ),
    ({"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/Users/me/shared"}, "Claude Code-credentials-f4c6087d"),
]
MANAGED_SLOT_2 = "Claude Code-credentials-72ce35d7"
"""Node, for CLAUDE_CONFIG_DIR=/Users/me/.aisquare/claude-accounts/2."""


def claude_code_service(env: Mapping[str, str]) -> str:
    """Claude Code's Ome("-credentials") over a launch environment, in Python."""
    secure = env.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    bare = not secure if secure is not None else not env.get("CLAUDE_CONFIG_DIR")
    if bare:
        return "Claude Code-credentials"
    directory = secure if secure is not None else env["CLAUDE_CONFIG_DIR"]
    digest = hashlib.sha256(unicodedata.normalize("NFC", directory).encode()).hexdigest()
    return f"Claude Code-credentials-{digest[:8]}"


def _item(token: str, *, expires_in: timedelta = timedelta(hours=7)) -> str:
    """The item's text: the JSON Claude Code writes, one line, as ``security -w`` prints it."""
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": "sk-ant-ort01-refresh-never-read",
                "expiresAt": int((NOW + expires_in).timestamp() * 1000),
                "scopes": ["user:inference", "user:profile"],
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            },
            "mcpOAuth": {"some-server": {"accessToken": "mcp-token-never-read"}},
        },
        separators=(",", ":"),
    )


class FakeKeychain:
    """A ``security`` that answers ``find-generic-password`` from a table, and logs each call."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        self.root = root
        self.tool = root / "security"
        self.table = root / "items.json"
        self.log = root / "calls.jsonl"
        self.table.write_text("{}", encoding="utf-8")
        self.tool.write_text(
            f"#!{sys.executable} -I\n"
            "import json, pathlib, sys, time\n"
            "here = pathlib.Path(__file__).parent\n"
            "args = sys.argv[1:]\n"
            "with open(here / 'calls.jsonl', 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "items = json.loads((here / 'items.json').read_text())\n"
            "key = args[args.index('-s') + 1] + '\\t' + args[args.index('-a') + 1]\n"
            f"item = items.get(key, {{'exit': 44, 'stderr': {NOT_FOUND!r}}})\n"
            "time.sleep(item.get('sleep', 0))\n"
            "sys.stdout.write(item.get('stdout', ''))\n"
            "sys.stderr.write(item.get('stderr', ''))\n"
            "sys.exit(item.get('exit', 0))\n",
            encoding="utf-8",
        )
        self.tool.chmod(0o755)

    def put(
        self,
        service_name: str,
        *,
        stdout: str = "",
        exit: int = 0,
        stderr: str = "",
        sleep: float = 0,
        account: str = "tester",
    ) -> None:
        table = json.loads(self.table.read_text(encoding="utf-8"))
        table[f"{service_name}\t{account}"] = {
            "stdout": stdout + ("\n" if stdout else ""),
            "exit": exit,
            "stderr": stderr,
            "sleep": sleep,
        }
        self.table.write_text(json.dumps(table), encoding="utf-8")

    def login(self, account: ClaudeAccount, token: str, **kwargs: Any) -> None:
        """Claude Code signed ``account`` in: its item holds ``token``."""
        self.put(core.keychain_service(account), stdout=_item(token, **kwargs))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def keychain(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeKeychain]:
    """macOS, as far as the credentials go: the Keychain read through a fake ``security``."""
    fake = FakeKeychain(tmp_path / "keychain")
    monkeypatch.setattr(core, "keychain_platform", lambda: True)
    monkeypatch.setattr(core, "KEYCHAIN_TOOL", str(fake.tool))
    monkeypatch.setenv("USER", "tester")
    core.forget_keychain_reads()
    yield fake
    core.forget_keychain_reads()


def _identity(account: ClaudeAccount, email: str) -> None:
    """What a signed-in Claude Code on macOS leaves in the directory: ``.claude.json`` only."""
    _sign_in(account, email, credentials=False)


# --- the item's names ------------------------------------------------------------------------


@pytest.mark.parametrize(("environ", "expected"), NODE_VECTORS, ids=lambda v: repr(v)[:60])
def test_the_plain_claudes_item_is_the_one_claude_code_names_byte_for_byte(
    environ: dict[str, str], expected: str
) -> None:
    """The raw CLAUDE_CONFIG_DIR, as a plain claude reads it: a trailing slash and an
    unexpanded ``~`` are other items; NFD reads as NFC; an empty one is no suffix; and
    CLAUDE_SECURESTORAGE_CONFIG_DIR wins, even set to nothing."""
    plain = core.default_account()
    assert core.keychain_service(plain, environ) == expected
    assert claude_code_service(environ) == expected  # the test's transcription agrees with Node


@pytest.mark.skipif(sys.platform == "win32", reason="a macOS path")
def test_a_managed_slots_item_hashes_the_dir_its_launch_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISQUARE_HOME", "/Users/me/.aisquare")
    slot = core._managed(2)
    assert str(slot.config_dir) == "/Users/me/.aisquare/claude-accounts/2"
    assert core.keychain_service(slot, {}) == MANAGED_SLOT_2
    # What the claude aisquare launches on each slot would name, from its own environment.
    for account in (core.default_account(), slot):
        launched = service.session_env(account, {"PATH": "/usr/bin"})
        assert core.keychain_service(account, {}) == claude_code_service(launched)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/me/.claude-work/")
    plain = core.default_account()
    launched = service.session_env(plain, dict(os.environ))
    assert core.keychain_service(plain) == claude_code_service(launched)
    assert core.keychain_service(plain) == "Claude Code-credentials-d3ebdfac"


def test_securestorage_names_every_accounts_item_as_claude_code_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every launch from this environment inherits it, and Claude Code keys on it alone."""
    slot = core._managed(3)
    shared = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/Users/me/shared"}
    assert core.keychain_service(slot, shared) == "Claude Code-credentials-f4c6087d"
    assert (
        core.keychain_service(core.default_account(), shared) == "Claude Code-credentials-f4c6087d"
    )
    empty = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": ""}
    assert core.keychain_service(slot, empty) == "Claude Code-credentials"


def test_under_a_managed_slot_the_plain_claudes_item_is_the_launching_shells(
    tmp_path: Path,
) -> None:
    """An agent on slot 3 runs aisquare with CLAUDE_CONFIG_DIR naming slot 3; the plain
    claude's item is named after the shell's own directory the launch kept, or none."""
    slot = core._managed(3)
    under = {"CLAUDE_CONFIG_DIR": str(slot.config_dir)}
    plain = core.default_account()
    assert core.keychain_service(plain, under) == "Claude Code-credentials"
    kept = {**under, "AISQUARE_PLAIN_CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"}
    assert core.keychain_service(plain, kept) == "Claude Code-credentials-1e91dd84"


@pytest.mark.parametrize(
    ("user", "login", "expected"),
    [
        ("tester", None, "tester"),
        ("first.last-2_x", None, "first.last-2_x"),
        ("first last", None, "claude-code-user"),
        ("jos\u00e9", None, "claude-code-user"),
        ("tester\n", None, "claude-code-user"),
        ("", "macuser", "macuser"),
        ("", None, "claude-code-user"),
        ("", "has space", "claude-code-user"),
    ],
)
def test_the_items_account_is_users_or_the_login_name_or_claude_code_user(
    user: str, login: str | None, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_login_name", lambda: login)
    assert core.keychain_account({"USER": user}) == expected


@POSIX_ONLY
@pytest.mark.parametrize("found", [True, False], ids=["passwd-entry", "none"])
def test_the_login_name_is_the_effective_users_passwd_entry(
    found: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.userInfo().username``: the passwd entry of the EFFECTIVE uid, or none."""
    asked: list[int] = []

    def getpwuid(uid: int) -> Any:
        asked.append(uid)
        if not found:
            raise KeyError(uid)
        return types.SimpleNamespace(pw_name="macuser")

    monkeypatch.setitem(sys.modules, "pwd", types.SimpleNamespace(getpwuid=getpwuid))
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    assert core._login_name() == ("macuser" if found else None)
    assert asked == [501]


# --- reading it ------------------------------------------------------------------------------


@POSIX_ONLY
def test_every_accounts_item_is_read_and_its_token_is_the_one_usage_sends(
    keychain: FakeKeychain,
) -> None:
    plain = core.default_account()
    two, three = core.create_account(), core.create_account()
    for account, email in ((plain, "one@example.com"), (two, "two@"), (three, "three@")):
        _identity(account, email)
        keychain.login(account, f"sk-ant-oat01-{account.slot}")
    fetch = _Usage({f"sk-ant-oat01-{n}": _payload(10.0 * n) for n in (1, 2, 3)})

    readings = service.read_usage([plain, two, three], now=NOW, fetch=fetch)

    assert sorted(fetch.calls) == ["sk-ant-oat01-1", "sk-ant-oat01-2", "sk-ant-oat01-3"]
    assert {slot: r.session_percent for slot, r in readings.items()} == {1: 10, 2: 20, 3: 30}
    services = {call[call.index("-s") + 1] for call in keychain.calls()}
    assert services == {core.keychain_service(a) for a in (plain, two, three)}
    assert len(services) == 3 and "Claude Code-credentials" in services  # plain: no suffix
    for call in keychain.calls():  # the argument list Claude Code reads with, no shell
        assert call[:3] == ["find-generic-password", "-a", "tester"] and call[3:5] == ["-w", "-s"]
    status = service.describe(two)
    assert status.signed_in and status.token_state == "ok" and status.subscription == "max 20x"


@POSIX_ONLY
def test_no_item_is_a_log_in_to_do(keychain: FakeKeychain) -> None:
    from aisquare.cli.ui.views.accounts import needs_login

    two = core.create_account()
    _identity(two, "two@example.com")
    status = service.describe(two)
    assert not status.signed_in and status.token_state == "missing"
    assert needs_login(status, None)  # the row offers Log in
    reading = service.usage(two, now=NOW, fetch=_Usage({}))
    assert not reading.available and reading.reason == (
        "no login in the Keychain or a credentials file — not signed in"
    )
    assert not service.sign_in_landed(two)


@POSIX_ONLY
def test_an_item_after_a_logout_holds_no_login_and_is_not_one(keychain: FakeKeychain) -> None:
    """``/logout`` keeps the item (the MCP servers' tokens are in it too), without the login."""
    two = core.create_account()
    _identity(two, "two@example.com")
    keychain.put(core.keychain_service(two), stdout=json.dumps({"mcpOAuth": {}}))
    _sign_in(two, "two@example.com")  # a stale file beside it is not read: the item answered
    assert not core.signed_in(two) and core.credentials(two) is None


@POSIX_ONLY
def test_with_no_item_the_credentials_file_is_the_login_as_some_setups_write_it(
    keychain: FakeKeychain,
) -> None:
    two = core.create_account()
    _sign_in(two, "two@example.com")  # .credentials.json, as on Linux
    creds = core.credentials(two)
    assert creds is not None and creds.access_token == "sk-ant-oat01-secret"
    assert core.signed_in(two)
    assert keychain.calls(), "the Keychain was asked first"
    keychain.login(two, "sk-ant-oat01-from-the-keychain")
    core.forget_keychain_reads()
    creds = core.credentials(two)
    assert creds is not None and creds.access_token == "sk-ant-oat01-from-the-keychain"


@POSIX_ONLY
@pytest.mark.parametrize("code", [37, 50], ids=["no-default-keychain", "no-such-keychain"])
def test_a_mac_with_no_login_keychain_reads_the_file(keychain: FakeKeychain, code: int) -> None:
    two = core.create_account()
    _sign_in(two, "two@example.com")
    keychain.put(core.keychain_service(two), exit=code, stderr="security: no keychain\n")
    assert core.credentials(two) is not None


@POSIX_ONLY
def test_an_expired_keychain_token_is_expired_and_never_sent(keychain: FakeKeychain) -> None:
    from aisquare.cli.ui.views.accounts import needs_login

    two = core.create_account()
    _identity(two, "two@example.com")
    keychain.login(two, "sk-ant-oat01-old", expires_in=timedelta(hours=-1))
    status = service.describe(two)
    assert status.signed_in and status.token_state == "expired" and needs_login(status, None)
    fetch = _Usage({})
    reading = service.usage(two, now=NOW, fetch=fetch)
    assert reading.needs_login and fetch.calls == []


@POSIX_ONLY
def test_the_item_printed_as_hex_reads_as_well(keychain: FakeKeychain) -> None:
    """``security -w`` prints a password with a byte it will not print as hex instead."""
    two = core.create_account()
    _identity(two, "two@example.com")
    keychain.put(core.keychain_service(two), stdout=_item("sk-ant-oat01-hex").encode().hex())
    creds = core.credentials(two)
    assert creds is not None and creds.access_token == "sk-ant-oat01-hex"


LOCKED = core.KEYCHAIN_LOCKED
REFUSED = core.KEYCHAIN_REFUSED


@POSIX_ONLY
@pytest.mark.parametrize(
    ("exit", "stderr", "reason"),
    [
        (36, "security: SecKeychainSearchCopyNext: User interaction is not allowed.\n", LOCKED),
        (1, "security: the keychain is locked\n", LOCKED),
        (
            51,
            "security: SecKeychainSearchCopyNext: The user name or passphrase you entered is "
            "not correct.\n",
            REFUSED,
        ),
        (128, "security: User canceled the operation.\n", LOCKED),
        (25, "security: something else\n", "could not read the Keychain (security exited 25)"),
    ],
    ids=["interaction-not-allowed", "locked", "denied", "canceled", "other"],
)
def test_a_keychain_that_would_not_say_is_said_and_offers_no_log_in(
    keychain: FakeKeychain, exit: int, stderr: str, reason: str
) -> None:
    from aisquare.cli.ui.views.accounts import account_cells, needs_login

    two = core.create_account()
    _sign_in(two, "two@example.com")  # a file beside it is not the answer: the Keychain is
    keychain.put(core.keychain_service(two), exit=exit, stderr=stderr)
    status = service.describe(two)
    assert status.signed_in and status.token_state == "unreadable"
    fetch = _Usage({})
    reading = service.usage(two, now=NOW, fetch=fetch)
    assert not reading.available and reading.reason == reason and not reading.needs_login
    assert fetch.calls == []
    assert not needs_login(status, reading)  # a log-in would not unlock the Keychain
    # Said where the usage would be: there is no status column (#8 on the board).
    assert str(account_cells(status, reading)["session"]) == reason


@POSIX_ONLY
def test_a_keychain_that_does_not_answer_in_time_is_said(
    keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "KEYCHAIN_TIMEOUT_SECONDS", 0.5)
    two = core.create_account()
    _identity(two, "two@example.com")
    keychain.put(core.keychain_service(two), stdout=_item("sk-ant-oat01-late"), sleep=5)
    login = core.stored_login(two)
    assert login.credentials is None
    assert login.unreadable == "could not read the Keychain (no answer in 0.5 s)"
    assert core.signed_in(two)


@POSIX_ONLY
def test_a_security_that_cannot_run_is_said(
    keychain: FakeKeychain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "KEYCHAIN_TOOL", str(tmp_path / "no-such-security"))
    two = core.create_account()
    _identity(two, "two@example.com")
    unreadable = core.stored_login(two).unreadable
    assert unreadable is not None
    assert unreadable.startswith(f"could not read the Keychain ({tmp_path / 'no-such-security'}: ")


@POSIX_ONLY
def test_the_token_never_appears_in_any_output_or_exception(
    keychain: FakeKeychain,
    runner: CliRunner,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-ant-oat01-NEVER-PRINT-ME"
    accounts = [core.create_account() for _ in range(4)]
    for account in accounts:
        _identity(account, f"slot{account.slot}@example.com")
    good, locked, slow, broken = accounts
    keychain.login(good, secret)
    keychain.put(
        core.keychain_service(locked),
        stdout=_item(secret),
        exit=36,
        stderr="security: User interaction is not allowed.\n",
    )
    keychain.put(core.keychain_service(slow), stdout=_item(secret), sleep=3)
    keychain.put(core.keychain_service(broken), stdout=_item(secret), exit=25, stderr="oops\n")
    monkeypatch.setattr(core, "KEYCHAIN_TIMEOUT_SECONDS", 0.5)

    def failing(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        raise OSError("connection refused")

    monkeypatch.setattr(service, "_http_get", failing)
    said: list[str] = []
    for account in accounts:
        said += [repr(core.stored_login(account)), repr(service.describe(account))]
        said.append(repr(service.usage(account, now=NOW)))
        try:
            said.append(str(service.login_fingerprint(account)))
        except service.LoginUnreadable as exc:  # a locked, slow or broken Keychain
            said += [str(exc), repr(exc)]
    with core._KEYCHAIN_READS_LOCK:
        said.append(repr(core._KEYCHAIN_READS))
    for args in (
        ["accounts", "usage"],
        ["--json", "accounts", "list", "--usage"],
        ["--json", "accounts", "usage"],
    ):
        result = runner.invoke(app, args)
        said += [result.output, repr(result.exception)]
    captured = capsys.readouterr()
    said += [captured.out, captured.err, caplog.text]
    for other in ("sk-ant-ort01-refresh-never-read", "mcp-token-never-read"):  # the item's rest
        assert all(other not in text for text in said), other
    assert all(secret not in text for text in said)
    assert any(LOCKED in text for text in said)  # the reasons did travel
    assert any("no answer in 0.5 s" in text for text in said)


# --- how often it is asked ---------------------------------------------------------------------


@POSIX_ONLY
def test_a_read_stands_for_a_few_seconds_and_a_refusal_until_someone_asks_again(
    keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tester: a locked Keychain in a desktop session puts its unlock dialog up for each
    read, and asq read every slot each tick. A refusal is asked again only when someone asks:
    a forced refresh or the page opened (``forget_keychain_reads``), a sign-in's check
    (``fresh``), or a command of its own (a new process)."""
    clock = [1000.0]
    monkeypatch.setattr(core, "_monotonic", lambda: clock[0])
    two, three = core.create_account(), core.create_account()
    _identity(two, "two@example.com")
    _identity(three, "three@example.com")
    keychain.login(two, "sk-ant-oat01-two")
    keychain.put(core.keychain_service(three), exit=36, stderr="User interaction is not allowed.")

    def asked(account: ClaudeAccount) -> int:
        wanted = core.keychain_service(account)
        return sum(1 for call in keychain.calls() if call[call.index("-s") + 1] == wanted)

    service.describe(two)  # credentials and signed_in: one read
    service.describe(three)
    assert (asked(two), asked(three)) == (1, 1)
    clock[0] += core.KEYCHAIN_MEMO_SECONDS - 1
    service.describe(two)
    service.describe(three)
    assert (asked(two), asked(three)) == (1, 1)
    clock[0] += 2  # past a read's few seconds
    service.describe(two)
    service.describe(three)
    assert (asked(two), asked(three)) == (2, 1)
    for _ in range(100):  # a day of the sidebar's minute ticks and the page's two seconds
        clock[0] += 900
        service.describe(three)
        service.cached_usage([three], now=NOW, fetch=_Usage({}))
        service.read_fleet_headroom([three], now=NOW, fetch=_Usage({}))
    assert asked(three) == 1, "a refusal is never asked again by a tick"
    core.credentials(two, fresh=True)  # a sign-in's check asks again
    assert asked(two) == 3
    core.forget_keychain_reads()  # a forced refresh, or the page opened
    service.describe(three)
    assert asked(three) == 2


@POSIX_ONLY
def test_a_refusal_read_before_a_refresh_does_not_stand_after_it(
    keychain: FakeKeychain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review n6: a read in flight when the refresh forgets them all answers its own caller
    but is not kept, so the read the refresh makes is the one that stands."""
    two = core.create_account()
    _identity(two, "two@example.com")
    service_name = core.keychain_service(two)
    ask = core._ask_keychain

    def slow_locked(name: str, account: str) -> core.KeychainRead:
        read = ask(name, account)  # locked, as it was when the read began
        core.forget_keychain_reads()  # ↻ Refresh usage, pressed meanwhile
        keychain.login(two, "sk-ant-oat01-unlocked")  # and the Keychain unlocked
        return read

    keychain.put(service_name, exit=36, stderr="User interaction is not allowed.")
    monkeypatch.setattr(core, "_ask_keychain", slow_locked)
    assert core.stored_login(two).unreadable == LOCKED
    monkeypatch.setattr(core, "_ask_keychain", ask)
    creds = core.credentials(two)
    assert creds is not None and creds.access_token == "sk-ant-oat01-unlocked"


@POSIX_ONLY
def test_the_five_minute_cache_serves_keychain_tokens_and_a_new_token_is_asked_at_once(
    keychain: FakeKeychain,
) -> None:
    two, three = core.create_account(), core.create_account()
    for account in (two, three):
        _identity(account, f"slot{account.slot}@example.com")
        keychain.login(account, f"sk-ant-oat01-{account.slot}")
    fetch = _Usage(
        {
            "sk-ant-oat01-2": _payload(20.0),
            "sk-ant-oat01-3": _payload(30.0),
            "sk-ant-oat01-2b": _payload(25.0),
        }
    )
    first = service.cached_usage([two, three], now=NOW, fetch=fetch)
    assert {s: r.session_percent for s, r in first.items()} == {2: 20, 3: 30}
    later = NOW + timedelta(minutes=2)
    core.forget_keychain_reads()  # minutes later: the Keychain is read again, and agrees
    again = service.cached_usage([two, three], now=later, fetch=fetch)
    assert {s: r.session_percent for s, r in again.items()} == {2: 20, 3: 30}
    assert sorted(fetch.calls) == ["sk-ant-oat01-2", "sk-ant-oat01-3"]  # served, not asked
    keychain.login(two, "sk-ant-oat01-2b")  # Claude Code refreshed slot 2's token
    core.forget_keychain_reads()
    third = service.cached_usage([two, three], now=later + timedelta(seconds=30), fetch=fetch)
    assert third[2].session_percent == 25 and third[3].session_percent == 30
    assert sorted(fetch.calls) == ["sk-ant-oat01-2", "sk-ant-oat01-2b", "sk-ant-oat01-3"]


@POSIX_ONLY
def test_refresh_usage_asks_a_locked_keychain_again_once_it_is_unlocked(
    keychain: FakeKeychain,
) -> None:
    two = core.create_account()
    _identity(two, "two@example.com")
    keychain.put(core.keychain_service(two), exit=36, stderr="User interaction is not allowed.")
    fetch = _Usage({"sk-ant-oat01-2": _payload(20.0)})
    [locked] = service.cached_usage([two], now=NOW, fetch=fetch).values()
    assert locked.reason == LOCKED
    keychain.login(two, "sk-ant-oat01-2")  # unlocked
    [still] = service.cached_usage([two], now=NOW, fetch=fetch).values()
    assert still.reason == LOCKED, "a refusal stands until someone asks: not on every tick"
    [read] = service.cached_usage([two], now=NOW, force=True, fetch=fetch).values()
    assert read.available and read.session_percent == 20


@POSIX_ONLY
def test_accounts_usage_reads_every_slot_on_a_mac(
    keychain: FakeKeychain, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch)
    plain = core.default_account()
    two, three = core.create_account(), core.create_account()
    for account, email in (
        (plain, "one@example.com"),
        (two, "two@example.com"),
        (three, "three@example.com"),
    ):
        _identity(account, email)
        keychain.login(account, f"sk-ant-oat01-{account.slot}")
    monkeypatch.setattr(
        service,
        "_http_get",
        _Usage({f"sk-ant-oat01-{n}": _payload(10.0 * n, week=n) for n in (1, 2, 3)}),
    )
    result = runner.invoke(app, ["--json", "accounts", "usage"])
    assert result.exit_code == 0, result.output
    rows = {row["account"]["slot"]: row["usage"] for row in json.loads(result.stdout)}
    assert {slot: usage["session_percent"] for slot, usage in rows.items()} == {1: 10, 2: 20, 3: 30}
    listed = runner.invoke(app, ["--json", "accounts", "list", "--usage"])
    assert listed.exit_code == 0, listed.output
    accounts = json.loads(listed.stdout)["accounts"]
    assert [a["usage"]["session_percent"] for a in accounts] == [10, 20, 30]
    assert all(a["signed_in"] and a["token_state"] == "ok" for a in accounts)
    assert "the CLI does not read" not in result.output + listed.output


@POSIX_ONLY
def test_headroom_reads_every_slots_keychain_item(keychain: FakeKeychain) -> None:
    two, three = core.create_account(), core.create_account()
    for account in (two, three):
        _identity(account, f"slot{account.slot}@example.com")
        keychain.login(account, f"sk-ant-oat01-{account.slot}")
    fetch = _Usage({"sk-ant-oat01-2": _payload(90.0), "sk-ant-oat01-3": _payload(10.0)})
    summary = service.read_fleet_headroom([two, three], now=NOW, fetch=fetch)
    assert summary.usable == 2 and set(summary.left_by_slot) == {2, 3}
    assert summary.left_by_slot[3] > summary.left_by_slot[2]


def test_linux_and_windows_never_ask_a_keychain(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform as it is (``keychain_platform`` off): the file, and no ``security``."""
    fake = FakeKeychain(tmp_path / "keychain")
    monkeypatch.setattr(core, "KEYCHAIN_TOOL", str(fake.tool))
    two = core.create_account()
    _sign_in(two, "two@example.com")
    creds = core.credentials(two)
    assert creds is not None and creds.access_token == "sk-ant-oat01-secret"
    assert core.signed_in(two) and service.describe(two).token_state == "ok"
    assert fake.calls() == []


@POSIX_ONLY
def test_a_read_from_many_threads_at_once_is_one_answer_each(keychain: FakeKeychain) -> None:
    """The headroom pick reads four slots at once; the memo is shared and locked."""
    accounts = [core.create_account() for _ in range(4)]
    for account in accounts:
        _identity(account, f"slot{account.slot}@example.com")
        keychain.login(account, f"sk-ant-oat01-{account.slot}")
    got: dict[int, str | None] = {}

    def read(account: ClaudeAccount) -> None:
        creds = core.credentials(account)
        got[account.slot] = creds.access_token if creds else None

    threads = [threading.Thread(target=read, args=(a,)) for a in accounts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert got == {a.slot: f"sk-ant-oat01-{a.slot}" for a in accounts}


# --- the review of #263 ---------------------------------------------------------------------


@POSIX_ONLY
def test_cancel_just_after_a_login_landed_keeps_the_slot_and_its_item(
    keychain: FakeKeychain,
) -> None:
    """Review M1: the last poll found no item (and left that in the memo); Claude Code
    stored the login a moment later; Cancel. The discard asks the Keychain itself, so the
    slot stays, and settling it keeps the login, wired, rather than orphan its item."""
    two = core.create_account()
    _identity(two, "two@example.com")
    assert service.sign_in_landed(two) is None  # the last poll: no item yet
    keychain.login(two, "sk-ant-oat01-just-landed")  # the item lands right after
    assert service.abandon_sign_in(two) is False
    assert two.config_dir.is_dir()
    settled = service.settle_sign_in(two)
    assert isinstance(settled, ClaudeAccountStatus) and settled.signed_in
    assert two.config_dir.is_dir()


@POSIX_ONLY
def test_a_cancelled_sign_in_that_never_landed_is_still_discarded(keychain: FakeKeychain) -> None:
    two = core.create_account()
    assert service.settle_sign_in(two) is True
    assert not two.config_dir.exists()


@POSIX_ONLY
@pytest.mark.parametrize(
    ("exit", "stderr"),
    [(36, "User interaction is not allowed."), (51, "The user name or passphrase you entered")],
    ids=["locked", "refused"],
)
def test_a_keychain_that_would_not_say_mid_window_is_no_landing(
    keychain: FakeKeychain, exit: int, stderr: str
) -> None:
    """Review m1 (tester's note e): a re-auth over an expired login, and the Keychain locks
    while the window is open. Its answer is no login, old or new: the window keeps waiting."""
    two = core.create_account()
    _identity(two, "two@example.com")
    keychain.login(two, "sk-ant-oat01-old", expires_in=timedelta(hours=-1))
    since = service.login_fingerprint(two)
    assert since is not None
    keychain.put(core.keychain_service(two), exit=exit, stderr=stderr)
    assert service.sign_in_landed(two, since=since) is None
    assert service.sign_in_landed(two) is None  # a fresh slot's window too
    keychain.login(two, "sk-ant-oat01-new")
    assert service.sign_in_landed(two, since=since) is not None  # the new login lands


@POSIX_ONLY
def test_a_log_in_over_a_login_that_cannot_be_read_is_refused_with_why(
    keychain: FakeKeychain,
) -> None:
    """Review n4: a fingerprint of nothing would let the login already stored count as the
    new one at the first poll; a store that would not say raises, with its reason."""
    two = core.create_account()
    _identity(two, "two@example.com")
    assert service.login_fingerprint(two) is None  # none stored: any login is the new one
    keychain.put(core.keychain_service(two), exit=36, stderr="User interaction is not allowed.")
    with pytest.raises(service.LoginUnreadable, match="locked"):
        service.login_fingerprint(two)


@POSIX_ONLY
def test_the_sign_in_reads_ask_the_keychain_not_a_read_of_a_moment_ago(
    keychain: FakeKeychain,
) -> None:
    """Review m4: an item stored within KEYCHAIN_MEMO_SECONDS of a read that found none is
    seen by the window's poll, by the fingerprint a Log in opens over and by the discard."""
    two, three, four = (core.create_account() for _ in range(3))
    for account in (two, three, four):
        _identity(account, f"slot{account.slot}@example.com")
        assert core.credentials(account) is None  # remembered: no item
        keychain.login(account, f"sk-ant-oat01-{account.slot}")
    assert core.credentials(two) is None, "the control: a plain read is the memo's"
    assert service.sign_in_landed(two) is not None
    assert service.login_fingerprint(three) is not None
    assert service.abandon_sign_in(four) is False and four.config_dir.is_dir()


def test_the_keychain_is_read_by_the_one_absolute_security_with_no_shell_or_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review m4: the call itself, on every platform (``subprocess.run`` stubbed). The tool is
    the module's own constant as written, not a test's: a fresh copy of the module says it."""
    import importlib.util
    import subprocess

    spec = importlib.util.find_spec("aisquare.core.claude_accounts")
    assert spec is not None and spec.loader is not None
    shipped = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shipped)
    assert shipped.KEYCHAIN_TOOL == "/usr/bin/security"  # absolute: never one found on PATH
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 44, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert shipped.read_keychain("Claude Code-credentials-1e91dd84", "tester").found is False
    [(argv, kwargs)] = calls
    assert argv == [
        "/usr/bin/security",
        "find-generic-password",
        "-a",
        "tester",
        "-w",
        "-s",
        "Claude Code-credentials-1e91dd84",
    ]
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "timeout": 5.0,
        "check": False,
    }


@POSIX_ONLY
@pytest.mark.parametrize("secure", ["/Users/me/shared", ""], ids=["set", "empty"])
def test_with_securestorage_the_fallback_file_is_where_claude_code_writes_it(
    keychain: FakeKeychain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secure: str
) -> None:
    """Review m2 (tester's note c): Claude Code's plaintext store is its wx() directory,
    SECURESTORAGE when set (empty: ~/.claude), as its item is keyed on it."""
    shared = tmp_path / "shared" if secure else core.home_config_dir()
    shared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(shared) if secure else "")
    two = core.create_account()
    _sign_in(two, "two@example.com")  # the config dir's own file: not where Claude Code looks
    assert core.plaintext_credentials_path(two) == shared / ".credentials.json"
    assert core.credentials(two) is None
    (shared / ".credentials.json").write_text(_item("sk-ant-oat01-shared"), encoding="utf-8")
    core.forget_keychain_reads()
    creds = core.credentials(two)
    assert creds is not None and creds.access_token == "sk-ant-oat01-shared"


def test_on_linux_securestorage_leaves_the_config_dirs_file_as_it_was(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(tmp_path / "shared"))
    two = core.create_account()
    _sign_in(two, "two@example.com")
    creds = core.credentials(two)
    assert creds is not None and creds.access_token == "sk-ant-oat01-secret"


@POSIX_ONLY
def test_asq_never_asks_a_locked_keychain_again_by_itself(keychain: FakeKeychain) -> None:
    """The tester's concern: a locked login Keychain in a desktop session may put its unlock
    dialog up on each read. In a real asq, every tick of the shell's accounts read and the
    sidebar's headroom reads the slot from the memo after the first refusal; opening the
    Accounts page and ↻ Refresh usage ask again, once each."""
    import asyncio

    from aisquare.cli.ui.app import FleetApp
    from tests.test_ui_accounts import accounts_read, open_accounts, settle

    two = core.create_account()
    _identity(two, "two@example.com")
    wanted = core.keychain_service(two)
    keychain.put(wanted, exit=36, stderr="User interaction is not allowed.")

    def asked() -> int:
        return sum(1 for call in keychain.calls() if call[call.index("-s") + 1] == wanted)

    async def run() -> list[int]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=service.overview)
        counts: list[int] = []
        async with app.run_test(size=(240, 40)) as pilot:
            await settle(app)
            for _ in range(5):  # the shell's two-second ticks and the sidebar's minute
                app.refresh_accounts()
                await accounts_read(app)
                app.refresh_headroom()
                await settle(app)
            counts.append(asked())
            view = await open_accounts(pilot)
            await settle(app)
            for _ in range(3):
                app.refresh_accounts()
                await accounts_read(app)
                view.refresh_readings()
                await settle(app)
            counts.append(asked())
            view.refresh_usage(force=True)
            await settle(app)
            counts.append(asked())
            for _ in range(3):
                app.refresh_accounts()
                await accounts_read(app)
            counts.append(asked())
            await pilot.pause()
        return counts

    assert asyncio.run(run()) == [1, 2, 3, 3]
