"""The sidebar's second section and the Settings hub it opens.

The human: "I want there to just be 2 sections, 1 the fleet projects one, the other the
"others" type section … within that you can see for now just one option - Settings …
and below that green dot or red dot small with aisquare written next to it based on login
status and claude account headroom status"; "doctor and all is completely removed from
left pane but within settings, there would be links to various things"; and later:
"the line items below it of the red/green dots clicking on that should directly take me
to accounts page but still that line wont be highlighted/in focus, still settings would
be in focus".
"""

from __future__ import annotations

import asyncio
import itertools
import math
import threading
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.color import Color, rgb_to_lab
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widgets import Button, Static, TabbedContent

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import (
    GREEN,
    ORANGE_RED,
    RED,
    SETTINGS_KEY,
    TIMES,
    YELLOW,
    OthersSection,
    SettingsRow,
    StatusLine,
    UpdateRow,
    headroom_dot,
    status_line_text,
)
from aisquare.cli.ui.views.app_settings import (
    AGENT_TAB,
    PREFERENCES_TAB,
    REMOTE_TAB,
    AppSettingsView,
)
from aisquare.cli.ui.views.settings_captain import CaptainToggle
from aisquare.cli.ui.views.settings_hub import SettingsHubView
from aisquare.cli.ui.views.settings_remote import RemoteSection
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import (
    AccountsOverview,
    CheckStatus,
    ClaudeAccount,
    ClaudeUsage,
    DoctorCheck,
    FleetAgentStatus,
    HeadroomSummary,
    ProjectInfo,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from tests.test_claude_accounts import _sign_in
from tests.test_ui_accounts import _overview, _status
from tests.test_ui_shell import composited
from tests.ui_workers import settle_page, settle_until

T = TypeVar("T")
SIZE = (140, 40)
GOOD = HeadroomSummary(level="good", percent_left=80.0, usable=3)


def _drive(
    fn: Callable[[Pilot[None]], Awaitable[T]],
    *,
    headroom: Callable[[Sequence[ClaudeAccount] | None], HeadroomSummary] | None = None,
    refresh_seconds: float = 3600,
    doctor: Callable[[], list[DoctorCheck]] | None = None,
    accounts: Callable[[], AccountsOverview] | None = None,
) -> T:
    """``fn`` against a ``FleetApp`` with a scripted headroom, a stub doctor (no findings
    unless told) and no accounts reader unless ``accounts`` is one, once the doctor's run at
    start has been painted."""

    async def run() -> T:
        app = FleetApp(
            refresh_seconds=refresh_seconds,
            doctor=doctor or (lambda: []),
            accounts=accounts,
            headroom=headroom or (lambda accounts: GOOD),
            headroom_seconds=3600,
        )
        async with app.run_test(size=SIZE) as pilot:
            await settle_page(app)
            return await fn(pilot)

    return asyncio.run(run())


def _app(pilot: Pilot[None]) -> FleetApp:
    app = pilot.app
    assert isinstance(app, FleetApp)
    return app


def _shown(widget: Static) -> str:
    plain = getattr(widget.visual, "plain", None)
    assert isinstance(plain, str)
    return plain


# --- the sidebar ------------------------------------------------------------------------


def test_the_sidebar_has_the_projects_then_settings_and_its_status_and_nothing_else(
    captain_off: None,
) -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], list[str], str, bool, bool]:
        app = _app(pilot)
        sidebar = app.sidebar
        sections = [child.id or type(child).__name__ for child in sidebar.children]
        others = app.query_one(OthersSection)
        # Remote's chip (#33) is there too, and shows only while Remote is on: off, as here,
        # it takes no row.
        inside = [type(child).__name__ for child in others.children if child.display]
        order = [type(child).__name__ for child in others.children]
        return (
            sections,
            inside,
            order,
            _shown(app.query_one(SettingsRow)),
            app.query_one(StatusLine).can_focus,
            app.query_one(UpdateRow).display,
        )

    sections, inside, order, settings, focusable, offered = _drive(go)
    assert sections == ["fleet-header", "projects-notice", "projects", "others"]
    assert inside == ["SettingsRow", "StatusLine"]
    # The ⬆ row (tests/test_ui_update_row.py) sits between the two, and shows only while a
    # newer release is offered: never here, as the suite runs from a checkout, which the
    # release check never checks (services.updates.route_is_checked).
    assert order.index("SettingsRow") < order.index("UpdateRow") < order.index("StatusLine")
    assert offered is False
    assert settings == "⚙︎ Settings"  # ⚙ in its text (monotone) presentation
    assert not focusable


