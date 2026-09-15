"""The ``xr`` doctor row: the extra, port 8748, and the cached whisper model.

docs/plans/clixr.md §4 ("`aisquare doctor` gains one XR check … report and
suggest a fix, never fail hard") and §16, where the row is a line in the
definition of done.

**Absences are ok; only faults warn.** ``install.sh`` treats any amber row but
``brain`` as unexpected and exits 2, and ``tests/install/cell.sh`` asserts that
exact set from a wheel with no extras and no model cache on five distributions
— so a row that warned because the optional extra was simply not installed, or
because a model nobody had asked for was not yet downloaded, turned the whole
matrix red on a healthy machine. The tests in the first block below pin each
absence as ``ok`` with its install or pre-download line in the detail, and each
fault as ``warn`` with a fix.

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
    "model_loadable": lambda path: True,
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


def test_a_missing_extra_is_ok_and_names_the_modules_and_the_install_line() -> None:
    """An optional extra nobody installed is not a fault: ok, with the line that installs it.

    Which import is absent still goes on the line — under ``[dev]`` the web
    half is present and only ``faster_whisper`` is not, and an operator who
    reads "no faster_whisper" knows which half they are missing.
    """
    result = check(has_module=lambda module: module != "faster_whisper")

    assert result.status is CheckStatus.ok
    assert "faster_whisper" in result.detail
    assert "starlette" not in result.detail, "a module that IS installed was reported missing"
    assert "[xr]" in result.detail, "the install line travels in the detail of an ok row"
    assert result.fix is None


def test_a_base_install_with_nothing_cached_is_ok_which_is_what_the_install_matrix_needs() -> None:
    """The exact machine ``tests/install/cell.sh`` grades: a wheel with no extras, an empty cache.

    Its acceptance criterion is every row ok except ``brain``; ``install.sh``
    exits 2 on any other amber row. Both absences at once must therefore be
    one ok line carrying both remedies.
    """
    result = check(has_module=lambda module: False, model_dir=lambda model: None)

    assert result.status is CheckStatus.ok, result
    assert result.fix is None
    assert "[xr]" in result.detail
    assert "python -c" in result.detail, "the pre-download line is on the ok line too"


def test_a_backend_missing_its_platform_wheels_warns_with_a_reinstall() -> None:
    """faster-whisper present without ctranslate2 or onnxruntime IS a fault, not an absence.

    Both ship as platform wheels, so a pip that reported success can still
    leave an import that fails — and it fails at the first press, as a
    decode error, which is the worst place to learn it.
    """
    result = check(has_module=lambda module: module != "onnxruntime")

    assert result.status is CheckStatus.warn
    assert "onnxruntime" in result.detail
    assert "ctranslate2" not in result.detail, "a wheel that IS present was reported missing"
    assert result.fix is not None and "[xr]" in result.fix and "Reinstall" in result.fix


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


def test_an_uncached_model_is_ok_and_says_what_it_will_cost() -> None:
    """THE REASON THIS CHECK EXISTS: the only one of the three that fails late.

    A missing extra stops the command at import and a held port stops it at
    bind — both loudly, both at the moment you start it. An uncached model lets
    everything start perfectly and goes to the network at the first
    push-to-talk, which in a demo is the worst possible moment. It is still
    not a fault — nothing is broken, a download simply has not happened — so
    the verdict is ok and the pre-download line rides in the detail.
    """
    result = check(model_dir=lambda model: None)

    assert result.status is CheckStatus.ok
    assert "first push-to-talk downloads it" in result.detail
    assert speech.download_fix(speech.DEFAULT_MODEL) in result.detail
    assert result.fix is None


def test_a_cached_model_with_no_loadable_snapshot_warns_with_the_download_line() -> None:
    """An interrupted download leaves the directory: ``is_dir`` says cached, the load fails.

    That is worse than an empty cache, because the row would have said
    "cached" and the first press then fails to load instead of downloading.
    """
    result = check(model_loadable=lambda path: False)

    assert result.status is CheckStatus.warn
    assert "no loadable snapshot" in result.detail
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


def test_a_fault_and_an_absence_together_warn_for_the_fault_and_still_name_the_absence() -> None:
    """One line, everything on it: the held port sets the verdict, the install line stays."""
    result = check(has_module=lambda module: False, port_in_use=lambda port: True)

    assert result.status is CheckStatus.warn
    assert "xr extra is not installed" in result.detail
    assert "[xr]" in result.detail, "the install line stays on the detail beside the fault"
    assert f"port {diagnostics._XR_PORT} is in use" in result.detail
    assert result.fix is not None
    assert "--port" in result.fix
    assert "[xr]" not in result.fix, "an absence is not a fault, so it is not in the fix"


# --- failing open -----------------------------------------------------------------------------


def test_every_seam_raising_is_an_ok_not_evaluated_line() -> None:
    """The property that protects every row after this one.

    ``doctor`` runs the checks in a list and renders the result; an exception
    escaping here would take the whole command down, which is a far worse
    outcome than not knowing whether port 8748 is free. Warn is for "I looked
    and it is wrong"; this is "I could not look".
    """
    result = check(
        has_module=explodes, port_in_use=explodes, model_dir=explodes, model_loadable=explodes
    )

    assert result.status is CheckStatus.ok, "a broken seam failed the machine's health"
    assert "not evaluated" in result.detail
    assert "seam exploded" in result.detail, "failing open must say what it could not read"
    assert "aisquare xr reports" in result.detail, "failing open must say what it cost"
    assert result.fix is None


@pytest.mark.parametrize("seam", ["has_module", "port_in_use", "model_dir", "model_loadable"])
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


def test_a_snapshot_is_loadable_only_when_it_holds_the_weights(tmp_path: Path) -> None:
    """``model.bin`` under some ``snapshots/<revision>/`` is what a complete download leaves.

    An interrupted one leaves the same directory with the weights absent or
    still ``*.incomplete`` under ``blobs/`` — which is why ``is_dir`` on the
    model directory cannot answer this and a second look is needed.
    """
    model_dir = tmp_path / f"models--Systran--faster-whisper-{speech.DEFAULT_MODEL}"
    snapshot = model_dir / "snapshots" / "0123abcd"
    snapshot.mkdir(parents=True)
    (model_dir / "blobs").mkdir()

    assert diagnostics._whisper_snapshot_loadable(model_dir) is False, "no weights yet"
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    assert diagnostics._whisper_snapshot_loadable(model_dir) is False, "config alone is not a model"
    (snapshot / "model.bin").write_bytes(b"\x00")
    assert diagnostics._whisper_snapshot_loadable(model_dir) is True
    assert diagnostics._whisper_snapshot_loadable(tmp_path / "absent") is False, "fails closed"


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


def test_the_row_is_ok_on_a_machine_that_merely_lacks_the_extra_and_the_model(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the CLI, with the real seams except the two that vary by machine.

    The extra may or may not be installed where the suite runs and the model
    may or may not be cached, so both are pinned to "absent" here and the port
    is pinned free; what is measured is that ``aisquare --json doctor`` reports
    the row ok — the shape ``install.sh`` grades.
    """
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setattr(diagnostics, "_has_module", lambda module: False)
    monkeypatch.setattr(diagnostics, "_xr_port_in_use", lambda port, host="127.0.0.1": False)

    rows = {
        row["name"]: row
        for row in json.loads(
            runner.invoke(app, ["--json", "doctor"], catch_exceptions=False).stdout
        )
    }

    assert rows["xr"]["status"] == "ok", rows["xr"]
    assert "[xr]" in rows["xr"]["detail"]
