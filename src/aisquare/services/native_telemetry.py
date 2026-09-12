"""Per-launch, loopback OTLP/JSON receiver for Codex's native telemetry.

The receiver has no provider credentials and never proxies model requests.
It writes redacted metadata into the existing spool; the existing ship command
owns gateway authentication, delivery and retry. It exits with its owner.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core import insights, orchestrator, outbox, paths, selfcli, spawn
from aisquare.core.agent_adapters.types import option_values
from aisquare.core.agent_sessions import METADATA_PRUNE_INTERVAL, prune_metadata
from aisquare.core.agent_sessions import NATIVE_METADATA_TTL as NATIVE_METADATA_TTL
from aisquare.core.store import is_locked_error, store_session

MAX_BYTES = 2_000_000
SYSTEM_CONFIG = Path("/etc/codex/config.toml")
_config_stamp: tuple[object, ...] | None = None


class NativeConfigError(ValueError):
    """A config layer could contain operator settings we cannot safely inspect."""


def _read_config(path: Path, layer: str) -> dict[str, Any]:
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("not a regular file")
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise NativeConfigError(
            f"Cannot inspect Codex {layer} config {path}: {exc}. "
            "Native telemetry configuration left unchanged."
        ) from exc


def operator_configured(config_dir: Path, args: list[str]) -> bool:
    """Preserve exporters in native user/system config and the selected profile.

    Codex ignores `otel` in project-local config. Other account homes and
    unselected profile files are not layers of this launch either.
    """
    overrides: list[dict[str, Any]] = []
    for value in option_values(args, "-c", "--config"):
        key, separator, raw = value.partition("=")
        if not separator:
            continue
        try:
            try:
                parsed = tomllib.loads(value)
            except tomllib.TOMLDecodeError:
                # Codex accepts an unquoted string as a config override value.
                parsed = tomllib.loads(f"{key}={json.dumps(raw)}")
        except tomllib.TOMLDecodeError:
            continue  # Native argument validation belongs to Codex.
        if "otel" in parsed:
            return True
        # Keep each dotted override: a shallow update would erase sibling keys
        # under profiles when a later -c configures something else there.
        overrides.append(parsed)
    configs = [
        _read_config(SYSTEM_CONFIG, "system"),
        _read_config(config_dir / "config.toml", "user"),
        *overrides,
    ]
    profile = next((config["profile"] for config in reversed(configs) if "profile" in config), None)
    for value in option_values(args, "-p", "--profile"):
        profile = value
    if (
        not isinstance(profile, str)
        or "\x00" in profile
        or Path(profile).name != profile
        or profile in {".", ".."}
    ):
        profile = None
    if profile:
        configs.append(_read_config(config_dir / f"{profile}.config.toml", f"profile {profile!r}"))
    for config in configs:
        if "otel" in config:
            return True
        legacy_profiles = config.get("profiles", {})
        if profile and isinstance(legacy_profiles, dict):
            selected = legacy_profiles.get(profile, {})
            if isinstance(selected, dict) and "otel" in selected:
                return True
    return False


_FIELDS = frozenset(
    {
        "event.name",
        "event",
        "model",
        "model_name",
        "provider_name",
        "conversation.id",
        "thread.id",
        "turn.id",
        "tool_name",
        "tool.name",
        "input_token_count",
        "output_token_count",
        "cached_token_count",
        "cached_input_token_count",
        "reasoning_output_token_count",
        "duration_ms",
        "success",
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "tool.execution_time_ms",
        "response_id",
        "call_id",
    }
)


class InvalidPayload(ValueError):
    """Malformed OTLP JSON cannot succeed on retry."""


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidPayload("OTLP object expected")
    return value


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise InvalidPayload("OTLP array of objects expected")
    return value


def _attributes(items: list[dict[str, Any]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in _objects(items):
        key = item.get("key")
        value = item.get("value", {})
        if not isinstance(key, str) or key not in _FIELDS or not isinstance(value, dict):
            continue
        for scalar in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if scalar in value:
                raw = value[scalar]
                if isinstance(raw, (str, int, float, bool)):
                    result[key] = insights.token_count(raw) if "token" in key else raw
                break
    return result


def events(payload: dict[str, Any]) -> Iterator[dict[str, object]]:
    """Documented Codex OTLP logs, the canonical model/tool event stream.

    Trace spans can describe the same usage again, so they are not replayed
    alongside the logs. Rust's logger can omit the event timestamp (zero)
    and set only the observed timestamp. Preserve that identity on retries
    without collapsing distinct calls that happen to have identical usage.
    """
    for resource in _objects(_object(payload).get("resourceLogs", [])):
        base = _attributes(_object(resource.get("resource", {})).get("attributes", []))
        for scope in _objects(resource.get("scopeLogs", [])):
            for log in _objects(scope.get("logRecords", [])):
                record = {**base, **_attributes(log.get("attributes", []))}
                record["event.name"] = (
                    record.get("event.name") or log.get("eventName") or "codex.event"
                )
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


def capture(payload: dict[str, Any], launch_id: str) -> int:
    observations = list(events(payload))  # Validate the entire batch before writing anything.
    _refresh_settings()
    if not insights.shipping_enabled() or not insights.settings().enabled:
        return 0
    count = 0
    # Queue without holding SQLite's writer lock. Checkpoint the completed
    # prefix once, including on partial failure, before asking for a retry.
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
                    "agent": "codex",
                    "at": datetime.now(UTC).isoformat(),
                    "run_key": launch_id,
                    "session_id": session_id,
                    "project_id": project_id,
                    "text": "",
                    "native": clean,
                }
                if outbox.enqueue_retryable(record) is None:
                    continue  # Permanent spool failure: this observer must fail open.
                updates[dedup_key] = "1"
                dedup[dedup_key] = "1"
                count += 1
        finally:
            with store.transaction():
                for key, value in updates.items():
                    store.set_meta(key, value)
    return count


def serve(
    ready: Path,
    owner_pid: int,
    launch_id: str,
    *,
    owner_alive: Callable[[], bool] | None = None,
) -> None:
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer

    token = secrets.token_urlsafe(32)

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
                capture(payload, launch_id)
            except InvalidPayload:
                self.send_error(400)
                return
            except OSError as exc:
                if outbox.temporary_write_error(exc):
                    self.send_error(503)
                    return
            except sqlite3.OperationalError as exc:
                if is_locked_error(exc) or "disk is full" in str(exc).lower():
                    self.send_error(503)
                    return
            except Exception:
                pass  # An unrecoverable observer failure costs a trace, never a launch.
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


def start(env: dict[str, str]) -> tuple[list[str], str]:
    """Start only for an opted-in launch. Failure costs telemetry, never launch."""
    if os.name != "posix":
        return [], "Codex native telemetry requires POSIX or WSL; native launch is available"
    directory = Path(tempfile.mkdtemp(prefix="aisquare-otel-"))
    ready = directory / "ready.json"
    child: subprocess.Popen[bytes] | None = None
    try:
        child = subprocess.Popen(
            selfcli.argv_for(
                [
                    "--ready",
                    str(ready),
                    "--owner-pid",
                    str(os.getpid()),
                    "--launch-id",
                    env["AISQUARE_LAUNCH_ID"],
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
        config = "otel.exporter.otlp-http"
        args = [
            "-c",
            f"{config}.endpoint={json.dumps(endpoint)}",
            "-c",
            f'{config}.protocol="json"',
            "-c",
            f"{config}.headers.Authorization={json.dumps('Bearer ' + state['token'])}",
        ]
        args += ["-c", "otel.log_user_prompt=false"]
        env["AISQUARE_PIPELINE_ID"] = env["AISQUARE_LAUNCH_ID"]
        env["AISQUARE_TRACE_AGENT_NAME"] = "aisquare-" + env.get("AISQUARE_ROLE", "coder")
        return args, "Codex native telemetry enabled (local receiver; gateway delivery uses ship)"
    except Exception as exc:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()
        return [], f"Codex launched without native telemetry: {exc}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--launch-id", required=True)
    options = parser.parse_args()
    serve(options.ready, options.owner_pid, options.launch_id)