def test_settings_opens_the_hub_and_each_link_opens_its_page_under_settings() -> None:
    async def go(pilot: Pilot[None]) -> list[tuple[str | None, str | None]]:
        app = _app(pilot)
        seen: list[tuple[str | None, str | None]] = []
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        seen.append((app.content.current, app.sidebar.selected_key))
        for link in ("#hub-app-settings", "#hub-accounts", "#hub-doctor"):
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            await pilot.click(link)
            await settle_page(app)
            seen.append((app.content.current, app.sidebar.selected_key))
        assert isinstance(app.query_one("#app-settings"), AppSettingsView)
        return seen

    assert _drive(go) == [
        ("settings-hub", SETTINGS_KEY),
        ("app-settings", SETTINGS_KEY),
        ("accounts", SETTINGS_KEY),
        ("doctor", SETTINGS_KEY),
    ]


def test_the_hubs_doctor_link_runs_the_checks() -> None:
    runs: list[int] = []

    async def go(pilot: Pilot[None]) -> tuple[int, int]:
        app = _app(pilot)
        app._doctor = lambda: runs.append(1) or []  # type: ignore[func-returns-value]
        before = len(runs)
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        await pilot.click("#hub-doctor")
        await settle_page(app)
        return before, len(runs)

    before, after = _drive(go)
    assert after == before + 1


def test_the_status_line_opens_accounts_and_settings_stays_the_highlighted_row() -> None:
    async def go(pilot: Pilot[None]) -> tuple[str | None, str | None, bool, bool]:
        app = _app(pilot)
        await pilot.click(app.query_one(StatusLine))
        await settle_page(app)
        settings = app.query_one(SettingsRow)
        line = app.query_one(StatusLine)
        return (
            app.content.current,
            app.sidebar.selected_key,
            settings.has_class("selected"),
            app.focused is line or line.has_class("selected"),
        )

    current, selected, highlighted, line_lit = _drive(go)
    assert current == "accounts"
    assert selected == SETTINGS_KEY and highlighted
    assert not line_lit


def test_the_keyboard_reaches_settings_and_walks_the_hubs_links_but_never_the_status_line() -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], str | None, str | None, str | None]:
        app = _app(pilot)
        app.sidebar.focus()
        await pilot.pause()
        keys = [row.selection_key for row in app.sidebar._rows()]
        for _ in range(len(keys)):
            await pilot.press("down")  # to the last row: Settings
        await pilot.press("enter")
        await settle_page(app)
        first = app.focused.id if app.focused is not None else None
        await pilot.press("down")
        second = app.focused.id if app.focused is not None else None
        await pilot.press("enter")
        await settle_page(app)
        return keys, first, second, app.content.current

    keys, first, second, current = _drive(go)
    assert keys[-1] == SETTINGS_KEY and "" not in keys  # the status line is no row
    assert first == "hub-app-settings"  # opened from the keyboard: the first link has it
    assert second == "hub-remote"  # Remote control, then Captain, under App settings (#41)
    assert current == "app-settings"


def test_remote_control_and_captain_open_app_settings_on_their_own_tab_and_leave_the_hub() -> None:
    """The owner's placement (#41): Remote in its own tab, the Captain box atop Agent
    settings — and the hub still has one obvious way in to each, a link that lands there."""

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = _app(pilot)
        seen: dict[str, object] = {}
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        hub = app.query_one(SettingsHubView)
        seen["hub holds"] = (len(hub.query(RemoteSection)), len(hub.query(CaptainToggle)))
        for stem in ("remote", "captain"):
            # From the other tab each time, so the link is what brings its tab forward.
            if app.query("#app-settings"):
                app.query_one("#app-settings-tabs", TabbedContent).active = PREFERENCES_TAB
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            await pilot.click(f"#hub-{stem}")
            await settle_page(app)
            tabs = app.query_one("#app-settings-tabs", TabbedContent)
            seen[stem] = (app.content.current, app.sidebar.selected_key, tabs.active)
        view = app.query_one(AppSettingsView)
        seen["remote in"] = [node.id for node in view.query_one(RemoteSection).ancestors]
        seen["captain first"] = view.query_one("#agent-settings").children[0].id
        return seen

    seen = _drive(go)
    assert seen["hub holds"] == (0, 0)
    assert seen["remote"] == ("app-settings", SETTINGS_KEY, REMOTE_TAB)
    assert seen["captain"] == ("app-settings", SETTINGS_KEY, AGENT_TAB)
    assert isinstance(seen["remote in"], list) and REMOTE_TAB in seen["remote in"]
    assert seen["captain first"] == "settings-captain"


