"""Typed TOML configuration: schema, defaults, load and save.

Two layers. The SYSTEM defaults are the field defaults of the models below and
live nowhere else (:func:`system_defaults` dumps them). The USER's file,
``config.toml``, holds only what the user changed: :func:`save_config` writes the
keys that differ from the system defaults, and :func:`load_config` lays the file
over them, so a key nobody set follows the system default even after it changes,
and :func:`reset_config` moving the file aside is a reset of every setting.
Unknown keys in the file are ignored so old configs keep loading.
"""

from __future__ import annotations

import codecs
import contextlib
import contextvars
import errno
import os
import re
import shutil
import tomllib
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aisquare.core import paths
from aisquare.core.atomic import write_replacing
from aisquare.core.paths import despite_windows_contention
from aisquare.models import Pool, RedactionLevel


class CaptureSettings(BaseModel):
    """Settings for the background capture pipeline."""

    enabled: bool = True


class ProjectsSettings(BaseModel):
    """Which directories become projects.

    ``capture`` off, the default since 0.8.1: a project exists only once it is added on
    purpose — ``aisquare project onboard``, ``init``, Welcome or ``+`` in ``asq``, a fleet
    spawn, ``team on`` — and a hooked session in any other directory records nothing
    there: no project row, no prompt, no turn row, no board. On, every directory a hooked
    session runs in gets a row, captured and unlisted, as 0.8.0 did (#139).

    Read from the config file alone, like ``[fleet]``: there is no
    ``AISQUARE_PROJECTS_*`` variable.
    """

    capture: bool = False


class RedactionSettings(BaseModel):
    """Settings controlling how captured data is scrubbed."""

    level: RedactionLevel = RedactionLevel.standard


class ExplainabilityTarget(BaseModel):
    """One explainability deployment (stg, prod, …) this machine can point at.

    Every field falls back to the top-level ``[explainability]`` default when
    unset, so a target is usually two lines — except the gateway and proxy of a
    project's own deployment (a destination's, #142), which the machine's never
    stand in for. ``api_key_env`` is a *key source*,
    never a key: the CLI reads the named environment variable at the moment it
    needs it. No path to a secrets file is ever baked into config or source —
    the operator sources their own file into the shell (or exports the var by
    any other means) and the CLI just names what it needs.
    """

    gateway_url: str = ""
    api_key_env: str = "EXPLAINABILITY_API_KEY"
    studio_id: str = ""  # informational; the gateway routes by key + agent name
    proxy_url: str | None = None
    agent_name_template: str | None = None
    roles: list[str] | None = None


class ExplainabilitySettings(BaseModel):
    """Settings for tracing agent sessions through the explainability proxy.

    ``enabled`` is False until the stg pipeline is verified green for this
    team — flipping it on is the only opt-in, and every other safeguard
    (proxy health probe, mode check, pre-existing env detection) fails open:
    a session always launches, at worst untraced with a warning.

    ``targets`` carries one entry per deployment, so moving this machine from
    staging to production is a config edit (or ``aisquare explainability enable
    --target prod``) rather than a code change::

        [explainability]
        enabled = true
        target  = "stg"

        [explainability.targets.stg]
        gateway_url = "https://stg-explainability-api.example"

        [explainability.targets.prod]
        gateway_url = "https://explainability-api.example"
        api_key_env = "EXPLAINABILITY_PROD_API_KEY"

    The selector is deliberately NOT called "profile": ``aisquare --profile``
    already means the top-level config profile, and one word for two unrelated
    selectors is how an operator points production traffic at staging.
    Two independent lanes share this section, and either may run without the
    other. ``enabled`` governs the PROXY lane: model traffic from a launched
    agent, routed via ``ANTHROPIC_BASE_URL``. ``ship`` governs the CLIENT lane:
    the insights the CLI itself holds — human prompts and board events — which
    no proxy can see because they never touch the model API. They meet at the
    gateway, in one Run per session, because both key on the board session id.

    ``ship`` is the single predicate on the primary path, so it carries the
    whole "is this configured" question: it is only ever written True once a
    gateway URL and a usable key both exist. No key or no config therefore
    means nothing is captured at all — not captured-then-discarded.

    The key itself is NOT here. ``config.toml`` is a readable settings file
    people paste into issues; a workspace credential lives in
    ``~/.aisquare/explainability-key`` at mode 600, or in
    ``EXPLAINABILITY_API_KEY``.
    """

    enabled: bool = False
    proxy_url: str = "http://127.0.0.1:9090"
    agent_name_template: str = "aisquare-{role}"
    target: str = "stg"
    # Every role the harness can launch, so every identity the fleet can emit is
    # registered (docs/plans/fleet-tui.md §3.3). The first three keep their order:
    # the runbooks quote them as a list.
    roles: list[str] = Field(
        default_factory=lambda: [
            "planner",
            "coder",
            "runner",
            "manager",
            "tester",
            "reviewer",
            "validator",
            "ui-tester",
        ]
    )
    targets: dict[str, ExplainabilityTarget] = Field(default_factory=dict)
    ship: bool = False
    gateway_url: str = ""


class ExperimentSettings(BaseModel):
    """Settings for the Collective Intelligence test bed.

    ``enabled`` is False, and stays False for everyone who has not deliberately
    opted in. That is not caution about a half-built feature: the
    ``prompt_submit`` call runs *synchronously* in front of a developer who has
    just hit enter, so the off state has to cost exactly nothing — no request,
    no connection, no measurable latency. Shipping in that state is what makes
    the client safe to land on ``main`` while the endpoint is still being
    built, instead of accruing on a branch.

    ``url`` is the server's base URL; ``run`` is the ``run_…`` id whose delivery
    descriptor drives this machine's sessions. There is deliberately nothing
    else: which hooks call the server and whether the recall tool is exposed
    are decided by the descriptor the server publishes, not by a flag here —
    a client-side switch would be a second place the experiment's shape lives.
    ``AISQUARE_CI``, ``AISQUARE_CI_URL`` and ``AISQUARE_CI_RUN`` override each
    field from the environment.

    There is deliberately no key field. The bearer token is read from
    ``AISQUARE_CI_KEY`` and nowhere else: ``config.toml`` is a file people
    diff, paste into issues and copy between machines, and a secret that lives
    there leaks by being ordinary.
    """

    enabled: bool = False
    url: str = ""
    run: str = ""


class ExperimentalSettings(BaseModel):
    """``[experimental]`` — features that ship OFF until the owner turns them on.

    ``captain``: the home-level agent (``docs/captain.md``). Off, ``aisquare captain``
    refuses, the fleet UI shows no insignia or captain view, the bundled ``captain``
    persona is absent and the voice page does not serve. ``AISQUARE_EXPERIMENTAL_CAPTAIN``
    wins over this field either way; ``core.experimental.captain_enabled`` is the one
    reader of both.
    """

    captain: bool = False


