"""Codex CLI 0.162.1 integration; native hooks keep the real terminal UI."""

import json
import os
import re
import shlex
import sys
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aisquare.core import harness
from aisquare.core.agent_adapters.types import (
    AgentCapabilities,
    AgentEvent,
    AgentTranscript,
    BadEffortError,
    EventKind,
    ForkArgs,
    HookSpec,
    HooksSwitch,
    NativeChoice,
    PermissionIntent,
    PluginInstall,
    SandboxWrites,
    ScreenPrompt,
    SkillDirs,
    WriteScope,
    block_reply,
    config_assignment,
    config_home,
    option_occurrences,
    option_values,
    payload_cwd,
    payload_text,
    rewrite_option_values,
    store_dir,
    store_read_only,
)
from aisquare.core.codex_accounts import CodexAccounts, _records_from_end, approvals_reviewer
from aisquare.core.paths import expand_user

#: ``model_reasoning_effort`` levels Codex names (0.162.1). Which of them a model
#: takes is Codex's to say, so another level is passed through with a note.
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
#: The one shared effort name Codex lacks: ``ultracode`` is Claude Code's ``max``
#: plus workflow orchestration, of which Codex takes the level alone.
EFFORT_ALIASES = {"ultracode": "max"}
#: ``approval_policy`` values the CLI takes: ``untrusted`` is gone, ``on-failure``
#: deprecated, and the granular table is config-only.
APPROVAL_POLICIES = ("on-request", "never")
#: Options that settle the sandbox or approvals, which ``--approve-for-me`` cannot
#: be combined with (Codex refuses the launch).
_APPROVAL_SWITCHES = frozenset(
    {"--approve-for-me", "--dangerously-bypass-approvals-and-sandbox", "--yolo"}
)
#: The features whose tools :meth:`CodexAdapter.tools_only_args` switches off (with
#: ``web_search="disabled"``), by their names in ``codex features list``. Measured on 0.162.1:
#: ``exec`` offers ``exec_command``, ``write_stdin``, ``request_user_input``, ``view_image``,
#: the multi-agent and goal tools and ``web_search``; with these off, the request offers
#: ``request_user_input``, which no feature switches off and which says it is only
#: available in Plan mode, and, beside an MCP server, Codex's three readers of MCP
#: resources (``list_mcp_resources``, ``list_mcp_resource_templates``, ``read_mcp_resource``).
TOOL_FEATURES = (
    "shell_tool",
    "unified_exec",
    "view_image",
    "multi_agent",
    "goals",
    "apps",
    "plugins",
    "browser_use",
    "computer_use",
    "sleep_tool",
    "tool_suggest",
    "skill_search",
    "in_app_browser",
    "image_generation",
)
#: A ``-c`` key splits on every dot, so an MCP server's name in one is a bare TOML key.
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
#: aisquare's Codex plugin (``plugins/codex``), by the name ``codex plugin add
#: aisquare@<marketplace>`` takes and ``config.toml`` keys it under, ``[plugins."aisquare@
#: aisquare-cli"]`` from this repository's marketplace (``.agents/plugins/marketplace.json``).
PLUGIN = "aisquare"
PLUGIN_MARKETPLACE = "aisquare-cli"
#: Where in the plugin Codex reads its hooks, which keys their trust (``[hooks.state]``).
PLUGIN_HOOKS = "hooks/hooks.json"
#: ``codex plugin`` has add, list and remove: no disable (``enabled = false`` is the
#: ``/plugins`` toggle) and no uninstall, so each verb that stops it is ``remove``.
_PLUGIN_VERBS = {"disable": "remove", "uninstall": "remove", "install": "add"}


class CodexPlugin:
    """aisquare's Codex plugin (``plugins/codex``), as Codex 0.162.1 installs it (measured):
    enabled in a Codex home's ``config.toml``, copied into its ``plugins/cache``, and run
    only once ``/hooks`` trusts its hooks. A plugin belongs to one Codex home, at no other
    scope. Read-only; never raises."""

    #: Its launcher stands down beside ANY aisquare hook in hooks.json whose program can
    #: start, every event sharing one command there: a partial hooks.json is not covered.
    stands_down_per_event = False

    def supported(self) -> bool:
        from aisquare.core import agents

        return agents.plugin_route_supported()

    def installed(self, config_dir: Path | None) -> PluginInstall | None:
        """The plugin where the home's ``config.toml`` enables it and ``codex plugin add``
        installed it, else ``None``.

        Enabled is a ``[plugins."aisquare@<marketplace>"]`` table without ``enabled =
        false``; installed is its folder in ``plugins/cache/<marketplace>/aisquare/
        <version>/``, whose manifest names the version. Both, because an enabled table
        with nothing installed runs nothing (Codex keeps one version there and removes the
        one it replaces). Trusted is each of its hooks holding a ``trusted_hash`` in
        ``[hooks.state]``, which ``/hooks`` writes: Codex skips a plugin's hooks until then.
        A hash for an older definition reads as trusted here; Codex asks again only when
        ``hooks.json`` itself changes, and keeps the trust across a version that does not.
        """
        home = _codex_home(config_dir)
        found = _enabled_install(home)
        if found is None:
            return None
        plugin_id, root, config = found
        return PluginInstall(
            home, _manifest_version(root) or root.name, trusted=_plugin_trusted(config, plugin_id)
        )

    def repo_installs(self, config_dir: Path | None) -> list[PluginInstall]:
        return []  # a Codex plugin is installed in a home, never for one repository

    def repo_install_here(self, config_dir: Path | None, cwd: Path | None) -> PluginInstall | None:
        return None

    def command(
        self, verb: str, config_dir: Path, *, scope: str = "user", project: Path | None = None
    ) -> str:
        """``codex plugin <verb> aisquare@<marketplace>``, aimed at ``config_dir`` as
        ``aisquare uninstall`` aims it (``lifecycle._codex_command``): nothing for the home
        this shell's ``codex`` reads, ``env -u CODEX_HOME`` for ``~/.codex`` from a shell
        that names another, the variable otherwise. ``disable`` and ``uninstall`` are
        ``remove`` (:data:`_PLUGIN_VERBS`)."""
        from aisquare.core import agents

        found = _enabled_install(config_dir)
        plugin_id = found[0] if found is not None else f"{PLUGIN}@{PLUGIN_MARKETPLACE}"
        argv = ["codex", "plugin", _PLUGIN_VERBS.get(verb, verb), plugin_id]
        command = shlex.join(argv) if sys.platform != "win32" else " ".join(argv)
        ambient = os.environ.get("CODEX_HOME", "").strip()
        default = agents.dir_identity(agents.home() / ".codex")
        here = agents.dir_identity(Path(ambient)) if ambient else default
        there = agents.dir_identity(config_dir)
        if there == here:
            return command
        if sys.platform == "win32":
            need = "unset" if there == default else f"set to {config_dir}"
            return f"{command} (run it with CODEX_HOME {need})"
        if there == default:
            return f"env -u CODEX_HOME {command}"
        return f"CODEX_HOME={shlex.quote(str(config_dir))} {command}"


#: Each write scope of a :class:`PermissionIntent` in ``--sandbox``'s words, and back.
_SANDBOXES = {
    "read_only": "read-only",
    "workspace": "workspace-write",
    "full": "danger-full-access",
}
_SCOPES: dict[str, WriteScope] = {"read-only": "read_only", "workspace-write": "workspace"}
#: The permission profile (``[permissions.<name>]``, chosen by ``default_permissions``) a
#: fleet launch defines for its sandbox: Codex's built-in profile for the write scope with
#: the aisquare store added (:meth:`CodexAdapter.store_access`).
PERMISSION_PROFILE = "aisquare"

#: The system layer of Codex's config, under every user's (``services.native_telemetry``
#: reads it too): 0.162.1 reads it (in its binary, review r14 of #113, finding 9).
SYSTEM_CONFIG = Path("/etc/codex/config.toml")
#: The managed layer an administrator sets over every user's config, also in 0.162.1's
#: binary (review r15 of #113, finding 4). Its ``requirements.toml`` beside it only
#: narrows what may be chosen, and its macOS MDM and cloud-managed layers are not files.
MANAGED_CONFIG = Path("/etc/codex/managed_config.toml")
_BUILT_IN_PROFILES = {"read_only": ":read-only", "workspace": ":workspace"}
#: ``-c`` keys (and their tables) that choose the sandbox: given natively, the sandbox is
#: the operator's, and Codex refuses ``sandbox_mode`` beside ``default_permissions``.
_SANDBOX_SETTINGS = (
    "sandbox_mode",
    "sandbox_workspace_write",
    "default_permissions",
    "permission_profile",
)


