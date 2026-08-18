"""`status` prints `spool: N queued` and the buffer lives in `queue/`.

The word is right and it is not the directory. `insight_sweeper` says spool,
"drain the spool" says spool, this counter says spool — and the records are in
`~/.aisquare/explainability/queue/`. Nothing shipped points at a wrong path
(@9bbc8ed7 swept both runbooks, the README and CONTRIBUTING and found none), so
there is no reference to correct. What was missing is that THE TOOL NEVER SAYS
WHERE.

That cost ninety minutes tonight and produced a false claim on the board — "the
spool is empty" while the record sat on disk — because the search was for a
directory assembled from the vocabulary rather than read off the filesystem. An
operator debugging a quiet client lane at 08:05 will do the same thing.

TWO DECISIONS PINNED HERE RATHER THAN LEFT INCIDENTAL:

THE EMPTY CASE NAMES IT TOO. "Show the path only when there is something in it"
is the version that fails the person it exists for: nobody goes looking in a
buffer they believe is full. `0 queued` is exactly when someone wants to check
whether the directory is empty or whether they are looking in the wrong place.

IT GOES ON THE EXISTING LINE. This is one path on a line an operator already
reads, not a new line — so the assertion is that the path shares a line with the
counters, which is the precise form of "did not get noisier" and does not break
when some other line is added elsewhere.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config


def _configure() -> None:
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.example"
    config.explainability.targets = {
        config.explainability.target: ExplainabilityTarget(gateway_url="https://gateway.example")
    }
    save_config(config)
    insights.reset_cache()


@pytest.fixture(autouse=True)
def _fresh() -> None:
    insights.reset_cache()


def _spool_line(output: str) -> str:
    """The one line carrying the counters, or "" if it is gone."""
    for line in output.splitlines():
        if line.strip().startswith("spool:"):
            return line
    return ""


def test_the_counter_line_names_the_directory_it_counts(runner: CliRunner) -> None:
    """The path, not the word — the point is that a reader can copy it."""
    _configure()
    insights.record_prompt("something to queue", session_id="s1", project_id="p1")

    result = runner.invoke(app, ["explainability", "status"])

    assert result.exit_code == 0, result.output
    line = _spool_line(result.stdout)
    assert "1 queued" in line, result.stdout
    assert str(outbox.queue_dir()) in line, (
        "the counter names a buffer and not its location, which is the mismatch "
        f"this file exists for: {line!r}"
    )


def test_the_empty_case_names_it_too(runner: CliRunner) -> None:
    """A quiet lane is WHEN someone goes looking, so this is the case that matters."""
    _configure()

    result = runner.invoke(app, ["explainability", "status"])

    assert result.exit_code == 0, result.output
    line = _spool_line(result.stdout)
    assert "0 queued" in line, result.stdout
    assert str(outbox.queue_dir()) in line, line


def test_the_path_shares_the_line_with_the_counters(runner: CliRunner) -> None:
    """ "Did not get noisier", in the form that survives unrelated additions.

    A line-count assertion would fail the day somebody adds a row elsewhere in
    `status`; this fails only if the path moves off the line it belongs to.
    """
    _configure()

    output = runner.invoke(app, ["explainability", "status"]).stdout

    carrying = [line for line in output.splitlines() if str(outbox.queue_dir()) in line]
    assert len(carrying) == 1, f"the path appears on {len(carrying)} lines: {carrying}"
    assert carrying[0].strip().startswith("spool:"), carrying[0]


def test_json_carries_the_path_as_a_field(runner: CliRunner) -> None:
    """A script must locate the buffer without parsing a sentence.

    It goes inside `shipping`, beside the counters it belongs to, because that
    object is already the one place those three integers live — a second home
    for the same subject would give the payload two answers.
    """
    _configure()

    result = runner.invoke(app, ["--json", "explainability", "status"])

    assert result.exit_code == 0, result.output
    shipping = json.loads(result.stdout)["shipping"]
    assert shipping["queue_dir"] == str(outbox.queue_dir()), shipping


def test_an_unresolvable_path_costs_the_path_and_not_the_command(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open: this is decoration on a status line.

    `status`'s exit code has one documented meaning — tracing on and the proxy
    refusing — and a directory that cannot be resolved must not borrow it.
    """
    _configure()
    monkeypatch.setattr(outbox, "queue_dir", lambda: (_ for _ in ()).throw(OSError("no home")))

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=True)

    assert result.exit_code == 0, result.output
    assert "0 queued" in _spool_line(result.stdout), result.stdout
    machine = runner.invoke(app, ["--json", "explainability", "status"])
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.stdout)["shipping"]["queue_dir"] is None
