"""Per-launch, loopback OTLP/JSON receiver for an agent's native telemetry.

For an agent with ``capabilities.native_telemetry``: the exporter's launch arguments are
its adapter's (``AgentAdapter.telemetry_args``), and its ``telemetry_reader`` says whether
the operator's own config exports already, what its tools' shells are kept from, and which
attributes of each log record the receiver keeps (``TelemetryReader.decode``; Codex's,
``core.agent_adapters.codex_config``). The receiver is started for one launch of one agent
(``--agent``), and parses OTLP itself.

The receiver has no provider credentials and never proxies model requests.
It writes redacted metadata into the existing spool; the existing ship command
owns gateway authentication, delivery and retry. It exits with its owner.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, MutableMapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aisquare.core import insights, orchestrator, outbox, paths, selfcli, spawn
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import AgentAdapter, LaunchPart, TelemetryReader
from aisquare.core.agent_sessions import METADATA_PRUNE_INTERVAL, prune_metadata
from aisquare.core.agent_sessions import NATIVE_METADATA_TTL as NATIVE_METADATA_TTL
from aisquare.core.store import store_session

MAX_BYTES = 2_000_000
HEADERS_ENV = "OTEL_EXPORTER_OTLP_LOGS_HEADERS"
"""OTLP's headers variable for the logs exporter, where the receiver's ``Authorization``
rides: a process's environment is its owner's to read (``/proc/<pid>/environ``), its
argv every local user's. Codex 0.162.1's otlp-http exporter sends it, and prefers it to
the general ``OTEL_EXPORTER_OTLP_HEADERS`` (both measured)."""
TOKEN_PREFIX = "aisquare-otlp-"
_OWN_HEADERS = re.compile(rf"Authorization=Bearer%20{TOKEN_PREFIX}[A-Za-z0-9_-]+")
_config_stamp: tuple[object, ...] | None = None


class InvalidPayload(ValueError):
    """Malformed OTLP JSON cannot succeed on retry."""


def _objects(value: Any) -> list[dict[str, Any]]:
    """Skip malformed members; proto3 JSON null means the field is absent."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _attributes(items: Any) -> dict[str, object]:
    """Every scalar attribute of an OTLP attribute list, as given: what the agent's reader
    keeps of them is its own (``TelemetryReader.decode``), before anything is written."""
    result: dict[str, object] = {}
    for item in _objects(items):
        key = item.get("key")
        value = item.get("value", {})
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        for scalar in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if scalar in value:
                raw = value[scalar]
                if isinstance(raw, (str, int, float, bool)):
                    result[key] = raw
                break
    return result


def events(payload: dict[str, Any], reader: TelemetryReader) -> Iterator[dict[str, object]]:
    """The agent's OTLP logs, the canonical model/tool event stream, each record as its
    ``reader`` keeps it (``TelemetryReader.decode``), token counts normalised.

    Trace spans can describe the same usage again, so they are not replayed
    alongside the logs. Rust's logger can omit the event timestamp (zero)
    and set only the observed timestamp. Preserve that identity on retries
    without collapsing distinct calls that happen to have identical usage.
    """
    resources = payload.get("resourceLogs")
    if resources is not None and (
        not isinstance(resources, list) or (resources and not _objects(resources))
    ):
        raise InvalidPayload("OTLP resourceLogs must contain resource objects")
    for resource in _objects(resources):
        attributes = resource.get("resource")
        base = _attributes(attributes.get("attributes") if isinstance(attributes, dict) else None)
        for scope in _objects(resource.get("scopeLogs", [])):
            for log in _objects(scope.get("logRecords", [])):
                named = log.get("eventName")
                kept = reader.decode(
                    {**base, **_attributes(log.get("attributes", []))},
                    named if isinstance(named, str) and named else None,
                )
                if kept is None:
                    continue
                record = {
                    key: insights.token_count(value)
                    if "token" in key and isinstance(value, str | int | float)
                    else value
                    for key, value in kept.items()
                }
                timestamp = log.get("timeUnixNano")
                if timestamp in (None, "", "0", 0):
                    timestamp = log.get("observedTimeUnixNano", "")
                record["native_time"] = timestamp
                yield record


def _refresh_settings() -> None:
    global _config_stamp
    path = paths.config_path()
    try:
        info = path.stat()
        stamp: tuple[object, ...] = (
            path,
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_size,
            info.st_ino,
        )
    except OSError:
        stamp = (path, None)
    if stamp != _config_stamp:
        insights.reset_cache()
        _config_stamp = stamp


