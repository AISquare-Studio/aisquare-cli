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

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import filelock, paths
from aisquare.core.config import AppConfig, ExplainabilitySettings, load_config, save_config
from aisquare.services import explainability_proxy as proxy
from aisquare.services.explainability import ProxyProbe
from aisquare.services.explainability_proxy import ProxyRecord

#: Patch targets as dotted strings. `monkeypatch.setattr(proxy.subprocess, ...)`
#: reads more naturally and mypy strict rejects it, correctly: `subprocess` and
#: `shutil` are imports the module happens to hold, not part of its interface.
_POPEN = "aisquare.services.explainability_proxy.subprocess.Popen"
_WHICH = "aisquare.services.explainability_proxy.shutil.which"
_EXECUTABLE = "aisquare.services.explainability_proxy.sys.executable"
_PIDFD_OPEN = "aisquare.services.explainability_proxy.os.pidfd_open"
_OS_KILL = "aisquare.services.explainability_proxy.os.kill"


def _owned_stopper(spawned: dict[str, Any], seen: list[int]) -> Callable[[Any], bool]:
    """A `_stop_owned` that records the pid and frees the port, as a real stop does."""

    def stop(record: Any) -> bool:
        seen.append(record.pid)
        spawned.pop("live", None)
        return True

    return stop


def _through_recorder(seen: list[int]) -> Callable[[Any, int], bool]:
    """A `_stop_through` that records the pid it was asked to signal."""

    def stop(_send: Any, pid: int) -> bool:
        seen.append(pid)
        return True

    return stop


def _owned_recorder(seen: list[int]) -> Callable[[Any], bool]:
    """A `_stop_owned` that records the pid and reports success, port untouched."""

    def stop(record: Any) -> bool:
        seen.append(record.pid)
        return True

    return stop