class RoleLaunchProfile(BaseModel):
    """One role's launch spec, carried verbatim and never interpreted.

    ``bin`` is the executable, ``env`` the variables to set, ``args`` extra
    arguments appended to the command. Values in ``env`` get ``~`` and ``$VAR``
    expanded at launch so they read exactly like the shell line they replace::

        [team.profiles.coder1]
        bin = "claude"
        args = ["--model", "opus"]

        [team.profiles.coder1.env]
        CLAUDE_CONFIG_DIR  = "$HOME/.claude2"
        CLAUDE_CODE_TMPDIR = "$HOME/.cache/claude2"

    Nothing here knows what any of those variables MEAN, which is the point.
    An earlier cut understood "accounts" and expanded a bare name into a pair
    of directories — one operator's convention baked into a tool with no
    business knowing it, unusable by anyone laid out differently and liable to
    break for its author the day they reorganised. The operator states the
    spec; we carry it.

    ``account`` is the ONE exception, and it is not that cut coming back: it
    names a Claude account the CLI itself owns — a slot number, an alias or the
    email it is signed in as (``aisquare accounts``) — never a path. It is
    resolved at launch by the one account resolver
    (``services.claude_accounts.choose``), after ``env``, so a binding that
    carries both a hand-written ``CLAUDE_CONFIG_DIR`` and an ``account`` runs on
    the account. Written by ``team bind <role> --account`` and App settings;
    ``None`` means the role expresses no preference and the project or machine
    default applies (#145).

    ``env_files`` is ``env`` for a value that is a secret: each maps a variable to a FILE
    holding its value, read at every launch (its text, trailing newlines dropped), so
    config.toml, the launch banner and ``team spawn``'s printed command never hold it.
    Still nothing here knows what the variable means. The reviewer's ``GH_TOKEN``, a
    token that cannot write the repository's contents, is what it is for (docs/fleet.md);
    a proxy's key is another. A file that cannot be read, or holds nothing, refuses the
    launch: started without its value, a reviewer would run on this shell's own
    credentials, and a blank one is a different login. A variable named in both maps
    takes the file's value; ``team bind`` keeps them apart.
    """

    bin: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    env_files: dict[str, str] = Field(default_factory=dict)
    args: list[str] = Field(default_factory=list)
    account: str | None = None

    @property
    def carries_launch(self) -> bool:
        """Whether it binds anything but its account: a writer that takes the account away
        drops the table when this is false, rather than leave an empty one that
        ``launch._declared_roles`` reads as a role the operator declared."""
        return bool(self.bin or self.env or self.env_files or self.args)


class TeamSettings(BaseModel):
    """Per-role launch settings for ``aisquare team``.

    ``profiles`` maps a role to its full launch spec — see
    :class:`RoleLaunchProfile`. This is the general mechanism: it covers
    parallel agent installs, wrapper scripts, proxies, regions, or any other
    knob, without this file learning about any of them.

    ONE map on purpose. #52 landed a narrower ``bins`` (role → executable)
    beside it — a strict subset of ``profiles.<role>.bin``, so two homes for
    one concept and a precedence rule every reader had to carry. It was
    deleted rather than deprecated because #52 is unreleased: no config file
    anywhere holds a ``bins`` key, and a hand-written one still loads, because
    unknown keys are ignored (see the module docstring).

    Resolution order is flag > env > profile > default; see
    ``aisquare.core.harness.resolve_binary`` and ``resolve_profile``.
    """

    profiles: dict[str, RoleLaunchProfile] = Field(default_factory=dict)


CLAUDE_PERMISSION_MODES: tuple[str, ...] = (
    "auto",
    "acceptEdits",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)
"""Claude Code's ``--permission-mode`` values (2.1.250): the Settings page offers them,
and ``fleet restart --permission-mode`` takes nothing else, or ``""`` for no flag."""


class FleetRoleSettings(BaseModel):
    """How the fleet launches one role — every field is a DEFAULT the user may change.

    ``permission_mode`` is any Claude Code ``--permission-mode`` value (``auto``,
    ``acceptEdits``, ``bypassPermissions``, ``manual``, ``dontAsk``, ``plan``); the
    empty string means "pass no flag". ``worktree`` puts the agent in its own git
    worktree; ``extra_args`` are appended to the agent command verbatim.
    Precedence: per-spawn flag > this config > built-in. NOT the environment:
    no ``[fleet]`` value is read from an env var (the orchestrator's own knobs
    — ``AISQUARE_TEAM``, ``AISQUARE_MODEL_<ROLE>`` and friends — are a
    different surface, documented in the README).
    """

    permission_mode: str = "auto"
    worktree: bool = False
    extra_args: list[str] = Field(default_factory=list)
    persona: str | None = None
    """The persona every spawn of this role gets unless ``--persona`` names another
    (docs/plans/spawn-personas.md §3.8). Checked at spawn, not at load: a name the
    project does not have refuses the spawn with this key in the message."""


def _default_fleet_roles() -> dict[str, FleetRoleSettings]:
    """The six fleet roles and their built-in launch shape (docs/plans/fleet-tui.md §3.6).

    ``ui-tester`` carries no ``extra_args`` here: its ``--chrome`` is the role's own
    default (``harness.ROLE_PROFILES[...].default_args``) and is added by ``launch``
    inside the window, so it applies to a hand-started ui-tester too.

    ``reviewer`` carries :data:`REVIEWER_ARGS`. It was launched ``--restricted`` as
    "read-only by construction", and Claude Code's ``--restricted`` removes Bash
    (2.1.296: unless ``--tools`` names it, and then only the named tools remain), so
    the reviewer could run none of ``aisquare task next``, ``gh pr diff`` or ``gh pr
    review`` — every step of its cycle — and no subagent it spawned could either,
    because a subagent's tools come from its session's. It kept ``Edit`` and
    ``Write``, so it was not read-only either. It now keeps the shell and is denied
    the tools that edit files. The shell is NOT read-only: a PR that injects
    ``sed -i``, ``git push`` or ``gh pr merge`` meets only its briefing and the
    ``auto`` mode's per-action classifier (round 25 of #257). No allowlist of commands
    holds it: its inline comments are ``gh api`` POSTs, a prefix a merge shares, and
    verifying a finding runs the PR's own code. What holds against a PR that steers
    it is the credentials it runs with: a ``gh`` token that cannot write contents
    cannot push or merge, and branch protection.
    """
    return {
        "manager": FleetRoleSettings(),
        "coder": FleetRoleSettings(worktree=True),
        "tester": FleetRoleSettings(),
        "reviewer": FleetRoleSettings(worktree=True, extra_args=list(REVIEWER_ARGS)),
        "validator": FleetRoleSettings(),
        "ui-tester": FleetRoleSettings(),
    }


#: The reviewer's built-in ``extra_args``: Claude Code without the tools that edit files.
#: One token, because the flag takes a variadic list and separate words would go on
#: swallowing whatever plain word came after them on the command line; the names apart
#: by spaces, which Claude Code splits on as it does on commas, because ``config set``
#: splits a list on commas and ``config get`` joins one with them.
REVIEWER_ARGS = ("--disallowedTools=Edit Write NotebookEdit",)


#: The reviewer's built-in ``extra_args`` in 0.6.0 and 0.7.0, which left it no shell
#: (see ``_default_fleet_roles``). ``aisquare config set`` and the Settings page write
#: every role out in full, so a config file either of them saved holds this list for the
#: reviewer whether or not anyone chose it.
_FORMER_REVIEWER_ARGS = ["--restricted"]

