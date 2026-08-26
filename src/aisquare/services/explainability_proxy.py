"""Start, stop and describe the local claude_code proxy.

The proxy is a separate process by design — it holds the workspace key, and the
CLI-to-proxy hop is loopback and unauthenticated precisely because it is not
shared. That design is right and this module does not change it. What it removes
is the four-variable incantation an operator had to assemble by hand out of
config the CLI already holds: the gateway URL, the key, the port, and the name of
the console script.

THREE THINGS THIS HAS TO GET RIGHT, because each one has already bitten someone:

*Whose proxy is that.* ``doctor``'s proxy row goes green when *a* service answers
``/health`` as ``aisquare-proxy`` in ``claude_code`` mode. It cannot tell a proxy
this machine started from one left over from last week pointed at staging, and a
misattributed Run is worse than a missing one. So ``up`` records what it started
and ``status`` reports ``managed`` versus ``foreign`` — never just "healthy".

*A port that is already taken.* The proxy prints ``Application startup complete``
and *then* fails to bind, so its own output cannot be trusted as proof. ``up``
therefore polls ``/health`` until the service identifies itself, and treats a
listener that answers as something else as a refusal rather than a success.

*The key must not reach the process table.* It goes in the child's environment,
never in argv — ``/proc/<pid>/cmdline`` is world-readable, and a key in a
``ps`` line is a key in every screen-share and every terminal scrollback.

NOT ON THE LAUNCH PATH. Nothing here is called by ``aisquare launch``. Tracing
must never block or slow a session, and spawning a process, binding a port and
polling health in front of a developer who just hit enter would put all three on
the hot path. ``up`` is a thing an operator types.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aisquare.core import paths
from aisquare.core.config import load_config
from aisquare.services.explainability import (
    GATEWAY_ENV_VAR,
    KEY_ENV_VAR,
    ProxyProbe,
    probe_proxy,
)
from aisquare.services.explainability_ops import ResolvedTarget, resolve_target

#: The SDK console script that runs the sidecar. Reached by name on PATH rather
#: than by importing the SDK: the proxy pulls FastAPI and uvicorn, and this
#: module is imported by the CLI's command registration, which every command
#: pays for. The script also works when the SDK lives in another environment.
PROXY_SCRIPT = "aisquare-proxy"

#: The SDK reads its port from here (``claude_proxy.PROXY_PORT``).
PORT_ENV_VAR = "AISQUARE_PROXY_PORT"

#: How long ``up`` waits for the proxy to answer /health before giving up, and
#: how often it asks. Ten seconds is generous for a local uvicorn boot and short
#: enough that a wedged start is reported rather than waited on.
_BOOT_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 0.25

#: How long ``down`` waits for a SIGTERM to be honoured before escalating. The
#: SDK installs a SIGTERM handler that flushes queued spans to its local inbox,
#: so killing early costs exactly the traces this exists to collect.
_STOP_TIMEOUT_SECONDS = 8.0


class ProxyError(RuntimeError):
    """A proxy operation failed in a way the operator has to act on.

    Raised rather than returned because these commands are explicit requests:
    ``up`` that could not start anything must not exit 0. This is the opposite
    of the launch path's fail-open rule, and deliberately so.
    """


def key_fingerprint(key: str) -> str:
    """A short, non-secret witness that two keys are the same key.

    Recorded so a deployment switch can be DETECTED. Rotating the key under one
    target changes it too, which is correct: the running proxy is holding the old
    credential and the gateway will reject it, so it needs replacing either way.

    Twelve hex characters of SHA-256. Never the key, never enough of it to
    shorten a search for it, and it lands in a mode-644 file beside the join log.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _boot_id() -> str:
    """This boot's identifier, or "?" where the kernel does not publish one.

    ``/proc/sys/kernel/random/boot_id`` changes on every boot, which is exactly
    the axis ``starttime`` cannot express.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "?"


def _port_went_quiet(url: str, timeout: float = _STOP_TIMEOUT_SECONDS) -> bool:
    """Whether nothing answers ``url`` any more, within ``timeout``.

    A stop that reports success is not proof the listener is gone: sockets linger
    and a second proxy may be sharing the port. Starting a replacement while
    anything still answers means the replacement's own startup poll can be
    satisfied by the process it was meant to replace.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not probe_proxy(url, timeout=1.0).healthy:
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return not probe_proxy(url, timeout=1.0).healthy


