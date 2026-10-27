"""SCOPE #113 091, item 4: Usage & stats sweeps Codex's rollouts, as it sweeps Claude Code's.

The sweep (``aisquare metrics backfill``, the dashboard's tick) reads every session of every
agent with account slots, where its agent keeps them (``AgentAccounts.transcript_roots``:
Codex's ``sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl`` in each home), cut into turns by
its own reader (``AgentTranscript.turns``), each row with the slot of the home it was found in
and the repository its rollout's ``session_meta`` names. A Codex session aisquare launched is
recorded at its Stop as well: the two share the session's row (the board's id for it,
``agent_events.session_key``) and its scan state, so neither counts what the other did. The
rollout is Codex 0.164.0's own (``tests/fixtures/agent_transcripts/codex-usage-0.164.0.jsonl``:
``codex exec "first turn hello"`` then ``codex exec resume <id> "second turn"``). Claude Code's
sweep is ``tests/test_usage_stats.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.store import store_session
from aisquare.models import SessionUsage, UsageStats
from aisquare.services import usage_stats
from aisquare.services.agent_events import session_key
from tests.projects import add_project
from tests.usage_stats_support import Machine

RECORDED = Path(__file__).parent / "fixtures" / "agent_transcripts" / "codex-usage-0.164.0.jsonl"
LINES = RECORDED.read_text(encoding="utf-8").splitlines()
THREAD = "01a16fa4-7678-7aa3-865e-27b8cee6e716"
"""The recorded session's thread id."""
OTHER = "01a16fa4-0000-7000-8000-000000000002"
FIRST_TURN = 18
"""Its lines to the first turn's ``task_complete``: what a Stop of the first turn reads."""
CODEX = get_adapter("codex")


def _moved(value: Any, cwd: Path, thread: str) -> Any:
    """``value`` with the recorded session's folder and thread id made ``cwd`` and ``thread``."""
    if isinstance(value, str):
        return value.replace("/work/alpha", str(cwd)).replace(THREAD, thread)
    if isinstance(value, list):
        return [_moved(item, cwd, thread) for item in value]
    if isinstance(value, dict):
        return {key: _moved(item, cwd, thread) for key, item in value.items()}
    return value


def _rollout(home: Path, cwd: Path, thread: str, *, lines: int = len(LINES)) -> Path:
    """The recorded session as Codex writes it in ``home``, run in ``cwd`` as ``thread``: its
    first ``lines`` lines (appended to what is there)."""
    path = home / "sessions" / "2026" / "10" / "24" / f"rollout-2026-10-24T16-40-11-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    have = path.read_text(encoding="utf-8").count("\n") if path.exists() else 0
    with path.open("a", encoding="utf-8") as handle:
        for line in LINES[have:lines]:
            handle.write(json.dumps(_moved(json.loads(line), cwd, thread)) + "\n")
    return path


def _tokens(rollout: Path) -> int:
    """What Codex's reader counts of the whole rollout, read once."""
    reader = CODEX.transcript_reader
    assert reader is not None
    scan = reader.usage(rollout, None)
    assert scan is not None
    return scan.delta.totals().total


def _row(home: Path, thread: str) -> SessionUsage:
    with store_session() as store:
        row = store.session_usage(session_key(CODEX, home, thread))
    assert row is not None, "the session is recorded under the board's id for it"
    return row