#: The version of the config file's format this build writes, at its top as
#: ``config_version``. A file without one was saved by 0.7.0 or earlier: version 0. Each
#: step of :data:`_MIGRATIONS` brings a file from one version to the next as it loads, and
#: the next save records the version reached, so a step runs once per file: what the file
#: holds after that was written by a build that knew the step, and is kept. Not a setting,
#: and not a field. Version 2 is the first that writes only what the user changed.
CONFIG_VERSION = 2
_VERSION_KEY = "config_version"


def replacing_the_former_reviewer_default(args: Sequence[str]) -> list[str]:
    """``args``, or :data:`REVIEWER_ARGS` when they are exactly 0.6.0/0.7.0's reviewer default.

    For what those releases wrote in a config file and nobody chose
    (:func:`_replace_the_former_reviewer_default`): there a longer list was chosen. A
    launch spec is :func:`replacing_the_former_reviewer_default_first`'s.
    """
    return list(REVIEWER_ARGS) if list(args) == _FORMER_REVIEWER_ARGS else list(args)


def replacing_the_former_reviewer_default_first(
    args: Sequence[str], today: Sequence[str]
) -> list[str]:
    """``args`` with 0.6.0/0.7.0's reviewer default replaced by ``today`` where it leads them,
    the rest kept: for a reviewer's launch spec those releases recorded, which a restart
    replays (``services.fleet``). ``today`` is the reviewer's ``extra_args`` in today's
    config, so the restart launches with the role arguments a fresh spawn does: replaced
    by :data:`REVIEWER_ARGS` alone whatever the config said, a reviewer its operator gave
    ``--model opus`` came back without it (round 21).

    A spec records the role's ``extra_args`` followed by the caller's, so a reviewer
    spawned with arguments of its own (``fleet spawn reviewer -- --model opus``) recorded
    ``["--restricted", "--model", "opus"]``. Matched exactly, as a config file is, that
    reviewer came back with no shell on every restart (round 19 of #257).

    The spec alone cannot tell that default from a list the operator wrote in config,
    ``["--restricted", "--model", "opus"]`` too, which loading keeps as written. Today's
    config can: it has been through :func:`_replace_the_former_reviewer_default`, so a
    ``--restricted`` still leading it was chosen, and the spec is replayed as recorded.
    Replaced regardless, a reviewer its operator kept from the shell got one on restart
    while a fresh spawn did not (round 20).

    Nor can it tell the default from a ``--restricted`` the caller gave first (``fleet
    spawn reviewer -- --restricted`` under ``extra_args = []``). So where ``today`` does
    not deny the editing tools, :data:`REVIEWER_ARGS` goes in beside it: replaced by an
    empty ``today`` alone, a reviewer its operator kept locked down came back with Bash,
    Edit and Write (round 22). That errs toward the reviewer without them, which a fresh
    spawn then gives every tool ``today`` does. Whether ``today`` denies them is read off
    the tools it denies (:func:`_denied_tools`), however it spells the list: matched as
    :data:`REVIEWER_ARGS`'s one token, a list given as two words, with commas or with one
    tool more got a second ``--disallowedTools`` beside it (round 23).
    """
    args = list(args)
    if list(today[: len(_FORMER_REVIEWER_ARGS)]) == _FORMER_REVIEWER_ARGS:
        return args
    if args[: len(_FORMER_REVIEWER_ARGS)] == _FORMER_REVIEWER_ARGS:
        denied = _denied_tools(REVIEWER_ARGS) <= _denied_tools(today)
        replacement = list(today) if denied else [*today, *REVIEWER_ARGS]
        return [*replacement, *args[len(_FORMER_REVIEWER_ARGS) :]]
    return args


_DISALLOWED_TOOLS = ("--disallowedTools", "--disallowed-tools")
"""Claude Code's flag for the tools a session may not use, in both its spellings."""


def _denied_tools(args: Sequence[str]) -> set[str]:
    """The tools ``args`` deny Claude Code: each value of ``--disallowedTools``, given with
    ``=`` or as the words after the flag up to the next option, split on spaces and commas
    as Claude Code splits it."""
    denied: set[str] = set()
    taking = False
    for arg in args:
        flag, equals, value = arg.partition("=")
        if flag in _DISALLOWED_TOOLS:
            taking = not equals
            denied.update(re.split(r"[\s,]+", value))
        elif arg.startswith("-"):
            taking = False
        elif taking:
            denied.update(re.split(r"[\s,]+", arg))
    denied.discard("")
    return denied


def _replace_the_former_reviewer_default(data: dict[str, Any]) -> None:
    """A reviewer a 0.6.0 or 0.7.0 save left with exactly ``["--restricted"]`` loads with today's.

    Changing the default alone fixes nothing for a config file that already holds it,
    and every 0.6.0 or 0.7.0 save holds it. Once, not on every load: the step from
    version 0 (:data:`_MIGRATIONS`), so the old list chosen afterwards, with ``config
    set``, is kept like any other. Replaced on every load
    instead, it was impossible to choose: ``config set`` reported it saved, the fleet
    launched without it and the next save erased it (review of #257). A reviewer given
    any other list — ``--restricted`` with more beside it included — keeps that list.
    Read from the file's own data, before the model: a model validator would rewrite
    every ``AppConfig`` built in memory too, ``config set``'s among them.
    """
    fleet = data.get("fleet")
    if not isinstance(fleet, dict):
        return
    roles = fleet.get("roles")
    reviewer = roles.get("reviewer") if isinstance(roles, dict) else None
    if isinstance(reviewer, dict) and isinstance(reviewer.get("extra_args"), list):
        reviewer["extra_args"] = replacing_the_former_reviewer_default(reviewer["extra_args"])


#: Every setting at its system default in version 1 (0.8.0, and the 0.8.1 builds before
#: the layers), as those builds wrote it. Frozen here, never derived from today's models:
#: a later change of a default must not change which of a version-1 file's values the step
#: below takes for unchosen.
_V1_DEFAULTS: dict[str, Any] = {
    "profile": "default",
    "api_url": "https://api.aisquare.studio",
    "default_pool": "project",
    "capture": {"enabled": True},
    "redaction": {"level": "standard"},
    "explainability": {
        "enabled": False,
        "proxy_url": "http://127.0.0.1:9090",
        "agent_name_template": "aisquare-{role}",
        "target": "stg",
        "roles": [
            "planner",
            "coder",
            "runner",
            "manager",
            "tester",
            "reviewer",
            "validator",
            "ui-tester",
        ],
        "targets": {},
        "ship": False,
        "gateway_url": "",
    },
    "team": {"profiles": {}},
    "fleet": {
        "tmux_socket": "asq",
        "escape_key": "f12",
        "max_agents_per_project": 4,
        "worktree_dir": ".aisquare-worktrees",
        "disable_native_agent_teams": True,
        "max_continuations_per_hour": 30,
        "roles": {
            "manager": {"permission_mode": "auto", "worktree": False, "extra_args": []},
            "coder": {"permission_mode": "auto", "worktree": True, "extra_args": []},
            "tester": {"permission_mode": "auto", "worktree": False, "extra_args": []},
            "reviewer": {
                "permission_mode": "auto",
                "worktree": True,
                "extra_args": ["--disallowedTools=Edit Write NotebookEdit"],
            },
            "validator": {"permission_mode": "auto", "worktree": False, "extra_args": []},
            "ui-tester": {"permission_mode": "auto", "worktree": False, "extra_args": []},
        },
        "pane_clipboard": "off",
    },
    "accounts": {
        "pick": "default",
        "switch_at": 85,
        "on_limit": "wait",
        "wait_if_reset_within_minutes": 15,
    },
    "snapshot": {"max_tokens": 150_000, "ignore": []},
    "experiment": {"enabled": False, "url": "", "run": ""},
}