def _stopper(spawned: dict[str, Any], seen: list[int]) -> Callable[[int], bool]:
    """A ``_terminate`` that also makes the port go quiet, as stopping one does.

    `up` now confirms the port is free before spawning a replacement, so a
    terminate stub that leaves the fake probe answering models a proxy that
    refused to die — which is a different test.
    """

    def terminate(pid: int) -> bool:
        seen.append(pid)
        spawned.pop("live", None)
        return True

    return terminate


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
    """A Popen stand-in: alive until told otherwise, and remembers its argv.

    Carries `terminate`/`kill`/`wait` because post-spawn rollback goes through the
    CHILD HANDLE rather than the pid — no pid-reuse question for a process we
    hold, and `wait` gives a confirmed exit instead of inferring one from
    `os.kill(pid, 0)`. `stopped` records that it happened; set `refuses_to_die`
    to model a child that survives, which is what makes the rollback report a
    still-live pid instead of claiming success.
    """

    def __init__(self, argv: list[str], env: dict[str, str], pid: int = 424242) -> None:
        self.argv, self.env, self.pid = argv, env, pid
        self.returncode: int | None = None
        self.stopped = False
        self.refuses_to_die = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if not self.refuses_to_die:
            self.stopped = True
            self.returncode = -15

    def kill(self) -> None:
        if not self.refuses_to_die:
            self.stopped = True
            self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("aisquare-proxy", timeout or 0)
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
    # ...and the bin dir beside the interpreter, which `_resolve_proxy_script`
    # now checks FIRST. Patching only `_WHICH` left that lookup live, so on any
    # machine with `[explainability]` installed the real
    # `.venv/bin/aisquare-proxy` won over the stub. CI installs `.[dev]` only, so
    # the script is never beside the interpreter there and this stayed green —
    # exactly the "green on CI, red for anyone following the runbook" shape this
    # file's own fixtures were fixed for twice.
    monkeypatch.setattr(_EXECUTABLE, str(isolated_home / "no-scripts-here" / "python"))

    # The fake pid is not a real process, so `_process_handle`'s `pidfd_open`
    # would raise and `_owned_handle` would correctly report "not ours" —
    # short-circuiting every stop-path test before it reached the decision it is
    # about. Stubbed at the OS boundary only: `_owned_handle`'s ordering and its
    # identity check still run for real. The real thing is covered separately,
    # against real children, in the stop-path tests below.
    @contextlib.contextmanager
    def fake_handle(_pid: int) -> Iterator[Callable[[int], None]]:
        yield lambda _sig: None

    monkeypatch.setattr(proxy, "_process_handle", fake_handle)
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
        child = _FakeProcess(argv, captured["env"])
        captured["child"] = child
        return child

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
    children: list[_FakeProcess] = []

    def spawn(argv: list[str], **kw: Any) -> _FakeProcess:
        child = _FakeProcess(argv, {})
        children.append(child)
        return child

    monkeypatch.setattr(_POPEN, spawn)
    monkeypatch.setattr(
        proxy, "probe_proxy", lambda _url, timeout=1.0: ProxyProbe(False, "unreachable")
    )
    monkeypatch.setattr(proxy, "_BOOT_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(proxy, "_POLL_INTERVAL_SECONDS", 0.05)

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0, result.output
    assert children and children[0].stopped, "left a proxy running that never answered"
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
    # `down` binds the handle, verifies inside it, then escalates — so the seam
    # is `_stop_through`, not a pid-taking terminate.
    monkeypatch.setattr(proxy, "_stop_through", _through_recorder(stopped))
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
    monkeypatch.setattr(proxy, "_stop_owned", _owned_stopper(spawned, stopped))
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
    monkeypatch.setattr(proxy, "_stop_owned", _owned_stopper(spawned, stopped))
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
    monkeypatch.setattr(proxy, "_stop_through", lambda _s, pid: pytest.fail(f"signalled {pid}"))

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
    monkeypatch.setattr(proxy, "_stop_through", lambda _s, pid: pytest.fail(f"signalled {pid}"))

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


def test_a_failed_stop_does_not_start_a_replacement(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The subtle one, and the reason a failed stop must be fatal.

    If the old proxy survives, it keeps the port AND ANSWERS THE NEW CHILD'S
    STARTUP POLL. `up` then records the new pid and reports the new deployment as
    managed while every span still goes to the old gateway. The health poll cannot
    tell two proxies on one port apart; only refusing to proceed can.

    The record is retained deliberately: the process it names is alive and is
    still ours to stop.
    """
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
    monkeypatch.setattr(proxy, "_stop_owned", lambda _record: False)
    spawned.pop("argv", None)

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0, result.output
    assert "argv" not in spawned, "spawned a replacement over a proxy that would not stop"
    assert paths.explainability_proxy_state_path().exists(), (
        "dropped the record of a process that is still alive and still ours"
    )


def test_a_port_that_stays_occupied_does_not_start_a_replacement(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """A stop that reports success is not proof the listener is gone.

    Same failure one step later: something still answers, the replacement's own
    startup poll is satisfied by it, and the spans go where this CLI cannot see.
    """
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
    stopped: list[int] = []
    monkeypatch.setattr(proxy, "_stop_owned", _owned_recorder(stopped))
    monkeypatch.setattr(proxy, "_port_went_quiet", lambda _url, timeout=0.0: False)
    spawned.pop("argv", None)

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0, result.output
    assert stopped == [424242], "the test's premise is gone: it must have tried to stop"
    assert "argv" not in spawned, "spawned into a port that was still answering"


def test_a_broken_new_target_does_not_stop_the_working_proxy(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Preflight has to happen BEFORE anything running is touched.

    The checks used to sit below the replacement, so a cutover to a target with
    no gateway stopped a working proxy and only then discovered it could not
    start another — trading a proxy on the wrong deployment for no proxy at all.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True, gateway_url="", proxy_url="http://127.0.0.1:9090", target="prod"
            )
        )
    )
    monkeypatch.setattr(proxy, "_stop_owned", lambda r: pytest.fail(f"stopped pid {r.pid}"))

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0
    assert "no gateway URL" in result.output


def test_up_refuses_rather_than_leaving_an_unidentifiable_orphan(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """A process we can never recognise again is worse than no process.

    `_owns` rejects a null identity, so recording one produced a proxy that was
    foreign to `status` and `down` from the moment it booted — an orphan holding
    the port, announced as a success. It is stopped instead, and the operator is
    handed the manual command.
    """
    monkeypatch.setattr(proxy, "_identity_token", lambda _pid: None)

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0, result.output
    assert spawned["child"].stopped, "left a proxy it could never identify again"
    assert not paths.explainability_proxy_state_path().exists()
    assert "Run the sidecar yourself" in result.output, "refused without a way forward"


@pytest.mark.skipif(
    proxy._boot_id() is None, reason="no /proc boot id here; identity comes from `ps` instead"
)
def test_the_proc_identity_token_is_scoped_to_this_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`starttime` is ticks SINCE boot, and the record outlives a reboot.

    Without the boot id the token repeats across the exact boundary it exists to
    protect: same pid, same tick, same basename — and for a console script that
    basename is usually `python`, so the collision is not even unlikely.

    SKIPPED where there is no boot id rather than asserted unconditionally: the
    implementation has a `ps` fallback whose `lstart` is absolute and needs no
    boot scoping, so on those platforms this test would be asserting a property
    of a code path that never runs.
    """
    boot_id = proxy._boot_id()
    token = proxy._proc_identity(os.getpid())

    assert boot_id is not None
    assert token is not None, "the skipif promised /proc is readable here"
    assert boot_id in token, f"token is not boot-scoped: {token}"

    monkeypatch.setattr(proxy, "_boot_id", lambda: "a-different-boot")

    assert proxy._proc_identity(os.getpid()) != token


def test_an_unreadable_boot_id_does_not_become_a_sentinel_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_boot_id` used to return "?" and that was accepted into the token.

    `proc:?:…` looks boot-scoped, compares equal across reboots, and reintroduces
    exactly the collision the boot id was added to prevent. The /proc source must
    decline, leaving `ps` — whose `lstart` is absolute — to answer.
    """
    monkeypatch.setattr(proxy, "_boot_id", lambda: None)

    assert proxy._proc_identity(os.getpid()) is None

    monkeypatch.setattr(proxy, "_ps_identity", lambda _pid: None)

    assert proxy._identity_token(os.getpid()) is None, (
        "with neither source answering, the token must be absent rather than a sentinel"
    )


def test_the_sidecar_is_found_beside_the_interpreter_not_only_on_path(
    configured: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pipx-shaped installation, which `shutil.which` alone cannot serve.

    The extra installs `aisquare-proxy` beside the interpreter running us. That
    directory need not be on PATH — pipx exposes the MAIN distribution's apps and
    not a dependency's, and invoking the CLI by absolute path does the same. The
    reproduction was: extra installed, `venv/bin/aisquare-proxy` present, and
    `proxy up` reporting the extra missing.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / proxy.PROXY_SCRIPT
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(_EXECUTABLE, str(bindir / "python"))
    monkeypatch.setattr(_WHICH, lambda _n: None)  # nothing on PATH at all

    assert proxy._resolve_proxy_script() == str(script)


def test_path_is_still_the_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The supported case where the SDK lives in a different environment."""
    monkeypatch.setattr(_EXECUTABLE, str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(_WHICH, lambda _n: "/elsewhere/bin/aisquare-proxy")

    assert proxy._resolve_proxy_script() == "/elsewhere/bin/aisquare-proxy"


def test_two_concurrent_ups_cannot_both_spawn(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """`up` is a check-then-act, so it has to be serialised across processes.

    Both callers passed the idempotence check, both spawned, both wrote the
    record, and both reported managed — and with real sockets one child satisfies
    the OTHER's health poll, so the surviving record can name the child that lost
    the bind and exited, leaving the real listener foreign.

    Modelled by holding the lock from outside: the second caller must refuse
    rather than proceed.
    """
    lock_path = paths.explainability_dir() / "proxy.lock"
    monkeypatch.setattr(proxy, "_LOCK_WAIT_SECONDS", 0.2)

    with filelock.held(lock_path, wait_s=1.0) as won:
        assert won, "the test could not take the lock it needs to hold"
        result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0, result.output
    assert "argv" not in spawned, "spawned while another lifecycle command held the lock"
    assert "still running" in result.output


def test_a_record_that_cannot_be_written_does_not_leave_a_live_orphan(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Commit failure must end in a durable record OR a confirmed-dead child.

    The write was unguarded, so an unwritable state directory let the raw
    exception escape with the proxy still running and nothing recording it.
    """

    def unwritable(_record: Any) -> None:
        raise PermissionError("read-only state directory")

    monkeypatch.setattr(proxy, "_write_record", unwritable)

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0, result.output
    assert spawned["child"].stopped, "left a live proxy with no record of it"
    assert "record could not be written" in result.output


def test_a_rollback_that_cannot_stop_the_child_says_so_and_names_the_pid(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The rollback paths used to claim "has been stopped" regardless.

    A child that survives has to be reported, with its pid, or the operator is
    told the machine is clean while a proxy holds the port.
    """
    monkeypatch.setattr(proxy, "_identity_token", lambda _pid: None)
    monkeypatch.setattr(proxy, "_STOP_TIMEOUT_SECONDS", 0.1)

    def spawn(argv: list[str], **kw: Any) -> _FakeProcess:
        child = _FakeProcess(argv, dict(kw.get("env") or {}))
        child.refuses_to_die = True
        spawned["child"] = child
        spawned["live"] = True
        return child

    monkeypatch.setattr(_POPEN, spawn)

    result = runner.invoke(app, ["explainability", "proxy", "up"])

    assert result.exit_code != 0
    assert "could NOT be stopped" in result.output
    assert "424242" in result.output, "did not name the pid that is still running"


def test_the_child_binds_the_address_the_url_names(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """`::1` was accepted by the preflight and then not honoured.

    The SDK defaults its host to 127.0.0.1, so an accepted `http://[::1]:9090`
    started a proxy on IPv4 that never answered the URL it was validated against.
    An ambient AISQUARE_PROXY_HOST was inherited too, which could move a
    "local sidecar" off the address the loopback check had approved.
    """
    monkeypatch.setenv(proxy.HOST_ENV_VAR, "0.0.0.0")
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True, gateway_url=_GATEWAY, proxy_url="http://[::1]:9090"
            )
        )
    )

    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    assert spawned["env"][proxy.HOST_ENV_VAR] == "::1", spawned["env"][proxy.HOST_ENV_VAR]
    assert spawned["env"][proxy.MODE_ENV_VAR] == "claude_code", "the proxy mode is not pinned"


def test_the_manual_command_names_the_key_source_that_is_actually_in_use(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It always said `$(cat ~/.aisquare/explainability-key)`.

    The production runbook sources the key from the target's named variable, and
    that file need not exist — so the printed recovery command told the operator
    to read something absent.
    """
    from aisquare.services import explainability_ops as ops

    env_backed = ops.resolve_target(load_config().explainability)
    assert env_backed.key_source == "env", "the fixture exports a key, so env must win"

    line = proxy._manual_command(env_backed, 9090)

    assert f"${env_backed.api_key_env}" in line
    assert "cat " not in line, line


def test_the_manual_command_is_shell_quoted(configured: None) -> None:
    """These are URLs and paths out of config; one with a space silently ran
    the wrong thing."""
    from aisquare.services import explainability_ops as ops

    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(
                enabled=True,
                gateway_url="https://gateway.example/a b",
                proxy_url="http://127.0.0.1:9090",
            )
        )
    )
    target = ops.resolve_target(load_config().explainability)

    line = proxy._manual_command(target, 9090)

    assert "'https://gateway.example/a b'" in line, line


