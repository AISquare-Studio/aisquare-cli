"""The brain layer: distiller outbox, watermark, recall — against a fake gbrain."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import brain
from aisquare.core.orchestrator import team_project
from aisquare.services import distill
from aisquare.services import team as team_service

_FAKE_GBRAIN = r"""# Schema-aware fake: `init` records the embedding choice into config.json
# exactly like real gbrain, and `query` (hybrid) REJECTS a vectorless brain --
# so the suite exercises the real create-time constraint instead of passing
# on a path that hard-fails for users.
#
# Python rather than /bin/sh: the product finds this through
# shutil.which("gbrain"), and Windows resolves that through PATHEXT, so the
# thing on PATH has to be something CreateProcess can start. See fake_gbrain
# for the per-platform launcher that fronts this file.
import os
import sys
from pathlib import Path


def home() -> Path:
    return Path(os.environ["GBRAIN_HOME"])


def main(argv: list[str]) -> int:
    if not argv:
        return 0
    command = argv[0]
    if command == "--version":
        # Asked without GBRAIN_HOME in the environment -- never resolve it here.
        print("gbrain 0.42.1.0")
        return 0
    if command == "init":
        pglite = home() / ".gbrain" / "brain.pglite"
        pglite.mkdir(parents=True, exist_ok=True)
        (pglite / "PG_VERSION").write_text("16\n", encoding="utf-8")
        with (home() / "init.log").open("a", encoding="utf-8") as log:
            log.write(" ".join(argv) + "\n")
        config = home() / ".gbrain" / "config.json"
        if "--no-embedding" in argv:
            config.write_text('{"embedding_disabled": true}', encoding="utf-8")
        else:
            config.write_text(
                '{"embedding_model": "openai:text-embedding-3-large"}', encoding="utf-8"
            )
        return 0
    if command == "put":
        if os.environ.get("FAKE_GBRAIN_FAIL") == "1":
            return 1
        slug = argv[1]
        pages = home() / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        # Bytes, not text: the page body carries whatever the distiller wrote,
        # and a locale codec must never get a vote (see the UTF-8 sweep).
        (pages / slug.replace("/", "_")).write_bytes(sys.stdin.buffer.read())
        with (home() / "puts.log").open("a", encoding="utf-8") as log:
            log.write(slug + "\n")
        return 0
    if command == "search":
        print(f"results for: {argv[1]}")
        return 0
    if command == "query":
        config = home() / ".gbrain" / "config.json"
        body = config.read_text(encoding="utf-8") if config.exists() else ""
        if "embedding_disabled" in body:
            print("hybrid query needs a vectorized brain", file=sys.stderr)
            return 1
        print(f"hybrid results for: {argv[1]}")
        return 0
    return 0


sys.exit(main(sys.argv[1:]))
"""


@pytest.fixture
def fake_gbrain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a scripted gbrain on PATH (keeping the inherited dirs for git etc.).

    The fake is Python, fronted by a launcher named so ``shutil.which`` finds
    it on either platform: a shebanged script on POSIX, a ``.cmd`` on Windows
    (which is how pip and npm put interpreted scripts on PATH -- PATHEXT makes
    ``which("gbrain")`` resolve it, and CreateProcess runs it through cmd.exe).
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    impl = bin_dir / "fake_gbrain.py"
    impl.write_text(_FAKE_GBRAIN, encoding="utf-8")
    if sys.platform == "win32":
        launcher = bin_dir / "gbrain.cmd"
        launcher.write_text(
            "@echo off\r\n" + f'"{sys.executable}" "{impl}" %*' + "\r\n", encoding="utf-8"
        )
    else:
        launcher = bin_dir / "gbrain"
        launcher.write_text(
            "#!/bin/sh\n" + f'exec "{sys.executable}" "{impl}" "$@"' + "\n", encoding="utf-8"
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Prepend rather than replace: the fake must win, but git and friends have
    # to stay reachable, and a hardcoded "/usr/bin:/bin" is not portable.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


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
    puts = (brain.brain_home(project.id) / "puts.log").read_text(encoding="utf-8").splitlines()
    assert any(slug.startswith("team/decision/") for slug in puts)
    assert any(slug.startswith("team/note/") for slug in puts)
    assert any(slug.startswith("team/task-done/") for slug in puts)
    assert not any("task-added" in slug for slug in puts)
    page = next(
        path
        for path in (brain.brain_home(project.id) / "pages").iterdir()
        if "decision" in path.name
    ).read_text(encoding="utf-8")
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
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty directory, not "/usr/bin:/bin": the point is a PATH with no
    # gbrain on it, and hardcoded POSIX dirs assert that only by accident on a
    # platform where they do not exist at all.
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
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
    puts_log.write_text("", encoding="utf-8")
    assert distill.drain(work_dir) == 0  # normal drain sees nothing new
    result = runner.invoke(app, ["--json", "team", "distill", "--all"])
    assert json.loads(result.stdout) == {"distilled": 3}  # rescan rebuilt them
    assert len(puts_log.read_text(encoding="utf-8").splitlines()) == 3


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
    assert "--embedding-model" in (brain.brain_home(project.id) / "init.log").read_text(
        encoding="utf-8"
    )


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


def test_lock_is_exclusive_across_handles_and_released_after(tmp_path: Path) -> None:
    """The contract both lock backends must satisfy.

    POSIX locks via ``fcntl.flock`` and Windows via ``msvcrt.locking``; this
    asserts the behaviour the brain depends on rather than either mechanism,
    so it runs — and means something — on both.
    """
    home = tmp_path / "brain"

    with brain._lock(home, wait_s=0) as first:
        assert first
        with brain._lock(home, wait_s=0) as second:
            assert not second  # a second holder must be refused, not queued

    with brain._lock(home, wait_s=0) as third:
        assert third  # and the lock is winnable again once released


def test_lock_gives_up_after_wait_s_rather_than_hanging(tmp_path: Path) -> None:
    home = tmp_path / "brain"
    with brain._lock(home, wait_s=0) as held:
        assert held
        started = time.monotonic()
        with brain._lock(home, wait_s=0.3) as waiter:
            assert not waiter
        assert time.monotonic() - started >= 0.3


def test_gbrain_output_is_decoded_as_utf8(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Same locale-codec trap as the snapshot packer, on the gbrain calls."""
    seen: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("aisquare.core.brain.shutil.which", lambda _name: "/usr/bin/gbrain")
    monkeypatch.setattr("aisquare.core.brain.subprocess.run", _capture)

    assert brain._run(tmp_path, ["put"], timeout=5) == "ok\n"
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