def _keep_only_what_v1_did_not_write_by_default(data: dict[str, Any]) -> None:
    """A file version 1 saved keeps only the values that differ from that version's defaults.

    Version 1 wrote every setting out, so its file cannot tell a value the user chose
    from one it wrote because it was the default. Taken as written, every such value
    would stay pinned when a default moves on — ``[accounts] pick = "default"`` from
    every 0.8.0 save would keep ``headroom`` from ever becoming the default. So a value
    equal to version 1's default (:data:`_V1_DEFAULTS`) is taken as unchosen and
    follows the system default from now on; one that differs is the user's and stays.
    A user who had chosen exactly the old default gets the new one: the file cannot say
    otherwise. Keys version 1 did not know, and entries the user added to a mapping,
    stay as written. The next save writes what is left (:func:`save_config`).
    """
    kept = _overrides(data, _V1_DEFAULTS)
    data.clear()
    data.update(kept)


#: The steps that bring a config file to :data:`CONFIG_VERSION`, in order: the one at
#: index ``n`` takes a parsed file from version ``n`` to ``n + 1``, in place.
_MIGRATIONS: tuple[Callable[[dict[str, Any]], None], ...] = (
    _replace_the_former_reviewer_default,
    _keep_only_what_v1_did_not_write_by_default,
)


#: The top-level keys every release up to 0.8.0 wrote into the file whatever their value,
#: and that this build writes only when changed: a file written whole holds all three.
_WRITTEN_WHOLE_BY = ("profile", "api_url", "default_pool")


def _written_whole(data: dict[str, Any]) -> bool:
    """Whether ``data`` is a file a release before the layers saved, every setting written out.

    Such a release keeps a higher ``config_version`` it finds (it does not know the
    version's steps, and must not undo them), so a version-2 file one of them saved again
    — an older install sharing ``~/.aisquare``, or a move back and forth — says 2 while
    holding every value that release's defaults gave it. A version cannot record that, nor
    can a marker this build writes: those releases keep a key they do not know. The
    file's values can. Those releases always wrote ``profile``, ``api_url`` and
    ``default_pool``, and every setting the user left alone at version 1's default. This
    build never writes a value at its own default, outside the role tables it writes whole
    (:func:`_with_whole_role_tables`).

    Both, then: the three keys, and a value at a default version 1 had and this build
    keeps. The three keys alone were not enough: a user who set all three away from their
    defaults had every save of theirs read as whole, and an ``accounts.pick = "default"``
    or ``on_limit = "wait"`` they chose dropped on every load (review of #259, round 13).
    Compared with version 1's defaults as well as this build's, a later build that moves a
    default does not read this build's choice of the new one as a sign of a whole file.
    """
    if not all(key in data for key in _WRITTEN_WHOLE_BY):
        return False
    then, today = _flat(_V1_DEFAULTS), _flat(system_defaults())
    return any(
        key in then and key in today and value == then[key] == today[key]
        for key, value in _flat(data).items()
        if not key.startswith("fleet.roles.")
    )


def _migrate(data: dict[str, Any]) -> None:
    """Bring a parsed config file to :data:`CONFIG_VERSION`, in place: every step of
    :data:`_MIGRATIONS` from the file's own version on. The version is taken out, not
    being a setting.

    A file at version 2 or later that an older release has saved again whole
    (:func:`_written_whole`) takes the version-1 step once more: taken as written, that
    release's defaults — ``pick = "default"``, ``on_limit = "wait"`` — would read as the
    user's own choices from then on (review of #261, M2). The next save writes it sparse.
    """
    version = data.pop(_VERSION_KEY, None)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        version = 0
    for step in _MIGRATIONS[version:]:
        step(data)
    if version >= len(_MIGRATIONS) and _written_whole(data):
        _keep_only_what_v1_did_not_write_by_default(data)


def _versioned(dumped: dict[str, Any]) -> dict[str, Any]:
    """``dumped`` as a file this build writes: ``config_version`` at its top.

    A higher version, which a newer build wrote and ``_keep_unknown`` kept, stays: that
    build's steps have run on the file, and must not run again on what this one saved.
    """
    kept = dumped.pop(_VERSION_KEY, None)
    newer = isinstance(kept, int) and not isinstance(kept, bool) and kept > CONFIG_VERSION
    return {_VERSION_KEY: kept if newer else CONFIG_VERSION, **dumped}


class FleetSettings(BaseModel):
    """The fleet: one private tmux server, one session per project, agents as windows.

    ``tmux_socket`` names the private server (``tmux -L``); ``escape_key`` is the
    key that hands focus from an embedded agent pane back to the sidebar;
    ``worktree_dir`` is relative to the repo root and kept out of git through
    ``.git/info/exclude``; ``disable_native_agent_teams`` exports
    ``CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=0`` into fleet launches so a manager
    cannot spawn help the sidebar cannot see; ``max_continuations_per_hour`` caps
    how often the manager's Stop hook may keep it going on new board events.
    """

    tmux_socket: str = "asq"
    escape_key: str = "f12"
    max_agents_per_project: int = 4
    worktree_dir: str = ".aisquare-worktrees"
    disable_native_agent_teams: bool = True
    max_continuations_per_hour: int = 30
    roles: dict[str, FleetRoleSettings] = Field(default_factory=_default_fleet_roles)
    pane_clipboard: Literal["off", "on"] = "off"
    """Whether a program in an agent's pane may set your clipboard (OSC 52, tmux's
    ``set-clipboard``). Off by default: on, any program a pane runs can write it. Applies
    to a server started after the change."""


class SnapshotSettings(BaseModel):
    """The codebase snapshot ``aisquare project onboard`` packs with Repomix.

    ``max_tokens`` is the budget a pack must fit. The full pack is tried first,
    then the compressed one (signatures only); a repo whose compressed pack is
    STILL over it keeps that pack as the skeleton with its per-file index —
    status ``skeleton_only``, every count recorded, ``onboard`` and ``doctor``
    naming the numbers — rather than getting nothing. 150 000 mirrors the
    server's ``REPO_PACK_MAX_TOKENS`` so artifacts stay consistent for a future
    sync — raise it for a repo you know is big, or leave more out, with
    ``ignore`` below or a ``.repomixignore`` at the repo root (#82).

    ``ignore`` is the operator's own list of what to leave out of the pack, in
    the glob syntax repomix's ``--ignore`` takes (``**/fixtures/**``,
    ``docs/generated/**``, ``*.snap``). It EXTENDS the built-in list
    (``core.snapshot.DEFAULT_IGNORE``: dependency trees, build output, caches,
    this tool's worktrees, and any nested git repository or worktree found
    below the root) rather than replacing it, and the repo's own ``.gitignore``
    and ``.repomixignore`` apply on top, read by repomix itself. On the command
    line the items are comma-separated, as repomix's own flag is::

        aisquare config set snapshot.ignore '**/fixtures/**,docs/generated/**'

    Read from the config file alone, like ``[fleet]``: there is no
    ``AISQUARE_SNAPSHOT_*`` variable for either key, because this layer has no
    per-key environment rung and one section is not the place to grow one.
    """

    max_tokens: int = 150_000
    ignore: list[str] = Field(default_factory=list)


