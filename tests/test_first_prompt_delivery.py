"""P0-W1a (#122): every first message reaches its agent and starts a turn, or the board says
it did not.

W0 measured the old one-Enter rule on a live box (``docs/plans/handoff-p0-reliability.md``,
"W0 results"): an Enter that reaches Claude Code before it draws its input box is dropped
while the text is kept, and one Enter once the box is drawn sends it. So a Claude Code agent
is given its first message on its command line (``claude -- "<prompt>"``), in a file the
launcher reads and deletes, and the row records it ``pending`` until the agent's
``UserPromptSubmit`` takes it. Everything else is typed through ``services.delivery``: once
the box is drawn, checked in the box, Enter as its own call, and pressed again only while
the text provably still waits there. ``fleet spawn coder --task <id>`` with no ``--prompt``
gives the agent a kickoff naming its task, where it used to type nothing.

Driven as ``tests/test_fleet_service.py`` drives the fleet: the real store in an isolated
home, real git under ``tmp_path``, and that suite's in-memory ``FakeTmux``, here taught to
draw Claude Code 2.1.309's input box the way W0 captured it.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import claude_screen as screen
from aisquare.core import harness
from aisquare.core import tmux as tmux_core
from aisquare.core.config import FleetSettings
from aisquare.core.store import store_session
from aisquare.core.tmux import WindowInfo
from aisquare.models import CheckStatus, FleetAgent, ProjectInfo
from aisquare.services import delivery, diagnostics
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.captain_screens import MARK, REAL_RULE, REAL_TOP_RULE
from tests.cycles import unscoped
from tests.fsperms import can_symlink
from tests.test_fleet_service import FakeClock, FakeTmux, _board_session, _command, _flag

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


def _box(composer: str = "", *, dim: str = "") -> list[str]:
    """Claude Code 2.1.309's idle screen as W0 captured it: the input box, ``composer`` in it
    after the mark and its no-break space, or Claude Code's own dim words (SGR 2)."""
    shown = f"\x1b[2m{dim}\x1b[0m" if dim else composer
    return [
        "✻ Cooked for 2s · done",
        REAL_TOP_RULE,
        f"\x1b[39m{MARK}\xa0{shown}",
        REAL_RULE,
        "  ⏵⏵ auto mode on (shift+tab to cycle)",
    ]


def _first_file(command: list[str]) -> Path:
    path = _flag(command, delivery.FIRST_PROMPT_FLAG)
    assert path is not None, f"no {delivery.FIRST_PROMPT_FLAG} in {command}"
    return Path(path)


def _row(agent_id: str) -> FleetAgent:
    with store_session() as store:
        row = store.get_fleet_agent(agent_id)
    assert row is not None
    return row


class ClaudeCode(FakeTmux):
    """A FakeTmux whose panes draw Claude Code's input box: a paste lands in it, and an Enter
    sends it, which its ``UserPromptSubmit`` hook reports (``on_submit``) — unless the Enter
    is swallowed, the way W0 measured an Enter sent before the box is drawn."""

    def __init__(self, *, swallow: int = 0) -> None:
        super().__init__()
        self.swallow = swallow
        self.submitted: list[str] = []
        self.on_submit: Callable[[str], None] | None = None
        self.after_enter: Callable[[str], None] | None = None

    def paste(self, pane_id: str, text: str) -> None:
        super().paste(pane_id, text)
        self.screens[pane_id] = _box(text)

    def send_keys(self, pane_id: str, *keys: str) -> None:
        super().send_keys(pane_id, *keys)
        if keys != ("Enter",):
            return
        if self.swallow:
            self.swallow -= 1
        else:
            composer = screen.composer_text(self.screens.get(pane_id, []))
            if composer:
                self.submitted.append(composer)
                self.screens[pane_id] = _box()
                if self.on_submit is not None:
                    self.on_submit(pane_id)
        if self.after_enter is not None:
            self.after_enter(pane_id)

    def enters(self, pane_id: str) -> int:
        return sum(1 for pane, kind, text in self.typed if pane == pane_id and text == "Enter")