def _filesystem(read: Sequence[Path], write: Sequence[Path]) -> str:
    """A ``[permissions.<name>].filesystem`` table as a ``-c`` value: ``read`` read-only,
    ``write`` writable, each path a TOML string (a Windows one's backslashes escaped)."""
    entries = {**dict.fromkeys(read, "read"), **dict.fromkeys(write, "write")}
    body = ", ".join(f"{json.dumps(str(path))} = {json.dumps(at)}" for path, at in entries.items())
    return f"{{{body}}}"


def _filesystem_table(value: object) -> dict[str, str]:
    """A ``-c permissions.<name>.filesystem=`` value as ``{path: "read" | "write"}``;
    ``{}`` for one that is absent or does not parse."""
    if not isinstance(value, str):
        return {}
    try:
        parsed = tomllib.loads(f"t = {value}")["t"]
    except (tomllib.TOMLDecodeError, RecursionError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(path): str(at) for path, at in parsed.items() if isinstance(at, str)}


#: Who answers an approval request (``approvals_reviewer``): the human, or Codex's
#: automatic reviewer (``--approve-for-me``; its old name ``guardian_subagent`` is
#: recorded as this one).
REVIEWERS = {"ask": "user", "auto": "auto_review"}
#: The hook events AISquare installs (``capabilities.hooks``) and decodes.
_EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "SessionEnd": "session_end",
    "Interrupt": "interrupt",
    "PermissionRequest": "attention",
    "PreToolUse": "attention",
    "PostToolUse": "tool_done",
}

#: Codex's status line while a turn runs: its spinner, a header (``Working``, or the one a
#: reasoning summary gives it), the time it has run and ``esc to interrupt``, then whatever
#: it adds (`` · 1 background terminal running · …``). Measured on 0.162.1. The spinner is
#: ``•`` in a terminal that reports truecolor (``COLORTERM``), and blinks ``•``/``◦``
#: otherwise, as in a window whose spawner had no ``COLORTERM`` to pass on (headless, SSH,
#: cron): measured under tmux, the glyph alternated on reads 0.3 s apart (review r4 of
#: #113, finding 7).
_STATUS_LINE = re.compile(r"[•◦] .+ \(\d+[hms](?: \d+[hms])* • esc to interrupt\)(?: · .*)?")
#: What starts Codex's composer row, and every user prompt in its transcript above it:
#: U+203A and a space.
_CHEVRON = "\u203a "
#: The header of the follow-ups queued during a turn, which Codex draws between its
#: status line and its composer, each one under it on a ``↳`` row (0.162.1).
_QUEUED = "• Queued follow-up input"
#: What starts a row Codex draws under its status line about it: a hint
#: (``└ Tip: …``, measured on 0.162.1 signed in with ChatGPT).
_DETAIL = "  \u2514 "
#: What starts a queued follow-up's row under its header.
_QUEUED_ROW = "  \u21b3 "
#: What starts the rows a detail or a follow-up wraps onto, and the queue's own hint.
_WRAPPED = "    "


IDLE_PROMPT_MARK = "? for shortcuts"
"""The hint Codex's footer ends its screen with at an empty prompt, in its alternate screen
(the default): a draft in the composer, or a dialog, takes its place (0.162.1)."""
#: What ends that screen in place of :data:`IDLE_PROMPT_MARK` once one Esc was pressed at the
#: empty prompt of a session with a turn behind it, until the next key: the same prompt, where
#: a typed line and its Enter land as one. A second Esc opens its transcript, where Enter
#: rewinds (``Browsing transcript · … ↵ rewind · esc back``). Never drawn over a draft
#: (0.162.1, measured under tmux).
_ESC_AGAIN = "esc again to edit previous message"
#: What Codex's empty composer reads: its placeholder, the same in every session, and the
#: one a follow-up shows (0.162.1).
_PLACEHOLDERS = frozenset({"Ask Codex to do anything", "Ask a follow-up question"})


def idle_on_screen(rows: Sequence[str]) -> bool:
    """Whether ``rows``, Codex's visible screen, show it at its EMPTY prompt with nothing
    drawn over it (``AgentCapabilities.idle_on_screen``).

    Measured on 0.162.1 under tmux. In its alternate screen (the default) the last row is
    the footer's :data:`IDLE_PROMPT_MARK`, which a draft in the composer, or any dialog,
    replaces, or, after one Esc there, its ``esc again to edit previous message``
    (:data:`_ESC_AGAIN`), the prompt all the same (Remote's Interrupt & tell sends that Esc
    before its line, and so does a phone's key pad). In the terminal's own
    (``--no-alt-screen``) the footer has no such hint: the
    last chevron row (U+203A) is then the empty composer's placeholder with the footer
    alone under it (``<model> <effort> · <directory>``). A dialog's last chevron row is its
    chosen option, its other options and its key hint under it (``1. Switch to
    gpt-6-luna`` … ``enter select · esc back``, after a turn near the limit; the folder-trust
    and hook-review dialogs), and a draft replaces the placeholder. A turn running keeps
    both layouts' empty prompt, its status line above the composer.
    """
    lines = [row.rstrip() for row in rows if row.strip()]
    if not lines:
        return False
    if lines[-1].strip().startswith((IDLE_PROMPT_MARK, _ESC_AGAIN)):
        return True
    composer = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].startswith(_CHEVRON)),
        None,
    )
    if composer is None or lines[composer][len(_CHEVRON) :].strip() not in _PLACEHOLDERS:
        return False
    footer = lines[composer + 1 :]
    return len(footer) == 1 and footer[0].startswith("  ")


def working_on_screen(rows: Sequence[str]) -> bool:
    """Whether ``rows``, Codex's visible screen, show its status line where Codex draws it
    while a turn runs unblocked (``AgentCapabilities.working_on_screen``).

    Measured on 0.162.1 under tmux, from the composer (the last row that starts with
    Codex's chevron, U+203A) up, blank rows apart: the follow-ups queued for the turn,
    when there are any (their header, then ``↳`` rows and the rows they wrap onto); then
    any rows Codex draws under its status line about it (``└ Tip: …``, wrapped onto rows
    indented four); then the status line, found by its shape: its header, the time it
    has run and ``esc to interrupt`` (review r3 of #113, finding 6; its Tip row, the
    tester's smoke 3). Nothing else may sit between them. In the approval view the last
    chevron row is the option chosen (``1. Yes, proceed (y)``), with the view's own rows
    above it, every one indented two and none of them a ``└`` or ``↳`` row's shape alone:
    the command awaiting approval, which can hold the status line's very words, among
    them, cut to ``[… N lines]`` in a short pane. The transcript, an agent's output and
    its ``• Running <command>`` included, sits above both. So the words count only in
    that place, never anywhere else on the screen.
    """
    lines = [row.rstrip() for row in rows if row.strip()]
    composer = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].startswith(_CHEVRON)),
        None,
    )
    if composer is None:
        return False
    above = _past(lines, composer - 1, head=_QUEUED, rows=(_QUEUED_ROW, _WRAPPED))
    above = _past(lines, above, head=_DETAIL, rows=(_WRAPPED,), head_indented=True)
    return above >= 0 and _STATUS_LINE.fullmatch(lines[above]) is not None


#: The key hint every dialog of Codex's own ends its screen with: ``enter continue · esc
#: quit`` (folder trust), ``enter confirm · esc skip`` (hook review), ``enter select · esc
#: back`` (``Switch to gpt-6-luna``, ``Implement this plan?``), ``Press enter to confirm or
#: esc to cancel`` (an approval), ``… | enter to submit answer | esc to interrupt`` (a
#: question). The footer under a draft names neither key, and the transcript browser's has
#: ``↵ rewind · esc back`` (0.162.1, measured under tmux).
_DIALOG_KEYS = re.compile(r"\benter\b.*\besc\b", re.IGNORECASE)
#: The option a dialog has chosen, U+203A and a space before it (``1. Trust and continue``
#: after them), at the margin or, in a question's view, indented.
_CHOSEN = re.compile(r"\s*\u203a \d{1,2}\. ")
#: A dialog's first option, chosen or not.
_FIRST_OPTION = re.compile(r"\s*(?:\u203a )?1\. ")


