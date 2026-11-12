"""Codex signs in by device code; Claude Code's sign-in is as it was (R13 of #113).

Every Codex sign-in aisquare starts runs ``codex login --device-auth``: ``accounts add
--agent codex``, *+ Add* in asq's Codex section, and *Log in* on a Codex row (Welcome's and
the doctor's sign-ins open that same *Log in*). Codex prints a URL and a one-time code; the
human opens the URL on any device and enters the code, so nothing waits on a localhost
callback this machine's browser must reach, and it works over SSH. ``accounts add --agent
codex --browser`` keeps ``codex login``'s browser sign-in, for a ChatGPT login that does
not allow device codes.

Claude Code signs in from its own TUI and does not change at all: its argv, its window's
command and variables, what ``accounts add`` prints for it and what the Accounts page says
of its sign-in are pinned here as 5ec5b1cb (the head before R13) printed them.

The Codex here is a fake on PATH that records its argv and writes an ``auth.json`` for the
sign-in it is told to expect, so a test sees the very argv aisquare built reach the binary.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Button, Static
from typer.testing import CliRunner

from aisquare.cli import accounts as accounts_cli
from aisquare.cli.app import app
from aisquare.cli.ui.sidebar import AccountsSelected
from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.core import claude_accounts as core
from aisquare.core import codex_accounts
from aisquare.core.tmux import TmuxServer
from aisquare.models import ClaudeAccount, DoctorCheck
from aisquare.services import claude_accounts as service
from aisquare.services import diagnostics, onboarding
from tests.fakebin import executable_fake, prepend_to_path
from tests.run_codex_native import CODEX_VERSIONS
from tests.test_claude_accounts import _sign_in as claude_sign_in
from tests.test_release_stage import help_console  # noqa: F401 — used by name
from tests.test_ui_accounts import (  # noqa: F401 — `no_real_tmux` is an autouse fixture
    _overview,
    _script_claude_sign_in,
    _status,
    drive,
    fleet_app,
    no_real_tmux,
    notice,
    open_accounts,
    press,
    settle,
    shown,
)
from tests.test_ui_codex_accounts import (  # noqa: F401 — `windows` is a fixture
    _Alive,
    _real_overview,
    _sign_in_codex,
    windows,
)
from tests.ui_workers import settle_page

CODEX = "codex"
CLAUDE = core.AGENT
DEVICE = ["login", "--device-auth"]
BROWSER = ["login"]
_REAL_WHICH = shutil.which
_REAL_INSTALL = service.install

#: The releases ``codex login --device-auth`` was measured on (R13 of #113): each takes the
#: flag, hidden from ``codex login --help``, prints a URL and a one-time code, and has the
#: strings of the device-code flow ("Follow these steps to sign in with ChatGPT using device
#: code authorization", "device auth timed out after 15 minutes"). 0.164.0 is the beta's
#: floor, so no release it holds signs in with plain ``login``.
DEVICE_AUTH_MEASURED = frozenset({"0.164.0", "0.166.0", "0.167.0"})

STEPS = "open the URL it prints, on any device, and enter the code it shows"
HINT = (
    "If your ChatGPT login does not allow device-code sign-in, turn it on in ChatGPT's "
    "security settings (for a workspace, its admin turns it on in the workspace "
    "permissions), or sign in with the browser"
)


# --------------------------------------------------------------------------- Claude Code's bar
#
# Each literal below is what 5ec5b1cb printed for it, before Codex's sign-in changed.


def test_claude_codes_sign_in_argv_and_window_are_byte_identical_to_before(
    fake_home: Path, tmp_path: Path
) -> None:
    """Claude Code has no sign-in command: its window runs ``accounts run <slot>`` with
    nothing after the slot, and carries the home and Claude Code's two variables."""
    account = ClaudeAccount(
        slot=2,
        config_dir=tmp_path / "claude-accounts" / "2",
        tmp_dir=tmp_path / "cache" / "2",
        managed=True,
    )
    # Absolute already, so the window carries them as written on every OS (a "/h/…" is
    # resolved against the drive on Windows).
    aisq, claude = str(tmp_path / "aisq"), str(tmp_path / "claude")
    environ = {"AISQUARE_HOME": aisq, "CLAUDE_CONFIG_DIR": claude}

    command, env = service.sign_in_window_command(account, environ, cwd=tmp_path)

    assert core.kind(CLAUDE).sign_in_args() == ()
    assert service.sign_in_command(account) == [
        sys.executable,
        "-m",
        "aisquare",
        "accounts",
        "run",
        "2",
    ]
    env_binary = shutil.which("env") or "/usr/bin/env"
    assert command == [
        env_binary,
        "-u",
        core.TMPDIR_VAR,
        sys.executable,
        "-m",
        "aisquare",
        "accounts",
        "run",
        "2",
    ]
    assert env == {"AISQUARE_HOME": aisq, "CLAUDE_CONFIG_DIR": claude}