def _claude_pane(fake: FakeTmux, tmp_path: Path) -> str:
    window: WindowInfo = fake.spawn_window("asq-t", name="coder-1", cwd=tmp_path, command=["c"])
    fake.set_command(window.pane_id, "claude")
    fake.screens[window.pane_id] = _box()
    return window.pane_id


def _deliver(
    fake: ClaudeCode, pane: str, clock: FakeClock, *, acked: Callable[[], bool] | None
) -> delivery.Delivered:
    return delivery.deliver_typed(
        fake,
        pane,
        "start on tsk_1",
        "the agent",
        press="press Enter in its pane",
        acked=acked,
        reads_box=True,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


# --- the composer, read by structure ----------------------------------------------------------


def test_the_composer_reads_a_draft_and_never_claude_codes_own_dim_words() -> None:
    """W0's captures: after a turn Claude Code draws a suggestion in the empty box, and a fresh
    session a placeholder, both dim (SGR 2). Read after the escapes were stripped they were
    drafts, and nothing would ever type into such an agent."""
    assert screen.composer_text(_box("my own draft")) == "my own draft"
    assert screen.composer_text(_box(dim="check the board again")) == ""
    assert screen.composer_text(_box(dim='Try "edit <filepath> to..."')) == ""
    assert screen.composer_text(_box()) == ""
    assert screen.composer_text(["Quick safety check", f" {MARK} No, exit"]) is None, "no box"


# --- the typed way: pressed again only while it provably did not submit ----------------------


def test_a_swallowed_enter_is_pressed_again_until_the_turn_starts(
    clock: FakeClock, tmp_path: Path
) -> None:
    """The failing case of #122, typed: the first Enter is dropped, the text stays in the box,
    and the agent sits idle with its message unsent. The door reads the box, sees its text
    still there with no turn started, and presses again; the second Enter sends it."""
    fake = ClaudeCode(swallow=1)
    pane = _claude_pane(fake, tmp_path)

    done = _deliver(fake, pane, clock, acked=lambda: bool(fake.submitted))

    assert fake.submitted == ["start on tsk_1"]
    assert (done.told.typed, done.acked, done.presses) == ("yes", True, 2)
    assert fake.enters(pane) == 2 and clock.now < 10, "pressed again after the first wait"


def test_an_enter_that_sends_it_is_pressed_once(clock: FakeClock, tmp_path: Path) -> None:
    """Negative control: the first Enter submits, so nothing is pressed again."""
    fake = ClaudeCode()
    pane = _claude_pane(fake, tmp_path)

    done = _deliver(fake, pane, clock, acked=lambda: bool(fake.submitted))

    assert (done.told.typed, done.acked, done.presses) == ("yes", True, 1)
    assert fake.enters(pane) == 1


@pytest.mark.parametrize(
    "after",
    [_box("my own words"), _box(), ["Allow this?", f" {MARK} 1. Yes", "Esc to cancel"]],
    ids=["a-human-replaced-it", "a-human-cleared-it", "a-dialog-came-up"],
)
def test_a_composer_a_human_changed_is_never_pressed_again(
    clock: FakeClock, tmp_path: Path, after: list[str]
) -> None:
    """The narrower rule that replaces "never a second Enter" (review of #259, round 10): an
    Enter into a box that holds anything but our text, or none, or under a dialog, answers
    whatever is there — a human's draft sent, "1. Yes" to a permission nobody saw."""
    fake = ClaudeCode(swallow=1)
    pane = _claude_pane(fake, tmp_path)

    def a_human(pane_id: str) -> None:
        fake.screens[pane_id] = after

    fake.after_enter = a_human
    done = _deliver(fake, pane, clock, acked=lambda: bool(fake.submitted))

    assert fake.enters(pane) == 1, "never pressed again"
    assert done.acked is False and fake.submitted == []


def test_a_message_still_in_the_box_after_every_press_is_unsent(
    clock: FakeClock, tmp_path: Path
) -> None:
    """Eight more presses on the schedule, 60 s in all, then it says so: ``unsent``, and the
    text is at the agent's prompt for an Enter to send."""
    fake = ClaudeCode(swallow=99)
    pane = _claude_pane(fake, tmp_path)

    done = _deliver(fake, pane, clock, acked=lambda: bool(fake.submitted))

    assert done.told.typed == "unsent" and done.acked is False
    assert done.presses == fake.enters(pane) == 1 + len(delivery.RE_PRESS)
    assert clock.now == pytest.approx(60 + delivery.ENTER_GAP, abs=1), "the text rendered at once"
    assert isinstance(done.told, delivery.TellResult)
    assert "still in its input box" in done.told.how


# --- the command line ---------------------------------------------------------------------------


def test_the_first_prompt_rides_the_command_line_in_a_file_and_never_the_spec(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A Claude Code agent's first message travels in a file only this user reads, named to
    ``launch`` by path: nothing is typed, so no Enter can be dropped, and the row's recorded
    launch, which a restart replays, never holds the text."""
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    command = _command(tmux)
    path = _first_file(command)

    assert tmux.typed == [], "nothing typed"
    assert path.read_text(encoding="utf-8") == "start on tsk_1"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "start on tsk_1" not in " ".join(command)
    assert (receipt.prompt_typed, receipt.prompt_delivery) == ("yes", "argv")
    row = _row(receipt.agent.id)
    assert row.first_prompt == "pending" and row.first_prompt_at is not None
    spec = row.launch_spec
    assert spec is not None and "start on tsk_1" not in spec.model_dump_json()
    assert delivery.FIRST_PROMPT_FLAG not in spec.extra_args
    # The minted session id is still planned and passed: the prompt is not in the arguments
    # the identity planner reads (services/explainability.py), so it cannot be read as one.
    assert _flag(command, "--session-id") == row.session_id is not None


def test_no_prompt_and_no_task_give_no_first_message(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder", worktree=False)
    assert delivery.FIRST_PROMPT_FLAG not in _command(tmux)
    assert (receipt.prompt_typed, receipt.prompt_delivery, receipt.prompt_acked) == (None,) * 3
    assert _row(receipt.agent.id).first_prompt is None


def test_spawn_with_a_task_and_no_prompt_passes_a_kickoff(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """#122's original report: ``fleet spawn coder --task <id>`` — the form the manager's
    briefing names — typed nothing, and the coder waited at an empty prompt. Its kickoff
    names the task as its ASSIGNED TO YOU block does, so the two never disagree; a
    ``--prompt`` beside the task follows the kickoff."""
    task, _ = team_service.add_task(
        "wire auth", key="wire-auth", role="coder", detail="the contract", cwd=project.root
    )
    receipt = fleet_service.spawn(project, "coder", worktree=False, task_id=task.id)
    kickoff = _first_file(_command(tmux)).read_text(encoding="utf-8")
    sid = team_service.short_id(receipt.agent.session_id or "")

    assert kickoff.startswith(f"You are {receipt.agent.label}.\nASSIGNED TO YOU: {task.id}")
    assert f"`aisquare task claim {task.id} --as {sid}`" in kickoff
    assert receipt.prompt_delivery == "argv" and tmux.typed == []

    fleet_service.spawn(project, "coder", worktree=False, task_id=task.id, prompt="use OAuth")
    both = _first_file(_command(tmux)).read_text(encoding="utf-8")
    assert both.startswith("You are ") and both.endswith("\n\nuse OAuth")


def test_a_restart_replays_no_first_prompt(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The restart is given its own first line (the resume line, or a hand-off from the
    board), never the first agent's message: the spec it replays holds none, and the
    replayed launch of the old command finds the old file read and gone."""
    first = fleet_service.spawn(project, "coder", worktree=False, prompt="secret first words")
    old = _first_file(_command(tmux))
    assert delivery.take_prompt_file(old) == "secret first words"  # its launch read it

    receipt = fleet_service.restart(project, first.agent.label)
    line = _first_file(_command(tmux)).read_text(encoding="utf-8")

    assert "secret first words" not in line and line.strip()
    assert (receipt.prompt_typed, receipt.prompt_delivery) == ("yes", "argv")
    assert "secret first words" not in _row(receipt.started.id).model_dump_json()
    assert delivery.take_prompt_file(old) is None, "a replay of the old command finds no file"


def test_the_typed_way_is_kept_for_type_and_it_waits_for_the_box(
    claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[fleet] first_prompt = "type"`` types it, and for Claude Code ready means its input
    box drawn, not a settle after the launcher leaves (W0): an Enter swallowed anyway is
    pressed again, and the row's ``UserPromptSubmit`` acknowledges the second."""
    fake = ClaudeCode(swallow=1)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: fake)
    monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
    monkeypatch.setattr(fleet_service, "settings", lambda: FleetSettings(first_prompt="type"))
    # Its SessionStart taken as come, as the fleet suite's `tmux` fixture has it: a Claude Code
    # prompt typed waits for that start first (`fleet._type_at_its_start`, #113).
    monkeypatch.setattr(fleet_service, "_started_since", lambda session_id, launched: True)
    looks, captures = 0, 0
    original, original_capture = fake.pane_facts, fake.capture

    def coming_up(pane_id: str) -> Any:
        nonlocal looks
        looks += 1
        if looks == 2:
            fake.set_command(pane_id, "claude")
        return original(pane_id)

    def drawn_a_beat_later(pane_id: str, **kwargs: Any) -> Any:
        nonlocal captures
        captures += 1
        if captures == 3 and pane_id not in fake.screens:
            fake.screens[pane_id] = _box()  # the box comes a beat after the exec
        return original_capture(pane_id, **kwargs)

    monkeypatch.setattr(fake, "pane_facts", coming_up)
    monkeypatch.setattr(fake, "capture", drawn_a_beat_later)

    def hook(pane_id: str) -> None:
        agent_id = str(fake.spawned[-1]["env"]["AISQUARE_FLEET_AGENT"])  # type: ignore[index]
        with store_session() as store:
            store.mark_prompted("no-such-session", datetime.now(tz=UTC), agent_id=agent_id)

    fake.on_submit = hook
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    pane = receipt.agent.pane_id

    assert delivery.FIRST_PROMPT_FLAG not in _command(fake)
    assert fake.typed[0] == (pane, "paste", "start on tsk_1") and fake.enters(pane) == 2
    assert (receipt.prompt_typed, receipt.prompt_delivery, receipt.prompt_acked) == (
        "yes",
        "typed",
        True,
    )
    assert _row(receipt.agent.id).first_prompt == "acked"


# --- the launcher -----------------------------------------------------------------------------


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched (as tests/test_launch.py)."""
    import shutil

    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def test_the_launcher_reads_and_deletes_the_first_prompt_and_passes_it_after_a_double_dash(
    runner: CliRunner,
    tmp_path: Path,
    spy: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last on Claude Code's command line, after ``--``: a prompt that starts with ``-`` is
    otherwise read as an option and Claude Code exits (W0). Read once: the file is gone,
    and a second launch of the same command starts without it, saying so."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    path = delivery.write_prompt_file("agt_launch", "-starts with a dash")

    first = runner.invoke(app, ["launch", "coder", delivery.FIRST_PROMPT_FLAG, str(path)])
    assert first.exit_code == 0, first.output
    assert spy["argv"][-2:] == ["--", "-starts with a dash"]
    assert not path.exists()

    spy.clear()
    again = runner.invoke(app, ["launch", "coder", delivery.FIRST_PROMPT_FLAG, str(path)])
    assert again.exit_code == 0, again.output
    assert "--" not in spy["argv"] and "starting without one" in again.output


@pytest.mark.parametrize("where", ["outside", "wrong-name", "a-link-out"])
def test_the_launcher_never_takes_a_file_the_fleet_did_not_write(
    runner: CliRunner,
    tmp_path: Path,
    spy: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """Review of #330, round 1: the option is hidden, not private. Pointed at ``~/.ssh/config``
    the launch read it into the agent's first message and deleted it. Only ``<agent
    id>.txt`` directly in ``run/prompts``, as its real path, is taken; anything else is
    neither read nor deleted, and the agent starts without a first message, which it says."""
    if where == "a-link-out" and not can_symlink():
        pytest.skip("no symlinks here")
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    secret = tmp_path / "config"
    secret.write_text("Host *\n  IdentityFile ~/.ssh/id_ed25519\n", encoding="utf-8")
    folder = delivery.prompts_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handed = {
        "outside": secret,
        "wrong-name": folder / "notes.txt",
        "a-link-out": folder / "agt_01linkout.txt",
    }[where]
    if where == "wrong-name":
        handed.write_text("not the fleet's", encoding="utf-8")
    if where == "a-link-out":
        handed.symlink_to(secret)

    result = runner.invoke(app, ["launch", "coder", delivery.FIRST_PROMPT_FLAG, str(handed)])

    assert result.exit_code == 0, result.output
    assert "--" not in spy["argv"] and "IdentityFile" not in " ".join(spy["argv"])
    assert secret.exists() and handed.exists(), "neither read into a prompt nor deleted"
    assert "not read, not deleted" in result.output


# --- acknowledgement, and a first message never taken ---------------------------------------


def test_user_prompt_submit_stamps_prompted_at_and_takes_the_first_prompt(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acknowledgement: the agent's own ``UserPromptSubmit`` stamps its session's
    ``prompted_at`` and flips its row from ``pending`` to ``acked`` — the row the window's
    ``AISQUARE_FLEET_AGENT`` names."""
    agent = fleet_service.spawn(project, "coder", worktree=False, prompt="go").agent
    assert agent.session_id is not None
    _board_session(agent, "waiting")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")

    team_service.hook_prompt_heartbeat(agent.session_id, project.root)

    with store_session() as store:
        session = store.get_session(agent.session_id)
    row = _row(agent.id)
    assert session is not None and session.prompted_at is not None
    assert row.first_prompt == "acked" and row.first_prompt_at is not None
    assert fleet_service.spawn(project, "coder", worktree=False, prompt="x").prompt_acked is False


def test_spawn_waits_for_the_acknowledgement_it_is_given_time_for(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The CLI's wait (``[fleet] prompt_ack_seconds``): with no ``UserPromptSubmit`` it gives
    up at the wait and says the message is pending, with what a listing will say."""
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="go", ack_wait=45.0)
    assert receipt.prompt_acked is False and 45.0 <= clock.now < 46.0
    assert any("no turn has started on its first message yet" in note for note in receipt.notes)


def test_an_unacknowledged_first_prompt_reads_attention_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first message still pending past ``prompt_ack_seconds`` at an idle input box is not
    going to be taken: the agent reads ``attention`` with what to do, and the board gets one
    ``prompt_unsent``, which wakes the manager, however many listings find it. A prompt that
    takes it ends that."""
    agent = fleet_service.spawn(project, "coder", worktree=False, prompt="go").agent
    assert agent.session_id is not None
    _board_session(agent, "waiting")
    tmux.screens[agent.pane_id] = _box("go")  # typed, never sent
    later = datetime.now(tz=UTC) + timedelta(seconds=46)
    monkeypatch.setattr(fleet_service, "_now", lambda: later)

    def unsent_events() -> list[str]:
        with store_session() as store:
            events = store.events_since(project.id, 0, limit=500)
        return [event.text for event in events if event.kind == "prompt_unsent"]

    for _ in range(2):
        [status] = fleet_service.list_agents(project)
        assert status.state == "attention"
        assert status.detail == fleet_service.unsent_detail(project, agent.label)
        assert status.detail.startswith("first prompt not taken — `aisquare fleet tell --project")
    assert len(unsent_events()) == 1 and _row(agent.id).first_prompt == "unsent"
    assert "prompt_unsent" in team_service.MANAGER_WAKE_KINDS

    with store_session() as store:
        store.mark_prompted(agent.session_id, later, agent_id=agent.id)  # a human's Enter
    [status] = fleet_service.list_agents(project)
    assert status.state != "attention"


def test_a_first_prompt_held_at_the_security_notes_after_session_start_is_never_silent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-FU1 (review of #404): a command-line first message waits behind Claude Code's
    first-run Security notes, whose only key is Enter. Its SessionStart has fired, so the
    trust-question read (no hook since the row began) never looks, and the screen is no idle
    box: the row read plain ``waiting`` and stayed ``pending``, silent, for good. Past
    ``prompt_ack_seconds`` it moves to ``unsent`` once, the board gets one ``prompt_unsent``
    naming the notes, and every listing reads ``attention`` with the notes' one key, not a
    ``fleet tell`` its notes would lose. Pressed past to an idle box, it reads as any
    message not taken; a prompt then takes it."""
    agent = fleet_service.spawn(project, "coder", worktree=False, prompt="go").agent
    assert agent.session_id is not None and agent.first_prompt == "pending"
    _board_session(agent, "waiting")  # its SessionStart has fired
    tmux.screens[agent.pane_id] = fleet_suite.SECURITY_SCREEN
    later = datetime.now(tz=UTC) + timedelta(seconds=FleetSettings().prompt_ack_seconds + 400)
    monkeypatch.setattr(fleet_service, "_now", lambda: later)
    nudges: list[str] = []
    monkeypatch.setattr(
        fleet_service, "nudge_manager", lambda *args, **kwargs: nudges.append(kwargs["reason"])
    )

    def unsent_events() -> list[str]:
        with store_session() as store:
            events = store.events_since(project.id, 0, limit=500)
        return [event.text for event in events if event.kind == "prompt_unsent"]

    for _ in range(2):
        [status] = fleet_service.list_agents(project)
        assert status.state == "attention"
        assert status.detail == f"{fleet_service.SECURITY_NOTES} — press Enter in its window"
    [said] = unsent_events()
    assert said == f"{agent.label}: first prompt not taken — held at Claude Code's Security notes"
    assert _row(agent.id).first_prompt == "unsent" and len(nudges) == 1

    tmux.screens[agent.pane_id] = _box()  # a human pressed Enter past the notes
    [status] = fleet_service.list_agents(project)
    assert (status.state, status.detail) == (
        "attention",
        fleet_service.unsent_detail(project, agent.label),
    )
    assert len(unsent_events()) == 1

    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_prompt_heartbeat(agent.session_id, project.root)  # its first turn starts
    [status] = fleet_service.list_agents(project)
    assert status.state != "attention" and _row(agent.id).first_prompt == "acked"


@pytest.mark.parametrize("evidence", ["its-stop", "its-session-stamp"])
def test_a_turn_whose_prompt_record_failed_is_not_read_as_not_taken(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    evidence: str,
) -> None:
    """Review of #330, round 1: the ``UserPromptSubmit`` hook fails open, so a locked store
    costs its record of the prompt, not the turn. The row stayed ``pending`` under a turn
    that ran, and once the agent sat idle past the wait a listing posted ``prompt_unsent``
    and woke the manager. The turn's own evidence takes it instead: the ``Stop`` that ended
    the turn, or the session's ``prompted_at`` when only the row's update was lost."""
    agent = fleet_service.spawn(project, "coder", worktree=False, prompt="go").agent
    assert agent.session_id is not None
    _board_session(agent, "waiting")
    if evidence == "its-stop":
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
        team_service.hook_stop(agent.session_id, project.root)
        monkeypatch.delenv("AISQUARE_FLEET_AGENT")
    else:
        with store_session() as store:
            store._conn.execute(  # type: ignore[attr-defined]
                "UPDATE team_session SET prompted_at = ? WHERE id = ?",
                ((datetime.now(tz=UTC) + timedelta(seconds=2)).isoformat(), agent.session_id),
            )
            store._conn.commit()  # type: ignore[attr-defined]
    tmux.screens[agent.pane_id] = _box()  # idle, the turn done
    later = datetime.now(tz=UTC) + timedelta(seconds=46)
    monkeypatch.setattr(fleet_service, "_now", lambda: later)

    [status] = fleet_service.list_agents(project)

    assert status.state != "attention" and _row(agent.id).first_prompt == "acked"
    with store_session() as store:
        events = store.events_since(project.id, 0, limit=500)
    assert [e for e in events if e.kind == "prompt_unsent"] == []


def test_a_first_prompt_still_inside_its_wait_is_not_attention(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Negative control: pending, but within ``prompt_ack_seconds`` — no capture, no event."""
    agent = fleet_service.spawn(project, "coder", worktree=False, prompt="go").agent
    _board_session(agent, "waiting")
    tmux.screens[agent.pane_id] = _box("go")
    tmux.captured.clear()
    [status] = fleet_service.list_agents(project)
    assert status.state != "attention" and agent.pane_id not in tmux.captured


# --- the store, the harness, doctor, the CLI ---------------------------------------------------


def test_a_v35_store_gains_the_first_prompt_columns_and_a_second_open_changes_nothing() -> None:
    """Schema 36 over main's store at 35: three nullable columns, read as "not tracked" on rows
    from before them; the presence pass finds them on the next open and adds nothing."""
    from aisquare.core.store import SCHEMA_VERSION, open_store
    from tests.test_store import _at_version, _contents, _db_path

    db = _at_version(35)
    import sqlite3

    raw = sqlite3.connect(str(db))
    try:
        raw.execute(
            "INSERT INTO fleet_agent (id, project_id, label, role, pane_id, cwd, created_at) "
            "VALUES ('agt_old', 'prj_old', 'coder-1', 'coder', '%1', '/tmp', "
            "'2026-10-01T00:00:00+00:00')"
        )
        raw.commit()
    finally:
        raw.close()
    store = open_store()
    try:
        old = store.get_fleet_agent("agt_old")
    finally:
        store.close()
    once = _contents(db)
    open_store().close()

    # W1a's step is v36; P1 WS-2's (#279) is v37 on a train that holds both.
    assert SCHEMA_VERSION >= 36 and _contents(db) == once
    assert old is not None and (old.first_prompt, old.first_prompt_at) == (None, None)
    assert _db_path() == db


def test_the_manager_cycle_says_what_task_delivers() -> None:
    manager = " ".join(harness.role_cycle("manager", "abcd1234", fleet=unscoped))
    assert "`--task` delivers the task; add `--prompt` only for extra instructions" in manager


def test_doctor_counts_the_weeks_first_messages(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``fleet-delivery``: acknowledged N of M over the last week, the time to a first turn,
    and a warning naming the agents whose message was never taken."""
    assert diagnostics._check_fleet_delivery() == [], "silent with nothing to count"
    taken = fleet_service.spawn(project, "coder", worktree=False, prompt="go").agent
    lost = fleet_service.spawn(project, "coder", worktree=False, prompt="go").agent
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.mark_prompted("none", now, agent_id=taken.id)
        store.mark_first_prompt_unsent(lost.id, now)

    [row] = diagnostics._check_fleet_delivery()
    assert row.name == "fleet-delivery" and row.status is CheckStatus.warn
    assert "acknowledged 1 of 2 first messages" in row.detail
    assert f"1 never taken ({lost.label})" in row.detail and row.fix


def test_fleet_spawn_json_says_how_the_first_message_went(
    runner: CliRunner, tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    result = runner.invoke(
        app, ["--json", "fleet", "spawn", "coder", "--no-worktree", "--prompt", "go", "-P",
              project.id]
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert (payload["prompt_typed"], payload["prompt_delivery"]) == (True, "argv")
    assert payload["prompt_acked"] is False, "nothing acknowledged it within the wait"
    assert sys.platform  # the wait ran on the suite's fake clock
