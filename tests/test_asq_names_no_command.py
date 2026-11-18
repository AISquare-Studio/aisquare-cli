"""asq never points its person to the command line (R13, #146).

The owner: "the whole purpose of TUI is to make it for users to never use the command line
since their manager is aware of it, so hide all of that". asq's own strings name no command
to type; the Doctor's buttons say what they do, never their command line; a row with no
button is handed to the manager (ruled Q2), which knows the command line, or says plainly
that a manager can fix it. The shell keeps every command: ``doctor``'s text, ``--help``,
errors printed in a terminal.
"""

from __future__ import annotations

import ast
import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Static

from aisquare.cli.ui.views.doctor import (
    COMMAND,
    NO_MANAGER_WAY,
    DoctorView,
    ask_text,
    render_checks,
    ui_way,
)
from aisquare.core import selfcli
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services.onboarding import FixCommand, FixResult

UI = Path(__file__).resolve().parents[1] / "src" / "aisquare" / "cli" / "ui"

PRINTED_IN_A_TERMINAL = {"instance_gate.py"}
"""asq's files whose text is printed in the terminal, before asq draws or after it quits:
the person is at a shell there, and a command is the way."""

BUILDERS = re.compile(
    r"\b(?:prog\.(?:line|words|command)|install_route\.command_line|fleet_command|"
    r"onboard_command|forget_command|config_dir_command)\("
)
"""The functions that spell a command; asq's views call none (``prog.word()`` names the product)."""

BUILDERS_ALLOWED = {
    ("app.py", "install_route.command_line("),  # run_ui's hand-off, printed after asq quits
}


def _docstrings(tree: ast.AST) -> set[int]:
    """The literals no person reads on screen: docstrings, attributes' doc strings, and the
    sources of patterns (``re.compile``), this guard's own among them."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            out.add(id(node.value))  # a docstring, or an attribute's: read by developers
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
        ):
            out.update(id(inner) for inner in ast.walk(node) if isinstance(inner, ast.Constant))
    return out


def _literals() -> Iterator[tuple[str, int, str]]:
    for path in sorted(UI.rglob("*.py")):
        if path.name in PRINTED_IN_A_TERMINAL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
            ):
                yield path.relative_to(UI).as_posix(), node.lineno, node.value


# --- the guard ---------------------------------------------------------------------------------


def test_no_asq_string_names_a_command_to_type() -> None:
    """Every string asq shows — f-strings' literal parts included — is free of a command."""
    found = [
        f"{name}:{line}: {COMMAND.search(text).group(0)!r} in {text[:70]!r}"  # type: ignore[union-attr]
        for name, line, text in _literals()
        if COMMAND.search(text)
    ]
    assert found == []


def test_the_guard_catches_what_it_is_for() -> None:
    for said in (
        "start one: rlx fleet spawn --project x manager",
        "aisquare doctor --live",
        "use `--project amber`",
        "tmux -L asq attach -t =asq-x",
        "install it: npm install -g @openai/codex",
        "curl -fsSL https://claude.ai/install.sh | bash",
    ):
        assert COMMAND.search(said), said
    for prose in ("rlx — keys", "Restart on its sidebar row", "rlx's doctor: its tmux check"):
        assert not COMMAND.search(prose), prose


def test_asq_calls_no_function_that_spells_a_command() -> None:
    """f-strings built with a command builder are not literals: asq's views call none, but for
    the shell's own prints before asq draws or after it quits."""
    found = []
    for path in sorted(UI.rglob("*.py")):
        if path.name in PRINTED_IN_A_TERMINAL:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            match = BUILDERS.search(line)
            if match and (path.name, match.group(0)) not in BUILDERS_ALLOWED:
                found.append(f"{path.relative_to(UI).as_posix()}:{number}: {stripped[:80]}")
    assert found == []


# --- the Doctor's buttons ----------------------------------------------------------------------


def test_a_fix_button_says_what_it_does_and_which_home_never_its_command() -> None:
    home = str(Path.home())
    connect = FixCommand("claude-code", ("agents", "connect", "claude-code"), "machine", "")
    # As a fix spells it on this platform, and with "/" after the home, as one written by
    # hand on Windows has it: both shown from "~", in this platform's separators (#517).
    native = str(Path.home() / ".aisquare" / "codex-accounts" / "4")
    slot = FixCommand(
        "claude-code",
        ("agents", "connect", "codex", "--config-dir", f"{home}/.aisquare/codex-accounts/3"),
        "machine",
        "",
    )
    slot_4 = FixCommand(
        "claude-code", ("agents", "connect", "codex", "--config-dir", native), "machine", ""
    )
    elsewhere = FixCommand(
        "c", ("agents", "connect", "claude-code", "--config-dir", "/srv/x"), "machine", ""
    )
    refresh = FixCommand("p", ("project", "onboard", "--refresh"), "project", "")
    fix_all = FixCommand("d", ("doctor", "--fix", "--yes"), "machine", "")
    sign_in = FixCommand("a", ("accounts", "run", "3"), "machine", "", interactive=True)

    assert connect.title == "Connect Claude Code"
    assert slot.title == f"Connect Codex ({Path('~', '.aisquare', 'codex-accounts', '3')})"
    assert slot_4.title == f"Connect Codex ({Path('~', '.aisquare', 'codex-accounts', '4')})"
    assert elsewhere.title == "Connect Claude Code (/srv/x)"
    assert refresh.title == "Refresh this project"
    assert fix_all.title == "Fix what the checks can"
    assert sign_in.title == "Sign in account 3"
    assert connect.label.endswith("agents connect claude-code"), "the shell's form stays"


