"""Claude Code's native integration vocabulary."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aisquare.core import harness, paths
from aisquare.core.agent_adapters.claude_screen import (
    box_drawn,
    box_idle,
    modal_showing,
    prompt_showing,
)
from aisquare.core.agent_adapters.types import (
    AgentCapabilities,
    AgentEvent,
    AgentScreen,
    AgentTranscript,
    BadEffortError,
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
    PluginInstall,
    SandboxWrites,
    SessionFlags,
    SkillDirs,
    WriteScope,
    block_reply,
    command_entry,
    option_occurrences,
    option_values,
    package_version,
    payload_cwd,
    payload_text,
)
from aisquare.core.claude_accounts import (
    INSTALL_ALTERNATIVE,
    INSTALL_COMMAND,
    ClaudeCodeAccounts,
)

#: ``--permission-mode`` for each approvals intent (``core.config.CLAUDE_PERMISSION_MODES``);
#: ``never`` is ``bypassPermissions`` with a ``full`` write scope, else ``dontAsk``.
_MODES = {"ask": "manual", "auto": "auto", "never": "dontAsk"}
#: Claude Code's seam for appending to its default system prompt: a FILE, so the body never
#: sits in argv, which ``ps`` shows. Either spelling in the launch arguments is the operator's.
SYSTEM_PROMPT_FLAG = "--append-system-prompt-file"
_SYSTEM_PROMPT_FLAGS = (SYSTEM_PROMPT_FLAG, "--append-system-prompt")
#: A headless call's flags around ``--json-schema``: ``harness.probe_model``'s isolation. No
#: ``--bare``: Claude Code 2.1.272 documents that under it "OAuth and keychain are never
#: read", so an OAuth-bound manager account could not pay.
_HEADLESS_HEAD = ("--tools", "", "--max-turns", "1", "--output-format", "json")
_HEADLESS_TAIL = ("--no-session-persistence", "--settings", "{}", "--strict-mcp-config")
#: Claude Code's Homebrew cask, offered on macOS after the native installer.
BREW_COMMAND = "brew install --cask claude-code"
#: A native install's target, named by its version (``…/share/claude/versions/2.1.250``).
_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+\S*$")


def _effort_level(payload: Mapping[str, Any]) -> str | None:
    """The ``effort.level`` field of a hook payload (an object; optional)."""
    effort = payload.get("effort")
    if not isinstance(effort, dict):
        return None
    level = effort.get("level")
    return level if isinstance(level, str) and level else None


class ClaudeCodePlugin:
    """The aisquare Claude Code plugin (``plugins/claude-code``), as ``core.agents`` reads it."""

    stands_down_per_event = True

    def supported(self) -> bool:
        from aisquare.core import agents

        return agents.plugin_route_supported()

    def installed(self, config_dir: Path | None) -> PluginInstall | None:
        from aisquare.core import agents

        return agents.claude_plugin(config_dir)

    def repo_installs(self, config_dir: Path | None) -> list[PluginInstall]:
        from aisquare.core import agents

        return agents.claude_repo_plugins(config_dir)

    def repo_install_here(self, config_dir: Path | None, cwd: Path | None) -> PluginInstall | None:
        from aisquare.core import agents

        return agents.claude_repo_plugin_here(config_dir, cwd)

    def command(
        self, verb: str, config_dir: Path, *, scope: str = "user", project: Path | None = None
    ) -> str:
        from aisquare.core import agents

        return agents.claude_plugin_command(verb, config_dir, scope=scope, project=project)


class ClaudeCodeAdapter:
    id = "claude-code"
    label = "Claude Code"
    binary = "claude"
    home_env = "CLAUDE_CONFIG_DIR"
    home_name = ".claude"
    settings_name = "settings.json"
    install_hint = "https://claude.ai/install.sh"
    #: The native installer first: what ``install.sh`` runs and the layout
    #: ``diagnostics.claude_code_version`` reads first; npm last, for it needs a Node.js.
    install_routes: tuple[InstallRoute, ...] = (
        InstallRoute("installer", INSTALL_COMMAND),
        InstallRoute("Homebrew", BREW_COMMAND, frozenset({"darwin"})),
        InstallRoute("npm", INSTALL_ALTERNATIVE),
    )
    version_args: tuple[str, ...] = ("--version",)
    hooks_off_setting = '"disableAllHooks": true'
    exit_command = "/exit"
    #: Esc answers its permission prompt No and closes its selectors.
    dismiss_key = "Escape"
    #: Every key means what tmux names it: shift+tab cycles the permission mode, alt+p
    #: switches the model, shift+enter is a newline (``C-j`` on a tmux too old for it).
    key_spellings: Mapping[str, str] = MappingProxyType({})
    accounts = ClaudeCodeAccounts()
    plugin = ClaudeCodePlugin()
    screen: AgentScreen | None = AgentScreen(
        start_dialog_mark="Enter to confirm",
        screen_prompt=prompt_showing,
        ready_on_screen=box_idle,
        prompt_on_screen=box_drawn,
        modal_on_screen=modal_showing,
    )
    capabilities = AgentCapabilities(
        launch_inputs=(".claude", ".mcp.json"),
        hooks=(
            HookSpec("SessionStart", "session-start", 120),
            HookSpec("UserPromptSubmit", "user-prompt-submit", 120),
            HookSpec("SessionEnd", "session-end"),
            HookSpec("Stop", "stop"),
            HookSpec("Notification", "notification"),
            # Fires INSTEAD of Stop when a turn ends on an API error (#146).
            HookSpec("StopFailure", "stop-failure"),
        ),
        assigns_session_id=True,
        session_flags=SessionFlags(
            names="--session-id",
            resumes=("--resume", "-r"),
            continues=("--continue", "-c"),
            forks=("--fork-session",),
        ),
        approves_for_me=True,
        ambient_first_run=True,
        reads_pane_readiness=True,
        starts_at_its_prompt=True,
        resumes_across_homes=True,
        pid_env="CLAUDE_PID",
        exports_home_env=False,
        own_doctor_check=True,
        model_proxy=True,
        model_ladders=True,
        legacy_fleet_args=True,
        value_options=frozenset(
            {
                "--model",
                "-m",
                "--add-dir",
                "--settings",
                "--effort",
                "--resume",
                "--session-id",
                "--permission-mode",
                "--mcp-config",
                "--allowedTools",
                "--disallowedTools",
                "--system-prompt",
                "--append-system-prompt",
                "--max-turns",
                "--output-format",
                "--input-format",
                "--name",
            }
        ),
        switch_options=frozenset(
            {
                "--dangerously-skip-permissions",
                "--continue",
                "--verbose",
                "--chrome",
                "--no-chrome",
            }
        ),
    )

    telemetry_reader = None  # no native telemetry: its traffic goes through the proxy

    @property
    def transcript_reader(self) -> AgentTranscript:
        # Read when Remote asks, never on a hook: the reader and rich's cell widths load then.
        from aisquare.core.agent_adapters.claude_transcript import ClaudeTranscript

        return ClaudeTranscript()

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        """As written, links and all (``core.agents._claude_home``): an explicit dir, else
        ``CLAUDE_CONFIG_DIR``, else ``~/.claude``. A link that leads nowhere is named as
        the link, where another agent's is resolved (:func:`config_home`)."""
        if explicit is not None:
            return paths.expand_user(explicit)
        value = env.get(self.home_env, "").strip()
        if value:
            return paths.expand_user(Path(value))
        return home / self.home_name

    def layout_version(self, binary: str) -> str | None:
        """From the install layout, never ``claude --version``: the native installer links
        ``claude`` to ``…/share/claude/versions/<version>`` (the target's name IS the
        version); an npm install links it to ``…/node_modules/@anthropic-ai/claude-code/
        cli.js`` with a ``package.json`` beside it. Anything else is ``None``: an honest
        blank in the detail beats a guess."""
        try:
            real = Path(binary).resolve()
        except (OSError, RuntimeError):
            return None
        if _VERSION_DIR.match(real.name):
            return real.name
        return package_version(str(real), depth=1)

    def hooks_switch(
        self, home: Path, settings_path: Path, args: Sequence[str] = ()
    ) -> HooksSwitch | None:
        """``"disableAllHooks": true`` in ``settings.json``, a literal ``true`` as Claude
        Code reads it; ``args`` are not read."""
        from aisquare.core.agents import read_json

        if read_json(settings_path).get("disableAllHooks") is True:
            return HooksSwitch(self.hooks_off_setting, settings_path)
        return None

    def hook_entry(self, spec: HookSpec, command: str) -> dict[str, Any]:
        """``{"type": "command", "command": …, "timeout": <seconds>}`` in a ``settings.json``
        group (:func:`command_entry`)."""
        return command_entry(spec, command)

    def lasting_installs(self, home: Path) -> tuple[LastingInstall, ...]:
        """The MCP servers that run aisquare in the ``.claude.json`` ``home`` uses and in the
        ``.mcp.json`` of each project it lists, and the aisquare plugin enabled in ``home`` or
        in a repository (``claude_installs``), each with the ``claude`` command that removes it."""
        from aisquare.core.agent_adapters import claude_installs

        return claude_installs.lasting_installs(home)

    def decode_event(self, payload: Mapping[str, Any]) -> AgentEvent | None:
        """One payload per hook subcommand (``aisquare hook session-start``…), which names
        the event; every field optional, a missing ``session_id`` included."""
        event = payload_text(payload, "hook_event_name")
        common: dict[str, Any] = {
            "session_id": payload_text(payload, "session_id"),
            "cwd": payload_cwd(payload),
        }
        if event == "SessionStart":
            return AgentEvent(
                "session_start",
                source=payload_text(payload, "source"),
                transcript_path=payload_text(payload, "transcript_path"),
                model=payload_text(payload, "model"),
                effort=_effort_level(payload),
                **common,
            )
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt")
            return AgentEvent(
                "prompt",
                prompt=prompt if isinstance(prompt, str) else None,
                transcript_path=payload_text(payload, "transcript_path"),
                model=payload_text(payload, "model"),
                effort=_effort_level(payload),
                **common,
            )
        if event == "SessionEnd":
            return AgentEvent("session_end", reason=payload_text(payload, "reason"), **common)
        if event == "Stop":
            # The loop guard: any value but absent/null/false is "already continuing".
            return AgentEvent(
                "stop", stop_hook_active=bool(payload.get("stop_hook_active")), **common
            )
        if event == "StopFailure":
            return AgentEvent(
                "turn_failed",
                error=payload_text(payload, "error"),
                message=payload_text(payload, "last_assistant_message"),
                details=payload_text(payload, "error_details"),
                **common,
            )
        if event == "Notification":
            return AgentEvent(
                "notification",
                message=payload_text(payload, "message"),
                notification_type=payload_text(payload, "notification_type"),
                **common,
            )
        return None

    def encode_reply(self, kind: EventKind, reply: str | None) -> str:
        """stdout of ``SessionStart``/``UserPromptSubmit`` becomes context; a ``Stop`` that
        keeps the turn going is the top-level ``block`` decision."""
        if not reply:
            return ""
        return block_reply(reply) if kind == "stop" else reply

    def resolve_model(
        self,
        role: str,
        *,
        binary: str,
        env: dict[str, str],
        probe: bool | None,
        refresh: bool,
        effort: str | None,
    ) -> harness.ModelResolution | None:
        """The role's ladder (``harness.resolve_model``), under the pins of
        ``[agents.models.claude-code]``: its model over the ladder's choice and its
        effort over the base and offset, each below the ``AISQUARE_MODEL_<ROLE>`` /
        ``AISQUARE_EFFORT_<ROLE>`` pins and an explicit ``--effort``."""
        from aisquare.core.agent_adapters.native_models import configured

        model, level = configured(self.id, role)
        if level is not None and effort is None and harness.role_effort_override(role, env) is None:
            self.validate(None, level)
            effort, effort_source = level, "configured"
        else:
            effort_source = None
        result = harness.resolve_model(
            role,
            probe=probe if model is None else False,
            refresh=refresh,
            effort=effort,
            context=harness.ProbeContext(binary=binary, env=env),
        )
        if result is None and model is None:
            return None
        if result is None:
            level, source = harness.resolve_effort(role, explicit=effort)
            result = harness.ModelResolution(
                role=role, model=model, effort=level, effort_source=source, source="configured"
            )
        elif model is not None and harness.role_model_override(role, env) is None:
            result = result.model_copy(
                update={"model": model, "source": "configured", "skipped": []}
            )
        if effort_source is not None:
            result = result.model_copy(update={"effort_source": effort_source})
        return result

    def mcp_args(
        self,
        executable: str,
        args: list[str],
        env_vars: list[str] | None = None,
    ) -> LaunchPart:
        import json

        config = {"mcpServers": {"aisquare": {"command": executable, "args": args}}}
        return LaunchPart(("--mcp-config", json.dumps(config)))

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
        """main's captain flags: the one server (``--strict-mcp-config``, so no other MCP
        server of the operator's loads, from any file), no built-in tool (``--tools ""``),
        and every tool of the server allowed (``--allowedTools mcp__<server>__*``). Its MCP
        servers inherit the session's environment whole, ``inherit`` included, so neither
        it nor ``home``, ``cwd`` or ``launch_args`` changes the answer."""
        import json

        config = {"mcpServers": {server: {"command": command, "args": args, "env": env}}}
        return [
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(config),
            "--tools",
            "",
            "--allowedTools",
            f"mcp__{server}__*",
        ]

    def context_files(self, home: Path) -> tuple[Path, ...]:
        return (home / "CLAUDE.md",)

    def browser_tools(self, home: Path) -> list[str] | None:
        return None  # MCP servers and plugins across its config dirs: the doctor's own scan

    def native_args(self, args: list[str]) -> list[str]:
        return list(args)

    def native_choice(self, args: Sequence[str]) -> NativeChoice:
        models = option_values(args, "--model", "-m")
        efforts = list(option_occurrences(args, "--effort"))
        return NativeChoice(
            models[-1] if models else None,
            efforts[-1].value if efforts else None,
            efforts[-1].index if efforts else None,
        )

    @staticmethod
    def effort_alias(effort: str) -> str:
        return effort.strip().lower()

    def validate(self, model: str | None, effort: str | None) -> None:
        if effort is not None and harness.normalize_effort(effort) is None:
            raise BadEffortError(
                f"Claude Code does not support effort {effort!r}; use one of: "
                + ", ".join((*harness.EFFORT_SCALE, harness.ULTRACODE))
            )

    def role_defaults(self, role: str, *, binary: str, args: Sequence[str]) -> harness.RoleDefaults:
        """``RoleProfile.default_args`` (the ui-tester's ``--chrome``, ``harness.
        profile_defaults``) for a binary whose name runs Claude Code (``adapter_for_binary``),
        and none, with a note, for a wrapper of another name (``claude2``), which may be
        another program and would reject them (``harness.withheld_defaults``)."""
        from aisquare.core.agent_adapters import adapter_for_binary

        runs = adapter_for_binary(binary)
        if runs is None or runs.id != self.id:
            return harness.withheld_defaults(role, binary, self.binary)
        return harness.profile_defaults(role, args)

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        self.validate(model, effort)
        return (["--model", model] if model else []) + (["--effort", effort] if effort else [])

    def permission_args(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> PermissionArgs:
        """:meth:`_permission_flags`: it reviews ``auto`` itself (``approves_for_me``)."""
        return PermissionArgs(tuple(self._permission_flags(intent, role, native)))

    def _permission_flags(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> list[str]:
        """``--permission-mode`` (``ask`` ``manual``, ``auto`` ``auto``, ``never``
        ``dontAsk``, or ``bypassPermissions`` with a ``full`` write scope); Claude Code has
        no sandbox, so ``read_only`` is the reviewer's denial of the tools that edit files
        (``core.config.REVIEWER_ARGS``). A mode or a denial ``native`` already passes (the
        reviewer's role arguments carry that denial) is not passed twice."""
        from aisquare.core.config import REVIEWER_ARGS

        mode = (
            "bypassPermissions"
            if intent.write == "full" and intent.approvals == "never"
            else _MODES[intent.approvals]
        )
        chosen = list(option_values(native, "--permission-mode"))
        denied = [arg for arg in REVIEWER_ARGS if arg not in native]
        return (["--permission-mode", mode] if not chosen else []) + (
            denied if intent.write == "read_only" else []
        )

    def store_access(self, write: WriteScope, store: Path) -> list[str]:
        return []  # no sandbox: its tools write wherever the user can, the store included

    def sandbox_writes(
        self, args: Sequence[str], cwd: Path, home: Path | None = None
    ) -> SandboxWrites | None:
        return None  # no sandbox: trusted as its user is

    def broker_access(
        self,
        args: Sequence[str],
        store: Path,
        inbox: Path,
        *,
        protect: Sequence[Path] = (),
        writable: Sequence[Path] = (),
    ) -> list[str] | None:
        return None  # no sandbox, so no broker: its board calls write the store themselves

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
            raise ValueError(
                "Claude Code uses --permission-mode; sandbox/approval are Codex settings"
            )
        return (["--permission-mode", permission_mode] if permission_mode else []) + list(
            self.label_args(label).args
        )

    def label_args(self, label: str) -> LaunchPart:
        return LaunchPart(("--name", label))

    def resume_args(self, native_session_id: str, transcript_path: Path | None) -> list[str]:
        """``--resume <transcript>``: Claude Code ≥ 2.1.223 finds a session by its
        transcript's absolute path from any directory, and keeps its id (#146)."""
        return ["--resume", str(transcript_path) if transcript_path else native_session_id]

    def fork_args(self, native_session_id: str, transcript_path: Path | None) -> ForkArgs | None:
        """``--resume <transcript> --fork-session``, main's hand-off Fork: a new session from
        the old one's conversation, which the launch names with ``--session-id <new>``."""
        resume = self.resume_args(native_session_id, transcript_path)
        return ForkArgs((*resume, "--fork-session"), mints_id=True)

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        return None  # no queue: `fleet tell` types into its prompt when the pane waits

    def disable_native_teams(self) -> LaunchPart:
        return LaunchPart(env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0"})

    def fleet_settings(self, home: Path, args: Sequence[str]) -> LaunchPart:
        """None: its dialogs in a running pane reach the board through ``Notification``."""
        return LaunchPart()

    def settings_layer(
        self,
        settings: Mapping[str, Any],
        file: Path,
        *,
        home: Path,
        env: Mapping[str, str],
        cwd: Path,
    ) -> LaunchPart | None:
        """``--settings <file>``, a JSON layer above the operator's settings files. None of its
        slots answers settings today; an agent's own would come here."""
        import json

        file.write_text(json.dumps(settings), encoding="utf-8")
        return LaunchPart(("--settings", str(file)))

    def unattended_env(self) -> Mapping[str, str]:
        """No session-rating survey (``How is Claude doing this session?``), which drew in the
        captain's pane mid-conversation, where nothing may type and no phone can answer."""
        return MappingProxyType({"CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1"})

    def telemetry_args(self, endpoint: str, *, keep_out: Sequence[str] = ()) -> LaunchPart:
        return LaunchPart()  # no native telemetry: its traffic goes through the proxy

    def system_prompt_args(
        self, block: Path, text: str, args: Sequence[str], home: Path, *, cwd: Path
    ) -> list[str] | None:
        """``--append-system-prompt-file <block>``, appended to Claude Code's default system
        prompt for the life of the process (``/clear`` keeps it; a restart passes it again);
        ``None`` where ``args`` carry ``--append-system-prompt`` of either spelling."""
        if any(
            arg == flag or arg.startswith(f"{flag}=")
            for arg in args
            for flag in _SYSTEM_PROMPT_FLAGS
        ):
            return None
        return [SYSTEM_PROMPT_FLAG, str(block)]

    def headless_call(
        self, schema: Path, out: Path, model: str | None, *, home: Path, cwd: Path
    ) -> list[str] | None:
        """``-p`` with ``--json-schema`` (the text of ``schema``, which it reads, ``OSError``
        when it cannot) and ``--output-format json``, isolated as the model probe is: no
        tools, one turn, no session saved, no settings, no MCP servers (``--strict-mcp-config``
        with none, so ``home`` and ``cwd`` are not read). ``out`` is unused: the answer is in
        the envelope it prints."""
        text = schema.read_text(encoding="utf-8").strip()
        model_args = ["--model", model] if model else []
        return ["-p", *model_args, *_HEADLESS_HEAD, "--json-schema", text, *_HEADLESS_TAIL]

    def headless_answer(self, stdout: str, out: Path) -> object | None:
        """The ``--output-format json`` envelope's ``structured_output``, else its ``result``
        read as JSON; ``None`` for one that says ``is_error``, or holds neither."""
        import json

        try:
            envelope = json.loads(stdout)
        except (ValueError, RecursionError):
            return None
        if not isinstance(envelope, dict) or envelope.get("is_error"):
            return None
        answer = envelope.get("structured_output")
        if answer is None and isinstance(envelope.get("result"), str):
            try:
                answer = json.loads(envelope["result"])
            except (ValueError, RecursionError):
                return None
        return answer

    def skill_dirs(self, home: Path, repo: Path | None) -> SkillDirs:
        """``<config dir>/skills`` and ``<repo>/.claude/skills``."""
        return SkillDirs(
            (home / "skills",), (repo / ".claude" / "skills",) if repo is not None else ()
        )
