"""Usage sharing in the TUI: the switch on the Usage & stats page, the one-time question, and
what is shown of what would be sent.

The widgets reach the service through ``Sharing``, here a fake that keeps its own state, so
nothing is written and nothing sent; the shell's question and its shipper tick are driven through
the whole fleet UI. The service itself is ``tests/test_usage_share.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Button, Static

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import SettingsRow
from aisquare.cli.ui.views.usage_sharing import (
    ConsentDialog,
    PreviewDialog,
    Sharing,
    SharingBox,
    status_text,
)
from aisquare.cli.ui.views.usage_stats import UsageStatsView
from aisquare.core import sharing as sharing_core
from aisquare.models import SharingStatus, ShipReport
from aisquare.services import usage_share
from tests import guard_clock
from tests.projects import add_project
from tests.test_ui_settings_hub import GOOD
from tests.ui_workers import released, settle_page, settle_until

T = TypeVar("T")
SENT = datetime(2026, 10, 26, 6, 30, tzinfo=UTC)


class FakeSharing:
    """The sharing service, in memory: it keeps the state the buttons change."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        forced_off: str | None = None,
        signed_in: bool = False,
        due: bool = True,
        ship_report: ShipReport | None = None,
    ) -> None:
        self.status_now = SharingStatus(
            enabled=enabled and forced_off is None,
            switch=enabled,
            forced_off=forced_off,
            signed_in=signed_in,
            last_sent_at=SENT if enabled else None,
            linked=signed_in if enabled else None,
        )
        self.due = due
        self.sample = not enabled
        self.ship_report = ship_report or ShipReport(sent=True, days=3, linked=signed_in)
        self.calls: list[str] = []

    def status(self) -> SharingStatus:
        return self.status_now

    def enable(self) -> SharingStatus:
        self.calls.append("enable")
        self.status_now = self.status_now.model_copy(
            update={
                "enabled": True,
                "switch": True,
                "last_sent_at": SENT,
                "linked": self.status_now.signed_in,
            }
        )
        return self.status_now

    def disable(self) -> ShipReport:
        self.calls.append("disable")
        self.status_now = self.status_now.model_copy(
            update={"enabled": False, "switch": False, "last_sent_at": None}
        )
        return ShipReport(sent=True, opt_out=True)

    def ship(self) -> ShipReport:
        self.calls.append("ship")
        return self.ship_report

    def ship_due(self) -> ShipReport:
        self.calls.append("ship_due")
        return ShipReport(skipped="nothing to send")

    def preview(self) -> str:
        self.calls.append("preview")
        return json.dumps({"schema_version": 1, "days": []}, indent=2)

    def is_sample(self) -> bool:
        return self.sample

    def consent_due(self) -> bool:
        self.calls.append("consent_due")
        return self.due

    def answer(self, yes: bool) -> ShipReport | None:
        self.calls.append("share" if yes else "declined")
        self.due = False
        if yes:
            self.status_now = self.status_now.model_copy(update={"enabled": True, "switch": True})
            return self.ship_report
        return None

    def seam(self) -> Sharing:
        return Sharing(
            status=self.status,
            enable=self.enable,
            disable=self.disable,
            ship=self.ship,
            ship_due=self.ship_due,
            preview=self.preview,
            is_sample=self.is_sample,
            consent_due=self.consent_due,
            answer=self.answer,
        )


class Host(App[None]):
    def __init__(self, fake: FakeSharing) -> None:
        super().__init__()
        self.fake = fake

    def compose(self) -> ComposeResult:
        yield UsageStatsView(
            loader=lambda range_, sweep, progress: _empty(range_),
            sharing=self.fake.seam(),
            id="usage",
        )


def _empty(range_: Any) -> Any:
    from aisquare.models import UsageStats

    return UsageStats(range=range_, until="2026-10-26")


