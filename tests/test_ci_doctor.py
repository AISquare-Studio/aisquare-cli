"""``doctor`` on the CI test bed: every question a developer would ask, one line each.

Off is ``ok`` and touches nothing. On, the checks are asked in the order the
hooks would hit them, each probe is bounded by the transport's own deadline,
credentials never reach the output, and the descriptor is fetched without
being cached — a diagnostic must not create state.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import paths
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import ci_client, diagnostics
from aisquare.services.diagnostics import doctor
from tests.ci_support import RUN, wire
from tests.stub_ci_server import StubCI, error_v1, live_descriptor, serve


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


def _ci_checks() -> dict[str, DoctorCheck]:
    return {c.name: c for c in doctor() if c.name.startswith("ci ")}


def test_off_is_ok_and_asks_the_network_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off is the intended state for everyone not asked to run the experiment;
    a permanent warning trains people to ignore the one line that matters."""
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    monkeypatch.setattr(
        ci_client, "exchange", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
    )
    checks = _ci_checks()
    assert set(checks) == {"ci test bed"}
    assert checks["ci test bed"].status is CheckStatus.ok
    assert "off" in checks["ci test bed"].detail


def test_enabled_without_an_endpoint_warns_with_a_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    check = _ci_checks()["ci test bed"]
    assert check.status is CheckStatus.warn
    assert "not_configured" in check.detail and check.fix and "AISQUARE_CI_URL" in check.fix


def test_a_scheme_less_url_gets_its_own_line_not_a_reachability_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "example.com")
    checks = _ci_checks()
    assert set(checks) == {"ci test bed"}
    assert checks["ci test bed"].status is CheckStatus.warn
    assert "http(s)://" in checks["ci test bed"].detail
    assert "reachable" not in checks["ci test bed"].detail


def test_a_missing_token_and_a_missing_run_are_named_separately(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    no_key = _ci_checks()["ci test bed"]
    assert no_key.status is CheckStatus.warn and "bearer token" in no_key.detail
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    no_run = _ci_checks()["ci test bed"]
    assert no_run.status is CheckStatus.warn and "no run id" in no_run.detail
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, "kernel0001")
    bad_run = _ci_checks()["ci test bed"]
    assert bad_run.status is CheckStatus.warn and "not a run id" in bad_run.detail


def test_a_healthy_stub_is_three_green_lines(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    wire(monkeypatch, stub)
    checks = _ci_checks()
    assert {name: c.status for name, c in checks.items()} == {
        "ci test bed": CheckStatus.ok,
        "ci endpoint": CheckStatus.ok,
        "ci descriptor": CheckStatus.ok,
    }
    assert RUN in checks["ci test bed"].detail
    assert "/ready answered 200" in checks["ci endpoint"].detail
    descriptor = checks["ci descriptor"].detail
    assert "hook_push on prompt_submit" in descriptor and "mcp_pull" in descriptor
    assert "60000 ms" in descriptor


def test_doctor_does_not_cache_the_descriptor_it_fetched(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    wire(monkeypatch, stub)
    doctor()
    assert not paths.ci_descriptor_path(RUN).exists()
    assert not paths.ci_cache_dir().exists()


@pytest.mark.parametrize(
    ("status", "phrase", "fix_word"),
    [(401, "token rejected", "AISQUARE_CI_KEY"), (404, "run not found", "AISQUARE_CI_RUN")],
)
def test_the_descriptor_line_says_which_credential_is_wrong(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, status: int, phrase: str, fix_word: str
) -> None:
    wire(monkeypatch, stub)
    stub.descriptor_json({"error": "no"}, status=status)
    check = _ci_checks()["ci descriptor"]
    assert check.status is CheckStatus.warn
    assert phrase in check.detail and check.fix and fix_word in check.fix
    assert "descriptor_unavailable" in check.detail


def test_the_descriptor_line_quotes_the_servers_error_code(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live 401 carries ``scope_resolution_failed`` and a sentence; both
    reach the line, and the fix still follows the status, not the words."""
    wire(monkeypatch, stub)
    stub.descriptor_json(
        error_v1("scope_resolution_failed", 401, "no valid experiment token. Run not found."),
        status=401,
    )
    check = _ci_checks()["ci descriptor"]
    assert check.status is CheckStatus.warn
    assert "token rejected (401) — scope_resolution_failed: no valid experiment token" in (
        check.detail
    )
    assert check.fix and "AISQUARE_CI_KEY" in check.fix, (
        "the message says 'not found'; the status wins"
    )


def test_a_503_on_the_descriptor_route_names_the_servers_reason(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire(monkeypatch, stub)
    stub.descriptor_json(
        error_v1("dependency_unavailable", 503, f"run {RUN} has no completed build"), status=503
    )
    check = _ci_checks()["ci descriptor"]
    assert check.status is CheckStatus.warn
    assert "dependency_unavailable" in check.detail and "no completed build" in check.detail
    assert check.fix and "AISQUARE_CI=0" in check.fix


def test_an_expired_run_is_named_as_expired(stub: StubCI, monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, stub)
    stub.descriptor_json(live_descriptor(expires_at="2026-01-01T00:00:00Z"))
    check = _ci_checks()["ci descriptor"]
    assert check.status is CheckStatus.warn and "expired" in check.detail


def test_an_unreachable_server_warns_truthfully(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://127.0.0.1:1")
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, RUN)
    checks = _ci_checks()
    assert checks["ci endpoint"].status is CheckStatus.warn
    assert "prompts still work" in checks["ci endpoint"].detail
    assert "descriptor_unavailable" in checks["ci endpoint"].detail
    assert checks["ci descriptor"].status is CheckStatus.warn


def test_credentials_in_the_url_never_reach_the_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://user:s3cr3t@127.0.0.1:1")
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, RUN)
    for check in doctor():
        assert "s3cr3t" not in check.detail
        assert "s3cr3t" not in (check.fix or "")


def test_a_black_hole_endpoint_cannot_hang_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic: a fake transport that sleeps well past the probe bound,
    recording the timeout it was handed. The bound is the transport's wall
    clock, so the sleep never matters and the timeout kwarg is what we asked."""
    seen: list[float] = []

    def black_hole(request: Any, timeout: float) -> Any:
        seen.append(timeout)
        time.sleep(3.0)
        raise AssertionError("unreachable: the deadline fires first")

    monkeypatch.setattr(ci_client, "urlopen", black_hole)
    monkeypatch.setattr(diagnostics, "_CI_PROBE_MS", 200)
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://blackhole.invalid")
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, RUN)
    started = time.monotonic()
    checks = _ci_checks()
    assert time.monotonic() - started < 2.0
    assert checks["ci endpoint"].status is CheckStatus.warn
    assert checks["ci descriptor"].status is CheckStatus.warn
    assert seen and all(abs(t - 0.2) < 0.01 for t in seen)


def test_doctor_still_answers_every_other_check_when_the_url_is_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo'd URL used to raise out of the port parser and lose the whole report."""
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "k")
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, RUN)
    for url in ("http://localhost:99999", "http://host:notaport", "http://a..example.com"):
        monkeypatch.setenv(ci_client.URL_ENV_VAR, url)
        checks = doctor()
        assert any(c.name == "python" for c in checks)
        assert any(c.name == "ci endpoint" and c.status is CheckStatus.warn for c in checks), url


def test_the_display_url_keeps_scheme_and_host_only() -> None:
    assert (
        diagnostics._display_url("https://user:pw@ci.example:9443/base")
        == "https://ci.example:9443"
    )
    assert diagnostics._display_url("example.com") == "example.com"
    assert "pw" not in diagnostics._display_url("user:pw@example.com")
