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
from datetime import datetime
from pathlib import Path
from typing import Any

from aisquare.core import harness
from aisquare.core.agent_adapters.transcripts import (
    YOU,
    PendingTool,
    TranscriptTail,
    dim_note,
    kept_input,
    lines_backwards,
    parse_line,
    question_lines,
    record_time,
    speaker_line,
    wrap,
)
from aisquare.core.agent_adapters.types import (
    GENERIC_HOOK,
    AgentCapabilities,
    AgentEvent,
    AgentTranscript,
    AnswerKey,
    AnswerKind,
    BadEffortError,
    CallAsk,
    EventKind,
    HookSpec,
    HooksSwitch,
    NativeChoice,
    PermissionIntent,
    SandboxWrites,
    WriteScope,
    config_home,
    option_occurrences,
    option_values,
    payload_text,
    store_dir,
    store_read_only,
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


class FakeTranscript:
    """Its log: one record a line, ``{"timestamp", "kind", …}`` — ``heard`` (the human's
    ``text``), ``said`` (its ``text``), ``run`` (a ``tool`` with ``args`` under an ``id``),
    ``pick`` (a question: ``title`` and ``choices``, under an ``id``), ``ran`` (the result of
    ``id``), ``halt`` (stopped by the human) and ``over`` (the turn's end). Its dialogs take
    ``y``/``n``, and a choice is picked by moving down to it and confirming."""

    speaker = "fake"

    def tail_facts(self, file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
        answered: set[str] = set()
        pending: list[PendingTool] = []
        newest, newest_at, key = "none", None, None
        text: str | None = None
        over = False
        for offset, raw in lines_backwards(file, size, budget=budget):
            record = parse_line(raw)
            if record is None:
                continue
            kind, at = record.get("kind"), record_time(record)
            said = {
                "heard": "user_prompt",
                "said": "assistant_text",
                "run": "assistant_tool",
                "pick": "assistant_tool",
                "ran": "tool_result",
                "halt": "interrupted",
            }.get(str(kind))
            if kind == "over" and newest == "none":
                over = True
                continue
            if said is None:
                continue
            if newest == "none":
                newest, newest_at, key = said, at, str(offset)
            if kind == "said" and text is None:
                text = str(record.get("text", ""))
            if kind == "halt":
                over = True
            if kind == "ran":
                answered.add(str(record.get("id")))
            if kind in ("run", "pick") and str(record.get("id")) not in answered and not over:
                pending.append(_fake_call(record, at))
            if kind == "heard":
                break
        return TranscriptTail(
            pending=tuple(reversed(pending)),
            newest=newest,
            newest_at=newest_at,
            last_text=text,
            last_text_at=newest_at if text is not None else None,
            marker_key=key,
        )

    def page_lines(self, record: dict[str, Any], width: int) -> list[str]:
        kind = record.get("kind")
        if kind == "heard":
            return [YOU, *wrap(str(record.get("text", "")), width), ""]
        if kind == "said":
            return [speaker_line(self.speaker), *wrap(str(record.get("text", "")), width), ""]
        if kind == "pick":
            asked = question_lines(_fake_call(record, None).input.get("questions"), width)
            return [speaker_line(self.speaker), *asked, ""]
        if kind == "run":
            return [speaker_line(self.speaker), dim_note(str(record.get("tool")), width), ""]
        return []

    def call_asks(self, tool: PendingTool) -> CallAsk:
        return "question" if tool.name == "pick" else "tool"

    def call_mark(self, tool: PendingTool) -> str | None:
        line = tool.input.get("line")
        return line if isinstance(line, str) else None

    def answer_keys(self, kind: AnswerKind, options: int = 0) -> tuple[AnswerKey, ...]:
        if kind == "permission":
            return (AnswerKey("allow", ("y",)), AnswerKey("deny", ("n",)))
        if kind == "question":
            picks = tuple(
                AnswerKey(str(n), (*("Down",) * (n - 1), "Enter")) for n in range(1, options + 1)
            )
            return (*picks, AnswerKey("Back", ("Escape",)))
        return ()


def _fake_call(record: Mapping[str, Any], at: datetime | None) -> PendingTool:
    """A ``run`` or a ``pick`` as a pending call; a pick's choices as the questions the
    transcript slot hands a card (``AgentTranscript.call_asks``)."""
    if record.get("kind") == "pick":
        choices = record.get("choices")
        options = [
            {"label": str(choice), "description": ""}
            for choice in (choices if isinstance(choices, list) else [])
        ]
        title = str(record.get("title", ""))
        arguments: object = {
            "questions": [
                {"header": "", "question": title, "multiSelect": False, "options": options}
            ]
        }
        name = "pick"
    else:
        arguments = record.get("args")
        name = str(record.get("tool", "tool"))
    kept, dropped = kept_input(arguments)
    return PendingTool(str(record.get("id")), name, name, kept, at, input_dropped=dropped)


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
    key_spellings: Mapping[str, str] = {"M-Enter": "C-o"}
    accounts = None
    plugin = None
    transcript_reader: AgentTranscript | None = FakeTranscript()
    capabilities = AgentCapabilities(
        launch_inputs=(".fakeagent",),
        hooks=tuple(
            HookSpec(event, GENERIC_HOOK, 30 if kind in {"session_start", "prompt"} else None)
            for event, kind in EVENTS.items()
        ),
        hook_names_home=True,
        strict_hooks_file=True,
        sandboxes_tools=True,
        value_options=frozenset(
            {"--model", "--effort", "--title", "--write", "--approvals", "--writable", "--readonly"}
        ),
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

    def browser_tools(self, home: Path) -> list[str] | None:
        return []

    def role_defaults(self, role: str, *, binary: str, args: Sequence[str]) -> harness.RoleDefaults:
        return harness.RoleDefaults()

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        self.validate(model, effort)
        return (["--model", model] if model else []) + (["--effort", effort] if effort else [])

    def permission_args(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> list[str]:
        if "--write" in native or "--approvals" in native:
            return []
        return [
            "--write",
            intent.write,
            "--approvals",
            intent.approvals,
            *self.store_access(intent.write, store_dir()),
        ]

    def store_access(self, write: WriteScope, store: Path) -> list[str]:
        # Its sandbox takes read-only directories, and writable ones within them; with
        # `full` it has none.
        if write == "full":
            return []
        return [arg for path in store_read_only(store) for arg in ("--readonly", str(path))]

    def sandbox_writes(
        self, args: Sequence[str], cwd: Path, home: Path | None = None
    ) -> SandboxWrites | None:
        # Its sandbox writes the workspace and /tmp under `--write workspace`, nothing but
        # its `--writable` paths under `--write read_only`, the `--readonly` ones read-only
        # within; with no `--readonly` (or `--write full`), there is no sandbox at all.
        if "--readonly" not in args or "full" in option_values(args, "--write"):
            return None
        read = tuple(Path(path) for path in option_values(args, "--readonly"))
        written = tuple(Path(path) for path in option_values(args, "--writable"))
        if "read_only" in option_values(args, "--write"):
            return SandboxWrites(written, read)
        return SandboxWrites((cwd, Path("/tmp"), *written), read, cwd, (Path("/tmp"),))

    def broker_access(
        self,
        args: Sequence[str],
        store: Path,
        inbox: Path,
        *,
        protect: Sequence[Path] = (),
        writable: Sequence[Path] = (),
    ) -> list[str] | None:
        # The store's run read-only whatever `args` held, the inbox writable beside it.
        result: list[str] = []
        found = False
        tokens = iter(args)
        for arg in tokens:
            if arg in ("--readonly", "--writable"):
                path = next(tokens, "")
                if path == str(store):
                    result += ["--readonly", path, "--writable", str(inbox)]
                    result += [arg for kept in protect for arg in ("--readonly", str(kept))]
                    result += [arg for kept in writable for arg in ("--writable", str(kept))]
                    found = True
                else:
                    result += [arg, path]
                continue
            result.append(arg)
        return result if found else None

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

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        return ["inbox", native_session_id, message]

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return ["--no-subagents"], {"FAKE_AGENT_TEAMS": "off"}

    def telemetry_args(self, endpoint: str, *, keep_out: Sequence[str] = ()) -> list[str]:
        return []
