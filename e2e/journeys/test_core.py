"""The core journeys: a fresh machine → aisquare installed → a fleet up → talking to an agent.

They share ONE terminal window, as a person would: J01 types the one-liner in it and
answers the installer's "Open the fleet UI now?" with Enter, J02 goes through Welcome
in that same asq, J04 clicks into an agent's pane. A journey whose predecessor failed
is reported as skipped — the state it needs is not there — rather than as a second
failure that would only repeat the first.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e.conftest import json_or_none, track
from e2e.runner.machine import Machine
from e2e.runner.recorder import Recorder
from e2e.runner.server import StaticServer
from e2e.runner.terminal import TerminalSession
from playwright.sync_api import Browser

README_ONE_LINER = (
    "curl -fsSL https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/install.sh | sh"
)
HANDOFF = re.compile(r"Open the aisquare fleet UI now\? \[Y/n\]")
WELCOME = re.compile(r"Three steps to your first fleet")
SIDEBAR = (0, 31)
"""The sidebar's columns at 160 wide: agent rows are looked up (and clicked) only there."""

J01 = (
    "J01",
    "Install with the README one-liner",
    "On a fresh machine I paste the one-line installer; it installs aisquare and offers "
    "to open the fleet UI, and I say yes.",
)
J02 = (
    "J02",
    'Welcome: from a project to "Your fleet is up"',
    "In the fleet UI's first-run page I use the folder I'm in, connect Claude Code, and "
    "start a manager and two coders.",
)
J04 = (
    "J04",
    "Talk to an agent",
    "I click a coder in the sidebar, type to it in its pane, then F12 and w take me back "
    "to Welcome.",
)


@pytest.fixture(scope="module")
def repo(machine: Machine) -> Path:
    return machine.seed_repo("acme-api")


@pytest.fixture(scope="module")
def term(
    browser: Browser,
    machine: Machine,
    server: StaticServer,
    repo: Path,
    run_dir: Path,
    recorder: Recorder,
) -> Iterator[TerminalSession]:
    session = TerminalSession(
        browser,
        machine,
        server,
        cwd=repo,
        title="qa@e2e-machine: ~/acme-api",
        video_dir=run_dir / "video" / "raw",
        log_dir=run_dir / "logs",
    )
    yield session
    video = session.close()
    if video and video.is_file():
        target = run_dir / "video" / "core.webm"
        video.replace(target)
        for journey in recorder.journeys:
            if journey.id in {"J01", "J02", "J04"}:
                journey.video = str(target.relative_to(run_dir))


def _passed(recorder: Recorder, jid: str) -> bool:
    return any(j.id == jid and j.status == "passed" for j in recorder.journeys)


def _require(recorder: Recorder, needed: str, journey: tuple[str, str, str]) -> None:
    if not _passed(recorder, needed):
        why = f"{needed} did not pass, so the state this journey starts from is not there"
        recorder.skipped(*journey, why=why)
        pytest.skip(why)


def test_j01_install(
    term: TerminalSession,
    machine: Machine,
    server: StaticServer,
    wheel: Path | None,
    recorder: Recorder,
) -> None:
    if track() == "release":
        one_liner = README_ONE_LINER
    else:
        assert wheel is not None
        # The README one-liner, pointed at this tree: install.sh and the wheel from the runner.
        one_liner = (
            f"curl -fsSL {server.url('/install.sh')} | "
            f"AISQUARE_INSTALL_PACKAGE={server.url('/dist/' + wheel.name)} sh"
        )
    with recorder.journey(*J01, terminal=term) as j:
        with j.step("S01", "A fresh machine", slug="fresh-machine") as s:
            term.run("command -v aisquare asq || echo 'aisquare: not installed yet'")
            s.check(
                "aisquare is not on this machine yet", "not installed yet" in term.screen().text
            )
        with j.step("S02", "Paste the one-liner and let it run", slug="installer-done") as s:
            term.type(one_liner)
            term.press("Enter")
            screen = term.wait_for(HANDOFF, timeout=900, stable_ms=800)
            s.check("installer reached its last question", HANDOFF.search(screen.text) is not None)
            s.check("installer printed no error line", "error " not in screen.text.lower())
            code, version = machine.probe("aisquare --version")
            s.check(
                "aisquare is installed (checked from a second window)",
                code == 0 and version.startswith("aisquare "),
                version.strip(),
            )
        with j.step("S03", "Say yes: open the fleet UI", slug="welcome") as s:
            term.press("Enter")
            screen = term.wait_for(WELCOME, timeout=90)
            s.check("asq took over the same terminal", screen.alt)
            s.check(
                "the installer registered the folder I'm standing in", "✓ acme-api" in screen.text
            )