def test_a_fix_s_result_says_its_title_never_its_command() -> None:
    fix = FixCommand("c", ("agents", "connect", "claude-code"), "machine", "")
    failed = FixResult(fix, 1, reason=f"{fix.label} failed: not_installed")

    assert FixResult(fix, 0).said() == "✓ Connect Claude Code"
    assert failed.said() == "✗ Connect Claude Code: Connect Claude Code failed: not_installed"
    assert failed.summary().startswith(f"✗ {fix.label}"), "the shell's summary stays"


# --- what a row says, and Ask the manager -----------------------------------------------------

WARN = DoctorCheck(
    name="fleet watcher",
    status=CheckStatus.warn,
    detail="not running",
    fix="rlx watch --stop, then rlx watch --detach",
)
CONNECT = DoctorCheck(
    name="claude-code",
    status=CheckStatus.warn,
    detail="hooks are missing",
    fix="(Re)connect it: rlx agents connect claude-code",
)
OWN_WAY = WARN.model_copy(update={"name": "fleet-manager", "ui_fix": "Restart it from its row"})
OK = DoctorCheck(name="python", status=CheckStatus.ok, detail="3.12", fix="never shown")


def test_a_row_says_rlx_s_own_way_never_the_shell_s_command() -> None:
    buttons = {"claude-code": "Connect Claude Code"}

    assert ui_way(OWN_WAY, buttons) == "Restart it from its row"
    assert ui_way(CONNECT, buttons) == "Connect Claude Code below fixes it"
    assert ui_way(WARN, buttons, manager="api's manager") == "Ask api's manager below to fix it"
    assert ui_way(WARN, buttons) == NO_MANAGER_WAY
    assert ui_way(OK, buttons) is None
    report = render_checks([OK, CONNECT, WARN, OWN_WAY], buttons).plain
    assert "rlx " not in report and "never shown" not in report


def test_the_manager_is_handed_the_shell_s_fix_whole() -> None:
    said = ask_text(WARN)
    assert "fleet watcher check warns: not running" in said
    assert "Its fix: rlx watch --stop, then rlx watch --detach" in said


class Host(App[None]):
    def __init__(self, view: DoctorView) -> None:
        super().__init__()
        self.view = view
        self.asked: list[tuple[str, Path | None]] = []

    def compose(self) -> ComposeResult:
        yield self.view

    def on_doctor_view_ask_manager(self, event: DoctorView.AskManager) -> None:
        self.asked.append((event.check.name, event.cwd))


def _doctor(
    manager: str | None, tmp_path: Path
) -> tuple[list[str], list[tuple[str, Path | None]], str]:
    async def drive() -> tuple[list[str], list[tuple[str, Path | None]], str]:
        view = DoctorView(cwd=tmp_path, run=selfcli.run, manager=lambda cwd: manager)
        host = Host(view)
        async with host.run_test(size=(140, 40)) as pilot:
            view.show([OK, CONNECT, WARN, OWN_WAY])
            await pilot.pause()
            await pilot.pause()
            labels = [str(b.label) for b in view.query("#doctor-fixes Button").results(Button)]
            ask = next(iter(view.asks), None)
            if ask is not None:
                await pilot.click(f"#{ask}")
                await pilot.pause()
            report = str(view.query_one("#doctor-report", Static).render())
            return labels, host.asked, report

    return asyncio.run(drive())


def test_with_a_manager_a_row_with_no_button_is_asked_of_it(tmp_path: Path) -> None:
    labels, asked, report = _doctor("api's manager", tmp_path)

    assert labels == ["Connect Claude Code", "Ask the manager: fleet watcher"]
    assert asked == [("fleet watcher", tmp_path)], "the row, and the report's own project"
    assert "→ Ask api's manager below to fix it" in report
    assert "→ Restart it from its row" in report, "a row with rlx's own way is not asked"


def test_with_no_manager_there_is_no_ask_and_the_row_says_so(tmp_path: Path) -> None:
    labels, asked, report = _doctor(None, tmp_path)

    assert labels == ["Connect Claude Code"] and asked == []
    assert f"→ {NO_MANAGER_WAY}" in report