def _identity_token(pid: int) -> str | None:
    """A token that changes if this pid stops being the process we started.

    PIDs ARE REUSED, and the record deliberately outlives a reboot, so
    ``os.kill(pid, 0)`` proves only that *something* holds that number. Without
    this, a stale record plus a recycled pid means SIGTERM to a stranger.

    Two sources, both reading the OS rather than guessing:

    * Linux — field 22 of ``/proc/<pid>/stat`` is the process start time in
      clock ticks since boot, paired here with argv[0]. The field is counted
      from after the LAST ``)`` because ``comm`` sits in parentheses and may
      itself contain spaces and parentheses; splitting from the left is the
      classic way to misparse this file.
    * Elsewhere — ``ps -o lstart=,comm=``, which carries the same two facts.

    Returns None when neither is available, and every caller treats that as
    "cannot verify" rather than "verified". Refusing to act on an unverifiable
    pid is the only safe direction: the cost is an operator stopping a proxy by
    hand, and the cost of guessing wrong is killing something else.
    """
    stat = Path(f"/proc/{pid}/stat")
    try:
        raw = stat.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    if raw:
        # THE BOOT ID IS NOT OPTIONAL, because the record deliberately survives a
        # reboot and `starttime` is measured in ticks SINCE boot. Without it the
        # token repeats across the exact boundary it exists to protect: after the
        # next boot the same pid can hold a different process started at the same
        # tick with the same basename — and for a console script that basename is
        # usually `python`, so the collision is not even unlikely. Same token,
        # false ownership, SIGTERM to a stranger.
        boot_id = _boot_id()
        tail = raw.rpartition(")")[2].split()
        # tail[0] is `state`, i.e. field 3, so starttime (field 22) is tail[19].
        if len(tail) > 19:
            try:
                argv0 = (
                    Path(f"/proc/{pid}/cmdline")
                    .read_bytes()
                    .split(b"\0")[0]
                    .decode("utf-8", "replace")
                )
            except OSError:
                argv0 = ""
            return f"proc:{boot_id}:{tail[19]}:{Path(argv0).name}"

    ps = shutil.which("ps")
    if ps is None:
        return None
    try:
        # Resolved path from shutil.which, fixed argv, no shell.
        completed = subprocess.run(
            [ps, "-p", str(pid), "-o", "lstart=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return "ps:" + " ".join(completed.stdout.split())


@dataclass(frozen=True)
class ProxyRecord:
    """What ``up`` started, as it was recorded.

    ``target``, ``gateway_url`` and ``key_fp`` are not diagnostics — they are the
    reuse check. Staging and production normally share one loopback URL, so
    matching on the URL alone would hand a prod-labelled session to a proxy still
    holding staging's gateway and key. ``identity`` is the same idea applied to
    the process: it answers "is this still the thing we started".
    """

    pid: int
    port: int
    url: str
    gateway_url: str
    target: str
    key_fp: str
    identity: str | None
    started_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "port": self.port,
            "url": self.url,
            "gateway_url": self.gateway_url,
            "target": self.target,
            "key_fp": self.key_fp,
            "identity": self.identity,
            "started_at": self.started_at,
        }


@dataclass(frozen=True)
class ProxyStatus:
    """The answer to "is a proxy up, and is it ours"."""

    running: bool
    #: Ours, healthy, AND serving the deployment this machine names. The field
    #: to branch on; `probe.healthy` alone never answered "whose proxy is that".
    managed: bool
    #: Started by this CLI and still the same process — true even when it is
    #: wedged or pointed at a target we have since switched away from, because
    #: those are exactly the cases `up` must be allowed to replace and `down`
    #: must be allowed to stop.
    owned: bool
    url: str
    probe: ProxyProbe
    record: ProxyRecord | None
    pid: int | None
    age_seconds: float | None
    gateway_url: str
    target: str
    #: The target the RUNNING proxy was started for, when we own it. Differs
    #: from `target` exactly when someone switched deployment without restarting.
    recorded_target: str | None

    @property
    def summary(self) -> str:
        """One line, naming the thing that is easy to get wrong."""
        if not self.running:
            return f"not running (nothing answers at {self.url})"
        if not self.probe.healthy:
            return f"something answers at {self.url} but it is not our proxy: {self.probe.reason}"
        if self.managed:
            age = _humanise(self.age_seconds)
            return f"running (pid {self.pid}, started {age} ago) at {self.url} → {self.gateway_url}"
        if self.owned and self.recorded_target and self.recorded_target != self.target:
            return (
                f"running (pid {self.pid}) at {self.url}, but it was started for target "
                f"'{self.recorded_target}' → {self.gateway_url}, and this machine now names "
                f"'{self.target}'. Its Runs are going to the OLD deployment — restart it: "
                "aisquare explainability proxy up"
            )
        if self.owned:
            return (
                f"our proxy (pid {self.pid}) is running at {self.url} but not serving this "
                f"machine's configuration — restart it: aisquare explainability proxy up"
            )
        # The case doctor cannot distinguish, said out loud.
        return (
            f"running at {self.url}, but NOT started by this CLI — its gateway and key "
            "are unknown from here, so the Runs it records may not be this machine's"
        )


def _humanise(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


def _port_of(url: str) -> int:
    """The port the configured proxy URL names, with http's default filled in."""
    parsed = urlparse(url)
    if parsed.port:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def read_record() -> ProxyRecord | None:
    """The recorded proxy, or None when there is none or it is unreadable.

    A corrupt record is treated as absent rather than fatal: it means the CLI
    has lost track of a process, which is what ``status`` should say, and a
    traceback here would block the one command that could explain it.
    """
    path = paths.explainability_proxy_state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return ProxyRecord(
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            url=str(raw["url"]),
            gateway_url=str(raw["gateway_url"]),
            target=str(raw["target"]),
            key_fp=str(raw["key_fp"]),
            identity=(str(raw["identity"]) if raw.get("identity") is not None else None),
            started_at=float(raw["started_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write_record(record: ProxyRecord) -> None:
    path = paths.explainability_proxy_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record.to_json(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _clear_record() -> None:
    with contextlib.suppress(OSError):
        paths.explainability_proxy_state_path().unlink()


def _alive(pid: int) -> bool:
    """Whether this pid exists. Signal 0 checks without delivering anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists and belongs to someone else — for our purposes, alive.
        return True
    return True


def _owns(record: ProxyRecord | None, url: str) -> bool:
    """Whether the recorded process is still the one this CLI started.

    Deliberately three conditions and not one. `_alive` alone was the original
    bug: the record outlives a reboot on purpose, pids are recycled, and
    `os.kill(pid, 0)` cannot tell a recycled pid from ours — so `down` could
    SIGTERM a stranger that happened to inherit the number.

    An UNVERIFIABLE identity counts as not-ours. That is the conservative
    direction: the cost is telling an operator to stop a proxy by hand, and the
    cost of the other direction is killing somebody else's process.
    """
    if record is None or record.url != url or not _alive(record.pid):
        return False
    if record.identity is None:
        return False
    return _identity_token(record.pid) == record.identity


def _serves_this_deployment(record: ProxyRecord, target: ResolvedTarget) -> bool:
    """Whether a proxy we own is pointed where this machine is now pointed.

    THE REASON THIS EXISTS. Staging and production normally share one loopback
    proxy URL, so a target switch changes nothing the URL can see: the old proxy
    keeps running, holding the old gateway and the old key, and reusing it sends
    prod-labelled sessions to staging. Matching on the URL alone made that
    outcome a green ✓.
    """
    return (
        record.target == target.name
        and record.gateway_url == target.gateway_url
        and record.key_fp == key_fingerprint(target.api_key or "")
    )


def status() -> ProxyStatus:
    """Describe the proxy: up or not, ours or not, and pointed where."""
    settings = load_config().explainability
    target = resolve_target(settings)
    url = target.proxy_url
    probe = probe_proxy(url)
    record = read_record()
    owned = _owns(record, url)
    # `managed` is the strong claim, and every conjunct is load-bearing. Ours,
    # AND still pointed at the deployment this machine names, AND actually
    # answering. Dropping the last one let `up` print "✓ … sessions are traced"
    # directly beneath "not running" for a wedged process; dropping the middle
    # one let a stale staging proxy serve production.
    matches = bool(record and owned and _serves_this_deployment(record, target))
    managed = bool(matches and probe.healthy)
    return ProxyStatus(
        running=probe.healthy or "unreachable" not in probe.reason,
        managed=managed,
        owned=owned,
        url=url,
        probe=probe,
        record=record if owned else None,
        pid=record.pid if owned and record else None,
        age_seconds=(time.time() - record.started_at) if owned and record else None,
        gateway_url=record.gateway_url if owned and record else target.gateway_url,
        target=target.name,
        recorded_target=record.target if owned and record else None,
    )


def up(*, log_path: Path | None = None) -> ProxyStatus:
    """Start the proxy from configured values and wait for it to answer.

    Idempotent: an already-running proxy of ours is returned as-is rather than
    started twice, because two proxies on one port means one of them silently
    lost the bind and nobody can say which is recording.
    """
    settings = load_config().explainability
    target = resolve_target(settings)

    existing = status()
    # Idempotent ONLY on the strong claim: ours, healthy, and serving this
    # deployment. The early return used to fire on ownership alone, which made
    # two different failures print success — a wedged process ("✓ … sessions are
    # traced" directly under "not running"), and a proxy still holding the
    # previous target's gateway and key after a deployment switch, quietly
    # sending prod-labelled sessions to staging.
    if existing.managed:
        return existing

    # PREFLIGHT BEFORE TOUCHING ANYTHING THAT IS RUNNING. These checks used to
    # sit below the replacement, so a cutover to a target with no gateway, or a
    # machine that had lost the console script, stopped a working proxy and only
    # then discovered it could not start another — trading a proxy on the wrong
    # deployment for no proxy at all, which is strictly worse because nothing is
    # recorded either way and only one of the two states is obvious.
    if not target.gateway_url:
        raise ProxyError(
            f"target '{target.name}' has no gateway URL — the proxy would have nowhere "
            "to send spans. Set one: aisquare explainability enable "
            f"--target {target.name} --gateway-url <url>"
        )
    if not target.api_key:
        raise ProxyError(
            f"no workspace key resolved for target '{target.name}' (looked at "
            f"${target.api_key_env} and {paths.aisquare_home() / 'explainability-key'}). "
            "The gateway rejects an unauthenticated proxy, so starting one would only "
            "look like it worked."
        )

    script = shutil.which(PROXY_SCRIPT)
    if script is None:
        raise ProxyError(
            f"{PROXY_SCRIPT} is not on PATH — the explainability extra provides it: "
            'pip install --upgrade "aisquare-cli[explainability]"'
        )

    port = _port_of(target.proxy_url)
    parsed = urlparse(target.proxy_url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        # The SDK refuses to bind beyond loopback without inbound keys rather
        # than become an open relay, so a remote proxy_url is somebody else's
        # service and starting a local one would not be what was asked.
        raise ProxyError(
            f"{target.proxy_url} is not a loopback address, so it names a proxy this "
            "machine does not own. `up` only starts local sidecars."
        )

    # The key travels in the environment and NEVER in argv — /proc/<pid>/cmdline
    # is world-readable. The gateway URL goes the same way for symmetry, and
    # because the SDK reads both from its own env contract.
    env = dict(os.environ)
    env[GATEWAY_ENV_VAR] = target.gateway_url
    env[KEY_ENV_VAR] = target.api_key
    env[PORT_ENV_VAR] = str(port)

    if existing.owned:
        # Ours, so we may replace it — and must, because neither remaining state
        # is usable: it is either not answering, or answering for the wrong
        # deployment.
        #
        # A FAILED STOP IS A HARD ERROR, and this is the subtle one. Ignoring
        # `_terminate`'s result let the old proxy survive, keep the port, and
        # ANSWER THE NEW CHILD'S STARTUP POLL — so `up` recorded the new pid and
        # reported the new deployment as managed while every span still went to
        # the old gateway. The health poll cannot distinguish two proxies on one
        # port; only refusing to proceed can. The record is retained on purpose:
        # the process it names is still alive and still ours to stop.
        if existing.pid and not _terminate(existing.pid):
            raise ProxyError(
                f"could not stop the proxy we started (pid {existing.pid}) at "
                f"{target.proxy_url} — it is running as another user, or refusing both "
                "SIGTERM and SIGKILL. NOT starting a replacement: it would fail to bind "
                "and the old proxy would answer for it, so this machine would report the "
                "new deployment while shipping to the old one. Stop pid "
                f"{existing.pid} by hand, then re-run."
            )
        # And confirm the port actually went quiet. A stop that reports success
        # while something still answers is the same failure one step later.
        if not _port_went_quiet(target.proxy_url):
            raise ProxyError(
                f"stopped the proxy we started, but something is still answering at "
                f"{target.proxy_url}. NOT starting a replacement — a second listener "
                "would satisfy the startup poll and the spans would go somewhere this "
                "CLI cannot see."
            )
        _clear_record()
    elif existing.probe.healthy:
        raise ProxyError(
            f"a proxy is already answering at {target.proxy_url}, and this CLI did not "
            "start it. Stop it yourself, or point this machine at it with: "
            f"aisquare explainability enable --proxy-url {target.proxy_url}"
        )

    destination = log_path or (paths.log_dir() / "explainability-proxy.log")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = destination.open("ab")
    try:
        # start_new_session detaches it from this process group, so the proxy
        # outlives the shell that started it and does not take a Ctrl-C meant
        # for the CLI. That is the whole point of a sidecar.
        # Absolute path from shutil.which, a fixed argv, and no shell.
        process = subprocess.Popen(
            [script],
            stdout=handle,
            stderr=handle,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        handle.close()
        raise ProxyError(f"could not start {script}: {exc}") from exc
    handle.close()

    deadline = time.monotonic() + _BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ProxyError(
                f"{PROXY_SCRIPT} exited immediately (code {process.returncode}). "
                f"Its output is in {destination}"
            )
        probe = probe_proxy(target.proxy_url, timeout=1.0)
        if probe.healthy:
            identity = _identity_token(process.pid)
            if identity is None:
                # We just started something we could never afterwards recognise:
                # `_owns` rejects a null identity, so the process would be foreign
                # to `status` and `down` from the moment it booted — an orphan
                # holding the port, reported as a success. Refuse, and stop the
                # child we still hold a handle to.
                _terminate(process.pid)
                key_file = paths.aisquare_home() / "explainability-key"
                raise ProxyError(
                    "started the proxy but could not read its process identity on this "
                    "platform (no readable /proc and no usable `ps`), so this CLI could "
                    "never recognise or stop it again. It has been stopped rather than "
                    "left as an orphan.\n"
                    "Run the sidecar yourself instead:\n"
                    f"  EXPLAINABILITY_GATEWAY_URL={target.gateway_url} \\\n"
                    f"  EXPLAINABILITY_API_KEY=$(cat {key_file}) \\\n"
                    f"  {PORT_ENV_VAR}={port} {PROXY_SCRIPT}"
                )
            record = ProxyRecord(
                pid=process.pid,
                port=port,
                url=target.proxy_url,
                gateway_url=target.gateway_url,
                target=target.name,
                key_fp=key_fingerprint(target.api_key),
                # Taken above rather than right after Popen: until /health
                # answers the child may not have finished exec'ing, and a token
                # read from the interpreter that preceded the exec would never
                # match again.
                identity=identity,
                started_at=time.time(),
            )
            _write_record(record)
            return status()
        time.sleep(_POLL_INTERVAL_SECONDS)

    # Timed out. Do not leave a process we cannot account for behind: the
    # occupied-port case prints "Application startup complete" and then fails,
    # so a running pid is not evidence that anything is listening.
    _terminate(process.pid)
    raise ProxyError(
        f"{PROXY_SCRIPT} did not answer {target.proxy_url}/health within "
        f"{_BOOT_TIMEOUT_SECONDS:.0f}s and has been stopped. Its output is in "
        f"{destination} — a port already in use is the usual cause, and the proxy "
        "reports startup BEFORE it binds, so its own log looks healthy."
    )


@contextlib.contextmanager
def _signal_handle(pid: int) -> Iterator[Callable[[int], None]]:
    """A way to signal ``pid`` that cannot land on a different process.

    Verifying identity and then calling ``os.kill(pid, …)`` leaves a window: the
    process can exit between the two and the number can be reused, so the signal
    arrives at whatever inherited it. A pidfd refers to the PROCESS, not the
    number — once opened, ``pidfd_send_signal`` either reaches that process or
    fails with ESRCH, and can never reach a successor. Linux 5.3+, and Python
    exposes both calls only there.

    Falls back to ``os.kill`` where pidfd is unavailable. The window is real
    there, and narrow: it needs the proxy to exit and the pid to be recycled
    inside the microseconds between the check and the signal. Ownership
    verification is what makes it narrow, and this is what closes it where the
    kernel allows.
    """

    def by_pid(sig: int) -> None:
        os.kill(pid, sig)

    opener = getattr(os, "pidfd_open", None)
    sender = getattr(signal, "pidfd_send_signal", None)
    if opener is None or sender is None:
        yield by_pid
        return

    try:
        fd = opener(pid)
    except (OSError, AttributeError):
        yield by_pid
        return

    def by_pidfd(sig: int) -> None:
        sender(fd, sig)

    try:
        yield by_pidfd
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _terminate(pid: int) -> bool:
    """SIGTERM, then SIGKILL if it will not go. True when it is gone."""
    try:
        with _signal_handle(pid) as send:
            try:
                send(signal.SIGTERM)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if not _alive(pid):
                    return True
                time.sleep(_POLL_INTERVAL_SECONDS)
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                send(signal.SIGKILL)
    except ProcessLookupError:
        # pidfd_open on a pid that is already gone.
        return True
    return not _alive(pid)


def down() -> str:
    """Stop the proxy this CLI started. Returns what happened.

    Refuses to touch a proxy it did not start. Killing a process on the strength
    of "something is listening on 9090" is how you take down a colleague's
    session, or a hosted proxy someone deliberately pointed this machine at.
    """
    record = read_record()
    if record is None:
        current = status()
        if current.probe.healthy:
            return (
                f"a proxy is running at {current.url} but this CLI did not start it — "
                "leaving it alone. Stop it where you started it."
            )
        return "no proxy started by this CLI, and nothing is answering"
    if not _alive(record.pid):
        _clear_record()
        return f"proxy (pid {record.pid}) was already gone — cleared the stale record"
    if not _owns(record, record.url):
        # Alive, but not the process we started: the record outlives a reboot and
        # pids get recycled, so this number now belongs to something else — or to
        # something we cannot identify on this platform. Either way, not ours to
        # signal.
        _clear_record()
        return (
            f"pid {record.pid} is alive but is NOT the proxy we started (pids are reused) "
            "— cleared the stale record and left that process alone"
        )
    if not _terminate(record.pid):
        raise ProxyError(
            f"could not stop pid {record.pid} — it is running as another user, or "
            "refusing both SIGTERM and SIGKILL"
        )
    _clear_record()
    return f"stopped the proxy (pid {record.pid}) at {record.url}"