def dialog_on_screen(rows: Sequence[str]) -> str | None:
    """What a dialog of Codex's own on ``rows``, its visible screen, asks: the words right
    above its options (``AgentCapabilities.dialog_on_screen``); ``None`` when no dialog is up.

    Measured on 0.162.1 under tmux: each of its dialogs ends the screen with its key hint
    (:data:`_DIALOG_KEYS`), and its last chevron row (U+203A) is then the option it has
    chosen, not the composer: the folder-trust question Codex asks in a git checkout no
    trust covers, its hook review, the ``Switch to gpt-6-luna for lower credit usage?``
    it opens after a turn's ``Stop`` near the limit, ``Implement this plan?`` after a
    Plan-mode turn's, and its approval and question views.
    A draft in the composer holds the last chevron row with the footer under it (a draft
    that starts ``1. `` too), a turn draws its status line above the composer, and the
    transcript after a second Esc ends on ``↵ rewind · esc back``: none of them is one.
    The words are the rows above the first option, past the blank rows between, up to the
    next blank row or a row at the margin (a prompt or the agent's words in the transcript
    above): ``Trust this folder? Codex can read, edit, and run files here, …``, ``Hooks need
    review 8 hooks are new or changed. …``, ``Approaching rate limits Switch to gpt-6-luna
    for lower credit usage?``, ``Implement this plan?``."""
    found = _dialog(rows)
    return found[0] if found is not None else None


#: The question Codex's folder-trust dialog opens with, in a git checkout no trust covers.
_TRUST_QUESTION = "Trust this folder?"
#: The key hint of Codex's approval view alone (a command, an edit, a permission), whose
#: options take their digit and Esc says No (0.162.1, measured under tmux).
_APPROVAL_KEYS = re.compile(r"\bPress enter to confirm or esc to cancel\b")
#: A dialog's option: its digit and its label, the chosen one behind U+203A.
_OPTION = re.compile(r"\s*(?:\u203a )?(\d{1,2})\. (.*)")


def screen_prompt(rows: Sequence[str]) -> ScreenPrompt | None:
    """The prompt a dialog of Codex's own on ``rows`` shows (``AgentCapabilities.
    screen_prompt``), read where :func:`dialog_on_screen` reads one; ``None`` with none up.

    Measured on 0.162.1 under tmux: its folder-trust question (``Trust this folder?``) is
    ``trust``, the owner's to answer. Its approval view (``Would you like to run the following
    command?`` and the rest that end on ``Press enter to confirm or esc to cancel``) is a
    ``chooser``: ``1. Yes, proceed (y)`` takes its digit (the first option that says Yes)
    and Esc answers No (``3. No, and tell Codex what to do differently (esc)``), as Remote's
    quick answers send them. Every other (the hook review, ``Switch to gpt-6-luna for lower
    credit usage?``, ``Implement this plan?``, a question's view) is a ``dialog`` whose keys
    are the owner's. ``whole`` is the dialog from its words down, the highlight dropped."""
    found = _dialog(rows)
    if found is None:
        return None
    words, region = found
    whole = " ".join(" ".join(region).replace(_CHEVRON.strip(), " ").split())
    if words.startswith(_TRUST_QUESTION):
        return ScreenPrompt("trust", words, None, None, whole)
    if _APPROVAL_KEYS.search(region[-1]):
        options = [match for match in map(_OPTION.match, region) if match is not None]
        yes = next((m.group(1) for m in options if m.group(2).startswith("Yes")), None)
        return ScreenPrompt("chooser", words, yes, "Escape", whole)
    return ScreenPrompt("dialog", words, None, None, whole)


def _dialog(rows: Sequence[str]) -> tuple[str, list[str]] | None:
    """A dialog of Codex's own on ``rows`` (:func:`dialog_on_screen`): its words, and its rows
    from them down to its key hint, each stripped, blank rows dropped; ``None`` with none."""
    lines = [row.rstrip() for row in rows]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or _DIALOG_KEYS.search(lines[-1]) is None:
        return None
    chosen = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if lines[index].lstrip().startswith(_CHEVRON)
        ),
        None,
    )
    if chosen is None or _CHOSEN.match(lines[chosen]) is None:
        return None
    first = next(
        (index for index in range(chosen, -1, -1) if _FIRST_OPTION.match(lines[index])), chosen
    )
    top = first - 1
    while top >= 0 and not lines[top].strip():
        top -= 1
    words: list[str] = []
    while top >= 0 and lines[top].strip() and lines[top].startswith(" "):
        words.insert(0, lines[top].strip())
        top -= 1
    return " ".join(words), [line.strip() for line in lines[top + 1 :] if line.strip()]


def _past(
    lines: list[str],
    index: int,
    *,
    head: str,
    rows: tuple[str, ...],
    head_indented: bool = False,
) -> int:
    """The index above the block that ends at ``index`` (``rows`` under ``head``), or
    ``index`` itself when no such block ends there. A ``head_indented`` block may be its
    head alone; the other needs a row under it."""
    top = index
    while top >= 0 and lines[top].startswith(rows):
        top -= 1
    if top < 0 or not lines[top].startswith(head):
        return index
    if not head_indented and top == index:
        return index
    return top - 1