def test_the_hub_is_titled_settings_and_says_what_each_link_opens() -> None:
    async def go(pilot: Pilot[None]) -> tuple[str, list[str], list[str]]:
        app = _app(pilot)
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        hub = app.query_one(SettingsHubView)
        return (
            _shown(hub.query_one("#settings-title", Static)),
            [str(button.label) for button in hub.query(".hub-link").results(Button)],
            [_shown(about) for about in hub.query(".hub-about").results(Static)],
        )

    title, links, about = _drive(go)
    assert title.endswith("Settings")
    assert links == [
        "App settings",
        "Remote control",
        "Captain",
        "Accounts",
        "Doctor",
        "Restart all…",
        "Stop all…",
    ]
    assert len(about) == 6 and all(about)  # the Fleet section's two share one line


# --- what the doctor found (review of #261, m9) ----------------------------------------

FAILING = DoctorCheck(name="tmux", status=CheckStatus.fail, detail="not installed")
WARNING = DoctorCheck(name="snapshot", status=CheckStatus.warn, detail="packed 9 days ago")
PASSING = DoctorCheck(name="store", status=CheckStatus.ok, detail="v30")
NOT_THE_DOCTOR = "no-such-group"
"""A worker group nothing runs in: ``settle_page`` then waits for the messages alone, and not
for a doctor run the test is holding on purpose."""


class HeldDoctor:
    """The doctor at start answers ``first`` at once; a later run waits for :meth:`release`
    and then answers ``then`` — a run the test reads the page in the middle of."""

    def __init__(self, first: list[DoctorCheck], then: list[DoctorCheck]) -> None:
        self.first, self.then = first, then
        self.runs = 0
        self._go = threading.Event()

    def __call__(self) -> list[DoctorCheck]:
        self.runs += 1
        if self.runs == 1:
            return list(self.first)
        self._go.wait(timeout=10)
        return list(self.then)

    def release(self) -> None:
        self._go.set()


def _doctor_page(app: FleetApp) -> tuple[str, str, str]:
    """The Doctor page's header, report and status line, as they read."""
    return (
        _shown(app.query_one("#doctor-scope", Static)),
        _shown(app.query_one("#doctor-report", Static)),
        _shown(app.query_one("#doctor-status", Static)),
    )


async def _open_doctor(pilot: Pilot[None], *, group: str | None = None) -> None:
    app = _app(pilot)
    await pilot.click(app.query_one(SettingsRow))
    await settle_page(app)
    await pilot.click("#hub-doctor")
    await settle_page(app, group=group)


def test_what_the_doctor_found_at_start_is_beside_settings_and_on_the_hubs_doctor_link() -> None:
    """The run at start paints a hidden page: the counts beside *⚙ Settings*, muted, and on
    the hub's Doctor link are what say it found something before anyone opens it."""

    async def go(pilot: Pilot[None]) -> tuple[str, str, str]:
        app = _app(pilot)
        row = app.query_one(SettingsRow)
        badge_style = str(row.visual.spans[-1].style)  # type: ignore[attr-defined]
        await pilot.click(row)
        await settle_page(app)
        return _shown(row), badge_style, _shown(app.query_one("#hub-doctor-about", Static))

    row, style, about = _drive(go, doctor=lambda: [FAILING, WARNING, WARNING, PASSING])
    assert row == "⚙︎ Settings  ✗ 1  ⚠ 2"
    assert "dim" in style and "not bold" in style, "a muted badge beside the bold name"
    assert about == "the checks — they run when the page opens · the last run found ✗ 1  ⚠ 2"


