"""SCOPE #113 091, item 4: Usage & stats sweeps Codex's rollouts, as it sweeps Claude Code's.

The sweep (``aisquare metrics backfill``, the dashboard's tick) reads every session of every
agent with account slots, as its agent's reader lists them in each home
(``AgentTranscript.conversations``: Codex's ``sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl``),
cut into turns by that reader (``AgentTranscript.turns``), each row with the slot of the home
it was found in (``AgentAccounts.transcript_roots``) and the repository its rollout's
``session_meta`` names (``AgentTranscript.opening``). A Codex session aisquare launched is
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
from tests.test_codex_accounts import _sign_in
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


def test_codex_home_redirected_the_plain_codex_folder_is_swept_too(
    machine: Machine, codex_home: Path
) -> None:
    """With ``CODEX_HOME`` pointing elsewhere, the ``~/.codex`` a plain ``codex`` reads with it
    unset is the plain slot's history too, as ``~/.claude`` is Claude Code's."""
    alpha = machine.repo("alpha")
    unset = core.plain_home("codex", {})
    assert unset != codex_home
    _rollout(unset, alpha, OTHER)

    assert usage_stats.sweep().sessions_new == 1
    assert _row(unset, OTHER).account_slot == core.kind("codex").plain_slot


def test_a_codex_accounts_plan_is_its_subscription_in_the_stats(
    machine: Machine, codex_home: Path
) -> None:
    """A Codex account's type is the plan its login names (``AgentAccounts.subscription``),
    never ``unknown`` for want of Claude Code's credentials."""
    managed = core.create_account("codex")
    _sign_in(managed.config_dir, "pro@example.com", plan="pro")
    _rollout(managed.config_dir, machine.repo("alpha"), OTHER)

    usage_stats.sweep()

    [row] = [r for r in usage_stats.stats("lifetime").accounts.rows if r.slot == managed.slot]
    assert row.subscription == "pro"


def test_a_codex_turn_across_local_midnight_is_two_turns(tmp_path: Path) -> None:
    """``AgentTranscript.turns`` cuts where the local day changes as well as at a prompt: the
    0.164.0 TUI rollout's second turn (an exec, then its answer: two requests) charged to two
    days when midnight falls between them."""
    reader = CODEX.transcript_reader
    assert reader is not None
    rollout = RECORDED.with_name("codex-0.164.0.jsonl")
    one_day = reader.turns(rollout, None, lambda stamp: "today")
    split = reader.turns(rollout, None, lambda s: "a" if s < "2026-10-24T04:00:21" else "b")
    assert one_day is not None and split is not None
    assert len(split.turns) == len(one_day.turns) + 1
    assert split.scan.delta.totals().total == one_day.scan.delta.totals().total


def test_every_rollout_codex_lists_names_the_slot_of_its_home(
    machine: Machine, codex_home: Path
) -> None:
    """What the sweep lists of a home (``AgentTranscript.conversations``) is under that home's
    ``AgentAccounts.transcript_roots``, by which a Stop's first record names its slot too: the
    two agree. A rollout outside every home names none."""
    reader = CODEX.transcript_reader
    assert reader is not None
    rollout = _rollout(codex_home, machine.repo("alpha"), THREAD)
    roots = core.kind("codex").transcript_roots(codex_home)

    listed = [path for _, path in reader.conversations(codex_home)]

    assert listed == [rollout]
    assert all(any(path.is_relative_to(root) for root in roots) for path in listed)
    assert usage_stats.account_slot(rollout) == core.kind("codex").plain_slot
    assert usage_stats.account_slot(machine.root / "elsewhere" / rollout.name) is None
