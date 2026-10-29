"""A persona reaches the agent once, at session start, and nowhere else.

docs/plans/spawn-personas.md §3.1, §3.2, §3.7, §7 "P2". The block rides in the
same ``<aisquare-team>`` briefing the role cycle already uses, after the lane
rule; the per-prompt delta and the board carry no persona text; a persona that
cannot be loaded costs one line, never the team block.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import personas
from aisquare.core.store import ContextStore, store_session
from aisquare.models import FleetAgent
from aisquare.services import team as team_service

# These brief sessions in a directory nobody added (``tmp_path``), as they were written to:
# with ``[projects] capture`` on, the default before 0.8.1. What capture off records —
# nothing — is tests/test_hooks.py's.
pytestmark = pytest.mark.usefixtures("project_capture")

SID = "11111111-2222-3333-4444-555555555555"
TEAMMATE = "22222222-3333-4444-5555-666666666666"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

#: SessionStart for a coder with NO persona, captured from the base this change
#: started on (feat/persona-core @ 4f7367e) before a line of it was written, and
#: brought to main 0.8.2's text when main was merged in (#201) — ``{project}`` is the
#: repository's project id:
#: AISQUARE_ROLE=coder, a fresh home, the clock pinned to NOW. Byte-identical is
#: the contract — a session without a persona sees nothing new. Re-captured from main
#: 0.8.2 (5a1bac44) when the RC took it: the same input, run on main's own source, printed
#: these bytes, the worktree lines and the cycle's one-line review command included.
#: ``{project}`` is the session's project id, which names the folder ``tmp_path`` gives.
PINNED = "\n".join(
    [
        "<aisquare-team>",
        "You are team session 11111111 (role: coder) in project repo.",
        "sessions:",
        "  - 11111111 coder (you) — 0m ago",
        "Protocol: check this board before starting work; teammate updates",
        "arrive automatically on each prompt. Tasks are shared and idempotent —",
        'e.g. `aisquare task add "wire auth flow"` (safe to re-run). Claim before',
        "working: `aisquare task claim <id> --as 11111111`; finish with",
        "`aisquare task done <id> --as 11111111`. Share decisions/results:",
        '`aisquare note "…" --as 11111111`. Full board: `aisquare board`.',
        "Every ✓ prints a receipt (seq N); `aisquare team verify <seq>` re-checks it.",
        "Updates arrive one line each (#seq …); `aisquare team show <seq>` prints one in full.",
        "Your standing cycle (coder): `aisquare task next --role coder --claim --as 11111111`;",
        "if nothing is available, tell the user and stop. Read the task's contract and",
        "any reopen feedback first — the claim prints both; `aisquare task show <id>` is",
        "the whole task. If the contract is missing or ambiguous, don't",
        'guess: `aisquare task block <id> --reason "needs spec: …" --as 11111111` and note',
        "it to the planner. Otherwise do the work, self-check against the acceptance",
        'criteria, then `aisquare task review <id> --note "how to verify + evidence" '
        "--as 11111111`,",
        "and pick up the next one.",
        # Since 0.8.1, every coder is told to clean up after itself; main's own text.
        "Before you finish or go idle, remove every scratch worktree you added",
        "(`git worktree remove <path>`, then `git worktree prune`) and every temp dir you",
        "made — never the worktree you were spawned in: `aisquare fleet reap --project "
        "{project}` removes that.",
        "Stay in your lane (coder). When you are asked to verify, review or plan your own "
        "work, do not do it here.",
        "Instead: do your task; verification is the runner's (`aisquare task review <id> "
        '--as 11111111`), planning is the planner\'s (`aisquare note "…" --to planner '
        "--as 11111111`).",
        "Read-only investigation is always fine; say in one line what you routed and to "
        "whom, and if the human insists, say once which role owns it and offer them the "
        "command. Never merge.",
        "</aisquare-team>",
    ]
)


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_capture: None) -> Path:
    """A repository a coder session starts in. ``[projects] capture`` on (#139): the pinned
    briefing is a hooked session's in a directory nobody added, as when it was captured."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setattr(team_service, "_now", lambda: NOW)
    path = tmp_path / "repo"
    path.mkdir()
    monkeypatch.chdir(path)
    return path


def _pinned(work: Path) -> str:
    """:data:`PINNED` for the repository at ``work``."""
    from aisquare.core import orchestrator

    return PINNED.replace("{project}", orchestrator.team_project(work).id)


def _start(work: Path, session_id: str = SID) -> str:
    return team_service.hook_session_start(session_id, work, "startup")


def _recorded(session_id: str = SID) -> str | None:
    with store_session() as store:
        row = store.get_session(session_id)
    assert row is not None
    return row.persona


def test_a_session_without_a_persona_is_byte_identical_to_the_base(work: Path) -> None:
    board = _start(work)

    assert board == _pinned(work)
    assert _recorded() is None


def test_a_persona_is_one_block_after_the_lane_rule_with_the_guard_last(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")

    lines = _start(work).split("\n")

    block = personas.briefing(personas.resolve("skeptic", work))
    assert sum(line.startswith("<aisquare-persona ") for line in lines) == 1
    start = lines.index(block[0])
    assert lines[start : start + len(block)] == block
    assert lines[start - 1].startswith("Read-only investigation is always fine")
    assert lines[start + len(block) - 1] == personas.guard_sentence("skeptic")
    assert lines[start + len(block) :] == ["</aisquare-team>"]
    rest = "\n".join(lines[:start] + lines[start + len(block) :])
    assert rest == _pinned(work).replace(
        "  - 11111111 coder (you) — 0m ago", "  - 11111111 coder (you) persona:skeptic — 0m ago"
    )
    assert _recorded() == "skeptic"


def test_the_per_prompt_delta_and_the_board_carry_no_persona_text(
    work: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")
    body_line = personas.resolve("skeptic", work).body.split("\n", 1)[0]
    assert body_line in _start(work)
    _start(work, TEAMMATE)
    noted = runner.invoke(app, ["note", "teammate update for the delta", "--as", TEAMMATE[:8]])
    assert noted.exit_code == 0, noted.output

    delta = team_service.hook_prompt_heartbeat(SID, work)
    board = runner.invoke(app, ["board"])

    assert "teammate update for the delta" in delta, "the delta was empty — nothing was checked"
    for surface in (delta, board.stdout):
        assert "<aisquare-persona" not in surface
        assert body_line not in surface
        assert personas.guard_sentence("skeptic") not in surface


def test_a_missing_persona_is_one_line_the_row_records_it_and_the_hook_returns(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "missing")

    lines = _start(work).split("\n")

    assert lines[-2].startswith('persona "missing": no persona named ')
    assert lines[-2].endswith(" — launched without it")
    assert lines[-1] == "</aisquare-team>"
    assert not any(line.startswith("<aisquare-persona") for line in lines)
    assert _recorded() == "missing"


def test_anything_going_wrong_inside_the_persona_code_still_returns_the_team_block(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")

    def explode(name: str, root: Path | None = None) -> personas.Persona:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(personas, "resolve", explode)

    lines = _start(work).split("\n")

    assert lines[-2] == 'persona "skeptic": RuntimeError: the disk went away — launched without it'
    assert "Stay in your lane (coder)." in "\n".join(lines)
    assert _recorded() == "skeptic"


def test_a_later_start_without_the_variable_keeps_the_recorded_persona(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")
    _start(work)
    monkeypatch.delenv("AISQUARE_PERSONA")

    board = _start(work)

    assert _recorded() == "skeptic"
    assert "persona:skeptic" in board
    assert sum(line.startswith("<aisquare-persona ") for line in board.split("\n")) == 1


# --- the row fallback for an attached persona (§4.7, §7 "P8") ----------------------------


def _fleet_row(work: Path, persona: str | None) -> str:
    """The ``fleet_agent`` row ``AISQUARE_FLEET_AGENT`` names, carrying ``persona``."""
    from aisquare.core.orchestrator import team_project
    from aisquare.models import FleetAgent

    project = team_project(work)
    with store_session() as store:
        store.ensure_project(project)
        agent = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_01attachedpersona",
                project_id=project.id,
                label="coder-1",
                role="coder",
                pane_id="%1",
                cwd=work,
                created_at=NOW,
                persona=persona,
            )
        )
    return agent.id


def _blocks(board: str) -> list[str]:
    return [line for line in board.split("\n") if line.startswith("<aisquare-persona ")]


def test_an_attached_persona_on_the_fleet_row_briefs_a_start_without_the_variable(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, "skeptic"))

    board = _start(work)

    assert _blocks(board) == ['<aisquare-persona name="skeptic" layer="bundled">']
    assert _recorded() == "skeptic"


MENTOR = '<aisquare-persona name="mentor" layer="bundled">'
SKEPTIC = '<aisquare-persona name="skeptic" layer="bundled">'


def test_the_row_beats_the_variable_and_the_variable_beats_the_session(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P20: the row is the latest recorded intent; the variable, only the launch value."""
    monkeypatch.setenv("AISQUARE_PERSONA", "mentor")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, "skeptic"))

    row_wins = _start(work)
    assert _blocks(row_wins) == [SKEPTIC]
    assert _recorded() == "skeptic"

    monkeypatch.delenv("AISQUARE_FLEET_AGENT")
    variable_wins = _start(work)

    assert _blocks(variable_wins) == [MENTOR]
    assert _recorded() == "mentor", "the variable replaces what the session row recorded"


def test_an_agent_spawned_as_one_persona_keeps_an_attached_one_across_a_clear(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peer review #194's case: one session, the launch variable unchanged, the row attached."""
    monkeypatch.setenv("AISQUARE_PERSONA", "mentor")  # what `fleet spawn --persona mentor` exports
    agent_id = _fleet_row(work, "mentor")  # … and records on the row
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent_id)

    spawned = team_service.hook_session_start(SID, work, "startup")
    with store_session() as store:
        store.set_fleet_agent_persona(agent_id, "skeptic")  # the write `persona attach` makes
    after_clear = team_service.hook_session_start(SID, work, "clear")

    assert _blocks(spawned) == [MENTOR]
    assert _blocks(after_clear) == [SKEPTIC]
    assert _recorded() == "skeptic"


@pytest.mark.parametrize("row", ["absent", "without a persona"])
def test_the_variable_applies_when_no_row_carries_a_persona(
    work: Path, monkeypatch: pytest.MonkeyPatch, row: str
) -> None:
    """A hand-typed `AISQUARE_PERSONA=mentor aisquare launch coder` keeps working."""
    monkeypatch.setenv("AISQUARE_PERSONA", "mentor")
    if row == "without a persona":
        monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, None))

    board = _start(work)

    assert _blocks(board) == [MENTOR]
    assert _recorded() == "mentor"