def test_all_clear_puts_nothing_beside_settings() -> None:
    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = _app(pilot)
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        about = app.query_one("#hub-doctor-about", Static)
        return _shown(app.query_one(SettingsRow)), _shown(about)

    row, about = _drive(go, doctor=lambda: [PASSING])
    assert row == "⚙︎ Settings"
    assert about == "the checks — they run when the page opens"


def test_doctor_shows_the_report_from_start_while_it_runs_the_checks_again() -> None:
    """The human: the doctor "runs just once when the app starts and then again when someone
    opens it via setting". Opening it runs them again, and until they answer the page is
    the report from start, saying so — then the new one, and the badge follows it."""
    doctor = HeldDoctor(first=[WARNING], then=[PASSING])

    Page = tuple[str, str, str]

    async def go(pilot: Pilot[None]) -> tuple[Page, int, Page, str]:
        app = _app(pilot)
        await _open_doctor(pilot, group=NOT_THE_DOCTOR)
        # The page's own run has reached the doctor, which holds it: the page is mid-run.
        await settle_until(app, lambda: doctor.runs == 2, group=NOT_THE_DOCTOR)
        during = _doctor_page(app)
        runs = doctor.runs
        doctor.release()
        await settle_page(app)
        return during, runs, _doctor_page(app), _shown(app.query_one(SettingsRow))

    during, runs, after, row = _drive(go, doctor=doctor)
    header, report, status = during
    assert header == "checks for this machine"
    assert "⚠ snapshot: packed 9 days ago" in report, "the report from start, at once"
    assert status == "… running the checks again"
    assert runs == 2, "start, then the page's own run (still held)"
    header, report, status = after
    assert "✓ store: v30" in report and "snapshot" not in report
    assert status == "", "answered: the status line is clear again"
    assert row == "⚙︎ Settings", "the badge follows the newest report"


