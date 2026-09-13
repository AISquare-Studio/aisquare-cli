"""The ``xr`` doctor row: the extra, port 8748, and the cached whisper model.

docs/plans/clixr.md §4 ("`aisquare doctor` gains one XR check … report and
suggest a fix, never fail hard") and §16, where the row is a line in the
definition of done.

Every seam is injected, so nothing here installs a package, opens a port for
longer than one connect, or looks at the operator's real Hugging Face cache —
with two deliberate exceptions, both at the bottom: the port probe is exercised
once against a REAL bound socket, because "is something listening" is the one
fact a fake cannot vouch for, and the real check is run once with its real
seams to pin that it neither raises nor writes anything.

``tests/test_doctor_does_not_create_state.py`` already pins that ``doctor``
creates no home. What this file adds is the XR flavour of the same promise, and
one property that matters more than any single verdict: **the check fails
open**. A diagnostic that crashes takes down every row after it, so a doctor
that cannot answer must say so in an ok line rather than raise.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics
from aisquare.services.xr import speech

# --- seams -----------------------------------------------------------------------------

#: A machine where everything is right. Each test below overrides exactly one
#: of these, so the thing being measured is the only difference from healthy.
HEALTHY = {
    "has_module": lambda module: True,
    "port_in_use": lambda port: False,
    "model_dir": lambda model: Path("/cache") / f"models--Systran--faster-whisper-{model}",
}


def check(**overrides: object) -> DoctorCheck:
    """Run the check on a healthy machine with ``overrides`` applied."""
    seams = {**HEALTHY, **overrides}
    return diagnostics._check_xr(**seams)  # type: ignore[arg-type]


def explodes(*_args: object, **_kwargs: object) -> bool:
    raise RuntimeError("seam exploded")


@pytest.fixture(autouse=True)
def default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the operator's own model export.

    ``isolated_home`` clears the other ambient knobs for the same reason and
    does not know about this one: with ``AISQUARE_XR_WHISPER_MODEL=small.en``
    exported in the shell that runs the suite, every assertion about the
    default model below would be measuring that shell instead of the code.
    """
    monkeypatch.delenv(speech.ENV_MODEL, raising=False)


# --- the healthy line ---------------------------------------------------------------------


def test_everything_present_is_ok_with_no_fix() -> None:
    result = check()

    assert result.name == "xr"
    assert result.status is CheckStatus.ok
    assert result.fix is None
    assert "xr extra installed" in result.detail
    assert f"port {diagnostics._XR_PORT} free" in result.detail
    assert speech.DEFAULT_MODEL in result.detail


# --- the three facts, one at a time ---------------------------------------------------------


def test_a_missing_extra_warns_and_names_the_modules_that_are_missing() -> None:
    """Which import is absent is the difference between a broken install and no install."""
    result = check(has_module=lambda module: module != "faster_whisper")

    assert result.status is CheckStatus.warn
    assert "faster_whisper" in result.detail
    assert "starlette" not in result.detail, "a module that IS installed was reported missing"
    assert result.fix is not None and "[xr]" in result.fix


def test_a_missing_extra_still_reports_the_facts_it_could_establish() -> None:
    """One problem must not blank the other two answers.

    A doctor that stops at the first bad news makes an operator fix, re-run,
    fix, re-run. Everything it knows goes on the line.
    """
    result = check(has_module=lambda module: False)

    assert f"port {diagnostics._XR_PORT} free" in result.detail
    assert f"whisper model {speech.DEFAULT_MODEL} cached" in result.detail


def test_a_held_port_warns_with_the_fix_naming_the_flag() -> None:
    result = check(port_in_use=lambda port: True)

    assert result.status is CheckStatus.warn
    assert f"port {diagnostics._XR_PORT} is in use" in result.detail
    assert result.fix is not None
    assert "already running" in result.fix and "--port" in result.fix


def test_an_uncached_model_warns_and_says_what_it_will_cost() -> None:
    """THE REASON THIS CHECK EXISTS: the only one of the three that fails late.

    A missing extra stops the command at import and a held port stops it at
    bind — both loudly, both at the moment you start it. An uncached model lets
    everything start perfectly and goes to the network at the first
    push-to-talk, which in a demo is the worst possible moment.
    """
    result = check(model_dir=lambda model: None)

    assert result.status is CheckStatus.warn
    assert "first push-to-talk downloads it" in result.detail
    assert result.fix == speech.download_fix(speech.DEFAULT_MODEL)


def test_the_configured_model_is_the_one_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setenv(speech.ENV_MODEL, "small.en")

    def record(model: str) -> Path:
        asked.append(model)
        return Path("/cache/small")

    result = check(model_dir=record)

    assert asked == ["small.en"], "the check looked for a model the operator did not configure"
    assert "small.en" in result.detail


