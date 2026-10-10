"""The Doctor view: one project's checks with their fixes, and a button per fix we can run.

docs/plans/fleet-tui.md §0 item 4 and §4.2: the findings are visible with the
fix for each, and where the fix is one of our own commands it is one click
(``services.onboarding.fix_commands`` decides which — everything else stays
text). A click runs ``apply_fix`` in a thread worker with the project's cwd,
then re-runs the doctor and posts :class:`DoctorRefreshed` so the shell and the
sidebar's Doctor section can follow. The UI process never changes directory.

asq's own Doctor (``machine=True``, never the Onboard or Project copies) also
carries Update and Uninstall. Both hand the terminal over rather than run in
here: the view posts :class:`DoctorView.HandOff`, the app quits, and ``run_ui``
hands this terminal to ``aisquare upgrade --reopen`` or ``aisquare uninstall``,
which show their plan and ask y/N themselves. An upgrade must not run under the
UI it replaces, and asq comes back after it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Static

from aisquare.core import selfcli
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import install_route, onboarding
from aisquare.services.onboarding import FixCommand, FixResult, Runner

_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}
_STYLE = {CheckStatus.ok: "green", CheckStatus.warn: "yellow", CheckStatus.fail: "bold red"}
FIX_WORKER = "doctor-fix"
"""The worker a one-click fix runs in (and the doctor after it): it writes."""

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


def render_checks(checks: list[DoctorCheck]) -> Text:
    """The `aisquare doctor` output as rich text (fixes indented under their check)."""
    text = Text()
    for check in checks:
        text.append(f"{_SYMBOL[check.status]} {check.name}: ", style=_STYLE[check.status])
        text.append(f"{check.detail}\n")
        if check.fix and check.status is not CheckStatus.ok:
            text.append(f"    → {check.fix}\n", style="dim")
    if not checks:
        text.append("(no checks yet)", style="dim")
    return text


def _doctor_via_cli(run: Runner) -> Refresh:
    def refresh(cwd: Path | None) -> list[DoctorCheck]:
        checks, error = onboarding.run_doctor(cwd, run=run)
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
        command = install_route.command_line(install_route.upgrade_argv(route, None))
    except Exception:  # the Doctor view must keep its buttons whatever this finds
        return None
    return f"aisquare does not upgrade this install itself ({reason}). Upgrade it with: {command}"


class DoctorView(VerticalScroll):
    """Scrollable doctor report for the selected project, with one-click fixes.

    ``cwd`` is the project root the fixes run in; the shell sets it through
    ``show(checks, cwd=…)`` (or the constructor). Without one, machine-level
    fixes (``agents connect``, ``doctor --fix``, ``explainability enable``) still
    run — they do not care where — and project-level ones (``project onboard``)
    render disabled rather than silently onboarding whatever directory the UI
    happens to have been started from.
    """

    DEFAULT_CSS = """
    DoctorView { padding: 1 2; }
    DoctorView #doctor-status { height: auto; margin-top: 1; }
    DoctorView #doctor-fixes { height: auto; }
    DoctorView #doctor-fixes Button { margin: 1 1 0 0; }
    DoctorView #doctor-machine { height: auto; margin-top: 1; }
    DoctorView #doctor-machine Button { margin: 0 1 0 0; }
    """

    class HandOff(Message):
        """Update or Uninstall: quit asq and run ``aisquare <args>`` in this terminal."""

        def __init__(self, args: tuple[str, ...]) -> None:
            self.args = args
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
    ) -> None:
        super().__init__(id=id, classes=classes)
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

    def compose(self) -> ComposeResult:
        yield Static("", id="doctor-report")
        yield Static("", id="doctor-status")
        yield Vertical(id="doctor-fixes")
        if self.machine:
            refusal = (self._update_refusal or _update_refusal)()
            with Horizontal(id="doctor-machine"):
                update = Button("Update aisquare", id="doctor-update")
                update.tooltip = Text(
                    refusal
                    or "Quits asq and runs `aisquare upgrade` here: it shows what it will do "
                    "and asks first. asq opens again when it is done."
                )
                update.disabled = refusal is not None
                yield update
                uninstall = Button("Uninstall aisquare…", id="doctor-uninstall", variant="error")
                uninstall.tooltip = (
                    "Quits asq and runs `aisquare uninstall` here: it lists what goes and "
                    "what stays, and asks first. asq opens again if nothing is removed."
                )
                yield uninstall
            if refusal is not None:  # said on screen, not only on hover
                yield Static(Text(f"Update: {refusal}", style="dim"), id="doctor-update-note")

    def on_mount(self) -> None:
        self._redraw()

    # ---------------------------------------------------------------- the report

    def show(self, checks: Iterable[DoctorCheck], *, cwd: Path | None = None) -> None:
        """Replace the report (and, when given, the cwd the fixes run in).

        ``cwd=None`` means "keep the one you have": most callers show a new
        report for the same project. A caller that has to go BACK to no project
        — the shell, when the report becomes the machine-wide one — sets
        :attr:`cwd` itself before calling this, or a project fix button would
        stay armed on a root the user has left.
        """
        if cwd is not None:
            self.cwd = cwd
        self.checks = list(checks)
        if self.is_mounted:
            self._redraw()

    def _redraw(self) -> None:
        self.query_one("#doctor-report", Static).update(render_checks(self.checks))
        self.call_later(self._rebuild_fixes)

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
            # ``Text``, never the str: a Button parses a str label as markup, and a
            # ``--config-dir /home/me/[archive]/.claude`` would lose its bracketed
            # segment on screen and read as a different directory (measured:
            # ``Content.from_text`` turns ``/home/me/[archive]/repo`` into
            # ``/home/me//repo``). Same rule for the tooltip; it carries the hint.
            button = Button(Text(fix.label), variant="warning", id=button_id)
            tooltip = Text(fix.source)
            if fix.scope == "project" and self.cwd is None:
                button.disabled = True
                tooltip.append("\n(select a project first — this runs in its root)")
            button.tooltip = tooltip
            buttons.append(button)
        self.fixes = fixes
        if buttons:
            await container.mount(*buttons)

    def _set_status(self, text: Text) -> None:
        self.query_one("#doctor-status", Static).update(text)

    # ---------------------------------------------------------------- the click

    def on_button_pressed(self, event: Button.Pressed) -> None:
        hand_off = HAND_OFFS.get(event.button.id or "") if self.machine else None
        if hand_off is not None:
            # The app decides: it refuses while ANY Doctor view's fix is still writing.
            event.stop()
            self.post_message(self.HandOff(hand_off))
            return
        fix = self.fixes.get(event.button.id or "")
        if fix is None:
            return
        event.stop()
        if self.busy:
            return
        self.busy = True
        for button in self.query("#doctor-fixes Button").results(Button):
            button.disabled = True
        self._set_status(Text(f"… running {fix.label}", style="dim"))
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
        status = Text(result.summary(), style="green" if result.ok else "bold red")
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
