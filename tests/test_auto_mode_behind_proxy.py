"""#150: auto mode's classifier request fails behind the explainability proxy.

The fix is the proxy's (AISquare-Explainability-SDK#1144). What the CLI does
meanwhile, each with its positive and its negative control:

- ``core.transcripts`` reads two facts off a Claude Code transcript, bounded:
  the first turn's input size (the session BASELINE) and the refusals in the
  tail;
- ``services.auto_mode.measure_baseline`` reads them for the newest sessions
  with a transcript on disk, creating nothing;
- the ``explainability auto-mode`` doctor line exists only when a fleet role
  runs ``auto`` behind a configured proxy, and says what the evidence says;
- ``fleet spawn`` repeats the warning on its receipt when the evidence is there;
- the Stop hook puts a session that was launched through the proxy and is
  being refused in ``attention`` with ONE board line.

The transcript shapes are the ones read off the reporting machine's own
transcripts (a coder's first turn: ``2 + 7,596 + 130,041`` tokens; a refusal
as a ``tool_result`` block).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths, transcripts
from aisquare.core.config import (
    AppConfig,
    FleetRoleSettings,
    FleetSettings,
    load_config,
    save_config,
)
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import CheckStatus, FleetAgent, ProjectInfo, TeamSession
from aisquare.services import auto_mode, diagnostics
from aisquare.services import hooks as hooks_service
from aisquare.services import team as team_service
from aisquare.services.explainability import PIPELINE_ID_ENV_VAR

REFUSAL = (
    "claude-opus-5[1m] is temporarily unavailable (server error), so auto mode cannot "
    "determine the safety of Bash right now. Wait a moment and then try this action again."
)

REPO = Path(__file__).resolve().parents[1]


# --- transcripts on disk --------------------------------------------------------------------


def _assistant(usage: dict[str, Any], *, model: str = "claude-opus-5") -> dict[str, Any]:
    return {"type": "assistant", "message": {"model": model, "role": "assistant", "usage": usage}}


def _refused() -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "content": REFUSAL, "is_error": True}],
        },
        "toolUseResult": "Error: " + REFUSAL,
    }


def _plain_result(text: str = "ok", *, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "content": text}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _transcript(path: Path, entries: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [entry if isinstance(entry, str) else json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_first_turn_size_sums_the_three_input_fields_of_the_first_assistant_entry(
    tmp_path: Path,
) -> None:
    path = _transcript(
        tmp_path / "coder.jsonl",
        [
            {"type": "summary", "summary": "intake"},
            {"type": "user", "message": {"role": "user", "content": "start"}},
            "not json at all {",
            _assistant(
                {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 7596,
                    "cache_read_input_tokens": 130041,
                    "output_tokens": 302,  # output is not part of the request
                }
            ),
            _assistant({"input_tokens": 999999}),  # a LATER turn: not the baseline
        ],
    )
    assert transcripts.first_turn_tokens(path) == 137639
    # Negatives: no assistant turn yet, a missing file, a usage with odd values.
    assert (
        transcripts.first_turn_tokens(_transcript(tmp_path / "fresh.jsonl", [{"type": "user"}]))
        is None
    )
    assert transcripts.first_turn_tokens(tmp_path / "missing.jsonl") is None
    odd = _transcript(
        tmp_path / "odd.jsonl",
        [_assistant({"input_tokens": "12", "cache_read_input_tokens": True})],
    )
    assert transcripts.first_turn_tokens(odd) == 0


def test_a_synthetic_api_error_entry_is_not_the_first_turn(tmp_path: Path) -> None:
    """Claude Code's own entry for a failed request carries zero usage: not a baseline of 0."""
    zeros = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    real = {
        "input_tokens": 2,
        "cache_creation_input_tokens": 6_959,
        "cache_read_input_tokens": 130_041,
    }
    both = {**_assistant(zeros, model="<synthetic>"), "isApiErrorMessage": True}
    flag_only = {**_assistant(zeros), "isApiErrorMessage": True}
    for name, synthetic in (
        ("both", both),
        ("model", _assistant(zeros, model="<synthetic>")),
        ("flag", flag_only),
    ):
        path = _transcript(tmp_path / f"{name}.jsonl", [synthetic, _assistant(real)])
        assert transcripts.first_turn_tokens(path) == 137_002, name
    # Only the synthetic entry so far: nothing measured yet, not "0k and fine".
    alone = _transcript(tmp_path / "alone.jsonl", [both])
    assert transcripts.first_turn_tokens(alone) is None