def _drive_page(fake: FakeSharing, fn: Callable[[Pilot[None], SharingBox], Awaitable[T]]) -> T:
    async def run() -> T:
        app = Host(fake)
        async with app.run_test(size=(120, 50)) as pilot:
            await settle_page(app)
            return await fn(pilot, app.query_one(SharingBox))

    return asyncio.run(run())


def _text(widget: Static) -> str:
    plain = getattr(widget.visual, "plain", None)
    assert isinstance(plain, str)
    return plain


def _line(box: SharingBox) -> str:
    return _text(box.query_one("#usage-share-status", Static))


def _label(box: SharingBox) -> str:
    return str(box.query_one("#usage-share-toggle", Button).label)


# --- the switch ----------


def test_the_switch_starts_off_and_says_nothing_leaves_the_machine() -> None:
    async def go(pilot: Pilot[None], box: SharingBox) -> tuple[str, str, bool, str]:
        toggle = box.query_one("#usage-share-toggle", Button)
        return (
            _label(box),
            _line(box),
            toggle.disabled,
            _text(box.query_one("#usage-share-about", Static)),
        )

    label, line, disabled, about = _drive_page(FakeSharing(), go)

    assert (label, line, disabled) == (
        "Turn sharing on",
        "Off — nothing leaves this machine.",
        False,
    )
    assert (
        "Never names, paths, remotes, prompts or arguments" in about and "Off by default" in about
    )
    # re-review of #444 (m2): whoever opts in here, not only in the dialog, is told
    assert "every repo you run Claude Code in, including ones you haven't added" in about


def test_one_click_turns_it_on_and_sends_the_first_shipment_and_one_click_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = guard_clock.install(monkeypatch)
    fake = FakeSharing(signed_in=True)

    async def go(pilot: Pilot[None], box: SharingBox) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        clock.step()
        await pilot.click("#usage-share-toggle")
        await settle_page(box.app)
        seen.append((_label(box), _line(box)))
        clock.step()
        await released(pilot, box.query_one("#usage-share-toggle", Button))
        await pilot.click("#usage-share-toggle")
        await settle_page(box.app)
        seen.append((_label(box), _line(box)))
        return seen

    seen = _drive_page(fake, go)

    assert fake.calls == ["enable", "ship", "disable"]  # one click each way; nothing else
    assert seen[0][0] == "Turn sharing off" and seen[0][1].startswith("On — last sent")
    assert "linked to your AISquare account" in seen[0][1]
    assert seen[1] == ("Turn sharing on", "Off — nothing leaves this machine.")


def test_a_click_that_lands_as_the_page_appears_is_not_how_someone_agrees_to_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_clock.install(monkeypatch)  # the clock does not move: the page has just appeared
    fake = FakeSharing()

    async def go(pilot: Pilot[None], box: SharingBox) -> str:
        await pilot.click("#usage-share-toggle")
        await settle_page(box.app)
        return _label(box)

    assert _drive_page(fake, go) == "Turn sharing on" and fake.calls == []


def test_the_keyboard_is_a_deliberate_gesture_and_flips_it() -> None:
    fake = FakeSharing()

    async def go(pilot: Pilot[None], box: SharingBox) -> str:
        box.query_one("#usage-share-toggle", Button).focus()
        await pilot.press("enter")
        await settle_page(box.app)
        return _label(box)

    assert _drive_page(fake, go) == "Turn sharing off" and fake.calls[:1] == ["enable"]


@pytest.mark.parametrize(
    "why", ["DO_NOT_TRACK is set", "AISQUARE_NO_TELEMETRY is set", "CI is set"]
)
def test_a_forced_off_switch_cannot_be_pressed_and_says_why(why: str) -> None:
    fake = FakeSharing(enabled=True, forced_off=why)

    async def go(pilot: Pilot[None], box: SharingBox) -> tuple[bool, str, str]:
        await pilot.click("#usage-share-toggle")
        await settle_page(box.app)
        toggle = box.query_one("#usage-share-toggle", Button)
        return toggle.disabled, _label(box), _line(box)

    disabled, label, line = _drive_page(fake, go)

    assert disabled is True and fake.calls == []
    assert (
        label == "Turn sharing on" and line == f"Off — forced: {why}. Nothing leaves this machine."
    )