class CodexAdapter:
    id = "codex"
    label = "Codex"
    binary = "codex"
    home_env = "CODEX_HOME"
    home_name = ".codex"
    settings_name = "hooks.json"
    install_hint = "npm install -g @openai/codex"
    version_args: tuple[str, ...] = ("--version",)
    hooks_off_setting = "`features.hooks = false`"
    #: Typed, then its Enter 0.2 s later: the paste-burst guard ate an Enter sent with it.
    exit_command = "/exit"
    #: Its dialogs' ``esc back``: Esc leaves the ``Switch to gpt-6-luna?`` offer on the
    #: current model and answers an approval No, back at the prompt (0.162.1).
    dismiss_key = "Escape"
    #: Measured on 0.162.1 under tmux 3.7c: shift+enter submits the prompt (it arrives
    #: as Enter), ctrl+J inserts the newline. Esc interrupts a turn and shift+tab cycles
    #: Default/Plan mode, as their names are sent.
    key_spellings: Mapping[str, str] = MappingProxyType({"S-Enter": "C-j"})
    accounts = CodexAccounts()
    plugin = CodexPlugin()
    capabilities = AgentCapabilities(
        launch_inputs=(".codex", ".agents"),
        hooks=(
            HookSpec("SessionStart", "codex", 120),
            HookSpec("UserPromptSubmit", "codex", 120),
            HookSpec("Stop", "codex", 30),
            HookSpec("SessionEnd", "codex", 3),
            HookSpec("Interrupt", "codex", 3),
            HookSpec("PermissionRequest", "codex", 3),
            HookSpec("PreToolUse", "codex", 3, "request_user_input"),
            HookSpec("PostToolUse", "codex", 3),
        ),
        requires_hook_trust=True,
        hook_names_home=True,
        strict_hooks_file=True,
        stop_reason_as_prompt=True,
        limits_in_transcript=True,
        native_telemetry=True,
        positional_prompt=True,
        idle_prompt_mark=IDLE_PROMPT_MARK,
        idle_on_screen=idle_on_screen,
        one_press_exits=frozenset({"C-c", "C-d"}),
        dialog_on_screen=dialog_on_screen,
        screen_prompt=screen_prompt,
        fleet_settings=(
            "notice.hide_rate_limit_model_nudge=true",
            "check_for_update_on_startup=false",
        ),
        first_context_file_only=True,
        sandbox_permissions=True,
        sandboxes_tools=True,
        redraws_while_asking=True,
        working_on_screen=working_on_screen,
        tool_shell_env="CODEX_THREAD_ID",
        # `codex exec resume <id> -c developer_instructions=Y` kept the thread's first X
        # and dropped Y; a later resume without -c still carried X (0.162.1).
        resume_keeps_system_prompt=True,
        # -c and -a as well: `fleet spawn` owns neither. `launch` owns both, and keeps
        # -c KEY=VALUE for Codex by its shape; its -a is --account (`launch --help`).
        value_options=frozenset(
            {
                "--model",
                "-m",
                "--config",
                "-c",
                "--add-dir",
                "--cd",
                "-C",
                "--image",
                "-i",
                "--profile",
                "-p",
                "--sandbox",
                "-s",
                "--ask-for-approval",
                "-a",
                "--enable",
                "--disable",
                "--local-provider",
                "--remote",
                "--remote-auth-token-env",
                "--color",
                "--output-schema",
                "--output-last-message",
                "-o",
            }
        ),
        switch_options=frozenset(
            {
                "--no-alt-screen",
                "--yolo",
                "--search",
                "--oss",
                "--json",
                "--approve-for-me",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "--no-daemon",
                "--strict-config",
                "--skip-git-repo-check",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
            }
        ),
    )

    @property
    def transcript_reader(self) -> AgentTranscript:
        # Read when Remote asks, never on a hook: the reader and rich's cell widths load then.
        from aisquare.core.agent_adapters.codex_transcript import CodexTranscript

        return CodexTranscript()

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        return config_home(self, home, env, explicit)

    def hooks_switch(
        self, home: Path, settings_path: Path, args: Sequence[str] = ()
    ) -> HooksSwitch | None:
        """``hooks = false`` under ``[features]``, or its deprecated alias ``codex_hooks``,
        in ``config.toml``, in the profile ``-p`` layers on it, or in ``args`` (``-c
        features.hooks=false``, ``--disable hooks``), a later layer winning and the
        canonical key winning over the alias wherever either is set, as Codex 0.162.1
        merges them."""
        user = home / "config.toml"
        layers: list[tuple[dict[str, Any], Path | None, str]] = [(_toml_features(user), user, "")]
        profile = next(reversed(option_values(args, "-p", "--profile")), None)
        if profile and Path(profile).name == profile and profile not in {".", ".."}:
            selected = home / f"{profile}.config.toml"
            layers.append((_toml_features(selected), selected, ""))
        for item in option_occurrences(args, "-c", "--config", "--enable", "--disable"):
            option = item.prefix.rstrip("=") or args[item.index]
            spelled = f"{option} {item.value}"
            if option in {"--enable", "--disable"}:
                layers.append(({item.value: option == "--enable"}, None, spelled))
                continue
            key, separator, raw = item.value.partition("=")
            if separator and key.strip().startswith("features."):
                try:
                    value = tomllib.loads("value=" + raw)["value"]
                except (ValueError, RecursionError):
                    continue  # Codex reads it as a string, which no feature switch takes
                layers.append(({key.strip().removeprefix("features."): value}, None, spelled))
        decided: dict[str, tuple[bool, Path | None, str]] = {}
        for features, path, spelled in layers:
            for key in ("hooks", "codex_hooks"):
                if isinstance(features.get(key), bool):
                    decided[key] = (features[key], path, spelled)
        key = "hooks" if "hooks" in decided else "codex_hooks"
        if key not in decided or decided[key][0]:
            return None
        _, path, spelled = decided[key]
        if path is None:
            return HooksSwitch(f"`{spelled}`")
        return HooksSwitch(f"`{key} = false` under [features]", path)

    def decode_event(self, payload: Mapping[str, Any]) -> AgentEvent | None:
        """Codex's command hook protocol: one ``aisquare hook codex`` for every event,
        named by ``hook_event_name``. A payload without a session id, or with one past
        256 characters, is not one of ours.

        A ``PermissionRequest`` is ``attention`` only when the human answers it: Codex
        fires it for its automatic reviewer's requests too (``--approve-for-me``, no
        prompt in the pane) with the same payload, so the session's rollout says which
        (:func:`~aisquare.core.codex_accounts.approvals_reviewer`); one it cannot read
        rings the bell, as a request nobody answers is worse than a false one.
        Measured on 0.162.1: PreToolUse comes before it, and PostToolUse (allowed),
        Interrupt (Esc) or Stop after.

        A ``prompt`` and a ``stop`` carry the model and the reasoning effort the rollout's
        newest turn ran with (:func:`turn_settings`): Codex's hooks carry no effort, and its
        ``Stop`` no model (0.162.1). A model the payload names wins."""
        native = payload_text(payload, "session_id")
        event = payload_text(payload, "hook_event_name")
        kind = _EVENTS.get(event or "")
        if not native or len(native) > 256 or kind is None:
            return None
        if event == "PreToolUse" and payload.get("tool_name") != "request_user_input":
            return None
        transcript = payload_text(payload, "transcript_path")
        if (
            event == "PermissionRequest"
            and transcript is not None
            and approvals_reviewer(Path(transcript)) == REVIEWERS["auto"]
        ):
            return None
        asks = "your approval" if event == "PermissionRequest" else "user input"
        model, effort = payload_text(payload, "model"), None
        if kind in ("prompt", "stop") and transcript is not None:
            ran, effort = turn_settings(Path(transcript))
            model = model or ran
        return AgentEvent(
            kind,  # type: ignore[arg-type]
            native,
            cwd=payload_cwd(payload),
            prompt=payload_text(payload, "prompt") if kind == "prompt" else None,
            transcript_path=payload_text(payload, "transcript_path"),
            model=model,
            effort=effort,
            source=payload_text(payload, "source") if kind == "session_start" else None,
            stop_hook_active=bool(payload.get("stop_hook_active")),
            turn_id=payload_text(payload, "turn_id"),
            message=f"{self.label} needs {asks}" if kind == "attention" else None,
        )

    def encode_reply(self, kind: EventKind, reply: str | None) -> str:
        """Context is plain stdout; a Stop that keeps the turn going is the ``block``
        decision, as JSON on exit 0."""
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
    ) -> harness.ModelResolution:
        from aisquare.core.agent_adapters.native_models import resolve_model

        # Resolve preferences first; only the effective AISquare-owned value is
        # validated by model_args after native overrides have been applied.
        result = resolve_model(self.id, role, env=env, effort=effort)
        level = self.effort_alias(result.effort)
        notes = []
        if level != result.effort.strip().lower():
            notes.append(f"Codex maps {result.effort!r} to native reasoning effort {level!r}.")
        elif level and level not in REASONING_EFFORTS:
            notes.append(
                f"Codex reasoning effort {level!r} is not one AISquare knows "
                f"({', '.join(REASONING_EFFORTS)}); it is passed on for Codex to validate."
            )
        return result.model_copy(update={"effort": level, "notes": notes})

    @staticmethod
    def effort_alias(effort: str) -> str:
        normalized = effort.strip().lower()
        return EFFORT_ALIASES.get(normalized, normalized)

    def native_choice(self, args: Sequence[str]) -> NativeChoice:
        """``-c model=…``/``-c model_reasoning_effort=…``, and ``--model``/``-m`` over both.
        The effort is a native config value, Codex's to validate: no ``effort_index``."""
        model = effort = None
        for item in option_occurrences(args, "-c", "--config"):
            parsed = config_assignment(item.value)
            if parsed is None:
                continue
            key, native = parsed
            if key == "model":
                model = native
            elif key == "model_reasoning_effort":
                effort = native
        models = option_values(args, "--model", "-m")
        return NativeChoice(models[-1] if models else model, effort)

    def native_args(self, args: list[str]) -> list[str]:
        def config(value: str) -> str:
            parsed = config_assignment(value)
            if parsed is not None:
                key, effort = parsed
                if (
                    key == "model_reasoning_effort"
                    and self.effort_alias(effort) != effort.strip().lower()
                ):
                    return key + '="' + self.effort_alias(effort) + '"'
                if key in {"model", "model_reasoning_effort"}:
                    return key + "=" + value.partition("=")[2]
            return value

        return rewrite_option_values(args, config, "-c", "--config")

    def mcp_args(
        self,
        executable: str,
        args: list[str],
        env_vars: list[str] | None = None,
    ) -> list[str]:
        return [
            "-c",
            f"mcp_servers.aisquare.command={json.dumps(executable)}",
            "-c",
            f"mcp_servers.aisquare.args={json.dumps(args)}",
            "-c",
            f"mcp_servers.aisquare.env_vars={json.dumps(env_vars or [])}",
            "-c",
            "mcp_servers.aisquare.required=false",
        ]

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
        """Every tool feature off (:data:`TOOL_FEATURES`, ``web_search="disabled"``), then
        the server as ``-c mcp_servers.<server>.*``, :meth:`mcp_args`' shape, with
        ``default_tools_approval_mode="approve"``, and every other server its config layers
        name for this launch switched off. Measured on 0.162.1 through these arguments: the
        request offered the server's tools, ``request_user_input`` and the MCP-resource
        readers, and ``exec`` ran a call of the server's tool unasked, which without that key
        it declined ("MCP tool call requires approval, but approval policy is never").

        Codex has no ``--strict-mcp-config``: an operator's ``mcp_servers`` table loads beside
        the server (a ``-c mcp_servers={…}`` inline table merges into it, measured), so each
        server :func:`_mcp_servers` finds in this launch's layers gets ``-c mcp_servers.
        <name>.enabled=false`` (measured: with one in ``home``'s ``config.toml`` and one in a
        trusted project's ``.codex/config.toml``, the request carried the server's tools
        alone). Only the layers' own: ``enabled=false`` on a name no layer defines makes
        Codex refuse its config ("invalid transport", measured). A Codex MCP server starts
        with ``HOME``, ``LC_CTYPE``, ``PATH`` and its ``env`` table alone (measured), so the
        names in ``inherit`` go in as its ``env_vars``, which hands it the session's values.
        ``ValueError`` for a server name, the server's or an operator's, that is not a bare
        TOML key, which a ``-c`` key cannot hold."""
        others = [name for name in _mcp_servers(home, launch_args, cwd) if name != server]
        for name in (server, *others):
            if not _BARE_KEY.fullmatch(name):
                raise ValueError(
                    f"an MCP server's name in a Codex -c key is a bare key, not {name!r}"
                )
        table = f"mcp_servers.{server}"
        pairs = ", ".join(f"{json.dumps(name)} = {_toml_string(v)}" for name, v in env.items())
        return [
            *(arg for feature in TOOL_FEATURES for arg in ("--disable", feature)),
            "-c",
            'web_search="disabled"',
            "-c",
            f"{table}.command={_toml_string(command)}",
            "-c",
            f"{table}.args=[{', '.join(map(_toml_string, args))}]",
            "-c",
            f"{table}.env={{{pairs}}}",
            "-c",
            f"{table}.env_vars=[{', '.join(map(_toml_string, inherit))}]",
            "-c",
            f'{table}.default_tools_approval_mode="approve"',
            *(arg for name in others for arg in ("-c", f"mcp_servers.{name}.enabled=false")),
        ]

    def context_files(self, home: Path) -> tuple[Path, ...]:
        return (home / "AGENTS.override.md", home / "AGENTS.md")

    def browser_tools(self, home: Path) -> list[str] | None:
        """Codex's own browser, as ``config.toml`` in ``home`` has it: the ``browser_use``
        feature (stable and on unless ``[features] browser_use = false``, 0.162.1) and each
        enabled ``browser@<marketplace>`` plugin (``browser@openai-bundled``)."""
        config = _read_toml(home / "config.toml")
        features = config.get("features")
        found = []
        if not (isinstance(features, dict) and features.get("browser_use") is False):
            found.append("the browser_use feature")
        plugins = config.get("plugins")
        for plugin_id, table in plugins.items() if isinstance(plugins, dict) else ():
            if str(plugin_id).partition("@")[0] != "browser":
                continue
            if isinstance(table, dict) and table.get("enabled", True) is not False:
                found.append(f"the {plugin_id} plugin")
        return found

    def validate(self, model: str | None, effort: str | None) -> None:
        self.reasoning_effort(effort)

    def role_defaults(self, role: str, *, binary: str, args: Sequence[str]) -> harness.RoleDefaults:
        """None: the ui-tester's browser is Codex's ``browser_use`` feature (stable and on
        in 0.162.1) and its bundled browser plugin, read by ``aisquare doctor``
        (:meth:`browser_tools`), not a flag; Claude Code's ``--chrome`` is not Codex's."""
        return harness.RoleDefaults()

    def model_args(self, model: str | None, effort: str | None) -> list[str]:
        effort = self.reasoning_effort(effort)
        return (["--model", model] if model else []) + (
            ["-c", f'model_reasoning_effort="{effort}"'] if effort else []
        )

    @staticmethod
    def reasoning_effort(effort: str | None) -> str | None:
        """The level for ``model_reasoning_effort``: a known one, or any level-shaped name.

        Levels depend on the model (Codex's config reference), so a name AISquare does
        not know is Codex's to accept or refuse. One that is not a name at all cannot
        be one and would not survive the TOML value it is written into.
        """
        if effort is None or not effort.strip():
            return None
        normalized = CodexAdapter.effort_alias(effort)
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", normalized):
            raise BadEffortError(
                f"Codex reasoning effort must be a level name such as "
                f"{', '.join(REASONING_EFFORTS)}, not {effort!r}"
            )
        return normalized

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
        """The fleet's sandbox and approval flags for ``role``, from its settings in Codex's
        words.

        With none chosen (``sandbox``, ``approval`` and ``approve_for_me`` unset), the
        role's built-in permission (``PermissionIntent.for_role``, :meth:`permission_args`):
        ``--approve-for-me`` (Codex's automatic reviewer, in the workspace-write sandbox),
        the analogue of Claude Code's ``auto`` mode, and for a reviewer's seats the
        read-only sandbox with that same reviewer. Codex refuses ``--approve-for-me``
        beside ``--sandbox`` or ``--ask-for-approval``, so a chosen sandbox or policy runs
        without it (``approve_for_me = true`` beside one is refused). A chosen sandbox is
        the store's profile for it (:meth:`store_access`; ``danger-full-access`` is
        ``--sandbox``), and ``on-request`` without ``--approve-for-me`` is the human's to
        answer (``approvals_reviewer="user"``). What the ``native`` arguments already
        choose is not given a second time.
        """
        # Native modes are deliberately distinct from Claude's approval vocabulary.
        if sandbox is not None and permission_mode is not None:
            raise ValueError("Use --sandbox for Codex, without --permission-mode")
        sandbox = sandbox if sandbox is not None else permission_mode
        if sandbox is not None and sandbox not in {
            "",
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise ValueError(
                "Codex permission mode must be read-only, workspace-write, or "
                "danger-full-access; Claude permission modes cannot be reused"
            )
        if approval is not None and approval not in APPROVAL_POLICIES:
            raise ValueError(
                f"Codex approval policy must be {' or '.join(APPROVAL_POLICIES)}, not {approval!r}"
            )
        if approve_for_me is None and sandbox is None and approval is None:
            return self.permission_args(PermissionIntent.for_role(role), role, native)
        reviewer = harness.base_role(role) == "reviewer"
        native_sandbox, native_approval = _native_approvals(native)
        if approve_for_me:
            clash = (
                f"--sandbox {sandbox}"
                if sandbox not in (None, "", "workspace-write")
                else f"--ask-for-approval {approval}"
                if approval is not None
                else native_sandbox or native_approval
            )
            if clash is not None:
                raise ValueError(
                    f"Codex --approve-for-me reviews approvals in the workspace-write sandbox "
                    f"and cannot run with {clash}; set fleet.roles.{role}.approve_for_me = "
                    "false to use it"
                )
            return ["--approve-for-me", *self._sandbox("workspace", native)]
        if sandbox is None:
            sandbox = "read-only" if reviewer else "workspace-write"
        if approval is None and reviewer:
            approval = "on-request"
        return [
            *(["--ask-for-approval", approval] if approval and native_approval is None else []),
            *(self._reviewer("ask", native) if approval != "never" else []),
            *(self._sandbox(_SCOPES.get(sandbox, "full"), native) if sandbox else []),
        ]

    def permission_args(
        self, intent: PermissionIntent, role: str, native: Sequence[str] = ()
    ) -> list[str]:
        """The write scope is the sandbox, the approvals who answers what it does not
        allow (measured on 0.162.1):

        - ``read_only`` and ``workspace`` are the store's profile (:meth:`store_access`),
          never ``--sandbox``, which would replace it; ``full`` is ``--sandbox
          danger-full-access``.
        - ``auto`` is Codex's automatic reviewer: ``--approve-for-me`` in the workspace,
          ``on-request`` with ``approvals_reviewer="auto_review"`` read-only (a reviewer's
          seats); ``full:auto`` is refused, as ``--approve-for-me`` runs only in
          workspace-write.
        - ``ask`` is ``on-request`` with ``approvals_reviewer="user"``: the human answers,
          on a resumed thread too, which keeps the reviewer it ran under otherwise, and
          whatever config.toml names.
        - ``never`` is ``--ask-for-approval never``.

        What ``native`` already chooses wins and is not given twice: ``--approve-for-me``
        cannot sit beside a native sandbox or policy (Codex refuses the launch), so it is
        left to them, and a native sandbox or reviewer is the operator's
        (:meth:`_sandbox`)."""
        if intent.approvals == "auto" and intent.write == "full":
            raise ValueError(
                "Codex reviews approvals automatically only in the workspace-write sandbox; "
                "use full:ask or full:never"
            )
        native_sandbox, native_approval = _native_approvals(native)
        sandbox = self._sandbox(intent.write, native)
        if intent.approvals == "auto" and intent.write == "workspace":
            return sandbox if native_sandbox or native_approval else ["--approve-for-me", *sandbox]
        approval = "never" if intent.approvals == "never" else "on-request"
        return [
            *(["--ask-for-approval", approval] if native_approval is None else []),
            *(self._reviewer(intent.approvals, native) if approval != "never" else []),
            *sandbox,
        ]

    def store_access(self, write: WriteScope, store: Path) -> list[str]:
        """``-c`` settings that run the session under the profile :data:`PERMISSION_PROFILE`:
        Codex's built-in one for ``write`` (``:read-only``, ``:workspace``) with ``store``
        read-only within it, all of it (:func:`store_read_only`), the more specific path
        winning over a writable one around it (the workspace, ``/tmp``). Each launch adds
        its broker's inbox, the one path there its sandbox may write (:meth:`broker_access`).
        ``full`` runs unsandboxed: nothing to add. Never a path denied reading (``none``):
        with one, Codex reads its own instructions through its sandbox helper, and where
        bwrap cannot bring up loopback (a GitHub runner) the session did not start (CI on
        ca6d43ea).

        Measured on 0.162.1, with the store outside ``/tmp``: from a sandboxed command
        the store's database reads and does not write (``attempt to write a readonly
        database``), and no file there can be made or changed; ``aisquare note`` and
        ``aisquare task claim`` succeed on their first, sandboxed attempt through the
        broker in the TUI and ``exec``. The profile holds beside ``--approve-for-me`` and
        over a ``sandbox_mode`` in config.toml, and gives way to ``--sandbox`` and to a pick
        in Codex's own ``/permissions``, which drops it until the next launch. Codex's
        guards in the workspace (``.git``, ``.codex``, ``.agents``) still hold.
        """
        if write == "full":
            return []
        profile = f"permissions.{PERMISSION_PROFILE}"
        return [
            "-c",
            f"default_permissions={json.dumps(PERMISSION_PROFILE)}",
            "-c",
            f"{profile}.extends={json.dumps(_BUILT_IN_PROFILES[write])}",
            "-c",
            f"{profile}.filesystem={_filesystem(store_read_only(store), ())}",
        ]

    def sandbox_writes(
        self, args: Sequence[str], cwd: Path, home: Path | None = None
    ) -> SandboxWrites | None:
        """What Codex's sandbox may write for a session with ``args`` in ``cwd``, read as
        Codex layers its settings: the system's :data:`SYSTEM_CONFIG`, ``config.toml`` in
        ``home`` (its Codex home; ``None`` is this shell's), the profile ``-p`` selects
        (``<name>.config.toml`` beside it), the administrator's :data:`MANAGED_CONFIG` over
        them, then ``args`` (review r13 of #113, finding 6; r14, finding 9; r15, finding
        4). ``None`` with no sandbox (``--sandbox danger-full-access``, ``sandbox_mode =
        "danger-full-access"``, the bypass flags). The sandbox is ``--sandbox``, else a
        ``-c sandbox_mode``, else the store's profile in ``args`` (which holds over a
        ``sandbox_mode`` in the files,
        measured on 0.162.1), else the files' ``sandbox_mode``. The workspace is ``-C``/
        ``--cd``'s directory, else ``cwd``. Measured on 0.162.1: ``read-only`` (the store's
        profile over ``:read-only``, or ``--sandbox read-only``) writes nothing but the
        profile's ``write`` paths, ``/tmp`` included; ``workspace-write`` (over
        ``:workspace``, ``--sandbox workspace-write``, ``--approve-for-me``, or no sandbox
        named: Codex's own default may be it, so it is taken as it) writes the workspace,
        ``/tmp`` and ``$TMPDIR``, with each ``--add-dir`` and every layer's
        ``sandbox_workspace_write.writable_roots`` (a table key or an inline table: all of
        them, the more writable reading). The profile's ``read`` paths are read-only
        within its writable ones, the more specific winning."""
        if any(arg in ("--dangerously-bypass-approvals-and-sandbox", "--yolo") for arg in args):
            return None
        codex_home = home if home is not None else _codex_home(None)
        files = [_read_toml(SYSTEM_CONFIG), _read_toml(codex_home / "config.toml")]
        profile = next(reversed(option_values(args, "-p", "--profile")), None)
        if profile and Path(profile).name == profile and profile not in {".", ".."}:
            files.append(_read_toml(codex_home / f"{profile}.config.toml"))
        files.append(_read_toml(MANAGED_CONFIG))  # over the user's, the files' last word
        settings: dict[str, str] = {}
        for value in option_values(args, "-c", "--config"):
            parsed = config_assignment(value)
            if parsed is not None:
                settings[parsed[0]] = value.partition("=")[2].strip()
        modes = option_values(args, "--sandbox", "-s")
        mode = modes[-1] if modes else None
        if mode is None and "sandbox_mode" in settings:
            mode = settings["sandbox_mode"].strip().strip("\"'")
        table = _filesystem_table(settings.get(f"permissions.{PERMISSION_PROFILE}.filesystem"))
        if mode is None and "default_permissions" in settings:
            extends = settings.get(f"permissions.{PERMISSION_PROFILE}.extends", "")
            mode = "read-only" if "read-only" in extends else "workspace-write"
        if mode is None:
            for layer in reversed(files):
                if isinstance(layer.get("sandbox_mode"), str):
                    mode = layer["sandbox_mode"]
                    break
        if mode == "danger-full-access":
            return None
        dirs = option_values(args, "-C", "--cd")
        workspace = (cwd / dirs[-1]) if dirs else cwd
        written = [Path(path) for path, at in table.items() if at == "write"]
        read = tuple(Path(path) for path, at in table.items() if at == "read")
        temporary = [
            Path("/tmp"),
            *([Path(os.environ["TMPDIR"])] if os.environ.get("TMPDIR") else []),
        ]
        if mode == "read-only":
            return SandboxWrites(tuple(written), read, None, ())
        listed: list[object] = []
        for layer in files:
            nested = layer.get("sandbox_workspace_write")
            if isinstance(nested, dict) and isinstance(nested.get("writable_roots"), list):
                listed += nested["writable_roots"]
        for key, wrapped in (
            ("sandbox_workspace_write.writable_roots", "v = {}"),
            ("sandbox_workspace_write", "v = {}"),
        ):
            if key not in settings:
                continue
            with suppress(tomllib.TOMLDecodeError, RecursionError, TypeError):
                found = tomllib.loads(wrapped.format(settings[key]))["v"]
                if isinstance(found, dict):
                    found = found.get("writable_roots", [])
                if isinstance(found, list):
                    listed += found
        added = [cwd / d for d in option_values(args, "--add-dir")]
        # `~olduser` names no home here: read as written, never a RuntimeError, which
        # cost the launch its store profile through the broker's catch-all (r14, 7).
        added += [cwd / expand_user(Path(item)) for item in listed if isinstance(item, str)]
        roots = [workspace, *temporary, *added, *written]
        return SandboxWrites(tuple(roots), read, workspace, tuple(temporary))

    def broker_access(
        self,
        args: Sequence[str],
        store: Path,
        inbox: Path,
        *,
        protect: Sequence[Path] = (),
        writable: Sequence[Path] = (),
    ) -> list[str] | None:
        """``args`` with every ``-c permissions.aisquare.filesystem=…`` (the store's
        profile, :meth:`store_access`) replaced by the store and ``protect`` read-only and
        ``inbox`` and ``writable`` writable: what the profile holds is this rule's,
        whatever a recorded launch spec said (one from before it had the store writable).
        ``None`` when ``args`` set no such table: the session runs unsandboxed or in a
        sandbox the operator chose. One ``-c`` cannot add a path to the table: Codex
        splits a ``-c`` key on every dot, and a home's path has them (``~/.aisquare``).

        Measured on 0.162.1: a ``read`` path inside the writable workspace (or ``/tmp``)
        refuses every write under it, a path that does not exist yet included, which
        then cannot be made; nothing is left on the host where it was absent. Codex
        itself keeps ``.git`` and ``.agents`` read-only in the workspace."""
        key = f"permissions.{PERMISSION_PROFILE}.filesystem"
        table = _filesystem((*store_read_only(store), *protect), (inbox, *writable))
        found = False

        def rewrite(value: str) -> str:
            nonlocal found
            assigned, separator, _ = value.partition("=")
            if not separator or assigned.strip() != key:
                return value
            found = True
            return f"{key}={table}"

        rewritten = rewrite_option_values(args, rewrite, "-c", "--config")
        return rewritten if found else None

    def _sandbox(self, write: WriteScope, native: Sequence[str]) -> list[str]:
        """The sandbox for ``write``, unless ``native`` chose one: a native ``--sandbox``
        replaces any profile (the store is then the operator's to reach) and a sandbox
        setting (``-c sandbox_mode=…``) cannot sit beside one, while ``--approve-for-me``
        runs in the workspace's, the store with it."""
        chosen, _ = _native_approvals(native)
        if _native_setting(native, _SANDBOX_SETTINGS):
            return []
        if chosen == "--approve-for-me":
            return self.store_access("workspace", store_dir())
        if chosen is not None:
            return []
        if write == "full":
            return ["--sandbox", _SANDBOXES["full"]]
        return self.store_access(write, store_dir())

    @staticmethod
    def _reviewer(approvals: str, native: Sequence[str]) -> list[str]:
        """``approvals_reviewer`` for ``ask`` or ``auto``, unless ``native`` chose one, or
        chose a switch that settles it (``--approve-for-me``, ``--yolo``)."""
        chosen, _ = _native_approvals(native)
        if chosen in _APPROVAL_SWITCHES or _native_setting(native, ("approvals_reviewer",)):
            return []
        return ["-c", f"approvals_reviewer={json.dumps(REVIEWERS[approvals])}"]

    def label_args(self, label: str) -> list[str]:
        return []

    def resume_args(self, native_session_id: str, transcript_path: Path | None) -> list[str]:
        """``codex resume <SESSION_ID>``: the thread by its id, any options after it."""
        return ["resume", native_session_id]

    def fork_args(self, native_session_id: str, transcript_path: Path | None) -> ForkArgs | None:
        """``codex fork <SESSION_ID>``: measured on 0.162.1, ``codex exec fork <id> "…"`` wrote
        a new rollout, a new thread id, whose request carried the source's user turns. The
        new id comes from its first hook. A thread forks only within its own home, as it
        resumes (``resumes_across_homes`` is False)."""
        return ForkArgs(("fork", native_session_id), mints_id=False)

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        """``codex queue --thread <id> --message=<text>``: measured on 0.162.1 against an
        interactive TUI session busy with a turn, the message ran as its next turn. The
        text is joined to its option, as ``--message <text>`` parses one that starts with
        ``-`` (``--no-verify was wrong``, ``- [ ] step``) as an option of its own and the
        queue exits with "unexpected argument" (measured)."""
        return ["queue", "--thread", native_session_id, f"--message={message}"]

    def disable_native_teams(self) -> tuple[list[str], dict[str, str]]:
        return ["-c", "agents.enabled=false"], {}

    def unattended_env(self) -> Mapping[str, str]:
        """None: the dialogs Codex opens in a running pane are kept out by
        ``capabilities.fleet_settings`` (its model-switch offer and update notice)."""
        return MappingProxyType({})

    def system_prompt_args(
        self, block: Path, text: str, args: Sequence[str], home: Path
    ) -> list[str] | None:
        """``-c developer_instructions=<text>``: measured on 0.162.1 with a loopback provider
        (``exec`` and the TUI alike), the value is the first part of the request's second
        developer message, after Codex's own base prompt and before its skills list.
        ``model_instructions_file`` would REPLACE that base prompt, so it is not the seam.

        The ``-c`` replaces the operator's own ``developer_instructions`` (measured: theirs
        was then absent from the request), so the value is theirs, from ``home``'s config
        layers (:func:`_developer_instructions`), a blank line, then ``text``. ``None`` where
        ``args`` set ``developer_instructions`` themselves. A thread keeps the value of its
        first turn across ``codex resume`` and ignores a new one
        (``capabilities.resume_keeps_system_prompt``)."""
        for item in option_occurrences(args, "-c", "--config"):
            parsed = config_assignment(item.value)
            if parsed is not None and parsed[0] == "developer_instructions":
                return None
        own = _developer_instructions(home, args)
        value = f"{own.rstrip()}\n\n{text}" if own and own.strip() else text
        return ["-c", f"developer_instructions={_toml_string(value)}"]

    def headless_call(self, schema: Path, out: Path, model: str | None) -> list[str] | None:
        """``exec --ephemeral --skip-git-repo-check --sandbox read-only -c features.hooks=false
        --output-schema <schema> -o <out> [-m <model>] -``, measured on 0.162.1 with a
        loopback provider: the request carries the schema as its strict ``json_schema``
        text format, ``out`` holds the final JSON, no rollout is written, and ``-`` reads
        the prompt from stdin. ``features.hooks=false`` because the hooks a manager slot
        trusted fire under ``exec`` (``--ignore-user-config`` does not stop them), and with
        it none did."""
        return [
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-c",
            "features.hooks=false",
            "--output-schema",
            str(schema),
            "-o",
            str(out),
            *(["-m", model] if model else []),
            "-",
        ]

    def headless_answer(self, stdout: str, out: Path) -> object | None:
        """``out`` read as JSON: ``-o`` writes the final message there, which
        ``--output-schema`` made the JSON."""
        try:
            if not out.is_file():  # a FIFO would block the read
                return None
            answer: object = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        return answer

    def skill_dirs(self, home: Path, repo: Path | None) -> SkillDirs:
        """Measured on 0.162.1, the request's skills list named a ``SKILL.md`` from each of
        ``$HOME/.agents/skills``, ``$CODEX_HOME/skills``, ``<repo>/.agents/skills`` and
        ``<repo>/.codex/skills`` (and its own ``$CODEX_HOME/skills/.system``). The user's
        ``.agents/skills`` comes first: every Codex home reads it, so one export there
        reaches every account."""
        from aisquare.core import agents

        return SkillDirs(
            (agents.home() / ".agents" / "skills", home / "skills"),
            (repo / ".agents" / "skills", repo / ".codex" / "skills") if repo is not None else (),
        )

    def telemetry_args(self, endpoint: str, *, keep_out: Sequence[str] = ()) -> list[str]:
        """Codex's own OTLP/HTTP log exporter (asynchronous; no model routing or auth
        change), without the user's prompts. Its headers come from the launch environment:
        0.162.1's exporter sends OTLP's ``OTEL_EXPORTER_OTLP_LOGS_HEADERS`` (measured).
        ``keep_out`` is the tools' shells' ``shell_environment_policy.exclude``, which
        keeps a variable from every command the agent runs while Codex's own process and
        its exporter keep it (measured on 0.162.1)."""
        config = "otel.exporter.otlp-http"
        excluded = ["-c", f"shell_environment_policy.exclude={json.dumps(list(keep_out))}"]
        return [
            "-c",
            f"{config}.endpoint={json.dumps(endpoint)}",
            "-c",
            f'{config}.protocol="json"',
            "-c",
            "otel.log_user_prompt=false",
            *(excluded if keep_out else []),
        ]


def _toml_features(path: Path) -> dict[str, Any]:
    """``path``'s ``[features]`` table; ``{}`` when there is none or it cannot be read."""
    try:
        if not path.is_file():  # a FIFO would block the read
            return {}
        with path.open("rb") as handle:
            features = tomllib.load(handle).get("features")
    except (OSError, ValueError, RecursionError):
        return {}
    return features if isinstance(features, dict) else {}


def turn_settings(path: Path) -> tuple[str | None, str | None]:
    """The model and the reasoning effort the rollout at ``path`` says its newest turn ran
    with: its newest ``turn_context``, which opens each turn (measured on 0.162.1:
    ``"model"``, and ``"effort"`` from ``model_reasoning_effort``, absent with none), read
    back from the end as :func:`~aisquare.core.codex_accounts.approvals_reviewer` reads who
    answers approvals. ``(None, None)`` when the rollout cannot be read or holds none; a
    field that is not a non-empty string is ``None``."""
    for record in _records_from_end(path):
        if record.get("type") != "turn_context":
            continue
        context = record.get("payload")
        if not isinstance(context, dict):
            return None, None
        model, effort = context.get("model"), context.get("effort")
        return (
            model if isinstance(model, str) and model else None,
            effort if isinstance(effort, str) and effort else None,
        )
    return None, None


def _toml_string(value: str) -> str:
    """``value`` as a TOML basic string, for a ``-c`` value: JSON's escapes are TOML's, and
    what JSON leaves bare and TOML refuses (DEL) is escaped too. Text beyond ASCII stays as
    written: JSON's ``\\ud83d\\ude00`` pairs for one character are no TOML escape."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _user_layers(home: Path, args: Sequence[str]) -> list[Path]:
    """The config files Codex layers for a session launched with ``args`` from ``home``,
    lowest first, but its project's: the system's :data:`SYSTEM_CONFIG`, ``config.toml`` in
    ``home``, the profile ``-p`` selects (``<name>.config.toml`` beside it), then the
    administrator's :data:`MANAGED_CONFIG`."""
    files = [SYSTEM_CONFIG, home / "config.toml"]
    profile = next(reversed(option_values(args, "-p", "--profile")), None)
    if profile and Path(profile).name == profile and profile not in {".", ".."}:
        files.append(home / f"{profile}.config.toml")
    files.append(MANAGED_CONFIG)
    return files


def _developer_instructions(home: Path, args: Sequence[str]) -> str | None:
    """The operator's own ``developer_instructions``, as Codex layers its config for a session
    launched with ``args`` from ``home`` (:func:`_user_layers`), the last that sets it a
    string winning; ``None`` when none does."""
    found = None
    for path in _user_layers(home, args):
        value = _read_toml(path).get("developer_instructions")
        if isinstance(value, str):
            found = value
    return found


def _project_layers(
    trust: Sequence[Mapping[str, Any]], args: Sequence[str], cwd: Path
) -> list[Path]:
    """The project's own config files Codex layers for a session in ``cwd`` (``-C``/``--cd``
    in ``args`` moved from it), outermost first, as measured on 0.162.1: only for a project
    ``trust`` (its user layers, :func:`_user_layers`) says ``trust_level = "trusted"`` of, by
    the real path of its root; in a git checkout ``.codex/config.toml`` in its root and in
    every folder from there down to ``cwd`` (none above the root, none for a trusted ``cwd``
    under an untrusted root), outside one in ``cwd`` alone (none in its parents)."""
    for value in option_values(args, "-C", "--cd"):
        cwd = cwd / value
    here = Path(os.path.realpath(cwd))
    walk = [here, *here.parents]
    root = next((folder for folder in walk if (folder / ".git").exists()), here)
    trusted = False
    for table in trust:
        projects = table.get("projects")
        entry = projects.get(str(root)) if isinstance(projects, dict) else None
        level = entry.get("trust_level") if isinstance(entry, dict) else None
        if isinstance(level, str):
            trusted = level == "trusted"
    if not trusted:
        return []
    return [folder / ".codex" / "config.toml" for folder in reversed(walk[: walk.index(root) + 1])]


def _mcp_servers(home: Path, args: Sequence[str], cwd: Path) -> list[str]:
    """The MCP servers Codex's config hands a session launched with ``args`` from ``home`` in
    ``cwd``, by name, in the order its layers first name them: every ``mcp_servers`` table of
    its user layers (:func:`_user_layers`), its project's (:func:`_project_layers`), then its
    ``-c`` overrides in ``args`` (``mcp_servers.<name>.…``, or a whole ``mcp_servers``
    inline table)."""
    user = [_read_toml(path) for path in _user_layers(home, args)]
    tables = [*user, *map(_read_toml, _project_layers(user, args, cwd))]
    names: dict[str, None] = {}
    for table in tables:
        servers = table.get("mcp_servers")
        if isinstance(servers, dict):
            names.update(
                dict.fromkeys(name for name, it in servers.items() if isinstance(it, dict))
            )
    for item in option_occurrences(args, "-c", "--config"):
        key, sep, value = item.value.partition("=")
        path = key.strip().split(".")
        if not sep or path[0] != "mcp_servers":
            continue
        if len(path) > 1:
            names[path[1]] = None
            continue
        with suppress(ValueError, RecursionError):
            servers = tomllib.loads(f"servers = {value}")["servers"]
            if isinstance(servers, dict):
                names.update(dict.fromkeys(servers))
    return list(names)


def _codex_home(config_dir: Path | None) -> Path:
    """The Codex home ``config_dir`` names, as ``agents connect codex`` resolves it; the one
    a session from this shell reads when ``None``."""
    from aisquare.core import agents

    spec = agents.spec(CodexAdapter.id, config_dir)
    return spec.home if spec is not None else Path(config_dir or ".")


def _enabled_install(home: Path) -> tuple[str, Path, dict[str, Any]] | None:
    """``(plugin id, installed copy, config.toml's table)`` for the first ``aisquare@
    <marketplace>`` that ``home`` enables and has a copy of, else ``None``."""
    config = _read_toml(home / "config.toml")
    tables = config.get("plugins")
    if not isinstance(tables, dict):
        return None
    for plugin_id, table in tables.items():
        name, _, marketplace = str(plugin_id).partition("@")
        if name != PLUGIN or not marketplace or Path(marketplace).name != marketplace:
            continue
        if not isinstance(table, dict) or table.get("enabled", True) is False:
            continue
        root = _installed_root(home / "plugins" / "cache" / marketplace / name)
        if root is not None:
            return str(plugin_id), root, config
    return None


def _plugin_trusted(config: Mapping[str, Any], plugin_id: str) -> bool:
    """Whether ``[hooks.state]`` trusts each hook the plugin's ``hooks.json`` holds: one
    group of one handler per event, keyed ``<plugin id>:hooks/hooks.json:<event in snake
    case>:0:0`` (tests/test_codex_plugin.py holds the file to that shape)."""
    hooks = config.get("hooks")
    states = hooks.get("state") if isinstance(hooks, dict) else None
    if not isinstance(states, dict):
        return False
    for spec in CodexAdapter.capabilities.hooks:
        event = re.sub(r"(?<!^)(?=[A-Z])", "_", spec.event).lower()
        state = states.get(f"{plugin_id}:{PLUGIN_HOOKS}:{event}:0:0")
        trusted = state.get("trusted_hash") if isinstance(state, dict) else None
        if not isinstance(trusted, str) or not trusted:
            return False
    return True


def _read_toml(path: Path) -> dict[str, Any]:
    """``path``'s TOML table, or ``{}``: absent, not a file, unreadable, or not TOML."""
    try:
        if not path.is_file():  # a FIFO would block the read
            return {}
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError, RecursionError):
        return {}