def test_refusals_are_counted_in_tool_results_only_and_from_the_tail(tmp_path: Path) -> None:
    path = _transcript(
        tmp_path / "manager.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "the doc says: " + REFUSAL}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": REFUSAL}]}},
            _plain_result(),
            _refused(),
            _refused(),
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "text", "text": REFUSAL}],
                            "is_error": True,
                        }
                    ]
                },
            },
        ],
    )
    assert transcripts.refusal_count(path) == 3  # a quote in a prompt or a reply is not one
    assert transcripts.refusal_count(tmp_path / "missing.jsonl") == 0
    # The tail is bounded: a refusal older than the window is not counted, a recent one is.
    old = _transcript(
        tmp_path / "old.jsonl", [_refused(), *[_plain_result("x" * 200) for _ in range(40)]]
    )
    assert transcripts.refusal_count(old, tail_bytes=4_000) == 0
    assert transcripts.refusal_count(old) == 1


def test_a_tool_output_that_quotes_the_refusal_is_not_one(tmp_path: Path) -> None:
    """An agent working on this repository reads the files that name the signature.

    A successful Read of this module or of the docs, a grep that exits non-zero
    over them, and a failing test run that prints the sentence all CONTAIN the
    marker; none of them is Claude Code refusing a tool call.
    """
    sources = [
        Path(transcripts.__file__),
        REPO / "docs" / "fleet.md",
        REPO / "docs" / "connecting-your-agents-to-explainability.md",
    ]
    reads = [source.read_text(encoding="utf-8") for source in sources]
    assert all(transcripts.REFUSAL_MARKER in text for text in reads), "the premise"
    grep_hit = f"docs/fleet.md:1:{REFUSAL}"
    quoted = [
        *[_plain_result(text) for text in reads],  # successful Reads
        _plain_result(grep_hit),  # a grep that matched: exit 0
        _plain_result("Exit code 1\n" + grep_hit, is_error=True),  # grep … && false
        _plain_result("FAILED tests/x.py\n" + REFUSAL + "\n" + "E" * 2_000, is_error=True),
        _plain_result(REFUSAL + "\n" + "x" * 5_000, is_error=True),  # long: an output
    ]
    assert transcripts.refusal_count(_transcript(tmp_path / "coder.jsonl", quoted)) == 0
    # The same session with the refusals themselves: they are still counted.
    refused = _transcript(tmp_path / "refused.jsonl", [*quoted, *[_refused()] * 3])
    assert transcripts.refusal_count(refused) == 3


def test_a_refusal_that_goes_on_past_its_sentence_is_still_one(tmp_path: Path) -> None:
    """The bare sentence need not be the whole tool result Claude Code writes.

    Its auto-mode denials carry a guidance paragraph after their sentence and
    run to ~1,050 characters, and #150 quotes this refusal as going on past
    "try this action again". A length cap sized to the bare sentence would
    zero every real count — no bell, no doctor evidence — while every other
    fixture here, the bare sentence, stayed green.
    """
    # Stands in for the guidance paragraph at its length (~900 characters).
    guidance = " ".join(
        ["If you have other tasks that don't depend on this action, continue working on those."]
        * 10
    )
    same_line = REFUSAL + " " + guidance
    own_paragraph = REFUSAL + "\n\n" + guidance
    assert min(len(same_line), len(own_paragraph)) > 1_000, "the premise: past the bare sentence"
    path = _transcript(
        tmp_path / "manager.jsonl",
        [
            _plain_result(same_line, is_error=True),
            _plain_result(own_paragraph, is_error=True),
            _refused(),
        ],
    )
    assert transcripts.refusal_count(path) == 3


# --- the baseline over the store ------------------------------------------------------------


def _project(root: Path) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    project = team_project(root)
    with store_session() as store:
        store.ensure_project(project)
    return project


def _session(
    project: ProjectInfo,
    session_id: str,
    transcript: Path | None,
    *,
    role: str = "coder",
    minutes_ago: int = 0,
) -> TeamSession:
    now = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    with store_session() as store:
        return store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=role,
                started_at=now,
                last_seen_at=now,
                transcript_path=str(transcript) if transcript is not None else None,
            )
        )


