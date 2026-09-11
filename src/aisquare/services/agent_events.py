"""Normalize native lifecycle observations into the shared AISquare services."""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from aisquare.core import agents
from aisquare.core.store import store_session
from aisquare.services import hooks, metrics


def session_key(agent: str, config_dir: Path, native_id: str) -> str:
    """A native ID is unique within an agent installation, not across vendors."""
    if agent == "claude-code":
        return native_id  # preserve existing board references
    return str(uuid5(NAMESPACE_URL, f"aisquare:{agent}:{config_dir.resolve()}:{native_id}"))


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def handle_codex(payload: dict[str, Any], config_dir: Path) -> str | None:
    """Codex's command hook protocol. Output is context, a Stop decision, or empty."""
    native = _text(payload, "session_id")
    event = _text(payload, "hook_event_name")
    if (
        not native
        or len(native) > 256
        or event
        not in {
            "SessionStart",
            "UserPromptSubmit",
            "Stop",
            "SessionEnd",
            "Interrupt",
            "PermissionRequest",
            "PreToolUse",
            "PostToolUse",
        }
    ):
        return None
    session_id = session_key("codex", config_dir, native)
    cwd_text = _text(payload, "cwd")
    cwd = Path(cwd_text) if cwd_text else None
    model = _text(payload, "model")
    turn = _text(payload, "turn_id")
    agents.observe_hooks("codex", config_dir)
    # These are exact launch tokens supplied by AISquare, never a search for
    # the newest transcript. Metadata may precede the fleet row without a race.
    with store_session() as store:
        for env_key, prefix in (
            ("AISQUARE_FLEET_AGENT", "fleet"),
            ("AISQUARE_LAUNCH_ID", "launch"),
        ):
            token = os.environ.get(env_key)
            if token:
                store.set_meta_once(f"{prefix}-session:{token}", session_id)
        cached_key = (
            f"agent-event:{session_id}:{event}:{turn}:{bool(payload.get('stop_hook_active'))}"
        )
        cacheable = bool(turn) and event in {"UserPromptSubmit", "Stop"}
        if cacheable:
            pending = json.dumps(
                {"pending_at": time.time(), "monotonic_at": time.monotonic(), "owner": uuid4().hex}
            )
            claimed = store.set_meta_once(cached_key, pending)
            cached = store.get_meta(cached_key)
            # A failed owner may have released the row between our INSERT
            # and SELECT. We still need a claim before dispatching.
            if not claimed and cached is None and not store.set_meta_once(cached_key, pending):
                return None
            if not claimed and cached is not None:
                try:
                    value = json.loads(cached)
                except ValueError:
                    value = None
                if isinstance(value, str):
                    return value or None
                if _pending_fresh(value):
                    return None
                if not store.compare_meta(cached_key, cached, pending):
                    return None
    try:
        output = _dispatch_codex(payload, config_dir, native, event, session_id, cwd, model)
    except Exception:
        if cacheable:
            with store_session() as store:
                store.delete_meta(cached_key, expected=pending)
        raise
    if cacheable:
        with store_session() as store:
            store.compare_meta(cached_key, pending, json.dumps(output or ""))
    return output


def _pending_fresh(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    monotonic = value.get("monotonic_at")
    stamp = monotonic if monotonic is not None else value.get("pending_at")
    if not isinstance(stamp, (int, float)):
        return False
    age = (time.monotonic() if monotonic is not None else time.time()) - stamp
    # Negative ages indicate a reboot or a legacy wall-clock step backwards.
    return 0 <= age < 180


def _dispatch_codex(
    payload: dict[str, Any],
    config_dir: Path,
    native: str,
    event: str,
    session_id: str,
    cwd: Path | None,
    model: str | None,
) -> str | None:
    common = {"agent": "codex", "native_session_id": native, "account": str(config_dir)}
    output: str | None = None
    if event == "SessionStart":
        output = hooks.session_start_context(
            cwd,
            session_id=session_id,
            source=_text(payload, "source"),
            transcript_path=_text(payload, "transcript_path"),
            model=model,
            **common,
        )
    elif event == "UserPromptSubmit":
        prompt = _text(payload, "prompt")
        source = "codex"
        if prompt:
            with store_session() as store:
                expected = store.get_meta(f"continuation-prompt:{session_id}")
                if expected == hashlib.sha256(prompt.encode()).hexdigest():
                    source = "codex:continuation"
                    store.set_meta(f"continuation-prompt:{session_id}", "")
        output = hooks.prompt_submitted(
            prompt,
            cwd,
            session_id=session_id,
            model=model,
            source=source,
            transcript_path=_text(payload, "transcript_path"),
            **common,
        )
    elif event == "Stop":
        decision = hooks.turn_stopped(
            cwd, session_id=session_id, stop_hook_active=bool(payload.get("stop_hook_active"))
        )
        if decision:
            with store_session() as store:
                store.set_meta(
                    f"continuation-prompt:{session_id}",
                    hashlib.sha256(decision.reason.encode()).hexdigest(),
                )
            output = json.dumps(decision.as_hook_output())
    elif event == "SessionEnd":
        hooks.session_ended(cwd, session_id=session_id)
        metrics.close_turn(session_id)
    elif event == "Interrupt":
        with store_session() as store:
            if store.get_session(session_id):
                store.touch_session(session_id, state="waiting")
        metrics.close_turn(session_id)
    elif event == "PermissionRequest" or (
        event == "PreToolUse" and payload.get("tool_name") == "request_user_input"
    ):
        hooks.needs_attention(cwd, session_id=session_id, message="Codex needs user input")
    elif event == "PostToolUse":
        with store_session() as store:
            session = store.get_session(session_id)
            if session:
                store.touch_session(session_id, state="working")
                store.renew_leases(session_id, harness_lease())
    return output


def harness_lease() -> datetime:
    from datetime import UTC, datetime, timedelta

    from aisquare.core.orchestrator import lease_minutes

    return datetime.now(UTC) + timedelta(minutes=lease_minutes())