def _stop(home: Path, rollout: Path, cwd: Path, turn: str) -> None:
    """A Codex Stop in ``home``, as its hook hands it over."""
    payload = {
        "session_id": THREAD,
        "cwd": str(cwd),
        "transcript_path": str(rollout),
        "hook_event_name": "Stop",
        "turn_id": turn,
    }
    done = CliRunner().invoke(
        app, ["hook", "codex", "--config-dir", str(home)], input=json.dumps(payload)
    )
    assert done.exit_code == 0, done.output


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch ``CODEX_HOME``: Codex's plain slot, as a shell that set it runs Codex."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_backfill_records_every_codex_home_with_its_slot_and_repository(
    machine: Machine, codex_home: Path
) -> None:
    """The SCOPE's acceptance: ``metrics backfill`` over a scratch ``CODEX_HOME/sessions`` and a
    managed Codex slot records both sessions, a row per turn (each prompt of the human's opens
    one), with the slot of the home each was found in and the repository its rollout names;
    ``metrics stats`` shows them. Before 091's lane the sweep read Claude Code's transcripts
    alone, and no hook had watched these."""
    managed = core.create_account("codex")
    alpha, beta = machine.repo("alpha"), machine.repo("beta")
    first = _rollout(codex_home, alpha, THREAD)
    second = _rollout(managed.config_dir, beta, OTHER)
    runner = CliRunner()

    backfill = runner.invoke(app, ["--json", "metrics", "backfill"])
    as_json = runner.invoke(
        app, ["--json", "metrics", "stats", "--range", "lifetime", "--no-sweep"]
    )
    as_text = runner.invoke(app, ["metrics", "stats", "--range", "lifetime", "--no-sweep"])

    report = json.loads(backfill.output)
    assert (report["transcripts"], report["sessions_new"], report["turns"]) == (2, 2, 4), report
    plain_slot = core.kind("codex").plain_slot
    assert (_row(codex_home, THREAD).account_slot, _row(codex_home, THREAD).repo_id) == (
        plain_slot,
        usage_stats.repo_of(alpha)[0],  # type: ignore[index]
    )
    assert (
        _row(managed.config_dir, OTHER).account_slot,
        _row(managed.config_dir, OTHER).repo_id,
    ) == (
        managed.slot,
        usage_stats.repo_of(beta)[0],  # type: ignore[index]
    )
    stats = UsageStats.model_validate(json.loads(as_json.output))
    assert (stats.sessions, stats.turns) == (2, 4)
    assert stats.tokens.total == _tokens(first) + _tokens(second)
    assert {row.slot for row in stats.accounts.rows} == {plain_slot, managed.slot}
    assert {row.name for row in stats.repos.rows} == {"alpha", "beta"}
    assert "alpha" in as_text.output and "beta" in as_text.output


def test_a_codex_session_its_stop_recorded_is_read_on_by_the_sweep_not_again(
    machine: Machine, codex_home: Path
) -> None:
    """The Stop of the first turn records it, on its home's slot and its repository (a hook's
    row carried neither before); the sweep then reads the second turn alone: the session's
    tokens once, in two turns."""
    alpha = machine.repo("alpha")
    add_project(alpha)
    rollout = _rollout(codex_home, alpha, THREAD, lines=FIRST_TURN)
    _stop(codex_home, rollout, alpha, "turn-1")
    hooked = _row(codex_home, THREAD)
    assert hooked.turns == 1
    assert (hooked.account_slot, hooked.repo_id) == (
        core.kind("codex").plain_slot,
        usage_stats.repo_of(alpha)[0],  # type: ignore[index]
    )

    _rollout(codex_home, alpha, THREAD)
    report = usage_stats.sweep()

    assert (report.sessions_new, report.sessions_updated, report.turns) == (0, 1, 1)
    swept = _row(codex_home, THREAD)
    assert swept.turns == 2 and swept.total_tokens == _tokens(rollout)


def test_a_codex_session_the_sweep_recorded_is_not_counted_again_at_its_stop(
    machine: Machine, codex_home: Path
) -> None:
    """The other way round: swept first, the session's Stop finds nothing new to count."""
    alpha = machine.repo("alpha")
    add_project(alpha)
    rollout = _rollout(codex_home, alpha, THREAD)
    assert usage_stats.sweep().turns == 2

    _stop(codex_home, rollout, alpha, "turn-2")
    usage_stats.sweep()

    row = _row(codex_home, THREAD)
    assert row.turns == 2 and row.total_tokens == _tokens(rollout)


def test_a_rollout_names_the_folder_its_session_ran_in() -> None:
    """``AgentTranscript.session_cwd``: a rollout's ``session_meta``, read off its head."""
    reader = CODEX.transcript_reader
    assert reader is not None
    assert reader.session_cwd(RECORDED) == Path("/work/alpha")
    assert reader.session_cwd(RECORDED.with_name("missing.jsonl")) is None