class PersonaImportSettings(BaseModel):
    """``[persona.import]`` — how ``aisquare persona import`` converts a source that is
    not already a skill (docs/plans/spawn-personas.md §3.9).

    ``engine``: ``auto`` tries the manager engine (headless Claude Code under the
    manager role's binding), then the api engine (the ``anthropic`` SDK,
    ``aisquare-cli[llm]``); ``manager`` or ``api`` forces one; ``off`` refuses every
    conversion, for a machine that must never spend a token by accident.
    ``api_model`` is the api engine's model — the manager engine rides the manager's
    model ladder. No key lives here: the SDK resolves credentials its own way.
    """

    engine: Literal["auto", "manager", "api", "off"] = "auto"
    api_model: str = "claude-opus-5"


class PersonaSettings(BaseModel):
    """``[persona]``. ``import`` is a Python keyword, so the field is ``import_`` and
    the file's key is ``import`` — read and written under that name."""

    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    import_: PersonaImportSettings = Field(default_factory=PersonaImportSettings, alias="import")
    #: The persona block is appended to the agent's system prompt at launch too (Claude
    #: Code: ``--append-system-prompt-file``), beside the session-start briefing that
    #: survives ``/clear``, so it holds over a long session as hook context alone may
    #: not (docs/plans/spawn-personas.md §9, gh #210). ``false`` keeps the briefing
    #: alone and appends nothing — for a session whose system prompt must stay exactly
    #: the binary's own. A binary without a seam this launcher knows (codex, aider) gets
    #: the briefing alone either way, and the launch says so. Owner decision, 2026-09-24.
    system_prompt: bool = True


class AccountsSettings(BaseModel):
    """How the fleet spends several Claude accounts (#146); every field is a changeable default.

    ``pick`` is how a launch chooses an account when nothing names one (no
    ``--account``, no role binding, no project default): ``headroom``, the
    default, reads each enabled, signed-in account's
    usage and takes, in priority order, the first one under ``switch_at``
    percent of the fuller of its two windows (five-hour and weekly) — or, when
    every account is over it, the one with the most room left; ``default`` takes
    the machine default (#145). Usage is the
    undocumented endpoint Claude Code's own
    ``/usage`` reads (docs/plans/claude-accounts.md §5), so ``headroom`` is best
    effort: an account whose usage cannot be read is skipped with a note, and
    when none can be read the machine default decides as before.

    ``on_limit`` is what the fleet does when an agent's turn ends on a usage
    limit (Claude Code's ``StopFailure`` hook, error ``rate_limit``): ``wait``
    marks the row ``limited`` and wakes the manager, and leaves Claude Code's own
    wait-and-continue-at-reset in place; ``switch``, the default, also hands the
    agent over — ``aisquare fleet switch`` — to the account with the most headroom,
    unless the limit resets within ``wait_if_reset_within_minutes`` (a reset ten
    minutes away is cheaper than a cold start elsewhere). With no other enabled,
    signed-in account to go to, ``switch`` is ``wait``
    (``services.claude_accounts.somewhere_to_hand_over``). ``switch_at`` doubles as
    the line the Accounts page colours red and ``doctor --live`` warns at.

    Both defaults were ``default`` and ``wait`` through 0.8.0; a file those builds
    saved keeps either only where it differs from them (``_V1_DEFAULTS``).

    ``share_trust``, on by default, copies a folder's trust into the account a launch
    runs on when any other account has it, so an agent never waits at Claude Code's
    "trust this folder?" for a folder the human already said yes to on another login.
    ``share_settings``, off by default, keeps every managed account's ``settings.json``
    a link to the plain claude's. Both: ``core.claude_sharing``.

    Read from the config file alone, like ``[fleet]``: there is no
    ``AISQUARE_ACCOUNTS_*`` variable, for the same reason that section gives.
    """

    pick: Literal["default", "headroom"] = "headroom"
    switch_at: int = Field(default=85, ge=1, le=100)
    on_limit: Literal["wait", "switch"] = "switch"
    wait_if_reset_within_minutes: int = Field(default=15, ge=0)
    share_trust: bool = True
    share_settings: bool = False


class AppConfig(BaseModel):
    """Root configuration object persisted at ``~/.aisquare/config.toml``."""

    profile: str = "default"
    api_url: str = "https://api.aisquare.studio"
    default_pool: Pool = "project"
    capture: CaptureSettings = Field(default_factory=CaptureSettings)
    projects: ProjectsSettings = Field(default_factory=ProjectsSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    explainability: ExplainabilitySettings = Field(default_factory=ExplainabilitySettings)
    team: TeamSettings = Field(default_factory=TeamSettings)
    fleet: FleetSettings = Field(default_factory=FleetSettings)
    accounts: AccountsSettings = Field(default_factory=AccountsSettings)
    snapshot: SnapshotSettings = Field(default_factory=SnapshotSettings)
    experiment: ExperimentSettings = Field(default_factory=ExperimentSettings)
    experimental: ExperimentalSettings = Field(default_factory=ExperimentalSettings)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)


def _dumped(model: BaseModel) -> dict[str, Any]:
    """``model`` as a config file holds it: without ``None``, which TOML cannot write."""
    return model.model_dump(mode="json", exclude_none=True)


def system_defaults() -> dict[str, Any]:
    """Every setting at its system default, nested as ``config.toml`` would hold it.

    The models' field defaults are the one place a default lives; this is them dumped.
    """
    return _dumped(AppConfig())


def overrides(config: AppConfig) -> dict[str, Any]:
    """What of ``config`` differs from the system defaults: what a save writes of it."""
    return _overrides(_dumped(config), system_defaults(), config)


def _overrides(
    values: dict[str, Any], defaults: dict[str, Any], model: Any = None
) -> dict[str, Any]:
    """What of ``values`` differs from ``defaults``, table by table.

    A key at its default is left out, at any depth, and a table left with nothing is
    left out with it; a list is one value, compared whole. A key ``defaults`` has no
    entry for stays: a key this build does not know, or an entry the user added to a
    mapping (``[team.profiles.<role>]``, ``[explainability.targets.<name>]``), whose
    being there is itself a setting. Such an entry is kept even when empty, holding
    only what differs from its model's defaults when ``model`` (what ``values`` was
    dumped from) says which model that is; without one, as for a file's own data
    (:func:`_keep_only_what_v1_did_not_write_by_default`), it is kept whole.
    """
    kept: dict[str, Any] = {}
    for key, value in values.items():
        sub = model.get(key) if isinstance(model, dict) else getattr(model, key, None)
        if key in defaults:
            default = defaults[key]
            if isinstance(value, dict) and isinstance(default, dict):
                nested = _overrides(value, default, sub)
                if nested:
                    kept[key] = nested
            elif value != default:
                kept[key] = value
        elif isinstance(value, dict) and isinstance(sub, BaseModel):
            try:
                entry_defaults = _dumped(type(sub)())
            except ValidationError:  # a model with a required field: nothing to diff against
                kept[key] = value
            else:
                kept[key] = _overrides(value, entry_defaults, sub)
        else:
            kept[key] = value
    return kept