def _manifest(root: Path) -> Path:
    return root / ".codex-plugin" / "plugin.json"


def _installed_root(plugin_dir: Path) -> Path | None:
    """The installed copy under ``plugin_dir`` (one folder per version): the one whose
    manifest was written last, should an older one be left beside it."""
    try:
        roots = [root for root in plugin_dir.iterdir() if _manifest(root).is_file()]
    except OSError:
        return None
    try:
        return max(roots, key=lambda root: _manifest(root).stat().st_mtime, default=None)
    except OSError:
        return roots[0]


def _manifest_version(root: Path) -> str | None:
    try:
        data = json.loads(_manifest(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


def _native_approvals(args: Sequence[str]) -> tuple[str | None, str | None]:
    """The options in ``args`` (up to Codex's own ``--``) that choose the sandbox and
    the approval policy, as written; a switch such as ``--approve-for-me`` chooses both."""
    for arg in args:
        if arg == "--":
            break
        if arg in _APPROVAL_SWITCHES:
            return arg, arg
    sandbox = approval = None
    for option in ("--sandbox", "-s"):
        for value in option_values(args, option):
            sandbox = sandbox or f"{option} {value}"
    for option in ("--ask-for-approval", "-a"):
        for value in option_values(args, option):
            approval = approval or f"{option} {value}"
    return sandbox, approval


def _native_setting(args: Sequence[str], keys: Sequence[str]) -> str | None:
    """The first ``-c`` in ``args`` that sets one of ``keys`` or a key of its table, as
    written, else ``None``."""
    for item in option_occurrences(args, "-c", "--config"):
        key = item.value.partition("=")[0].strip()
        if any(key == name or key.startswith(f"{name}.") for name in keys):
            return item.value
    return None
