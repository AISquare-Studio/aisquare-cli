"""The brain layer: distiller outbox, watermark, recall — against a fake gbrain."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import brain
from aisquare.core.teambus import team_project
from aisquare.services import distill
from aisquare.services import team as team_service

_FAKE_GBRAIN = """#!/bin/sh
# Schema-aware fake: `init` records the embedding choice into config.json
# exactly like real gbrain, and `query` (hybrid) REJECTS a vectorless brain —
# so the suite exercises the real create-time constraint instead of passing
# on a path that hard-fails for users.
case "$1" in
  --version) echo "gbrain 0.42.1.0";;
  init)
    mkdir -p "$GBRAIN_HOME/.gbrain/brain.pglite"
    echo 16 > "$GBRAIN_HOME/.gbrain/brain.pglite/PG_VERSION"
    echo "$@" >> "$GBRAIN_HOME/init.log"
    CFG="$GBRAIN_HOME/.gbrain/config.json"
    if echo "$@" | grep -q -- --no-embedding; then
      echo '{"embedding_disabled": true}' > "$CFG"
    else
      echo '{"embedding_model": "openai:text-embedding-3-large"}' > "$CFG"
    fi
    ;;
  put)
    [ "$FAKE_GBRAIN_FAIL" = "1" ] && exit 1
    mkdir -p "$GBRAIN_HOME/pages"
    cat > "$GBRAIN_HOME/pages/$(echo "$2" | tr '/' '_')"
    echo "$2" >> "$GBRAIN_HOME/puts.log"
    ;;
  search) echo "results for: $2";;
  query)
    if grep -q embedding_disabled "$GBRAIN_HOME/.gbrain/config.json" 2>/dev/null; then
      echo "hybrid query needs a vectorized brain" >&2; exit 1
    fi
    echo "hybrid results for: $2";;
  *) exit 0;;
