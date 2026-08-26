"""``explainability proxy up|down|status`` — the sidecar, without the incantation.

The proxy stays a separate process holding its own key; that design is right.
What these commands remove is assembling four environment variables by hand out
of config the CLI already holds, which is the step the runbook asked for and the
step people got wrong.

Nothing here spawns a real proxy. The subprocess and the health probe are the two
seams, and both are patched — a test that bound a real port would be a test that
fails when a colleague is running their own proxy on the same box, which is
precisely the situation this feature exists to make legible.

WHAT IS ACTUALLY WORTH PINNING, because "it starts a process" is not:

* the key never reaches argv (``/proc/<pid>/cmdline`` is world-readable);
* a healthy proxy this CLI did NOT start is reported as foreign, not as success,
  and ``down`` refuses to kill it;
* a start that never answers ``/health`` does not leave the process behind, and
  is not reported as working — the proxy prints "Application startup complete"
  BEFORE it binds, so its own log is not evidence;
* ``up`` twice does not start two.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilitySettings, save_config
from aisquare.services import explainability_proxy as proxy
from aisquare.services.explainability import ProxyProbe

#: Patch targets as dotted strings. `monkeypatch.setattr(proxy.subprocess, ...)`
#: reads more naturally and mypy strict rejects it, correctly: `subprocess` and
#: `shutil` are imports the module happens to hold, not part of its interface.
_POPEN = "aisquare.services.explainability_proxy.subprocess.Popen"
_WHICH = "aisquare.services.explainability_proxy.shutil.which"


def _recorder(seen: list[int]) -> Callable[[int], bool]:
    """A ``_terminate`` stand-in that records the pid and reports success.

    Spelled out rather than `lambda pid: seen.append(pid) or True`, which mypy
    flags for the right reason: `append` returns None, so that expression is
    True by accident of falsiness rather than by saying so.
    """

    def terminate(pid: int) -> bool:
        seen.append(pid)
        return True

    return terminate


_GATEWAY = "https://gateway.example"
_KEY = "-".join(["not", "a", "real", "key"])


class _FakeProcess:
    """A Popen stand-in: alive until told otherwise, and remembers its argv."""

    def __init__(self, argv: list[str], env: dict[str, str], pid: int = 424242) -> None:
        self.argv, self.env, self.pid = argv, env, pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def configured(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine with a gateway and a key, and no proxy running."""
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True, gateway_url=_GATEWAY, proxy_url="http://127.0.0.1:9090"
            )
        )
    )
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", _KEY)
    monkeypatch.setattr(_WHICH, lambda _name: "/usr/local/bin/aisquare-proxy")
    monkeypatch.setattr(proxy, "_alive", lambda _pid: True)
    # Ownership now needs a verifiable identity, so the fake OS must supply a
    # stable one. A None here means "cannot verify", which is correctly treated
    # as not-ours — the conservative direction, and not the case under test.
    monkeypatch.setattr(proxy, "_identity_token", lambda _pid: "proc:1234:aisquare-proxy")


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the spawn instead of performing it, and model the port going live.

    The probe is STATEFUL — unhealthy until Popen is called, healthy after — and
    that ordering is the whole reason `up` can be written at all. A fixture that
    answered healthy from the start describes a machine where a proxy is already
    running, which is the case `up` correctly REFUSES; every test that expected a
    successful start then failed against the refusal rather than the feature.
    Reality has the same sequence: nothing on the port, then something.
    """
    captured: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["new_session"] = kwargs.get("start_new_session")
        captured["live"] = True
        return _FakeProcess(argv, captured["env"])

    def fake_probe(_url: str, timeout: float = 1.0) -> ProxyProbe:
        if captured.get("live"):
            return ProxyProbe(True, "proxy healthy")
        return ProxyProbe(False, f"proxy unreachable at {_url}")

    monkeypatch.setattr(_POPEN, fake_popen)
    monkeypatch.setattr(proxy, "probe_proxy", fake_probe)
    return captured


def test_up_takes_the_key_from_config_and_keeps_it_out_of_argv(
    configured: None, spawned: dict[str, Any], runner: CliRunner
) -> None:
    """The whole point of the command, and the one security property it owes.

    A key in argv is a key in `ps`, in every screen-share and every scrollback.
    It travels in the child's environment instead.
    """
    result = runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert spawned["argv"] == ["/usr/local/bin/aisquare-proxy"], spawned["argv"]
    assert _KEY not in " ".join(spawned["argv"]), "the key reached the process table"
    assert spawned["env"]["EXPLAINABILITY_API_KEY"] == _KEY
    assert spawned["env"]["EXPLAINABILITY_GATEWAY_URL"] == _GATEWAY
    assert spawned["env"]["AISQUARE_PROXY_PORT"] == "9090"


def test_up_detaches_so_the_proxy_outlives_the_shell(
    configured: None, spawned: dict[str, Any], runner: CliRunner
) -> None:
    """A sidecar that dies with the command that started it is not a sidecar.

    It also must not take a Ctrl-C aimed at the CLI, which is the same flag.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    assert spawned["new_session"] is True