def _reader(agent: str) -> TelemetryReader | None:
    try:
        return get_adapter(agent).telemetry_reader
    except ValueError:
        return None


def capture(payload: dict[str, Any], launch_id: str, *, agent: str) -> int:
    """Spool what ``agent``'s launch ``launch_id`` exported, redacted; how many were queued."""
    reader = _reader(agent)
    observations = list(events(payload, reader)) if reader is not None else []
    _refresh_settings()
    if not observations or not insights.shipping_enabled() or not insights.settings().enabled:
        return 0
    count = 0
    spool_failed = False
    # The outbox owns durable retry identity independently of this board cache.
    # No board writer lock is held while files are queued.
    with store_session() as store:
        session_id = store.get_meta(f"launch-session:{launch_id}")
        session = store.get_session(session_id) if session_id else None
        marker_key = f"native-launch:{launch_id}"
        try:
            marker = json.loads(store.get_meta(marker_key) or "{}")
        except ValueError:
            marker = {}
        project_id = (
            session.project_id
            if session
            else (marker.get("project_id") if isinstance(marker, dict) else None)
        )
        project_id = project_id or orchestrator.team_project().id
        updates = {marker_key: json.dumps({"seen_at": time.time(), "project_id": project_id})}
        dedup = store.list_meta(f"native-event:{launch_id}:")
        providers = store.list_meta(f"native-provider:{launch_id}:")
        try:
            for native in observations:
                if native.get("provider_name") and native.get("conversation.id"):
                    provider_key = f"native-provider:{launch_id}:{native['conversation.id']}"
                    provider = insights._outbound(str(native["provider_name"]))
                    if providers.get(provider_key) != provider:
                        providers[provider_key] = provider
                        updates[provider_key] = provider
            with outbox.retry_batch() as batch:
                for native in observations:
                    # Redact before writing anything destined for the gateway. No raw
                    # body, prompt, tool parameters, authorization or exporter headers.
                    clean = {
                        key: insights._outbound(value) if isinstance(value, str) else value
                        for key, value in native.items()
                    }
                    digest = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
                    dedup_key = f"native-event:{launch_id}:{digest}"
                    if dedup_key in dedup:
                        continue
                    # Provider is reported at conversation start, not on each SSE
                    # usage event. Enrich after hashing so a late startup observation
                    # cannot make a retried usage record count twice.
                    known_provider = providers.get(
                        f"native-provider:{launch_id}:{native.get('conversation.id')}"
                    )
                    if known_provider:
                        clean.setdefault("provider_name", known_provider)
                    record: dict[str, object] = {
                        "v": insights.RECORD_VERSION,
                        "kind": "native_event",
                        "agent": agent,
                        "at": datetime.now(UTC).isoformat(),
                        "run_key": launch_id,
                        "session_id": session_id,
                        "project_id": project_id,
                        "text": "",
                        "native": clean,
                    }
                    queued = batch.enqueue(record, dedup_key)
                    if queued is None:
                        spool_failed = True
                        break  # One failed write per request on a permanently unwritable queue.
                    updates[dedup_key] = "1"
                    dedup[dedup_key] = "1"
                    count += int(queued)
        except BaseException:
            try:
                with store.transaction():
                    for key, value in updates.items():
                        store.set_meta(key, value)
            except Exception as checkpoint_error:
                outbox.report_failure(checkpoint_error)
            raise
        else:
            with store.transaction():
                for key, value in updates.items():
                    store.set_meta(key, value)
    if not spool_failed:
        outbox.clear_failure()
    return count


