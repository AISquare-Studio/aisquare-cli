"""A start that carries a first prompt obeys 13227 (review of #240, finding 2).

The Spawn dialog starts the captain through ``brain.start`` with whatever the owner typed
under First prompt. That prompt went on to ``fleet.spawn``, whose first-prompt typing reads
the pane's process and never its text: at the trust dialog a fresh captain parks at, its
Enter picked the highlighted "No, exit", the captain exited, and the receipt said
``prompt_typed=True``. The standing rule is that a fresh captain is started bare and typed
into only once its input box shows. ``say`` kept it; a start with a prompt did not.

The fleet is the real one here, over the fleet suite's fake tmux, which is given a screen:
what reaches the pane is read off the fake, never off a recorder of ``brain``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from aisquare.cli.ui import spawn as spawn_dialog
from aisquare.core import tmux as tmux_core
from aisquare.core.config import FleetSettings
from aisquare.core.tmux import Capture, TmuxError, TmuxServer, WindowInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import brain
from tests import captain_screens as shots
from tests import test_fleet_service as fleet_suite
from tests.test_captain_say import Clock
from tests.test_fleet_service import FakeTmux

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests
# (``clock`` is autouse there: the fleet's own waits pass on a fake clock).
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path

FIRST = "what is up"


class ScreenTmux(FakeTmux):
    """The fleet suite's fake tmux with the two things a captain's real window adds: its
    foreground is Claude Code as soon as it is up, and its pane shows ``screen``."""

    def __init__(self) -> None:
        super().__init__()
        self.screen: list[str] = []
        self.unreadable = False

    def spawn_window(
        self,
        session: str,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        width: int = tmux_core.DEFAULT_WINDOW_WIDTH,
        height: int = tmux_core.DEFAULT_WINDOW_HEIGHT,
    ) -> WindowInfo:
        window = super().spawn_window(
            session, name=name, cwd=cwd, command=command, env=env, width=width, height=height
        )
        self.set_command(window.pane_id, "claude")
        return window

    def capture(
        self, pane_id: str, *, scrollback: int = 0, height: int | None = None, flags: bool = False
    ) -> Capture:
        if self.unreadable:
            raise TmuxError("tmux capture-pane failed: no such pane")
        return Capture(lines=list(self.screen), facts=self.facts[pane_id], scrollback=0)

    def reached(self) -> list[tuple[str, str]]:
        """What reached any pane, in order: ``("paste", text)``, ``("key", "Enter")``."""
        return [(kind, text) for _, kind, text in self.typed]


@pytest.fixture
def tmux(monkeypatch: pytest.MonkeyPatch) -> ScreenTmux:
    """The fleet suite's ``tmux`` fixture, answering with the fake that has a screen."""
    fake = ScreenTmux()

    def factory(config: FleetSettings | None = None) -> TmuxServer:
        return fake

    monkeypatch.setattr(fleet_service, "server", factory)
    monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
    return fake