def test_up_is_idempotent(configured: None, spawned: dict[str, Any], runner: CliRunner) -> None:
    """Two proxies on one port means one lost the bind and nobody knows which."""
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    spawned.pop("argv", None)  # keep "live": the port stays occupied by OUR proxy

    result = runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "argv" not in spawned, "started a second proxy over a running one"


def test_a_start_that_never_answers_is_a_failure_and_leaves_nothing_behind(
    configured: None, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The occupied-port case, which the proxy's own log reports as healthy.

    It prints "Application startup complete" and THEN fails to bind, so a live
    pid proves nothing. `up` must report failure, and must not leave a process
    it cannot account for.
    """
    killed: list[int] = []
    monkeypatch.setattr(_POPEN, lambda argv, **kw: _FakeProcess(argv, {}))
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(False, "unreachable")
    )
    monkeypatch.setattr(proxy, "_terminate", _recorder(killed))
    monkeypatch.setattr(proxy, "_BOOT_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(proxy, "_POLL_INTERVAL_SECONDS", 0.05)

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0, result.output
    assert killed == [424242], "left a proxy running that never answered"
    assert not paths.explainability_proxy_state_path().exists(), (
        "recorded a proxy that never came up"
    )


def test_up_refuses_when_a_proxy_it_did_not_start_is_answering(
    configured: None, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Somebody else's proxy — a colleague's, or a deliberate remote one."""
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(True, "proxy healthy")
    )
    monkeypatch.setattr(_POPEN, lambda *a, **k: pytest.fail("started over a live proxy"))

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0
    assert "did not start it" in result.output


def test_up_refuses_without_a_key(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """A proxy the gateway rejects looks exactly like a working one from here."""
    save_config(
        AppConfig(explainability=ExplainabilitySettings(enabled=True, gateway_url=_GATEWAY))
    )
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)
    monkeypatch.setattr(_WHICH, lambda _n: "/usr/local/bin/aisquare-proxy")
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(False, "unreachable")
    )
    monkeypatch.setattr(_POPEN, lambda *a, **k: pytest.fail("started without a key"))

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0
    assert "key" in result.output.lower()


def test_status_distinguishes_ours_from_a_stranger(
    configured: None, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The question `doctor` cannot answer, which is why this command exists.

    Its proxy row goes green for ANY service answering as aisquare-proxy in
    claude_code mode — including one left running last week against another
    deployment, whose Runs land somewhere else entirely.
    """
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(True, "proxy healthy")
    )

    payload = json.loads(
        runner.invoke(
            app, ["--json", "explainability", "proxy", "status"], catch_exceptions=False
        ).output
    )

    assert payload["healthy"] is True
    assert payload["managed"] is False, "a proxy we never started reported as managed"
    assert "NOT started by this CLI" in payload["summary"]


def test_status_reports_managed_after_up(
    configured: None, spawned: dict[str, Any], runner: CliRunner
) -> None:
    """The positive control: `managed` must be reachable, or the field is a lie."""
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    payload = json.loads(
        runner.invoke(
            app, ["--json", "explainability", "proxy", "status"], catch_exceptions=False
        ).output
    )

    assert payload["managed"] is True
    assert payload["pid"] == 424242
    assert payload["gateway"] == _GATEWAY


def test_status_exits_nonzero_when_nothing_is_listening(
    configured: None, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """So a timer or a shell `&&` can depend on it."""
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(False, "unreachable")
    )

    assert runner.invoke(app, ["explainability", "proxy", "status"]).exit_code == 1


def test_down_stops_only_what_we_started(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    stopped: list[int] = []
    monkeypatch.setattr(proxy, "_terminate", _recorder(stopped))
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    result = runner.invoke(app, ["explainability", "proxy", "down"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert stopped == [424242]
    assert not paths.explainability_proxy_state_path().exists()


def test_down_leaves_a_stranger_alone(
    configured: None, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Killing on "something is on the port" ends a colleague's session.

    It would also kill a hosted proxy this machine was pointed at deliberately.
    """
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(True, "proxy healthy")
    )
    monkeypatch.setattr(proxy, "_terminate", lambda pid: pytest.fail(f"killed pid {pid}"))

    result = runner.invoke(app, ["explainability", "proxy", "down"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "did not start it" in result.output


def test_down_clears_a_record_whose_process_has_gone(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """A reboot leaves the file behind; the next `up` must not think it is running."""
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    monkeypatch.setattr(proxy, "_alive", lambda _pid: False)

    result = runner.invoke(app, ["explainability", "proxy", "down"], catch_exceptions=False)

    assert "stale" in result.output
    assert not paths.explainability_proxy_state_path().exists()


def test_up_will_not_start_a_proxy_for_a_remote_url(
    configured: None, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """A non-loopback proxy_url names someone else's service.

    The SDK refuses to bind beyond loopback without inbound keys rather than
    become an open relay, so starting a local one here would not be what was
    asked for and would trace to the wrong place.
    """
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True, gateway_url=_GATEWAY, proxy_url="http://proxy.internal:9090"
            )
        )
    )
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(False, "unreachable")
    )
    monkeypatch.setattr(
        _POPEN, lambda *a, **k: pytest.fail("started a local proxy for a remote url")
    )

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0
    assert "loopback" in result.output