def test_j02_welcome(
    term: TerminalSession, machine: Machine, repo: Path, recorder: Recorder
) -> None:
    _require(recorder, "J01", J02)
    with recorder.journey(*J02, terminal=term) as j:
        with j.step("S01", "My project is already picked", slug="project") as s:
            # The installer ran from inside acme-api and registered it (install.sh
            # choose_project), so Welcome opens with step 1 done; "Choose another" is
            # the way out for someone who wanted a different folder.
            screen = term.wait_for(r"Choose another", timeout=30)
            s.check("step 1 done: acme-api is the project", "✓ acme-api" in screen.text)
            s.check(
                "Welcome offers a way to choose another folder", "Choose another" in screen.text
            )
        with j.step("S02", "Claude Code connected", slug="claude-connected") as s:
            screen = term.screen()
            if "connected — the manager gets its instructions" not in screen.text:
                term.press_until_focused("Connect")
                term.press("Enter")
            screen = term.wait_for(r"connected — the manager gets its instructions", timeout=60)
            s.check("aisquare's hooks are in Claude Code", True)
            s.check("Welcome found the claude on PATH", "Claude Code" in screen.text)
        with j.step("S03", "Start the manager", slug="manager-started") as s:
            term.press_until_focused("Start manager")
            term.press("Enter")
            screen = term.wait_for(r"manager — started", timeout=90)
            s.check(
                "the manager appears in the sidebar",
                screen.find(r"manager", cols=SIDEBAR) is not None,
            )
        with j.step("S04", "Start the coders", slug="fleet-up") as s:
            term.press_until_focused("Start the coders")
            term.press("Enter")
            screen = term.wait_for(r"Your fleet is up", timeout=120)
            for label in ("manager", "coder-1", "coder-2"):
                s.check(f"{label} is in the sidebar", screen.find(label, cols=SIDEBAR) is not None)
            _code, out = machine.probe("aisquare --json fleet ls", cwd=repo)
            data = json_or_none(out)
            agents = (data or {}).get("agents", []) if isinstance(data, dict) else []
            labels = sorted(a.get("agent", {}).get("label", "") for a in agents)
            s.check(
                "aisquare fleet ls lists the three agents",
                labels == ["coder-1", "coder-2", "manager"],
                ", ".join(labels) or out.strip()[:200],
            )
            for coder in ("coder-1", "coder-2"):
                s.check(
                    f"{coder} works in its own git worktree",
                    (repo / ".aisquare-worktrees" / coder / ".git").exists(),
                )


def test_j04_agent_pane(term: TerminalSession, recorder: Recorder) -> None:
    _require(recorder, "J02", J04)
    words = "hello from the nightly e2e"
    with recorder.journey(*J04, terminal=term) as j:
        with j.step("S01", "Click coder-1 in the sidebar", slug="open-coder") as s:
            term.click_text(r"coder-1", cols=SIDEBAR)
            screen = term.wait_for(r"e2e stand-in agent: coder-1", timeout=45)
            s.check("coder-1's pane is on screen", True)
        with j.step("S02", "Type to the agent", slug="typed") as s:
            # Click inside the pane to give it the keyboard, as a person would.
            term.click_cell(100, 30)
            term.type(words)
            term.press("Enter")
            screen = term.wait_for(re.escape(words), timeout=20)
            # The terminal echoes the line, and the agent (cat) prints it back.
            screen = term.settle()
            s.check(
                "the keystrokes reached the agent and came back",
                screen.text.count(words) >= 2,
                f"seen {screen.text.count(words)} times",
            )
        with j.step("S03", "F12, then w: back to Welcome", slug="back-to-welcome") as s:
            term.press("F12")
            term.press("w")
            screen = term.wait_for(r"Your fleet is up", timeout=30)
            s.check("Welcome is back and still says the fleet is up", True)


J06 = (
    "J06",
    "Doctor and Accounts pages",
    "From the sidebar I open the Doctor to see the machine is healthy, then Accounts to see "
    "which Claude logins aisquare knows, and w takes me back to Welcome.",
)


def test_j06_pages(term: TerminalSession, recorder: Recorder) -> None:
    _require(recorder, "J02", J06)
    with recorder.journey(*J06, terminal=term) as j:
        with j.step("S01", "Open the Doctor from the sidebar", slug="doctor") as s:
            term.click_text(r"^ ?Doctor\b", cols=SIDEBAR)
            screen = term.wait_for(r"claude-code", timeout=60)
            s.check("the doctor lists the Claude Code connection", "claude-code" in screen.text)
            s.check("the sidebar's doctor summary shows no failures", "✗ 0" in screen.text)
        with j.step("S02", "Open Accounts from the sidebar", slug="accounts") as s:
            term.click_text(r"^ ?Accounts\b", cols=SIDEBAR)
            screen = term.wait_for(r"Add Claude account", timeout=30)
            s.check("Accounts offers to add a Claude account", True)
            s.check("Accounts shows the AISquare sign-in section", "AISquare" in screen.text)
        with j.step("S03", "w: back to Welcome", slug="back-to-welcome") as s:
            term.press("w")
            term.wait_for(r"Your fleet is up", timeout=30)
            s.check("Welcome is back", True)
