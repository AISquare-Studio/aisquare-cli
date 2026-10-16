"""The watchlist: known bugs from the 0.8.0 QA report, re-checked every night.

Each item types the QA report's own reproduction into a SECOND terminal window on the
same machine (the first one is busy running asq) and photographs the answer. A bug
that is still there is reported as **still open** — an expected failure (``xfail``),
never a red nightly. The night it stops reproducing, the item reports **FIXED ·
promote me**: the check should then move into a real journey as a gate.

Every item restores what it changed, so the order of items does not matter.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from e2e.runner.machine import Machine
from e2e.runner.recorder import JourneyRecorder, Recorder
from e2e.runner.server import StaticServer
from e2e.runner.terminal import TerminalSession
from playwright.sync_api import Browser


@pytest.fixture(scope="module")
def shell(
    browser: Browser,
    machine: Machine,
    server: StaticServer,
    repo: Path,
    run_dir: Path,
    recorder: Recorder,
) -> Iterator[TerminalSession]:
    if not any(j.id == "J01" and j.status == "passed" for j in recorder.journeys):
        pytest.skip("J01 did not install aisquare, so there is nothing to re-check")
    session = TerminalSession(
        browser,
        machine,
        server,
        cwd=repo,
        title="qa@e2e-machine: ~/acme-api — second window",
        cols=120,
        rows=30,
        video_dir=run_dir / "video" / "raw",
        log_dir=run_dir / "logs",
    )
    yield session
    video = session.close()
    if video and video.is_file():
        target = run_dir / "video" / "watchlist.webm"
        video.replace(target)
        for journey in recorder.journeys:
            if journey.kind == "watch":
                journey.video = str(target.relative_to(run_dir))


@contextmanager
def watch(
    recorder: Recorder, shell: TerminalSession, finding: str, title: str, story: str
) -> Iterator[JourneyRecorder]:
    """A watch item: still open ⇒ xfail (reported, never red); fixed ⇒ passes."""
    try:
        with recorder.journey(
            f"W-{finding}", title, story, terminal=shell, kind="watch", finding=finding
        ) as journey:
            yield journey
    except AssertionError as exc:
        pytest.xfail(f"{finding} still open — {str(exc).splitlines()[0]}")


def _last_line(shell: TerminalSession) -> str:
    lines = [line.strip() for line in shell.screen().lines if line.strip()]
    # The prompt is the last line; the command's last output line is the one before it.
    return lines[-2] if len(lines) >= 2 else ""


def test_qa01_forget_does_not_pin(
    shell: TerminalSession, machine: Machine, recorder: Recorder
) -> None:
    story = (
        "QA-01: forgetting a project from inside its own folder must not write a machine-wide "
        "pin (~/.aisquare/state.json) that re-routes every other session."
    )
    try:
        with (
            watch(
                recorder, shell, "QA-01", "`project forget` leaves no machine-wide pin", story
            ) as j,
            j.step("S01", "Register a project, then forget it from inside it", slug="forget") as s,
        ):
            shell.run(
                "clear && mkdir -p ~/beta && cd ~/beta && git init -q"
                " && git commit -q --allow-empty -m init"
                " && aisquare project onboard >/dev/null 2>&1"
                " && aisquare project forget beta"
                "; ls ~/.aisquare/state.json; cd ~/acme-api",
                timeout=180,
            )
            screen = shell.screen()
            s.check(
                "no ~/.aisquare/state.json was written",
                "No such file" in screen.text,
                "state.json exists: every session on the machine is now pinned",
            )
    finally:
        machine.probe("rm -f ~/.aisquare/state.json")


def test_qa13_task_keys_do_not_merge(shell: TerminalSession, recorder: Recorder) -> None:
    story = (
        "QA-13: two different tasks with non-Latin titles must be two tasks; an ASCII-only "
        "idempotency key makes the second one 'already tracked' instead."
    )
    with (
        watch(recorder, shell, "QA-13", "Different tasks stay different", story) as j,
        j.step("S01", "Add two different tasks with Cyrillic titles", slug="tasks") as s,
    ):
        shell.run(
            'clear && aisquare task add "Исправить вход" && aisquare task add "Добавить тесты"'
        )
        text = shell.screen().text
        s.check(
            "the second task was added, not merged into the first",
            text.count("✓ added") >= 2,
            "the second add answered 'already tracked'"
            if "already tracked" in text
            else text[-200:],
        )


def test_qa19_search_does_not_crash(shell: TerminalSession, recorder: Recorder) -> None:
    story = (
        "QA-19: searching remembered context for an everyday word (AND/OR/NOT) must not crash "
        "with a traceback."
    )
    with (
        watch(recorder, shell, "QA-19", "Context search survives the word AND", story) as j,
        j.step("S01", 'Search memory for "AND"', slug="search") as s,
    ):
        code = shell.run('clear && aisquare context search "AND"')
        s.check("the search exits cleanly", code == 0, f"exit {code}")


def test_qa09_capture_can_be_turned_off(
    shell: TerminalSession, machine: Machine, repo: Path, recorder: Recorder
) -> None:
    story = (
        "QA-09: with capture.enabled = false, a prompt sent through Claude Code's "
        "UserPromptSubmit hook must not be stored in the prompt log."
    )
    payload = json.dumps(
        {
            "session_id": "watch-qa-09",
            "transcript_path": "/dev/null",
            "cwd": str(repo),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "watchlist: do not record me",
        }
    )
    try:
        with (
            watch(recorder, shell, "QA-09", "Turning capture off stops capture", story) as j,
            j.step("S01", "Turn capture off, then send a prompt", slug="capture") as s,
        ):
            shell.run(
                "clear && aisquare config set capture.enabled false"
                f" && echo '{payload}' | aisquare hook user-prompt-submit >/dev/null"
                " && aisquare log | grep -c 'do not record me'"
            )
            stored = _last_line(shell)
            s.check(
                "the prompt was not recorded",
                stored == "0",
                f"`aisquare log` shows it {stored} time(s)",
            )
    finally:
        machine.probe("aisquare config set capture.enabled true >/dev/null 2>&1; true")