def serve(
    ready: Path,
    owner_pid: int,
    launch_id: str,
    *,
    agent: str,
    owner_alive: Callable[[], bool] | None = None,
) -> None:
    """Receive ``agent``'s OTLP logs for launch ``launch_id`` on a loopback port, until the
    owner (``owner_pid``, else ``owner_alive``) is gone; ``ready`` says the port and token."""
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer

    token = TOKEN_PREFIX + secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        timeout = 0.5

        def do_POST(self) -> None:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BYTES:
                    self.send_error(413)
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("OTLP request must be an object")
            except Exception:
                self.send_error(400)
                return
            try:
                capture(payload, launch_id, agent=agent)
            except InvalidPayload:
                self.send_error(400)
                return
            except OSError as exc:
                outbox.report_failure(exc)
                # Only known permanent write failures are acknowledged. Unknown
                # observer bugs must be visible and must not silently lose a batch.
                if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    self.send_error(503)
                    return
            except sqlite3.Error as exc:
                outbox.report_failure(exc)
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                if code != sqlite3.SQLITE_READONLY and "readonly database" not in str(exc).lower():
                    self.send_error(503)
                    return
            except Exception as exc:
                outbox.report_failure(exc)
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 0.5
        staging = ready.with_suffix(".tmp")
        staging.write_text(json.dumps({"port": server.server_port, "token": token}))
        os.chmod(staging, 0o600)
        staging.replace(ready)
        last_pruned = 0.0
        try:
            while owner_alive is None or owner_alive():
                # Avoid opening the board on each socket timeout. The store's
                # own gate coordinates maintenance with other hook processes.
                if time.monotonic() - last_pruned >= METADATA_PRUNE_INTERVAL:
                    with suppress(Exception), store_session() as store:
                        prune_metadata(store)
                    last_pruned = time.monotonic()
                server.handle_request()
                if owner_alive is None:
                    try:
                        os.kill(owner_pid, 0)
                    except ProcessLookupError:
                        break
        finally:
            with suppress(Exception), store_session() as store:
                store.clear_native_launch(launch_id)
            with suppress(OSError):
                ready.unlink(missing_ok=True)
            with suppress(OSError):
                ready.parent.rmdir()


def disown_headers(env: MutableMapping[str, str]) -> None:
    """Drop a receiver's header this launch inherited: a launch from a traced agent's tool
    shell carries that agent's (:data:`HEADERS_ENV`), which authenticates its receiver,
    never this one's, and would replace an exporter's own headers. Only ever ours."""
    if _OWN_HEADERS.fullmatch(env.get(HEADERS_ENV, "")):
        del env[HEADERS_ENV]


def start_receiver(
    env: dict[str, str], adapter: AgentAdapter, keep_out: Sequence[str] = ()
) -> tuple[LaunchPart, str]:
    """Start only for an opted-in launch. Failure costs telemetry, never launch.

    The receiver's header (:data:`HEADERS_ENV`) is kept out of the shells the agent's
    tools run in, beside ``keep_out``, the patterns the operator's config already keeps
    out (``TelemetryReader.tool_shell_excludes``): every OpenTelemetry SDK in a process a
    tool starts
    reads that variable, where it would send the token to the app's own collector in
    place of the app's headers (review r2 of #113, finding 5)."""
    if os.name != "posix":
        return (
            LaunchPart(),
            f"{adapter.label} native telemetry requires POSIX or WSL; native launch is available",
        )
    directory = Path(tempfile.mkdtemp(prefix="aisquare-otel-"))
    ready = directory / "ready.json"
    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen(
            selfcli.isolated_argv_for(
                [
                    "--ready",
                    str(ready),
                    "--owner-pid",
                    str(os.getpid()),
                    "--launch-id",
                    env["AISQUARE_LAUNCH_ID"],
                    "--agent",
                    adapter.id,
                ],
                module="aisquare.services.native_telemetry",
            ),
            env=spawn.untraced_env(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 2
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
        if not ready.exists():
            raise RuntimeError("local telemetry receiver did not start")
        state = json.loads(ready.read_text())
        # Native exporter does asynchronous delivery. No model routing or auth changes.
        endpoint = f"http://127.0.0.1:{state['port']}/v1/logs"
        exporter = adapter.telemetry_args(endpoint, keep_out=[*keep_out, HEADERS_ENV])
        env[HEADERS_ENV] = "Authorization=" + quote("Bearer " + state["token"], safe="")
        env["AISQUARE_PIPELINE_ID"] = env["AISQUARE_LAUNCH_ID"]
        env["AISQUARE_TRACE_AGENT_NAME"] = "aisquare-" + env.get("AISQUARE_ROLE", "coder")
        return (
            exporter,
            f"{adapter.label} native telemetry enabled "
            "(local receiver; gateway delivery uses ship)",
        )
    except Exception as exc:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()
        return LaunchPart(), f"{adapter.label} launched without native telemetry: {exc}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--agent", required=True)
    options = parser.parse_args()
    serve(options.ready, options.owner_pid, options.launch_id, agent=options.agent)
