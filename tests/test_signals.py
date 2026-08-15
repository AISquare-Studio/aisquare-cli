"""#23: first-class signals — named board states, structured events, no grep.

The incident this retires: a watcher substring-matching "READY" in free text
fired on a note saying "NOT READY". Signals are set/read by name, their
events carry structured payload fields, and consumers never touch prose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import store_session

CODER = "bbbb2222-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def _start(runner: CliRunner, work: Path) -> None:
    payload = json.dumps({"cwd": str(work), "session_id": CODER, "source": "startup"})
    result = runner.invoke(
        app, ["hook", "session-start"], input=payload, env={"AISQUARE_ROLE": "coder"}
    )
    assert result.exit_code == 0


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_signal_set_read_list_round_trip(runner: CliRunner, work_dir: Path) -> None:
    _start(runner, work_dir)
    set_result = runner.invoke(
        app, ["--json", "team", "signal", "fold-ready", "on", "--as", "bbbb2222"]
    )
    assert set_result.exit_code == 0, set_result.output
    payload = json.loads(set_result.stdout)
    assert payload["name"] == "fold-ready" and payload["value"] == "on"
    assert payload["prev"] is None and payload["delivered"] is True
    assert payload["seq"] > 0

    read = runner.invoke(app, ["--json", "team", "signal", "fold-ready"])
    got = json.loads(read.stdout)
    assert got["value"] == "on" and got["set_by"] == CODER and got["seq"] == payload["seq"]

    human = runner.invoke(app, ["team", "signal", "fold-ready"])
    assert "fold-ready = on" in _flat(human.output) and "bbbb2222" in _flat(human.output)

    listing = json.loads(runner.invoke(app, ["--json", "team", "signals"]).stdout)
    assert [s["name"] for s in listing] == ["fold-ready"]


def test_signal_overwrite_carries_prev_and_structured_event(
    runner: CliRunner, work_dir: Path
) -> None:
    _start(runner, work_dir)
    runner.invoke(app, ["team", "signal", "fold-ready", "off", "--as", "bbbb2222"])
    second = json.loads(
        runner.invoke(
            app, ["--json", "team", "signal", "fold-ready", "on", "--as", "bbbb2222"]
        ).stdout
    )
    assert second["prev"] == "off"

    events = json.loads(runner.invoke(app, ["--json", "team", "log", "--kind", "signal"]).stdout)
    assert len(events) == 2
    latest: dict[str, Any] = events[-1]["payload"]
    assert latest["name"] == "fold-ready" and latest["value"] == "on"
    assert latest["prev"] == "off" and latest["set_by"] == CODER
    assert events[0]["payload"]["prev"] is None  # first set had no prior value
    # The human text stays readable prose, structured fields ride alongside.
    assert events[-1]["payload"]["text"] == "fold-ready: on (was off)"


def test_signal_persists_across_store_sessions(runner: CliRunner, work_dir: Path) -> None:
    _start(runner, work_dir)
    runner.invoke(app, ["team", "signal", "phase", "2", "--as", "bbbb2222"])
    with store_session():  # a full store open/close cycle in between
        pass
    read = json.loads(runner.invoke(app, ["--json", "team", "signal", "phase"]).stdout)
    assert read["value"] == "2"


def test_the_literal_incident_a_ready_watcher_ignores_not_ready_prose(
    runner: CliRunner, work_dir: Path
) -> None:
    _start(runner, work_dir)
    runner.invoke(app, ["note", "heads up: definitely NOT READY yet", "--as", "bbbb2222"])
    runner.invoke(app, ["note", "READY or not, review the doc", "--as", "bbbb2222"])
    watched = json.loads(runner.invoke(app, ["--json", "team", "log", "--kind", "signal"]).stdout)
    ready_hits = [e for e in watched if e["payload"].get("name") == "ready"]
    assert ready_hits == []  # prose flowed past; the watcher stayed silent

    runner.invoke(app, ["team", "signal", "ready", "yes", "--as", "bbbb2222"])
    watched = json.loads(runner.invoke(app, ["--json", "team", "log", "--kind", "signal"]).stdout)
    ready_hits = [e for e in watched if e["payload"].get("name") == "ready"]
    assert len(ready_hits) == 1 and ready_hits[0]["payload"]["value"] == "yes"


def test_signal_watcher_cursor_flow_with_since_seq(runner: CliRunner, work_dir: Path) -> None:
    _start(runner, work_dir)
    first = json.loads(
        runner.invoke(app, ["--json", "team", "signal", "phase", "1", "--as", "bbbb2222"]).stdout
    )
    runner.invoke(app, ["note", "noise between polls", "--as", "bbbb2222"])
    runner.invoke(app, ["team", "signal", "phase", "2", "--as", "bbbb2222"])
    delta = json.loads(
        runner.invoke(
            app,
            ["--json", "team", "log", "--kind", "signal", "--since-seq", str(first["seq"])],
        ).stdout
    )
    assert [e["payload"]["value"] for e in delta] == ["2"]  # only what changed since


def test_signal_receipts_verify_like_any_write(runner: CliRunner, work_dir: Path) -> None:
    _start(runner, work_dir)
    signal = json.loads(
        runner.invoke(app, ["--json", "team", "signal", "gate", "open", "--as", "bbbb2222"]).stdout
    )
    verified = runner.invoke(app, ["team", "verify", str(signal["seq"]), "--as", "bbbb2222"])
    assert verified.exit_code == 0, verified.output
    assert "gate: open" in _flat(verified.output)


def test_signal_validation_rejects_non_tokens(runner: CliRunner, work_dir: Path) -> None:
    _start(runner, work_dir)
    bad_name = runner.invoke(
        app, ["--json", "team", "signal", "Fold Ready", "on", "--as", "bbbb2222"]
    )
    assert bad_name.exit_code == 1
    assert json.loads(bad_name.stdout)["error"] == "invalid_signal"
    bad_value = runner.invoke(
        app, ["--json", "team", "signal", "fold-ready", "not ready", "--as", "bbbb2222"]
    )
    assert bad_value.exit_code == 1
    assert json.loads(bad_value.stdout)["error"] == "invalid_signal"
    missing = runner.invoke(app, ["--json", "team", "signal", "never-set"])
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error"] == "not_found"
