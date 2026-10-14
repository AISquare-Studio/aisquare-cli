"""A synthetic third terminal agent, registered only in tests (tests/test_adapter_contract.py).

Written by following docs/coding-agents.md, "Adding a terminal agent", and nothing else:
its native vocabulary is deliberately unlike Claude Code's and Codex's (its own event
names, payload keys, reply envelope and flags), so a shared service that special-cases
either of them, or reads a payload or writes a flag itself, fails the conformance suite
for this one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aisquare.core import harness
from aisquare.core.agent_adapters.types import (
    GENERIC_HOOK,
    AgentCapabilities,
    AgentEvent,
    BadEffortError,
    EventKind,
    HookSpec,
    HooksSwitch,
    NativeChoice,
    PermissionIntent,
    config_home,
    option_occurrences,
    option_values,
    payload_text,
)

#: Its native event names, each the kind it decodes to.
EVENTS: dict[str, EventKind] = {
    "session.open": "session_start",
    "prompt.sent": "prompt",
    "turn.done": "stop",
    "session.close": "session_end",
    "turn.cancel": "interrupt",
    "approval.wanted": "attention",
    "tool.result": "tool_done",
    "turn.error": "turn_failed",
    "notice": "notification",
}


class FakeAgentAdapter:
    id = "fake-agent"
    label = "Fake Agent"
    binary = "fakeagent"
    home_env = "FAKE_AGENT_HOME"
    home_name = ".fakeagent"
    settings_name = "hooks.json"
    install_hint = "pipx install fake-agent"
    version_args: tuple[str, ...] = ("version",)
    hooks_off_setting = '"hooks_off": true'
    exit_command = "/quit"
    accounts = None
    plugin = None
    capabilities = AgentCapabilities(
        hooks=tuple(
            HookSpec(event, GENERIC_HOOK, 30 if kind in {"session_start", "prompt"} else None)
            for event, kind in EVENTS.items()
        ),
        hook_names_home=True,
        strict_hooks_file=True,
        value_options=frozenset({"--model", "--effort", "--title", "--write", "--approvals"}),
        switch_options=frozenset({"--no-subagents"}),
    )

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        return config_home(self, home, env, explicit)

    def hooks_switch(
        self, home: Path, settings_path: Path, args: Sequence[str] = ()
    ) -> HooksSwitch | None:
        if "--no-hooks" in args:
            return HooksSwitch("`--no-hooks`")
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(settings, dict) and settings.get("hooks_off") is True:
            return HooksSwitch(self.hooks_off_setting, settings_path)
        return None

    def decode_event(self, payload: Mapping[str, Any]) -> AgentEvent | None:
        kind = EVENTS.get(payload_text(payload, "hook_event_name") or "")
        thread = payload_text(payload, "thread")
        if kind is None or thread is None:
            return None
        workdir = payload_text(payload, "workdir")
        return AgentEvent(
            kind,
            thread,
            cwd=Path(workdir) if workdir else None,
            prompt=payload_text(payload, "text"),
            transcript_path=payload_text(payload, "log"),
            model=payload_text(payload, "model_id"),
            effort=payload_text(payload, "thinking"),
            source=payload_text(payload, "how"),
            reason=payload_text(payload, "why"),
            stop_hook_active=payload.get("continuing") is True,
            turn_id=payload_text(payload, "turn"),
            error=payload_text(payload, "error_code"),
            message=payload_text(payload, "message"),
            details=payload_text(payload, "detail"),
            notification_type=payload_text(payload, "notice_type"),
        )

    def encode_reply(self, kind: EventKind, reply: str | None) -> str:
        if not reply:
            return ""
        if kind == "stop":
            return json.dumps({"continue": True, "instructions": reply})
        return json.dumps({"context": reply})

    def resolve_model(
        self,
        role: str,
        *,
        binary: str,
        env: dict[str, str],
        probe: bool | None,
        refresh: bool,
        effort: str | None,
    ) -> harness.ModelResolution:
        from aisquare.core.agent_adapters.native_models import resolve_model

        return resolve_model(self.id, role, env=env, effort=effort)

    def mcp_args(
        self, executable: str, args: list[str], env_vars: list[str] | None = None
    ) -> list[str]:
        server = {"command": executable, "args": args, "env": env_vars or []}
        return ["--mcp", json.dumps({"aisquare": server})]

    def native_args(self, args: list[str]) -> list[str]:
        return list(args)

    def native_choice(self, args: Sequence[str]) -> NativeChoice:
        models = option_values(args, "--model")
        efforts = list(option_occurrences(args, "--effort"))
        return NativeChoice(
            models[-1] if models else None,
            efforts[-1].value if efforts else None,
            efforts[-1].index if efforts else None,
        )

    def validate(self, model: str | None, effort: str | None) -> None:
        if effort is not None and effort.strip().lower() not in {"low", "high"}:
            raise BadEffortError(f"Fake Agent thinks low or high, not {effort!r}")

    def effort_alias(self, effort: str) -> str:
        return effort.strip().lower()

    def context_files(self, home: Path) -> tuple[Path, ...]:
        return (home / "FAKE.md",)

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        self.validate(model, effort)
        return (["--model", model] if model else []) + (["--effort", effort] if effort else [])

    def permission_args(self, intent: PermissionIntent, role: str) -> list[str]:
        return ["--write", intent.write, "--approvals", intent.approvals]

    def fleet_args(
        self,
        role: str,
        label: str,
        permission_mode: str | None,
        *,
        sandbox: str | None = None,
        approval: str | None = None,
        approve_for_me: bool | None = None,
        native: Sequence[str] = (),
    ) -> list[str]:
        if sandbox or approval or approve_for_me:
            raise ValueError("Fake Agent takes a permission intent, not a sandbox")
        intent = (
            PermissionIntent.parse(permission_mode)
            if permission_mode
            else PermissionIntent.for_role(role)
        )
        return [*self.permission_args(intent, role), *self.label_args(label)]

    def label_args(self, label: str) -> list[str]:
        return ["--title", label]

    def resume_args(self, native_session_id: str, transcript_path: Path | None) -> list[str]:
        return ["--thread", native_session_id]

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return ["--no-subagents"], {"FAKE_AGENT_TEAMS": "off"}

    def telemetry_args(self, endpoint: str, authorization: str) -> list[str]:
        return []