def test_the_line_says_whether_the_server_linked_the_shipments_to_the_account() -> None:
    base = SharingStatus(enabled=True, switch=True, last_sent_at=SENT)
    anonymous = status_text(base)
    linked = status_text(base.model_copy(update={"signed_in": True, "linked": True}))
    pending = status_text(base.model_copy(update={"signed_in": True, "linked": False}))
    never = status_text(base.model_copy(update={"last_sent_at": None}))
    failed = status_text(base.model_copy(update={"last_error": "HTTP 503", "failures": 2}))

    assert "anonymous (sign in to AISquare to link it" in anonymous
    assert "last sent 2026-10-26" in anonymous or "last sent 2026-10-25" in anonymous
    assert "· linked to your AISquare account" in linked
    assert "the next shipment is linked to your AISquare account" in pending
    assert never.startswith("On — nothing sent yet")
    assert "fail" not in failed.lower() and "503" not in failed and "retry" not in failed.lower()


def test_a_first_shipment_that_fails_is_not_said_and_the_switch_stays_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing fails silently, whatever the server's state (the owner's decision): the page
    shows the consent state and the last shipment that WENT, never "failed" or "retrying"."""
    clock = guard_clock.install(monkeypatch)
    fake = FakeSharing(ship_report=ShipReport(error="HTTP 404 — Could not reach the server"))

    async def go(pilot: Pilot[None], box: SharingBox) -> tuple[str, str, list[str]]:
        clock.step()
        await pilot.click("#usage-share-toggle")
        await settle_page(box.app)
        toasts = [n.message for n in box.app._notifications]
        return _label(box), _line(box), toasts

    label, line, toasts = _drive_page(fake, go)

    assert label == "Turn sharing off"
    assert line.startswith("On — ")
    for word in ("fail", "404", "retry", "error", "reach", "wait"):
        assert word not in line.lower(), word
    assert toasts == []


def test_the_state_is_read_again_when_the_page_is_shown_again() -> None:
    fake = FakeSharing()

    async def go(pilot: Pilot[None], box: SharingBox) -> tuple[str, str]:
        before = _label(box)
        fake.status_now = fake.status_now.model_copy(update={"enabled": True, "switch": True})
        box.refresh_status()  # as the page's `on_show` does after the shell's question
        await settle_page(box.app)
        return before, _label(box)

    assert _drive_page(fake, go) == ("Turn sharing on", "Turn sharing off")


# --- what is shared ----------


def test_whats_shared_shows_the_exact_body_and_sends_nothing() -> None:
    fake = FakeSharing()

    async def go(pilot: Pilot[None], box: SharingBox) -> tuple[str | None, bool, int]:
        await pilot.click("#usage-share-preview")
        await settle_page(box.app)
        screen = box.app.screen
        assert isinstance(screen, PreviewDialog)
        shown = _text(screen.query_one("#preview-body", Static))
        body = screen.body
        await pilot.press("escape")
        await settle_page(box.app)
        return body, shown == body, len(box.app.screen_stack)

    body, shown, depth = _drive_page(fake, go)

    assert body is not None and json.loads(body) == {"schema_version": 1, "days": []} and shown
    assert depth == 1  # Escape closed it
    assert fake.calls == ["preview"]  # looking sends nothing and turns nothing on


def test_the_preview_is_captioned_a_sample_while_sharing_is_off_and_exact_when_on() -> None:
    async def about(pilot: Pilot[None], box: SharingBox) -> str:
        await pilot.click("#usage-share-preview")
        await settle_page(box.app)
        screen = box.app.screen
        assert isinstance(screen, PreviewDialog)
        return _text(screen.query_one("#preview-about", Static))

    off = _drive_page(FakeSharing(), about)
    on = _drive_page(FakeSharing(enabled=True), about)

    assert "SAMPLE" in off and "placeholder ids" in off and "only days from then on" in off
    assert "SAMPLE" not in on and on.startswith("Exactly what the next shipment would send")


def test_the_consent_text_says_only_days_from_yes_on_are_sent() -> None:
    from aisquare.cli.ui.views.usage_sharing import CONSENT_TEXT

    assert "Only days from the moment you say yes are sent" in CONSENT_TEXT
    assert "every repo you run Claude Code in, including ones you haven't added" in CONSENT_TEXT
    assert "exactly what's sent" not in CONSENT_TEXT.lower()


def test_the_preview_is_built_off_the_ui_thread_and_a_failure_is_said() -> None:
    fake = FakeSharing()

    def broken() -> str:
        raise RuntimeError("the store is locked")

    seam = fake.seam()
    seam.preview = broken

    async def run() -> str:
        class Page(App[None]):
            def compose(self) -> ComposeResult:
                yield Static("page")

        app = Page()
        async with app.run_test(size=(100, 40)):
            await app.push_screen(PreviewDialog(seam))
            await settle_page(app)
            return _text(app.screen.query_one("#preview-body", Static))

    assert asyncio.run(run()) == "could not build it: the store is locked"


# --- the question ----------


def _fleet(fake: FakeSharing) -> FleetApp:
    return FleetApp(
        refresh_seconds=3600,
        doctor=lambda: [],
        accounts=None,
        headroom=lambda accounts: GOOD,
        headroom_seconds=3600,
        sharing=fake.seam(),
    )


async def _open_settings(app: FleetApp, pilot: Pilot[None]) -> None:
    await pilot.click(app.query_one(SettingsRow))  # leaves the welcome page: onboarded, in use
    await settle_page(app)
    await settle_until(
        app, lambda: isinstance(app.screen, ConsentDialog) or len(app.screen_stack) == 1
    )


def test_the_question_is_asked_once_after_onboarding_and_no_thanks_is_remembered(
    tmp_path: Path, sharing_allowed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_project(tmp_path / "work")
    (tmp_path / "work").mkdir(exist_ok=True)
    fake = FakeSharing()

    async def go() -> tuple[list[str], str, int, list[str]]:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)) as pilot:
            await settle_page(app)
            asked_at_start = [type(app.screen).__name__]  # the welcome page: not asked yet
            await _open_settings(app, pilot)
            during = type(app.screen).__name__
            default_focus = getattr(app.screen.focused, "id", None)
            await pilot.click("#consent-no")
            await settle_page(app)
            after = len(app.screen_stack)
            await pilot.click(app.query_one(SettingsRow))  # another page: not asked again
            await settle_page(app)
            asked_again = [type(app.screen).__name__]
            return (
                [*asked_at_start, during, str(default_focus)],
                str(after),
                len(app.screen_stack),
                asked_again,
            )

    first, after, depth, again = asyncio.run(go())

    assert first[0] != "ConsentDialog" and first[1] == "ConsentDialog"
    assert first[2] == "consent-no", "No thanks has the focus: Enter alone does not opt in"
    assert after == "1" and depth == 1 and again[0] != "ConsentDialog"
    assert fake.calls.count("consent_due") == 1 and fake.calls.count("declined") == 1
    assert "share" not in fake.calls and "enable" not in fake.calls


def test_share_anonymous_usage_turns_it_on_and_sends_the_first_shipment(
    tmp_path: Path, sharing_allowed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = guard_clock.install(monkeypatch)
    add_project(tmp_path / "work")
    fake = FakeSharing(signed_in=True)

    async def go() -> list[str]:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)) as pilot:
            await settle_page(app)
            await _open_settings(app, pilot)
            clock.step()
            await pilot.click("#consent-share")
            await settle_page(app)
            await settle_until(app, lambda: "share" in fake.calls)
            return [type(app.screen).__name__]

    assert asyncio.run(go()) != ["ConsentDialog"]
    assert fake.calls == ["consent_due", "share"]
    assert fake.status_now.enabled is True


def test_escape_is_no_thanks(
    tmp_path: Path, sharing_allowed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_project(tmp_path / "work")
    fake = FakeSharing()

    async def go() -> int:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)) as pilot:
            await settle_page(app)
            await _open_settings(app, pilot)
            await pilot.press("escape")
            await settle_page(app)
            await settle_until(app, lambda: "declined" in fake.calls)
            return len(app.screen_stack)

    assert asyncio.run(go()) == 1 and fake.calls == ["consent_due", "declined"]


def test_see_exactly_whats_sent_opens_the_preview_over_the_question_and_returns_to_it(
    tmp_path: Path, sharing_allowed: None
) -> None:
    add_project(tmp_path / "work")
    fake = FakeSharing()

    async def go() -> tuple[list[str], list[str]]:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)) as pilot:
            await settle_page(app)
            await _open_settings(app, pilot)
            await pilot.click("#consent-see")
            await settle_page(app)
            over = [type(screen).__name__ for screen in app.screen_stack[1:]]
            await pilot.press("escape")  # closes the preview, not the question
            await settle_page(app)
            back = [type(screen).__name__ for screen in app.screen_stack[1:]]
            return over, back

    over, back = asyncio.run(go())

    assert over == ["ConsentDialog", "PreviewDialog"] and back == ["ConsentDialog"]
    assert fake.calls.count("preview") == 1 and "declined" not in fake.calls


def test_the_question_is_never_asked_where_something_forces_sharing_off(tmp_path: Path) -> None:
    """The suite forces it off (AISQUARE_NO_TELEMETRY), as a CI run or DO_NOT_TRACK does."""
    add_project(tmp_path / "work")
    fake = FakeSharing()

    async def go() -> int:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)) as pilot:
            await settle_page(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            return len(app.screen_stack)

    assert asyncio.run(go()) == 1 and "consent_due" not in fake.calls


def test_the_question_is_not_asked_when_it_was_answered_before(
    tmp_path: Path, sharing_allowed: None
) -> None:
    add_project(tmp_path / "work")
    fake = FakeSharing(due=False)

    async def go() -> int:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)) as pilot:
            await settle_page(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            await settle_until(app, lambda: "consent_due" in fake.calls)
            return len(app.screen_stack)

    assert asyncio.run(go()) == 1 and fake.calls == ["consent_due"]


def test_with_no_project_yet_there_is_no_question(tmp_path: Path, sharing_allowed: None) -> None:
    fake = FakeSharing()

    async def go() -> int:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)) as pilot:
            await settle_page(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            return len(app.screen_stack)

    assert asyncio.run(go()) == 1 and "consent_due" not in fake.calls


# --- the shell's shipper ----------


def test_the_housekeeping_tick_tries_a_shipment_only_where_sharing_is_on(
    sharing_allowed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSharing()
    state = {"on": False}
    monkeypatch.setattr(sharing_core, "enabled", lambda: state["on"])

    async def go() -> tuple[list[str], list[str], list[str]]:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)):
            await settle_page(app)
            app._ship_at = 0.0
            app._housekeeping()
            await settle_page(app)
            off = list(fake.calls)
            state["on"] = True
            app._ship_at = 0.0
            app._housekeeping()
            await settle_page(app)
            on = list(fake.calls)
            app._housekeeping()  # the next look is five minutes away
            await settle_page(app)
            return off, on, list(fake.calls)

    off, on, again = asyncio.run(go())

    assert off == [] and on == ["ship_due"] and again == ["ship_due"]


def test_the_shell_does_not_look_at_the_shipper_before_a_minute_has_passed(
    sharing_allowed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSharing()
    monkeypatch.setattr(sharing_core, "enabled", lambda: True)

    async def go() -> list[str]:
        app = _fleet(fake)
        async with app.run_test(size=(130, 50)):
            await settle_page(app)
            app._housekeeping()
            await settle_page(app)
            return list(fake.calls)

    assert asyncio.run(go()) == []
    assert usage_share.TIMEOUT_S == 5.0