def test_the_doctor_page_names_the_project_its_checks_are_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings → Doctor runs for the project most recently selected, and with *⚙ Settings*
    highlighted nothing on screen said which: the header names it, by its nickname."""
    root = tmp_path / "alpha"
    root.mkdir()
    with store_session() as store:
        store.onboard_project(ProjectInfo(id="prj_a", root=root))
        store.set_project_nickname("prj_a", "Payments [v2]")

    def no_agents(project: ProjectInfo, *, live_only: bool = True) -> list[FleetAgentStatus]:
        return []

    monkeypatch.setattr(fleet_service, "list_agents", no_agents)
    doctor = HeldDoctor(first=[WARNING], then=[PASSING])

    async def go(pilot: Pilot[None]) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
        app = _app(pilot)
        app.doctor_scope = "prj_a"  # what selecting the project does: it runs nothing
        await _open_doctor(pilot, group=NOT_THE_DOCTOR)
        during = _doctor_page(app)
        doctor.release()
        await settle_page(app)
        return during, _doctor_page(app)

    during, after = _drive(go, doctor=doctor)
    assert during[0] == "checks for this machine", (
        "the report on screen is still the one from start"
    )
    assert during[2] == "… running the checks for Payments [v2]"
    assert after[0] == "checks for Payments [v2]" and after[2] == ""


# --- the status line --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "dot", "colour"),
    [
        (HeadroomSummary(level="good", percent_left=80.0, usable=3), "●", GREEN),
        (HeadroomSummary(level="fair", percent_left=40.0, usable=2), "●", YELLOW),
        (HeadroomSummary(level="low", percent_left=10.0, usable=2), "●", ORANGE_RED),
        (HeadroomSummary(level="critical", percent_left=2.0, usable=1), "●", RED),
        (HeadroomSummary(level="empty", percent_left=0.0, usable=2), "💀", ""),
        # Offline, a locked Keychain, no usable account: nothing READ — grey, not a skull.
        (HeadroomSummary(level="empty"), "●", "grey50"),
        (HeadroomSummary(level="unknown"), "●", "grey50"),
        (None, "●", "grey50"),
        # Nothing usable and an account needs a login — red only when told EVERY enabled
        # one does (``every_needs_login``, the shell's count); alone it is grey (N2).
        (HeadroomSummary(level="empty", needs_login=2), "●", "grey50"),
    ],
    ids=[
        "good",
        "fair",
        "low",
        "critical",
        "empty",
        "unread",
        "unknown",
        "before-the-first",
        "a-login-due-but-not-every",
    ],
)
def test_the_claude_dot_follows_the_headroom_level(
    summary: HeadroomSummary | None, dot: str, colour: str
) -> None:
    assert headroom_dot(summary) == (dot, colour)
    text = status_line_text(True, summary)
    assert text.plain.split("  ")[1].startswith(dot)
    if summary is not None:
        assert f"{summary.usable}{TIMES} claude" in text.plain


@pytest.mark.parametrize(("aisquare", "colour"), [(True, GREEN), (False, RED), (None, "grey50")])
def test_the_aisquare_dot_is_the_sign_in(aisquare: bool | None, colour: str) -> None:
    text = status_line_text(aisquare, GOOD)
    assert text.plain.startswith("● aisquare")
    first = text.spans[0]
    assert text.plain[first.start : first.end] == "● " and str(first.style) == colour


def test_a_login_due_puts_a_warning_on_the_line() -> None:
    due = HeadroomSummary(level="fair", percent_left=30.0, usable=1, needs_login=1)
    assert status_line_text(True, due).plain == f"● aisquare  ● 1{TIMES} claude ⚠"
    assert "⚠" not in status_line_text(True, GOOD).plain


def test_the_line_paints_the_injected_headroom() -> None:
    critical = HeadroomSummary(level="critical", percent_left=3.0, usable=2, needs_login=1)

    async def go(pilot: Pilot[None]) -> str:
        return _shown(_app(pilot).query_one(StatusLine))

    assert _drive(go, headroom=lambda accounts: critical) == f"● aisquare  ● 2{TIMES} claude ⚠"


def _delta_e(one: Color, other: Color) -> float:
    """CIE76: the distance in Lab, where about 2.3 is the least an eye tells apart."""
    a, b = rgb_to_lab(one), rgb_to_lab(other)
    return math.dist((a.L, a.a, a.b), (b.L, b.a, b.b))


def test_each_band_paints_a_dot_its_neighbours_cannot_be_taken_for() -> None:
    """The colour the eye gets — the composited strip, the theme applied — band by band, in
    a dark theme and a light one. ``yellow`` was the theme's to paint: textual-dark's is
    #fd971f, an orange beside the 5-25 % band's #ff8700, so 40 % left read as 10 % left
    (validator of PR #261, MAJOR 2: ΔE 10; textual-light's pair was ΔE 28). Each band is
    ΔE 35 or more from the next, and 0 % is the skull."""
    bands = {75.0: "good", 40.0: "fair", 15.0: "low", 3.0: "critical"}

    async def go(pilot: Pilot[None]) -> tuple[dict[str, list[Color]], list[str]]:
        app = _app(pilot)
        line = app.query_one(StatusLine)
        seen: dict[str, list[Color]] = {}
        skulls: list[str] = []
        for theme in ("textual-dark", "textual-light"):
            app.theme = theme
            for left, level in bands.items():
                app.sidebar.show_headroom(HeadroomSummary(level=level, percent_left=left, usable=2))
                await pilot.pause()
                (strip,) = line.render_lines(Region(0, 0, line.size.width, 1))
                _aisquare, claude = [segment for segment in strip if "●" in segment.text]
                assert claude.style is not None and claude.style.color is not None
                seen.setdefault(theme, []).append(Color.from_rich_color(claude.style.color))
            app.sidebar.show_headroom(HeadroomSummary(level="empty", percent_left=0.0, usable=2))
            await pilot.pause()
            skulls.append(composited(line))
        return seen, skulls

    seen, skulls = _drive(go)
    for theme, colours in seen.items():
        gaps = [round(_delta_e(one, other)) for one, other in itertools.pairwise(colours)]
        assert min(gaps) >= 35, (theme, [colour.hex for colour in colours], gaps)
    assert all("💀" in skull for skull in skulls), skulls


def test_the_headroom_is_read_at_start_and_never_on_the_refresh_tick() -> None:
    """The usage cache answers it, at most one network read per account per five minutes —
    and the two-second tick never asks: ticks come and go, each one reading the accounts and
    painting their frame (``_accounts_read``, the 2 s path), and the reader is not called.

    The ticks are the tick's own call, each settled until its frame is painted — not a fast
    timer, whose tick could land as the app is torn down, with the sidebar already gone."""
    calls: list[datetime] = []
    frames: list[datetime] = []

    def reader(accounts: Sequence[ClaudeAccount] | None) -> HeadroomSummary:
        calls.append(datetime.now(UTC))
        return GOOD

    def overview() -> AccountsOverview:
        frames.append(datetime.now(UTC))
        return _overview(_status(1, "me@example.com"), _status(2, "two@example.com"))

    async def go(pilot: Pilot[None]) -> tuple[int, int, int]:
        app = _app(pilot)
        at_start = len(calls)
        for _ in range(5):
            app.refresh_data()  # what the tick runs
            await settle_page(app)
        return at_start, len(calls), len(frames)

    at_start, after, painted = _drive(go, headroom=reader, accounts=overview)
    assert painted >= 6, "the premise: the accounts were read and painted tick after tick"
    assert at_start == 1  # once the first frame landed
    assert after == 1


def test_a_read_asked_for_while_one_runs_is_made_once_after_it() -> None:
    """``refresh_headroom`` dropped a request made while a read was in flight, so a login
    landing mid-read was not counted until the next minute (review of #261, m3)."""
    started: list[int] = []
    release = threading.Event()

    def reader(accounts: Sequence[ClaudeAccount] | None) -> HeadroomSummary:
        started.append(len(started) + 1)
        if len(started) == 2:
            assert release.wait(10)  # the second read is held, as a slow cache read is
        return GOOD

    async def go(pilot: Pilot[None]) -> list[int]:
        app = _app(pilot)
        app.refresh_headroom()  # the second read: held
        await asyncio.sleep(0.1)
        for _ in range(3):
            app.refresh_headroom()  # asked three times meanwhile
        release.set()
        await settle_page(app)
        return list(started)

    assert _drive(go, headroom=reader) == [1, 2, 3], "one read owed, made once, after it"


@pytest.mark.parametrize("slot_1", ["never-signed-in", "token-gone"])
def test_a_signed_out_account_puts_a_warning_on_the_line(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, slot_1: str
) -> None:
    """The real readers, with the accounts on disk: a slot whose login is gone gets *Log in*
    on the Accounts page, and the line's ⚠ — it never reached the summary, which reads only
    signed-in slots (review of #261, m2). A disabled one counts for nothing."""
    calls: list[int] = []

    def usage(account: ClaudeAccount, **kwargs: Any) -> ClaudeUsage:
        calls.append(account.slot)
        return ClaudeUsage(available=True, session_percent=40.0, week_percent=10.0)

    monkeypatch.setattr(accounts_service, "usage", usage)
    good = core.create_account()  # slot 2
    _sign_in(good, "two@example.com")
    gone = core.create_account()  # slot 3: signed out, and disabled
    accounts_service.set_disabled(str(gone.slot), True)
    if slot_1 == "token-gone":
        _sign_in(core.default_account(), "me@example.com", credentials=False)

    async def go(pilot: Pilot[None]) -> tuple[str, HeadroomSummary | None]:
        app = _app(pilot)
        await settle_until(app, lambda: app.sidebar.headroom is not None)
        return _shown(app.query_one(StatusLine)), app.sidebar.headroom

    line, headroom = _drive(
        go, headroom=accounts_service.read_fleet_headroom, accounts=accounts_service.overview
    )
    assert line.endswith(f"● 1{TIMES} claude ⚠"), line  # slot 1 signed out, slot 2 good
    assert headroom is not None and headroom.needs_login == 1 and headroom.usable == 1
    assert calls == [good.slot], "only the signed-in slot was asked"


def test_every_account_signed_out_is_a_red_dot_and_a_warning_not_grey(fake_home: Path) -> None:
    async def go(pilot: Pilot[None]) -> tuple[str, tuple[str, str]]:
        app = _app(pilot)
        await settle_until(app, lambda: app.sidebar.headroom is not None)
        sidebar = app.sidebar
        dot = headroom_dot(sidebar.headroom, every_needs_login=sidebar.every_needs_login)
        return _shown(app.query_one(StatusLine)), dot

    line, dot = _drive(
        go, headroom=accounts_service.read_fleet_headroom, accounts=accounts_service.overview
    )
    assert line.endswith(f"● 0{TIMES} claude ⚠"), line
    assert dot == ("●", RED)  # every enabled account needs a login


def test_a_page_change_to_the_accounts_reads_the_headroom_again() -> None:
    """``AccountsChanged`` (a slot added, removed or signed in) re-reads the dot at once."""
    from aisquare.cli.ui.views.accounts import AccountsChanged

    calls: list[Any] = []

    async def go(pilot: Pilot[None]) -> int:
        app = _app(pilot)
        before = len(calls)
        app.post_message(AccountsChanged())
        await settle_page(app)
        return len(calls) - before

    assert _drive(go, headroom=lambda accounts: calls.append(accounts) or GOOD) == 1  # type: ignore[func-returns-value]


def test_red_only_when_every_enabled_account_needs_a_login() -> None:
    nothing_usable = HeadroomSummary(level="empty", needs_login=1)
    assert headroom_dot(nothing_usable, every_needs_login=True) == ("●", RED)
    assert headroom_dot(nothing_usable) == ("●", "grey50")
    usable = HeadroomSummary(level="good", percent_left=80.0, usable=1, needs_login=1)
    assert headroom_dot(usable, every_needs_login=True) == ("●", GREEN), "one is usable"


def test_an_unreachable_main_account_beside_a_signed_out_spare_is_grey_with_a_warning(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's repro (review of #261, N2): slot 1 signed in but the endpoint
    unreachable, slot 2 a spare never signed in. Nothing is usable because nothing could
    be READ — grey, with the ⚠ for the spare; red was "for want of a login"."""

    def usage(account: ClaudeAccount, **kwargs: Any) -> ClaudeUsage:
        return ClaudeUsage(available=False, reason="could not reach the usage endpoint")

    monkeypatch.setattr(accounts_service, "usage", usage)
    _sign_in(core.default_account(), "me@example.com")
    core.create_account()  # slot 2, never signed in

    async def go(pilot: Pilot[None]) -> tuple[str, tuple[str, str]]:
        app = _app(pilot)
        await settle_until(app, lambda: app.sidebar.headroom is not None)
        sidebar = app.sidebar
        dot = headroom_dot(sidebar.headroom, every_needs_login=sidebar.every_needs_login)
        return _shown(app.query_one(StatusLine)), dot

    line, dot = _drive(
        go, headroom=accounts_service.read_fleet_headroom, accounts=accounts_service.overview
    )
    assert line.endswith(f"● 0{TIMES} claude ⚠"), line
    assert dot == ("●", "grey50")


def test_a_sign_out_while_an_accounts_read_is_in_flight_reaches_the_warning_at_once() -> None:
    """The reviewer's repro (review of #261, N1): ``AccountsChanged`` marks the headroom due
    and asks for a frame, but the tick's read already in flight carries the frame from
    BEFORE the change. The due read was spent on that frame, and the owed read that held
    the sign-out was painted with no headroom read after it: the ⚠ waited a minute."""
    from aisquare.cli.ui.views.accounts import AccountsChanged

    state = {"signed_out": False}
    hold, entered, release = threading.Event(), threading.Event(), threading.Event()

    def overview() -> AccountsOverview:
        signed_out = state["signed_out"]  # what the files say as the read begins
        if hold.is_set():
            hold.clear()
            entered.set()
            assert release.wait(10)  # then held, as a writer holds the store
        return _overview(
            _status(1, "me@example.com"),
            _status(2, "two@example.com", signed_in=not signed_out),
        )

    async def go(pilot: Pilot[None]) -> tuple[int, int]:
        app = _app(pilot)
        await settle_until(app, lambda: app.sidebar.headroom is not None)
        assert app.sidebar.headroom is not None
        before = app.sidebar.headroom.needs_login
        hold.set()
        app.refresh_data()  # the two-second tick: its read takes the files, then waits
        assert await asyncio.to_thread(entered.wait, 5), "the tick's read began"
        state["signed_out"] = True  # a sign-out lands meanwhile…
        app.post_message(AccountsChanged())  # …and the page says so
        await pilot.pause()
        release.set()
        await settle_until(
            app,
            lambda: app.sidebar.headroom is not None and app.sidebar.headroom.needs_login == 1,
            timeout=5,
        )
        assert app.sidebar.headroom is not None
        return before, app.sidebar.headroom.needs_login

    before, after = _drive(go, headroom=lambda accounts: GOOD, accounts=overview)
    assert before == 0, "the premise: both signed in at first"
    assert after == 1, "the ⚠ follows the sign-out, not a minute later"