# ── the stop path, against REAL processes ────────────────────────────────────
#
# Every test above stubs the OS boundary, which is right for testing decisions
# and is exactly why the signal machinery shipped twice with the wrong ordering:
# `_process_handle`, the ESRCH rule, the PermissionError path, the SIGTERM→
# SIGKILL escalation and `_terminate_child`'s wait-confirmation had no coverage
# at all, so a regression reintroducing the swallowed-ESRCH bug would have passed
# the entire suite. These use real children. They are slower and they are the
# only tests here that would notice.


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    """A real child that ignores nothing and exits when signalled."""
    child = subprocess.Popen(["sleep", "60"])
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_stop_through_really_stops_a_real_process(sleeper: subprocess.Popen[bytes]) -> None:
    """The escalation, end to end, with no stubs between it and the kernel."""
    with proxy._process_handle(sleeper.pid) as send:
        assert proxy._stop_through(send, sleeper.pid) is True

    sleeper.wait(timeout=5)
    assert sleeper.poll() is not None


def test_the_pidfd_is_bound_before_ownership_is_checked(
    sleeper: subprocess.Popen[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering that took three attempts, asserted rather than described.

    `_owned_handle` must bind to the process FIRST and verify SECOND. Verifying
    first leaves a window in which the process can exit and its number be
    recycled, and the handle then binds to the successor.

    Pinned by observation order: the identity check must not run until after the
    handle exists.
    """
    order: list[str] = []
    real_handle = proxy._process_handle
    real_token = proxy._identity_token

    @contextlib.contextmanager
    def watched_handle(pid: int) -> Iterator[Callable[[int], None]]:
        order.append("bind")
        with real_handle(pid) as send:
            yield send

    def watched_token(pid: int) -> str | None:
        order.append("verify")
        return real_token(pid)

    monkeypatch.setattr(proxy, "_process_handle", watched_handle)
    monkeypatch.setattr(proxy, "_identity_token", watched_token)

    record = ProxyRecord(
        pid=sleeper.pid,
        port=9090,
        url="http://127.0.0.1:9090",
        gateway_url=_GATEWAY,
        target="stg",
        key_fp=proxy.key_fingerprint(_KEY),
        identity=real_token(sleeper.pid),
        started_at=time.time(),
    )

    with proxy._owned_handle(record) as send:
        assert send is not None, "the record describes this live child, so it is ours"

    assert order == ["bind", "verify"], f"verified before binding: {order}"


def test_a_mismatched_identity_yields_no_sender(sleeper: subprocess.Popen[bytes]) -> None:
    """A recycled pid: alive, right number, different process."""
    record = ProxyRecord(
        pid=sleeper.pid,
        port=9090,
        url="http://127.0.0.1:9090",
        gateway_url=_GATEWAY,
        target="stg",
        key_fp=proxy.key_fingerprint(_KEY),
        identity="proc:some-other-boot:1:aisquare-proxy",
        started_at=time.time(),
    )

    with proxy._owned_handle(record) as send:
        assert send is None

    assert sleeper.poll() is None, "the child was signalled despite not being ours"


def test_esrch_is_terminal_and_never_becomes_a_numeric_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst line in the earlier version, pinned.

    `pidfd_open` raising ESRCH means the pid is gone — which is precisely when the
    number may already belong to somebody else. Falling back to a numeric
    `os.kill` there is the one thing that must not happen.
    """
    monkeypatch.setattr(_PIDFD_OPEN, lambda _pid: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(_OS_KILL, lambda *a: pytest.fail("fell back to a numeric kill"))

    # `_terminate` reports "gone", which is true, and signals nothing.
    assert proxy._terminate(424242) is True


def test_the_numeric_fallback_translates_esrch_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS, Windows or a pre-5.3 kernel there is no pidfd.

    `os.kill` raises `ProcessLookupError`, which is an `OSError` and was neither
    `_Gone` nor `PermissionError` — so a proxy that exited between the liveness
    check and the SIGTERM gave the operator a raw traceback instead of "was
    already gone".
    """
    monkeypatch.delattr(_PIDFD_OPEN, raising=False)
    monkeypatch.setattr(_OS_KILL, lambda *a: (_ for _ in ()).throw(ProcessLookupError()))

    assert proxy._terminate(424242) is True


def test_a_permission_error_reports_failure_rather_than_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another user's process. False is what makes `up` refuse to replace it."""
    monkeypatch.delattr(_PIDFD_OPEN, raising=False)
    monkeypatch.setattr(_OS_KILL, lambda *a: (_ for _ in ()).throw(PermissionError()))

    assert proxy._terminate(424242) is False


def test_terminate_child_confirms_the_exit(sleeper: subprocess.Popen[bytes]) -> None:
    """Rollback uses the `Popen` handle, so the exit is confirmed not inferred."""
    assert proxy._terminate_child(sleeper, sleeper.pid) is True
    assert sleeper.poll() is not None


def test_terminate_child_escalates_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that ignores SIGTERM must still be stopped."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)",
        ]
    )
    try:
        time.sleep(0.5)
        monkeypatch.setattr(proxy, "_STOP_TIMEOUT_SECONDS", 0.5)

        assert proxy._terminate_child(child, child.pid) is True
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_changing_the_proxy_url_stops_the_old_proxy_instead_of_orphaning_it(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The deployment-switch failure, reached through the one field it missed.

    `_owns` used to short-circuit on `record.url != <current proxy_url>`, so
    `enable --proxy-url http://127.0.0.1:9190` made the running proxy stop being
    ours. `up` then saw neither an owned proxy nor a healthy one on the new port,
    spawned a second, and `_write_record` clobbered the only record of the first —
    which then ran forever on 9090 with the old gateway and key, unstoppable by
    `down`. The runbook's manual form uses 9190 while the default is 9090, so
    this is a path operators take.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    stopped: list[int] = []
    monkeypatch.setattr(proxy, "_stop_owned", _owned_stopper(spawned, stopped))
    runner.invoke(
        app,
        ["explainability", "enable", "--proxy-url", "http://127.0.0.1:9190"],
        catch_exceptions=False,
    )
    spawned.pop("argv", None)

    result = runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert stopped == [424242], "orphaned the proxy on the old port"
    assert spawned["env"][proxy.PORT_ENV_VAR] == "9190", "did not move to the new port"


def test_status_still_owns_a_proxy_on_a_port_we_no_longer_configure(
    configured: None, spawned: dict[str, Any], runner: CliRunner
) -> None:
    """Ownership is about the process; the URL is about the configuration.

    `down` has to be able to stop it, which means `status` has to still call it
    ours.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    runner.invoke(
        app,
        ["explainability", "enable", "--proxy-url", "http://127.0.0.1:9190"],
        catch_exceptions=False,
    )

    payload = json.loads(
        runner.invoke(
            app, ["--json", "explainability", "proxy", "status"], catch_exceptions=False
        ).output
    )

    assert payload["managed"] is False, "a proxy on an unconfigured port is not managed"
    assert payload["pid"] == 424242, "lost track of a process we started"


def test_status_does_not_call_a_working_proxy_misconfigured_from_a_keyless_shell(
    configured: None, spawned: dict[str, Any], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """`managed` must describe the machine, not the shell that asked.

    Start the proxy with the key exported, then ask from a shell that cannot
    resolve one: comparing the recorded fingerprint against
    `key_fingerprint(None or "")` reported a healthy correctly-pointed proxy as
    "not serving this machine's configuration — restart it". Acting on that
    either hits the no-key preflight or kills a working proxy.
    """
    runner.invoke(app, ["explainability", "proxy", "up"], catch_exceptions=False)
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)

    payload = json.loads(
        runner.invoke(
            app, ["--json", "explainability", "proxy", "status"], catch_exceptions=False
        ).output
    )

    assert payload["managed"] is True, payload["summary"]
    assert "restart" not in payload["summary"].lower(), payload["summary"]
