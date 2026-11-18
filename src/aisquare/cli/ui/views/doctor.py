"""The Doctor view: one project's checks with their fixes, and a button per fix we can run.

docs/plans/fleet-tui.md §0 item 4 and §4.2: the findings are visible with the
fix for each, and where the fix is one of our own commands it is one click
(``services.onboarding.fix_commands`` decides which — everything else stays
text). A click runs ``apply_fix`` in a thread worker with the project's cwd,
then re-runs the doctor and posts :class:`DoctorRefreshed` so the shell's report
can follow. The UI process never changes directory. A slot's sign-in (``aisquare
accounts run <n>``, ``… login --device-auth`` for Codex) needs the agent's own terminal, so its
button (*Sign in account N*) runs nothing here: it posts
``AccountsSelected(sign_in=n)``, and the Accounts page opens that sign-in in its
pane and records the account when it lands.

asq's own Doctor (``machine=True``, never the Onboard copy) also carries a
Re-run button — the shell runs the checks at start, when the page opens and
after a fix, and this is the way to ask again — and Update and Uninstall.
Those two hand the terminal over rather than run in here: the view posts
:class:`DoctorView.HandOff`, the app quits, and ``run_ui`` hands this terminal
to ``aisquare upgrade --reopen`` or ``aisquare uninstall``, which show their
plan and ask y/N themselves. An upgrade must not run under the
UI it replaces, and asq comes back after it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Static

from aisquare.cli.ui.sidebar import AccountsSelected
from aisquare.core import program, selfcli
from aisquare.core.agent_adapters import stage_mark_of
from aisquare.core.selfcli import CliResult
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import install_route, onboarding
from aisquare.services.onboarding import FixCommand, FixResult, Runner

_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}
_STYLE = {CheckStatus.ok: "green", CheckStatus.warn: "yellow", CheckStatus.fail: "bold red"}
FIX_WORKER = "doctor-fix"
"""The worker a one-click fix runs in (and the doctor after it): it writes."""
ASK_WORKER = "doctor-ask-manager"
"""The worker *Ask the manager* hands a row to the manager in (``fleet.tell``): it writes."""
ROUTE_WORKER = "doctor-update-route"
"""The worker the machine Doctor reads how rlx was installed in, for Update."""


def _update_tooltip() -> str:
    """What Update does, for its tooltip: what happens, never the command (R13)."""
    return (
        "Quits rlx and updates it here: it shows what it will do and asks first. rlx opens "
        "again when it is done."
    )


Refresh = Callable[[Path | None], list[DoctorCheck]]
"""``cwd -> checks``: how the view re-runs the doctor after a fix. Raising is
allowed and means "no answer" — the view keeps the last report and says why."""


class DoctorRefreshed(Message):
    """The doctor was re-run (after a fix); ``checks`` is the new report."""

    def __init__(self, checks: list[DoctorCheck], cwd: Path | None = None) -> None:
        self.checks = checks
        self.cwd = cwd
        super().__init__()


class FixApplied(Message):
    """One fix button finished; ``result`` says whether it worked and why not."""

    def __init__(self, result: FixResult) -> None:
        self.result = result
        super().__init__()


_SUBCOMMANDS = (
    "fleet|team|accounts|agents|project|doctor|config|remote|explainability|metrics|captain|"
    "upgrade|uninstall|instances|launch|init|login|logout|board|watch|persona|task|note|hook|code"
)
COMMAND = re.compile(
    rf"\b(?:rlx|aisquare|asq|relax) (?:{_SUBCOMMANDS})\b"
    r"|`--[a-z][\w-]*"
    r"|\btmux -[LS]\b|\bnpm install\b|\bpip install\b|\bbrew install\b|\buv tool\b"
    r"|curl -fsSL|\bpipx (?:install|inject|upgrade)\b"
    # Linux's package managers, as install_hint names them there and on WSL, and gh's sign-in
    # (review of #503, m1: asq's Doctor passed these through as plain words).
    r"|\b(?:apt|apt-get|dnf|yum|zypper) install\b|\bpacman -S\b|\bgh auth login\b"
    r"|\bclaude mcp add\b"
)
"""What a command to type looks like in text asq would show (R13): ours, a package manager's,
tmux's, gh's, a backticked flag. ``tests/test_asq_names_no_command.py`` sweeps asq's strings
with it."""


def names_a_command(text: str) -> bool:
    """Whether ``text`` tells its reader a command to type (:data:`COMMAND`)."""
    return COMMAND.search(text) is not None


NO_MANAGER_WAY = "a project's manager can fix this: start one, then ask it here"
"""What a row says whose fix rlx has no button for, while no manager runs to ask (ruling Q2)."""


def ui_way(
    check: DoctorCheck, buttons: Mapping[str, str], *, manager: str | None = None
) -> str | None:
    """What asq's Doctor says under a row in place of its shell ``fix`` (R13, #146): the row's
    own ``ui_fix``; else the one-click button its fix makes (``buttons``: check name → its
    title); else, when a manager runs, the Ask button beside it; else plain words. ``None``
    for a row that is ok or has no fix. Never a command: the manager, asked, is handed it."""
    if check.status is CheckStatus.ok or not (check.fix or check.ui_fix):
        return None
    if check.ui_fix:
        return check.ui_fix
    if check.fix and not names_a_command(check.fix):
        return check.fix  # plain words already: the dashboard, a reset to wait for
    if check.name in buttons:
        return f"{buttons[check.name]} below fixes it"
    if manager is not None:
        return f"Ask {manager} below to fix it"
    return NO_MANAGER_WAY


def render_checks(
    checks: list[DoctorCheck],
    buttons: Mapping[str, str] | None = None,
    *,
    manager: str | None = None,
) -> Text:
    """The doctor's report as asq shows it: each row, and under one that is not ok what
    rlx itself has for it (:func:`ui_way`), never the shell's command."""
    text = Text()
    for check in checks:
        name = check.name + stage_mark_of(check.name)  # a beta agent's row, as the CLI's
        text.append(f"{_SYMBOL[check.status]} {name}: ", style=_STYLE[check.status])
        text.append(f"{check.detail}\n")
        way = ui_way(check, buttons or {}, manager=manager)
        if way is not None:
            text.append(f"    → {way}\n", style="dim")
    if not checks:
        text.append("(no checks yet)", style="dim")
    return text