def test_a_target_switch_does_not_reuse_the_previous_deployments_proxy(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """THE reuse defect, and the reason the record carries target/gateway/key.

    Staging and production normally share one loopback proxy URL, so a target
    switch changes nothing the URL can see. Matching on the URL alone left the
    old proxy running with the old gateway and the old key, and `up` returned a
    green ✓ — production-labelled sessions going to staging, silently.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    first_gateway = json.loads(
        runner.invoke(
            app, ["--json", "explainability", "proxy", "status"], catch_exceptions=False
        ).output
    )["gateway"]
    assert first_gateway == _GATEWAY

    # Same loopback URL, different deployment — exactly the runbook's cutover.
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True,
                gateway_url="https://other-deployment.example",
                proxy_url="http://127.0.0.1:9090",
                target="prod",
            )
        )
    )
    stopped: list[int] = []
    monkeypatch.setattr(proxy, "_terminate", _recorder(stopped))
    spawned.pop("argv", None)

    result = runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert stopped == [424242], "kept the previous deployment's proxy"
    assert "argv" in spawned, "did not start a proxy for the new deployment"
    assert spawned["env"]["EXPLAINABILITY_GATEWAY_URL"] == "https://other-deployment.example"


def test_status_names_the_stale_deployment_rather_than_reporting_healthy(
    configured: None, spawned: dict[str, Any], runner: CliRunner
) -> None:
    """A proxy serving the wrong deployment must not read as managed."""
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True,
                gateway_url="https://other-deployment.example",
                proxy_url="http://127.0.0.1:9090",
                target="prod",
            )
        )
    )

    payload = json.loads(
        runner.invoke(
            app, ["--json", "explainability", "proxy", "status"], catch_exceptions=False
        ).output
    )

    assert payload["managed"] is False, "a proxy on the old deployment reported as managed"
    assert "OLD deployment" in payload["summary"], payload["summary"]


def test_up_does_not_call_a_wedged_process_a_success(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Ownership alone used to satisfy the early return, so `up` printed

    "✓ not running (nothing answers at …)" immediately followed by "Sessions
    launched from now on are traced." Both lines from one run: the process is
    ours, and it is not serving anything.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    spawned.pop("live", None)  # the process stays alive; the port goes quiet
    stopped: list[int] = []
    monkeypatch.setattr(proxy, "_terminate", _recorder(stopped))
    spawned.pop("argv", None)

    result = runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    assert stopped == [424242], "left a wedged proxy in place"
    assert "argv" in spawned, "did not replace the wedged proxy"
    assert "not running" not in result.output, result.output


def test_down_will_not_signal_a_recycled_pid(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The record outlives a reboot on purpose, and pids are reused.

    `os.kill(pid, 0)` proves only that SOMETHING holds that number, so liveness
    was never evidence of ownership — it was a SIGTERM aimed at a stranger.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    # Same pid, different process: exactly what a recycled pid looks like.
    monkeypatch.setattr(proxy, "_identity_token", lambda _pid: "proc:99999:something-else")
    monkeypatch.setattr(proxy, "_terminate", lambda pid: pytest.fail(f"signalled pid {pid}"))

    result = runner.invoke(app, ["explainability", "proxy", "down"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "NOT the proxy we started" in result.output
    assert not paths.explainability_proxy_state_path().exists(), "kept a record it distrusted"


def test_an_unverifiable_pid_is_never_signalled(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """No /proc and no usable `ps` means "cannot verify", not "verified".

    The conservative direction costs an operator one manual stop; the other
    direction kills a process we cannot name.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    monkeypatch.setattr(proxy, "_identity_token", lambda _pid: None)
    monkeypatch.setattr(proxy, "_terminate", lambda pid: pytest.fail(f"signalled pid {pid}"))

    result = runner.invoke(app, ["explainability", "proxy", "down"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "NOT the proxy we started" in result.output


def test_the_key_fingerprint_is_not_the_key(configured: None) -> None:
    """It is recorded in a plain file beside the join log, so it must not carry
    the credential — only enough to notice that it changed."""
    fingerprint = proxy.key_fingerprint(_KEY)

    assert _KEY not in fingerprint
    assert len(fingerprint) == 12
    assert fingerprint != proxy.key_fingerprint(_KEY + "x")