def _claude_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: "/opt/bin/claude" if name == "claude" else _REAL_WHICH(name),
    )


def test_what_accounts_add_prints_for_claude_code_is_byte_identical_to_before(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of what ``accounts add`` prints for Claude Code, landed and not, and the
    argv its session is given: nothing (Claude Code's first session signs in)."""
    _claude_installed(monkeypatch)
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)
    given: list[tuple[str, ...]] = []

    def signs_in(account: ClaudeAccount, args: Sequence[str] = (), **_: Any) -> int:
        given.append(tuple(args))
        claude_sign_in(account, "two@example.com")
        return 0

    monkeypatch.setattr(service, "run_session", signs_in)
    landed = runner.invoke(app, ["accounts", "add"])
    monkeypatch.setattr(service, "run_session", lambda account, args=(), **_: 1)
    not_landed = runner.invoke(app, ["accounts", "add"])

    assert given == [()]
    assert landed.exit_code == 0, landed.output
    assert landed.stderr == (
        "Slot 2 is ready. Claude Code opens now with its own login — sign in there, then "
        "leave it (/exit) and this command records the account.\n"
        "✓ Added Claude account 2: two@example.com\n"
        "  open it: aisquare accounts run 2 · as a role: aisquare launch coder --account 2\n"
    )
    assert not_landed.exit_code == 1
    assert not_landed.stderr == (
        "Slot 3 is ready. Claude Code opens now with its own login — sign in there, then "
        "leave it (/exit) and this command records the account.\n"
        "✗ no sign-in landed in slot 3 (Claude Code exited with status 1) — nothing was added\n"
    )


def test_the_accounts_pages_words_for_a_claude_sign_in_are_byte_identical_to_before() -> None:
    """The *Log in* tooltip's words, the sign-in pane's header after "Logging in to …",
    and the fix a Claude row with no login shows on hover."""
    assert accounts_view._sign_in_opens(CLAUDE) == (
        "Claude Code opens below; type /login if it does not ask"
    )
    assert accounts_view._sign_in_prompt(CLAUDE) == (
        " — type /login if Claude Code does not prompt you."
    )
    assert service._log_in(CLAUDE) == (
        "press Log in (in a terminal: aisquare accounts run {slot}, then /login)"
    )


def test_a_claude_sign_in_pane_and_its_closing_say_what_they_said_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The header of a Claude sign-in pane, and the notice when it closes with no login."""
    _script_claude_sign_in(monkeypatch, tmp_path, lands=False)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#claude-add")
        await settle(app_)
        header = shown(view.query_one("#login-header", Static))
        view._poll_login()  # the recorder answers "no server": the pane closed
        await pilot.pause()
        await settle(app_)
        return header, notice(view)

    header, said = drive(go)
    assert header == (
        "Logging in to account 2 — type /login if Claude Code does not prompt you. This "
        "pane closes by itself once the login is recorded; F12 hands focus back to the "
        "sidebar."
    )
    assert said == (
        "sign-in cancelled — Claude Code closed before a sign-in landed; the new slot was discarded"
    )


@pytest.mark.parametrize(
    ("ending", "notice_said"),
    [
        (
            "closed",
            "sign-in cancelled — Claude Code closed before a sign-in landed; nothing changed",
        ),
        ("cancel", "sign-in cancelled — cancelled; nothing changed"),
    ],
    ids=["closed", "cancel"],
)
def test_a_claude_log_in_on_an_existing_slot_says_what_it_said_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ending: str, notice_said: str
) -> None:
    """*Log in* on Claude slot 2, which exists: the pane's header, and the whole notice when
    its Claude Code closes with no login or *Cancel* is pressed. Nothing follows "nothing
    changed" for Claude Code (a Codex *Log in* names its browser sign-in there)."""
    _script_claude_sign_in(monkeypatch, tmp_path, lands=False)
    overview = _overview(_status(1, "me@example.com"), _status(2, None))

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await press(pilot, "#account-login-2")
        await settle(app_)
        assert view.login is not None and not view.login.fresh
        header = shown(view.query_one("#login-header", Static))
        if ending == "closed":
            view._poll_login()  # the recorder answers "no server": the pane closed
            await pilot.pause()
        else:
            await pilot.click("#login-cancel")
        await settle(app_)
        return header, notice(view)

    header, said = drive(go, overview=overview)
    assert header == (
        "Logging in to account 2 — type /login if Claude Code does not prompt you. This "
        "pane closes by itself once the login is recorded; F12 hands focus back to the "
        "sidebar."
    )
    assert said == notice_said


class _DoctorHost(App[None]):
    """The Doctor page alone, keeping the slot each ``AccountsSelected`` asked to sign in."""

    def __init__(self, view: DoctorView) -> None:
        super().__init__()
        self.view = view
        self.sign_ins: list[int | None] = []

    def compose(self) -> ComposeResult:
        yield self.view

    def on_accounts_selected(self, message: AccountsSelected) -> None:
        self.sign_ins.append(message.sign_in)


def _doctor_buttons(
    checks: Sequence[DoctorCheck],
) -> tuple[list[tuple[str, str]], list[int | None]]:
    """The Doctor page's fix buttons for ``checks``, each ``(label, tooltip)``, and the
    slots the sign-ins they open asked for, each button pressed once."""

    async def press_each() -> tuple[list[tuple[str, str]], list[int | None]]:
        view = DoctorView(refresh=lambda cwd: list(checks))
        host = _DoctorHost(view)
        async with host.run_test(size=(140, 40)) as pilot:
            view.show(list(checks))
            await settle_page(host)
            buttons = list(view.query("#doctor-fixes Button").results(Button))
            seen = [(str(button.label), str(button.tooltip)) for button in buttons]
            for button in buttons:
                await pilot.click(f"#{button.id}")
                await settle_page(host)
            return seen, host.sign_ins

    return asyncio.run(press_each())


def test_claude_codes_doctor_row_and_sign_in_button_for_a_slot_with_no_login_are_as_before(
    fake_home: Path,
) -> None:
    """The doctor's row for a Claude slot with no login, and the button the Doctor page
    makes of it, *Sign in account 3*: it opens that slot's *Log in* on the Accounts page and
    runs nothing itself."""
    two, _three = core.create_account(), core.create_account()
    claude_sign_in(two, "two@example.com")

    checks = diagnostics._claude_accounts_checks()

    hint = "Sign in from asq → Accounts, or: aisquare accounts run 3"
    assert [(c.name, c.status.value, c.detail, c.fix) for c in checks] == [
        (
            "claude-accounts",
            "warn",
            "2 added beside the plain claude: 2 two@example.com · 3 not signed in",
            hint,
        )
    ]
    assert onboarding.fix_commands(checks) == [
        onboarding.FixCommand(
            check="claude-accounts",
            argv=("accounts", "run", "3"),
            scope="machine",
            source=hint,
            interactive=True,
        )
    ]
    assert _doctor_buttons(checks) == (
        [
            (
                "Sign in account 3",
                f"{hint}\nOpens the Accounts page with aisquare accounts run 3 in its pane.",
            )
        ],
        [3],
    )


# --------------------------------------------------------------------------- a fake codex


class FakeCodex:
    """A ``codex`` on PATH, laid out as npm installs it (its ``package.json`` names
    :attr:`version`), that records each argv it is run with and writes an ``auth.json`` into
    ``$CODEX_HOME`` only for the argv it is told signs in (:meth:`signs_in`): any other ends
    as a refused device code ends, with status 1 and no login."""

    def __init__(self, root: Path, version: str) -> None:
        self.root = root
        self.version = version
        self.log = root / "argv.jsonl"
        self.accepts = root / "signs-in.json"
        self.signs_in(DEVICE)

    def signs_in(self, *argvs: Sequence[str]) -> None:
        self.accepts.write_text(json.dumps([list(a) for a in argvs]), encoding="utf-8")

    def argvs(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


def _fake_codex(root: Path, version: str, monkeypatch: pytest.MonkeyPatch) -> FakeCodex:
    package = root / "node_modules" / "@openai" / "codex"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    fake = FakeCodex(root, version)
    helper = root / "fake-codex.py"
    helper.write_text(
        "import base64, json, os, sys\n"
        f"LOG, ACCEPTS = {str(fake.log)!r}, {str(fake.accepts)!r}\n"
        "argv = sys.argv[1:]\n"
        "with open(LOG, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(argv) + '\\n')\n"
        "def part(o):\n"
        "    return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b'=').decode()\n"
        "with open(ACCEPTS, encoding='utf-8') as f:\n"
        "    accepted = json.load(f)\n"
        "if argv not in accepted:\n"
        "    print('Error logging in with device code: device code login is not enabled',\n"
        "          file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "claims = {'email': 'stub@example.com',\n"
        "          'https://api.openai.com/auth': {'chatgpt_plan_type': 'plus'}}\n"
        "token = part({'alg': 'none'}) + '.' + part(claims) + '.sig'\n"
        "with open(os.path.join(os.environ['CODEX_HOME'], 'auth.json'), 'w') as f:\n"
        "    json.dump({'auth_mode': 'chatgpt', 'tokens': {'id_token': token,\n"
        "               'refresh_token': 'r', 'access_token': 'a'}}, f)\n"
        "print('Successfully logged in')\n",
        encoding="utf-8",
    )
    script = executable_fake(
        package / "bin",
        "codex",
        posix=f'"{sys.executable}" "{helper}" "$@"',
        windows=f'"{sys.executable}" "{helper}" %*',
        exit_code=None,  # Codex's own status: 1 where no login landed
    )
    prepend_to_path(script.parent, monkeypatch)
    return fake


@pytest.fixture
def codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCodex:
    """The current release the beta holds, on PATH, signing in by device code alone."""
    return _fake_codex(tmp_path / "codex", CODEX_VERSIONS[-1], monkeypatch)


@pytest.fixture
def home() -> Path:
    """The redirected home ``conftest.isolated_agent_home`` gives the accounts core."""
    redirected = core._home()
    redirected.mkdir(parents=True, exist_ok=True)
    return redirected


# --------------------------------------------------------------------------- the argv


def test_every_release_the_beta_holds_was_measured_taking_device_auth() -> None:
    """No release the beta holds keeps plain ``login``: each was measured taking
    ``--device-auth`` (:data:`DEVICE_AUTH_MEASURED`), the floor included. A release added to
    CI's matrix fails here until ``codex login --device-auth`` is measured on it (in a
    scratch ``CODEX_HOME``), or gated to plain ``login`` on the version the adapter reads."""
    assert set(CODEX_VERSIONS) <= DEVICE_AUTH_MEASURED, sorted(
        set(CODEX_VERSIONS) - DEVICE_AUTH_MEASURED
    )
    assert min(CODEX_VERSIONS, key=lambda v: tuple(map(int, v.split(".")))) == "0.164.0"
    assert core.kind(CODEX).sign_in_args() == ("login", "--device-auth")
    assert core.kind(CODEX).browser_sign_in_args() == ("login",)
    assert codex_accounts.DEVICE_SIGN_IN_ARGS == ("login", "--device-auth")


@pytest.mark.parametrize("version", CODEX_VERSIONS)
def test_accounts_add_codex_runs_login_device_auth_on_each_release_the_beta_holds(
    version: str,
    home: Path,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``accounts add --agent codex`` runs ``codex login --device-auth`` in the new slot,
    whichever release is installed, and says what to do with what Codex prints: no word of
    a browser it opens."""
    codex = _fake_codex(tmp_path / f"codex-{version}", version, monkeypatch)
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)

    result = runner.invoke(app, ["accounts", "add", "--agent", "codex"])

    assert service.install(CODEX).version == version
    assert result.exit_code == 0, result.output
    assert codex.argvs() == [DEVICE]
    [slot] = core.managed_accounts(CODEX)
    assert result.stderr.startswith(
        f"Slot {slot.slot} is ready. `codex login --device-auth` runs now in it — {STEPS}, "
        "and this command records the account.\n"
    ), result.stderr
    assert f"Added Codex account {slot.slot}: stub@example.com" in result.stderr
    assert "browser" not in result.stderr


def test_accounts_add_codex_with_browser_runs_the_browser_sign_in(
    home: Path, codex: FakeCodex, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--browser`` runs plain ``codex login``, the browser's sign-in, for a ChatGPT login
    that does not allow device codes, and says so as ``accounts add`` said it before."""
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)
    codex.signs_in(BROWSER)

    result = runner.invoke(app, ["accounts", "add", "--agent", "codex", "--browser"])

    assert result.exit_code == 0, result.output
    assert codex.argvs() == [BROWSER]
    [slot] = core.managed_accounts(CODEX)
    assert result.stderr.startswith(
        f"Slot {slot.slot} is ready. `codex login` runs now in it — finish the sign-in in the "
        "browser it opens, and this command records the account.\n"
    ), result.stderr
    assert f"Added Codex account {slot.slot}: stub@example.com" in result.stderr


def test_browser_with_a_claude_code_add_is_refused_in_one_line_before_anything_runs(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code has one sign-in, its own TUI's: ``--browser`` is refused with one line, no
    slot is made and nothing is run, and under ``--json`` it is a usage error."""
    _claude_installed(monkeypatch)
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)
    started: list[ClaudeAccount] = []
    monkeypatch.setattr(service, "run_session", lambda account, *a, **k: started.append(account))

    refused = runner.invoke(app, ["accounts", "add", "--browser"])
    named = runner.invoke(app, ["accounts", "add", "--agent", "claude-code", "--browser"])
    as_json = runner.invoke(app, ["--json", "accounts", "add", "--browser"])

    line = "✗ --browser is for --agent codex only: Claude Code has one sign-in, its own login\n"
    assert (refused.exit_code, refused.stderr) == (1, line)
    assert (named.exit_code, named.stderr) == (1, line)
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout) == {"error": "usage", "ref": "claude-code"}
    assert started == [] and core.managed_accounts() == []


def test_a_device_code_sign_in_with_no_login_names_where_it_is_turned_on_and_browser(
    home: Path, codex: FakeCodex, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex exits with no login landed (a ChatGPT login that does not allow device codes):
    the failure line says where device-code sign-in is turned on and names ``--browser``. A
    ``--browser`` sign-in that fails names nothing more: there is no other way left."""
    monkeypatch.setattr(accounts_cli, "not_interactive_reason", lambda: None)
    codex.signs_in()  # neither way lands a login

    device = runner.invoke(app, ["accounts", "add", "--agent", "codex"])
    browser = runner.invoke(app, ["accounts", "add", "--agent", "codex", "--browser"])

    assert codex.argvs() == [DEVICE, BROWSER]
    assert device.exit_code == 1
    assert device.stderr.splitlines()[-1] == (
        "✗ no sign-in landed in slot 2 (Codex exited with status 1) — nothing was added. "
        f"{HINT}: aisquare accounts add --agent codex --browser"
    )
    assert browser.exit_code == 1
    assert browser.stderr.splitlines()[-1] == (
        "✗ no sign-in landed in slot 2 (Codex exited with status 1) — nothing was added"
    )
    assert core.managed_accounts(CODEX) == []  # both slots discarded


@pytest.mark.usefixtures("help_console")
def test_accounts_add_help_lists_browser_as_codexs_option() -> None:
    """The one line ``accounts add --help`` gains, the only one a Claude Code user sees. Drawn
    with no styles (``help_console``): CI forces a terminal, whose styles split ``--browser``
    into ``-`` and ``-browser``."""
    result = CliRunner().invoke(app, ["accounts", "add", "--help"])
    assert result.exit_code == 0, result.output
    [line] = [each for each in result.stdout.splitlines() if "--browser" in each]
    assert "Codex: sign in with the browser (`codex login`), not a device code" in line


# --------------------------------------------------------------------------- asq → Accounts


def _replay(window: Mapping[str, Any], runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> int:
    """Run a sign-in window's command as its pane would, here: ``accounts run <slot> …``,
    whose ``exec`` of the agent becomes a child process, so the fake codex records it."""
    command = list(window["command"])
    argv = command[command.index("aisquare", command.index("-m")) + 1 :]
    monkeypatch.setattr(service, "install", _REAL_INSTALL)  # the fake on PATH, not /opt/bin
    ran: list[int] = []

    def exec_(binary: str, args: list[str], env: dict[str, str]) -> None:
        ran.append(subprocess.run(args, env=env, check=False).returncode)

    monkeypatch.setattr(accounts_cli, "_exec", exec_)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    [status] = ran
    return status


def test_asq_add_codex_runs_login_device_auth_and_says_what_to_do_with_the_code(
    windows: list[dict[str, Any]],  # noqa: F811
    codex: FakeCodex,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """*+ Add* in the Codex section opens ``accounts run <slot> login --device-auth``, and
    its pane says to open the URL Codex prints on any device and enter the code. Run as the
    pane would, the command reaches codex as ``login --device-auth``."""
    monkeypatch.setattr(TmuxServer, "pane_facts", lambda self, pane: _Alive())
    # Kept past the page's close, which gives up a sign-in still open, to run its command.
    monkeypatch.setattr(service, "abandon_sign_in", lambda account: False)

    async def go(pilot: Pilot[None]) -> tuple[ClaudeAccount, str]:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        await pilot.click("#codex-add")
        await settle(app_)
        assert view.login is not None and view.login.fresh
        return view.login.account, shown(view.query_one("#login-header", Static))

    account, header = drive(go, overview=_real_overview())
    [window] = windows
    assert window["command"][-5:] == ["accounts", "run", str(account.slot), *DEVICE]
    assert header == (
        f"Logging in to account {account.slot} — `codex login --device-auth` runs in this "
        f"pane: {STEPS}. This pane closes by itself once the login is recorded; F12 hands "
        "focus back to the sidebar."
    )
    assert _replay(window, runner, monkeypatch) == 0
    assert codex.argvs() == [DEVICE]
    assert core.identity(account) is not None  # the fake's device sign-in landed


def test_asq_log_in_on_a_codex_slot_runs_login_device_auth_there(
    windows: list[dict[str, Any]],  # noqa: F811
    codex: FakeCodex,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """*Log in* on a Codex row (where Welcome's and the doctor's sign-ins land too) runs
    ``login --device-auth`` in that slot, and its tooltip names it."""
    account = core.create_account(CODEX)  # a slot whose login never landed
    monkeypatch.setattr(TmuxServer, "pane_facts", lambda self, pane: _Alive())

    async def go(pilot: Pilot[None]) -> bool:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        view.begin_claude_sign_in(account.slot, agent=CODEX)
        await settle(app_)
        return view.login is not None and not view.login.fresh

    assert drive(go, overview=_real_overview())
    [window] = windows
    assert window["command"][-5:] == ["accounts", "run", str(account.slot), *DEVICE]
    assert accounts_view._sign_in_opens(CODEX) == "`codex login --device-auth` opens below"
    assert _replay(window, runner, monkeypatch) == 0
    assert codex.argvs() == [DEVICE]
    assert core.identity(account) is not None


def test_the_doctors_sign_in_for_a_codex_slot_stays_one_click_that_opens_its_log_in() -> None:
    """The doctor's row for a Codex slot with no login names its device-code sign-in after
    the slot, and the Doctor page still makes it one click, *Sign in account 2*, which opens
    that slot's *Log in* (where ``login --device-auth`` runs): a button admits after a slot
    any token of an agent's own ``sign_in_args``, read from the agents."""
    account = core.create_account(CODEX)  # a slot whose login never landed

    [check] = diagnostics._agent_accounts_checks(CODEX)

    run = " ".join(("aisquare accounts run", str(account.slot), *DEVICE))
    hint = f"Sign in from asq → Accounts, or: {run}"
    assert (check.name, check.fix) == ("codex-accounts", hint)
    assert onboarding.fix_commands([check]) == [
        onboarding.FixCommand(
            check="codex-accounts",
            argv=("accounts", "run", str(account.slot)),
            scope="machine",
            source=hint,
            interactive=True,
        )
    ]
    assert _doctor_buttons([check]) == (
        [
            (
                f"Sign in account {account.slot}",
                f"{hint}\nOpens the Accounts page with aisquare accounts run "
                f"{account.slot} in its pane.",
            )
        ],
        [account.slot],
    )


def test_a_codex_pane_that_closes_with_no_login_names_the_browser_sign_in(
    windows: list[dict[str, Any]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device-code sign-in whose Codex closed with no login: a new slot's notice names
    ``accounts add --agent codex --browser``, a *Log in*'s the browser's sign-in run on its
    slot, each after where device-code sign-in is turned on."""
    existing = core.create_account(CODEX)
    said: dict[str, str] = {}

    async def go(pilot: Pilot[None]) -> None:
        app_ = fleet_app(pilot)
        view = await open_accounts(pilot)
        slots: tuple[tuple[str, int | None], ...] = (("fresh", None), ("existing", existing.slot))
        for key, slot in slots:
            view.begin_claude_sign_in(slot, agent=CODEX)
            await settle(app_)
            assert view.login is not None
            view._poll_login()  # the recorder answers "no server": the pane closed
            await pilot.pause()
            await settle(app_)
            said[key] = notice(view)

    drive(go, overview=_real_overview())
    assert said["fresh"] == (
        "sign-in cancelled — Codex closed before a sign-in landed; the new slot was "
        f"discarded. {HINT}: aisquare accounts add --agent codex --browser"
    )
    assert said["existing"] == (
        "sign-in cancelled — Codex closed before a sign-in landed; nothing changed. "
        f"{HINT}: aisquare accounts run {existing.slot} login"
    )