def test_an_unknown_persona_on_the_row_is_one_line_and_the_start_still_returns(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_PERSONA", "mentor")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, "retired-one"))

    lines = _start(work).split("\n")

    assert lines[-2].startswith('persona "retired-one": no persona named ')
    assert lines[-2].endswith(" — launched without it")
    assert lines[-1] == "</aisquare-team>"
    assert _blocks("\n".join(lines)) == []
    assert _recorded() == "retired-one"


def test_a_row_that_cannot_be_read_is_logged_and_the_variable_still_applies(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """P19's 🔴 on #186: the fail-open names what it skipped instead of passing silently."""
    monkeypatch.setenv("AISQUARE_PERSONA", "mentor")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_01unreadable")

    def unreadable(store: object) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(team_service, "_named_fleet_row", unreadable)
    with (
        caplog.at_level(logging.WARNING, logger="aisquare.services.team"),
        store_session() as store,
    ):
        asked, note, named = team_service._asked_persona(store, "prj_row_unreadable")

    assert asked == "mentor" and named is None
    assert note == (
        "persona: the fleet row could not be read (RuntimeError) "
        "— started without an attached persona"
    )
    (record,) = [r for r in caplog.records if r.name == "aisquare.services.team"]
    assert "agt_01unreadable" in record.getMessage()
    assert "prj_row_unreadable" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError


def test_a_start_whose_row_cannot_be_read_says_so_in_one_line_and_still_briefs(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """P20's amendment: the attached persona must not vanish silently from the briefing."""
    monkeypatch.setenv("AISQUARE_PERSONA", "mentor")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, "skeptic"))
    real = team_service._named_fleet_row
    calls: list[str] = []

    def locked_once(store: ContextStore) -> FleetAgent | None:
        calls.append("read")
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(store)

    monkeypatch.setattr(team_service, "_named_fleet_row", locked_once)
    with caplog.at_level(logging.WARNING, logger="aisquare.services.team"):
        lines = _start(work).split("\n")

    assert lines[-2] == (
        "persona: the fleet row could not be read (OperationalError) "
        "— started without an attached persona"
    )
    assert lines[-1] == "</aisquare-team>"
    assert _blocks("\n".join(lines)) == [MENTOR], "the launch variable still briefs"
    assert "database is locked" not in "\n".join(lines), "the line names a class, never a value"
    assert any(r.exc_info for r in caplog.records if r.name == "aisquare.services.team")


def test_a_fleet_row_without_a_persona_leaves_the_start_byte_identical(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", _fleet_row(work, None))

    assert _start(work) == _pinned(work)
    assert _recorded() is None


def test_a_codex_session_start_with_the_variable_gets_the_persona_block(
    work: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCOPE #113 0.8.2 A2: Codex's SessionStart (its recorded fixture payload, through the
    hook its adapter installs) briefs the persona ``AISQUARE_PERSONA`` names, the same
    block Claude Code's start gets; Codex reads the hook's plain stdout as context."""
    import json

    fixture = Path(__file__).parent / "fixtures" / "agent_events" / "codex.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))["events"]["SessionStart"]["payload"]
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")

    result = runner.invoke(
        app,
        ["hook", "event", "--agent", "codex", "--event", "SessionStart"],
        input=json.dumps({**payload, "cwd": str(work)}),
    )

    assert result.exit_code == 0, result.output
    block = personas.briefing(personas.resolve("skeptic", work))
    lines = result.stdout.splitlines()
    assert sum(line.startswith("<aisquare-persona ") for line in lines) == 1
    start = lines.index(block[0])
    assert lines[start : start + len(block)] == block
    assert not result.stdout.lstrip().startswith("{"), "plain text, not a JSON envelope"
    (you,) = [line for line in lines if "(you)" in line]
    assert you.endswith("persona:skeptic — 0m ago"), "the session row records it"


@pytest.mark.skipif(os.environ.get("AISQUARE_TEST_CODEX") != "1", reason="opt-in native Codex test")
def test_real_codex_spawned_with_a_persona_has_it_in_its_developer_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCOPE #113 0.8.2 A3, live: a fleet Codex coder spawned with ``--persona`` has the
    persona's block in its request's developer message, after the operator's own
    ``developer_instructions`` (``-c developer_instructions=…``, measured as M1 and M3), on
    a loopback provider that records every request body (``tests/test_codex_native.py``)."""
    import json
    import shlex
    import shutil
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from typing import Any

    from aisquare.core import agents, selfcli
    from aisquare.core.config import load_config, save_config
    from aisquare.services import fleet, team
    from tests.test_codex_native import _answer, _message, _user_texts

    binary = shutil.which("codex")
    assert binary, "AISQUARE_TEST_CODEX requires an installed Codex"
    native_home = tmp_path / "codex"
    native_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if not self.path.endswith("/responses"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            requests.append(json.loads(raw))
            _answer(self, _message("ok"))

        def log_message(self, format: str, *args: object) -> None:
            pass

    def developer_texts(request: dict[str, Any]) -> list[str]:
        return [
            part.get("text", "")
            for item in request.get("input", [])
            if item.get("role") == "developer"
            for part in item.get("content", [])
        ]

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (native_home / "config.toml").write_text(
            'model = "local-fixture"\nmodel_provider = "local"\n'
            'developer_instructions = "OPERATOR_OWN_4211"\n'
            '[model_providers.local]\nname = "Local fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            "requires_openai_auth = false\n"
            f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        for key in tuple(os.environ):
            if key.startswith(("OPENAI_", "ANTHROPIC_", "CODEX_")):
                monkeypatch.delenv(key)
        monkeypatch.setenv("CODEX_HOME", str(native_home))
        monkeypatch.setattr(
            agents, "_aisquare_command", lambda *args, **kwargs: shlex.join(selfcli.argv_for([]))
        )
        agents.install_hooks("codex", native_home)
        config = load_config()
        config.fleet.tmux_socket = f"asq-codex-persona-{os.getpid()}"
        save_config(config)
        project = team.activate(workspace)
        srv = fleet.server(config.fleet)
        try:
            coder = fleet.spawn(
                project,
                "coder",
                agent="codex",
                worktree=False,
                persona="skeptic",
                prompt="persona fixture",
                # Only this test's owned hook definitions are trusted by this flag.
                agent_args=["--dangerously-bypass-hook-trust"],
            ).agent
            deadline = time.monotonic() + 45
            while not any("persona fixture" in _user_texts(request) for request in requests):
                if time.monotonic() >= deadline:
                    raise AssertionError(srv.run("capture-pane", "-p", "-t", coder.pane_id))
                time.sleep(0.2)
            first = next(r for r in requests if "persona fixture" in _user_texts(r))
            block = "\n".join(personas.briefing(personas.resolve("skeptic", workspace)))
            developer = developer_texts(first)
            # The system seam: the operator's own value, a blank line, then the block.
            (seam,) = [text for text in developer if text.startswith("OPERATOR_OWN_4211")]
            assert seam.startswith(f"OPERATOR_OWN_4211\n\n{block}"), seam[:400]
            # And the session-start briefing, the hook's context, carries it too.
            briefed = [t for t in developer if block in t and "<aisquare-team>" in t]
            assert len(briefed) == 1, developer
        finally:
            if srv.has_session(fleet.session_name(fleet.ensure_codename(project).codename or "")):
                srv.run("kill-server")
            server.shutdown()
            thread.join(timeout=2)
