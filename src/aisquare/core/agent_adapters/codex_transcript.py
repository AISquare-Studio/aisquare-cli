"""Codex's transcript, read for Remote: its session rollout
(``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl``, the ``transcript_path``
its hooks report). :class:`CodexTranscript` is Codex's answer to the transcript slot
(``AgentAdapter.transcript_reader``, ``types.AgentTranscript``).

A rollout is one record a line, ``{"timestamp", "type", "payload"}``. Measured on 0.162.1
with a loopback provider and the TUI under tmux (an approval asked and answered with ``1``
and with Esc, a ``request_user_input`` in Plan mode answered with a digit, a plan proposed):

- **The conversation** is the ``response_item`` records: a ``message`` (``role`` ``user``,
  ``assistant`` or ``developer``; ``content`` parts of ``input_text``/``output_text``), a
  call (``function_call`` with JSON ``arguments``, ``custom_tool_call`` with a freeform
  ``input``, ``local_shell_call`` with an ``action``), a call's result
  (``function_call_output``/``custom_tool_call_output``, paired by ``call_id``) and
  ``reasoning``. A ``user`` message that is Codex's own context (``<environment_context>``,
  ``<turn_aborted>``, AGENTS.md) is in one of its own tags held closed, as Claude Code's own
  records are.
  ``compacted`` records a compaction. The ``event_msg`` copies of these (``item_completed``,
  ``user_message``, ``agent_message``) are not read again.
- **A turn** opens with ``event_msg`` ``task_started`` and ends with ``task_complete``
  (its ``last_agent_message`` the agent's last words) or ``turn_aborted`` (``reason``
  ``interrupted``: an Esc, an approval or a question answered with Esc, after an ``aborted
  by user`` result for the call it stopped).
- **A model response** ends with a ``token_usage_record`` (0.162.1, written as the
  response completes) and an ``event_msg`` ``token_count`` (every version, written once
  its calls' results are in). The next response starts only once they are, so only the
  newest response can hold a call still waiting: the walk stops at the response before it.
- **An approval or a question waiting for the human leaves no record**: the call is in the
  rollout without its result until it is answered, as a call at work is. The board's
  attention (the PermissionRequest hook, and PreToolUse for ``request_user_input``) is what
  says a prompt is up, as Claude Code's Notification says it for a tool use.
- **The dialogs' keys**: an approval view's ``1. Yes, proceed``, ``2. Yes, and don't ask
  again…`` and ``3. No… (esc)`` take their digit, and Esc stops the turn; a question's
  options take their digit (one question: picked and submitted), Esc stops the turn.

**What a turn end leaves** (``[accounts] before_limit``'s safe point; measured on 0.164.0 with
a loopback provider and the TUI under tmux, ``tests/fixtures/agent_transcripts/
codex-background-0.164.0.jsonl``):

- **An exec still running.** An ``exec_command`` that outlives its ``yield_time_ms`` is
  answered ``Process running with session ID <n>`` (its result's header, above ``Output:``)
  and the turn can end with it running: the TUI then says ``1 background terminal running ·
  /ps to view · /stop to close``. When the process ends, by itself or by ``/stop`` (status
  ``failed``, exit 137), Codex writes an ``event_msg`` ``item_completed`` whose ``item`` is a
  ``CommandExecution`` with the call's ``id`` and ``process_id`` ``<n>``, at an idle prompt as
  well as in a turn; a ``write_stdin`` to session ``<n>`` answered ``Process exited with code
  <c>`` says it ended too (:func:`_open_execs`).
- **A turn begun.** The Stop hook runs with the rollout ending on the turn's last record:
  only its ``task_complete`` follows. A turn opens with ``task_started`` and every
  ``response_item`` is one at work; between turns Codex writes ``thread_settings_applied`` (a
  mode or setting changed) and that ``item_completed``, which are none (:func:`_turn_begun`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from aisquare.core.agent_adapters.transcripts import (
    DIM,
    MAX_LINE,
    OFF,
    TAIL_RECORDS,
    YOU,
    PendingTool,
    TranscriptTail,
    dim_note,
    first_question,
    head_records,
    kept_input,
    lines_backwards,
    one_per_line,
    parse_line,
    question_lines,
    record_time,
    speaker_line,
    wrap,
)
from aisquare.core.agent_adapters.types import (
    AnswerKey,
    AnswerKind,
    CallAsk,
    HoldsOpen,
    TurnsScan,
    UsageScan,
    executable_name,
    process_executable,
)
from aisquare.core.ledger import SessionLedger
from aisquare.core.shell_line import command_line

SPEAKER = "codex"

_CALLS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
"""The ``response_item`` types that call a tool, each paired with its result by ``call_id``."""
_RESULTS = frozenset({"function_call_output", "custom_tool_call_output"})
_RESPONSE_ENDS = frozenset({"token_count"})
"""The ``event_msg`` types that close a model response (and ``token_usage_record``)."""

_QUESTION_TOOL = "request_user_input"
"""Codex's tool for asking the human (Plan mode only, 0.162.1): ``questions`` of ``id``,
``header``, ``question`` and ``options`` of ``label`` and ``description``."""

_OWN_TAGS = (
    "environment_context",
    "turn_aborted",
    "user_action",
    "recommended_plugins",
    "user_shell_command",
    "subagent_notification",
    "skill",
    "user_instructions",
)
"""The tags Codex wraps the context it writes as a ``user`` message in. Measured in 0.162.1
rollouts: ``environment_context``, ``turn_aborted``, ``user_action`` (a ``/review``'s
results) and ``recommended_plugins``. Named beside them in its binary: ``user_shell_command``
(a ``!`` command), ``subagent_notification`` and ``skill``; ``user_instructions`` is an
earlier version's AGENTS.md. Any other tag is the human's (review r16 of #113, 1/8)."""
_OWN_TEXT = re.compile(rf"\s*<({'|'.join(_OWN_TAGS)})>")
"""How a ``user`` message Codex writes itself begins: one of :data:`_OWN_TAGS`, held closed
(:func:`_own`)."""
_INSTRUCTIONS = "# AGENTS.md instructions for "
"""How the AGENTS.md Codex hands the model as a ``user`` message begins."""

_PROPOSED_PLAN = re.compile(r"<proposed_plan>\s*(.*?)\s*</proposed_plan>", re.DOTALL)
"""A plan Codex proposes in Plan mode, inside its final message (0.162.1)."""

# Read off the bytes of a line too long to parse: the record's own keys, never text quoted
# in a string (a ``\"`` inside one never matches).
_RAW_TYPE = re.compile(rb'"type"\s*:\s*"([a-z_]+)"')
_RAW_CALL_ID = re.compile(rb'"call_id"\s*:\s*"([^"\\]{1,256})"')
_RAW_NAME = re.compile(rb'"name"\s*:\s*"([^"\\]{1,64})"')
_RAW_ROLE_ASSISTANT = re.compile(rb'"role"\s*:\s*"assistant"')
_RAW_ROLE_USER = re.compile(rb'"role"\s*:\s*"user"')
_RAW_TEXT = re.compile(rb'"text"\s*:\s*"([^"]{0,64})')
_RAW_HEAD = 1024
"""How much of a long line's start holds its ``type`` keys: the record's and its payload's
come before any content (``{"timestamp", "type", "payload": {"type", …}}``)."""

_ANSWERS: Mapping[AnswerKind, tuple[AnswerKey, ...]] = {
    "permission": (AnswerKey("1", ("1",)), AnswerKey("2", ("2",)), AnswerKey("No", ("Escape",))),
    "question": (AnswerKey("Cancel", ("Escape",)),),
}
"""Codex 0.162.1's approval view (``1. Yes, proceed``, ``2. Yes, and don't ask again``, Esc
for ``No``, which stops the turn) and question view (each option's digit, then Esc, which
stops the turn); it has no plan call to approve."""


class CodexTranscript:
    """Codex's answers to the transcript slot (``types.AgentTranscript``)."""

    speaker = SPEAKER

    def records(
        self, lines: Iterable[tuple[int, bytes]], ahead: Iterable[tuple[int, bytes]] = ()
    ) -> Iterator[tuple[int, dict[str, Any] | None]]:
        return one_per_line(lines)  # one whole record a line: none read ahead

    def tail_facts(self, file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
        return _rollout_tail(file, size, budget, written)

    def page_lines(self, record: dict[str, Any], width: int) -> list[str]:
        return _rollout_lines(record, width)

    def call_asks(self, tool: PendingTool) -> CallAsk:
        return "question" if tool.name == _QUESTION_TOOL else "tool"

    def call_mark(self, tool: PendingTool) -> str | None:
        """The command an approval view shows (``$ <cmd>``), or a question's words."""
        if tool.name == _QUESTION_TOOL:
            return first_question(tool.input)
        command = tool.input.get("cmd", tool.input.get("command"))
        if isinstance(command, list):
            command = " ".join(part for part in command if isinstance(part, str))
        return command if isinstance(command, str) and command else None

    def answer_keys(self, kind: AnswerKind, options: int = 0) -> tuple[AnswerKey, ...]:
        keys = _ANSWERS.get(kind, ())
        if kind != "question":
            return keys
        return (*(AnswerKey(str(n), (str(n),)) for n in range(1, options + 1)), *keys)

    def usage(self, file: Path, state: str | None) -> UsageScan:
        """Its ``token_usage_record`` lines (:mod:`codex_usage`)."""
        from aisquare.core.agent_adapters import codex_usage

        return codex_usage.usage(file, state)

    def ledger(
        self, file: Path, *, since: datetime | None, until: datetime | None
    ) -> SessionLedger:
        """The rollout's ``token_usage_record`` lines and the rest (:mod:`codex_ledger`)."""
        from aisquare.core.agent_adapters import codex_ledger

        return codex_ledger.read(file, since=since, until=until)

    def context_tokens(self, file: Path) -> int | None:
        from aisquare.core.agent_adapters import codex_ledger

        return codex_ledger.context_tokens(file)

    def compacted_since(self, file: Path, since: datetime) -> bool:
        from aisquare.core.agent_adapters import codex_ledger

        return codex_ledger.compacted_since(file, since)

    def reply_since(self, file: Path, since: datetime | None) -> str | None:
        return _rollout_reply(file, since)

    def held_open(self, args: Sequence[str]) -> HoldsOpen | None:
        """A process is Codex's when what it runs is (``types.process_executable``: its first
        word, or the script of its npm shim, ``node …/bin/codex``); a shell, tmux or another
        agent whose arguments merely mention codex is none (``None``).

        ``clients`` for every process of its managed app-server daemon, which runs the thread
        of a ``codex`` started with no arguments (0.164.0's default) and keeps its rollout open
        for minutes after that ``codex`` quit, a hold Codex itself keeps to
        (:meth:`server_note`): a TUI of Codex's that runs is named before it (SMOKE11, 5A).
        Measured on 0.164.0 (#47's reopen), a no-argument ``codex`` starts two, both run from
        ``<CODEX_HOME>/packages/app-server-daemon/releases/<version>/bin/codex`` and both
        living on after ``/exit``: ``app-server --listen unix://… --managed-daemon``, which
        holds the rollout, and ``app-server daemon pid-update-loop``, which holds none.
        ``session`` for a form that can be the daemon's client (:func:`_runs_as`): its TUI
        (no subcommand, a prompt, ``resume``, ``fork``, and ``--remote <ADDR>`` whatever
        else it is given) and ``agents`` and ``app-server proxy``, each measured connected to
        the daemon's socket. ``alone`` for any other, never the daemon's client: one that
        runs embedded (a fleet Codex's ``-c`` overrides, ``exec``, ``review``), which holds
        its rollout itself, and one that runs no thread of the daemon's (``app-server`` of its
        own, ``exec-server``, ``login`` and every other subcommand, ``execpolicy``,
        ``tcp-tunnel`` and ``cloud-tasks`` too). Each holds what it has open from its first
        prompt on for as long as its session lives (``lsof`` on 0.164.0,
        ``core.process_table``)."""
        from aisquare.core.agent_adapters.codex import CodexAdapter

        image = process_executable(args)
        if image is None or executable_name(image) != CodexAdapter.binary:
            return None
        if _DAEMON_DIR in image.replace("\\", "/"):
            return "clients"
        return _runs_as(args[args.index(image) + 1 :])

    def server_note(self, run: Sequence[str]) -> str:
        """Its managed app-server daemon's, measured on 0.164.0 (#47): the daemon holds what
        it has open, as Codex sees it (a ``codex resume`` of the thread it keeps sat on "This
        conversation is open in another app", and its ``R`` retried in vain), keeps a thread
        :data:`_DAEMON_KEEPS` after its TUI quit, and stops at ``codex app-server daemon
        stop`` on that account's home (which leaves its ``pid-update-loop`` running), run
        through ``run``."""
        stop = command_line([*run, "app-server", "daemon", "stop"])
        return (
            "it is Codex's background app-server, which keeps a thread open after its TUI quit "
            f"and lets go of it by itself {_DAEMON_KEEPS}: retry then, or stop it first with "
            f"`{stop}`"
        )

    def conversations(self, home: Path) -> Iterator[tuple[str, Path]]:
        """``sessions/YYYY/MM/DD/rollout-<local time>-<thread id>.jsonl``, by its name."""
        with suppress(OSError):
            for path in (home / "sessions").glob("*/*/*/rollout-*.jsonl"):
                if named := _ROLLOUT_NAME.match(path.name):
                    yield named.group(1), path

    def opening(self, file: Path) -> tuple[str | None, str | None]:
        """The ``cwd`` its ``session_meta`` names, and the first ``user`` message that is not
        Codex's own context (:func:`_own`: AGENTS.md, ``<environment_context>``…)."""
        cwd: str | None = None
        first: str | None = None
        for record in head_records(file):
            payload = _payload(record)
            if cwd is None and record.get("type") == "session_meta":
                cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
            elif (
                first is None
                and record.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                text = _message_text(payload)
                first = text if text.strip() and not _own(text.lstrip()) else None
            if cwd is not None and first is not None:
                break
        return cwd, first

    def background_work(self, file: Path, since: datetime | None) -> list[str]:
        """The exec sessions its turns left running (:func:`_open_execs`)."""
        return _open_execs(file, since)

    def activity_since(self, file: Path, offset: int) -> bool:
        """A turn opened or at work after ``offset`` (:func:`_turn_begun`)."""
        return _turn_begun(file, offset)

    def turns(self, file: Path, state: str | None, day_of: Callable[[str], str]) -> TurnsScan:
        """:meth:`usage`, cut at each prompt of the human's (:mod:`codex_usage`)."""
        from aisquare.core.agent_adapters import codex_usage

        return codex_usage.turns(file, state, day_of)


_ROLLOUT_NAME = re.compile(r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)\.jsonl$")
"""A rollout's name: the local time its session began, then its thread id."""
_APP_SERVER = "app-server"
_DAEMON = "daemon"
_MANAGED_DAEMON = "--managed-daemon"
"""The subcommand, its ``daemon`` subcommand and the flag of the app-server daemon Codex
manages itself (0.164.0, ``ps`` in SMOKE11 and #47's reopen), which a ``codex`` with no
arguments starts and attaches its TUI to: ``app-server … --managed-daemon`` and ``app-server
daemon pid-update-loop``."""
_DAEMON_KEEPS = "about 30 idle minutes later"
"""How long Codex's managed daemon keeps a thread after its TUI quit (:meth:`CodexTranscript.
server_note`): measured on 0.164.0 with ``lsof`` every 15 s (#47), held from ``/exit`` at
14:58:21 to between 15:28:13 and 15:28:28, the daemon living on and holding nothing after."""
_DAEMON_DIR = "/packages/app-server-daemon/"
"""Where under its home Codex installs the daemon it manages (``<CODEX_HOME>/packages/
app-server-daemon/releases/<version>/bin/codex``): whatever runs from there is the daemon."""
_EMBEDDED = frozenset(
    {
        "-c",
        "--config",
        "--search",
        "--no-daemon",
        "--strict-config",
        "--dangerously-bypass-hook-trust",
        "-p",
        "--profile",
    }
)
"""The options a Codex runs embedded with, never on its managed daemon (:func:`_runs_as`).
Measured on 0.164.0 (#50): a TUI given each, one prompt, its daemon running, and ``lsof``
named its own native binary the rollout's holder, where a ``codex`` with no arguments had
the daemon hold it; each but ``--no-daemon`` says so ("Running without the shared background
server: … requires embedded mode"). Not ``--enable`` nor ``--disable``, which that warning
names beside ``-c``: one the daemon's features already match (``--enable hooks``,
``--disable artifact``) runs on the daemon, the daemon holding the rollout, and one they do
not asks to run without it this time or to restart it with them, or refuses to start
(measured). Not ``--oss``, which 0.164.0's binary lists beside these and which would not
start on a loopback (unmeasured)."""
_COMMANDS = frozenset(
    {
        "agents",
        "tcp-tunnel",
        "exec",
        "e",
        "review",
        "login",
        "logout",
        "mcp",
        "plugin",
        _APP_SERVER,
        "remote-control",
        "app",
        "completion",
        "update",
        "doctor",
        "sandbox",
        "debug",
        "execpolicy",
        "apply",
        "a",
        "resume",
        "queue",
        "archive",
        "delete",
        "migrate-rollouts",
        "migrate-nfs-db",
        "unarchive",
        "fork",
        "cloud",
        "cloud-tasks",
        "exec-server",
        "features",
        "help",
        "responses-api-proxy",
        "stdio-to-uds",
    }
)
"""Codex 0.164.0's subcommands and their aliases: every name ``codex completion bash`` lists
(``codex --help`` hides ``execpolicy``, ``tcp-tunnel``, ``responses-api-proxy`` and
``stdio-to-uds``), and ``cloud-tasks``, the alias it hides, which its help lists beside its
subcommand's description as it does ``e`` and ``a`` (``codex help cloud-tasks`` says ``Usage:
codex cloud``; ``codex help`` of any other word, ``mcp-server``, ``proto`` or ``chat``,
"unrecognized subcommand"). A first word that is none of them is a prompt's (``codex
[OPTIONS] [PROMPT]``), a TUI's."""
_OWN_COMMANDS = {
    _APP_SERVER: frozenset(
        {
            _DAEMON,
            "proxy",
            "generate-ts",
            "generate-json-schema",
            "generate-internal-json-schema",
            "help",
        }
    ),
    "cloud": frozenset({"exec", "status", "list", "apply", "diff", "help"}),
    "cloud-tasks": frozenset({"exec", "status", "list", "apply", "diff", "help"}),
    "completion": frozenset({"bash", "elvish", "fish", "powershell", "zsh"}),
    "debug": frozenset(
        {"models", _APP_SERVER, "prompt-input", "trace-reduce", "clear-memories", "help"}
    ),
    "exec": frozenset({"resume", "fork", "review", "help"}),
    "e": frozenset({"resume", "fork", "review", "help"}),
    "exec-server": frozenset({"forward", "help"}),
    "execpolicy": frozenset({"check", "help"}),
    "features": frozenset({"list", "enable", "disable", "help"}),
    "login": frozenset({"status", "help"}),
    "mcp": frozenset({"list", "get", "add", "remove", "login", "logout", "help"}),
    "plugin": frozenset({"add", "list", "marketplace", "remove", "help"}),
    "remote-control": frozenset({"start", "stop", "pair", "help"}),
    "sandbox": frozenset({"uninstall", "help"}),
}
"""The subcommands of each of :data:`_COMMANDS` that has its own, as ``codex completion
bash`` lists them (0.164.0): a word right after its subcommand's name that is one of them is
that, never a prompt's (``codex execpolicy check --rules <PATH>``, ``codex app-server
generate-ts``), as an option's value is not (:func:`_could_be_prompt`)."""
_REMOTE = "--remote"
"""The option that connects Codex's TUI to an app-server it names (``--remote <ADDR>``:
``ws://``, ``wss://`` or ``unix://[PATH]``), its own or the daemon's; Codex refuses it with
any subcommand but the TUI's (``codex --remote unix:// exec …``: "only supported for
interactive TUI commands"). Measured on 0.164.0 (tester-50-51), ``codex --remote unix:// -c
model=…`` was connected to the daemon's socket, its ``-c`` notwithstanding."""
_RESUMES = frozenset({"resume", "fork"})
"""The subcommands that run Codex's TUI on a thread (``resume``/``fork [OPTIONS] [SESSION_ID]
[PROMPT]``), on its daemon unless they run embedded."""
_CLIENTS = frozenset({"agents"})
_PROXY = "proxy"
"""The other forms that connect to Codex's managed daemon, measured on 0.164.0 (``lsof -U``:
a socket whose peer is the daemon's on the path it listens on): ``agents`` ("Browse all agent
sessions on the shared local app-server daemon") and ``app-server proxy`` ("Proxy stdio bytes
to the running app-server control socket"), ``proxy`` being one of ``app-server``'s own
subcommands (:data:`_OWN_COMMANDS`). With the daemon holding a thread, these, a TUI with no
subcommand, ``--remote unix://``, a prompt (``codex 'review the diff with -c and -p'``, which
``ps`` lists as ``review the diff with -c and -p``), ``resume <id>`` and ``fork <id>`` were
connected to it; ``app-server`` (stdio), ``app-server --listen ws://…``, ``exec-server`` and
``login`` were not, and ``review`` and ``exec``, mid-turn, held their own rollouts."""
_THREAD_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
"""A thread id (a UUID), the ``SESSION_ID`` of a ``resume`` or a ``fork``."""


def _runs_as(words: Sequence[str]) -> HoldsOpen:
    """How a Codex run with ``words`` after its binary holds what it writes
    (``AgentTranscript.held_open``), read as Codex reads its command line up to the prompt
    and never into it: ``ps`` loses quoting, so ``codex 'add a -c flag'`` lists as ``codex
    add a -c flag`` (tester-50). Options come first, each value skipped
    (``capabilities.value_options``); the first word that is neither is the subcommand, or
    else a prompt's, which ends the reading.

    ``clients`` for the daemon's own (``app-server daemon …``, ``app-server …
    --managed-daemon``). ``alone`` for an option of :data:`_EMBEDDED` (``--config=…``, ``-c…`` and
    ``-p…`` too) before the prompt, unless :data:`_REMOTE` is too, and for a subcommand of
    :data:`_COMMANDS` that is not one of those that can be the daemon's client
    (:data:`_RESUMES`, :data:`_CLIENTS`, ``app-server`` :data:`_PROXY`). ``session`` for
    those, for no subcommand, and for a first word that is none (a prompt). A subcommand's
    name followed by a word that could be a prompt's (:func:`_could_be_prompt`) reads as a
    prompt too: ``codex 'exec the tests'`` and ``codex exec the tests`` list alike, and the
    TUI the first is, is a possible client (the MANAGER DECISION: naming a Codex the human
    started, "quit it first", is the lesser harm; telling them to stop the daemon under an
    open TUI breaks their conversation). So is ``--remote <ADDR>`` with an option of
    :data:`_EMBEDDED`, measured connected (the MANAGER DECISION, #51's reopen)."""
    from aisquare.core.agent_adapters.codex import CodexAdapter

    values = CodexAdapter.capabilities.value_options
    rest = iter(words)
    embedded = remote = False
    for word in rest:
        if word == "--":
            break  # a prompt
        if word.startswith("-"):
            embedded = embedded or _marks_embedded(word)
            remote = remote or word.split("=", 1)[0] == _REMOTE
            if "=" not in word and word in values:
                next(rest, None)  # its value
            continue
        after = list(rest)
        if word == _APP_SERVER and (after[:1] == [_DAEMON] or _MANAGED_DAEMON in after):
            return "clients"
        if embedded and not remote:
            return "alone"
        if word not in _COMMANDS:
            return "session"  # a prompt's first word
        if word in _RESUMES:
            return _resumes_as(after, remote=remote)
        if word in _CLIENTS or (word == _APP_SERVER and after[:1] == [_PROXY]):
            return "session"
        own = _OWN_COMMANDS.get(word, frozenset())
        return "session" if _could_be_prompt(after, own) else "alone"
    return "alone" if embedded and not remote else "session"


def _marks_embedded(option: str) -> bool:
    """Whether ``option`` is one of :data:`_EMBEDDED`, ``--config=…`` too, or ``-c``/``-p``
    with its value joined to it (``-cmodel=…``, ``-pp2``)."""
    return option.split("=", 1)[0] in _EMBEDDED or option[:2] in _EMBEDDED


def _resumes_as(words: Sequence[str], *, remote: bool) -> HoldsOpen:
    """A ``resume`` or a ``fork`` run with ``words`` after it, ``remote`` when
    :data:`_REMOTE` came before it: ``alone`` for an option of :data:`_EMBEDDED` before its
    prompt, unless :data:`_REMOTE` is there too, else ``session``. Its ``SESSION_ID`` is
    read past when it is a thread id (:data:`_THREAD_ID`); any other word is its prompt
    (``--last``'s too), a session's name, or a prompt that begins with ``resume`` (``codex
    'resume work -c'``), and ends the reading."""
    from aisquare.core.agent_adapters.codex import CodexAdapter

    values = CodexAdapter.capabilities.value_options
    rest = iter(words)
    embedded = named = False
    for word in rest:
        if word == "--":
            break
        if word.startswith("-"):
            embedded = embedded or _marks_embedded(word)
            remote = remote or word.split("=", 1)[0] == _REMOTE
            if "=" not in word and word in values:
                next(rest, None)
            continue
        if named or not _THREAD_ID.fullmatch(word):
            break
        named = True
    return "alone" if embedded and not remote else "session"


def _could_be_prompt(words: Sequence[str], own: frozenset[str]) -> bool:
    """Whether ``words``, after a subcommand's name, could be the rest of a prompt that starts
    with that name: one is neither an option, the value right after one, nor, the first such,
    one of the subcommand's own (``own``, :data:`_OWN_COMMANDS`) (``app-server --listen
    ws://127.0.0.1:4500`` and ``execpolicy check --rules /r.rules`` cannot; ``exec the
    tests`` and ``exec review the diff`` can)."""
    valued = False
    for word in words:
        if word.startswith("-") and word != "--":
            valued = "=" not in word
        elif valued:
            valued = False
        elif word in own:
            own = frozenset()  # its own subcommand, the first word only
        else:
            return True
    return False


_REPLY_MAX_BYTES = 64_000_000
"""How far back :func:`_rollout_reply` reads for the prompt a reply answers, as Claude Code's
reader does: a turn bigger than this has lost its anchor, and says "no prompt"."""
_RAW_TIMESTAMP = re.compile(rb'\A\{"timestamp"\s*:\s*"([^"\\]{1,64})"')
"""A record's time off the bytes of a line too long to parse: its first key."""


def _rollout_reply(file: Path, since: datetime | None) -> str | None:
    """The agent's words since the rollout's newest prompt of the human's (``AgentTranscript.
    reply_since``): each assistant ``message`` after it, oldest first, joined by newlines.

    The prompt is the newest ``user`` message that is not Codex's own context (:func:`_own`:
    its ``<turn_aborted>``, ``<environment_context>``, AGENTS.md), so a turn the human
    interrupted keeps its prompt. A call (``function_call`` and the rest) answers it too,
    with no words, as a tool use does on Claude Code. The ``event_msg`` copies of the
    conversation (``agent_message``, ``item_completed``) are not read again. Read backwards
    from the end up to :data:`_REPLY_MAX_BYTES`; a line too long to parse is read for its
    kind (a long message of the agent's is parsed all the same: it is the reply)."""
    try:
        size = file.stat().st_size
    except OSError:
        return None
    words: list[str] = []
    answered = False
    try:
        for _, raw in lines_backwards(file, size, budget=_REPLY_MAX_BYTES):
            if len(raw) > MAX_LINE:
                kind, _ = _rollout_unparsed(raw, set(), None)
                if kind == "user_prompt":
                    stamp = _RAW_TIMESTAMP.match(raw)
                    when = stamp.group(1).decode("utf-8", "replace") if stamp else None
                    return _reply(words, answered, record_time({"timestamp": when}), since)
                if kind == "assistant_text":
                    with suppress(ValueError, UnicodeDecodeError, RecursionError):
                        words.insert(0, _message_text(_payload(json.loads(raw))))
                answered = answered or kind in ("assistant_text", "assistant_tool")
                continue
            record = parse_line(raw)
            if record is None or record.get("type") != "response_item":
                continue
            payload = _payload(record)
            kind = payload.get("type")
            if kind in _CALLS:
                answered = True
            elif kind == "message" and payload.get("role") == "assistant":
                answered = True
                text = _message_text(payload)
                if text.strip():
                    words.insert(0, text)
            elif kind == "message" and payload.get("role") == "user":
                if _own(_message_text(payload).lstrip()):
                    continue
                return _reply(words, answered, record_time(record), since)
    except OSError:
        return None
    return None


def _reply(
    words: list[str], answered: bool, asked_at: datetime | None, since: datetime | None
) -> str | None:
    if since is not None and asked_at is not None and asked_at < since:
        return None  # an older turn's prompt: the reply has not reached the rollout yet
    return "\n".join(words).strip() if answered else None


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _message_text(payload: Mapping[str, Any]) -> str:
    """A ``message``'s text parts, joined."""
    content = payload.get("content")
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    return "\n".join(
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _own(text: str) -> bool:
    """Whether a ``user`` message is Codex's own context, not the human's words: one of its
    tags held closed (:data:`_OWN_TEXT`), or the AGENTS.md it passes on. Text a person typed
    that only starts like one is theirs, as typed, and so is a prompt that opens with a tag
    of its own (``<task>…</task> …``): taken for context, it was missing from the Transcript
    tab, and the tail read ``own`` until the agent answered (review r16 of #113, 1/8)."""
    if text.startswith(_INSTRUCTIONS):
        return True
    opening = _OWN_TEXT.match(text)
    return opening is not None and f"</{opening.group(1)}>" in text


def _call_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("call_id")
    return value if isinstance(value, str) and value else None


def _call_name(payload: Mapping[str, Any]) -> str:
    if payload.get("type") == "local_shell_call":
        return "local_shell"
    name = payload.get("name")
    return name if isinstance(name, str) and name else "tool"


def _call_input(payload: Mapping[str, Any]) -> object:
    """A call's input as an object: ``function_call``'s JSON ``arguments``, a
    ``custom_tool_call``'s freeform ``input`` (a patch, a script) as ``{"input": …}``, a
    ``local_shell_call``'s ``action`` command as ``{"command": …}``."""
    kind = payload.get("type")
    if kind == "custom_tool_call":
        return {"input": _string(payload.get("input"))}
    if kind == "local_shell_call":
        action = payload.get("action")
        command = action.get("command") if isinstance(action, dict) else None
        if isinstance(command, list):
            return {"command": " ".join(part for part in command if isinstance(part, str))}
        return {}
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed: Any = json.loads(arguments) if isinstance(arguments, str) else None
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _summary(name: str, arguments: object) -> str:
    """A call as one line: its name, and the first line of what it runs or asks."""
    detail = ""
    if isinstance(arguments, dict):
        for key in ("cmd", "command", "input", "path", "query", "url", "prompt"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip().splitlines()[0]
                break
        else:
            asked = first_question(arguments)
            detail = asked.strip().splitlines()[0] if asked.strip() else ""
    return f"{name}({detail[:72]})" if detail else name


def _pending(payload: Mapping[str, Any], call_id: str, at: datetime | None) -> PendingTool:
    name = _call_name(payload)
    arguments = _call_input(payload)
    kept, dropped = kept_input(arguments)
    return PendingTool(
        tool_use_id=call_id,
        name=name,
        summary=_summary(name, arguments),
        input=kept,
        at=at,
        input_dropped=dropped,
    )


def _key(file: Path, offset: int) -> str:
    """A record's key: where it is in the rollout, which only ever grows. The rollout's name
    holds its thread's id, so two agents' records at the same offset are two keys."""
    return f"{file.name}:{offset}"


def _rollout_tail(file: Path, size: int, budget: int, written: datetime) -> TranscriptTail:
    """What the end of a Codex rollout says, free to raise ``OSError``.

    Newest record first, and the walk ends at whichever comes first: the human's own last
    prompt or the turn's ``task_started``, the end of the response before the newest one
    (once an item of the newest has been met: every call of the one before has its
    result), ``TAIL_RECORDS`` conversation records, or ``budget`` bytes. A turn that ended
    (its ``task_complete`` or ``turn_aborted`` met before anything of it but Codex's own
    records) waits on nothing: the walk reads back only to the agent's last words, and is
    never cut. Every result met answers its call; a call met without one is pending. A
    line too long to parse is read for its ids alone (:func:`_rollout_unparsed`). A walk
    that ran out inside the newest response of a turn still running says so (``cut_at``):
    a call of it may wait unseen.
    """
    answered: set[str] = set()
    pending: list[PendingTool] = []  # newest first
    newest, newest_at, marker_key = "none", None, None
    last_text: str | None = None
    text_at: datetime | None = None
    spoke = False  # the agent's newest message has been met, whether or not it was read
    ended = False  # the newest turn's end was met before any record of it
    final: str | None = None  # what that end says the agent said last
    in_response = False  # an item of the newest response has been met
    examined = 0
    cut = False  # the walk ran out before it could stop where nothing older waits
    own_only = True  # every record walked so far is one Codex wrote itself
    under_own = "none"
    under_own_at: datetime | None = None
    under_own_key: str | None = None

    def said(kind: str, at: datetime | None, key: str) -> None:
        nonlocal newest, newest_at, marker_key, own_only, under_own, under_own_at, under_own_key
        if newest == "none":
            newest, newest_at, marker_key = kind, at, key
        elif own_only and kind != "own":
            under_own, under_own_at, under_own_key = kind, at, key
        own_only = own_only and kind == "own"

    for offset, raw in lines_backwards(file, size, budget=budget):
        if ended and spoke:
            break  # a turn that ended, its last words met: nothing older changes the tail
        key = _key(file, offset)
        if len(raw) > MAX_LINE:
            at = written if newest == "none" else None
            kind, call = _rollout_unparsed(raw, answered, at)
            if kind == "ended":
                ended = ended or own_only
            elif kind is not None:
                examined += 1
                in_response = in_response or kind in ("assistant_text", "assistant_tool")
                spoke = spoke or kind == "assistant_text"
                if call is not None and not ended:
                    pending.append(call)
                said(kind, at, key)
                if kind == "user_prompt":
                    break
            continue
        record = parse_line(raw)
        if record is None:
            continue
        top = record.get("type")
        payload = _payload(record)
        kind = payload.get("type")
        at = record_time(record)
        if top == "event_msg":
            if kind in ("task_complete", "turn_aborted") and own_only:
                ended = True
                if kind == "turn_aborted":
                    examined += 1
                    said("interrupted", at, key)
                else:
                    found = payload.get("last_agent_message")
                    final = found if isinstance(found, str) and found.strip() else None
            elif kind == "task_started":
                break  # the turn's start: nothing older waits on anyone
            elif kind in _RESPONSE_ENDS and in_response:
                break  # the response before the newest: every call of it has its result
            continue
        if top == "token_usage_record":
            if in_response:
                break
            continue
        if top == "compacted":
            examined += 1
            said("own", at, key)
            continue
        if top != "response_item":
            continue
        examined += 1
        if examined > TAIL_RECORDS:
            cut = True
            break
        if kind in _RESULTS:
            call_id = _call_id(payload)
            if call_id is not None:
                answered.add(call_id)
            said("tool_result", at, key)
        elif kind in _CALLS:
            in_response = True
            call_id = _call_id(payload)
            if call_id is not None and call_id not in answered and not ended:
                pending.append(_pending(payload, call_id, at))
            said("assistant_tool", at, key)
        elif kind == "reasoning":
            in_response = True  # thinking alone: the record before it says what it does
        elif kind == "message" and payload.get("role") == "assistant":
            in_response = True
            text = _message_text(payload)
            if text.strip():
                said("assistant_text", at, key)
                if not spoke:
                    spoke, last_text, text_at = True, text, at
        elif kind == "message" and payload.get("role") == "user":
            if _own(_message_text(payload).lstrip()):
                said("own", at, key)
                continue
            said("user_prompt", at, key)
            break  # the human's own prompt: nothing before it waits on anyone
    else:
        cut = size > budget  # the reader stopped on its budget, short of the file's start
    if last_text is None and final is not None:
        last_text, text_at = final, newest_at
    return TranscriptTail(
        pending=() if ended else tuple(reversed(pending)),
        newest=newest,
        newest_at=newest_at,
        last_text=last_text,
        last_text_at=text_at,
        marker_key=marker_key,
        cut_at=written if cut and not ended else None,
        under_own=under_own,
        under_own_at=under_own_at,
        under_own_key=under_own_key,
    )


def _rollout_unparsed(
    raw: bytes, answered: set[str], at: datetime | None
) -> tuple[str | None, PendingTool | None]:
    """A line too long to parse, read off its bytes: what record it is, and its call when it
    is one still without a result (known by id and name alone, its input not read).

    ``ended`` for a turn's ``task_complete`` (its last words too long to parse); a result
    answers its call (``answered`` grows); ``None`` for a record the tail does not read.
    """
    head = raw[:_RAW_HEAD]
    types = [match.decode() for match in _RAW_TYPE.findall(head)[:2]]
    top = types[0] if types else ""
    kind = types[1] if len(types) > 1 else ""
    if top == "compacted":
        return "own", None
    if top == "event_msg":
        return ("ended", None) if kind == "task_complete" else (None, None)
    if top != "response_item":
        return None, None
    found = _RAW_CALL_ID.search(raw)
    call_id = found.group(1).decode("utf-8", "replace") if found else None
    if kind in _RESULTS:
        if call_id is not None:
            answered.add(call_id)
        return "tool_result", None
    if kind in _CALLS:
        if call_id is None or call_id in answered:
            return "assistant_tool", None
        named = _RAW_NAME.search(head)
        name = named.group(1).decode("utf-8", "replace") if named else "tool"
        call = PendingTool(call_id, name, name, {}, at, input_dropped=True)
        return "assistant_tool", call
    if kind == "message":
        if _RAW_ROLE_ASSISTANT.search(head):
            return "assistant_text", None
        if _RAW_ROLE_USER.search(head):
            opening = _RAW_TEXT.search(head)
            words = opening.group(1).decode("utf-8", "replace").lstrip() if opening else ""
            own = words.startswith(_INSTRUCTIONS) or _OWN_TEXT.match(words) is not None
            return ("own" if own else "user_prompt"), None
    return None, None


# --- a turn end: the exec sessions it left running, and a turn begun since ------------------

_STDIN_TOOL = "write_stdin"
"""Codex's tool that writes to (or polls) an exec session: ``arguments.session_id``."""
_RUNNING = re.compile(r"^Process running with session ID (\d+)\s*$", re.MULTILINE)
_EXITED = re.compile(r"^Process exited with code -?\d+\s*$", re.MULTILINE)
_OUTPUT = "\nOutput:"
"""Where an exec result's header ends and the command's own output begins: only the header
says whether the process runs, so a command that prints the words is not taken for one."""
_EXEC_MARKS = (
    b"Process running with session ID",
    b"Process exited with code",
    b'"CommandExecution"',
    b'"write_stdin"',
)
"""What a line must hold to say anything of an exec session; every other one is skipped
unparsed."""
_RAW_RUNNING = re.compile(rb"Process running with session ID (\d+)")
_RAW_EXITED = re.compile(rb"Process exited with code -?\d+")
_RAW_PROCESS = re.compile(rb'"process_id"\s*:\s*"(\d+)"')
_TURN_MARKS = (b'"response_item"', b'"task_started"')


def _open_execs(file: Path, since: datetime | None) -> list[str]:
    """``AgentTranscript.background_work`` for a rollout: the session id of each exec its
    turns left running (the module docstring's "What a turn end leaves"), oldest first.

    One is running from a result saying ``Process running with session ID <n>`` until a
    ``CommandExecution`` ``item_completed`` names process ``<n>`` or a ``write_stdin`` to it is
    answered ``Process exited``. ``since`` leaves out the sessions a result reported before
    it: they ran in an earlier process of the agent, gone with it. A line too long to parse
    is read by its header's bytes. ``OSError`` when the file cannot be read."""
    running: dict[str, None] = {}  # ordered: the oldest first
    polled: dict[str, str] = {}  # a ``write_stdin`` call → the session it writes to
    with file.open("rb") as handle:
        for raw in handle:
            if not any(mark in raw for mark in _EXEC_MARKS):
                continue
            fact = _exec_fact(raw)
            if fact is None:
                continue
            kind, call, session, when = fact
            if kind == "polls" and call is not None and session is not None:
                polled[call] = session
            elif kind == "running" and session is not None:
                if since is None or when is None or when >= since:
                    running.setdefault(session)
            elif kind == "exited" and call is not None:
                running.pop(polled.get(call, ""), None)
            elif kind == "ended" and session is not None:
                running.pop(session, None)
    return list(running)


def _exec_fact(raw: bytes) -> tuple[str, str | None, str | None, datetime | None] | None:
    """What one rollout line says of an exec session, as ``(kind, call id, session, when)``:
    ``polls`` (a ``write_stdin`` call and the session it names), ``running`` (a result whose
    header says its process runs), ``exited`` (a result whose header says it exited),
    ``ended`` (a ``CommandExecution`` completed); ``None`` for anything else."""
    record = parse_line(raw)
    if record is None:
        return _raw_exec_fact(raw) if len(raw) > MAX_LINE else None
    payload = _payload(record)
    when = record_time(record)
    if record.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "CommandExecution":
            return None
        process = item.get("process_id")
        return "ended", None, str(process) if isinstance(process, str | int) else None, when
    if record.get("type") != "response_item":
        return None
    kind = payload.get("type")
    if kind in _CALLS and _call_name(payload) == _STDIN_TOOL:
        arguments = _call_input(payload)
        session = arguments.get("session_id") if isinstance(arguments, dict) else None
        named = str(session) if isinstance(session, str | int) else None
        return "polls", _call_id(payload), named, when
    if kind not in _RESULTS:
        return None
    header = _message_text({"content": payload.get("output")}).split(_OUTPUT, 1)[0]
    found = _RUNNING.search(header)
    if found is not None:
        return "running", _call_id(payload), found.group(1), when
    if _EXITED.search(header) is not None:
        return "exited", _call_id(payload), None, when
    return None


def _raw_exec_fact(raw: bytes) -> tuple[str, str | None, str | None, datetime | None] | None:
    """:func:`_exec_fact` off the head of a line too long to parse: its keys and a result's
    header come before any long content (a ``write_stdin`` call is not read: its session
    ends by its ``item_completed`` all the same)."""
    head = raw[:_RAW_HEAD]
    types = [match.decode() for match in _RAW_TYPE.findall(head)[:2]]
    stamp = _RAW_TIMESTAMP.match(raw)
    when = record_time({"timestamp": stamp.group(1).decode()}) if stamp else None
    if types[:1] == ["event_msg"] and b'"CommandExecution"' in head:
        process = _RAW_PROCESS.search(head)
        return ("ended", None, process.group(1).decode(), when) if process else None
    if types[:1] != ["response_item"] or len(types) < 2 or types[1] not in _RESULTS:
        return None
    found = _RAW_CALL_ID.search(head)
    call = found.group(1).decode("utf-8", "replace") if found else None
    running = _RAW_RUNNING.search(head)
    if running is not None:
        return "running", call, running.group(1).decode(), when
    return ("exited", call, None, when) if _RAW_EXITED.search(head) else None


def _turn_begun(file: Path, offset: int) -> bool:
    """``AgentTranscript.activity_since`` for a rollout: a ``task_started`` or any
    ``response_item`` on a line that starts at or after byte ``offset``. A rollout shorter than
    ``offset`` was rewritten, and a line of either kind that does not parse (too long, or still
    being written) counts: both are a turn as like as not. ``OSError`` when it cannot be read."""
    with file.open("rb") as handle:
        if handle.seek(0, 2) < offset:
            return True
        handle.seek(max(0, offset - 1))
        if offset > 0:
            handle.readline()  # the rest of the line ``offset`` falls in: begun before it
        for raw in handle:
            if not any(mark in raw for mark in _TURN_MARKS):
                continue
            record = parse_line(raw)
            if record is None:
                return True
            if record.get("type") == "response_item":
                return True
            if record.get("type") == "event_msg" and _payload(record).get("type") == (
                "task_started"
            ):
                return True
    return False


# --- the page: one record as terminal lines ---------------------------------------------------


def _rollout_lines(record: dict[str, Any], width: int) -> list[str]:
    """One rollout record as terminal lines, or ``[]`` when it is not conversation: the
    human's words, the agent's (a proposed plan in full), each call as one dim line (a
    question in full), an interruption and a compaction as a dim note. Results, reasoning,
    Codex's own context and the ``event_msg`` copies of the conversation are not shown."""
    top = record.get("type")
    payload = _payload(record)
    kind = payload.get("type")
    if top == "compacted":
        return [f"{DIM}  ⎿ conversation compacted{OFF}", ""]
    if top == "event_msg":
        if kind == "turn_aborted":
            return [dim_note("interrupted", width), ""]
        return []
    if top != "response_item":
        return []
    if kind == "message":
        role = payload.get("role")
        text = _message_text(payload)
        if not text.strip():
            return []
        if role == "user":
            return [] if _own(text.lstrip()) else [YOU, *wrap(text, width), ""]
        if role == "assistant":
            return [speaker_line(SPEAKER), *_said_rows(text, width), ""]
        return []
    if kind in _CALLS:
        name = _call_name(payload)
        arguments = _call_input(payload)
        if name == _QUESTION_TOOL and isinstance(arguments, dict):
            asked = question_lines(arguments.get("questions"), width)
            if asked:
                return [speaker_line(SPEAKER), *asked, ""]
        return [speaker_line(SPEAKER), dim_note(_summary(name, arguments), width), ""]
    return []


def _said_rows(text: str, width: int) -> list[str]:
    """The agent's words, a ``<proposed_plan>`` in them as a plan: what is around it as
    words, the plan under a dim ``plan:`` line, indented."""
    lines: list[str] = []
    rest = text
    while (found := _PROPOSED_PLAN.search(rest)) is not None:
        before = rest[: found.start()].strip()
        if before:
            lines.extend(wrap(before, width))
        lines.extend([f"{DIM}  ⎿ plan:{OFF}", *wrap(found.group(1), width, indent="    ")])
        rest = rest[found.end() :]
    if rest.strip():
        lines.extend(wrap(rest.strip() if lines else rest, width))
    return lines
