"""Codex CLI integration, tested on 0.164.0 and 0.162.1; native hooks keep the real terminal UI."""

import json
import math
import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from aisquare.core import harness
from aisquare.core.agent_adapters import codex_config
from aisquare.core.agent_adapters.types import (
    AgentCapabilities,
    AgentEvent,
    AgentScreen,
    AgentTranscript,
    BadEffortError,
    CompactCommand,
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
    PriceTable,
    SandboxWrites,
    ScreenPrompt,
    SkillDirs,
    TelemetryReader,
    Vocabulary,
    WriteScope,
    block_reply,
    command_entry,
    config_assignment,
    config_home,
    option_occurrences,
    option_values,
    package_version,
    payload_cwd,
    payload_text,
    rewrite_option_values,
    store_dir,
    store_read_only,
)
from aisquare.core.codex_accounts import CodexAccounts, _records_from_end, approvals_reviewer
from aisquare.core.config import FleetSettings
from aisquare.core.paths import expand_user
from aisquare.core.pricing import Price

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
#: ``worktrees`` is the TUI's (:data:`TUI_TOOLS_SERVER`): it switches that off on 0.162.1, and
#: on 0.164.0, where the feature is "removed" and always on, it is accepted and does nothing.
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
    "worktrees",
)
#: The MCP server Codex's TUI starts for itself, whose tools every TUI session is offered as
#: ``mcp__codex_worktrees`` (``create_worktree``, ``list_worktrees``, …; ``exec`` offers none).
#: A server of that name in the config "owns the TUI tools namespace", and the TUI starts
#: none: one switched off leaves it no tool, measured on 0.162.1 and on 0.164.0, where
#: ``--disable worktrees`` leaves it in and ``enabled=false`` alone is refused ("invalid
#: transport", the name being no layer's).
TUI_TOOLS_SERVER = "codex_worktrees"
#: Every tool feature switched off (:data:`TOOL_FEATURES`), and web search.
_TOOLS_OFF = (
    *(arg for feature in TOOL_FEATURES for arg in ("--disable", feature)),
    "-c",
    'web_search="disabled"',
)
#: A ``-c`` key splits on every dot, so an MCP server's name in one is a bare TOML key.
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
#: aisquare's Codex plugin (``plugins/codex``), by the name ``codex plugin add
#: aisquare@<marketplace>`` takes and ``config.toml`` keys it under, ``[plugins."aisquare@
#: aisquare-cli"]`` from this repository's marketplace (``.agents/plugins/marketplace.json``).
PLUGIN = "aisquare"
PLUGIN_MARKETPLACE = "aisquare-cli"
#: The last aisquare release with no Codex hooks: an install of it, or of one before it,
#: has none to write, and its ``agents refresh-hooks codex`` fails.
LAST_WITHOUT_HOOKS = "0.8.2"
#: Codex's own installer, as ``install.sh --agent codex`` runs it (its ``CODEX_INSTALLER_URL``).
INSTALL_COMMAND = "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
#: Codex's Homebrew cask, offered on macOS after its installer.
BREW_COMMAND = "brew install --cask codex"
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
        ``aisquare uninstall`` aims it (:func:`_aimed`). ``disable`` and ``uninstall`` are
        ``remove`` (:data:`_PLUGIN_VERBS`)."""
        found = _enabled_install(config_dir)
        plugin_id = found[0] if found is not None else f"{PLUGIN}@{PLUGIN_MARKETPLACE}"
        return _aimed(["codex", "plugin", _PLUGIN_VERBS.get(verb, verb), plugin_id], config_dir)


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

#: What a fleet window's Codex launches with (:meth:`CodexAdapter.fleet_settings`), each
#: unless a config layer of the operator's sets its key.
FLEET_SETTINGS = ("notice.hide_rate_limit_model_nudge=true", "check_for_update_on_startup=false")
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
#: OpenAI's Standard-tier list prices for the models Codex runs, read 2026-10-24 off
#: https://developers.openai.com/api/docs/pricing (:func:`_openai`).
PRICES_AS_OF = "2026-10-24"
PRICES_SOURCE = (
    "OpenAI API list prices, Standard tier: https://developers.openai.com/api/docs/pricing "
    "(read 2026-10-24)"
)
#: The page's short context: ≤272K input tokens.
_SHORT_CONTEXT = 272_000


def _openai(
    input: float,
    output: float,
    cached: float,
    long: tuple[float, float] | None = None,
    *,
    writes: bool = True,
) -> Price:
    """A row as the page states it, USD per million tokens: input, output and cached input,
    and the long context's input and output (over :data:`_SHORT_CONTEXT`; its cached input
    is the same multiple). A cache write is the 30-minute one, 1.25x input (the 12-hour one
    is 2.5x, a TTL Codex's records do not tell apart); ``writes=False`` where the page lists
    none (``-``, or no such column), and a request that wrote to the cache is unpriced."""
    long_input, long_output = long if long is not None else (None, None)
    return Price(
        input,
        output,
        cache_read=cached / input,
        cache_write=1.25 if writes else None,
        long_prompt_over=_SHORT_CONTEXT if long is not None else None,
        long_input=long_input,
        long_output=long_output,
    )


PRICES: Mapping[str, Price] = MappingProxyType(
    {
        "gpt-6.1-sol": _openai(2.00, 10.00, 0.10, (4.00, 15.00)),
        "gpt-6-luna": _openai(0.10, 0.50, 0.01, (0.20, 0.75)),
        "gpt-6-sol": _openai(2.00, 10.00, 0.20, (4.00, 15.00)),
        "gpt-6-astra": _openai(10.00, 50.00, 1.00, (20.00, 75.00)),
        "gpt-5.6-sol": _openai(4.00, 20.00, 0.40, (8.00, 30.00)),
        "gpt-5.6-terra": _openai(2.00, 12.00, 0.20, (4.00, 18.00)),
        "gpt-5.6-luna": _openai(0.20, 1.20, 0.02, (0.40, 1.80)),
        "gpt-5.5": _openai(5.00, 30.00, 0.50, (10.00, 45.00), writes=False),
        "gpt-5.4": _openai(2.50, 15.00, 0.25, (5.00, 22.50), writes=False),
        "gpt-5.3-codex": _openai(1.75, 14.00, 0.175, writes=False),
    }
)
#: A dated snapshot's suffix (``gpt-5.4-2026-03-05``), priced as its model.
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def canonical_model(model: str) -> str:
    """``model`` as :data:`PRICES` keys it: lower case, no provider prefix (``openai/``), no
    snapshot date."""
    return _SNAPSHOT.sub("", model.strip().lower().rpartition("/")[2])


PRICE_TABLE = PriceTable(PRICES_AS_OF, PRICES_SOURCE, PRICES, canonical_model)


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
    #: Its own installer first (the layout the Accounts page reads first), npm last.
    install_routes: tuple[InstallRoute, ...] = (
        InstallRoute("installer", INSTALL_COMMAND),
        InstallRoute("Homebrew", BREW_COMMAND, frozenset({"darwin"})),
        InstallRoute("npm", install_hint),
    )
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
    screen: AgentScreen | None = AgentScreen(
        idle_prompt_mark=IDLE_PROMPT_MARK,
        idle_on_screen=idle_on_screen,
        working_on_screen=working_on_screen,
        dialog_on_screen=dialog_on_screen,
        screen_prompt=screen_prompt,
    )
    capabilities = AgentCapabilities(
        approves_for_me=True,
        launch_inputs=(".codex", ".agents"),
        vocabulary=Vocabulary(
            env_prefixes=("OPENAI",),
            files=("auth.json",),
            flags=(
                "--sandbox",
                "--ask-for-approval",
                "--approve-for-me",
                "--add-dir",
                "--output-schema",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "--skip-git-repo-check",
                "--ephemeral",
            ),
        ),
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
        hookless_through=LAST_WITHOUT_HOOKS,
        hook_names_home=True,
        strict_hooks_file=True,
        stop_reason_as_prompt=True,
        limits_in_transcript=True,
        native_telemetry=True,
        positional_prompt=True,
        # A bare `/compact` at its idle prompt compacts (a `compacted` rollout record, no
        # hook); `/compact <text>` runs as a prompt instead (measured on 0.164.0, #17).
        compact=CompactCommand("/compact", instructions=False),
        one_press_exits=frozenset({"C-c", "C-d"}),
        first_context_file_only=True,
        sandbox_permissions=True,
        sandboxes_tools=True,
        redraws_while_asking=True,
        tool_shell_env="CODEX_THREAD_ID",
        # `RUN env` in a `codex exec` tool shell: 0.162.1 set these beside CODEX_THREAD_ID and
        # CODEX_HOME, 0.164.0 (npm) CODEX_TOOL_CALL_ID and its two package variables too.
        tool_shell_vars=(
            "CODEX_SESSION_ID",
            "CODEX_TOOL_CALL_ID",
            "CODEX_CI",
            "CODEX_SANDBOX_NETWORK_DISABLED",
            "CODEX_PERMISSION_PROFILE",
            "CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS",
            "CODEX_VERSION",
            "CODEX_MANAGED_BY_NPM",
            "CODEX_MANAGED_PACKAGE_ROOT",
        ),
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
    def telemetry_reader(self) -> TelemetryReader:
        from aisquare.core.agent_adapters.codex_config import CodexTelemetry

        return CodexTelemetry()

    price_table: PriceTable | None = PRICE_TABLE

    @property
    def transcript_reader(self) -> AgentTranscript:
        # Loaded when asked: by Remote, and at a turn's end for its token usage (`usage`), where
        # the reader and rich's cell widths cost a Stop hook about 5 ms.
        from aisquare.core.agent_adapters.codex_transcript import CodexTranscript

        return CodexTranscript()

    def config_dir(self, home: Path, env: Mapping[str, str], explicit: Path | None = None) -> Path:
        return config_home(self, home, env, explicit)

    def layout_version(self, binary: str) -> str | None:
        """From the install layout: the standalone installer links ``…/releases/<version>-
        <target>/bin/codex``; npm's ``@openai/codex`` has a ``package.json`` above its
        ``bin``. Neither: ``None``."""
        try:
            resolved = Path(binary).resolve()
        except (OSError, RuntimeError):
            return None
        match = re.search(r"/releases/(\d+\.\d+\.\d+[^/-]*)", resolved.as_posix())
        if match:
            return match.group(1)
        return package_version(str(resolved))

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

    def hook_entry(self, spec: HookSpec, command: str) -> dict[str, Any]:
        """``{"type": "command", "command": …, "timeout": <seconds>}`` in a ``hooks.json``
        group (:func:`command_entry`), as Claude Code's ``settings.json`` writes one."""
        return command_entry(spec, command)

    def lasting_installs(self, home: Path) -> tuple[LastingInstall, ...]:
        """What ``home``'s ``config.toml`` keeps of aisquare's: each ``[mcp_servers.<name>]``
        whose ``command`` and ``args`` run aisquare, and the aisquare plugin, a ``[plugins.
        "aisquare@<marketplace>"]`` table without ``enabled = false``, each with the
        ``codex`` command that removes it (:func:`_aimed`). ``config.toml`` is Codex's own
        file, which a running Codex rewrites too (trust, plugins), so it is never edited
        here. A launch's ``-c mcp_servers.aisquare…`` (:meth:`mcp_args`) is written nowhere:
        there is nothing of it to find."""
        from aisquare.core import agents
        from aisquare.core.agent_adapters.lasting import fetches_aisquare, runs_aisquare

        path = agents.dir_identity(home / "config.toml")
        config = codex_config.read_layer(path)
        servers = config.get("mcp_servers")
        plugins = config.get("plugins")
        found = [
            LastingInstall(
                self.id,
                "mcp",
                str(name),
                path,
                home,
                _aimed(["codex", "mcp", "remove", str(name)], home),
                fetches=fetches_aisquare(spec),
            )
            for name, spec in (servers.items() if isinstance(servers, dict) else ())
            if runs_aisquare(spec)
        ]
        found += [
            LastingInstall(
                self.id,
                "plugin",
                str(plugin_id),
                path,
                home,
                _aimed(["codex", "plugin", "remove", str(plugin_id)], home),
                fetches=True,
            )
            for plugin_id, table in (plugins.items() if isinstance(plugins, dict) else ())
            if str(plugin_id).partition("@")[0] == PLUGIN
            and isinstance(table, dict)
            and table.get("enabled", True) is not False
        ]
        return tuple(found)

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
    ) -> LaunchPart:
        return LaunchPart(
            (
                "-c",
                f"mcp_servers.aisquare.command={json.dumps(executable)}",
                "-c",
                f"mcp_servers.aisquare.args={json.dumps(args)}",
                "-c",
                f"mcp_servers.aisquare.env_vars={json.dumps(env_vars or [])}",
                "-c",
                "mcp_servers.aisquare.required=false",
            )
        )

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
        TOML key, which a ``-c`` key cannot hold.

        Last, the TUI's own server (:data:`TUI_TOOLS_SERVER`) is named, with the server's
        command, and switched off: with the features above off, the TUI still offered its
        worktree tools beside the server's (SMOKE8 8b, 0.162.1 and 0.164.0)."""
        mine = (server, TUI_TOOLS_SERVER)
        others = [name for name in _mcp_servers(home, launch_args, cwd) if name not in mine]
        table = f"mcp_servers.{_bare_key(server)}"
        off = _servers_off(others)
        pairs = ", ".join(f"{json.dumps(name)} = {_toml_string(v)}" for name, v in env.items())
        return [
            *_TOOLS_OFF,
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
            *off,
            "-c",
            f"mcp_servers.{TUI_TOOLS_SERVER}.command={_toml_string(command)}",
            "-c",
            f"mcp_servers.{TUI_TOOLS_SERVER}.enabled=false",
        ]

    def instruction_files(self, home: Path, cwd: Path | None) -> tuple[Path, ...]:
        """Measured on 0.164.0 (SCOPE #113 main-274, N4, and again for the blank and empty
        files and a folder outside git): in ``$CODEX_HOME``, the first of
        ``AGENTS.override.md`` and ``AGENTS.md`` that reads and is not blank; then, from the
        git root (the nearest folder up from ``cwd`` holding ``.git``, the filesystem's root
        aside) down to ``cwd``, or in ``cwd`` alone outside a repository, each folder's
        ``AGENTS.override.md`` where it is there, an empty one included, which leaves that
        folder's ``AGENTS.md`` unread, else its ``AGENTS.md``. Nothing above the git root, and
        never a ``CLAUDE.md``. Codex's defaults: no ``project_root_markers`` or
        ``project_doc_fallback_filenames`` of the operator's, and at most
        ``project_doc_max_bytes`` (32 KiB) of the project's files. ``cwd`` ``None``: both
        of the config dir's, the override first."""
        own = tuple(home / name for name in _INSTRUCTIONS)
        if cwd is None:
            return own
        loaded = next((path for path in own if _not_blank(path)), None)
        return (*((loaded,) if loaded is not None else ()), *_project_instructions(cwd))

    def browser_tools(self, home: Path) -> list[str] | None:
        """Codex's own browser, as ``config.toml`` in ``home`` has it: the ``browser_use``
        feature (stable and on unless ``[features] browser_use = false``, 0.162.1) and each
        enabled ``browser@<marketplace>`` plugin (``browser@openai-bundled``)."""
        config = codex_config.read_layer(home / "config.toml")
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

    def first_prompts_since(self, home: Path, cwd: Path, since: datetime) -> list[str] | None:
        """Its rollouts' ``user`` messages (``codex_transcript.prompts_since``): Codex fires its
        first hook only with its first prompt, and none at all with its hooks untrusted, so
        its rollout is what says a first message ran."""
        from aisquare.core.agent_adapters.codex_transcript import prompts_since

        return prompts_since(home, cwd, since)

    def tool_policy(
        self,
        role: str,
        *,
        binary: str,
        args: Sequence[str],
        fleet: FleetSettings,
        effort: str | None = None,
    ) -> harness.ToolPolicy:
        """Withheld, with a note: Codex has no tool deny list and no skill-listing setting;
        what its tools may do is its sandbox's and approvals' (the role's ``permission``,
        :meth:`permission_args`)."""
        return harness.withheld_policy(
            role,
            binary,
            fleet,
            why=f"runs {self.label}, whose tools are its sandbox's and approvals'",
        )

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
            return list(self.permission_args(PermissionIntent.for_role(role), role, native).args)
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
    ) -> PermissionArgs:
        """:meth:`_permission_flags`: it reviews ``auto`` itself (``approves_for_me``)."""
        return PermissionArgs(tuple(self._permission_flags(intent, role, native)))

    def _permission_flags(
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
        Codex layers its settings (``codex_config.layers``): the system's config,
        ``config.toml`` in ``home`` (its Codex home; ``None`` is this shell's), the profile
        ``-p`` selects (``<name>.config.toml`` beside it), the administrator's managed config
        over them, then ``args`` (review r13 of #113, finding 6; r14, finding 9; r15, finding
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
        files = codex_config.layers(codex_home, args)  # the managed one the files' last word
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

    def label_args(self, label: str) -> LaunchPart:
        return LaunchPart()

    def resume_args(self, native_session_id: str, transcript_path: Path | None) -> list[str]:
        """``codex resume <SESSION_ID>``: the thread by its id, any options after it."""
        return ["resume", native_session_id]

    def fork_args(self, native_session_id: str, transcript_path: Path | None) -> ForkArgs | None:
        """``codex fork <SESSION_ID> -C .``: measured on 0.162.1, ``codex exec fork <id> "…"``
        wrote a new rollout, a new thread id, whose request carried the source's user turns.
        The new id comes from its first hook. A thread forks only within its own home, as it
        resumes (``resumes_across_homes`` is False).

        ``-C .`` is the fork's own directory, the one its window starts in (its worktree):
        started anywhere but the source's last cwd, a bare ``fork`` opens ``Working directory
        · fork``, whose highlight and Esc both run the fork in the SOURCE's tree, and waits
        there with no hook (SMOKE8 of #113). Measured on 0.162.1 and 0.164.0: with ``-C`` (a
        path or ``.``, before ``fork`` or after its id) neither opens it, even under a
        ``resume_cwd = "session"`` its "Always use session directory" saved, and the fork
        runs in that directory with the source's turns and its own line."""
        return ForkArgs(("fork", native_session_id, "-C", "."), mints_id=False)

    def queue_args(self, native_session_id: str, message: str) -> list[str] | None:
        """``codex queue --thread <id> --message=<text>``: measured on 0.162.1 against an
        interactive TUI session busy with a turn, the message ran as its next turn. The
        text is joined to its option, as ``--message <text>`` parses one that starts with
        ``-`` (``--no-verify was wrong``, ``- [ ] step``) as an option of its own and the
        queue exits with "unexpected argument" (measured)."""
        return ["queue", "--thread", native_session_id, f"--message={message}"]

    def disable_native_teams(self) -> LaunchPart:
        return LaunchPart(("-c", "agents.enabled=false"))

    def fleet_settings(self, home: Path, args: Sequence[str]) -> LaunchPart:
        """``-c`` for each of :data:`FLEET_SETTINGS` no config layer of the operator's sets
        (``codex_config.operator_sets``: their overrides in ``args``, the system and user
        config of ``home``, its ``-p`` profile, the administrator's managed config; a layer
        that cannot be read may set it, and is left to): ``notice.
        hide_rate_limit_model_nudge=true`` keeps its ``Switch to gpt-6-luna for lower credit
        usage?`` dialog, opened past a turn near the five-hour limit while its hooks say
        waiting, out of a pane the fleet types into (measured on 0.162.1), and
        ``check_for_update_on_startup=false`` its ``Update available`` dialog out of a
        starting pane: an npm-installed 0.162.1 opened it once 0.163.0 was out (CI on
        72a88fc0), its first choice, which an Enter picks, running ``npm install -g
        @openai/codex``. A ``-c`` is over the managed config too, so one the administrator
        set is left as they set it, with a note that says so (review r18 of #113, 14)."""
        given: list[str] = []
        notes: list[str] = []
        managed = codex_config.read_layer(codex_config.MANAGED_CONFIG)
        for setting in FLEET_SETTINGS:
            key, _, value = setting.partition("=")
            try:
                if codex_config.operator_sets(home, list(args), key):
                    if codex_config.sets(managed, key):
                        notes.append(
                            f"{self.label}'s {key} is left as the administrator's "
                            f"{codex_config.MANAGED_CONFIG} sets it, not {value}"
                        )
                    continue
            except codex_config.NativeConfigError:
                continue
            given += ["-c", setting]
        return LaunchPart(tuple(given), notes=tuple(notes))

    def settings_layer(
        self,
        settings: Mapping[str, Any],
        file: Path,
        *,
        home: Path,
        env: Mapping[str, str],
        cwd: Path,
    ) -> LaunchPart | None:
        """Its ``-c`` overrides, the top layer of its config, one a leaf (``a.b=<TOML>``):
        no file. Each value is the TOML Codex parses back as it was (:func:`_toml_value`),
        and ``ValueError`` refuses what a ``-c`` cannot say: a ``None`` (TOML has none) and
        a key that is not bare, which Codex would split on its dots into another path. None
        of its slots answers settings today; they spell their ``-c``."""
        return LaunchPart(
            tuple(
                arg
                for key, value in _leaves(settings)
                for arg in ("-c", f"{key}={_toml_value(value)}")
            )
        )

    def unattended_env(self) -> Mapping[str, str]:
        """None: the dialogs Codex opens in a running pane are kept out by
        :meth:`fleet_settings` (its model-switch offer and update notice)."""
        return MappingProxyType({})

    def system_prompt_args(
        self, block: Path, text: str, args: Sequence[str], home: Path, *, cwd: Path
    ) -> list[str] | None:
        """``-c developer_instructions=<text>``: measured on 0.162.1 with a loopback provider
        (``exec`` and the TUI alike), the value is the first part of the request's second
        developer message, after Codex's own base prompt and before its skills list.
        ``model_instructions_file`` would REPLACE that base prompt, so it is not the seam.

        The ``-c`` replaces the operator's own ``developer_instructions`` (measured: theirs
        was then absent from the request), so the value is theirs, from every config layer of
        a session from ``home`` in ``cwd``, a trusted project's included
        (:func:`_developer_instructions`; review r17 of #113, finding 7), a blank line, then
        ``text``. ``None`` where
        ``args`` set ``developer_instructions`` themselves. A thread keeps the value of its
        first turn across ``codex resume`` and ignores a new one
        (``capabilities.resume_keeps_system_prompt``)."""
        for item in option_occurrences(args, "-c", "--config"):
            parsed = config_assignment(item.value)
            if parsed is not None and parsed[0] == "developer_instructions":
                return None
        own = _developer_instructions(home, args, cwd)
        value = f"{own.rstrip()}\n\n{text}" if own and own.strip() else text
        return ["-c", f"developer_instructions={_toml_string(value)}"]

    def headless_call(
        self, schema: Path, out: Path, model: str | None, *, home: Path, cwd: Path
    ) -> list[str] | None:
        """``exec --ephemeral --skip-git-repo-check --sandbox read-only -c features.hooks=false
        <no tools> --output-schema <schema> -o <out> [-m <model>] -``, measured on 0.162.1
        with a loopback provider: the request carries the schema as its strict
        ``json_schema`` text format, ``out`` holds the final JSON, no rollout is written, and
        ``-`` reads the prompt from stdin. ``features.hooks=false`` because the hooks a
        manager slot trusted fire under ``exec`` (``--ignore-user-config`` does not stop
        them), and with it none did.

        No tools, as Claude Code's ``--tools ""`` call has none: its stdin frames an
        untrusted source (review r17 of #113, finding 4). Every tool feature is off and web
        search (:meth:`tools_only_args`' switches), and every MCP server the config layers
        name for a session from ``home`` in ``cwd`` is switched off (:func:`_mcp_servers`,
        :data:`TUI_TOOLS_SERVER` too where a layer names it). Measured on 0.162.1 and 0.164.0
        with an MCP server in ``home``'s ``config.toml``: without them the request offered
        ``exec_command``, the multi-agent and goal tools, ``web_search`` and the server's
        tool; with them, ``request_user_input`` alone, which no feature switches off (M11).
        ``ValueError`` for a server name that is not a bare TOML key, which a ``-c`` key
        cannot hold."""
        return [
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-c",
            "features.hooks=false",
            *_TOOLS_OFF,
            *_servers_off(_mcp_servers(home, [], cwd)),
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

    def telemetry_args(self, endpoint: str, *, keep_out: Sequence[str] = ()) -> LaunchPart:
        """Codex's own OTLP/HTTP log exporter (asynchronous; no model routing or auth
        change), without the user's prompts. Its headers come from the launch environment:
        0.162.1's exporter sends OTLP's ``OTEL_EXPORTER_OTLP_LOGS_HEADERS`` (measured).
        ``keep_out`` is the tools' shells' ``shell_environment_policy.exclude``, which
        keeps a variable from every command the agent runs while Codex's own process and
        its exporter keep it (measured on 0.162.1)."""
        config = "otel.exporter.otlp-http"
        excluded = ("-c", f"shell_environment_policy.exclude={json.dumps(list(keep_out))}")
        return LaunchPart(
            (
                "-c",
                f"{config}.endpoint={json.dumps(endpoint)}",
                "-c",
                f'{config}.protocol="json"',
                "-c",
                "otel.log_user_prompt=false",
                *(excluded if keep_out else ()),
            )
        )


#: The instruction files of one folder, in Codex's precedence: the override replaces the other.
_INSTRUCTIONS = ("AGENTS.override.md", "AGENTS.md")


def _not_blank(path: Path) -> bool:
    """Whether ``path`` reads as text that is not blank, as Codex reads its home's files."""
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


def _there(path: Path) -> bool:
    """Whether ``path`` is a file or a link, as Codex looks for a folder's instruction file
    (``symlink_metadata``): a folder of that name is passed over."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _project_instructions(cwd: Path) -> list[Path]:
    """The project's instruction files a session in ``cwd`` loads
    (:meth:`CodexAdapter.instruction_files`): one per folder from the git root down."""
    try:
        start = cwd.resolve()
    except (OSError, RuntimeError):
        start = cwd.absolute()
    ancestors = [start, *start.parents]
    # `.git` by `metadata`, a link followed; the filesystem's root, which has no parent, never.
    root = next((f for f in ancestors if f != f.parent and os.path.exists(f / ".git")), None)
    folders = reversed(ancestors[: ancestors.index(root) + 1]) if root is not None else [start]
    return [
        found
        for folder in folders
        if (found := next((folder / n for n in _INSTRUCTIONS if _there(folder / n)), None))
    ]


def _leaves(settings: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Each leaf of ``settings``, by its dotted key, as a ``-c`` override names it, an empty
    table one too. ``ValueError`` for a key that is not bare: Codex splits a ``-c`` key on
    every dot and reads no quotes, so ``my.server`` would name another path."""
    found: list[tuple[str, Any]] = []
    for key, value in settings.items():
        if not isinstance(key, str) or not _BARE_KEY.fullmatch(key):
            raise ValueError(f"a Codex -c key is bare keys joined by dots, not {key!r}")
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            found += _leaves(value, f"{dotted}.")
        else:
            found.append((dotted, value))
    return found


def _toml_value(value: object) -> str:
    """``value``, a leaf of a settings document, as the TOML value a ``-c`` override parses
    back as it was (Codex parses ``_x_ = <value>``, and takes one that does not parse as a
    bare string): a string as :func:`_toml_string`, a list as an array, a mapping as an
    inline table (an array of tables included). ``ValueError`` for ``None``, which TOML has
    no value for, and for what a JSON document does not hold."""
    if value is None:
        raise ValueError("a Codex -c value is TOML, which has no null")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ValueError(f"a TOML integer is 64-bit, not {value}")
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return repr(value) if math.isfinite(value) else ("inf" if value > 0 else "-inf")
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, Mapping):
        pairs = (f"{_inline_key(key)} = {_toml_value(item)}" for key, item in value.items())
        return "{" + ", ".join(pairs) + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"a Codex -c value is TOML, not {type(value).__name__}")


def _inline_key(key: object) -> str:
    """``key`` of an inline table: bare where it may be, else quoted (TOML parses this one,
    where Codex's own key splitting does not reach)."""
    if not isinstance(key, str):
        raise ValueError(f"a TOML key is a string, not {key!r}")
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


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


def _developer_instructions(home: Path, args: Sequence[str], cwd: Path) -> str | None:
    """The operator's own ``developer_instructions``, as Codex layers its config for a session
    launched with ``args`` from ``home`` in ``cwd`` (``codex_config.layers``), the last that
    sets it a string winning; ``None`` when none does."""
    found = None
    for table in codex_config.layers(home, args, cwd):
        value = table.get("developer_instructions")
        if isinstance(value, str):
            found = value
    return found


def _mcp_servers(home: Path, args: Sequence[str], cwd: Path) -> list[str]:
    """The MCP servers Codex's config hands a session launched with ``args`` from ``home`` in
    ``cwd``, by name, in the order its layers first name them: every ``mcp_servers`` table of
    its config layers (``codex_config.layers``), then its ``-c`` overrides in ``args``
    (``mcp_servers.<name>.…``, or a whole ``mcp_servers`` inline table)."""
    names: dict[str, None] = {}
    for table in codex_config.layers(home, args, cwd):
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


def _bare_key(name: str) -> str:
    """``name``, an MCP server's, which a ``-c`` key splits on every dot: ``ValueError`` for
    one that is not a bare TOML key."""
    if not _BARE_KEY.fullmatch(name):
        raise ValueError(f"an MCP server's name in a Codex -c key is a bare key, not {name!r}")
    return name


def _servers_off(names: Sequence[str]) -> list[str]:
    """``-c mcp_servers.<name>.enabled=false`` for each of ``names``, which must be ones a
    config layer defines (Codex refuses its config over one no layer does, "invalid
    transport"). ``ValueError`` for a name that is not a bare key (:func:`_bare_key`)."""
    return [arg for name in names for arg in ("-c", f"mcp_servers.{_bare_key(name)}.enabled=false")]


def _aimed(argv: list[str], home: Path) -> str:
    """``argv`` (a ``codex`` command) as one line that acts on Codex home ``home``.

    ``codex`` reads ``$CODEX_HOME``, else ``~/.codex``: nothing is added for the home this
    shell's ``codex`` already reads, ``env -u CODEX_HOME`` for ``~/.codex`` from a shell
    that names another, and the variable otherwise, as ``claude_installs.mcp_removal`` aims
    ``claude``. Native Windows has neither form, so there the variable it needs is said
    after it.
    """
    from aisquare.core import agents
    from aisquare.core.shell_line import command_line

    command = command_line(argv)
    ambient = os.environ.get(CodexAdapter.home_env, "").strip()
    default = agents.dir_identity(agents.home() / CodexAdapter.home_name)
    here = agents.dir_identity(Path(ambient)) if ambient else default
    there = agents.dir_identity(home)
    if there == here:
        return command
    if sys.platform == "win32":
        need = "unset" if there == default else f"set to {home}"
        return f"{command} (run it with {CodexAdapter.home_env} {need})"
    if there == default:
        return f"env -u {CodexAdapter.home_env} {command}"
    return f"{CodexAdapter.home_env}={command_line([str(home)])} {command}"


def _codex_home(config_dir: Path | None) -> Path:
    """The Codex home ``config_dir`` names, as ``agents connect codex`` resolves it; the one
    a session from this shell reads when ``None``."""
    from aisquare.core import agents

    spec = agents.spec(CodexAdapter.id, config_dir)
    return spec.home if spec is not None else Path(config_dir or ".")


def _enabled_install(home: Path) -> tuple[str, Path, dict[str, Any]] | None:
    """``(plugin id, installed copy, config.toml's table)`` for the first ``aisquare@
    <marketplace>`` that ``home`` enables and has a copy of, else ``None``."""
    config = codex_config.read_layer(home / "config.toml")
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