def _sized(path: Path, tokens: int, *, refusals: int = 0) -> Path:
    return _transcript(
        path,
        [_assistant({"input_tokens": tokens}), *[_refused() for _ in range(refusals)]],
    )


def test_the_baseline_reads_the_newest_sessions_with_a_transcript_and_creates_nothing(
    isolated_home: Path, tmp_path: Path
) -> None:
    assert not isolated_home.exists()
    assert auto_mode.measure_baseline() == auto_mode.Baseline()  # no database: nothing to read
    assert not isolated_home.exists(), "doctor must not create the home it reports on"

    paths.ensure_home()
    project = _project(tmp_path / "repo")
    _session(project, "s-old", _sized(tmp_path / "t" / "old.jsonl", 90_000), minutes_ago=30)
    _session(
        project, "s-new", _sized(tmp_path / "t" / "new.jsonl", 137_000, refusals=5), minutes_ago=1
    )
    _session(project, "s-gone", tmp_path / "t" / "deleted.jsonl", minutes_ago=2)  # not on disk
    _session(project, "s-none", None, minutes_ago=3)  # never reported one
    _session(
        project,
        "s-fresh",
        _transcript(tmp_path / "t" / "fresh.jsonl", [{"type": "user"}]),
        minutes_ago=4,
    )

    baseline = auto_mode.measure_baseline()

    assert [sample.session_id for sample in baseline.samples] == ["s-new", "s-fresh", "s-old"]
    assert baseline.latest == 137_000 and baseline.largest == 137_000 and baseline.median == 113_500
    assert baseline.refused_sessions == 1 and baseline.predicted == 137_000
    assert baseline.describe() == "137k (newest of 2 sessions; median 114k, largest 137k)"
    assert auto_mode.measure_baseline(limit=1).samples[0].session_id == "s-new"
    assert auto_mode.Baseline().describe().startswith("not measurable yet")


# --- the doctor line ------------------------------------------------------------------------


def _configure(*, tracing: bool, modes: dict[str, str] | None = None) -> None:
    config = AppConfig()
    config.explainability.enabled = tracing
    config.explainability.proxy_url = "http://127.0.0.1:9199"
    config.fleet = FleetSettings(
        roles={
            role: FleetRoleSettings(permission_mode=mode) for role, mode in (modes or {}).items()
        }
    )
    save_config(config)


def _doctor_row() -> Any:
    return next((c for c in diagnostics.doctor() if c.name == auto_mode.CHECK_NAME), None)


def test_the_doctor_line_exists_only_for_auto_mode_behind_a_configured_proxy(
    isolated_home: Path, tmp_path: Path
) -> None:
    paths.ensure_home()
    _configure(tracing=False)
    assert auto_mode.doctor_check() is None  # no proxy: nothing to warn about
    _configure(tracing=True, modes={role: "acceptEdits" for role in auto_mode.auto_roles()})
    assert auto_mode.auto_roles() == [] and auto_mode.doctor_check() is None  # no classifier user
    _configure(tracing=True, modes={"coder": "acceptEdits"})
    assert "coder" not in auto_mode.auto_roles() and "manager" in auto_mode.auto_roles()

    # Tracing on, roles in auto, no transcript yet: a warning that says the size is unmeasured.
    check = auto_mode.doctor_check()
    assert check is not None and check.status is CheckStatus.warn
    assert "manager" in check.detail and "not measurable yet" in check.detail
    assert check.fix and "--permission-mode acceptEdits" in check.fix
    assert "AISquare-Explainability-SDK/issues/1144" in check.fix
    assert "explainability disable" in check.fix
    assert _doctor_row() is not None, "it reaches the real doctor"


_TABLE_STEP = re.compile(r"add a `(\[fleet\.roles\.[\w-]+\])` table with `([^`]+)`")


def _follow_table_step(text: str) -> None:
    """Do what the printed table step says: its header, then its key, in the config file."""
    match = _TABLE_STEP.search(text)
    assert match is not None, text
    header, key = match.groups()
    with paths.config_path().open("a", encoding="utf-8") as handle:
        handle.write(f"\n{header}\n{key}\n")


