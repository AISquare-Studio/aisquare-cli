"""Every place this CLI starts a process, and whether it carries an identity.

Tracing identity is PROCESS-level: it rides in ``ANTHROPIC_BASE_URL`` plus the
``X-Agent-Name``/``X-Pipeline-Id`` pair in ``ANTHROPIC_CUSTOM_HEADERS``, and a
child inherits the parent's environment unless told otherwise. That makes every
``subprocess``/``exec`` call in this package a decision — traced or excluded —
and an *undecided* one is not neutral: a probe subprocess that inherits a real
role's identity mints a junk Run under that role and corrupts the dataset the
morning experiments measure.

The headers are not the whole identity. A traced launch also exports a MARKER
beside them — :data:`MARKER_ENV_VARS`, written by
``services.explainability.trace_marker`` — and that is what a process
DOWNSTREAM of the agent keys its records on: ``core.insights.run_key`` files
every insight under the run key, and the session→Run join the hook writes reads
the rest. So a child that keeps the marker files its work under the parent's
Run even when its own model traffic is untraced — which is why
:data:`IDENTITY_ENV_VARS`, not :data:`TRACING_ENV_VARS`, is what a stripping
seam removes.

The names are deliberately not re-spelled in this prose. The marker started as
a pair and is three names now, and every place that had written the pair out by
hand had to be found and widened by hand when the third arrived — one of them
was missed, and a session inherited a stale run trace id and filed its join
against the previous session's Run. Point at the tuple; it is the inventory.

So the decisions are written down here rather than left implicit, and
``tests/test_spawn_seams.py`` walks the AST of this package on every run to
assert that ``SEAMS`` still names every call site that exists. A docstring
inventory drifts silently the first time someone adds a ``subprocess.run``;
this one fails the build instead. That is the whole reason the registry lives
in ``src`` and not in the test: it is the artefact, the test is only the latch.

``untraced_env`` is how a seam acts on an "excluded" ruling.

THE INVENTORY (see ``SEAMS`` for the machine-readable copy)

Traced — these ARE the agent, and are meant to carry an identity:
  * ``cli/launch.py::_exec`` — ``aisquare launch`` replaces itself with the
    agent. Wired by ``services.explainability.wire_session``.
  * ``cli/team.py::spawn`` — ``aisquare team spawn --exec``, same wiring.

Excluded, and actively stripped — these talk to a model or to us, and would
otherwise inherit a live identity:
  * ``core/harness.py::probe_model`` — runs ``claude -p`` once per alias to
    check entitlement. A real LLM process, but NOT a session: it is a
    yes/no question about the account, and a Run for it is pure noise
    attributed to whoever happened to be probing.
  * ``services/persona_import.py::draft_with_manager`` — ``persona import``'s
    manager engine: one headless ``claude -p`` under the manager role's binding
    that turns a source into a persona draft (docs/plans/spawn-personas.md
    §3.9). A real model process but not a session — like the probe, one
    question and one answer — so a Run for it would be attributed to whoever
    happened to import.
  * ``core/brain.py::_run`` — the gbrain worker. Not an agent session, and its
    own ``_env`` already contemplates an Anthropic key path
    (``ANTHROPIC_API_KEY`` is popped only when embeddings are off), so an
    inherited base URL could route it through our proxy under the parent's
    name.
  * ``services/distill.py::spawn_drain`` — a detached ``aisquare team distill``
    of our own. A background worker of ours is not an agent session and has no
    business wearing one's identity.
  * ``services/hooks.py::_detach`` — a detached ``aisquare hook hand-over`` of
    our own, for the same reason; it also outlives the pane whose hook started
    it, which is the whole point of detaching it.

Excluded, nothing stripped — these are not model processes at all, and
narrowing their environment would be change without a reason:
  * ``core/brain.py::gbrain_version`` — ``gbrain --version``, a string.
  * ``core/claude_accounts.py::_ask_keychain`` — ``/usr/bin/security
    find-generic-password`` on macOS: the Claude Code login an account's
    Keychain item holds, read for its usage. No model.
  * ``core/agents.py::hook_binary_version`` — ``<hook's aisquare> --version``,
    a string: doctor asking another install of this CLI what version it is,
    so hooks that name a stale binary stop grading as healthy (#84).
  * ``core/snapshot.py::node_version`` — ``node --version``, a string. Backs the
    doctor's repomix line, which has to gate on the floor repomix declares
    (``node >= 22``) rather than on whether ``npx`` exists.
  * ``core/snapshot.py::head_sha`` and ``core/workspace.py::git_common_root`` —
    ``git rev-parse``; stripped, as every git seam is (:func:`git_env`).
  * ``services/ci_snapshot.py::_git`` — the CI test bed's snapshot plumbing
    (``stash create``, ``update-ref``, ``rev-parse``, ``remote get-url``). No
    model; stripped anyway because it runs inside a traced session's hook and
    a child of a hook is not the agent.
  * ``core/snapshot.py::_run_repomix`` — repomix packs files; no model.
  * ``core/editor.py::edit_text`` — the operator's ``$EDITOR``. It is theirs,
    and it should get their environment.
  * ``core/clipboard.py::write`` — the platform's clipboard tool (``pbcopy``,
    ``clip.exe``, ``wl-copy``, ``xclip``, ``xsel``), handed a copy's text on
    stdin. No model.
  * ``core/clipboard.py::_ask`` — ``tmux show-environment SSH_CONNECTION`` in the
    user's own tmux, and ``ps`` for this process's ancestors: whether the session
    is remote. Strings, no model.
  * ``cli/watch.py::action_open_transcript`` — a pager/viewer on a file.
  * ``core/paths.py::restrict_to_owner`` — ``icacls``, locking the
    credentials file to this account on Windows. A permissions tool, not a
    model process; it is also the seam that guards the API key, so an
    inherited base URL is irrelevant to it either way.
  * ``core/paths.py::_whoami_sid`` — ``whoami /user``, reading this
    account's SID so the ``icacls`` above can name a trustee that cannot be
    spoofed by a stray ``USER`` in the environment. Same argument as its
    caller.
  * ``services/explainability_ops.py::install_sdk`` — ``pip install``.
  * ``services/install_route.py::run_installer`` — ``uv tool install --force``,
    ``aisquare upgrade`` replacing its own install. A package manager, not a
    model process.
  * ``services/install_route.py::run_captured`` — the NEW install after an
    upgrade, asked ``--version`` and ``agents refresh-hooks``. Our own CLI, like
    ``core/selfcli.py::run`` below.
  * ``services/install_route.py::exec_replace`` — ``uv tool uninstall``, the
    last step of ``aisquare uninstall``, which this process becomes.
  * ``core/selfcli.py::exec_self`` — asq handing its terminal to ``aisquare
    upgrade`` or ``uninstall`` (the Doctor view's buttons), and ``upgrade
    --reopen`` opening asq again. Our own CLI, like ``core/selfcli.py::run``.
  * ``services/explainability_ops.py::sdk_doctor`` — the SDK's own doctor
    script. Not stripped: it needs the ``EXPLAINABILITY_*`` environment to
    diagnose the machine it is running on.

Excluded, the fleet's own plumbing (docs/plans/fleet-tui.md §3.4):
  * ``core/tmux.py::_tmux`` — every tmux command, stripped: the private server
    outlives every agent and would hand an inherited identity to all of them.
  * ``core/tmux_control.py::_spawn_client`` — the UI's one long-lived ``tmux -C`` client
    per server, which carries the pane on screen's commands; stripped, since its
    ``attach-session`` starts a server when none runs.
  * ``core/selfcli.py::run`` — our own CLI as a subprocess for the fleet UI's
    onboarding (``init``, ``doctor``); no model process; not stripped, for the
    same reason ``sdk_doctor`` is not.
  * ``cli/fleet.py::_exec_attach`` — ``tmux attach``, a terminal client.
  * ``services/fleet.py::_git`` — ``git worktree`` (add, remove, list) and the
    branch queries behind ``fleet reap`` (§3.5); no model, stripped, as every git
    seam is (:func:`git_env`).
  * ``services/fleet.py::_run_native`` — ``codex queue`` for ``fleet tell`` to a
    busy Codex agent: a client of the agent's local session store, no model;
    stripped, so the caller's identity is not handed to a Codex process.
  * ``services/ngrok_tunnel.py::popen_tied`` — the R panel's ngrok, under the
    parent that ends it with this process; a tunnel to ``127.0.0.1``, no model,
    not stripped.
  * ``services/captain/speaker.py::run_subprocess`` — the captain's speech
    synthesiser (``powershell.exe`` + System.Speech, ``say``, ``spd-say``), the
    text on stdin or as one argument; no model process, and stripped anyway:
    a synthesiser has no use for an identity, and ``powershell.exe`` is a shell.

Checked and NOT a seam, recorded so the next reader does not re-derive it:
  * ``services/project.py`` — catches ``subprocess.SubprocessError`` but starts
    nothing itself; it is a caller of ``core/snapshot.py``'s seams.

NOT a spawn seam, and not fixable here: Claude Code subagents (``Task``) and
Workflow agents run IN-PROCESS inside one agent and inherit its environment
verbatim, so they collapse into the parent's identity — one pipeline session,
one trace, one AGENT span (runner receipt, 2026-08-17). Process is the identity
boundary. Nothing in this module can change that; it is a property of where
those agents run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: The HEADER half of a tracing identity — the routing the wiring sets, and the
#: names it stands down on when the operator already owns them. Kept beside the
#: seam registry rather than imported from ``services.explainability`` because
#: ``core`` does not depend on ``services`` — ``tests/test_spawn_seams.py`` pins
#: the two against each other so they cannot drift apart. What a seam strips is
#: :data:`IDENTITY_ENV_VARS`, which is this plus :data:`MARKER_ENV_VARS` below.
TRACING_ENV_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")

LAUNCH_AGENT_ENV = "AISQUARE_LAUNCH_AGENT"

#: The pid ``aisquare launch`` ran as, which the agent it execs keeps (POSIX): a hook
#: whose agent hands it this pid (``AgentCapabilities.pid_env``) is the launch's own
#: process (``core.agent_sessions.own_process``).
LAUNCH_PID_ENV = "AISQUARE_LAUNCH_PID"

#: The MARKER half, and the INVENTORY of it: the run key, the role the launch
#: ran as, and — when the launch owns its Run rather than letting the proxy key
#: it — the gateway Run key, all exported beside the headers by
#: ``services.explainability.trace_marker``. Duplicated here for the same reason
#: as above, and pinned to the wiring's own names by
#: ``tests/test_spawn_seams.py`` in both directions.
#:
#: THIS TUPLE IS WHERE A NEW MARKER IS ADDED, and everything that strips,
#: unsets or disowns an identity reads it rather than naming its members: the
#: seams here through :data:`IDENTITY_ENV_VARS`, the printed ``team spawn``
#: prelude, and ``services.explainability.disown_inherited_trace``. It went from
#: two names to three once, and the copies that were prose rather than reads had
#: to be chased down one at a time — the one in the spawn prelude was missed.
#:
#: They are not decoration: ``core.insights.run_key`` files every insight under
#: the run key when it is set, and the hook reads the marker to write the
#: session→Run join. Stripping only the headers left an excluded child with the
#: parent's run key — measured on the tmux seam, where the private server hands
#: its environment to every window: an agent that then launched untraced (the
#: default) filed its insights and its join under whoever started the server,
#: which ``trace_marker``'s own docstring calls "worse than no record because it
#: reads as evidence".
MARKER_ENV_VARS = (
    "AISQUARE_PIPELINE_ID",
    "AISQUARE_TRACE_AGENT_NAME",
    "AISQUARE_RUN_TRACE_ID",
    "AISQUARE_LAUNCH_ID",
    LAUNCH_AGENT_ENV,
    "AISQUARE_FLEET_AGENT",
)

#: Everything :func:`untraced_env` removes: the whole identity, header and
#: marker. Separate from :data:`TRACING_ENV_VARS` because that tuple has a
#: second job — it is the stand-down list the wiring shares, and it is joined
#: into a shell snippet the CLI prints — so widening it in place would change
#: user-visible output and the reserved-var guard.
IDENTITY_ENV_VARS = (*TRACING_ENV_VARS, *MARKER_ENV_VARS)

#: The variables that tie a process to one board, seat or role. A fleet window gets
#: each of its own (``fleet.spawn``'s ``-e`` pairs). A tmux server copies the
#: environment of the client that starts it into its global environment, and every
#: window opened on it inherits that, so none of these may reach a server (fix 3 of
#: the fleet-board card, follow-up tsk_01m3k89bkhpj): the owner's asqui launcher
#: exported the launch folder's git root as AISQUARE_TEAM_HUB, and the server it
#: started pinned every window opened there by hand to that board. The switches
#: (``AISQUARE_TEAM``, ``AISQUARE_TEAM_DELTA``) are not here: a shell that sets one
#: has made a choice for its whole fleet.
TEAM_ENV_VARS = (
    "AISQUARE_TEAM_HUB",
    "AISQUARE_FLEET_AGENT",
    "AISQUARE_ROLE",
    "AISQUARE_PERSONA",
    "AISQUARE_BOARD_CONTEXT",
)

TRACED = "traced"
EXCLUDED = "excluded"


@dataclass(frozen=True)
class Seam:
    """One process-spawn site and the ruling on it."""

    decision: str
    reason: str
    #: True when the seam actively removes the identity rather than merely not
    #: adding one. Only meaningful for ``EXCLUDED``.
    strips_identity: bool = False


#: Every ``subprocess``/``os.exec*`` call site in this package, keyed by
#: ``<path under src>::<enclosing function>``. Line numbers are deliberately
#: not part of the key: they move on every edit, and a guard that fails on
#: reformatting is a guard people learn to silence.
SEAMS: dict[str, Seam] = {
    "aisquare/services/native_telemetry.py::start_receiver": Seam(
        EXCLUDED, "local OTLP receiver; not a model process", strips_identity=True
    ),
    "aisquare/cli/launch.py::_exec": Seam(
        TRACED, "the launch seam — this process BECOMES the agent"
    ),
    "aisquare/cli/team.py::spawn": Seam(
        TRACED, "the spawn seam (--exec) — this process BECOMES the agent"
    ),
    "aisquare/core/harness.py::probe_model": Seam(
        EXCLUDED,
        "runs `claude -p` to test an entitlement; a Run for it is junk data "
        "attributed to whoever was probing",
        strips_identity=True,
    ),
    "aisquare/services/persona_import.py::draft_with_manager": Seam(
        EXCLUDED,
        "`persona import`'s manager engine runs `claude -p` once to draft a persona; like "
        "the entitlement probe it is a question, not a session, and a Run for it is junk "
        "attributed to whoever imported",
        strips_identity=True,
    ),
    "aisquare/core/brain.py::_run": Seam(
        EXCLUDED,
        "the gbrain worker — not an agent session, and it has an Anthropic key "
        "path that an inherited base URL would redirect through our proxy",
        strips_identity=True,
    ),
    "aisquare/services/distill.py::spawn_drain": Seam(
        EXCLUDED,
        "a detached `aisquare team distill` of ours — a background worker is not an agent session",
        strips_identity=True,
    ),
    "aisquare/services/hooks.py::_detach": Seam(
        EXCLUDED,
        "a detached `aisquare hook hand-over` of ours (#146; review of #205, finding 1) — the "
        "worker that moves a limited agent is not an agent session, and it outlives the pane "
        "whose hook started it",
        strips_identity=True,
    ),
    "aisquare/cli/accounts.py::_exec": Seam(
        EXCLUDED,
        "`aisquare accounts run` replaces itself with a plain Claude Code session on one "
        "account — not a board role, so it takes no identity and drops an inherited one",
        strips_identity=True,
    ),
    "aisquare/services/claude_accounts.py::run_session": Seam(
        EXCLUDED,
        "the foreground Claude Code session `aisquare accounts add` waits on so the user can "
        "sign in — the same plain session as `accounts run`, and identity-stripped for the "
        "same reason",
        strips_identity=True,
    ),
    "aisquare/core/brain.py::gbrain_version": Seam(EXCLUDED, "`gbrain --version`, a string"),
    "aisquare/core/claude_accounts.py::_ask_keychain": Seam(
        EXCLUDED,
        "`/usr/bin/security find-generic-password` on macOS — reads the login Claude Code keeps "
        "in an account's Keychain item, for its usage; a string, no model",
    ),
    "aisquare/core/agents.py::hook_binary_version": Seam(
        EXCLUDED,
        "`<the aisquare a hook names> --version`, a string — doctor asking another "
        "install of this CLI its version, so hooks pointing at a stale binary stop "
        "grading as healthy (#84). An eager callback that exits before any command "
        "runs; no model process",
    ),
    "aisquare/core/snapshot.py::head_sha": Seam(
        EXCLUDED, "`git rev-parse HEAD`, in `git_env()`", strips_identity=True
    ),
    "aisquare/core/snapshot.py::node_version": Seam(
        EXCLUDED, "`node --version`, a string — the floor repomix declares"
    ),
    "aisquare/core/snapshot.py::_run_repomix": Seam(EXCLUDED, "repomix packs files; no model"),
    "aisquare/core/workspace.py::git_common_root": Seam(
        EXCLUDED, "`git rev-parse`, in `git_env()`", strips_identity=True
    ),
    "aisquare/services/ci_snapshot.py::_git": Seam(
        EXCLUDED,
        "git plumbing for the CI turn snapshot — a child of a traced hook is not the agent",
        strips_identity=True,
    ),
    "aisquare/core/editor.py::edit_text": Seam(
        EXCLUDED, "the operator's $EDITOR — it is theirs, it gets their environment"
    ),
    "aisquare/core/clipboard.py::write": Seam(
        EXCLUDED, "the platform's clipboard tool, handed a copy's text on stdin; no model"
    ),
    "aisquare/core/clipboard.py::_ask": Seam(
        EXCLUDED, "`tmux show-environment` and `ps`, asking whether the session is remote"
    ),
    "aisquare/cli/watch.py::action_open_transcript": Seam(
        EXCLUDED, "a pager/viewer on a transcript file"
    ),
    "aisquare/core/paths.py::restrict_to_owner": Seam(
        EXCLUDED, "`icacls` — locks the credentials file to this account; no model"
    ),
    "aisquare/core/paths.py::_whoami_sid": Seam(
        EXCLUDED,
        "`whoami /user` — reads THIS account's SID for the icacls trustee above. "
        "An identity question about the operating system, not the model API",
    ),
    "aisquare/services/explainability_ops.py::install_sdk": Seam(
        EXCLUDED, "`pip install` — reaches PyPI, never the model API"
    ),
    "aisquare/services/install_route.py::run_installer": Seam(
        EXCLUDED,
        "`uv tool install --force` for `aisquare upgrade` — a package manager reaching an "
        "index, never the model API; not stripped, like `install_sdk`",
    ),
    "aisquare/services/install_route.py::run_captured": Seam(
        EXCLUDED,
        "the upgraded install asked `--version` and `agents refresh-hooks`: our own CLI as "
        "a subprocess, no model process; not stripped, like `core/selfcli.py::run`",
    ),
    "aisquare/services/install_route.py::exec_replace": Seam(
        EXCLUDED,
        "`uv tool uninstall` replacing this process as the last step of `aisquare "
        "uninstall` — a package manager, never the model API; not stripped",
    ),
    "aisquare/core/selfcli.py::exec_self": Seam(
        EXCLUDED,
        "asq handing its terminal to `aisquare upgrade`/`uninstall`, and `upgrade "
        "--reopen` opening asq again: our own CLI replacing this process (a waited "
        "child on Windows). Starts no model process; not stripped, like `run`",
    ),
    "aisquare/services/explainability_ops.py::sdk_doctor": Seam(
        EXCLUDED,
        "the SDK's own `explainability-doctor` console script. Deliberately NOT "
        "stripped: it talks to the GATEWAY, not the model API, and it needs the "
        "EXPLAINABILITY_* environment to answer at all — stripping would make "
        "the diagnostic lie about the machine it is diagnosing",
    ),
    # --- the fleet (docs/plans/fleet-tui.md §3.4) ---------------------------------
    "aisquare/core/tmux.py::_tmux": Seam(
        EXCLUDED,
        "every tmux command, including the one that starts the fleet's private "
        "server. The server outlives every agent and hands its environment to all "
        "of them, so an inherited identity here would become EVERY agent's identity; "
        "each window's agent takes its own through `aisquare launch` instead",
        strips_identity=True,
    ),
    "aisquare/core/tmux_control.py::_spawn_client": Seam(
        EXCLUDED,
        "the fleet UI's one long-lived `tmux -C` control client per server — a tmux "
        "client, not an agent. Stripped like `_tmux`: its `attach-session` starts the "
        "server when none is running, and the server hands its environment to every window",
        strips_identity=True,
    ),
    "aisquare/core/selfcli.py::run": Seam(
        EXCLUDED,
        "our own CLI as a subprocess (`init`, `doctor`, `project onboard` for the "
        "fleet UI, run with cwd=<project>). Starts no model process. NOT stripped: "
        "`doctor --live` needs the EXPLAINABILITY_* environment to diagnose the "
        "machine it is on, the same reason `sdk_doctor` is not",
    ),
    "aisquare/services/store_broker.py::run_broker": Seam(
        EXCLUDED,
        "the store broker a launch of a sandboxed agent starts outside its sandbox (review "
        "r9 of #113): a process of ours, no model process. NOT stripped: what it runs is the "
        "agent's own board calls, which keep the agent's Run as from its tool shell",
    ),
    "aisquare/services/store_broker.py::_run": Seam(
        EXCLUDED,
        "one whitelisted board call (`aisquare <argv>`) the broker runs for its sandboxed "
        "agent; no model process. NOT stripped: it is the agent's own call, filed under its "
        "Run as it would be from the tool shell it came from",
    ),
    "aisquare/cli/fleet.py::_exec_attach": Seam(
        EXCLUDED,
        "`tmux attach` — a terminal client on the fleet server, not an agent; "
        "the agents inside already have their identities",
    ),
    "aisquare/services/fleet.py::_run_native": Seam(
        EXCLUDED,
        "`codex queue --thread <id> --message <text>` for `fleet tell` to a busy Codex "
        "agent (`AgentAdapter.queue_args`): a client of the agent's local session store, "
        "no model process; stripped, so the caller's run and fleet identity stay its own",
        strips_identity=True,
    ),
    "aisquare/services/fleet.py::_git": Seam(
        EXCLUDED,
        "`git worktree add/remove/list` and `git branch --merged` for the fleet's "
        "per-coder worktrees (docs/plans/fleet-tui.md §3.5). No model process; "
        "stripped, as every git seam is (`git_env()`)",
        strips_identity=True,
    ),
    "aisquare/core/tmux.py::_sysctl": Seam(
        EXCLUDED,
        "`sysctl -n kern.bootsessionuuid` (macOS) or `kern.boottime` (macOS, the BSDs), at a "
        "fleet spawn and when uninstall, upgrade and fleet shutdown ask whether an agent may "
        "still run; an identifier and a time, no model",
    ),
    "aisquare/services/ngrok_tunnel.py::popen_tied": Seam(
        EXCLUDED,
        "the R panel's ngrok, run by a small Python parent (`-I`) that ends its process "
        "group once this process is gone. A tunnel to 127.0.0.1, never the model API; not "
        "stripped, like `tmux attach`",
    ),
    "aisquare/services/captain/speaker.py::run_subprocess": Seam(
        EXCLUDED,
        "the captain's speech synthesiser (`powershell.exe` + System.Speech, `say`, "
        "`spd-say`) with the text on stdin or as one argument (T3). No model process; "
        "stripped anyway — a synthesiser has no use for an identity, and powershell "
        "is a shell",
        strips_identity=True,
    ),
}


def untraced_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``base`` (default the current environment) without the tracing identity.

    A plain copy minus :data:`IDENTITY_ENV_VARS` — the headers AND the marker
    pair, because a child that keeps the run key files its records under the
    parent's Run however its own model traffic is routed. Never mutates
    ``base``, and never raises — this runs on paths whose whole contract is that
    they degrade quietly, and a child losing four variables it was not entitled
    to is not a failure worth reporting.
    """
    source = os.environ if base is None else base
    # Nor the scope of a command a sandboxed agent's broker runs (``AISQUARE_BROKERED*``,
    # ``services.store_broker``): its child (a detached `team distill`) is aisquare's own work
    # outside the sandbox, run in the board's directory, which the scope's tree need not hold
    # (review r12 of #113, finding 5).
    return {
        key: value
        for key, value in source.items()
        if key not in IDENTITY_ENV_VARS and not key.startswith("AISQUARE_BROKERED")
    }