esac
"""


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def fake_gbrain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a scripted gbrain on PATH (keeping system dirs for git etc.)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "gbrain"
    script.write_text(_FAKE_GBRAIN)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir


def _seed_events(runner: CliRunner) -> None:
    team_service.activate()
    runner.invoke(app, ["note", "we will use JWT", "--kind", "decision"])
    runner.invoke(app, ["note", "just chatter"])  # plain notes do not distill
    runner.invoke(app, ["task", "add", "wire auth"])
    task_id = json.loads(runner.invoke(app, ["--json", "task", "list"]).stdout)[0]["id"]
    runner.invoke(app, ["task", "done", task_id, "--note", "all tests green"])


def test_drain_distills_communication_not_churn(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path
) -> None:
    _seed_events(runner)
    assert distill.drain(work_dir) == 3  # decision + note + task_done; task_added/activate skipped
    project = team_project(work_dir)
    puts = (brain.brain_home(project.id) / "puts.log").read_text().splitlines()
    assert any(slug.startswith("team/decision/") for slug in puts)
    assert any(slug.startswith("team/note/") for slug in puts)
    assert any(slug.startswith("team/task-done/") for slug in puts)
    assert not any("task-added" in slug for slug in puts)
    page = next(
        path
        for path in (brain.brain_home(project.id) / "pages").iterdir()
        if "decision" in path.name
    ).read_text()
    assert "we will use JWT" in page and "aisquare-team" in page
    assert distill.drain(work_dir) == 0  # watermark: nothing new on re-drain


def test_drain_holds_watermark_on_put_failure(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_events(runner)
    monkeypatch.setenv("FAKE_GBRAIN_FAIL", "1")
    assert distill.drain(work_dir) == 0  # first durable event failed; nothing written
    monkeypatch.delenv("FAKE_GBRAIN_FAIL")
    assert distill.drain(work_dir) == 3  # retried from the held watermark


def test_drain_without_gbrain_is_a_quiet_noop(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no gbrain anywhere
    _seed_events(runner)
    assert distill.drain(work_dir) == 0


def test_recall_auto_drains_the_backlog(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path
) -> None:
    _seed_events(runner)
    # No manual distill: recall drains the backlog itself, initialising the
    # brain on the way, then searches.
    found = runner.invoke(app, ["recall", "auth"])
    assert found.exit_code == 0, found.output
    assert "results for: auth" in found.stdout
    assert distill.drain(work_dir) == 0  # recall left nothing undistilled


def test_distill_all_backfills_past_the_watermark(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path
) -> None:
    _seed_events(runner)
    assert distill.drain(work_dir) == 3
    # Simulate the pre-broadening era: watermark advanced, pages missing.
    project = team_project(work_dir)
    puts_log = brain.brain_home(project.id) / "puts.log"
    puts_log.write_text("")
    assert distill.drain(work_dir) == 0  # normal drain sees nothing new
    result = runner.invoke(app, ["--json", "team", "distill", "--all"])
    assert json.loads(result.stdout) == {"distilled": 3}  # rescan rebuilt them
    assert len(puts_log.read_text().splitlines()) == 3


def test_distill_cli_reports_count(runner: CliRunner, fake_gbrain: Path, work_dir: Path) -> None:
    _seed_events(runner)
    result = runner.invoke(app, ["--json", "team", "distill"])
    assert json.loads(result.stdout) == {"distilled": 3}


def test_brain_master_switch(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_events(runner)
    monkeypatch.setenv("AISQUARE_BRAIN", "0")
    assert distill.drain(work_dir) == 0
    assert team_service.recall("auth", work_dir) is None


def test_concurrent_drain_lock_is_exclusive(fake_gbrain: Path, work_dir: Path) -> None:
    project = team_project(work_dir)
    with brain.drain_lock(project.id) as first:
        assert first
        with brain.drain_lock(project.id) as second:
            assert not second  # a running drain makes the next one skip


def test_recall_uses_hybrid_on_an_embedding_brain(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Brain created WITH embeddings (knob on at first distill) → hybrid query.
    monkeypatch.setenv("AISQUARE_BRAIN_EMBED", "1")
    _seed_events(runner)
    runner.invoke(app, ["team", "distill"])
    result = runner.invoke(app, ["recall", "auth"])
    assert "hybrid results for: auth" in result.stdout
    project = team_project(work_dir)
    assert brain.brain_embeds(project.id)  # schema really embeds
    assert "--embedding-model" in (brain.brain_home(project.id) / "init.log").read_text()


def test_recall_falls_back_to_keyword_on_a_vectorless_brain(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Brain created WITHOUT embeddings, then the knob is flipped on. recall
    # must stay functional (keyword), not hard-fail on a hybrid query the
    # vectorless brain rejects (round-4 finding 2).
    _seed_events(runner)
    runner.invoke(app, ["team", "distill"])  # embeddings off → no-embedding brain
    project = team_project(work_dir)
    assert not brain.brain_embeds(project.id)
    monkeypatch.setenv("AISQUARE_BRAIN_EMBED", "1")
    result = runner.invoke(app, ["recall", "auth"])
    assert result.exit_code == 0
    assert "results for: auth" in result.stdout  # keyword fallback, not a failure
    assert "hybrid" not in result.stdout


def test_recall_degrades_when_hybrid_fails_at_query_time(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Embedding brain, but the query-time embed call fails (key expired): the
    # hybrid `query` errors, recall must fall back to keyword rather than None.
    monkeypatch.setenv("AISQUARE_BRAIN_EMBED", "1")
    _seed_events(runner)
    runner.invoke(app, ["team", "distill"])
    monkeypatch.setattr(
        brain,
        "_run",
        _degrading_run(brain._run),
    )
    result = runner.invoke(app, ["recall", "auth"])
    assert "results for: auth" in result.stdout  # degraded to keyword, still works


def _degrading_run(real: object) -> object:
    def wrapped(home: Path, argv: list[str], **kw: object) -> object:
        if argv and argv[0] == "query":
            return None  # hybrid fails at query time
        return real(home, argv, **kw)  # type: ignore[operator]

    return wrapped


def test_ensure_sizes_schema_for_embeddings_when_enabled(
    monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(brain, "brain_ready", lambda _pid: False)

    def _capture(home: object, argv: list[str], **kw: object) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(brain, "_run", _capture)

    monkeypatch.setenv("AISQUARE_BRAIN_EMBED", "1")
    brain._ensure("prj_x")
    assert "--embedding-model" in calls[-1]
    assert "openai:text-embedding-3-large" in calls[-1]  # default model
    assert "--no-embedding" not in calls[-1]

    monkeypatch.setenv("AISQUARE_BRAIN_EMBED_MODEL", "openai:text-embedding-3-small")
    brain._ensure("prj_x")
    assert "openai:text-embedding-3-small" in calls[-1]  # override honoured

    monkeypatch.delenv("AISQUARE_BRAIN_EMBED")
    monkeypatch.delenv("AISQUARE_BRAIN_EMBED_MODEL")
    brain._ensure("prj_x")
    assert "--no-embedding" in calls[-1]  # network-call-free default


def test_embed_knob_accepts_truthy_words(monkeypatch: pytest.MonkeyPatch) -> None:
    for on in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("AISQUARE_BRAIN_EMBED", on)
        assert brain.embeddings_enabled(), on
    for off in ("", "0", "false", "no", "off", "nope"):
        monkeypatch.setenv("AISQUARE_BRAIN_EMBED", off)
        assert not brain.embeddings_enabled(), off


def test_doctor_warns_when_embed_knob_mismatches_the_schema(
    runner: CliRunner, fake_gbrain: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_events(runner)
    runner.invoke(app, ["team", "distill"])  # embeddings off → vectorless brain
    monkeypatch.setenv("AISQUARE_BRAIN_EMBED", "1")  # now flip the knob on
    result = runner.invoke(app, ["doctor"])
    assert "created WITHOUT embeddings" in result.output  # doctor surfaces the no-op
    assert "team distill --all" in result.output  # and points to the real fix
