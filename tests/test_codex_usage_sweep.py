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
import os
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core import paths
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.store import SCHEMA_VERSION, store_session
from aisquare.models import SessionUsage, UsageStats
from aisquare.services import transcript_facts, usage_share, usage_stats
from aisquare.services.agent_events import session_key
from tests.projects import add_project
from tests.test_codex_accounts import _sign_in
from tests.usage_stats_support import IST, MODEL, NOW, Machine, prompt, request

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


def _stop(home: Path, rollout: Path, cwd: Path, turn: str, thread: str = THREAD) -> None:
    """A Codex Stop in ``home``, as its hook hands it over."""
    payload = {
        "session_id": thread,
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
    assert hooked.turns == 1 and hooked.agent == "codex"
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


def test_a_codex_account_in_the_stats_is_called_by_its_alias_or_plain_codex(
    machine: Machine, codex_home: Path
) -> None:
    """SMOKE11 3, the tester's repro: ``accounts alias <slot> codex-b`` on a Codex slot, a Codex
    session on it and one on the plain codex, ``metrics stats``. Claude Code's rows read their
    alias or ``plain claude``; the Codex ones read ``account N`` and ``account 0``, in the table
    and in ``--json``, as the aliases were read from Claude Code's registry alone and the
    unaliased name was Claude Code's. Each agent's own registry and plain name now."""
    machine.account(1, "max")
    machine.account(2, "max")  # a Claude Code slot, made before the Codex one takes a number
    managed = core.create_account("codex")
    alpha = machine.repo("alpha")
    for slot, session in ((1, "plain-claude"), (2, "claude-a")):
        machine.session(
            slot,
            alpha,
            session,
            prompt(NOW - timedelta(minutes=5), cwd=alpha),
            request(NOW - timedelta(minutes=4), f"msg_{session}", cwd=alpha),
        )
    _rollout(codex_home, alpha, THREAD)
    _rollout(managed.config_dir, alpha, OTHER)
    runner = CliRunner()
    for slot, alias in ((2, "claude-a"), (managed.slot, "codex-b")):
        named = runner.invoke(app, ["accounts", "alias", str(slot), alias])
        assert named.exit_code == 0, named.output

    as_json = runner.invoke(app, ["--json", "metrics", "stats", "--range", "lifetime"])
    as_text = runner.invoke(app, ["metrics", "stats", "--range", "lifetime", "--no-sweep"])

    stats = UsageStats.model_validate(json.loads(as_json.output))
    assert {(row.slot, row.label) for row in stats.accounts.rows} == {
        (1, "plain claude"),
        (2, "claude-a"),
        (core.kind("codex").plain_slot, "plain codex"),
        (managed.slot, "codex-b"),
    }
    assert "plain codex" in as_text.output and "codex-b" in as_text.output, as_text.output
    assert re.search(r"account -?\d", as_text.output) is None, as_text.output


def test_the_plain_codex_is_called_plain_codex_with_its_home_gone(machine: Machine) -> None:
    """tester-46's T9: turns recorded on the plain codex's slot and ``~/.codex`` gone since
    (moved aside). The accounts list a plain home only while it exists, so the stats call the
    slot by its number's own name (``core.slot_name``), ``plain codex``, never Claude Code's
    unaliased one (``default_label``: ``account 0``)."""
    home = core.plain_home("codex")
    assert home == machine.home / ".codex", "a scratch home's, never the developer's"
    _rollout(home, machine.repo("alpha"), THREAD)
    assert usage_stats.sweep().sessions_new == 1
    home.rename(home.with_name(".codex-aside"))
    plain = core.kind("codex").plain_slot
    assert core.find_account(plain) is None, "the fixture"
    runner = CliRunner()

    as_json = runner.invoke(
        app, ["--json", "metrics", "stats", "--range", "lifetime", "--no-sweep"]
    )
    as_text = runner.invoke(app, ["metrics", "stats", "--range", "lifetime", "--no-sweep"])

    stats = UsageStats.model_validate(json.loads(as_json.output))
    assert [(row.slot, row.label) for row in stats.accounts.rows] == [(plain, "plain codex")]
    assert "plain codex" in as_text.output, as_text.output
    assert re.search(r"account -?\d", as_text.output) is None, as_text.output


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


def test_a_rollout_s_folder_is_the_one_its_session_meta_names(
    machine: Machine, codex_home: Path
) -> None:
    """Codex keeps its own opening (``AgentTranscript.opening``: its ``session_meta``'s
    ``cwd``), which no record of the rollout carries at its top level, where main's rule for a
    Claude Code transcript reads (review r28 of #113, n3)."""
    repo = machine.repo("alpha")
    rollout = _rollout(codex_home, repo, THREAD)

    assert usage_stats.session_cwd(rollout) == repo
    assert usage_stats._head_cwd(rollout) is None


# --- its reviewer's rollouts: a subagent's, counted in its session (SMOKE12 F2) --------------

GUARDED = RECORDED.with_name("codex-guardian-parent-0.166.0.jsonl")
"""Codex 0.166.0's own rollout (SMOKE12, sanitized): a fleet coder run with ``--approve-for-me``
on GPT-6.1-Sol, whose push outside its worktree the auto-reviewer allowed."""
GUARDIAN = RECORDED.with_name("codex-guardian-0.166.0.jsonl")
"""That review's rollout, beside the coder's: ``session_meta`` ``source`` ``{"subagent":
{"other": "guardian"}}``, ``thread_source`` ``guardian_review``, ``parent_thread_id`` the
coder's thread, one review and its ``token_usage_record``."""
GUARDED_THREAD = "01a1a450-987b-7000-8000-00000000c0d1"
GUARDIAN_THREAD = "01a1a450-989d-7000-8000-0000000a0d1f"


def _in(value: Any, cwd: Path) -> Any:
    """``value`` with the recorded folder made ``cwd`` in every string of it, its keys too
    (the files a patch names)."""
    if isinstance(value, str):
        return value.replace("/work/alpha", str(cwd))
    if isinstance(value, list):
        return [_in(item, cwd) for item in value]
    if isinstance(value, dict):
        return {_in(key, cwd): _in(item, cwd) for key, item in value.items()}
    return value


def _recorded(source: Path, home: Path, cwd: Path) -> Path:
    """A recorded 0.166.0 rollout as Codex writes it in ``home``, run in ``cwd``: each line
    moved as JSON and written by ``json.dumps``. Swapped into the text, a Windows ``cwd``'s
    backslashes (``C:\\Users\\…``: ``\\U``, ``\\A``) made 6 of the reviewer's 16 lines no
    JSON, every one naming its model among them (review r37 of #113, finding 3)."""
    lines = source.read_text(encoding="utf-8").splitlines()
    thread = json.loads(lines[0])["payload"]["id"]
    path = home / "sessions" / "2026" / "11" / "03" / f"rollout-2026-11-03T21-08-27-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(_in(json.loads(line), cwd)) + "\n" for line in lines),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("source", [GUARDED, GUARDIAN], ids=["session", "review"])
def test_a_recorded_rollout_moved_into_a_windows_folder_is_still_one_json_record_a_line(
    tmp_path: Path, source: Path
) -> None:
    """Review r37 of #113, finding 3, on every platform: a folder spelt as Windows spells one
    (one name on POSIX) is moved into every line as a string, and each line still parses,
    the reviewer's model kept."""
    cwd = Path("C:\\Users\\Alpha\\work\\alpha")
    moved = _recorded(source, tmp_path, cwd).read_text(encoding="utf-8").splitlines()

    records = [json.loads(line) for line in moved]

    assert len(records) == len(source.read_text(encoding="utf-8").splitlines())
    assert all("/work/alpha" not in line for line in moved)
    assert records[0]["payload"]["cwd"] == str(cwd), "its session_meta's folder"
    if source == GUARDIAN:
        assert "codex-auto-review" in json.dumps(records)


def test_a_codex_reviewers_rollout_counts_once_in_the_session_it_reviewed_for(
    machine: Machine, codex_home: Path
) -> None:
    """SMOKE12 F2: ``--approve-for-me`` runs its reviewer as a thread of its own, whose rollout
    names the session it reviews for (``AgentTranscript.parent_session``). The session's Stop
    records it; the sweep reads the review into the session's row, a turn of its own there,
    never a session of its own: the review's tokens once, in the session (``codex-auto-review``,
    its model), and neither a second sweep nor the session's next Stop counts them again.
    Before, the sweep recorded the review as a second session."""
    alpha = machine.repo("alpha")
    add_project(alpha)
    session = _recorded(GUARDED, codex_home, alpha)
    review = _recorded(GUARDIAN, codex_home, alpha)
    _stop(codex_home, session, alpha, "turn-1", GUARDED_THREAD)
    hooked = _row(codex_home, GUARDED_THREAD)
    assert hooked.total_tokens == _tokens(session)

    report = usage_stats.sweep()

    assert (report.transcripts, report.sessions_new, report.sessions_updated) == (2, 0, 1)
    assert report.turns == 1, "the review, a turn of the session's"
    with store_session() as store:
        assert [row.session_id for row in store.session_usages()] == [hooked.session_id]
        turns = store.turn_usages(hooked.session_id)
    row = _row(codex_home, GUARDED_THREAD)
    assert row.transcript_path == str(session), "the session's own transcript, still"
    assert row.total_tokens == _tokens(session) + _tokens(review)
    assert row.usage["models"]["codex-auto-review"]["requests"] == 1
    assert [turn.source for turn in turns] == [None, "sweep"]

    warmed = json.loads(transcript_facts.facts_cache_path().read_text(encoding="utf-8"))
    assert str(session) in warmed["files"] and str(review) not in warmed["files"]

    again = usage_stats.sweep()
    _stop(codex_home, session, alpha, "turn-2", GUARDED_THREAD)
    usage_stats.sweep()

    assert (again.sessions_new, again.sessions_updated, again.turns) == (0, 0, 0)
    assert _row(codex_home, GUARDED_THREAD).total_tokens == row.total_tokens


def test_a_codex_review_a_sweep_before_recorded_as_a_session_is_counted_there_never_twice(
    machine: Machine, codex_home: Path
) -> None:
    """A review a sweep before this fix recorded as a session of its own (its row naming its
    rollout) keeps being read there: its tokens are never read again into its session's."""
    alpha = machine.repo("alpha")
    session = _recorded(GUARDED, codex_home, alpha)
    review = _recorded(GUARDIAN, codex_home, alpha)
    reader = CODEX.transcript_reader
    assert reader is not None
    scan = reader.usage(review, None)
    assert scan is not None
    alone = session_key(CODEX, codex_home, GUARDIAN_THREAD)
    with store_session() as store:
        recorded = store.record_usage(
            SessionUsage(
                session_id=alone,
                project_id="prj_x",
                transcript_path=str(review),
                state=scan.state,
                total_tokens=scan.delta.totals().total,
                started_at=NOW,
                updated_at=NOW,
                agent="codex",
            ),
            None,
            previous_state=None,
        )
    assert recorded

    usage_stats.sweep()

    assert _row(codex_home, GUARDED_THREAD).total_tokens == _tokens(session)
    assert _row(codex_home, GUARDIAN_THREAD).total_tokens == _tokens(review)


def test_a_rollout_is_a_subagents_by_its_session_meta_source_and_parent_alone(
    tmp_path: Path,
) -> None:
    """``parent_session`` reads a rollout's ``session_meta``: a reviewer's names the thread it
    reviews for; every session's own rollout recorded, 0.162.1's to 0.166.0's, names none, nor
    does a ``parent_thread_id`` whose ``source`` is no subagent's, nor a file that is gone."""
    reader = CODEX.transcript_reader
    assert reader is not None
    assert reader.parent_session(GUARDIAN) == GUARDED_THREAD
    folder = RECORDED.parent
    own = sorted(set(folder.glob("codex-*.jsonl")) - {GUARDIAN})
    assert len(own) >= 9 and all(reader.parent_session(path) is None for path in own)
    lines = GUARDIAN.read_text(encoding="utf-8").splitlines()
    meta = json.loads(lines[0])
    meta["payload"]["source"] = "cli"
    named = tmp_path / "named.jsonl"
    named.write_text("\n".join([json.dumps(meta), *lines[1:]]) + "\n", encoding="utf-8")
    assert reader.parent_session(named) is None
    assert reader.parent_session(tmp_path / "gone.jsonl") is None


def test_a_codex_review_found_before_its_session_has_a_row_waits_for_it(
    machine: Machine, codex_home: Path
) -> None:
    """Swept with no Stop before, the session is recorded first and its review read into it in
    the same sweep, newer though the review's rollout is (listed first). A review whose session
    has no row, its rollout gone, is read into none and is no session either."""
    alpha = machine.repo("alpha")
    session = _recorded(GUARDED, codex_home, alpha)
    review = _recorded(GUARDIAN, codex_home, alpha)
    os.utime(session, (1, 1))
    orphan = review.with_name(review.name.replace(GUARDIAN_THREAD, OTHER))
    orphan.write_text(
        review.read_text(encoding="utf-8").replace(GUARDED_THREAD, THREAD), encoding="utf-8"
    )

    report = usage_stats.sweep()

    assert (report.transcripts, report.sessions_new, report.turns) == (3, 1, 2)
    assert _row(codex_home, GUARDED_THREAD).total_tokens == _tokens(session) + _tokens(review)
    with store_session() as store:
        assert len(store.session_usages()) == 1, "the orphan review is no session"


# --- the opt-in usage share: Claude Code's alone ---------------------------------------------


@pytest.mark.usefixtures("sharing_allowed")
def test_the_shared_batch_holds_the_claude_code_session_alone_and_the_stats_both(
    machine: Machine, codex_home: Path
) -> None:
    """The owner's decision (2026-10-27): opt-in usage sharing shares Claude Code's usage alone,
    as its consent says (``usage_share.COVERAGE``, unchanged), now that the sweep records
    Codex's too. One Claude Code session and one Codex session, swept: ``metrics stats`` shows
    both, and the preview of the shared batch (what ``metrics share preview`` prints and a
    shipment posts) holds the Claude Code session's tokens, repository, session and account
    alone: no Codex token, model, repository or slot."""
    alpha, beta = machine.repo("alpha"), machine.repo("beta")
    at = NOW - timedelta(days=1)
    machine.session(
        core.DEFAULT_SLOT,
        alpha,
        "claude-session",
        prompt(at, cwd=alpha),
        request(at + timedelta(minutes=1), "m1", cwd=alpha),
    )
    rollout = _rollout(codex_home, beta, THREAD)
    runner = CliRunner()
    assert runner.invoke(app, ["metrics", "backfill"]).exit_code == 0

    as_json = runner.invoke(
        app, ["--json", "metrics", "stats", "--range", "lifetime", "--no-sweep"]
    )
    shown = json.loads(usage_share.preview(now=NOW, tz=IST))

    stats = UsageStats.model_validate(json.loads(as_json.output))
    by_repo = {row.name: row.tokens.total for row in stats.repos.rows}
    assert stats.sessions == 2 and set(by_repo) == {"alpha", "beta"}
    assert by_repo["beta"] == _tokens(rollout)
    with store_session() as store:
        claude_row = store.session_usage("claude-session")
    assert claude_row is not None and claude_row.agent == "claude-code"
    assert _row(codex_home, THREAD).agent == "codex"
    days = shown["days"]
    assert sum(day["tokens"]["total"] for day in days) == by_repo["alpha"]
    assert sum(day["sessions"] for day in days) == 1
    assert sum(len(day["repos"]) for day in days) == 1
    assert {name for day in days for name in day["models"]} == {MODEL}, "no Codex model"
    assert len(core.list_accounts()) == 2, "Claude Code's plain slot and Codex's"
    assert {day["accounts"]["registered"] for day in days} == {1}


def _as_before_v47() -> None:
    """The store as a build of this branch left it before v47 (v50 since the 0.9.2 merge): its
    usage rows, Codex's among them, and no ``usage_session.agent``."""
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.execute("ALTER TABLE usage_session DROP COLUMN agent")
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        raw.commit()
    finally:
        raw.close()


@pytest.mark.usefixtures("sharing_allowed")
def test_a_codex_row_recorded_before_v47_is_in_no_preview(
    machine: Machine, codex_home: Path
) -> None:
    """Review r28 of #113, m1: this branch recorded Codex sessions (the Stop hook's, the
    sweep's) before v47 gave a usage row its agent, and a row that names none reads as the
    default agent's, which is shared, until a new turn rewrites it. v47's backfill gives each
    its agent (its transcript's here: no board session names it), so the preview of the shared
    batch holds the Claude Code session alone, as it did before the store was taken back."""
    alpha, beta = machine.repo("alpha"), machine.repo("beta")
    at = NOW - timedelta(days=1)
    machine.session(
        core.DEFAULT_SLOT,
        alpha,
        "claude-session",
        prompt(at, cwd=alpha),
        request(at + timedelta(minutes=1), "m1", cwd=alpha),
    )
    _rollout(codex_home, beta, THREAD)
    assert CliRunner().invoke(app, ["metrics", "backfill"]).exit_code == 0
    claude_alone = json.loads(usage_share.preview(now=NOW, tz=IST))["days"]
    _as_before_v47()

    swept = CliRunner().invoke(app, ["--json", "metrics", "backfill"])
    shown = json.loads(usage_share.preview(now=NOW, tz=IST))["days"]

    assert swept.exit_code == 0, swept.output
    assert _row(codex_home, THREAD).agent == "codex", "backfilled from its rollout's path"
    with store_session() as store:
        claude_row = store.session_usage("claude-session")
    assert claude_row is not None and claude_row.agent == "claude-code"
    assert shown == claude_alone
    assert sum(day["sessions"] for day in shown) == 1
    assert {name for day in shown for name in day["models"]} == {MODEL}, "no Codex model"


def _agents_unset(*sessions: str) -> None:
    """``usage_session.agent`` NULL again on ``sessions`` (every row with none), in a store
    left at v47: what a v47 build that had no backfill left, or a v46 build wrote since."""
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        where = f" WHERE session_id IN ({', '.join('?' * len(sessions))})" if sessions else ""
        raw.execute(f"UPDATE usage_session SET agent = NULL{where}", sessions)
        raw.commit()
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()


@pytest.mark.usefixtures("sharing_allowed")
@pytest.mark.parametrize(
    "case", ["(c) a v46 build's row after the backfill", "(d) v47 with no backfill"]
)
def test_a_codex_row_still_naming_no_agent_is_attributed_before_a_batch_is_built(
    machine: Machine, codex_home: Path, case: str
) -> None:
    """tester-42 on #42, (c) and (d): v47's backfill runs once, when the column is added, so
    a store a v47 build migrated before it existed (4e5176a2..1efa3ce0) never ran it, and a
    v46 build sharing a store that did still writes NULL; such a Codex row was shared as the
    default agent's (4233 tokens, model 'other'). The sharing path gives every row still NULL
    the backfill's attribution when it builds a batch, and writes it back."""
    alpha, beta = machine.repo("alpha"), machine.repo("beta")
    at = NOW - timedelta(days=1)
    machine.session(
        core.DEFAULT_SLOT,
        alpha,
        "claude-session",
        prompt(at, cwd=alpha),
        request(at + timedelta(minutes=1), "m1", cwd=alpha),
    )
    _rollout(codex_home, beta, THREAD)
    assert CliRunner().invoke(app, ["metrics", "backfill"]).exit_code == 0
    claude_alone = json.loads(usage_share.preview(now=NOW, tz=IST))["days"]
    if case.startswith("(c)"):
        _rollout(codex_home, beta, OTHER)  # a second session, written by a v46 build
        assert CliRunner().invoke(app, ["metrics", "backfill"]).exit_code == 0
        _agents_unset(session_key(CODEX, codex_home, OTHER))
    else:
        _agents_unset()

    shown = json.loads(usage_share.preview(now=NOW, tz=IST))["days"]

    assert shown == claude_alone
    assert {name for day in shown for name in day["models"]} == {MODEL}, "no 'other' model"
    assert _row(codex_home, OTHER if case.startswith("(c)") else THREAD).agent == "codex"
    with store_session() as store:
        claude_row = store.session_usage("claude-session")
    assert claude_row is not None and claude_row.agent == "claude-code", "written back"


def test_a_row_from_before_agents_is_claude_codes_and_an_agent_unknown_here_is_not_shared() -> None:
    """Whose usage is shared is its agent's capability (``AgentCapabilities.usage_shared``):
    Claude Code's, and a usage row from before agents were recorded, which no release but
    Claude Code's wrote; never Codex's, nor an agent this install does not know."""
    assert usage_stats.shared_usage(None) and usage_stats.shared_usage("claude-code")
    assert not usage_stats.shared_usage("codex")
    assert not usage_stats.shared_usage("an-agent-this-install-does-not-know")


def test_a_row_that_names_no_agent_is_shared_as_the_default_agent_s_never_as_null(
    machine: Machine, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share needs a positive attribution (review r28 of #113, m1; board note 4247, mutant
    S1): a NULL agent is shared only as the default agent's usage is, never for being NULL.
    With a default agent whose usage is not shared, a NULL is not, and a store taken back to
    v46 previews its Claude Code row alone, which v47's backfill attributed by its path."""
    from aisquare.core import agent_adapters

    alpha, beta = machine.repo("alpha"), machine.repo("beta")
    at = NOW - timedelta(days=1)
    machine.session(
        core.DEFAULT_SLOT,
        alpha,
        "claude-session",
        prompt(at, cwd=alpha),
        request(at + timedelta(minutes=1), "m1", cwd=alpha),
    )
    _rollout(codex_home, beta, THREAD)
    assert CliRunner().invoke(app, ["metrics", "backfill"]).exit_code == 0
    claude_alone = json.loads(usage_share.preview(now=NOW, tz=IST))["days"]
    _as_before_v47()
    monkeypatch.setattr(agent_adapters, "DEFAULT_AGENT", CODEX.id)

    shown = json.loads(usage_share.preview(now=NOW, tz=IST))["days"]

    assert not usage_stats.shared_usage(None), "a NULL is the default agent's, not shared"
    assert shown == claude_alone, "the Claude Code row is shared by its own attribution"
    assert {name for day in shown for name in day["models"]} == {MODEL}, "no 'other' model"