def test_an_unsupported_model_name_is_named_back_rather_than_silently_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in a shell profile must be visible at the moment you go looking for it.

    Reporting the default here would hide the operator's own export — they would
    read "base.en cached", believe the row, and keep the broken variable.
    """
    monkeypatch.setenv(speech.ENV_MODEL, "large-v3")

    result = check(model_dir=lambda model: pytest.fail(f"looked for {model!r} anyway"))

    assert result.status is CheckStatus.warn
    assert "large-v3" in result.detail
    assert result.fix is not None
    assert "base.en" in result.fix and "small.en" in result.fix


def test_several_problems_are_reported_together_with_every_fix() -> None:
    result = check(has_module=lambda module: False, port_in_use=lambda port: True)

    assert result.status is CheckStatus.warn
    assert "xr extra is not installed" in result.detail
    assert f"port {diagnostics._XR_PORT} is in use" in result.detail
    assert result.fix is not None
    assert "[xr]" in result.fix and "--port" in result.fix


# --- failing open -----------------------------------------------------------------------------


def test_every_seam_raising_is_an_ok_not_evaluated_line() -> None:
    """The property that protects every row after this one.

    ``doctor`` runs the checks in a list and renders the result; an exception
    escaping here would take the whole command down, which is a far worse
    outcome than not knowing whether port 8748 is free. Warn is for "I looked
    and it is wrong"; this is "I could not look".
    """
    result = check(has_module=explodes, port_in_use=explodes, model_dir=explodes)

    assert result.status is CheckStatus.ok, "a broken seam failed the machine's health"
    assert "not evaluated" in result.detail
    assert "seam exploded" in result.detail, "failing open must say what it could not read"
    assert "aisquare xr reports" in result.detail, "failing open must say what it cost"
    assert result.fix is None


@pytest.mark.parametrize("seam", ["has_module", "port_in_use", "model_dir"])
def test_any_single_seam_raising_fails_open(seam: str) -> None:
    """Each seam separately, so one lucky short-circuit cannot cover the others."""
    result = check(**{seam: explodes})

    assert result.status is CheckStatus.ok
    assert "not evaluated" in result.detail, f"{seam} raising escaped the guard"


def test_the_model_name_raising_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one seam that is NOT a parameter: reading the environment."""
    monkeypatch.setattr(speech, "model_name", explodes)

    result = check()

    assert result.status is CheckStatus.ok
    assert "not evaluated" in result.detail


# --- the port probe, once for real --------------------------------------------------------------


@pytest.fixture
def listening() -> Iterator[int]:
    """A real socket, listening on a real ephemeral port, closed afterwards."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield int(server.getsockname()[1])


def test_the_port_probe_sees_a_real_listener(listening: int) -> None:
    assert diagnostics._xr_port_in_use(listening) is True


def test_the_port_probe_sees_a_real_free_port(listening: int) -> None:
    """The negative control, on a port that was just released.

    Without this the probe could return True unconditionally and the test above
    would still pass — and a doctor that always says "in use" would send every
    operator chasing a process that is not there.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = int(probe.getsockname()[1])

    assert free != listening
    assert diagnostics._xr_port_in_use(free) is False


def test_the_port_probe_does_not_bind_the_port_it_reports_on(listening: int) -> None:
    """A diagnostic must not take the resource it is reporting on, even briefly.

    Binding to find out would race the server it is about to tell you to start.
    Proven by probing a free port and then binding it — if the probe had held
    it, this would fail with "address already in use".
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free = int(probe.getsockname()[1])

    assert diagnostics._xr_port_in_use(free) is False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as after:
        after.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        after.bind(("127.0.0.1", free))  # would raise if the probe still held it


# --- where the model cache is looked for ----------------------------------------------------


def test_the_cache_follows_hf_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    cached = tmp_path / "hf" / "hub" / f"models--Systran--faster-whisper-{speech.DEFAULT_MODEL}"

    assert diagnostics._whisper_model_dir(speech.DEFAULT_MODEL) is None
    cached.mkdir(parents=True)
    assert diagnostics._whisper_model_dir(speech.DEFAULT_MODEL) == cached


def test_the_cache_falls_back_to_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert diagnostics._hf_hub_cache() == tmp_path / ".cache" / "huggingface" / "hub"


def test_a_file_where_the_model_directory_should_be_is_not_a_cached_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``is_dir``, not ``exists`` — a stray file must not read as a downloaded model."""
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    stray = tmp_path / "hf" / "hub" / f"models--Systran--faster-whisper-{speech.DEFAULT_MODEL}"
    stray.parent.mkdir(parents=True)
    stray.write_text("not a model", encoding="utf-8")

    assert diagnostics._whisper_model_dir(speech.DEFAULT_MODEL) is None


# --- the real check, with its real seams ------------------------------------------------------


def test_the_real_check_neither_raises_nor_writes_anything(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run on whatever this machine actually has, asserting only what must hold everywhere.

    The verdict is deliberately not asserted — it depends on whether the extra
    is installed here, which is exactly the thing the row is for. What must hold
    on every machine is that it answers, names itself, and creates nothing.
    """
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

    result = diagnostics._check_xr()

    assert result.name == "xr"
    assert result.status is not CheckStatus.fail, "the XR row must never fail a machine"
    assert result.detail
    assert not (tmp_path / "hf").exists(), "the check created the cache it was looking for"
    assert not isolated_home.exists(), "the check created the aisquare home"


# --- the CLI surface --------------------------------------------------------------------------


def test_doctor_emits_the_xr_row_in_text_and_json(runner: CliRunner) -> None:
    text = runner.invoke(app, ["doctor"], catch_exceptions=False).output
    payload = json.loads(runner.invoke(app, ["--json", "doctor"], catch_exceptions=False).stdout)

    names = [row["name"] for row in payload]
    assert "xr" in names, names
    assert " xr: " in text, text
    assert names.index("fleet") < names.index("xr"), (
        "the xr row must sit after fleet, where it was added"
    )


def test_the_xr_row_carries_a_fix_whenever_it_warns(runner: CliRunner) -> None:
    """The doctor idiom in one assertion: a warning without a fix is a complaint."""
    rows = {
        row["name"]: row
        for row in json.loads(
            runner.invoke(app, ["--json", "doctor"], catch_exceptions=False).stdout
        )
    }

    xr = rows["xr"]
    assert xr["status"] in {"ok", "warn"}, "the XR row must never fail a machine"
    if xr["status"] == "warn":
        assert xr["fix"], xr
