"""Offline self-tests of the harness: no browser, no ttyd, no aisquare — seconds to run.

    e2e/.venv/bin/python -m pytest -c e2e/pytest.ini e2e/selftest

They pin the rules the nightly's verdict depends on, so a harness bug cannot quietly
turn a broken product green (or a healthy one red).
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from e2e.contracts import Environment, Journey, Run, Step, load, save, verdict_for
from e2e.runner.machine import RealHomeError, _real_home, _refuse_real_home
from e2e.runner.recorder import Recorder
from e2e.runner.server import StaticServer
from e2e.runner.terminal import TRACEBACK, Screen


def _journey(status: str) -> Journey:
    return Journey(id="J", title="t", story="s", status=status, duration_s=0.0)  # type: ignore[arg-type]


# --- verdicts ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "env_failed", "expected"),
    [
        (["passed", "passed"], False, "PASSED"),
        (["passed", "failed"], False, "RC_FAILED"),
        (["failed", "error"], False, "GATE_ERROR"),  # a harness fault outranks a product failure
        (["failed"], True, "ENV_FAILED"),  # and the environment outranks both
        ([], False, "GATE_ERROR"),  # a run that tested nothing is never PASSED
        (["skipped", "skipped"], False, "GATE_ERROR"),
    ],
)
def test_verdict_precedence(statuses: list[str], env_failed: bool, expected: str) -> None:
    assert verdict_for([_journey(s) for s in statuses], env_failed=env_failed) == expected


def test_run_json_round_trips(tmp_path: Path) -> None:
    step = Step(id="J01.S01.x", title="x", status="passed", started_at="t", duration_s=1.5)
    run = Run(
        run_id="1",
        started_at="a",
        finished_at="b",
        verdict="PASSED",
        env=Environment(os="o", runner="local", track="main", agent="a", aisquare="0", commit="c"),
        journeys=[
            Journey(id="J01", title="t", story="s", status="passed", duration_s=2.0, steps=[step])
        ],
    )
    save(run, tmp_path / "run.json")
    assert load(tmp_path / "run.json") == run


def test_load_refuses_other_schemas(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text('{"schema": "something/else"}', encoding="utf-8")
    with pytest.raises(ValueError, match="not an"):
        load(tmp_path / "run.json")


# --- the machine never becomes the developer's home ---------------------------------------


def test_the_real_home_is_refused() -> None:
    with pytest.raises(RealHomeError):
        _refuse_real_home(_real_home())


def test_a_parent_of_the_real_home_is_refused() -> None:
    with pytest.raises(RealHomeError):
        _refuse_real_home(_real_home().parent)


def test_a_scratch_home_is_accepted(tmp_path: Path) -> None:
    _refuse_real_home(tmp_path / "home")  # no exception


# --- the loopback server only serves its routes ------------------------------------------


def test_server_routes_and_refuses_traversal(tmp_path: Path) -> None:
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    srv = StaticServer({"/pub/": tmp_path / "pub"}).start()
    try:
        with urllib.request.urlopen(srv.url("/pub/a.txt")) as response:
            assert response.read() == b"hello"
        for path in ("/pub/../secret.txt", "/pub/%2e%2e/secret.txt", "/secret.txt", "/"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(srv.url(path))
            assert caught.value.code == 404, path
    finally:
        srv.close()


# --- reading screens ------------------------------------------------------------------------


def _screen(lines: list[str]) -> Screen:
    return Screen(
        cols=40,
        rows=len(lines),
        lines=lines,
        alt=True,
        frames=0,
        bsu=False,
        since_write_ms=1e9,
        prompts=0,
        exit_codes=[],
        ws="open",
    )


def test_find_respects_a_column_band() -> None:
    screen = _screen([" manager      │ coder-1 is busy", " coder-1      │"])
    assert screen.find("coder-1") == (16, 0)  # anywhere: the first match is in the pane
    assert screen.find("coder-1", cols=(0, 14)) == (1, 1)  # the sidebar only


@pytest.mark.parametrize(
    "text",
    [
        "Traceback (most recent call last):",
        '  File "/x/aisquare/cli/app.py", line 12, in main',
        "│ ❱ 12 │ raise ValueError",  # Rich's traceback marker
    ],
)
def test_a_crash_on_screen_is_detected(text: str) -> None:
    assert TRACEBACK.search(text)


def test_ordinary_screens_are_not_crashes() -> None:
    assert not TRACEBACK.search("✓ doctor: 23 checks, 1 not ok · File saved")


# --- the recorder's failure semantics ---------------------------------------------------------


def test_a_failed_check_fails_the_step_and_the_journey(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    with (
        pytest.raises(AssertionError, match=re.escape("J9.S01.broken: the thing")),
        rec.journey("J9", "t", "s", terminal=None) as j,
        j.step("S01", "broken") as s,
    ):
        s.check("the thing", False)
    (journey,) = rec.journeys
    assert journey.status == "failed"
    assert journey.steps[0].status == "failed"
    assert journey.steps[0].checks[0].ok is False


def test_an_unexpected_exception_is_a_harness_error_not_a_product_failure(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    with (
        pytest.raises(RuntimeError),
        rec.journey("J9", "t", "s", terminal=None) as j,
        j.step("S01", "boom"),
    ):
        raise RuntimeError("ttyd died")
    assert rec.journeys[0].status == "error"
    assert rec.journeys[0].steps[0].status == "error"
    assert verdict_for(rec.journeys) == "GATE_ERROR"


def test_a_skipped_journey_is_recorded_with_its_reason(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    rec.skipped("J9", "t", "s", why="J1 did not pass")
    assert rec.journeys[0].status == "skipped"
    assert rec.journeys[0].error == "J1 did not pass"