def _with_whole_role_tables(sparse: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    """``sparse``, with every ``[fleet.roles.<role>]`` table of ``full`` written whole.

    A release before the layers (0.8.0 and earlier) reads the file as it stands, not over
    the defaults. Given one key of one role, it took ``fleet.roles`` to be that role
    alone: the reviewer launched there without ``--disallowedTools`` and every coder in
    the main checkout (review of #261, M2). Whole, every role reads the same in either
    build; everything else stays sparse, where an older build's own defaults apply. The
    price: a later change to a role's default reaches a saved file only through a step
    of :data:`_MIGRATIONS`, as the reviewer's did.
    """
    fleet = full.get("fleet")
    roles = fleet.get("roles") if isinstance(fleet, dict) else None
    if not isinstance(roles, dict) or not roles:
        return sparse
    whole = dict(sparse)
    whole["fleet"] = {**sparse.get("fleet", {}), "roles": roles}
    return whole


def _flat(values: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """``values`` as dotted keys to their leaf values: ``{"accounts.pick": "headroom", …}``."""
    flat: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, dict) and value:
            flat.update(_flat(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def defaults_a_whole_file_reader_lacks(config: AppConfig) -> list[tuple[str, Any, Any]]:
    """The settings ``config`` takes from a system default that version 1 had otherwise:
    ``(key, today's default, version 1's)`` for each, in the file's order.

    What a release that reads the file without the defaults under it (0.8.0 and earlier)
    reads differently from a file this build saved: the file says nothing of them, so
    that release applies its own default (``accounts.pick``: ``default``, not
    ``headroom``). A value the user set is in the file and reads the same in both.
    """
    today, then, ours = _flat(system_defaults()), _flat(_V1_DEFAULTS), _flat(_dumped(config))
    return [
        (key, value, then[key])
        for key, value in today.items()
        if key in then and then[key] != value and ours.get(key) == value
    ]


def holds_a_setting(path: Path | None = None) -> bool:
    """Whether the file at ``path`` (default: the standard location) holds anything a reset
    would undo: a value that is not the system default, or a key this build does not know.

    Not whether a file is there: every save leaves one, holding at least its
    ``config_version`` and the whole role tables, and a reset of that kept a backup of
    nothing and said the user's settings were in it (review of #261, m8). A file that
    cannot be read holds something, and resetting it is the repair.
    """
    target = Path(os.path.realpath(path or paths.config_path()))
    try:
        data = _parse_toml(despite_windows_contention(target.read_bytes))
    except FileNotFoundError:
        return False
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return True
    _migrate(data)
    return bool(_overrides(data, system_defaults()))


def _overlay(defaults: Any, layer: Any) -> Any:
    """``layer`` laid over ``defaults``: tables merge key by key, any other value replaces.

    Key by key into a mapping too, which the models' own defaults would not do: a file
    that changes one key of ``[fleet.roles.coder]`` holds that key alone, and validated
    as it stands it would leave the coder with a plain role's defaults and the fleet with
    no other role.
    """
    if not isinstance(defaults, dict) or not isinstance(layer, dict):
        return layer
    return {**defaults, **{key: _overlay(defaults.get(key), v) for key, v in layer.items()}}


def _keep_unknown(existing: Any, dumped: Any, model: Any) -> Any:
    """``dumped``, plus any key in ``existing`` that ``model`` has no field for.

    A build whose model lacks a field discards it on load and cannot write it
    back, so a config written by a NEWER build and then saved by an OLDER one
    loses everything the older one never heard of. Measured on this machine: a
    build with a three-field ``ExplainabilitySettings`` erased ``target``,
    ``roles``, ``ship``, ``gateway_url`` and ``[explainability.targets]`` —
    exit 0, no warning, and because the tracing seam is fail-open the result is
    a green-looking machine with no tracing.

    The model stays AUTHORITATIVE for everything it knows, including absence:
    unsetting a role binding has to actually remove it, so a key the model has
    a field for is taken from ``dumped`` or not at all. Only keys with no
    corresponding field survive from the file. Recursion follows the model, so a
    field added inside an existing section is preserved too — which is the shape
    the harm actually took, all five lost keys being sub-keys of a section both
    builds knew about.

    A field whose value is a MAPPING of sub-models (``targets: dict[str,
    Target]``, ``[team.profiles.<role>]``, ``[fleet.roles.<role>]``) keeps two
    rules apart. Its keys are data, so the model owns WHICH entries exist: a
    removed target or role stays removed, and a stale entry is a stale
    deployment, not an unknown field. But every entry the model kept is still a
    model, and an unknown field INSIDE it survives like any other. The mapping
    used to be replaced wholesale, so any save from this build erased what
    other builds keep in those entries — ``[team.profiles.coder].agent`` (#113),
    ``[fleet.roles.coder].persona`` (#201) — while an unknown top-level section
    beside them survived (final review of #203, store F2). The recursion is
    #201's, so the two builds agree on it. A mapping of plain values has
    nothing to recurse into and is the model's.
    """
    if not isinstance(existing, dict) or not isinstance(dumped, dict):
        return dumped
    if isinstance(model, dict):
        return {
            key: _keep_unknown(existing.get(key), value, model.get(key))
            for key, value in dumped.items()
        }
    fields = getattr(type(model), "model_fields", None)
    if not fields:
        return dumped
    # A field is known by its file key: the alias when it has one (``[persona.import]``
    # is the field ``import_``), else its name.
    by_key = {info.alias or name: name for name, info in fields.items()}
    merged = dict(dumped)
    for key, value in existing.items():
        if key not in by_key:
            merged.setdefault(key, value)
        elif key in dumped:
            merged[key] = _keep_unknown(value, dumped[key], getattr(model, by_key[key], None))
    return merged


#: The byte-order marks UTF-16 opens with, little-endian first: Windows PowerShell
#: 5.1's ``>``, ``Out-File`` and ``Set-Content -Encoding Unicode`` write ``FF FE``.
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def decode_by_bom(raw: bytes, *, errors: str = "strict") -> str:
    """``raw`` decoded by the byte-order mark it opens with: UTF-16 after a UTF-16 mark, else
    UTF-8 with any UTF-8 mark dropped (:func:`_parse_toml` says why). For any text file a
    human may have saved on Windows: ``services.ngrok_tunnel`` reads ngrok's config with it.
    """
    return raw.decode("utf-16" if raw.startswith(_UTF16_BOMS) else "utf-8-sig", errors)


def _parse_toml(raw: bytes) -> dict[str, Any]:
    """The TOML document in ``raw``, decoded by the byte-order mark it opens with.

    ``tomllib`` refuses a leading U+FEFF ("Invalid statement (at line 1,
    column 1)"), so a config saved by Windows PowerShell 5.1's ``Set-Content
    -Encoding UTF8`` or Notepad's "UTF-8 with BOM" made ``load_config`` raise
    for every command, and ``save_config``'s merge read dropped the unknown
    keys it exists to keep. Decoded as ``utf-8-sig``, as ``core.credentials``
    and ``core.state_file`` read theirs; a file without a BOM reads exactly as
    before (review of the #203 store fixes, round 1).

    A file that opens with a UTF-16 mark is decoded as UTF-16. PowerShell 5.1's
    ``>`` and ``Out-File`` write that, and ``load_config`` refused it, so
    ``init --reinit`` could not see the explainability section in it, took it
    for a file with nothing to protect, and replaced it with the defaults
    without the refusal a readable one gets (review of the #203 final-review
    fixes). Read, it is protected like any other, and a save writes it back as
    UTF-8. Anything else that is not UTF-8 still raises ``UnicodeDecodeError``,
    as ``tomllib.load`` did: ``load_config`` reports it, and ``save_config``
    will not write over it unless told to discard it.
    """
    loaded: dict[str, Any] = tomllib.loads(decode_by_bom(raw))
    return loaded


_ONE_LOAD: contextvars.ContextVar[dict[Path, AppConfig] | None] = contextvars.ContextVar(
    "_ONE_LOAD", default=None
)


@contextlib.contextmanager
def one_load() -> Iterator[None]:
    """Within it, :func:`load_config` reads and parses each file once, for readers that only
    read: Welcome's probe, every two seconds, loaded config.toml once for the agent binding
    and again for the coders' folder (round 25 of #257). A load that fails is not kept.
    Never across uses."""
    token = _ONE_LOAD.set({})
    try:
        yield
    finally:
        _ONE_LOAD.reset(token)


_LAST_PARSE: tuple[Path, bytes, AppConfig] | None = None
"""The last file :func:`load_config` parsed: its path, its bytes, and what they validated to,
which no caller that may change it is handed (each gets a copy; ``copy=False`` reads it)."""


def load_config(path: Path | None = None, *, copy: bool = True) -> AppConfig:
    """Load configuration from ``path`` (default: the standard location).

    The file is laid over the system defaults (:func:`_overlay`): what it sets wins, and
    whatever it leaves out is the default. A missing file yields the defaults alone.
    Within :func:`one_load`, a file loaded already is not read again.

    Parsed again only when its bytes change (:func:`_parsed`). ``copy=False`` for a reader
    that only reads, and never changes or saves what it is handed: the kept parse itself,
    not a deep copy of it. ``capture_on()`` copied the whole model for one bool, several
    times a session start and on asq's every refresh tick (review of #259, round 19).

    :func:`one_load` keeps a copy of a ``copy=False`` load, never the kept parse: it hands
    what it keeps to every later load in its block, ``copy=True`` ones too, and a caller
    there that changed its config changed the kept parse, which every later load of the
    same bytes, in any caller, then copied (round 20).
    """
    target = path or paths.config_path()
    loaded = _ONE_LOAD.get()
    if loaded is not None and target in loaded:
        return loaded[target]
    if not target.exists():
        config = AppConfig()
    else:
        config = _parsed(target, despite_windows_contention(target.read_bytes), copy=copy)
    if loaded is not None:
        loaded[target] = config if copy else config.model_copy(deep=True)
    return config


def _parsed(target: Path, raw: bytes, *, copy: bool = True) -> AppConfig:
    """``raw``, the bytes of the config file at ``target``, as an :class:`AppConfig` the caller
    may change: a copy of the last parse when the bytes are that parse's, else parsed,
    migrated and validated, and kept. Without ``copy``, the kept parse, which the caller
    must not change.

    asq's refresh tick asked the file for ``[projects] capture``, the fleet's settings once
    per project, the roles' and the accounts' settings, and each ask read, parsed and
    validated it whole: about 250 µs for a 1.3 KB config, where reading it and copying the
    kept model is about 45 µs (review of #259, round 18). Keyed by the bytes, not by
    ``stat``: a file rewritten in place to the same size within one tick of the kernel's
    coarse clock has the same inode, size and mtime, and Remote's ``remote.json`` measured
    195 same-size rewrites of 200 sharing one
    (:meth:`aisquare.services.remote_server.Runtime.reload_if_changed`). A copy,
    because callers change what they are handed and save it. A file that will not parse
    raises, and nothing is kept: the next load reads it again.
    """
    global _LAST_PARSE
    last = _LAST_PARSE
    if last is not None and last[0] == target and last[1] == raw:
        return last[2].model_copy(deep=True) if copy else last[2]
    data = _parse_toml(raw)
    _migrate(data)
    config = AppConfig.model_validate(_overlay(system_defaults(), data))
    _LAST_PARSE = (target, raw, config.model_copy(deep=True) if copy else config)
    return config


def save_config(
    config: AppConfig, path: Path | None = None, *, discard_unreadable: bool = False
) -> Path:
    """Write ``config`` as TOML to ``path`` (default: the standard location).

    Parent directories are created on demand. Returns the written path.

    **Only what differs from the system defaults is written** (:func:`overrides`): the
    file is the user's layer over the defaults, so a value nobody changed is not in it
    and follows the default when that changes, and a config at every default writes a
    file with nothing but its ``config_version``. Up to version 1 every setting was
    written out, and nothing could tell a user's choice from a default afterwards.

    **A file already there that cannot be read is not written over** unless
    ``discard_unreadable`` says to: its read error is raised and the file is
    left as it was. The save keeps what the file holds that ``config`` does
    not know (:func:`_keep_unknown`), and a file it cannot read could hold
    anything, a configured explainability section included, which nothing
    would report missing afterwards. ``init --reinit --yes`` is the one caller
    that passes it: the operator asked for the defaults over whatever is
    there. Every other writer loads the file first, and ``load_config``
    raises on the same file.

    ``exclude_none`` because **TOML has no null**: ``tomli_w`` raises
    ``TypeError`` on ``None`` rather than writing anything, so an optional
    field left unset would make the whole file unwritable. Omitting the key is
    also the correct round-trip — it reloads as the model's default, which is
    the ``None`` we dropped.

    **The write is atomic and follows symlinks.** Both are properties callers
    depend on, and both were previously discovered rather than read, which is why
    they are stated here rather than only commented at the code:

    - The new content goes to a temp file beside the destination and arrives by
      ``os.replace``, so a concurrent reader sees the whole old file or the whole
      new one, never a partial document. The parent directory is flushed after
      the rename so the change survives a hard kill.
    - If ``path`` is a SYMLINK, the file it points at is written and the link is
      preserved. ``os.replace`` swaps the NAME it is given, so without resolving
      it would replace the link with a regular file — and severing a dotfiles
      link is silent in the way that costs most: the tracked file keeps its old
      contents, ``git status`` shows nothing, and the next machine sync restores
      settings that stopped being live.

    Following the link means a write needs permission on the REAL file's
    directory. When it does not have it, this raises and the error names the
    resolved path — not the link the caller passed — because otherwise a
    permission error points at a directory that is perfectly writable. Nothing
    is modified on that path: the link and the original file both survive.

    **A BROKEN link is followed too, and its missing directories are created.**
    Measured: a link at a path four levels deep that does not exist yet produces
    all four directories plus the file, exit 0, and the link then resolves. That
    is what makes a dangling link work rather than fail — but FOLLOWING A LINK
    AND CREATING WHAT IT POINTS AT ARE TWO DECISIONS, and only the first is ours
    to take from a link. Following honours intent the user stated; materialising
    a tree they never created invents it, at a location no command named and
    possibly on another filesystem — a broken link into a mounted Windows drive
    would have this function create directories there, silently.

    So a link whose target DIRECTORY is missing raises instead, naming that
    directory and the remedies. A link into a directory that EXISTS is written
    normally: after a fresh clone only the file is missing, nothing is invented,
    and that case keeps working. ``aisquare doctor`` flags a missing target, so
    the state is visible before a write meets it.

    This restriction applies ONLY when a link was followed. An ordinary first
    write still creates ``~/.aisquare`` — that path never involved a pointer to
    somewhere else.

    Returns the path the CALLER asked for, not the resolved one: commands echo it
    back to an operator, and where the bytes physically land is not their concern.
    """
    target = path or paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    written = Path(os.path.realpath(target))
    if written != target and not written.parent.exists():
        # A link was followed and its destination directory is not there. Create
        # it and we would be inventing a tree at a path no command named; the
        # only honest thing left is to say so. The state is already broken from
        # the user's point of view — their link does not resolve — so nothing
        # that works today stops working, which is what makes this refusal
        # affordable on a seam that must not gain failures.
        raise FileNotFoundError(
            errno.ENOENT,
            f"{target} is a symlink to {written}, but its directory "
            f"{written.parent} does not exist. Following the link is deliberate; "
            f"creating a directory tree there is not. Clone or create "
            f"{written.parent}, or repoint the link.",
            str(written.parent),
        )
    written.parent.mkdir(parents=True, exist_ok=True)

    dumped = _dumped(config)
    if written.exists():
        # Keys this build has never heard of belong to whoever wrote them; see
        # _keep_unknown. A file this read cannot parse or decode is written over
        # only when the caller says to discard it (see the docstring). Reading
        # failed open here: a config we cannot parse is the state a reset repairs.
        # But the reset is the operator's call, and failing open made it for them.
        # A config.toml PowerShell 5.1 saved as UTF-16 was replaced by
        # `init --reinit` with no `--yes`, explainability targets and all,
        # because nothing could read the section that would have refused it
        # (review of the #203 final-review fixes). `init` now refuses that reset
        # without `--yes`, and passes `discard_unreadable` with it.
        #
        # THROUGH THE RETRY, like the rename in `write_replacing` below and
        # `load_config` above. A `PermissionError` IS an `OSError`, so under the
        # NTFS contention this module measures, the fail-open silently skipped
        # the unknown-key preservation — and `_keep_unknown`'s own docstring says
        # what that costs: "exit 0, no warning, and because the tracing seam is
        # fail-open the result is a green-looking machine with no tracing".
        # A file that is busy for 40 microseconds is not one we cannot read.
        try:
            existing = _parse_toml(despite_windows_contention(written.read_bytes))
        except FileNotFoundError:
            pass  # gone since `exists()`: nothing is left to keep
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            if not discard_unreadable:
                raise
        else:
            dumped = _keep_unknown(existing, dumped, config)
    # Kept in full until here: _keep_unknown follows the model into every section, and
    # a section at its defaults may still hold keys this build does not know.
    dumped = _with_whole_role_tables(_overrides(dumped, system_defaults(), config), dumped)
    # What this writes is this build's, at the version every step has reached (_migrate).
    payload = tomli_w.dumps(_versioned(dumped))

    # Written BESIDE the target and renamed over it, never into the target
    # itself — ``core.atomic.write_replacing``, the durable-replace recipe this
    # function first wrote down and measured: a sibling temp (``os.replace`` is
    # only atomic within one filesystem; a name under /tmp would reintroduce a
    # copy step), fsynced so a crash cannot publish a file whose contents never
    # reached the disk, renamed over the target, then the parent directory
    # synced so the rename itself is durable (fail-open; +2.15 ms median per
    # write on a native disk, affordable because every call site is a typed
    # operator command and none is on the launch, session or heartbeat path).
    # A concurrent reader sees either the whole old file or the whole new one —
    # and the QUIETEST caller is the one a torn write hurt: ``cli/launch.py``
    # treats an unreadable config as "launching untraced" by design. The temp
    # is removed on any failure rather than left next to the file an operator
    # reads. POSIX rename semantics hold because ~/.aisquare is a native disk;
    # on a DrvFs /mnt/c or \\wsl.localhost path the guarantee softens, and
    # nothing in this code can tell which kind of path it is on. On NTFS the
    # rename is refused while another process has the config open, even only
    # to read it; ``write_replacing`` retries it through
    # ``paths.despite_windows_contention``, so every file written with that
    # recipe gets the retry, not only this one.
    try:
        # keep_mode: a config the operator tightened to 0600 stays 0600 — the
        # rewrite used to reset it to the umask default.
        write_replacing(written, payload, keep_mode=True)
    except OSError as exc:
        if written == target:
            raise
        # A symlink was followed, so the path that failed is NOT the one the
        # caller passed. Unqualified, the operator reads "permission denied" and
        # goes looking at a directory that is perfectly writable — we would have
        # swapped a silent sever for a confusing error and lost either way. The
        # class and errno are preserved so callers matching on either still work.
        detail = (
            f"{exc.strerror or exc} — {target} is a symlink, so the config is "
            f"written through it and this needs write permission on "
            f"{written.parent}, not on the link"
        )
        try:
            raise type(exc)(exc.errno, detail, str(written)) from exc
        except TypeError:  # an OSError subclass with an unusual signature
            raise OSError(exc.errno, detail, str(written)) from exc
    return target


#: The UTC stamp a reset's backup is named with: ``config.toml.bak-20261014T031700Z``, the
#: shape ``accounts remove`` names a retired slot with.
_BACKUP_STAMP = "%Y%m%dT%H%M%SZ"


def reset_config(path: Path | None = None) -> Path | None:
    """Every setting back to its system default: returns where the user's file went, or ``None``.

    The file at ``path`` (default: the standard location) moves aside to
    ``config.toml.bak-<UTC stamp>`` beside it — a ``-2``, ``-3`` after the stamp when
    that name is taken — and nothing takes its place, so everything loads as the system
    default (:func:`load_config`). Moving it back undoes the reset. Nothing to reset — no
    file, or one that holds no setting (:func:`holds_a_setting`), such as a reset or a
    save of the defaults leaves — returns ``None`` and moves nothing. One that no longer
    parses is reset like any other.

    Through a symlink the file it points at is copied aside and emptied, never the link
    moved: moved, the next save would put a regular file where it was, the severed
    dotfiles link :func:`save_config` takes care never to make.
    """
    target = path or paths.config_path()
    written = Path(os.path.realpath(target))
    if not written.is_file() or not holds_a_setting(target):
        return None
    stamp = datetime.now(UTC).strftime(_BACKUP_STAMP)
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    counter = 1
    while backup.exists() or backup.is_symlink():
        counter += 1
        backup = target.with_name(f"{target.name}.bak-{stamp}-{counter}")
    if written == target:
        despite_windows_contention(lambda: os.replace(target, backup))
    else:
        # copy2: the mode too, so a config tightened to 0600 is not copied out readable.
        despite_windows_contention(lambda: shutil.copy2(written, backup))
        write_replacing(written, "", keep_mode=True)
    return backup
