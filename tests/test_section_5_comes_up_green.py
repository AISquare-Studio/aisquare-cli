"""§5 of the cutover calls `doctor` "the one command that proves it green".

Nothing held the green state itself. `test_doctor_live_proxy.py` pins the proxy
ROW — up, down, tracing on, tracing off, and `--live`'s promise to dial — and it
is thorough about that row. But the operator's question at 08:00 is not "is the
proxy row correct", it is "did the whole section come up green and did the
command exit 0", and that composite had no test because until this shift there
was no healthy proxy to assemble it against. Every existing green assertion
injects a `ProxyProbe(True, ...)` somewhere in the middle.

@8dd460fb's `tests/proxy_stub` changed that: a real `/health` on a loopback
port, so the state can be BUILT by running the runbook's own commands and then
observed through the CLI's own JSON. Measured that way at 9151637, all five
explainability rows come up `ok` and `doctor` exits 0 — the first time tonight
anyone has seen §5's claim be true rather than assumed.

WHAT THIS ASSERTS AND WHY NOT MORE. The exit code and the absence of any `fail`
row, not `status == ok` per row. `explainability sdk` is `warn` on a machine
without the SDK installed and `ok` on one with it, so pinning it green would
make this test a statement about the developer's venv rather than about the
CLI. `warn` is also not what stops an operator: `doctor` exits on `fail`, which
is the signal §5 actually tells them to read.

The `config` row is pinned in more detail because §5 prints it verbatim and
tells the reader to compare line for line — target, key variable and the three
identities. A row that quietly stopped naming the identities would leave that
block wrong in the one document a human executes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from tests.proxy_stub import healthy_proxy

_KEY_VAR = "GREENSTATE_KEY"

#: The explainability rows §5 prints. Named rather than pattern-matched: if a
#: row is renamed or dropped, the runbook's verbatim block is stale and this
#: should say so instead of silently checking four rows out of five.
_SECTION = (
    "explainability",
    "explainability sdk",
    "explainability config",
    "explainability redaction",
    "explainability proxy",
)


def _configure(runner: CliRunner, proxy_url: str) -> None:
    """Build the state by running §4, not by writing a config file."""
    for argv in (
        ["init", "--yes"],
        [
            "explainability",
            "enable",
            "--target",
            "prod",
            "--gateway-url",
            "https://gateway.invalid",
            "--key-env",
            _KEY_VAR,
            "--proxy-url",
            proxy_url,
        ],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, f"setup `aisquare {' '.join(argv)}` failed: {result.output}"


def _doctor(runner: CliRunner) -> tuple[int, dict[str, dict[str, Any]]]:
    result = runner.invoke(app, ["--json", "doctor"], catch_exceptions=False)
    payload = json.loads(result.stdout)
    assert payload, "doctor produced no checks at all"
    return result.exit_code, {row["name"]: row for row in payload}


def test_a_correctly_wired_machine_comes_up_green(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5's claim, assembled from the real commands and a real /health."""
    monkeypatch.setenv(_KEY_VAR, "not-a-real-key")

    with healthy_proxy() as proxy_url:
        _configure(runner, proxy_url)
        code, rows = _doctor(runner)

    missing = [name for name in _SECTION if name not in rows]
    assert not missing, f"§5 prints rows that doctor no longer emits: {missing}"

    failed = {name: rows[name]["detail"] for name in _SECTION if rows[name]["status"] == "fail"}
    assert not failed, f"the section is not green: {failed}"
    assert code == 0, f"doctor exited {code} on a correctly wired machine"


def test_the_proxy_row_names_the_proxy_it_reached(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "healthy" without a URL cannot be checked against `ss -ltnp`.

    §5 carries @8dd460fb's warning that a green proxy row proves *a* proxy is
    answering, not that the operator started it — advice that is only followable
    if the row says WHICH proxy.
    """
    monkeypatch.setenv(_KEY_VAR, "not-a-real-key")

    with healthy_proxy() as proxy_url:
        _configure(runner, proxy_url)
        _, rows = _doctor(runner)
        detail = rows["explainability proxy"]["detail"]

    assert proxy_url in detail, detail
    assert "healthy" in detail, detail


def test_the_config_row_still_carries_what_section_5_prints(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5 prints this line verbatim and says to compare line for line."""
    monkeypatch.setenv(_KEY_VAR, "not-a-real-key")

    with healthy_proxy() as proxy_url:
        _configure(runner, proxy_url)
        _, rows = _doctor(runner)
        detail = rows["explainability config"]["detail"]

    assert "prod" in detail, detail
    assert f"${_KEY_VAR}" in detail, detail
    assert "identities:" in detail, detail
    for role in ("aisquare-planner", "aisquare-coder", "aisquare-runner"):
        assert role in detail, detail


def test_the_same_machine_with_the_proxy_down_is_not_green(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control, and the only reason the green assertion means anything.

    Identical configuration; the single difference is that nothing is listening.
    The port is one the stub has already released, so this dials a real closed
    port rather than trusting a patched prober — and it is never 9090, which on
    this machine holds a long-lived proxy that is not ours.
    """
    monkeypatch.setenv(_KEY_VAR, "not-a-real-key")

    with healthy_proxy() as proxy_url:
        pass  # the server is shut down on exit; the port is now closed

    _configure(runner, proxy_url)
    code, rows = _doctor(runner)

    assert rows["explainability proxy"]["status"] == "fail", rows["explainability proxy"]
    assert code != 0, "doctor reported success with the proxy down"


def test_the_section_is_absent_until_the_operator_configures_it(runner: CliRunner) -> None:
    """Nothing ships before the user has configured it — including diagnostics.

    Also the empty-ratchet guard for the rows above: if these names vanished
    from doctor entirely, every `not failed` assertion would pass over an empty
    section. Here their ABSENCE is the assertion, so the two directions cannot
    both be satisfied by a doctor that stopped emitting them.
    """
    result = runner.invoke(app, ["init", "--yes"])
    assert result.exit_code == 0, result.output

    _, rows = _doctor(runner)

    assert "explainability" in rows, "the always-on switch row is gone too"
    for name in _SECTION[1:]:
        assert name not in rows, f"{name} is reported before anything is configured"