def ask_text(check: DoctorCheck) -> str:
    """What a manager is handed when asked to fix ``check`` (ruling Q2): the row and its shell
    fix, which the manager, unlike asq's person, runs."""
    said = "fails" if check.status is CheckStatus.fail else "warns"
    return (
        f"rlx's doctor: its {check.name} check {said}: {check.detail}\n"
        f"Its fix: {check.fix}\n"
        "Please fix it, then tell me what you did."
    )


def _shown_here(run: Runner) -> Runner:
    """``run`` for the report asq shows: ``selfcli.run``'s child is told so
    (``program.IN_APP_ENV``), and its rows' details leave out their commands; every fix stays
    the shell's (R13b). A stand-in is left as it is, and the fixes run as they always did:
    a fix may start what lasts, and nothing that lasts carries the variable."""
    if run is not selfcli.run:
        return run

    def shown(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        return selfcli.run(args, cwd=cwd, env={**os.environ, program.IN_APP_ENV: "1"})

    return shown


def _doctor_via_cli(run: Runner) -> Refresh:
    def refresh(cwd: Path | None) -> list[DoctorCheck]:
        checks, error = onboarding.run_doctor(cwd, run=_shown_here(run))
        if error is not None:
            raise RuntimeError(error)
        return checks

    return refresh


#: The machine buttons and the ``aisquare`` arguments each hands the terminal to.
#: ``--reopen`` brings asq back, after Enter, unless the upgrade failed or the
#: uninstall removed something (then there is no asq to come back to).
HAND_OFFS: dict[str, tuple[str, ...]] = {
    "doctor-update": ("upgrade", "--reopen"),
    "doctor-uninstall": ("uninstall", "--reopen"),
}


def _update_refusal() -> str | None:
    """Why ``aisquare upgrade`` would only refuse this install, with its command, or ``None``.

    ``install_route``'s own offline answer (files under ``sys.prefix`` and one PATH
    lookup): Windows, an editable checkout, pipx, Homebrew, a plain venv or system
    pip are told a command instead of upgraded, so Update would only cost the asq
    session. An answer that cannot be had offers the button: the CLI then says why.
    """
    try:
        route = install_route.detect()
        reason = install_route.not_automated(route)
        if reason is None:
            return None
    except Exception:  # the Doctor view must keep its buttons whatever this finds
        return None
    # The way it was installed updates it; asq names no command to type (R13).
    return (
        f"rlx does not upgrade this install itself ({reason}). Update it the way you installed it."
    )


class DoctorView(VerticalScroll):
    """Scrollable doctor report for the selected project, with one-click fixes.

    ``cwd`` is the project root the fixes run in; the shell sets it through
    ``show(checks, cwd=…)`` (or the constructor). Without one, machine-level
    fixes (``agents connect``, ``doctor --fix``) still
    run — they do not care where — and project-level ones (``project onboard``)
    render disabled rather than silently onboarding whatever directory the UI
    happens to have been started from.
    """

    DEFAULT_CSS = """
    DoctorView { padding: 1 2; }
    DoctorView #doctor-scope { height: auto; margin-bottom: 1; text-style: bold; }
    DoctorView #doctor-rerun { margin-bottom: 1; }
    DoctorView #doctor-status { height: auto; margin-top: 1; }
    DoctorView #doctor-fixes { height: auto; }
    DoctorView #doctor-fixes Button { margin: 1 1 0 0; }
    DoctorView #doctor-machine { height: auto; margin-top: 1; }
    DoctorView #doctor-machine Button { margin: 0 1 0 0; }
    """

    class Rerun(Message):
        """Re-run: the shell runs the checks again for the scope this report is about."""

    class HandOff(Message):
        """Update or Uninstall: quit asq and run ``aisquare <args>`` in this terminal."""

        def __init__(self, args: tuple[str, ...]) -> None:
            self.args = args
            super().__init__()

    class AskManager(Message):
        """Ask the manager: hand ``check``'s fix to the manager the app finds for ``cwd``,
        which knows the command line asq never asks its person to use (ruling Q2)."""

        def __init__(self, check: DoctorCheck, cwd: Path | None) -> None:
            self.check = check
            self.cwd = cwd
            super().__init__()

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        run: Runner = selfcli.run,
        refresh: Refresh | None = None,
        id: str | None = None,
        classes: str | None = None,
        machine: bool = False,
        update_refusal: Callable[[], str | None] | None = None,
        manager: Callable[[Path | None], str | None] | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._manager = manager
        """Who *Ask the manager* asks for a report about ``cwd`` (``"coder's manager"``), from
        what the app already holds; ``None`` (or no seam): no manager runs, and a row says so."""
        self.asks: dict[str, DoctorCheck] = {}
        """Button id → the row its *Ask the manager* hands to the manager."""
        self.machine = machine
        """asq's own Doctor: also Update and Uninstall, which act on the whole install."""
        self._update_refusal = update_refusal
        self.cwd = cwd
        self._run = run
        self._refresh: Refresh = refresh if refresh is not None else _doctor_via_cli(run)
        self.checks: list[DoctorCheck] = []
        self.fixes: dict[str, FixCommand] = {}
        """Button id → the fix it runs, for the report currently shown."""
        self.busy = False
        self.about: str | None = None
        """What the report on screen is about — a project's name, or "this machine" — named
        in the machine Doctor's header (review of #261, m9: nothing on screen said which
        project the rows described)."""
        self._rerunning = False
        """A run is under way and no report has arrived since: the status line says so."""

    def compose(self) -> ComposeResult:
        if self.machine:
            yield Static("", id="doctor-scope")
            rerun = Button("Re-run checks", id="doctor-rerun")
            rerun.tooltip = "Run every check again now"
            yield rerun
        yield Static("", id="doctor-report")
        yield Static("", id="doctor-status")
        yield Vertical(id="doctor-fixes")
        if self.machine:
            with Horizontal(id="doctor-machine"):
                # Offered once the install route is known (_read_update_refusal).
                update = Button("Update rlx", id="doctor-update", disabled=True)
                update.tooltip = "Checking how rlx was installed…"
                yield update
                uninstall = Button("Uninstall rlx…", id="doctor-uninstall", variant="error")
                uninstall.tooltip = (
                    "Quits rlx and uninstalls it here: it lists what goes and what stays, and "
                    "asks first. rlx opens again if nothing is removed."
                )
                yield uninstall

    def on_mount(self) -> None:
        self._redraw()
        if self.machine:
            self._read_update_refusal()

    @work(thread=True, name=ROUTE_WORKER, group=ROUTE_WORKER, exit_on_error=False)
    def _read_update_refusal(self) -> None:
        """Whether Update would only refuse (:func:`_update_refusal`), read off the UI thread.

        ``install_route.detect`` reads distribution metadata, uv's receipt and PATH. Read
        as the machine Doctor composed, at every asq start, it ran before asq's first
        paint, which showed on a network-mounted home (round 23 of #257).
        """
        refusal = (self._update_refusal or _update_refusal)()
        self.app.call_from_thread(self._offer_update, refusal)

    def _offer_update(self, refusal: str | None) -> None:
        update = self.query_one("#doctor-update", Button)
        update.tooltip = Text(refusal or _update_tooltip())
        update.disabled = refusal is not None
        if refusal is not None:  # said on screen, not only on hover
            note = Static(Text(f"Update: {refusal}", style="dim"), id="doctor-update-note")
            self.mount(note, after=self.query_one("#doctor-machine"))

    # ---------------------------------------------------------------- the report

    def show(
        self, checks: Iterable[DoctorCheck], *, cwd: Path | None = None, about: str | None = None
    ) -> None:
        """Replace the report (and, when given, the cwd the fixes run in and what it is about).

        ``cwd=None`` means "keep the one you have": most callers show a new
        report for the same project. A caller that has to go BACK to no project
        — the shell, when the report becomes the machine-wide one — sets
        :attr:`cwd` itself before calling this, or a project fix button would
        stay armed on a root the user has left. ``about=None`` keeps :attr:`about`.
        """
        if cwd is not None:
            self.cwd = cwd
        if about is not None:
            self.about = about
        self.checks = list(checks)
        if self._rerunning:
            self._rerunning = False
            if self.is_mounted:
                self._set_status(Text(""))
        if self.is_mounted:
            self._redraw()

    def show_running(self, about: str | None = None) -> None:
        """A run of the checks about ``about`` (default: :attr:`about`) is under way.

        The report on screen stays until the run answers — the one from asq's start
        is there at once when the page opens — and the status line says the checks
        are running again, or for what, when the report shown is about something else.
        """
        about = about or self.about
        self._rerunning = True
        if not self.checks:
            self.about = about  # nothing on screen is about anything else yet
            status = "… running the checks"
        elif about == self.about:
            status = "… running the checks again"
        else:
            status = f"… running the checks for {about}"
        if self.is_mounted:
            self._set_status(Text(status, style="dim"))
            self._show_about()

    def _manager_name(self) -> str | None:
        try:
            return self._manager(self.cwd) if self._manager is not None else None
        except Exception:  # what the app holds could not say: no Ask, plain words
            return None

    def _redraw(self) -> None:
        self._show_about()
        buttons: dict[str, str] = {}
        for fix in onboarding.fix_commands(self.checks):
            buttons.setdefault(fix.check, fix.title)
        report = render_checks(self.checks, buttons, manager=self._manager_name())
        self.query_one("#doctor-report", Static).update(report)
        self.call_later(self._rebuild_fixes)

    def _show_about(self) -> None:
        if self.machine:
            header = Text(f"checks for {self.about}") if self.about else Text("")
            self.query_one("#doctor-scope", Static).update(header)

    async def _rebuild_fixes(self) -> None:
        """Replace the buttons for the report now shown.

        The removal is AWAITED before the mount: ids are reused across reports
        (``fix-0`` is always the first button), and mounting a new ``fix-0``
        while the old one is still leaving the DOM is a duplicate-id crash.
        """
        container = self.query_one("#doctor-fixes", Vertical)
        await container.remove_children()
        fixes: dict[str, FixCommand] = {}
        buttons: list[Button] = []
        for index, fix in enumerate(onboarding.fix_commands(self.checks)):
            button_id = f"fix-{index}"
            fixes[button_id] = fix
            # ``Text``, never the str: a Button parses a str label as markup. What it does,
            # never its command line (R13): ``Connect Claude Code``, ``Sign in account 3``.
            slot = fix.sign_in_slot
            button = Button(Text(fix.title), variant="warning", id=button_id)
            tooltip = Text(f"Fixes the {fix.check} check.")
            if slot is not None:
                tooltip = Text(f"Opens the Accounts page to sign in to account {slot}.")
            if fix.scope == "project" and self.cwd is None:
                button.disabled = True
                tooltip.append("\n(select a project first — this runs in its root)")
            button.tooltip = tooltip
            buttons.append(button)
        self.fixes = fixes
        asks: dict[str, DoctorCheck] = {}
        manager = self._manager_name()
        fixed = {fix.check for fix in fixes.values()}
        for index, check in enumerate(self.checks):
            if (
                manager is None
                or check.status is CheckStatus.ok
                or not check.fix
                or not names_a_command(check.fix)  # said as it is: nothing to hand over
                or check.ui_fix
                or check.name in fixed
            ):
                continue
            button_id = f"ask-{index}"
            asks[button_id] = check
            button = Button(Text(f"Ask the manager: {check.name}"), id=button_id)
            button.tooltip = Text(f"Hands the {check.name} check's fix to {manager}.")
            buttons.append(button)
        self.asks = asks
        if buttons:
            await container.mount(*buttons)

    def _set_status(self, text: Text) -> None:
        self.query_one("#doctor-status", Static).update(text)

    # ---------------------------------------------------------------- the click

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.machine and event.button.id == "doctor-rerun":
            event.stop()
            self.show_running()
            self.post_message(self.Rerun())
            return
        hand_off = HAND_OFFS.get(event.button.id or "") if self.machine else None
        if hand_off is not None:
            # The app decides: it refuses while ANY Doctor view's fix is still writing.
            event.stop()
            self.post_message(self.HandOff(hand_off))
            return
        asked = self.asks.get(event.button.id or "")
        if asked is not None:
            event.stop()
            self.post_message(self.AskManager(asked, self.cwd))
            return
        fix = self.fixes.get(event.button.id or "")
        if fix is None:
            return
        event.stop()
        if fix.sign_in_slot is not None:
            # The agent's own login needs a terminal: the Accounts page opens it in its
            # pane and records the account when it lands; the report follows its refresh.
            self.post_message(AccountsSelected(sign_in=fix.sign_in_slot))
            return
        if self.busy:
            return
        self.busy = True
        for button in self.query("#doctor-fixes Button").results(Button):
            button.disabled = True
        self._set_status(Text(f"… {fix.title}", style="dim"))
        self._apply(fix)

    @work(thread=True, exclusive=True, name=FIX_WORKER, group=FIX_WORKER, exit_on_error=False)
    def _apply(self, fix: FixCommand) -> None:
        """Run the fix, then the doctor, off the UI thread; report back on it."""
        result = onboarding.apply_fix(fix, self.cwd, run=self._run)
        checks: list[DoctorCheck] | None
        recheck_error: str | None = None
        try:
            checks = self._refresh(self.cwd)
        except Exception as exc:  # the re-run gave no answer; keep the last report
            checks = None
            recheck_error = str(exc) or type(exc).__name__
        self.app.call_from_thread(self._finish, result, checks, recheck_error)

    def _finish(
        self, result: FixResult, checks: list[DoctorCheck] | None, recheck_error: str | None
    ) -> None:
        self.busy = False
        status = Text(result.said(), style="green" if result.ok else "bold red")
        if recheck_error is not None:
            status.append(f"\n⚠ re-check failed, showing the previous report: {recheck_error}")
        self._set_status(status)
        if checks is not None:
            self.show(checks)
        else:
            self._redraw()  # re-enable the buttons against the old report
        self.post_message(FixApplied(result))
        if checks is not None:
            self.post_message(DoctorRefreshed(checks, self.cwd))