def test_the_printed_mode_fix_is_one_this_config_accepts(
    isolated_home: Path, runner: CliRunner
) -> None:
    """`config set` only writes keys the config has; a trimmed [fleet.roles] has fewer."""
    paths.ensure_home()
    # A [fleet.roles] that lists only coder: manager runs on the built-in auto, no table.
    _configure(tracing=True, modes={"coder": "acceptEdits"})
    check = auto_mode.doctor_check()
    assert check is not None and check.fix
    assert "config set fleet.roles.manager" not in check.fix
    assert (
        'add a `[fleet.roles.manager]` table with `permission_mode = "acceptEdits"` '
        f"to {paths.config_path()}"
    ) in check.fix
    # Never the header and the key as one line: TOML does not parse that, and a config
    # that does not parse sends every role back to the built-in auto.
    assert "] permission_mode" not in check.fix
    refused = runner.invoke(
        app, ["config", "set", "fleet.roles.manager.permission_mode", "acceptEdits"]
    )
    assert refused.exit_code != 0, "the command it no longer prints fails on this config"
    # Followed as printed, the file still parses, manager is off auto, and the
    # config's own customisation survives.
    _follow_table_step(check.fix)
    assert load_config().fleet.roles["coder"].permission_mode == "acceptEdits"
    assert "manager" not in auto_mode.auto_roles()

    # A role in auto WITH a table: the command is named for it, and it works.
    _configure(tracing=True, modes={"coder": "acceptEdits", "tester": "auto"})
    check = auto_mode.doctor_check()
    assert check is not None and check.fix
    command = "aisquare config set fleet.roles.tester.permission_mode acceptEdits"
    assert command in check.fix
    result = runner.invoke(app, command.split()[1:])
    assert result.exit_code == 0, result.output
    assert "tester" not in auto_mode.auto_roles()


def test_the_doctor_line_reads_the_evidence(isolated_home: Path, tmp_path: Path) -> None:
    paths.ensure_home()
    _configure(tracing=True)
    project = _project(tmp_path / "repo")

    _session(project, "s-small", _sized(tmp_path / "t" / "small.jsonl", 72_000))
    fine = auto_mode.doctor_check()
    assert fine is not None and fine.status is CheckStatus.ok
    assert "72k" in fine.detail and "grows past it" in fine.detail and fine.fix is None

    _session(project, "s-big", _sized(tmp_path / "t" / "big.jsonl", 137_639), minutes_ago=0)
    big = auto_mode.doctor_check()
    assert big is not None and big.status is CheckStatus.warn
    assert "138k" in big.detail and "refused from the first one" in big.detail

    _session(project, "s-refused", _sized(tmp_path / "t" / "refused.jsonl", 98_000, refusals=7))
    refused = auto_mode.doctor_check()
    assert refused is not None and refused.status is CheckStatus.warn
    assert "1 of the last 3 sessions were refused" in refused.detail
    assert "AISquare-Explainability-SDK#1144" in refused.detail
    # A 98k baseline is under the line: it is stated beside it, not as the size that failed.
    assert "above ~100k tokens, and this machine's session baseline is 98k" in refused.detail
    assert "fails at this machine's session baseline" not in refused.detail