@pytest.fixture
def wait(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """``brain``'s clock: sleeping advances it, so a wait for the box costs no real time."""
    fake = Clock()
    monkeypatch.setattr(brain, "_now", fake)
    monkeypatch.setattr(brain, "_sleep", fake.sleep)
    return fake


def _about_the_prompt(receipt: fleet_service.SpawnReceipt) -> list[str]:
    return [note for note in receipt.notes if "first prompt" in note]


# --- the pin: a fresh captain at Claude Code's trust dialog -----------------------------------


def test_the_spawn_dialogs_first_prompt_is_never_typed_into_the_trust_dialog(
    tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    """The finding's repro, through the dialog's own start: the real capture of the trust
    dialog, "No, exit" highlighted. Nothing is typed, and the receipt says so and why."""
    tmux.screen = list(shots.REAL_TRUST)
    receipt = spawn_dialog.start_captain({"prompt": FIRST})
    assert tmux.reached() == [], "nothing reached the dialog, whose Enter picks No, exit"
    assert receipt.prompt_typed is False, "never reported typed when it was not"
    (note,) = _about_the_prompt(receipt)
    assert note.startswith("the first prompt was NOT typed: ")
    assert "trust its folder" in note and str(brain.brain_dir()) in note
    assert note in receipt.failures
    assert wait.slept == 0.0, "said at once, not waited out"
    assert receipt.agent.ended_at is None, "and the captain is up, for the owner to attach to"


@pytest.mark.parametrize(
    ("screen", "showing"),
    [
        pytest.param(shots.REAL_CHOOSER, "a numbered choice", id="the real chooser"),
        pytest.param(shots.MODEL_PICKER, "a dialog waiting for Enter or Esc", id="a picker"),
        pytest.param(shots.RATING_ABOVE_BOX, "the session-rating prompt", id="the survey"),
    ],
)
def test_a_first_prompt_is_never_typed_into_a_dialog_and_the_note_names_what_shows(
    screen: list[str], showing: str, tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    """``say`` and ``send``'s refusals, word for word (13227, 13325): the one guarded door."""
    tmux.screen = list(screen)
    receipt = brain.start(FIRST)
    assert tmux.reached() == []
    assert receipt.prompt_typed is False
    (note,) = _about_the_prompt(receipt)
    assert note.startswith(f"the first prompt was NOT typed: the captain's pane shows {showing}")
    assert note in receipt.failures


def test_a_first_prompt_is_not_typed_blind_into_a_pane_that_cannot_be_read(
    tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    tmux.unreadable = True
    receipt = brain.start(FIRST)
    assert tmux.reached() == []
    assert receipt.prompt_typed is False
    (note,) = _about_the_prompt(receipt)
    assert "could not read the captain's pane" in note


def test_a_first_prompt_is_not_typed_when_the_box_never_draws(
    tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    """The fleet's first-prompt typing went ahead once the process was up, whatever the pane
    showed. The drawn box is the evidence typing needs (M3): without it, a note at the
    deadline, and a short one, because the caller is a button."""
    tmux.screen = []
    receipt = brain.start(FIRST)
    assert tmux.reached() == []
    assert receipt.prompt_typed is False
    (note,) = _about_the_prompt(receipt)
    assert f"never drew its prompt for {brain.SEND_TIMEOUT_S:g}s" in note
    assert brain.SEND_TIMEOUT_S <= wait.slept < brain.SEND_TIMEOUT_S + 5


def test_a_first_prompt_never_types_while_a_say_waits_for_its_reply(
    tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    """One delivery at a time, as for ``send``: the door is one door."""
    tmux.screen = list(shots.INPUT_BOX)
    with brain._one_at_a_time(brain._now() + timedelta(seconds=5), 5.0):
        receipt = brain.start(FIRST)
    assert tmux.reached() == []
    assert receipt.prompt_typed is False
    (note,) = _about_the_prompt(receipt)
    assert "another message to the captain is still waiting" in note


# --- the control: an idle input box still gets the prompt ------------------------------------


@pytest.mark.parametrize(
    "box",
    [
        pytest.param(shots.INPUT_BOX, id="the input box"),
        pytest.param(shots.REAL_IDLE_AFTER_STOP, id="the real idle box, its top rule named"),
    ],
)
def test_a_first_prompt_is_still_typed_at_a_drawn_input_box(
    box: list[str], tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    """One paste, one Enter, and a receipt that says typed with nothing to add."""
    tmux.screen = list(box)
    receipt = brain.start(FIRST)
    assert tmux.reached() == [("paste", FIRST), ("key", "Enter")]
    assert receipt.prompt_typed is True
    assert _about_the_prompt(receipt) == [] and receipt.failures == []


def test_a_first_prompt_goes_in_after_the_new_boxs_one_settle(
    tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    """The door's own settle (coderp's M3), as ``say`` gives a captain it started: a box that
    has just drawn may not have bracketed paste on yet."""
    tmux.screen = list(shots.INPUT_BOX)
    brain.start(FIRST)
    assert tmux.reached() == [("paste", FIRST), ("key", "Enter")]
    assert wait.slept == brain.TYPE_SETTLE_S


def test_a_first_prompt_waits_for_the_box_and_is_typed_once_it_shows(
    tmux: ScreenTmux, claude_on_path: Path, wait: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window that is up with nothing drawn yet is not a place to type either."""
    drawn_at: list[float] = []

    def draw_after_three_polls(seconds: float) -> None:
        wait.sleep(seconds)
        if wait.slept >= 3.0 and not tmux.screen:
            tmux.screen = list(shots.INPUT_BOX)
            drawn_at.append(wait.slept)

    monkeypatch.setattr(brain, "_sleep", draw_after_three_polls)
    receipt = brain.start(FIRST)
    assert tmux.reached() == [("paste", FIRST), ("key", "Enter")]
    assert receipt.prompt_typed is True
    assert drawn_at, "typed only after the box had drawn"
    assert wait.slept - drawn_at[0] >= brain.TYPE_SETTLE_S, "and after its one settle"


def test_a_bare_start_types_nothing_and_reads_no_pane(
    tmux: ScreenTmux, claude_on_path: Path, wait: Clock
) -> None:
    """``say`` and the bare command start the captain with no prompt: as before, no wait, no
    read (a read here would raise), nothing about a prompt on the receipt."""
    tmux.unreadable = True
    receipt = brain.start()
    assert tmux.reached() == []
    assert receipt.prompt_typed is None
    assert _about_the_prompt(receipt) == [] and receipt.failures == []
    assert wait.slept == 0.0
