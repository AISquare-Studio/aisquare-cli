"""A synthetic terminal agent shaped like Gemini CLI, registered only in tests.

Written from SCOPE #113 0.8.2 part C (Gemini CLI's docs, read only: nothing of it is
installed or measured) to hold the shapes neither Claude Code nor Codex has, which the
shared code once assumed: its launch variable ``GEMINI_CLI_HOME`` names the folder its
config dir ``.gemini`` is in (``AgentAccounts.config_dir``, S1), its hook timeouts are
milliseconds (``AgentAdapter.hook_entry``, S2), and every hook reply is JSON. Its
installers, what it keeps of aisquare's (MCP servers in ``settings.json``, the aisquare
extension) and its account slots are its own answers. ``id`` is not ``gemini-cli``: a real
adapter for it takes that name, and this one is retired then.

tests/test_adapter_contract.py holds it to the protocol beside the fake;
tests/test_third_agent_seams.py runs it through hooks, uninstall and Welcome. No other
file names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from aisquare.core import agents, harness
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters.lasting import fetches_aisquare, runs_aisquare
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
    ForkArgs,
    HookSpec,
    HooksSwitch,
    InstallRoute,
    LastingInstall,
    LaunchPart,
    NativeChoice,
    PermissionArgs,
    PermissionIntent,
    SandboxWrites,
    SkillDirs,
    TelemetryReader,
    WriteScope,
    config_home,
    option_values,
    package_version,
    payload_cwd,
    payload_text,
    stepped_down,
)
from aisquare.core.claude_accounts import FailedTurn, LimitNotice
from aisquare.core.claude_sharing import SharingError, TrustScope
from aisquare.core.shell_line import command_line
from aisquare.models import ClaudeAccount, ClaudeIdentity, ClaudeUsage, UsageWindow

AGENT = "gemini-shaped"
HOME_ENV = "GEMINI_CLI_HOME"
CONFIG = ".gemini"
#: Its hook events (docs/hooks/reference.md), each the kind it decodes to.
EVENTS: dict[str, EventKind] = {
    "SessionStart": "session_start",
    "BeforeAgent": "prompt",
    "AfterAgent": "stop",
    "SessionEnd": "session_end",
    "Notification": "attention",
    "AfterTool": "tool_done",
}
#: The aisquare extension, as ``gemini extensions install`` names it.
EXTENSION = "aisquare"
#: Where its sessions record the day's quotas, per model, in its config dir.
QUOTA = "quota.json"
DAY_MINUTES = 24 * 60
#: ``-r``/``--resume``, ``-m``/``--model`` and the rest that take a value (cli-reference.md).
VALUE_OPTIONS = frozenset(
    {
        "--model",
        "-m",
        "--approval-mode",
        "--resume",
        "-r",
        "-e",
        "--extensions",
        "--include-directories",
        "--allowed-mcp-server-names",
        "-o",
        "--output-format",
        "-p",
        "--prompt",
    }
)


def _milliseconds(seconds: int) -> int:
    return seconds * 1000


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


class GeminiShapedTranscript:
    """Its session log, one record a line: ``{"type": "user"|"gemini"|"info"|"error",
    "content", "toolCalls": [{"id", "name", "args"}], "timestamp"}``, and a call's result in a
    later ``{"$patch": {"toolCalls": [{"id", "status", "result"}]}}`` line (:meth:`records`
    folds it into the call). Its permission dialog takes ``1`` for Allow once and Esc for
    No; a question's options are picked by their numbers."""

    speaker = "gemini"

    def records(
        self, lines: Iterable[tuple[int, bytes]]
    ) -> Iterator[tuple[int, dict[str, Any] | None]]:
        """Newest first, as read: a ``$patch`` line, newer than the record whose calls it
        answers, is held until that record is reached and folded into its calls by id."""
        patches: dict[str, dict[str, Any]] = {}
        for offset, raw in lines:
            record = parse_line(raw)
            patch = record.get("$patch") if record is not None else None
            if isinstance(patch, dict):
                for call in _calls(patch):
                    patches.setdefault(str(call.get("id")), {}).update(call)
                yield offset, None
                continue
            if record is not None and _calls(record):
                record = {
                    **record,
                    "toolCalls": [
                        {**call, **patches.pop(str(call.get("id")), {})} for call in _calls(record)
                    ],
                }
            yield offset, record

    def tail_facts(self, file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
        pending: list[PendingTool] = []
        newest, newest_at, key = "none", None, None
        text: str | None = None
        for offset, record in self.records(lines_backwards(file, size, budget=budget)):
            if record is None:
                continue
            kind, at = record.get("type"), record_time(record)
            if kind == "info" and newest == "none":
                newest, newest_at, key = "interrupted", at, str(offset)
                break
            said = {"user": "user_prompt", "gemini": "assistant_text"}.get(str(kind))
            if said is None:
                continue
            calls = _calls(record)
            if kind == "gemini" and calls:
                said = "assistant_tool"
            if newest == "none":
                newest, newest_at, key = said, at, str(offset)
            if kind == "gemini" and record.get("content") and text is None:
                text = str(record["content"])
            pending += [_pending(call, at) for call in calls if not _answered(call)]
            if kind == "user":
                break
        return TranscriptTail(
            pending=tuple(pending),
            newest=newest,
            newest_at=newest_at,
            last_text=text,
            last_text_at=newest_at if text is not None else None,
            marker_key=key,
        )

    def page_lines(self, record: dict[str, Any], width: int) -> list[str]:
        kind = record.get("type")
        if kind == "user":
            return [YOU, *wrap(str(record.get("content", "")), width), ""]
        if kind != "gemini":
            return []  # an info line, an error: no conversation
        lines = [speaker_line(self.speaker)]
        if record.get("content"):
            lines += wrap(str(record["content"]), width)
        for call in _calls(record):
            asked = _pending(call, None)
            if asked.name == "ask_user":
                lines += question_lines(asked.input.get("questions"), width)
            else:
                status = call.get("status")  # its result, folded in from a later line
                shown = f"{asked.name} · {status}" if isinstance(status, str) else asked.name
                lines.append(dim_note(shown, width))
        return [*lines, ""]

    def call_asks(self, tool: PendingTool) -> CallAsk:
        return "question" if tool.name == "ask_user" else "tool"

    def call_mark(self, tool: PendingTool) -> str | None:
        command = tool.input.get("command")
        return command if isinstance(command, str) else None

    def answer_keys(self, kind: AnswerKind, options: int = 0) -> tuple[AnswerKey, ...]:
        if kind == "permission":
            return (AnswerKey("Allow once", ("1",)), AnswerKey("No", ("Escape",)))
        if kind == "question":
            picks = tuple(AnswerKey(str(n), (str(n),)) for n in range(1, min(options, 9) + 1))
            return (*picks, AnswerKey("Cancel", ("Escape",)))
        return ()

    def reply_since(self, file: Path, since: datetime | None) -> str | None:
        said: list[str] = []
        answered = False
        try:
            records = list(lines_backwards(file, file.stat().st_size, budget=1 << 20))
        except OSError:
            return None
        for _, raw in records:
            record = parse_line(raw) or {}
            kind = record.get("type")
            if kind == "user":
                at = record_time(record)
                if since is not None and at is not None and at < since:
                    return None
                return "\n".join(said).strip() if answered else None
            if kind == "gemini":
                answered = True
                if record.get("content"):
                    said.insert(0, str(record["content"]))
        return None


def _answered(call: Mapping[str, Any]) -> bool:
    """A call its result has reached (a ``$patch`` folded in): it waits on no one."""
    return call.get("status") in ("success", "error", "cancelled") or "result" in call


def _calls(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    calls = record.get("toolCalls")
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _pending(call: Mapping[str, Any], at: datetime | None) -> PendingTool:
    """A tool call as a pending one; ``ask_user``'s question and options as the questions the
    transcript slot hands a card."""
    name = str(call.get("name", "tool"))
    args = call.get("args")
    if name == "ask_user" and isinstance(args, dict):
        listed = args.get("options")
        options = [
            {"label": str(option), "description": ""}
            for option in (listed if isinstance(listed, list) else [])
        ]
        args = {
            "questions": [
                {
                    "header": "",
                    "question": str(args.get("question", "")),
                    "multiSelect": False,
                    "options": options,
                }
            ]
        }
    kept, dropped = kept_input(args)
    return PendingTool(str(call.get("id")), name, name, kept, at, input_dropped=dropped)


class GeminiShapedTelemetry:
    """Its OpenTelemetry (docs/cli/telemetry.md): an exporter its ``settings.json`` names
    (``telemetry.otlpEndpoint``) is the operator's, and of each log record the receiver keeps
    its ``gen_ai.*`` model and token attributes and its ``gemini_cli.*`` event's name, the
    tool and the timing. It has no setting of its tools' shells' environment."""

    tool_shell_policy = "settings of its tools' environment"
    FIELDS = frozenset(
        {
            "event.name",
            "session.id",
            "gen_ai.request.model",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "function_name",
            "duration_ms",
        }
    )

    def operator_configured(self, home: Path, args: Sequence[str], cwd: Path) -> bool:
        telemetry = agents.read_json(home / "settings.json").get("telemetry")
        return isinstance(telemetry, dict) and bool(telemetry.get("otlpEndpoint"))

    def tool_shell_excludes(self, home: Path, args: Sequence[str], cwd: Path) -> list[str] | None:
        return []

    def decode(
        self, attributes: Mapping[str, object], event: str | None
    ) -> dict[str, object] | None:
        kept = {key: value for key, value in attributes.items() if key in self.FIELDS}
        kept["event.name"] = kept.get("event.name") or event or "gemini_cli.event"
        return kept


class GeminiShapedTrust:
    """``trustedFolders.json`` in the config dir: ``{path: "TRUST_FOLDER" | "TRUST_PARENT" |
    "DO_NOT_TRUST"}`` (docs/cli/trusted-folders.md). A ``TRUST_PARENT`` entry covers the folders
    under the one it names; ``file_name`` is the file's place in the home."""

    file_name = f"{CONFIG}/trustedFolders.json"

    def scope(self, base: TrustScope) -> TrustScope:
        folder = str(base.folder)
        return TrustScope(base.folder, folder, (folder, str(base.folder.parent)))

    def _load(self, account: ClaudeAccount) -> dict[str, str]:
        try:
            data = json.loads((account.config_dir / self.file_name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise SharingError(f"its {self.file_name} cannot be read ({exc})") from exc
        return {str(key): str(value) for key, value in data.items()}

    def read(self, account: ClaudeAccount) -> dict[str, bool]:
        return {key: value != "DO_NOT_TRUST" for key, value in self._load(account).items()}

    def grant(self, account: ClaudeAccount, *keys: str) -> list[str]:
        data = self._load(account)
        written = [key for key in dict.fromkeys(keys) if data.get(key) != "TRUST_FOLDER"]
        if written:
            data.update(dict.fromkeys(written, "TRUST_FOLDER"))
            path = account.config_dir / self.file_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        return written


class GeminiShapedAccounts:
    """Its numbered slots: ``GEMINI_CLI_HOME`` names each slot, and the agent keeps its config
    dir, its login (``oauth_creds.json``) and its account (``google_accounts.json``) in the
    slot's ``.gemini``. There is no login subcommand: the first run of the TUI signs in."""

    agent = AGENT
    home_env = HOME_ENV
    extra_env: tuple[str, ...] = ()
    plain_vars: Mapping[str, str] = {HOME_ENV: "AISQUARE_PLAIN_GEMINI_CLI_HOME"}
    dir_name = "gemini-shaped-accounts"
    plain_slot = -2
    plain_label = "plain gemini"
    install_command = "npm install -g @google/gemini-cli"
    network_usage = False
    legacy_slots = False
    noun = "Gemini-shaped"
    relogin = "/auth"
    sign_out = "inside Gemini (/auth)"
    shares_settings = False
    trust = GeminiShapedTrust()

    def sign_in_args(self) -> tuple[str, ...]:
        return ()  # no login subcommand: its TUI signs in on its first run in the slot

    def plain_home(self, home: Path, plain: Mapping[str, str]) -> Path:
        value = plain.get(HOME_ENV, "").strip()
        return Path(value).expanduser() if value else home

    def launched_account(
        self, env: Mapping[str, str], accounts: Sequence[ClaudeAccount]
    ) -> ClaudeAccount | None:
        return accounts_core.account_in_home(self.agent, env, accounts)

    def config_dir(self, slot_dir: Path) -> Path:
        return slot_dir / CONFIG

    def _read(self, account: ClaudeAccount, name: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.config_dir(account.config_dir) / name).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def identity(
        self, account: ClaudeAccount, *, env: Mapping[str, str] | None = None
    ) -> ClaudeIdentity | None:
        active = (self._read(account, "google_accounts.json") or {}).get("active")
        if not self.signed_in(account) or not isinstance(active, str) or not active:
            return None
        return ClaudeIdentity(email=active)

    def signed_in(self, account: ClaudeAccount, *, fresh: bool = False) -> bool:
        return bool((self._read(account, "oauth_creds.json") or {}).get("refresh_token"))

    def subscription(self, account: ClaudeAccount) -> str | None:
        return None

    def token_state(self, account: ClaudeAccount) -> str:
        return "ok" if self.signed_in(account) else "missing"

    def usage(self, account: ClaudeAccount, *, now: datetime | None, fetch: object) -> ClaudeUsage:
        """Its quotas are per day per model (docs/resources/quota-and-pricing.md): one window
        of a day for each model, from ``quota.json`` in its config dir (``{"models": {model:
        {"used_percent", "resets_at"}}, "recorded_at"}``, a stand-in for the real CLI, which
        documents no local quota file). Read offline, as Codex's are."""
        data = self._read(account, QUOTA) or {}
        models = data.get("models")
        if not isinstance(models, dict) or not models:
            return ClaudeUsage(available=False, reason="no session has recorded its quotas yet")
        windows = tuple(
            UsageWindow(
                name=f"{model} daily",
                minutes=DAY_MINUTES,
                used_percent=float(quota["used_percent"]),
                resets_at=_when(quota.get("resets_at")),
            )
            for model, quota in models.items()
            if isinstance(quota, dict) and isinstance(quota.get("used_percent"), int | float)
        )
        return ClaudeUsage(
            available=bool(windows), windows=windows, fetched_at=_when(data.get("recorded_at"))
        )

    def limit_from_transcript(
        self, path: Path, *, now: datetime | None = None
    ) -> LimitNotice | None:
        return None

    def failed_turn(self, path: Path) -> FailedTurn | None:
        return None

    def login_fingerprint(self, account: ClaudeAccount) -> str | None:
        creds = self._read(account, "oauth_creds.json")
        token = (creds or {}).get("refresh_token")
        return hashlib.sha256(str(token).encode()).hexdigest() if token else None


class GeminiShapedAdapter:
    id = AGENT
    label = "Gemini-shaped"
    binary = "gemini"
    home_env = HOME_ENV
    home_name = CONFIG
    settings_name = "settings.json"
    install_hint = "npm install -g @google/gemini-cli"
    install_routes: tuple[InstallRoute, ...] = (
        InstallRoute("npm", "npm install -g @google/gemini-cli"),
        InstallRoute("Homebrew", "brew install gemini-cli", frozenset({"darwin"})),
    )
    version_args: tuple[str, ...] = ("--version",)
    hooks_off_setting = '"hooksConfig": {"enabled": false}'
    exit_command = "/quit"
    dismiss_key = "Escape"
    key_spellings: Mapping[str, str] = {}
    accounts = GeminiShapedAccounts()
    plugin = None
    telemetry_reader: TelemetryReader | None = GeminiShapedTelemetry()
    transcript_reader: AgentTranscript | None = GeminiShapedTranscript()
    capabilities = AgentCapabilities(
        launch_inputs=(CONFIG, ".agents"),
        hooks=tuple(
            HookSpec(event, GENERIC_HOOK, 60 if kind in {"session_start", "prompt"} else 5)
            for event, kind in EVENTS.items()
        ),
        hook_names_home=True,
        limits_in_transcript=True,
        value_options=VALUE_OPTIONS,
        switch_options=frozenset({"-s", "--sandbox", "-y", "--yolo", "-d", "--debug"}),
    )

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        """An explicit dir, else ``$GEMINI_CLI_HOME/.gemini``, else ``~/.gemini``
        (``config_home``, through its accounts' ``config_dir``)."""
        return config_home(self, home, env, explicit)

    def layout_version(self, binary: str) -> str | None:
        return package_version(binary)

    def hooks_switch(
        self, home: Path, settings_path: Path, args: Sequence[str] = ()
    ) -> HooksSwitch | None:
        config = agents.read_json(settings_path).get("hooksConfig")
        if isinstance(config, dict) and config.get("enabled") is False:
            return HooksSwitch(self.hooks_off_setting, settings_path)
        return None

    def hook_entry(self, spec: HookSpec, command: str) -> dict[str, Any]:
        """A named handler, its timeout in milliseconds (docs/hooks/reference.md: default
        60000)."""
        entry: dict[str, Any] = {"name": "aisquare", "type": "command", "command": command}
        if spec.timeout is not None:
            entry["timeout"] = _milliseconds(spec.timeout)
        return entry

    def lasting_installs(self, home: Path) -> tuple[LastingInstall, ...]:
        """The MCP servers that run aisquare in ``home``'s ``settings.json`` (``mcpServers``),
        and the aisquare extension installed in ``home/extensions/aisquare`` and not disabled
        in ``extensions/extension-enablement.json``, each with the ``gemini`` command that
        removes it, aimed at the home through ``GEMINI_CLI_HOME`` (the folder ``.gemini`` is
        in)."""
        settings = agents.dir_identity(home / self.settings_name)
        servers = agents.read_json(settings).get("mcpServers")
        found = [
            LastingInstall(
                self.id,
                "mcp",
                str(name),
                settings,
                home,
                _aimed(["gemini", "mcp", "remove", str(name)], home),
                fetches=fetches_aisquare(spec),
            )
            for name, spec in (servers.items() if isinstance(servers, dict) else ())
            if runs_aisquare(spec)
        ]
        manifest = home / "extensions" / EXTENSION / "gemini-extension.json"
        enablement = agents.read_json(home / "extensions" / "extension-enablement.json")
        disabled = enablement.get(EXTENSION, {}) == {"overrides": ["!*"]}
        if manifest.is_file() and not disabled:
            found.append(
                LastingInstall(
                    self.id,
                    "plugin",
                    EXTENSION,
                    agents.dir_identity(manifest),
                    home,
                    _aimed(["gemini", "extensions", "uninstall", EXTENSION], home),
                    fetches=True,
                    version=str(agents.read_json(manifest).get("version") or "") or None,
                )
            )
        return tuple(found)

    def decode_event(self, payload: Mapping[str, Any]) -> AgentEvent | None:
        event = payload_text(payload, "hook_event_name") or ""
        kind = EVENTS.get(event)
        if kind is None:
            return None
        notice = payload_text(payload, "notification_type")
        if kind == "attention" and notice not in (None, "ToolPermission"):
            kind = "notification"
        prompt = payload.get("prompt")
        return AgentEvent(
            kind,
            payload_text(payload, "session_id"),
            cwd=payload_cwd(payload),
            prompt=prompt if isinstance(prompt, str) else None,
            transcript_path=payload_text(payload, "transcript_path"),
            source=payload_text(payload, "source"),
            reason=payload_text(payload, "reason"),
            stop_hook_active=payload.get("stop_hook_active") is True,
            message=payload_text(payload, "message"),
            notification_type=notice,
        )

    def encode_reply(self, kind: EventKind, reply: str | None) -> str:
        """JSON only: context as ``hookSpecificOutput.additionalContext``, a stop that keeps
        the turn going as ``{"decision": "block", "reason": …}``."""
        if not reply:
            return ""
        if kind == "stop":
            return json.dumps({"decision": "block", "reason": reply})
        event = "SessionStart" if kind == "session_start" else "BeforeAgent"
        return json.dumps(
            {"hookSpecificOutput": {"hookEventName": event, "additionalContext": reply}}
        )

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
    ) -> LaunchPart:
        """A server only from a settings file (docs/tools/mcp-server.md: no flag adds one for
        a session): ``mcpServers`` in its launch's settings layer, each variable it inherits
        named as ``$NAME``, which its settings expand."""
        env = {name: f"${name}" for name in env_vars or []}
        server = {"command": executable, "args": args, "env": env}
        return LaunchPart(settings={"mcpServers": {"aisquare": server}})

    def tools_only_args(
        self,
        server: str,
        command: str,
        args: list[str],
        env: dict[str, str],
        *,
        inherit: Sequence[str],
        home: Path,
        cwd: Path,
        launch_args: Sequence[str],
    ) -> list[str] | None:
        return None

    def native_args(self, args: list[str]) -> list[str]:
        return list(args)

    def native_choice(self, args: Sequence[str]) -> NativeChoice:
        models = option_values(args, "--model", "-m")
        return NativeChoice(models[-1] if models else None)

    def validate(self, model: str | None, effort: str | None) -> None:
        if effort is not None:
            raise BadEffortError("Gemini-shaped takes no effort: thinking is a settings override")

    def effort_alias(self, effort: str) -> str:
        return effort.strip().lower()

    def context_files(self, home: Path) -> tuple[Path, ...]:
        return (home / "GEMINI.md",)

    def browser_tools(self, home: Path) -> list[str] | None:
        return []

    def role_defaults(self, role: str, *, binary: str, args: Sequence[str]) -> harness.RoleDefaults:
        return harness.RoleDefaults()

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        self.validate(model, effort)
        return ["-m", model] if model else []

    def permission_args(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> PermissionArgs:
        """Its approval mode for the intent stepped down where it has no reviewer
        (:func:`stepped_down`), with the note that says so."""
        asked, notes = stepped_down(self.capabilities, self.label, intent)
        return PermissionArgs(tuple(self._permission_flags(asked, role, native)), notes)

    def _permission_flags(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> list[str]:
        """``--approval-mode``: ``plan`` read-only, ``yolo`` never asking, ``default`` asking.
        It has no reviewer of its own (``auto_edit`` approves every edit, unreviewed), so
        ``auto`` never reaches here: :meth:`permission_args` runs it as ``ask``."""
        if option_values(native, "--approval-mode") or "--yolo" in native or "-y" in native:
            return []
        if intent.write == "read_only":
            mode = "plan"
        else:
            mode = {"ask": "default", "auto": "default", "never": "yolo"}[intent.approvals]
        return ["--approval-mode", mode]

    def store_access(self, write: WriteScope, store: Path) -> list[str]:
        return []  # its tools run unsandboxed unless the operator chose its --sandbox

    def sandbox_writes(
        self, args: Sequence[str], cwd: Path, home: Path | None = None
    ) -> SandboxWrites | None:
        return None

    def broker_access(
        self,
        args: Sequence[str],
        store: Path,
        inbox: Path,
        *,
        protect: Sequence[Path] = (),
        writable: Sequence[Path] = (),
    ) -> list[str] | None:
        return None

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
            raise ValueError("Gemini-shaped takes a permission intent, not a sandbox")
        intent = (
            PermissionIntent.parse(permission_mode)
            if permission_mode
            else PermissionIntent.for_role(role)
        )
        return list(self.permission_args(intent, role, native).args)

    def label_args(self, label: str) -> LaunchPart:
        return LaunchPart(env={"CLI_TITLE": label})  # its title is a variable, not a flag

    def resume_args(self, native_session_id: str, transcript_path: Path | None) -> list[str]:
        return ["--resume", native_session_id]

    def fork_args(self, native_session_id: str, transcript_path: Path | None) -> ForkArgs | None:
        return None

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        return None

    def disable_native_teams(self) -> LaunchPart:
        """Its sub-agents are a setting alone (docs/core/subagents.md), no flag or variable."""
        return LaunchPart(settings={"experimental": {"enableAgents": False}})

    def fleet_settings(self, home: Path, args: Sequence[str]) -> LaunchPart:
        """Its update notice off (``general.enableAutoUpdateNotification``), unless the
        operator's settings say it themselves."""
        general = agents.read_json(home / self.settings_name).get("general")
        if isinstance(general, dict) and "enableAutoUpdateNotification" in general:
            return LaunchPart()
        return LaunchPart(settings={"general": {"enableAutoUpdateNotification": False}})

    def settings_layer(self, settings: Mapping[str, Any], file: Path) -> LaunchPart | None:
        """A settings file of the launch's own, which ``GEMINI_CLI_SYSTEM_DEFAULTS_PATH`` names
        (docs/reference/configuration.md): the system-defaults layer, under every operator's."""
        file.write_text(json.dumps(settings), encoding="utf-8")
        return LaunchPart(env={"GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(file)})

    def unattended_env(self) -> Mapping[str, str]:
        return {}

    def telemetry_args(self, endpoint: str, *, keep_out: Sequence[str] = ()) -> LaunchPart:
        """Its exporter by its variables alone (docs/cli/telemetry.md), prompts left out."""
        return LaunchPart(
            env={
                "GEMINI_TELEMETRY_ENABLED": "true",
                "GEMINI_TELEMETRY_TARGET": "local",
                "GEMINI_TELEMETRY_OTLP_ENDPOINT": endpoint,
                "GEMINI_TELEMETRY_OTLP_PROTOCOL": "http",
                "GEMINI_TELEMETRY_LOG_PROMPTS": "false",
            }
        )

    def system_prompt_args(
        self, block: Path, text: str, args: Sequence[str], home: Path, *, cwd: Path
    ) -> list[str] | None:
        return None  # GEMINI_SYSTEM_MD replaces its prompt: no seam beside it

    def skill_dirs(self, home: Path, repo: Path | None) -> SkillDirs:
        project = (repo / CONFIG / "skills", repo / ".agents" / "skills") if repo else ()
        return SkillDirs((home / "skills",), project)

    def headless_call(
        self, schema: Path, out: Path, model: str | None, *, home: Path, cwd: Path
    ) -> list[str] | None:
        return None  # no schema for its -p answer: the engine ladder goes on

    def headless_answer(self, stdout: str, out: Path) -> object | None:
        return None


def _aimed(argv: list[str], home: Path) -> str:
    """``argv`` aimed at config dir ``home``: ``GEMINI_CLI_HOME`` names the folder it is in."""
    parent = home.parent if home.name == CONFIG else home
    return f"{HOME_ENV}={command_line([str(parent)])} {command_line(argv)}"