def test_a_transient_refusal_or_two_is_not_a_refused_session(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The doctor line and the spawn note count a session refused at the Stop hook's bar."""
    paths.ensure_home()
    _configure(tracing=True)
    project = _project(tmp_path / "repo")
    _session(project, "s-blip", _sized(tmp_path / "t" / "blip.jsonl", 80_000, refusals=2))

    baseline = auto_mode.measure_baseline()
    assert baseline.samples[0].refusals == 2 and baseline.refused_sessions == 0
    fine = auto_mode.doctor_check()
    assert fine is not None and fine.status is CheckStatus.ok
    assert auto_mode.spawn_note("auto") is None

    _session(project, "s-hit", _sized(tmp_path / "t" / "hit.jsonl", 80_000, refusals=3))
    assert auto_mode.measure_baseline().refused_sessions == 1
    warned = auto_mode.doctor_check()
    assert warned is not None and warned.status is CheckStatus.warn


def test_a_transcript_doctor_may_not_stat_does_not_crash_doctor(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_file`` raises PermissionError under a directory this user may not traverse."""
    paths.ensure_home()
    _configure(tracing=True)
    project = _project(tmp_path / "repo")
    locked = tmp_path / "locked"
    _session(project, "s-locked", _sized(locked / "hidden.jsonl", 137_000), minutes_ago=0)
    _session(project, "s-small", _sized(tmp_path / "t" / "small.jsonl", 72_000), minutes_ago=5)
    real_is_file = Path.is_file

    def is_file(self: Path) -> bool:
        if self.parent == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)

    baseline = auto_mode.measure_baseline()
    assert [sample.session_id for sample in baseline.samples] == ["s-small"]
    check = auto_mode.doctor_check()
    assert check is not None and "72k" in check.detail
    assert _doctor_row() is not None, "the whole doctor still runs"


# --- the spawn receipt ----------------------------------------------------------------------


def test_the_spawn_note_needs_auto_mode_a_proxy_and_evidence(
    isolated_home: Path, tmp_path: Path
) -> None:
    paths.ensure_home()
    _configure(tracing=True)
    project = _project(tmp_path / "repo")
    assert auto_mode.spawn_note("auto") is None  # nothing measured: doctor carries that case
    _session(project, "s-big", _sized(tmp_path / "t" / "big.jsonl", 141_000))
    note = auto_mode.spawn_note("auto")
    assert note is not None and "141k" in note and "--permission-mode acceptEdits" in note
    assert auto_mode.spawn_note("acceptEdits") is None  # no classifier: no note
    assert auto_mode.spawn_note("") is None
    _configure(tracing=False)
    assert auto_mode.spawn_note("auto") is None  # no proxy: no note
    _configure(tracing=True)
    _session(project, "s-small", _sized(tmp_path / "t" / "small.jsonl", 80_000))  # the newest
    assert auto_mode.spawn_note("auto") is None  # under the line, nothing refused
    _session(project, "s-hit", _sized(tmp_path / "t" / "hit.jsonl", 80_000, refusals=3))
    hit = auto_mode.spawn_note("auto")
    assert hit is not None and "1 of the last 3 sessions were refused" in hit


# --- the Stop hook ----------------------------------------------------------------------------


def _events(project: ProjectInfo) -> list[tuple[str, str]]:
    with store_session() as store:
        return [(e.kind, e.text) for e in store.recent_events(project.id, limit=50)]


def _state(session_id: str) -> str:
    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None
    return session.state


def _launched(monkeypatch: pytest.MonkeyPatch, *, traced: bool) -> None:
    """The environment the agent's hook inherits: a traced launch's marker, or none."""
    if traced:
        monkeypatch.setenv(PIPELINE_ID_ENV_VAR, "run-150")
    else:
        monkeypatch.delenv(PIPELINE_ID_ENV_VAR, raising=False)


def test_a_refused_session_is_put_in_attention_once_with_one_board_line(
    isolated_home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.ensure_home()
    _configure(tracing=True, modes={"coder": "auto"})
    _launched(monkeypatch, traced=True)
    project = _project(tmp_path / "repo")
    team_service.activate(project.root)
    transcript = _sized(tmp_path / "t" / "coder.jsonl", 137_000, refusals=5)
    _session(project, "sess-coder", transcript)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_auto1",
                project_id=project.id,
                label="coder-auth",
                role="coder",
                pane_id="%4",
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
                session_id="sess-coder",
            )
        )
    payload = json.dumps({"session_id": "sess-coder", "cwd": str(project.root)})

    result = runner.invoke(app, ["hook", "stop"], input=payload)

    assert result.exit_code == 0, result.output
    assert _state("sess-coder") == "attention"
    blocked = [text for kind, text in _events(project) if kind == auto_mode.EVENT_KIND]
    assert len(blocked) == 1
    assert blocked[0].startswith("coder-auth: auto mode is refusing its tool calls")
    assert (
        "5 refusals" in blocked[0] and "fleet.roles.coder.permission_mode acceptEdits" in blocked[0]
    )
    assert "aisquare fleet restart coder-auth" in blocked[0]

    # The next Stop finds the same refusals: the feed does not repeat, the row is waiting again.
    again = runner.invoke(app, ["hook", "stop"], input=payload)
    assert again.exit_code == 0
    assert len([1 for kind, _ in _events(project) if kind == auto_mode.EVENT_KIND]) == 1
    assert _state("sess-coder") == "waiting"


def test_fewer_refusals_than_the_threshold_or_no_transcript_change_nothing(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.ensure_home()
    _configure(tracing=True)
    _launched(monkeypatch, traced=True)
    project = _project(tmp_path / "repo")
    team_service.activate(project.root)
    _session(project, "sess-two", _sized(tmp_path / "t" / "two.jsonl", 137_000, refusals=2))
    _session(project, "sess-none", None)
    _session(project, "sess-gone", tmp_path / "t" / "gone.jsonl")

    assert hooks_service.turn_stopped(project.root, session_id="sess-two") is None
    assert auto_mode.record_refusals("sess-none") == 0
    assert auto_mode.record_refusals("sess-gone") == 0
    assert auto_mode.record_refusals("sess-unknown") == 0

    assert _state("sess-two") == "waiting"  # a transient 5xx or two is not a blocked session
    assert [kind for kind, _ in _events(project) if kind == auto_mode.EVENT_KIND] == []


def _fleet_agent(project: ProjectInfo, session_id: str, *, label: str, role: str) -> None:
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{label}",
                project_id=project.id,
                label=label,
                role=role,
                pane_id="%4",
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
                session_id=session_id,
            )
        )


def test_only_a_session_launched_through_the_proxy_is_blamed_on_it(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook asks how THIS session was launched, not what the config says now.

    Untraced, the refusal is a classifier call that failed at Anthropic: no
    proxy line, even on a machine configured to trace — a guard (an unhealthy
    probe, the operator's own ANTHROPIC_BASE_URL) launched it untraced. Traced,
    it is named even after ``explainability disable``: a running agent keeps
    the proxy it started with, and is still refused through it.
    """
    paths.ensure_home()
    _configure(tracing=True)
    project = _project(tmp_path / "repo")
    team_service.activate(project.root)
    _session(project, "sess-plain", _sized(tmp_path / "t" / "plain.jsonl", 137_000, refusals=4))
    _fleet_agent(project, "sess-plain", label="coder-plain", role="coder")

    _launched(monkeypatch, traced=False)
    assert hooks_service.turn_stopped(project.root, session_id="sess-plain") is None
    assert auto_mode.record_refusals("sess-plain") == 0
    assert _state("sess-plain") == "waiting"
    assert [kind for kind, _ in _events(project) if kind == auto_mode.EVENT_KIND] == []

    # The same refusals in a session launched traced, on a machine that has
    # since turned tracing off: named, once.
    _configure(tracing=False)
    _launched(monkeypatch, traced=True)
    assert auto_mode.record_refusals("sess-plain") == 4
    assert _state("sess-plain") == "attention"
    blocked = [text for kind, text in _events(project) if kind == auto_mode.EVENT_KIND]
    assert len(blocked) == 1 and "behind the explainability proxy" in blocked[0]
    # coder has no [fleet.roles] table in this config: the step is the table, not `config set`.
    assert 'add a `[fleet.roles.coder]` table with `permission_mode = "acceptEdits"`' in blocked[0]
    assert "config set fleet.roles.coder" not in blocked[0]


def test_a_config_that_does_not_parse_does_not_cost_the_notice(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook reads the session's launch, not the config file, to decide."""
    paths.ensure_home()
    _configure(tracing=True)
    _launched(monkeypatch, traced=True)
    project = _project(tmp_path / "repo")
    team_service.activate(project.root)
    _session(project, "sess-coder", _sized(tmp_path / "t" / "coder.jsonl", 137_000, refusals=3))
    _fleet_agent(project, "sess-coder", label="coder-auth", role="coder")
    paths.config_path().write_text("[fleet\n", encoding="utf-8")

    assert auto_mode.record_refusals("sess-coder") == 3
    assert _state("sess-coder") == "attention"
    blocked = [text for kind, text in _events(project) if kind == auto_mode.EVENT_KIND]
    assert len(blocked) == 1 and blocked[0].startswith("coder-auth: auto mode is refusing")


def test_the_hook_never_raises_when_the_store_is_damaged(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.ensure_home()
    # Launched traced, so the hook gets as far as opening the store.
    _launched(monkeypatch, traced=True)
    with store_session():
        pass
    paths.db_path().write_bytes(b"not a database")
    assert auto_mode.record_refusals("sess-any") == 0
    assert auto_mode.measure_baseline() == auto_mode.Baseline()