def test_a_fix_with_no_command_is_said_as_it_is_and_is_not_asked() -> None:
    prose = DoctorCheck(
        name="workspace-credits",
        status=CheckStatus.warn,
        detail="low",
        fix="Top up the workspace in the dashboard before spawning a fleet into it",
    )
    assert ui_way(prose, {}, manager="api's manager") == prose.fix


def test_the_tmux_row_links_its_page_in_asq_and_keeps_its_command_for_the_shell() -> None:
    from aisquare.services import diagnostics

    class Missing:
        def available(self) -> bool:
            return False

    row = diagnostics._check_tmux(Missing())  # type: ignore[arg-type]
    assert row.ui_fix is not None and diagnostics.TMUX_INSTALL_PAGE in row.ui_fix
    assert row.fix is not None and row.fix.startswith("Install it: "), "the shell keeps it"


#: (platform, /etc/os-release): macOS, a Debian-family Linux, a Fedora one, one the hint table
#: does not know (it then names every package manager), and Windows (the fleet's WSL2, apt).
_PLATFORMS: dict[str, tuple[str, str | None]] = {
    "macos": ("darwin", None),
    "debian": ("linux", 'ID=ubuntu\nID_LIKE="debian"\n'),
    "fedora": ("linux", "ID=fedora\n"),
    "unknown-linux": ("linux", "ID=arch\n"),
    "windows-wsl": ("win32", None),
}


class _Tmux:
    """A tmux seam at ``version`` (``None``: not installed)."""

    def __init__(self, version: tuple[int, int] | None) -> None:
        self._version = version

    def available(self) -> bool:
        return self._version is not None

    def version(self) -> tuple[int, int] | None:
        return self._version

    def binary(self) -> str:
        return "/usr/bin/tmux"


@pytest.mark.parametrize("where", sorted(_PLATFORMS))
def test_the_install_rows_name_no_command_in_asq_on_any_platform(
    where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #503, m1: on Linux and WSL ``install_hint`` names apt or dnf, which COMMAND did
    not know, so asq's Doctor showed "Upgrade it: apt install tmux" and "Install GitHub CLI: apt
    install gh, then: gh auth login" as plain words. On every platform each row's shell fix keeps
    its command, which COMMAND knows for one, and what asq shows (:func:`ui_way`) names none."""
    from aisquare.services import diagnostics

    platform, release = _PLATFORMS[where]
    os_release = tmp_path / "os-release"
    if release is not None:
        os_release.write_text(release, encoding="utf-8")
    real = diagnostics.install_hint

    def hint(package: str) -> str:
        return real(package, platform=platform, os_release=os_release)

    def nowhere(name: str, *args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(diagnostics, "install_hint", hint)
    monkeypatch.setattr("shutil.which", nowhere)
    rows = [
        diagnostics._check_tmux(_Tmux(None)),  # type: ignore[arg-type]
        diagnostics._check_tmux(_Tmux((3, 0))),  # type: ignore[arg-type]
        diagnostics._check_gh(),
    ]
    assert [row.status for row in rows] == [CheckStatus.warn] * 3
    pages = [
        diagnostics.TMUX_INSTALL_PAGE,
        diagnostics.TMUX_INSTALL_PAGE,
        diagnostics.GH_INSTALL_PAGE,
    ]
    for row, page in zip(rows, pages, strict=True):
        assert row.fix is not None and COMMAND.search(row.fix), (row.name, row.fix)
        way = ui_way(row, {})
        assert way is not None and not COMMAND.search(way), (row.name, way)
        assert page in way, f"{row.name}: asq links its install page: {way}"


def test_the_gh_rows_sign_in_note_names_its_command_at_a_shell_and_none_in_asq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #503, m1: an ok gh row's detail ended "(no login found: gh auth login)" in asq
    too. The shell keeps the command; asq says what is missing (``detail_advice``)."""
    from aisquare.core import program as prog
    from aisquare.services import diagnostics

    def found(name: str, *args: Any, **kwargs: Any) -> str:
        return "/usr/bin/gh"

    for variable in ("GH_TOKEN", "GITHUB_TOKEN", prog.IN_APP_ENV):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(diagnostics, "_gh_config_dir", lambda: tmp_path)  # no hosts.yml
    monkeypatch.setattr("shutil.which", found)
    shell = diagnostics._check_gh()
    assert shell.status is CheckStatus.ok and "(no login found: gh auth login)" in shell.detail
    monkeypatch.setenv(prog.IN_APP_ENV, "1")
    app = diagnostics._check_gh()
    assert app.detail.endswith("(not signed in to GitHub CLI)"), app.detail
    assert not COMMAND.search(app.detail), app.detail


def test_no_doctor_row_s_asq_wording_names_a_command() -> None:
    from aisquare.services import diagnostics

    rows = diagnostics.doctor()
    assert rows, "the doctor ran"
    assert [row.name for row in rows if row.ui_fix and COMMAND.search(row.ui_fix)] == []
