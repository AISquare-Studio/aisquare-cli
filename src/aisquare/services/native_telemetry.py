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
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aisquare.core import insights, outbox, selfcli, spawn
from aisquare.core.store import store_session

MAX_BYTES = 2_000_000


def operator_configured(config_dir: Path, args: list[str]) -> bool:
    """Conservatively preserve native exporter config at any effective layer."""
    if any("otel." in arg or arg.startswith("otel=") for arg in args):
        return True
    files = {config_dir / "config.toml", Path("/etc/codex/config.toml")}
    files.update(config_dir.glob("*.config.toml"))
    for parent in (Path.cwd(), *Path.cwd().parents):
        files.add(parent / ".codex" / "config.toml")
    for path in files:
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            config = tomllib.load(handle)
        if config.get("otel") or any(p.get("otel") for p in config.get("profiles", {}).values()):
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


def _attributes(items: list[dict[str, Any]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        key = item.get("key")
        value = item.get("value", {})
        if key not in _FIELDS or not isinstance(value, dict):
            continue
        for scalar in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if scalar in value:
                result[key] = value[scalar]
                break
    return result


def events(payload: dict[str, Any]) -> Iterator[dict[str, object]]:
    """Documented Codex OTLP logs, the canonical model/tool event stream.

    Trace spans can describe the same usage again, so they are not replayed
    alongside the logs. Rust's logger can omit the event timestamp (zero)
    and set only the observed timestamp. Preserve that identity on retries
    without collapsing distinct calls that happen to have identical usage.
    """
    for resource in payload.get("resourceLogs", []):
        base = _attributes(resource.get("resource", {}).get("attributes", []))
        for scope in resource.get("scopeLogs", []):
            for log in scope.get("logRecords", []):
                record = {**base, **_attributes(log.get("attributes", []))}
                record["event.name"] = (
                    record.get("event.name") or log.get("eventName") or "codex.event"
                )
                timestamp = log.get("timeUnixNano")
                if timestamp in (None, "", "0", 0):
                    timestamp = log.get("observedTimeUnixNano", "")
                record["native_time"] = timestamp
                yield record


def capture(payload: dict[str, Any], launch_id: str) -> int:
    insights.reset_cache()
    if not insights.shipping_enabled() or not insights.settings().enabled:
        return 0
    count = 0
    with store_session() as store:
        session_id = store.get_meta(f"launch-session:{launch_id}")
        session = store.get_session(session_id) if session_id else None
        observations = list(events(payload))
        for native in observations:
            if native.get("provider_name") and native.get("conversation.id"):
                store.set_meta(
                    f"native-provider:{launch_id}:{native['conversation.id']}",
                    insights._outbound(str(native["provider_name"])),
                )
        for native in observations:
            # Redact before writing anything destined for the gateway. No raw
            # body, prompt, tool parameters, authorization or exporter headers.
            clean = {
                key: insights._outbound(value) if isinstance(value, str) else value
                for key, value in native.items()
            }
            digest = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
            dedup_key = f"native-event:{launch_id}:{digest}"
            if store.get_meta(dedup_key):
                continue
            # Provider is reported at conversation start, not on each SSE
            # usage event. Enrich after hashing so a late startup observation
            # cannot make a retried usage record count twice.
            provider = store.get_meta(
                f"native-provider:{launch_id}:{native.get('conversation.id')}"
            )
            if provider:
                clean.setdefault("provider_name", provider)
            record: dict[str, object] = {
                "v": insights.RECORD_VERSION,
                "kind": "native_event",
                "agent": "codex",
                "at": datetime.now(UTC).isoformat(),
                "run_key": launch_id,
                "session_id": session_id,
                "project_id": session.project_id if session else None,
                "text": "",
                "native": clean,
            }
            if outbox.enqueue(record) is not None:
                store.set_meta(dedup_key, "1")
                count += 1
    return count


def serve(ready: Path, owner_pid: int, launch_id: str) -> None:
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer

    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
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
                capture(payload, launch_id)
            except Exception:
                self.send_error(400)
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
        try:
            while True:
                server.handle_request()
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    break
        finally:
            ready.unlink(missing_ok=True)
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