def git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of every git command aisquare runs: ``base`` (default the current
    environment) without the tracing identity (:func:`untraced_env`), with
    ``GIT_OPTIONAL_LOCKS=0`` and ``GIT_TERMINAL_PROMPT=0``.

    No git aisquare runs is a model process, and one started from a traced session's hook is
    not the agent, so none keeps the identity. ``GIT_OPTIONAL_LOCKS=0``: a command that only
    reads takes no optional lock, so a ``git status`` never writes the index back under the
    developer's own git or a fleet agent's ``checkout`` (review of #259, round 20); the
    variable, not ``--no-optional-locks``, which a git before 2.15 refuses (round 27, 4/7).
    ``GIT_TERMINAL_PROMPT=0``: git fails rather than wait on a credential prompt no one sees.
    The one place they are set: the fleet's seam and the CI snapshot's had drifted apart,
    so a fix to one did not reach the other (round 28, 10/10).
    """
    env = untraced_env(base)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def agent_identity_env_vars() -> tuple[str, ...]:
    """Every registered agent's process-identity variables: the one it sets in its tools'
    shells alone (``AgentCapabilities.tool_shell_env``, Codex's ``CODEX_THREAD_ID``), the
    others those shells get for that one session or sandbox (``tool_shell_vars``: Codex's
    ``CODEX_SESSION_ID``, ``CODEX_CI``, …) and the one it hands each hook its pid in
    (``pid_env``, Claude Code's ``CLAUDE_PID``).

    Each says which agent process a shell is under. A tmux server started from inside an
    agent's tool shell pinned them for every window opened on it, and a ``codex`` started by
    hand in such a window took itself for that thread's nested child
    (``core.agent_sessions.own_process``); ``aisquare launch`` clears them only for the
    agent it starts."""
    from aisquare.core.agent_adapters import adapters

    named = (
        name
        for adapter in adapters()
        for name in (
            adapter.capabilities.tool_shell_env,
            adapter.capabilities.pid_env,
            *adapter.capabilities.tool_shell_vars,
        )
        if name
    )
    return tuple(dict.fromkeys(named))


def tool_shell_homes(env: Mapping[str, str]) -> tuple[str, ...]:
    """The config-dir variable (``AgentAdapter.home_env``) of every agent whose tool shell
    ``env`` is: one holding a variable only its tools' shells are given
    (``AgentCapabilities.tool_shell_env``, ``tool_shell_vars``). That home is the spawner's
    own: a fleet server started from a Codex tool shell pinned its ``CODEX_HOME`` for every
    window, while the one a slot's window runs under travels with that window
    (``claude_accounts.carry_environment``) and the launch sets its agent's (SMOKE8 of
    #113). Claude Code's hooks get its ``CLAUDE_PID``, its shells nothing of their own, so
    its home is never one."""
    from aisquare.core.agent_adapters import adapters

    homes = (
        adapter.home_env
        for adapter in adapters()
        if any(
            env.get(name)
            for name in (adapter.capabilities.tool_shell_env, *adapter.capabilities.tool_shell_vars)
            if name
        )
    )
    return tuple(dict.fromkeys(homes))


def tmux_client_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """What a tmux client runs with: :func:`untraced_env` less :data:`TEAM_ENV_VARS`, every
    agent's process identity (:func:`agent_identity_env_vars`) and, from an agent's tool
    shell, that agent's home (:func:`tool_shell_homes`).

    The client that finds no server starts one, and the server keeps that client's
    environment as its global one, so a server the fleet starts holds no team
    variable: ``AISQUARE_PERSONA`` among them, so a window opened in it by hand is briefed
    as no one's persona (review of #201, F7). Nor the identity of the agent whose tool
    shell started it. The home (``AISQUARE_HOME``) stays: the windows need it.
    """
    env = untraced_env(base)
    dropped = {*TEAM_ENV_VARS, *agent_identity_env_vars(), *tool_shell_homes(env)}
    return {k: v for k, v in env.items() if k not in dropped}
