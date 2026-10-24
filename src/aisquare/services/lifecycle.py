"""Setup, upgrade and removal of aisquare itself.

Everything the upgrade path calls is imported at module top, and that is
load-bearing rather than tidy: ``uv tool install --force`` deletes and
recreates the environment this process was loaded from while it is still
running (measured: the directory's inode changes). Any import made after the
installer returns would load the NEW version's module into the OLD process —
or fail. So the code after the install reaches only what is already in
memory, and anything that needs the new version runs AS the new version, in a
subprocess.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import credentials as credentials_store
from aisquare.core import paths
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    defaults_a_whole_file_reader_lacks,
    load_config,
    save_config,
)
from aisquare.core.store import store_session
from aisquare.core.tmux import (
    SocketAnswer,
    SocketReads,
    TmuxError,
    TmuxServer,
    asking_server,
    server_may_run,
)
from aisquare.core.version import DISTRIBUTION, __version__
from aisquare.core.workspace import project_id_for
from aisquare.models import ClaudeIdentity, ProjectInfo, SetupReport
from aisquare.services import agents as agents_service
from aisquare.services import explainability as explainability_service
from aisquare.services import install_route, onboarding
from aisquare.services import project as project_service


class ExplainabilityResetRefused(RuntimeError):
    """``--reinit`` would discard a configured explainability section.

    Raised rather than warned because the loss is not recoverable from anything
    on the machine: ``[explainability.targets]`` holds a gateway URL and the
    NAME of the environment variable holding the key, both configured out of
    band. Afterwards ``status`` reads as a plausible *unconfigured* machine
    rather than a broken one, so nothing downstream reports it.

    A config that cannot be read is refused too, as
    :class:`UnreadableConfigResetRefused`: what it configures cannot be seen,
    so neither can what a reset would take.
    """

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


class UnreadableConfigResetRefused(ExplainabilityResetRefused):
    """``--reinit`` would replace a ``config.toml`` it cannot read; ``summary`` is why not.

    The reset used to go ahead on such a file, on the grounds that a section
    nobody can read has nothing to protect. That held only while "cannot read"
    meant "holds nothing". A config.toml PowerShell 5.1 saved as UTF-16 holds
    everything its operator configured, and it was replaced with the defaults
    by a plain ``--reinit`` (review of the #203 final-review fixes). A file in
    an encoding this build now reads is refused like any other. One it still
    cannot read needs ``--yes``, which says the operator means to lose what
    is in it. ``doctor``'s fix for an invalid config says so, so the recovery
    it prescribes is still one command.
    """


def _configured_explainability(settings: ExplainabilitySettings) -> str | None:
    """What a reset would take, or None if there is nothing configured.

    Keys on three fields rather than one: a machine mid-cutover may have any of
    them set, and checking only ``targets`` would let a half-configured machine
    be reset in silence.
    """
    parts: list[str] = []
    if settings.targets:
        parts.append("targets " + ", ".join(sorted(settings.targets)))
    if settings.enabled:
        parts.append("tracing enabled")
    if settings.gateway_url:
        parts.append(f"gateway {settings.gateway_url}")
    return "; ".join(parts) or None


def initialize(
    path: Path | None,
    *,
    api_key: str | None,
    local: bool,
    agents: list[str],
    onboard: bool,
    reinit: bool,
    assume_yes: bool,
    explainability: bool | None = None,
) -> SetupReport:
    """Set up ``~/.aisquare``, register the project, and connect agents.

    Idempotent and non-interactive: safe to re-run (``assume_yes`` is therefore
    moot for now). ``reinit`` resets ``config.toml`` to defaults. Agents named via
    ``--agent`` are connected (hooks installed + context ingested); cloud auth is
    not wired yet.

    ``path`` is refused unless it is a folder (``project.NotAFolder``), before anything is
    written: a file or a mistyped folder was registered as a project of its own (review of
    #259, round 43, 3/7).
    """
    root = project_service.typed_root(path)
    home = paths.aisquare_home()
    already_initialized = paths.config_path().exists() or paths.db_path().exists()
    paths.ensure_home()

    discarded: str | None = None
    unreadable: str | None = None
    if reinit and paths.config_path().exists():
        try:
            existing: ExplainabilitySettings | None = load_config().explainability
        except Exception as exc:
            # Nothing can say what it configures, so the reset needs consent
            # (UnreadableConfigResetRefused).
            existing, unreadable = None, str(exc)
            if not assume_yes:
                raise UnreadableConfigResetRefused(unreadable) from exc
        if existing is not None:
            discarded = _configured_explainability(existing)
            if discarded and not assume_yes:
                raise ExplainabilityResetRefused(discarded)

    if reinit or not paths.config_path().exists():
        save_config(AppConfig(), discard_unreadable=unreadable is not None)

    project = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    with store_session() as store:
        _, warning = project_service.onboard_on_purpose(store, project)  # deliberate (#139)

    notes: list[str] = []
    if warning is not None:
        notes.append(f"Warning: {warning}.")
    if unreadable is not None:
        notes.append(f"reset replaced a config.toml it could not read ({unreadable})")
    if discarded:
        # Consent was given, so the reset happened — but say what went, because
        # nothing downstream reports a missing targets table.
        notes.append(f"reset discarded the configured explainability section ({discarded})")
    if api_key:
        # Merged rather than replaced: `serve` keeps its bearer token in the same
        # file, and a whole-file write erased it (and, in the other order, this
        # key). One helper owns the format so the two cannot diverge again --
        # and reports whether the file could really be locked to this account,
        # because on NTFS the 0600 that guarded it is a no-op.
        _, restricted = credentials_store.store(**{credentials_store.API_KEY: api_key})
        # One string with one branch, rather than two near-copies sharing a
        # prefix: the two paths cannot drift into saying different things about
        # where the key landed.
        notes.append(
            "Stored API key in ~/.aisquare/credentials"
            + (
                "."
                if restricted
                else " — but could NOT restrict it to your account; other users on this "
                "machine may be able to read it."
            )
        )
    elif not local:
        notes.append(
            "No API key given — running local-only; re-run with --api-key to connect later."
        )

    notes.extend(_explainability_step(explainability))

    onboarded = 0
    if onboard:
        report = project_service.onboard(path, refresh=False)
        onboarded = len(report.seeded)

    for agent in agents:
        try:
            connection = agents_service.connect(agent)
        except (KeyError, ValueError) as exc:
            notes.append(f"Could not connect {agent}: {exc}")
            continue
        # Always installed: `connect` refuses rather than return a connection without hooks.
        if connection.hooks_off is not None:
            notes.append(
                f"Installed {agent}'s hooks and imported {connection.imported} entries, but "
                f'{connection.hooks_off} sets "disableAllHooks": true, so they will not run '
                "until you remove that key."
            )
        else:
            notes.append(
                f"Connected {agent}: hooks installed, imported {connection.imported} entries."
            )
        # What `agents connect` says beside an enabled plugin, from the same helper.
        beside = agents_service.plugin_beside_note(agent)
        if beside is not None:
            notes.append(beside)

    return SetupReport(
        home=home,
        already_initialized=already_initialized,
        project=project,
        onboarded=onboarded,
        notes=notes,
    )


def _explainability_step(decision: bool | None) -> list[str]:
    """The optional explainability step: offer it, take it, or leave no trace.

    Three outcomes, and the middle one is the important one:

    * ``True``  — the user opted in; configure and say what will be captured.
    * ``None``  — not asked or not answered. Mention the step exists, if and
      only if it could actually be accepted here, and change nothing.
    * ``False`` — declined. Say nothing, do nothing. #50's first acceptance
      clause is that declining leaves ZERO behavioural change, and a decline
      that still wrote a config key or printed a nag would not be zero.

    Never raises: a machine with a broken gateway config must still finish
    ``init``.
    """
    if decision is False:
        return []
    try:
        offer = explainability_service.shipping_offer()
    except Exception:  # setup must not die of an optional step
        return []
    if decision is None:
        if not offer.available:
            return []
        return [
            "Explainability: this machine can ship "
            f"{explainability_service.ShippingOffer.CAPTURES} to {offer.gateway_url}. "
            "Off until you ask for it: aisquare init --explainability"
        ]
    if not offer.available:
        return [f"Explainability not configured — {offer.reason}"]
    state = explainability_service.configure_shipping()
    if not state.configured:
        return [f"Explainability not configured — {state.reason}"]
    return [
        f"Explainability on: shipping {explainability_service.ShippingOffer.CAPTURES} "
        f"to {state.gateway_url}. Drain with: aisquare explainability ship"
    ]


# --- upgrade -----------------------------------------------------------------------------

HOOK_AGENT = "claude-code"
"""The one agent whose hooks aisquare writes (``core.agents._HOOKS``)."""

VERSION_CHECK_TIMEOUT_SECONDS = 60.0
HOOK_REFRESH_TIMEOUT_SECONDS = 120.0


class InvalidVersion(ValueError):
    """``--version`` named something that is not a version."""


class UpgradeRefused(RuntimeError):
    """This install is not one ``aisquare upgrade`` runs; ``command`` is the one that does."""

    def __init__(self, reason: str, command: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.command = command


@dataclass(frozen=True)
class HookSite:
    """One Claude Code config directory, and what its aisquare hooks run.

    ``reason`` is set when the directory is left alone, and says why.
    """

    config_dir: Path
    programs: tuple[str, ...] = ()
    reason: str | None = None
    hooks_off: Path | None = None
    """For a site the upgrade rewrites: its settings file, when that switches every hook off
    (``"disableAllHooks": true``, :func:`agent_core.hooks_off`). The hooks are rewritten so
    they run once the key goes; until then Claude Code runs none, so it is not "connected"."""


@dataclass(frozen=True)
class UpgradePlan:
    """What ``aisquare upgrade`` would do, decided before anything runs."""

    route: install_route.InstallRoute
    current: str
    target: str | None
    """The version asked for with ``--version``; ``None`` means the latest release."""
    latest: install_route.LatestRelease | None
    """What PyPI said, or ``None`` when it was not asked (a pinned or refused run)."""
    argv: tuple[str, ...]
    env: dict[str, str]
    reason: str | None
    """Why this route is reported rather than run; ``None`` when it runs."""
    refresh: tuple[HookSite, ...] = ()
    """Recorded hook sites the new install re-connects afterwards (issue #58)."""
    left: tuple[HookSite, ...] = ()
    """Recorded hook sites left as they are, each with its reason."""
    live_agents: tuple[str, ...] = ()
    """Live fleet agents, counted as uninstall counts them (:func:`running_fleet`)."""
    fleet_error: str | None = None
    """Why the fleet's live agents could not be counted, when they could not."""
    remote: str | None = None
    """The Remote that is on for this home in another process, named (:func:`running_remote`);
    ``None`` when none is."""
    pin_refused: bool = False
    """Whether :attr:`reason` is that ``--version`` cannot pick a release for this route
    (``install_route.pin_refusal``): Homebrew's, a checkout's or a local source's."""
    pin_unmet: str | None = None
    """Why ``--version``'s release may not be installed here, when ``--check`` found so: the
    reinstall's Python, or the uv cutoff, excludes it, or PyPI does not list it. The first
    two are :attr:`reason` too when the route is otherwise one ``aisquare upgrade`` runs;
    an index set up for it may list what PyPI does not."""
    config_defaults_lost: tuple[str, ...] = ()
    """The settings a move back to a release that reads config.toml whole
    (:data:`LAST_WHOLE_CONFIG` and earlier) takes from that release's defaults, not this
    one's, as the file says nothing of them: ``accounts.pick default (here headroom)``."""
    env_files_dropped: tuple[str, ...] = ()
    """The roles bound to a file (``RoleLaunchProfile.env_files``) that a move back to a
    release before :data:`FIRST_ENV_FILES` ignores, each with its variables:
    ``reviewer (GH_TOKEN)``."""

    @property
    def runnable(self) -> bool:
        return self.reason is None

    @property
    def backwards(self) -> bool:
        """Whether this run moves to a release older than the one running (``--version``)."""
        return self.target is not None and install_route.is_newer(self.current, self.target) is True

    @property
    def fleet_warning(self) -> str | None:
        """What upgrading now costs the live fleet agents, or ``None`` when none runs.

        Uninstall refuses while they run, because the program their hooks call goes
        for good. Here it goes only while ``uv tool install --force`` recreates the
        environment, so it is said, in the plan and in the question, rather than
        refused: stopping the fleet costs every session in it, and the window is
        seconds (review of #257).
        """
        if not self.live_agents:
            return None
        count = len(self.live_agents)
        return (
            f"{count} fleet agent{'s are' if count != 1 else ' is'} running "
            f"({', '.join(self.live_agents)}): a hook {'they fire' if count != 1 else 'it fires'} "
            "while the install is being replaced fails, and that turn's board update is lost. "
            f"To be safe, stop {'them' if count != 1 else 'it'} first: {FLEET_SHUTDOWN}"
        )

    @property
    def remote_warning(self) -> str | None:
        """What upgrading now costs the Remote on in another process, or ``None`` when none is.

        Said, as :attr:`fleet_warning` is, rather than refused, which uninstall does: the
        install is away only while it is replaced. Its process keeps the release it started
        with, and what it has not imported yet it imports from the new one (follow-up of
        #259)."""
        if self.remote is None:
            return None
        return (
            f"Remote is on ({self.remote}): while the install is being replaced, its page and "
            f"what phones ask of it can fail, and after that it runs {self.current} over the new "
            f"install's files until it is started again. To be safe, turn it off first: "
            f"{REMOTE_OFF}"
        )

    @property
    def config_warning(self) -> str | None:
        """What a move back reads differently in config.toml, or ``None`` when nothing.

        This release's file holds only what the user changed, over defaults built in; a
        release up to :data:`LAST_WHOLE_CONFIG` reads it with its own defaults instead, so
        the settings the file leaves to today's defaults change with the move (review of
        #261, M2). Said, as :attr:`fleet_warning` is, rather than refused: nothing is lost,
        and the way forward again reads the file as before.
        """
        if not self.config_defaults_lost:
            return None
        first = self.config_defaults_lost[0].split()[0]
        return (
            f"{self.target} reads config.toml without this release's defaults under it, so "
            f"what your file leaves to them takes {self.target}'s there: "
            f"{', '.join(self.config_defaults_lost)}. Your own settings read the same in "
            f"both; to keep one of these there, set it after the move (`aisquare config set "
            f"{first} …`)"
        )

    @property
    def env_files_warning(self) -> str | None:
        """What a move back costs the roles bound to a file, or ``None`` when it costs none.

        A release before :data:`FIRST_ENV_FILES` ignores the keys its config does not know,
        so such a role launches there with whatever value the launching shell has: for a
        reviewer bound to a token that cannot push, often one that can. 0.8.1 refuses an
        unreadable file to prevent exactly that, and the move back said nothing of it
        (review of #259, round 2). Said, as :attr:`fleet_warning` is, rather than refused:
        the bindings stay in config.toml for the way forward again.
        """
        if not self.env_files_dropped:
            return None
        one = len(self.env_files_dropped) == 1
        return (
            f"{self.target} predates `team bind --env-file` (new in {FIRST_ENV_FILES}) and "
            f"ignores {'that binding' if one else 'those bindings'}: "
            f"{', '.join(self.env_files_dropped)} would launch there with whatever value the "
            "launching shell has, such as a GH_TOKEN that can push. The "
            f"{'binding stays' if one else 'bindings stay'} in config.toml, read again from "
            f"{FIRST_ENV_FILES} on"
        )

    @property
    def command(self) -> str:
        return install_route.command_line(self.argv)

    @property
    def latest_version(self) -> str | None:
        return self.latest.version if self.latest is not None else None

    @property
    def destination(self) -> str:
        """Where the upgrade goes, for a person: a version, or what ``@latest`` means."""
        # Not "the latest release": a recorded setting may pick another (`resolution`).
        return self.target or self.latest_version or "the release uv picks"

    @property
    def update_available(self) -> bool | None:
        """Whether PyPI has something newer; ``None`` when PyPI could not say."""
        latest = self.latest_version
        if latest is None:
            return None
        newer = install_route.is_newer(latest, self.current)
        return (latest.strip() != self.current.strip()) if newer is None else newer

    @property
    def up_to_date(self) -> bool:
        """Nothing to install: the pin is what runs, or nothing newer is published.

        A running version AHEAD of PyPI's (a pre-release, a mirror) is up to
        date here — moving it would be a downgrade, and a downgrade is only ever
        done by asking for one with ``--version``.
        """
        if self.target is not None:
            return install_route.same_version(self.target, self.current)
        return self.update_available is False


@dataclass(frozen=True)
class HookRefresh:
    """One recorded hook site, re-connected by the new install — or why not."""

    config_dir: Path
    ok: bool
    error: str | None = None
    hooks_off: Path | None = None
    """:attr:`HookSite.hooks_off`: rewritten, but switched off there."""


@dataclass(frozen=True)
class UpgradeReport:
    """What an upgrade did. ``problem`` is set when the new version could not be confirmed."""

    plan: UpgradePlan
    exit_code: int
    version: str | None = None
    """What the install reports afterwards, asked in a new process, also after a failed
    install; ``None`` when it could not say."""
    problem: str | None = None
    hooks: tuple[HookRefresh, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def installed(self) -> bool:
        """Whether the installer itself reported success."""
        return self.exit_code == 0

    @property
    def upgraded(self) -> bool:
        """Whether a NEW process reports the version this upgrade was for."""
        return self.installed and self.problem is None and self.version is not None


def upgrade_plan(target: str | None = None, *, check: bool = False) -> UpgradePlan:
    """Decide what ``aisquare upgrade`` would do. Reads only; starts nothing.

    PyPI is asked only when its answer is used: by ``--check``, and by a run
    that targets the latest release on a route that runs. A refused route is
    refused offline, and a pin needs no lookup; ``--check`` with one also says
    whether the reinstall's Python can take it.
    """
    if target is not None:
        pinned = install_route.version_argument(target)
        if pinned is None:
            raise InvalidVersion(target)
        target = pinned
    route = install_route.detect()
    reason = install_route.not_automated(route)
    pin_refused = False
    if target is not None and not install_route.same_version(target, __version__):
        # The pin is what was asked: a route's reason for the newest says how to get that.
        # The version that runs is nothing to do, on every route (a later review of #257).
        refusal = install_route.pin_refusal(route, target)
        if refusal is not None:
            reason, pin_refused = refusal, True
    latest: install_route.LatestRelease | None = None
    pin_unmet: str | None = None
    if check or (reason is None and target is None):
        latest = _latest_for(route, target)
        if target is not None:
            pin_unmet = _pin_unmet(route, target, latest)
        # uv refuses a pin its Python or its cutoff excludes, and the run named it again
        # (review of #257's fixes). A route aisquare does not run keeps its own reason and
        # command (a later review). Not a pin PyPI does not list: `uv tool install` also
        # reads an index its receipt does not record (uv.toml, UV_INDEX_URL), as
        # `_as_recorded` says, and that index may list it (round 20).
        if not latest.pin_unlisted:
            reason = reason or pin_unmet
    refresh: tuple[HookSite, ...] = ()
    left: tuple[HookSite, ...] = ()
    dropped: tuple[str, ...] = ()
    live: tuple[str, ...] = ()
    fleet_error: str | None = None
    remote: str | None = None
    lost: tuple[str, ...] = ()
    if reason is None:
        refresh, left = refresh_sites(route.facts)
        if target is not None and lacks_refresh_hooks(target):
            # upgrade() leaves the hooks alone on a move back to a release before
            # `agents refresh-hooks`, so the plan must not promise a re-connect: the plan,
            # its --json, the question and the report agree (review of #257). Said once,
            # here, with the remedy: a note in the report said it again.
            left = (*left, *(_left_by_move_back(site, target) for site in refresh))
            refresh = ()
        if target is not None and reads_config_whole(target):
            lost = _config_defaults_lost()
        if target is not None and lacks_env_files(target):
            dropped = _env_file_bindings()
        live, _unlistened, fleet_error = running_fleet()
        remote = running_remote()
    return UpgradePlan(
        route=route,
        current=__version__,
        target=target,
        latest=latest,
        pin_refused=pin_refused,
        pin_unmet=pin_unmet,
        argv=tuple(install_route.upgrade_argv(route, target, current=__version__)),
        env=install_route.installer_env(route),
        reason=reason,
        refresh=refresh,
        left=left,
        live_agents=live,
        fleet_error=fleet_error,
        remote=remote,
        config_defaults_lost=lost,
        env_files_dropped=dropped,
    )


def _pin_unmet(
    route: install_route.InstallRoute, target: str, latest: install_route.LatestRelease
) -> str | None:
    """Why uv would refuse ``--version``'s ``target`` here, from the lookup, or ``None``."""
    if latest.pin_unlisted:
        return f"PyPI lists no {DISTRIBUTION} {target}"
    if latest.pin_after_cutoff:
        held = install_route.cutoff(route) or "--exclude-newer"
        return f"your uv cutoff ({held}) excludes {target}, uploaded after it"
    if latest.pin_requires is None:
        return None
    # A uv tool's reinstall restates its Python; another route's need not run on this one.
    runs = "this install's upgrade runs on" if route.receipt is not None else "this install runs on"
    return (
        f"{target} requires Python {latest.pin_requires}, and {runs} Python "
        f"{install_route.reinstall_python(route)}; installing it needs a Python that meets "
        f"{latest.pin_requires}"
    )


def lacks_refresh_hooks(release: str) -> bool:
    """Whether ``release`` predates ``agents refresh-hooks`` (:data:`REFRESH_HOOKS`), so the
    install it makes cannot rewrite the hooks without re-importing CLAUDE.md. Decided on the
    release, not on the direction: from 0.8.1, a move back to 0.8.0 left every site and
    advised `agents connect` (review of #257)."""
    return install_route.is_newer(FIRST_REFRESH_HOOKS, release) is True


def reads_config_whole(release: str) -> bool:
    """Whether ``release`` reads config.toml as it stands, without the system defaults under
    it: :data:`LAST_WHOLE_CONFIG` and earlier."""
    return install_route.is_newer(release, LAST_WHOLE_CONFIG) is False


def _config_defaults_lost() -> tuple[str, ...]:
    """Each setting a release that :func:`reads_config_whole` reads otherwise
    (:attr:`UpgradePlan.config_defaults_lost`); none when the config cannot be read, which
    says nothing either way."""
    try:
        config = load_config()
    except Exception:
        return ()
    return tuple(
        f"{key} {theirs} (here {ours})"
        for key, ours, theirs in defaults_a_whole_file_reader_lacks(config)
    )


def lacks_env_files(release: str) -> bool:
    """Whether ``release`` predates ``env_files`` bindings (:data:`FIRST_ENV_FILES`), whose
    keys its config then ignores."""
    return install_route.is_newer(FIRST_ENV_FILES, release) is True


def _env_file_bindings() -> tuple[str, ...]:
    """Each role bound to a file, with its variables (:attr:`UpgradePlan.env_files_dropped`);
    none when the config cannot be read, which says nothing either way."""
    try:
        profiles = load_config().team.profiles
    except Exception:
        return ()
    return tuple(
        f"{role} ({', '.join(sorted(profile.env_files))})"
        for role, profile in sorted(profiles.items())
        if profile.env_files
    )


def _left_by_move_back(site: HookSite, target: str) -> HookSite:
    """``site`` left as it is by a move back to ``target``, a release that
    :func:`lacks_refresh_hooks`, with the command that rewrites its hooks for that release."""
    rewrite = install_route.command_line(
        ["aisquare", "agents", "connect", HOOK_AGENT, "--config-dir", str(site.config_dir)]
    )
    why = (
        f"{target} predates `agents refresh-hooks` (new in {FIRST_REFRESH_HOOKS}): the move "
        f"back leaves the hooks as they are; `{rewrite}` rewrites them for {target}"
    )
    return HookSite(site.config_dir, site.programs, why)


def _held_back(route: install_route.InstallRoute) -> str | None:
    """Why PyPI's newest may not be what uv takes for this install, or ``None``.

    The settings its uv receipt records that may change the release (``UvReceipt.holds``),
    named as recorded, or that the receipt could not be read. Nothing more is claimed:
    review after review found each finer account of WHY false for some combination of uv
    settings (#257).
    """
    receipt = route.receipt
    if receipt is not None and receipt.unreadable is not None:
        return f"this install's uv receipt could not be read ({receipt.unreadable})"
    if receipt is None or not receipt.holds:
        return None
    return f"this install's uv settings can hold releases back ({', '.join(receipt.holds)})"


def _cutoff_alone(route: install_route.InstallRoute) -> bool:
    """Whether a global uv cutoff is the only such setting: the one case PyPI's upload times
    answer, measured with real uv (:func:`_latest_for`)."""
    holds = route.receipt.holds if route.receipt is not None else ()
    return bool(holds) and set(holds) <= {"exclude-newer", "exclude-newer-span"}


def _latest_for(
    route: install_route.InstallRoute, pin: str | None = None
) -> install_route.LatestRelease:
    """PyPI's newest release, pre-releases counted when this install's upgrade takes them
    (``install_route.takes_prereleases``); under a global uv cutoff alone, the newest
    uploaded before it; never one the reinstall's Python cannot take. Under any other
    setting that can hold a release back, PyPI's word says nothing about what uv takes, so
    it is not asked (:func:`_held_back`). With ``pin``, whether that Python can take it.

    Taken as the target under a cutoff, PyPI's newest failed the unchanged version uv
    rightly left as §3.9.1's silent no-op; then, not asked, every run and --check found
    something to do, and each reinstall changed nothing (#257).
    """
    why = _held_back(route)
    before = None
    if why is not None:
        if _cutoff_alone(route):
            before = install_route.cutoff_time(route, datetime.now(UTC))
        if before is None:
            return install_route.LatestRelease(None, f"PyPI was not asked: {why}")
    # The Python the reinstall runs on, which uv reads each Requires-Python for; the rest
    # only when asked for.
    asked: dict[str, Any] = {"python": install_route.reinstall_python(route)}
    if install_route.takes_prereleases(route, __version__):
        asked["prereleases"] = True
    if before is not None:
        asked["uploaded_before"] = before
    if pin is not None:
        asked["pin"] = pin
    found = install_route.fetch_latest(**asked)
    if before is None or found.version is None:
        return found
    return replace(found, cutoff=install_route.cutoff(route))


def runs_this_install(binary: agent_core.HookBinary, found: install_route.Facts) -> bool:
    """Whether a hook's program is THIS install — the one being upgraded.

    A console script counts when it IS one of this environment's scripts or a
    link into them: the installer's hooks name ``~/.local/bin/aisquare``'s
    target, and a hand-written hook may name the link itself. ``python -m
    aisquare`` counts when its interpreter sits in this environment's ``bin``,
    compared by directory because every venv's ``python`` resolves to one base
    interpreter.
    """
    if binary.module_form:
        return agent_core.dir_identity(binary.program.parent) == agent_core.dir_identity(
            found.executable.parent
        )
    program = agent_core.dir_identity(binary.program)
    prefix = agent_core.dir_identity(found.prefix)
    return program == prefix or prefix in program.parents


def refresh_sites(found: install_route.Facts) -> tuple[tuple[HookSite, ...], tuple[HookSite, ...]]:
    """Which recorded hook sites the upgraded install re-connects, and which it leaves.

    Only directories THIS home connected (``agents.json``) — a directory found on
    disk was never this home's to rewrite — and only those that still carry
    aisquare hooks: an operator who removed them by hand did not ask for them
    back. A site whose hooks run ANOTHER install of aisquare that is still on
    disk is left alone and named, because re-connecting it would quietly move a
    developer's hooks off the checkout they chose and onto this release. A
    program that is gone is not another install: those hooks fail every
    session, and re-connecting is the fix.
    """
    refresh: list[HookSite] = []
    left: list[HookSite] = []
    seen: set[Path] = set()
    for directory in agent_core.connected_dirs(HOOK_AGENT):
        key = agent_core.dir_identity(directory)
        if key in seen:
            continue
        seen.add(key)
        binaries, error = hook_binaries(directory)
        if error is not None:
            left.append(HookSite(directory, reason=error))
            continue
        if not binaries:
            continue
        programs = tuple(str(binary.program) for binary in binaries)
        # Whether it STARTS, as the plugin's launcher asks (agent_core.starts): a script whose
        # `#!` Python is gone exists, and was left as "another install", failing every
        # session (a later review of #257). Its os.path checks, not Path's: on 3.11 to 3.13
        # those raise PermissionError for a directory this user cannot enter, and upgrade
        # crashed. A program that cannot start counts as gone, and re-connecting is the fix.
        foreign = next(
            (
                b
                for b in binaries
                if agent_core.starts(b.program) and not runs_this_install(b, found)
            ),
            None,
        )
        unwritable = agents_service.settings_unwritable(directory / "settings.json")
        if foreign is not None:
            left.append(
                HookSite(
                    directory,
                    programs,
                    f"its hooks run {foreign.program}, another install of aisquare",
                )
            )
        elif unwritable is not None:
            # Planned, it failed in the new install after a good upgrade, which then
            # exited 1 and kept asq closed; the remedy it printed failed the same way
            # (review of #257). The hooks name the tool path the reinstall keeps.
            left.append(HookSite(directory, programs, _CANNOT_REWRITE.format(unwritable)))
        else:
            # Rewritten all the same, as `agents connect` does: they run once the key goes.
            # Said, because the plan and the report called such a site re-connected, where
            # connect and init now say it is not (sweep of #257).
            off = agent_core.hooks_off(HOOK_AGENT, directory)
            refresh.append(HookSite(directory, programs, hooks_off=off))
    return tuple(refresh), tuple(left)


#: Why a site whose hooks are ours is left: aisquare may not write its settings.json.
_CANNOT_REWRITE = "its settings.json cannot be rewritten: {}"


#: A ``"command": "…"`` pair in settings.json text, found without parsing the JSON.
_COMMAND_FIELD = re.compile(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"')


def lenient_hook_commands(text: str) -> list[str]:
    """The aisquare hook commands in settings.json ``text``, valid JSON or not.

    Claude Code reads its settings more forgivingly than ``json.loads``: a
    Latin-1 byte or a trailing comma need not stop it running the hooks inside.
    So whether a file holds OUR hooks is decided from every ``"command"`` value
    in it, matched as text, each judged by the same matcher ``connect`` uses.
    """
    found: list[str] = []
    for match in _COMMAND_FIELD.finditer(text):
        try:
            command = json.loads(f'"{match.group(1)}"')
        except ValueError:
            command = match.group(1)
        if isinstance(command, str) and agent_core.hook_binary(command) is not None:
            found.append(command)
    return found


#: Byte-order marks a hand-saved settings.json may start with: PowerShell 5.1 writes
#: UTF-16, Notepad has written UTF-8 with a BOM.
_BOMS = ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"), (b"\xef\xbb\xbf", "utf-8-sig"))


def lenient_text(raw: bytes) -> str:
    """``raw`` as text for :func:`lenient_hook_commands`: by its BOM, else UTF-8 with
    replacement characters, so no byte stops the search for our commands."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return raw.decode(codec, errors="replace")
    return raw.decode("utf-8", errors="replace")


def settings_unreadable(directory: Path) -> str | None:
    """Why the aisquare hooks in ``directory`` cannot be read or rewritten, or ``None``.

    ``hook_commands`` reads a file it cannot parse as one with no hooks (#247),
    which is right for doctor and wrong for a step that has to act on the hooks.
    A site is named, with its reason, when:
    - its settings.json cannot be read at all, which may hide hooks; or
    - it holds aisquare hook commands (:func:`lenient_hook_commands`) in a file
      that is not UTF-8, not valid JSON, or not an object with a ``hooks``
      object. Claude Code may still run those hooks, and this CLI cannot rewrite
      that file safely.
    A file that cannot be parsed but holds nothing of ours is not this
    command's business (review of #254).
    """
    settings = directory / "settings.json"
    if paths.names_no_home(settings):
        return None  # a home this machine does not have holds no file (agent_core.present)
    # One read, "missing" split out. An exists() first raised PermissionError on 3.11 to
    # 3.13 for a directory this user cannot enter, and answers False on 3.14, passing it
    # as one with no hooks; either broke the promise above (review of #257). "Missing" is
    # agent_core's one notion of it, a symlink loop included, so this reader and the
    # doctor agree that such a directory is gone (review of #257).
    try:
        raw = settings.read_bytes()
    except OSError as exc:
        if agent_core.nothing_there(exc):
            return None
        return f"its settings.json could not be read ({exc})"
    problem: str | None = None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError is one
        problem = f"cannot be read as UTF-8 JSON ({exc})"
    else:
        if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
            problem = "is not a JSON object with a hooks object"
    if problem is None or not lenient_hook_commands(lenient_text(raw)):
        return None
    # Said by upgrade's plan and by uninstall's: both act on the hooks by rewriting the
    # file, so the ending names that, not either command's verb (review of #257).
    return (
        f"its settings.json holds aisquare hooks but {problem}, "
        "so aisquare cannot rewrite it safely"
    )


def hook_binaries(directory: Path) -> tuple[list[agent_core.HookBinary], str | None]:
    """The distinct programs ``directory``'s aisquare hooks run — or why it could not be read.

    The one reader for upgrade's refresh and uninstall's plan. A settings.json
    this user cannot read, or that is not UTF-8, is a reason (:func:`settings_unreadable`),
    and so is anything else the read raises: one bad file must not stop the work
    on every other directory, and must never pass for a directory with no hooks.
    """
    unreadable = settings_unreadable(directory)
    if unreadable is not None:
        return [], unreadable
    try:
        commands = agent_core.hook_commands(HOOK_AGENT, directory)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        # RuntimeError: pathlib's, for a recorded `~olduser/.claude` (sweep of #257).
        return [], f"its settings.json could not be read ({exc})"
    binaries: list[agent_core.HookBinary] = []
    for command in commands:
        binary = agent_core.hook_binary(command)
        if binary is not None and binary not in binaries:
            binaries.append(binary)
    return binaries, None


def hooks_stuck(directory: Path) -> tuple[list[agent_core.HookBinary], str | None]:
    """The programs ``directory``'s aisquare hooks run, and why they cannot be taken out of
    it, or ``None`` when they can: :func:`hook_binaries`' reason, else, for hooks that are
    there, a settings.json this user may not write.

    The one rule for uninstall's plan and for `agents disconnect`
    (``agents_service.access``): disconnect asked only the first half and ended in a
    PermissionError traceback on a read-only settings.json holding the hooks, the
    home-manager shape, which the doctor's own fixes sent people to (review of #257).
    """
    binaries, error = hook_binaries(directory)
    if error is None and binaries:
        unwritable = agents_service.settings_unwritable(directory / "settings.json")
        if unwritable is not None:
            error = _CANNOT_REWRITE.format(unwritable)
    return binaries, error


def upgrade(plan: UpgradePlan, *, to_stderr: bool = False) -> UpgradeReport:
    """Run the plan: install, confirm the version in a NEW process, refresh the hooks.

    ``to_stderr`` sends the installer's output to stderr, for ``--json``.
    Nothing after the installer imports anything (see the module docstring);
    the version check and the hook refresh are the new install's own
    processes. A hook site that fails is reported and the rest still run.
    """
    if plan.reason is not None:
        raise UpgradeRefused(plan.reason, plan.command)
    code = install_route.run_installer(plan.argv, env=plan.env, to_stderr=to_stderr)
    if code != 0:
        # Asked too: uv resolves before it replaces anything (measured, uv 0.12.19), so a
        # run it could not resolve, as under a cutoff that excludes even the release that
        # runs, leaves that release installed, and no reinstall from nothing is needed.
        kept, _ = _installed_version(plan)
        return UpgradeReport(
            plan, exit_code=code, version=kept, problem=f"{plan.argv[0]} exited {code}"
        )
    ran = _as_recorded(plan.route)
    version, problem = _verify(plan, ran)
    if problem is not None:
        return UpgradeReport(plan, exit_code=code, version=version, problem=problem)
    notes: list[str] = []
    latest = plan.latest_version
    moved_elsewhere = latest is not None and not install_route.same_version(version or "", latest)
    pypis = plan.latest is not None and plan.latest.cutoff is None
    unseen = _newly_recorded(plan, ran)
    now = f" — its uv settings now record: {', '.join(unseen)}" if unseen else ""
    if plan.target is None and version is not None and moved_elsewhere and (pypis or unseen):
        # Not why: "your package index served" named an index that had served the newer one,
        # and "newest" was false where a setting picks (`resolution = "lowest"`).
        python = plan.latest.for_python if plan.latest is not None else None
        which = f"PyPI's latest for Python {python}" if python else "PyPI's latest"
        held = f"{which} is {latest}; " if pypis else ""
        notes.append(f"{held}uv picks {version} here{now}")
    notes.append(
        f"asq, `aisquare serve` and `aisquare remote serve` processes that were already "
        f"running keep {plan.current} until they are restarted"
    )
    if version is not None and lacks_refresh_hooks(version):
        # A move back to a release before `agents refresh-hooks`: the plan left every site,
        # each saying so with its remedy.
        return UpgradeReport(plan, exit_code=code, version=version, notes=tuple(notes))
    hooks = tuple(_refresh(site, plan.route.facts) for site in plan.refresh)
    return UpgradeReport(plan, exit_code=code, version=version, hooks=hooks, notes=tuple(notes))


def _newly_recorded(plan: UpgradePlan, ran: install_route.InstallRoute) -> list[str]:
    """The settings that can hold releases back which the receipt uv wrote for the run
    records and the plan's receipt did not: from uv's own config, applied to this run."""
    seen = set(plan.route.receipt.holds) if plan.route.receipt is not None else set()
    return [key for key in (ran.receipt.holds if ran.receipt else ()) if key not in seen]


def _as_recorded(route: install_route.InstallRoute) -> install_route.InstallRoute:
    """``route`` with the receipt uv wrote for the install that just ran.

    A cutoff or an index set in uv's own settings (uv.toml, ``UV_EXCLUDE_NEWER``,
    ``UV_INDEX_URL``) applies to that install and is recorded in its receipt, never in
    the one the plan read (measured, uv 0.12.19). Only reads: tomllib and the receipt
    parser are already in memory (see the module docstring).
    """
    try:
        receipt = install_route.read_receipt(route.facts.prefix)
    except OSError:
        receipt = None
    return route if receipt is None else replace(route, receipt=receipt)


def _reason_line(*texts: str) -> str:
    """Why a command failed, whole: from the first text that says anything, the line that
    names the failure, read past Rich's wrapping and box borders. The child writes to a
    pipe, so Rich lays it out at 80 columns, and the last line was a wrapped path's tail
    (``n'``) or a usage box's bottom border (review of #257). A traceback's exception
    line comes with what follows it; this CLI's own ``✗ …`` loses its mark."""
    for text in texts:
        verdict = onboarding.stderr_verdict(text)
        if verdict:
            return verdict.removeprefix("✗ ")
    return ""


def _installed_version(plan: UpgradePlan) -> tuple[str | None, str | None]:
    """``(version, problem)``: what the install in ``plan``'s prefix reports, asked in a NEW
    process, or why it could not say."""
    probe = agent_core.HookBinary(plan.route.facts.executable, module_form=True)
    answer = install_route.run_captured(probe.version_argv(), timeout=VERSION_CHECK_TIMEOUT_SECONDS)
    if answer.error is not None:
        return None, f"the new install could not be started ({answer.error})"
    if answer.returncode != 0:
        said = _reason_line(answer.stderr, answer.stdout) or f"exit {answer.returncode}"
        return None, f"the new install could not be started ({said})"
    found = agent_core.version_in(answer.stdout)
    if found is None:
        return None, "the new install did not report a version"
    return found, None


def _verify(plan: UpgradePlan, ran: install_route.InstallRoute) -> tuple[str | None, str | None]:
    """``(version, problem)`` from asking the NEW install its version in a new process.

    The exit code of the installer is not the evidence: §3.9.1's failure was a
    success code over an unchanged version. So success is a version the new
    process reports — the pin when one was asked for, otherwise any move that is
    not BACK: a downgrade is only done by asking for one with ``--version``. An
    unchanged version is a failure exactly when PyPI said there is something
    newer and the receipt uv wrote for the install that ``ran`` (:func:`_as_recorded`)
    explains nothing the plan had not accounted for: a recorded cutoff the plan had compared
    excused a real no-op as ✓ (#257). Settings only that receipt records
    (:func:`_newly_recorded`), and an unknown latest, make it the release uv picks.
    """
    found, problem = _installed_version(plan)
    if found is None:
        return None, problem
    if plan.target is not None:
        if install_route.same_version(found, plan.target):
            return found, None
        return found, f"{plan.target} was asked for, but the new install reports {found}"
    if install_route.is_newer(plan.current, found):
        # The command asks for this release or newer (install_route._uv_spec), so uv fails
        # rather than resolve below it. Still a failure, never ✓ and asq reopened on a
        # release that may have no `upgrade` of its own (sweep of #257).
        return found, f"the new install reports {found}, which is older than {plan.current}"
    latest = plan.latest_version
    if not install_route.same_version(found, plan.current) or latest is None:
        return found, None
    if _newly_recorded(plan, ran):
        # A setting in uv's own config (uv.toml, UV_EXCLUDE_NEWER) the plan could not see held
        # it back: what uv picks, said with those settings named (upgrade()'s note).
        return found, None
    return found, (
        f"uv reported success but aisquare still reports {found}, not {latest} — the "
        "silent no-op docs/plans/one-line-install.md §3.9.1 describes"
    )


#: What the new install runs per site: ``agents refresh-hooks``, never ``agents
#: connect``, which also re-imports the agent's ``CLAUDE.md`` into memory — on every
#: upgrade it brought back sections the user had removed (review of #251).
REFRESH_HOOKS = ("agents", "refresh-hooks", HOOK_AGENT)
#: The first release with ``agents refresh-hooks``: 0.7.0, on PyPI, has none.
FIRST_REFRESH_HOOKS = "0.8.0"
#: The last release that reads config.toml as it stands: those after lay it over defaults.
LAST_WHOLE_CONFIG = "0.8.0"
#: The first release that reads ``env_files`` (``team bind --env-file``): 0.8.0 ignores it.
FIRST_ENV_FILES = "0.8.1"


def _refresh(site: HookSite, found: install_route.Facts) -> HookRefresh:
    """Rewrite one site's hooks BY the new install, so they name the new install.

    The console script beside the interpreter, not ``python -m aisquare``: hooks
    are written for whatever program the refresh ran as, and the module form
    would fall back to whichever ``aisquare`` comes first on PATH.
    """
    script = found.executable.with_name("aisquare.exe" if found.platform == "win32" else "aisquare")
    if not script.exists():
        return HookRefresh(site.config_dir, False, f"the new install has no {script}")
    argv = [str(script), *REFRESH_HOOKS, "--config-dir", str(site.config_dir)]
    answer = install_route.run_captured(argv, timeout=HOOK_REFRESH_TIMEOUT_SECONDS)
    if answer.error is not None:
        return HookRefresh(site.config_dir, False, answer.error)
    if answer.returncode != 0:
        said = _reason_line(answer.stderr, answer.stdout) or f"exit {answer.returncode}"
        return HookRefresh(site.config_dir, False, said)
    return HookRefresh(site.config_dir, True, hooks_off=site.hooks_off)


# --- uninstall ---------------------------------------------------------------------------
#
# The ORDER is the design, and every step that can go wrong stops the ones after
# it from removing the means to retry:
#
#   1. refuse while the fleet has live agents — their sessions would keep firing
#      hooks at a program that is about to vanish;
#   2. remove aisquare's hook groups from every Claude Code directory, one
#      directory at a time, each failure recorded and the rest still attempted;
#   3. only when the home STAYS, record in agents.json that nothing is connected;
#   4. with --purge only, delete the home — behind a guard, never through a link;
#   5. LAST, and only when 2-4 all succeeded, hand the process to the package
#      manager. A failure anywhere above keeps the package, so `aisquare
#      uninstall` is still there to run again.
#
# Nothing here goes through `agents_service.disconnect`: it calls set_connected,
# which calls `paths.ensure_home()`, and an uninstall that recreated the home it
# was keeping out of — or had just purged — would be the one place that broke the
# promise that the plan never creates ~/.aisquare.

#: Files that make a directory an aisquare home: at least one must be there
#: before --purge deletes anything.
HOME_MARKERS = ("config.toml", "context.db", "agents.json")

FLEET_SHUTDOWN = "aisquare fleet shutdown --all --yes"
FLEET_FORGET = f"{FLEET_SHUTDOWN} --forget"
REMOTE_OFF = (
    "press Ctrl-C where `aisquare remote serve` runs, or turn Remote off in that asq's R panel"
)
"""How a Remote that is on in another process is turned off (:func:`running_remote`)."""


class UninstallRefused(RuntimeError):
    """Uninstall will not start; ``error`` is the machine-readable reason."""

    def __init__(self, message: str, error: str) -> None:
        super().__init__(message)
        self.error = error


@dataclass(frozen=True)
class McpRegistration:
    """An MCP server entry in a ``.claude.json`` that runs aisquare."""

    name: str
    file: Path
    """The ``.claude.json`` that holds it: the file itself, links resolved, so one file
    reached two ways is one, and a link the purge deletes never stands for a file it
    leaves (sweep 2 of #257)."""
    project: str | None = None
    """The project a local-scope entry belongs to; ``None`` for user scope."""
    config_dir: Path | None = None
    """The ``CLAUDE_CONFIG_DIR`` under which ``claude`` reads ``file``: ``None`` for
    ``~/.claude.json``, which it reads with that variable unset."""
    fetches: bool = False
    """Whether it runs aisquare through a runner that fetches the package (``uvx``,
    ``uv tool run``, ``uv run --with``, ``pipx run``), so it still starts once the
    package is gone and makes the home again (:func:`_fetches_aisquare`)."""
    shared: bool = False
    """A project-scope entry: ``file`` is the repository's own ``.mcp.json``, which
    ``claude mcp remove --scope project`` edits from inside ``project``."""
    asked_in: tuple[Path, ...] = ()
    """For a project-scope entry, each ``.claude.json`` (the file itself) whose block for
    the project has not declined it: sessions under that config dir start it, or ask to
    (:func:`_project_registrations`)."""


@dataclass(frozen=True)
class UninstallPlan:
    """What ``aisquare uninstall`` would do, read before anything is touched.

    Read-only, and it never creates ``~/.aisquare``: the store is opened only when
    ``context.db`` already exists, and every other read is a file that is there
    or is not.
    """

    route: install_route.InstallRoute
    hooks: tuple[HookSite, ...]
    """Every Claude Code directory holding aisquare hooks, with what they run."""
    unreadable: tuple[HookSite, ...]
    """Directories whose hooks cannot be taken out: a settings.json that could not be
    read, which may hold some, or one holding ours that this user may not write."""
    mcp: tuple[McpRegistration, ...]
    package_argv: tuple[str, ...]
    package_env: dict[str, str]
    package_reason: str | None
    """Why the package step is printed rather than run; ``None`` when it runs."""
    home: Path
    home_exists: bool
    home_entries: tuple[str, ...]
    accounts: tuple[str, ...]
    """The Claude Code logins kept in the home's account slots, described."""
    keychain: bool
    """Whether those logins' tokens live in the macOS Keychain, which a purge leaves."""
    purge: bool
    purge_refusal: str | None
    """Why --purge may not delete the home: it refuses a run with ``purge`` set, and a
    plan without it does not offer --purge for this home."""
    live_agents: tuple[str, ...]
    """Live fleet rows that may still be running: started since this machine last booted,
    on a socket a server may still listen on."""
    unlistened: int
    """Live rows that do not block: no server on their socket, or started before the last
    boot."""
    fleet_error: str | None
    """Why the fleet's live agents could not be counted, when they could not."""
    tmux_unusable: str | None = None
    """Why tmux cannot be used here while :attr:`live_agents` may run (:func:`_tmux_unusable`):
    ``fleet shutdown`` can then stop none of them, and only its ``--forget`` ends their rows
    (round 27 of #257)."""
    unreached: tuple[str, ...] = ()
    """Those of :attr:`live_agents` whose server's socket is gone or could not be asked, or
    has another server on it than the one they were started on: no tmux client reaches
    them, so ``fleet shutdown`` leaves them live, and only its ``--forget`` ends their rows
    (round 27 of #257; follow-up)."""
    taken: tuple[str, ...] = ()
    """Those of :attr:`unreached` whose socket has another server on it: the way back to
    their own server, ``SIGUSR1``, waits until no server answers there, as tmux's handler
    takes the path from whatever listens on it (review of #259, round 3)."""
    listening: tuple[str, ...] = ()
    """The rest of :attr:`live_agents`: a server answers on their socket, so ``--forget``
    leaves them live. Sorted per row, never by subtracting :attr:`unreached` from
    :attr:`live_agents`: a label names its project's folder, and two folders share a name
    (``~/work/api``, ``~/oss/api``), so one ``manager (api)`` unreached hid the other that
    answers (round 29 of #257)."""
    plugins: tuple[agent_core.ClaudePlugin, ...] = ()
    """Where the aisquare Claude Code plugin is enabled: in a config dir (user scope) or
    in a repository (project and local scope). Uninstall leaves it, as Claude Code owns
    it, and it keeps running aisquare there: through uvx once the package is gone,
    which makes the home again (review of #257)."""
    plugin_answers: tuple[Path, ...] = ()
    """The files the plugin's launcher keeps uv's answers in (:func:`plugin_answers`), each
    where it or what a launcher cut short left beside it is there
    (:func:`_plugin_answers_there`): outside the home, so named beside it, and deleted
    with --purge (:attr:`deletes_answers`)."""
    remote: str | None = None
    """The Remote that is on for this home in another process, named (:func:`running_remote`);
    ``None`` when none is. It refuses the run, as a running fleet agent does."""

    @property
    def package_command(self) -> str:
        return install_route.command_line(self.package_argv)

    @property
    def blocking(self) -> tuple[HookSite, ...]:
        """The ``unreadable`` sites whose hooks keep the package and the home: every one, unless
        this run purges (:attr:`purges`). Every one is then inside the home (an account slot, a
        retired one too), and the purge deletes their hooks with it, as it does a plugin there
        (:attr:`lasting_plugins`) (sweep of #257)."""
        return () if self.purges else self.unreadable

    @property
    def blocked(self) -> bool:
        """Whether some directory's hooks cannot be taken out (:attr:`blocking`). The run then
        keeps the package and the home, which those hooks still call, so the plan, its
        --json and the question say so too (review of #257)."""
        return bool(self.blocking)

    @property
    def purges(self) -> bool:
        """Whether this run deletes the home: --purge, a home the guard lets it delete
        (:attr:`purge_refusal`), and no site outside the home whose hooks cannot be taken
        out, as such a site stops the run before the purge. The plan, its --json, the
        question and :func:`uninstall` all follow this one rule (sweep of #257)."""
        return (
            self.purge
            and self.home_exists
            and self.purge_refusal is None
            and all(self.in_home(site.config_dir) for site in self.unreadable)
        )

    @property
    def deletes_answers(self) -> bool:
        """Whether this run deletes the plugin's answers (:attr:`plugin_answers`): with --purge
        on a home the guard lets it delete, unless a site whose hooks cannot be taken out keeps
        the package (:attr:`blocked`). Not tied to deleting the home: they are outside it, so
        a home already gone is no reason to keep them, and they hold uv's paths and the
        session's PATH (round 27 of #257)."""
        return self.purge and self.purge_refusal is None and not self.blocked

    def in_home(self, path: Path) -> bool:
        """Whether ``path`` is inside the home, so a purge deletes it with everything in it."""
        return agent_core.dir_identity(self.home) in agent_core.dir_identity(path).parents

    @property
    def lasting_plugins(self) -> tuple[agent_core.ClaudePlugin, ...]:
        """The plugins still enabled after this run: when it purges, not those whose config
        dir is inside the home it deletes (the fleet's account slots, retired ones too),
        which go with it and can run nothing afterwards (review of #257)."""
        if not self.purges:
            return self.plugins
        return tuple(plugin for plugin in self.plugins if not self.in_home(plugin.config_dir))

    @property
    def lasting_mcp(self) -> tuple[McpRegistration, ...]:
        """The MCP registrations still there after this run: when it purges, not those in a
        ``.claude.json`` inside the home (an account slot's), which the purge deletes, as it
        does a plugin there (sweep 2 of #257). Nor a repository's ``.mcp.json`` server that
        only blocks inside the home have not declined (:attr:`McpRegistration.asked_in`):
        the purge deletes them, and every config dir left declined it, so no session starts
        it, and its removal would edit the repository's committed file (round 27). ``file``
        is the file itself, so the answer is the same after the purge as before it."""
        if not self.purges:
            return self.mcp
        return tuple(
            entry
            for entry in self.mcp
            if not self.in_home(entry.file)
            and not (entry.shared and all(self.in_home(path) for path in entry.asked_in))
        )

    @property
    def refusal(self) -> UninstallRefused | None:
        """The reason nothing may start, or ``None``."""
        if self.live_agents:
            count = len(self.live_agents)
            running = (
                f"{count} fleet agent{'s are' if count != 1 else ' is'} running "
                f"({', '.join(self.live_agents)}) — their sessions would keep calling hooks "
                f"that no longer exist."
            )
            if self.tmux_unusable is not None:
                # Not "no tmux, so no server": a server outlives its program leaving PATH.
                # The way out is the operator's word (round 27 of #257), but for a row whose
                # socket answers, which `--forget` leaves live (round 28).
                held = ""
                if self.listening:
                    one = len(self.listening) == 1
                    held = (
                        f" A server answers on the socket of {', '.join(self.listening)}, so "
                        f"{'it is' if one else 'they are'} likely running, and --forget "
                        f"leaves {'its row' if one else 'their rows'} live."
                    )
                forget = ""
                if self.unreached:
                    one = len(self.unreached) == 1
                    forget = (
                        f", or, if {', '.join(self.unreached)} {'is' if one else 'are'} not "
                        f"running, end {'its row' if one else 'their rows'} on your word: "
                        f"{FLEET_FORGET}"
                    )
                return UninstallRefused(
                    f"{running} tmux is not usable here ({self.tmux_unusable}), so nothing can "
                    f"stop them.{held} Once tmux is usable, run {FLEET_SHUTDOWN}{forget}",
                    error="fleet_running",
                )
            unreached = ""
            if self.unreached:
                # `fleet shutdown` cannot reach them, and leaves them live: said, with the
                # two ways on (round 27 of #257). tmux's SIGUSR1 handler unlinks whatever is
                # at the path and binds it, so sent while another server listens there it
                # hands that server's path to the old one, and every agent on it is the one
                # no client reaches: only once no server answers (review of #259, round 3),
                # as `stop`, `shutdown`, `reap` and the doctor say.
                one = len(self.unreached) == 1
                once = ""
                if self.taken:
                    once = (
                        " once no server answers on that socket"
                        if len(self.taken) == len(self.unreached)
                        else f" (for {', '.join(self.taken)}, once no server answers on its socket)"
                    )
                unreached = (
                    f"; it cannot reach {', '.join(self.unreached)}, as the socket "
                    f"{'its' if one else 'their'} tmux server listened on is gone, could not "
                    f"be asked, or has another server on it. If {'it is' if one else 'they are'} "
                    f"still running, send that server SIGUSR1{once}, which makes tmux create "
                    f"its socket again, and run it again; if not, end "
                    f"{'its row' if one else 'their rows'} on your word: {FLEET_FORGET}"
                )
            return UninstallRefused(
                f"{running} Stop them first: {FLEET_SHUTDOWN}{unreached}", error="fleet_running"
            )
        if self.remote is not None:
            # Refused as the fleet is: with the package gone, nothing is left to turn it off,
            # sign its phones out or switch its writes off, and its link and passphrase work
            # for as long as it runs (follow-up of #259).
            return UninstallRefused(
                f"Remote is on ({self.remote}): with aisquare gone, nothing could turn it off, "
                f"sign its phones out or switch its writes off, and its link and passphrase "
                f"would work for as long as it runs. Turn it off first: {REMOTE_OFF}",
                error="remote_on",
            )
        if self.purge and self.purge_refusal is not None:
            return UninstallRefused(
                f"--purge will not delete {self.home}: {self.purge_refusal}",
                error="purge_refused",
            )
        lasting = self.lasting_plugins
        fetching = tuple(entry for entry in self.lasting_mcp if entry.fetches)
        # A purge deletes the home, or with the home already gone still the plugin's answers
        # (round 27 of #257); the next session undoing either is the same (round 28).
        purging = self.purges or self.deletes_answers
        if purging and fetching:
            # As a plugin below: Claude Code's next session starts the server, uvx fetches
            # aisquare, and `aisquare serve` makes the home again (review of #257). One
            # naming the installed program can only fail once the package is gone.
            where = ", ".join(f"{entry.name} in {mcp_place(entry)}" for entry in fetching)
            return UninstallRefused(
                f"--purge would not last: the MCP server {where} runs aisquare through a "
                f"runner that fetches it, so Claude Code's next session starts it and makes "
                f"{self.home} again. Remove it first: "
                f"{'; '.join(mcp_removal(entry) for entry in fetching)}",
                error="mcp_registered",
            )
        if purging and lasting:
            # A purge the next session undoes is not one: refused like the fleet, with
            # the command that clears the way. A plain uninstall only says so.
            where = ", ".join(plugin_place(plugin) for plugin in lasting)
            return UninstallRefused(
                f"--purge would not last: the aisquare plugin is enabled in {where}, so "
                f"Claude Code's next session there runs aisquare and makes {self.home} again. "
                f"Remove the plugin first: "
                f"{'; '.join(plugin_removal(plugin) for plugin in lasting)}",
                error="plugin_enabled",
            )
        return None


def plugin_removal(plugin: agent_core.ClaudePlugin) -> str:
    """The command that removes the aisquare plugin from its config dir — at its scope,
    and for a project- or local-scope install from inside its repository."""
    return agent_core.claude_plugin_command(
        "uninstall", plugin.config_dir, scope=plugin.scope, project=plugin.project
    )


def plugin_place(plugin: agent_core.ClaudePlugin) -> str:
    """Where the aisquare plugin runs: its config dir, or the repository that enables it."""
    if plugin.project is None:
        return str(plugin.config_dir)
    return f"{plugin.project} ({plugin.scope} scope)"


@dataclass(frozen=True)
class HookRemoval:
    """One directory's hooks, removed — or why not."""

    config_dir: Path
    ok: bool
    error: str | None = None


@dataclass(frozen=True)
class UninstallReport:
    """What uninstall did. ``package_runs`` means the package step comes next, by exec."""

    plan: UninstallPlan
    hooks: tuple[HookRemoval, ...] = ()
    record_error: str | None = None
    """Why agents.json could not be updated, when it could not."""
    purged: bool = False
    purge_error: str | None = None
    answers_deleted: bool = False
    """Whether the plan's answers files were deleted (:attr:`UninstallPlan.deletes_answers`),
    each but those in :attr:`plugin_answers_errors`."""
    plugin_answers_errors: tuple[tuple[Path, str], ...] = ()
    """Each answers file of the plan's that could not be deleted, and why. A cache: said,
    and it fails nothing."""
    notes: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return any(not hook.ok for hook in self.hooks) or self.purge_error is not None

    @property
    def package_runs(self) -> bool:
        return not self.failed and self.plan.package_reason is None


def _user_home() -> Path:
    """The user's home directory (an indirection so tests can name one)."""
    return Path.home()


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows a junction — anything a delete could be led through.

    ``os.path``'s, which never raise: ``Path.is_symlink`` raised PermissionError on 3.11
    to 3.13 under a folder this user cannot enter (round 19 of #257).
    """
    if os.path.islink(path):
        return True
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return bool(is_junction(path)) if is_junction is not None else False


def _home_there(home: Path) -> tuple[bool, str | None]:
    """Whether ``home`` is there (a link counts), and why it could not be looked at.

    ``os.lstat`` where ``Path.exists`` and ``is_symlink`` raised PermissionError on 3.11 to
    3.13 under a folder this user cannot enter, and ESTALE or EIO on a network mount, so
    ``uninstall`` ended in a traceback, ``--dry-run`` and ``--json`` too (round 19 of
    #257). A home that cannot be looked at counts as there: nothing says it is gone, so
    the plan keeps it and ``--purge`` refuses it rather than calling it deleted.
    """
    try:
        os.lstat(home)
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError as exc:
        return True, exc.strerror or str(exc)
    return True, None


def custom_home(home: Path) -> bool:
    """Whether AISQUARE_HOME moved the home away from ``~/.aisquare``."""
    if not os.environ.get(paths.HOME_ENV_VAR):
        return False
    return agent_core.dir_identity(home) != agent_core.dir_identity(_user_home() / ".aisquare")


def purge_refusal(home: Path, *, custom: bool) -> str | None:
    """Why ``home`` must not be deleted, or ``None`` when ``--purge`` may delete it.

    Every check is about the one mistake that cannot be undone, a recursive
    delete of the wrong directory:
    - a link (#198 plans links between account slots and ~/.claude, and a
      delete led through one would take the target);
    - the user's home, or anything above it, or a filesystem root;
    - a directory with none of aisquare's markers;
    - ANY home AISQUARE_HOME moved elsewhere. A directory someone chose can hold
      their own things under names aisquare also uses (``projects/``,
      ``screenshots/``), and no name tells the two apart (review of #253), so
      only ``~/.aisquare`` — aisquare's by name — is ever deleted.
    """
    there, unreachable = _home_there(home)
    if unreachable is not None:
        return f"{home} cannot be looked at ({unreachable})"
    if _is_link(home):
        return f"{home} is a link — delete what it points at by hand if that is what you mean"
    if not there:
        return None
    if not os.path.isdir(home):
        return f"{home} is not a directory"
    resolved = agent_core.dir_identity(home)
    user_home = agent_core.dir_identity(_user_home())
    if resolved == user_home:
        return f"{home} is your home directory"
    if resolved == Path(resolved.anchor):
        return f"{home} is the root of a filesystem"
    if resolved in user_home.parents:
        return f"{home} contains your home directory"
    if not any(os.path.isfile(home / marker) for marker in HOME_MARKERS):
        return f"{home} holds none of {', '.join(HOME_MARKERS)}, so it is not an aisquare home"
    if custom:
        return (
            f"AISQUARE_HOME moved the home to {home}, and --purge deletes only ~/.aisquare: a "
            "directory you chose may hold your own files under names aisquare also uses — "
            "delete it by hand"
        )
    return None


def _account_dirs() -> list[Path]:
    """Every directory under the managed account root: the slots and the removed ones.

    Asked one entry at a time with ``os.path.isdir``, which never raises. ``Path.is_dir``
    raised PermissionError on 3.11 to 3.13 for a slot linked into a folder this user
    cannot enter, and inside one ``except`` that cost the plan every slot, a linked-out
    one whose hooks hold the purge up included. That slot is skipped, as agent_core's
    scan skips such a directory: no session of this user can read hooks there.
    """
    root = paths.claude_accounts_dir()
    if not os.path.isdir(root):
        return []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    return [child for child in children if os.path.isdir(child)]


def _accounts_kept(logins: Mapping[Path, ClaudeIdentity | None]) -> tuple[str, ...]:
    """The logins the home keeps in its account slots, as a person reads them.

    ``logins`` is the login in each ``.claude.json`` the MCP scan parsed
    (:func:`_mcp_registrations`), which reads every slot's: such a file can reach tens of
    MB, and read again here for an email, the plan parsed each twice (round 23 of #257).
    A slot it did not read is read here.
    """
    described: list[str] = []
    for account in accounts_core.managed_accounts():
        key = agent_core.dir_identity(accounts_core.claude_json_path(account))
        identity = logins[key] if key in logins else accounts_core.identity(account)
        who = identity.email if identity is not None else "not signed in"
        described.append(f"slot {account.slot}: {who}")
    retired = [d for d in _account_dirs() if ".removed-" in d.name]
    if retired:
        described.append(f"{len(retired)} removed slot{'s' if len(retired) != 1 else ''}")
    return tuple(described)


#: The executable names that ARE aisquare, as either platform writes them.
_PROGRAM_NAMES = frozenset({"aisquare", "asq", "aisquare.exe", "asq.exe"})


def _runs_aisquare(spec: object) -> bool:
    """Whether an ``mcpServers`` entry starts aisquare — by program, never by mention.

    ``command`` counts when its file name IS the program (read as a Windows path
    whatever this machine is, since the file may come from either; ``/`` splits
    there too). An argument counts only as a BARE program name — ``uvx --from
    aisquare-cli aisquare serve`` — or as the ``-m aisquare`` pair. A path or a
    file that merely ends in ``aisquare`` (``~/Code/AISquare``, ``aisquare.json``)
    is another server's argument, and naming that server for removal would be
    wrong (review of #253).
    """
    if not isinstance(spec, dict):
        return False
    command = spec.get("command")
    if isinstance(command, str) and PureWindowsPath(command).name.lower() in _PROGRAM_NAMES:
        return True
    args = spec.get("args")
    tokens = [str(arg) for arg in args] if isinstance(args, list) else []
    for index, token in enumerate(tokens):
        if token.lower() in _PROGRAM_NAMES:
            return True
        if token == "-m" and tokens[index + 1 : index + 2] == ["aisquare"]:
            return True
    return False


#: A ``--with`` item that NAMES its requirement (PEP 508): the name, then extras, a version,
#: a marker, an ``@`` direct reference (``aisquare_cli @ https://…``) or nothing.
_NAMED_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\]\s*)?(?:[;@<>=!~]|$)")
#: The archives a ``--with`` item may be a file of (a wheel, an sdist), whose names a
#: requirement's name could otherwise be read off.
_ARCHIVES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".zip")


def _requirement_items(value: str) -> list[str]:
    """A ``uv run --with`` value's comma-separated requirements: split outside extras'
    brackets, as uv splits them, with a part that starts with a version operator
    (``rich>=1,<2``) kept with the one before it."""
    parts: list[str] = []
    depth, current = 0, ""
    for char in value:
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        depth = max(0, depth + {"[": 1, "]": -1}.get(char, 0))
        current += char
    parts.append(current)
    items: list[str] = []
    for part in (part.strip() for part in parts):
        if items and part[:1] in ("<", ">", "=", "!", "~"):
            items[-1] += f",{part}"
        elif part:
            items.append(part)
    return items


def _may_install_aisquare(value: str) -> bool:
    """Whether a ``uv run --with`` value may install aisquare: an item that names it, or one
    that names nothing — a URL (``git+https://…/aisquare-cli``), a path (a checkout) or an
    archive (a wheel) — which may hold it. Read as fetching: the entry runs aisquare, so an
    unnamed item is likely where it comes from, and a purge read as lasting is undone by
    the next session (round 25 of #257). An item named for another package is not."""
    for item in _requirement_items(value):
        named = _NAMED_REQUIREMENT.match(item)
        if named is None or item.lower().endswith(_ARCHIVES):
            return True
        if install_route.canonical_name(named.group(1)) == DISTRIBUTION:
            return True
    return False


def _with_values(tokens: list[str]) -> list[tuple[str, str]]:
    """``uv run``'s ``--with``/``-w``, ``--with-editable`` and ``--with-requirements`` among
    ``tokens`` as ``(flag, value)``, in each spelling clap takes: ``--with X``, ``--with=X``,
    ``-w X``, ``-w=X``, ``-wX``."""
    values: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        flag, glued, value = token.partition("=")
        if flag in ("--with", "--with-editable", "--with-requirements"):
            values.extend(
                (flag, after) for after in ([value] if glued else tokens[index + 1 : index + 2])
            )
        elif token == "-w":
            values.extend(("--with", after) for after in tokens[index + 1 : index + 2])
        elif token.startswith("-w") and not token.startswith("--"):
            values.append(("--with", token[2:].removeprefix("=")))
    return values


def _fetches_aisquare(spec: object) -> bool:
    """Whether an ``mcpServers`` entry that runs aisquare (:func:`_runs_aisquare`) runs it
    through a runner that fetches the package when it is not installed, so the server
    outlives an uninstall: ``uvx``, ``uv tool run``, ``uv run --with aisquare-cli`` and
    ``pipx run``.

    The subcommand is looked for among the arguments BEFORE the program (the bare
    ``aisquare`` or ``-m aisquare`` :func:`_runs_aisquare` reads), not at a fixed
    position: uv and pipx take their own options first, so ``uv --offline tool run``
    and ``pipx --verbose run`` read as other commands, and the purge they undo went
    ahead (review of #257). ``uv run`` fetches only what its ``--with`` flags install
    (:func:`_may_install_aisquare`; an editable path or a requirements file may hold it,
    so either counts); without them the program comes from the project or PATH, as an
    installed one does.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("command"), str):
        return False
    program = PureWindowsPath(spec["command"]).name.lower().removesuffix(".exe")
    args = spec.get("args")
    tokens = [str(arg) for arg in args] if isinstance(args, list) else []
    end = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.lower() in _PROGRAM_NAMES
            or (token == "-m" and tokens[index + 1 : index + 2] == ["aisquare"])
        ),
        len(tokens),
    )
    before = tokens[:end]
    if program == "uvx":
        return True
    if program == "pipx":
        return "run" in before
    if program != "uv":
        return False
    if any(before[index : index + 2] == ["tool", "run"] for index in range(len(before))):
        return True
    return "run" in before and any(
        flag != "--with" or _may_install_aisquare(value) for flag, value in _with_values(before)
    )


def _mcp_registrations(
    directories: Iterable[Path],
) -> tuple[tuple[McpRegistration, ...], dict[Path, ClaudeIdentity | None]]:
    """aisquare's MCP servers in every ``.claude.json`` these directories use, and in the
    ``.mcp.json`` of every project those list (:func:`_project_registrations`) — read only.
    Beside them, the login each ``.claude.json`` read records, by the file it reaches
    (``agent_core.dir_identity``, which falls back to the spelling where pathlib cannot
    resolve one), for :func:`_accounts_kept`, so no file is parsed twice.

    Listed, never edited: ``.claude.json`` is Claude Code's own file, rewritten by
    every running session, and a second writer racing them is how a login gets
    lost. ``claude mcp remove`` is the tool that owns it.
    """
    found: list[McpRegistration] = []
    logins: dict[Path, ClaudeIdentity | None] = {}
    read: set[Path] = set()
    listed: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    default = agent_core.dir_identity(accounts_core.home_config_dir().parent / ".claude.json")
    for directory in directories:
        for spelled in agent_core.claude_json_paths(directory):
            # The file itself: a slot linked to ~/.claude, or a ~/.claude* sibling that is a
            # link, reaches it twice. read_json fails open.
            path = agent_core.dir_identity(spelled)
            if path in read:
                continue
            read.add(path)
            # Where `claude` reads it: ~/.claude.json with CLAUDE_CONFIG_DIR unset, any other
            # .claude.json with the variable naming its own directory (which outlives a purge
            # as the file does), and one linked to a file of another name through the
            # directory it was found in.
            if path == default:
                reach: Path | None = None
            elif path.name == ".claude.json":
                reach = path.parent
            else:
                reach = directory
            data = agent_core.read_json(path)
            logins[path] = accounts_core.identity_in(data)
            servers = data.get("mcpServers")
            if isinstance(servers, dict):
                found.extend(
                    McpRegistration(str(name), path, None, reach, _fetches_aisquare(spec))
                    for name, spec in servers.items()
                    if _runs_aisquare(spec)
                )
            projects = data.get("projects")
            if not isinstance(projects, dict):
                continue
            for project, block in projects.items():
                if not isinstance(block, dict):
                    continue
                listed.setdefault(str(project), []).append((path, block))
                servers = block.get("mcpServers")
                if isinstance(servers, dict):
                    found.extend(
                        McpRegistration(
                            str(name), path, str(project), reach, _fetches_aisquare(spec)
                        )
                        for name, spec in servers.items()
                        if _runs_aisquare(spec)
                    )
    found.extend(_project_registrations(listed))
    return tuple(found), logins


def _project_registrations(
    listed: dict[str, list[tuple[Path, dict[str, Any]]]],
) -> list[McpRegistration]:
    """aisquare's MCP servers in the ``.mcp.json`` of every project a ``.claude.json``
    lists (``listed``: each project's blocks, each beside the file it was read from) —
    project scope.

    A repository's committed ``.mcp.json`` starts its servers in every session there,
    and none of them was read: one running aisquare through ``uvx`` made the purged
    home again at the next session in that repository (review of #257). A server the
    operator declined there never starts and is left out, but only if every block that
    lists the project declined it. Each block answers for the sessions Claude Code
    starts under its config dir and its spelling of the project, and one that has not
    answered asks at its next session there, where an approval starts the server: the
    lists were merged across config dirs, and one dir's no left out a server another
    never answered (round 26). A block declines a name in its ``disabledMcpjsonServers``
    and not in its ``enabledMcpjsonServers``, as the doctor reads one
    (``diagnostics._declined_project_servers``). A project reached under two spellings
    is one ``.mcp.json``, read once, with every spelling's blocks. Each entry keeps the
    files whose blocks have not declined it (``asked_in``), so a purge can drop the
    blocks it deletes (:attr:`UninstallPlan.lasting_mcp`).
    """
    found: list[McpRegistration] = []
    blocks_of: dict[Path, list[tuple[Path, dict[str, Any]]]] = {}
    spelled: dict[Path, str] = {}
    for project, blocks in listed.items():
        if not Path(project).is_absolute():
            continue  # another platform's path, or none: read here, it would name the cwd's
        path = agent_core.dir_identity(Path(project) / ".mcp.json")
        spelled.setdefault(path, project)
        blocks_of.setdefault(path, []).extend(blocks)
    for path, blocks in blocks_of.items():
        servers = agent_core.read_json(path).get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for name, spec in servers.items():
            asked_in = tuple(
                dict.fromkeys(file for file, block in blocks if not _declines(block, str(name)))
            )
            if _runs_aisquare(spec) and asked_in:
                found.append(
                    McpRegistration(
                        str(name),
                        path,
                        spelled[path],
                        None,
                        _fetches_aisquare(spec),
                        shared=True,
                        asked_in=asked_in,
                    )
                )
    return found


def _declines(block: dict[str, Any], name: str) -> bool:
    """Whether one ``.claude.json`` project block declined the ``.mcp.json`` server
    ``name``: listed in its ``disabledMcpjsonServers`` and not in its
    ``enabledMcpjsonServers`` (a name in both is approved, as the doctor reads it)."""

    def named(field: str) -> bool:
        names = block.get(field)
        return isinstance(names, list) and name in (str(item) for item in names)

    return named("disabledMcpjsonServers") and not named("enabledMcpjsonServers")


def mcp_place(entry: McpRegistration) -> str:
    """Where an MCP registration is: its ``.claude.json``, and the project it belongs to;
    a project-scope one's ``.mcp.json``, which names the project itself."""
    if entry.shared:
        return str(entry.file)
    return str(entry.file) + (f", project {entry.project}" if entry.project else "")


def mcp_removal(entry: McpRegistration) -> str:
    """The command that removes ``entry``: ``claude mcp remove`` at its scope, aimed at the
    ``.claude.json`` that holds it, and for a project's entry run from inside the project.

    ``claude mcp remove`` acts on the file ``CLAUDE_CONFIG_DIR`` names (``~/.claude.json``
    when it is unset), and its local scope is the project it runs in; its project scope
    is that project's ``.mcp.json``, whatever the variable names. Bare, it answered
    "No MCP server named …" for a project's entry run from elsewhere and for one in
    another config dir, and left both (measured on Claude Code 2.1.296, review of #257).
    Built as :func:`agent_core.claude_plugin_command` builds the plugin's: nothing for
    the file this shell's ``claude`` already reads, ``env -u`` for ``~/.claude.json``
    from a shell that names another, and the directory otherwise, every part quoted.
    Native Windows has no ``env -u``, ``VAR=value command`` or (in PowerShell 5.1)
    ``&&``, so there the folder and the variable the command needs are said after it.
    """
    scope = "project" if entry.shared else "user" if entry.project is None else "local"
    command = install_route.command_line(["claude", "mcp", "remove", "--scope", scope, entry.name])
    ambient = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    here = agent_core.dir_identity(Path(ambient)) if ambient else None
    if entry.shared:
        # Project scope is the `.mcp.json` of the directory it runs in, whatever config dir
        # the shell names: only the project is needed.
        there = here
    elif entry.config_dir is None:
        there = None
    else:
        there = agent_core.dir_identity(entry.config_dir)
    if sys.platform == "win32":
        needs = [] if entry.project is None else [f"in {entry.project}"]
        if there != here:
            needs.append(
                "with CLAUDE_CONFIG_DIR unset"
                if entry.config_dir is None
                else f"with CLAUDE_CONFIG_DIR set to {entry.config_dir}"
            )
        return f"{command} (run it {', '.join(needs)})" if needs else command
    if there == here:
        line = command
    elif entry.config_dir is None:
        line = f"env -u CLAUDE_CONFIG_DIR {command}"
    else:
        line = f"CLAUDE_CONFIG_DIR={install_route.command_line([str(entry.config_dir)])} {command}"
    if entry.project is None:
        return line
    return f"cd {install_route.command_line([entry.project])} && {line}"


def mcp_note(entry: McpRegistration) -> str:
    """What an uninstall leaves registered: one MCP server that runs aisquare, and its removal."""
    return (
        f"the MCP server {entry.name} is still registered in {mcp_place(entry)}, and Claude "
        f"Code owns that file — remove it: {mcp_removal(entry)}"
    )


def _plugins(directories: Iterable[Path]) -> tuple[agent_core.ClaudePlugin, ...]:
    """The aisquare plugin wherever these config dirs enable it: in the dir itself (user
    scope), and in each repository a project- or local-scope install of theirs enables
    (sweep of #257: those were never read, so --purge went ahead and the next session
    in the repository made the home again). Read only; empty where the plugin route
    does not run (:func:`agent_core.plugin_route_supported`)."""
    if not agent_core.plugin_route_supported():
        return ()
    found: list[agent_core.ClaudePlugin] = []
    seen: set[Path] = set()
    for directory in directories:
        key = agent_core.dir_identity(directory)
        if key in seen:
            continue
        seen.add(key)
        user = agent_core.claude_plugin(directory)
        if user is not None:
            found.append(user)
        found.extend(agent_core.claude_repo_plugins(directory))
    return tuple(found)


def _claude_dirs_for_mcp() -> list[Path]:
    """``~/.claude`` and every ``~/.claude*`` directory, hooked or not, for the MCP scan.

    ``claude mcp add`` on a plain install writes ``~/.claude.json``, beside
    ``~/.claude``, whether or not that directory carries hooks; reading a few
    small files is cheap next to telling nobody about a server that will fail to
    start once the package is gone. The siblings are the doctor's
    (``agent_core.claude_dir_siblings``), so the two scans agree on which there are.
    """
    default = accounts_core.home_config_dir()
    return [default, *(p for p in agent_core.claude_dir_siblings() if p != default)]


_LIVE_ROWS = (
    "SELECT f.label, f.tmux_socket, p.root, f.created_at, {recorded} FROM fleet_agent AS f "
    "LEFT JOIN project AS p ON p.id = f.project_id WHERE f.ended_at IS NULL"
)
#: The facts a row has recorded since v26 (``FleetAgent.socket_path``, ``.boot_id``,
#: ``.server_started_at``), then what a v25 store answers in their place, then a store from
#: before them: nothing recorded. Tried in that order, so a store this plan does not migrate
#: answers with everything it has (round 25 of #257; follow-up).
_RECORDED = (
    "f.socket_path, f.boot_id, f.server_started_at",
    "f.socket_path, f.boot_id, NULL",
    "NULL, NULL, NULL",
)
#: One live fleet row: ``(label (project), socket, created, socket path, boot id, server
#: started)``, the last three what its spawn recorded of where its server listened, in which
#: boot, and when that server started, else ``None``.
_LiveRow = tuple[str, str, datetime | None, Path | None, str | None, datetime | None]


def _siblings_hiding_hooks() -> list[Path]:
    """``~/.claude*`` directories whose aisquare hooks only show without strict parsing.

    The on-disk scan behind :func:`agent_core.hook_dirs` reads leniently since
    #247, so a sibling whose settings.json is Latin-1 or not valid JSON reads as
    having no hooks there, though Claude Code may still run them. Those are found
    here, and the plan then names them as sites it cannot clean, which keeps the
    package (review of #254). A sibling this user cannot read at all stays
    skipped, as the scan's own docstring rules: it cannot be shown to carry our
    hooks, and another account's backup must not block an uninstall forever.
    """
    found: list[Path] = []
    for directory in _claude_dirs_for_mcp():
        try:
            raw = (directory / "settings.json").read_bytes()
        except OSError:
            continue
        if lenient_hook_commands(lenient_text(raw)):
            found.append(directory)
    return found


def _live_fleet_agents() -> tuple[tuple[_LiveRow, ...], str | None]:
    """The fleet's live rows (:class:`_LiveRow`), or why they could not be read.

    Read with a plain ``query_only`` connection, never through ``store_session``:
    that runs ``ensure_home`` (recreating ``cache/`` and ``log/``) and migrates
    the schema, and the plan writes nothing (review of #253). As the last
    connection, its close leaves no ``-wal``/``-shm`` behind; a ``mode=ro`` one
    would (measured). Opened only when ``context.db`` exists and is not empty. A
    store from before the fleet has no rows to count, and one from before v25 has
    rows with nothing recorded; any other store that cannot answer is reported,
    never raised — a damaged board is no reason to keep a user from removing the tool.
    """
    database = paths.db_path()
    try:
        if not database.is_file() or database.stat().st_size == 0:
            return (), None
    except OSError as exc:
        return (), str(exc)
    try:
        connection = sqlite3.connect(str(database), timeout=2.0)
    except sqlite3.Error as exc:
        return (), str(exc) or type(exc).__name__
    try:
        connection.execute("PRAGMA query_only = ON")
        for recorded in _RECORDED:
            try:
                rows = connection.execute(_LIVE_ROWS.format(recorded=recorded)).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such column" not in str(exc) or recorded == _RECORDED[-1]:
                    raise
                continue
            break
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return (), None  # a store from before the fleet: nothing can be live
        return (), str(exc) or type(exc).__name__
    except sqlite3.Error as exc:
        return (), str(exc) or type(exc).__name__
    finally:
        connection.close()
    shown = _shown_as(tuple((label, root) for label, _, root, *_ in rows))
    return (
        tuple(
            (
                shown[label, root],
                socket_name or "asq",
                _created(created),
                Path(path) if path else None,
                booted or None,
                _created(started) if started else None,
            )
            for label, socket_name, root, created, path, booted, started in rows
        ),
        None,
    )


def _shown_as(rows: Sequence[tuple[str, str | None]]) -> dict[tuple[str, str | None], str]:
    """How each live row ``(label, project root)`` is named in the plan: ``<label>
    (<folder>)``, as the refusal reads at a glance, or ``<label> (<root path>)`` where
    that would collide.

    A label names only its project's folder, and two projects can sit in folders of one
    name (``~/work/api``, ``~/oss/api``): with a live manager each, the refusal printed
    ``manager (api)`` twice, and ``--json``'s ``live_agents`` could not tell them apart
    either (round 29 of #257; follow-up). A row with no project is ``unknown project``,
    which has no root to show.
    """
    folders: dict[tuple[str, str], set[str]] = {}
    for label, root in rows:
        if root:
            folders.setdefault((label, Path(root).name), set()).add(root)
    shown: dict[tuple[str, str | None], str] = {}
    for label, root in rows:
        if not root:
            shown[label, root] = f"{label} (unknown project)"
        elif len(folders[label, Path(root).name]) > 1:
            shown[label, root] = f"{label} ({root})"
        else:
            shown[label, root] = f"{label} ({Path(root).name})"
    return shown


def _created(value: object) -> datetime | None:
    """A row's ``created_at`` as the store wrote it (ISO 8601, UTC), or ``None`` unread."""
    try:
        when = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _tmux_unusable() -> str | None:
    """Why ``fleet shutdown`` would refuse the tmux here, or ``None``: its own test
    (``fleet._require_usable_tmux``), a tmux missing or too old, or one whose ``tmux -V``
    did not answer. Asking only whether it is on PATH named the shutdown for a tmux it then
    refused (round 28 of #257)."""
    try:
        TmuxServer().require()
    except TmuxError as exc:  # TmuxUnavailable included
        return str(exc)
    return None


def _may_run(row: _LiveRow, reads: SocketReads | None = None) -> SocketAnswer | None:
    """Whether the agent of live ``row`` may still be running, and why; ``None`` when it
    cannot be. :func:`~aisquare.core.tmux.server_may_run`, the rule ``fleet shutdown`` asks
    too, so a row counted here is never one it ends as gone (round 27 of #257): one whose
    socket is gone counts until ``fleet shutdown --forget`` ends it on the operator's word,
    unless it recorded no boot and predates this one (round 28). ``reads`` is the plan's,
    so rows on one server ask it once. Asked through :func:`~aisquare.core.tmux.asking_server`,
    which writes nothing and waits seconds, not 30, for a server that does not answer.
    """
    _label, socket_name, created, recorded, booted, started = row
    return server_may_run(
        asking_server(socket_name), recorded, booted, created, started, reads=reads
    )


def _fleet_answers() -> tuple[tuple[tuple[str, SocketAnswer], ...], int, str | None]:
    """:func:`running_fleet`, with what each live row's socket said (:func:`_may_run`)."""
    rows, fleet_error = _live_fleet_agents()
    reads = SocketReads()  # one per server, and the boot once, for every row
    live = tuple((row[0], answer) for row in rows if (answer := _may_run(row, reads)) is not None)
    return live, len(rows) - len(live), fleet_error


def running_fleet() -> tuple[tuple[str, ...], int, str | None]:
    """``(live, unlistened, error)``: the fleet agents that may be running, the live rows
    that cannot be (:func:`_may_run`), and why the fleet could not be read. The one count
    upgrade and uninstall both act on.
    """
    live, unlistened, fleet_error = _fleet_answers()
    return tuple(label for label, _ in live), unlistened, fleet_error


def running_remote() -> str | None:
    """The Remote on for this home in another process, named, or ``None``: an ``aisquare
    remote serve`` in another shell or another asq's R panel, which upgrade warns about and
    uninstall refuses for (``services.remote_server.remote_served_by``). Asked of the lock
    the Remote holds while it is on, so it creates nothing, as the plans promise.

    Imported here, not at the top: ``aisquare``'s import must not load the server
    (tests/test_remote_stays_off_the_hook_path.py), and the plans ask once. asq's own Remote
    is off by the time its Update or Uninstall runs this, as it stops it before handing over.
    """
    from aisquare.services import remote_server

    return remote_server.remote_served_by()


def plugin_answers(cache: str | None = None) -> Path:
    """The file the Claude Code plugin's launcher keeps uv's answers in.

    ``${XDG_CACHE_HOME:-~/.cache}/aisquare/plugin-tested-python``, as
    plugins/claude-code/scripts/aisquare-hook names it, ``cache`` standing for
    ``XDG_CACHE_HOME`` (this shell's when ``None``; :func:`agent_core.xdg_cache_home`):
    whether uv has a Python aisquare is tested on, per uv and its settings. The one file
    aisquare writes outside the home, so ``--purge`` would have left it, holding a uv
    path (round 20 of #257).
    """
    return agent_core.xdg_cache_home(cache) / "aisquare" / "plugin-tested-python"


#: Where a purge looks for the plugin's answers, and what it cannot see: said with every
#: purge, in its plan and its report (round 27 of #257).
ANSWERS_LOOKED_FOR = (
    "the plugin's answers about uv are looked for under $XDG_CACHE_HOME, ~/.cache and each "
    "Flatpak app's cache (~/.var/app/*/cache); a Claude Code run under any other "
    "XDG_CACHE_HOME keeps them in aisquare/plugin-tested-python there, to delete by hand"
)


def _flatpak_caches() -> list[Path]:
    """Each Flatpak app's cache, ``~/.var/app/<app id>/cache``: the ``XDG_CACHE_HOME`` a
    Flatpak sets for the app it runs, an IDE whose terminal runs Claude Code among them."""
    apps = agent_core.home() / ".var" / "app"
    try:
        names = sorted(os.listdir(apps))
    except OSError:
        return []
    return [apps / name / "cache" for name in names]


def _plugin_answers_there() -> tuple[Path, ...]:
    """Each place :func:`plugin_answers` may be holding something of the launcher's: the
    file (or a link by its name), or what a launcher cut short left beside it
    (:func:`_answers_cut_short`), with or without the file (round 26 of #257).

    Asked under this shell's ``XDG_CACHE_HOME``, under ``~/.cache``, and under each
    Flatpak app's cache (:func:`_flatpak_caches`). The launcher runs with the variables
    Claude Code started with, which this shell cannot read: a Claude Code started from a
    desktop launcher or an IDE often has none of the shell's, and one in a Flatpak IDE's
    terminal has that app's own (round 27). Any other is past what this can see, which
    every purge says (:data:`ANSWERS_LOOKED_FOR`).
    """
    places: dict[str, Path] = {}
    caches = (plugin_answers(str(cache)) for cache in _flatpak_caches())
    for answers in (plugin_answers(), plugin_answers(""), *caches):
        places.setdefault(os.path.realpath(answers.parent), answers)  # one folder, asked once
    found: list[Path] = []
    for answers in places.values():
        there = os.path.lexists(answers) and not os.path.isdir(answers)
        if there or _answers_cut_short(answers):
            found.append(answers)
    return tuple(found)


def uninstall_plan(*, purge: bool = False) -> UninstallPlan:
    """Decide what ``aisquare uninstall`` would do. Reads only; never creates the home."""
    route = install_route.detect()
    candidates = [*agent_core.hook_dirs(HOOK_AGENT), *_account_dirs(), *_siblings_hiding_hooks()]
    hooks: list[HookSite] = []
    unreadable: list[HookSite] = []
    seen: set[Path] = set()
    for directory in candidates:
        key = agent_core.dir_identity(directory)
        if key in seen:
            continue
        seen.add(key)
        # Hooks of ours that cannot be taken out: the run would fail on the write, so the
        # plan says so first and keeps the package (review of #257). The rule
        # `agents disconnect` asks too.
        binaries, error = hooks_stuck(directory)
        if error is not None:
            unreadable.append(HookSite(directory, reason=error))
        elif binaries:
            hooks.append(HookSite(directory, tuple(str(b.program) for b in binaries)))
    home = paths.aisquare_home()
    try:
        entries = tuple(sorted(c.name for c in home.iterdir())) if home.is_dir() else ()
    except OSError:
        entries = ()
    answers, unlistened, fleet_error = _fleet_answers()
    live = tuple(label for label, _ in answers)
    custom = custom_home(home)
    claude_dirs = [*candidates, *_claude_dirs_for_mcp()]
    mcp, logins = _mcp_registrations(claude_dirs)
    return UninstallPlan(
        route=route,
        hooks=tuple(hooks),
        unreadable=tuple(unreadable),
        mcp=mcp,
        package_argv=tuple(install_route.remove_argv(route)),
        package_env=install_route.installer_env(route),
        package_reason=install_route.not_removable(route),
        home=home,
        home_exists=_home_there(home)[0],
        home_entries=entries,
        accounts=_accounts_kept(logins),
        keychain=accounts_core.keychain_platform(),
        purge=purge,
        purge_refusal=purge_refusal(home, custom=custom),
        live_agents=live,
        unlistened=unlistened,
        fleet_error=fleet_error,
        tmux_unusable=_tmux_unusable() if live else None,
        unreached=tuple(label for label, answer in answers if answer != "answers"),
        taken=tuple(label for label, answer in answers if answer == "another"),
        listening=tuple(label for label, answer in answers if answer == "answers"),
        plugins=_plugins(claude_dirs),
        plugin_answers=_plugin_answers_there(),
        remote=running_remote(),
    )


def uninstall(plan: UninstallPlan) -> UninstallReport:
    """Carry out the plan up to — not including — removing the package.

    Raises :class:`UninstallRefused` before touching anything when the plan
    refuses. Each directory's hooks are removed on their own: one that fails is
    recorded and the rest still go (fail open per directory). The package step
    is the caller's (:func:`remove_package`), and only when
    :attr:`UninstallReport.package_runs` says nothing failed.
    """
    refusal = plan.refusal
    if refusal is not None:
        raise refusal
    removals: list[HookRemoval] = []
    for site in plan.hooks:
        try:
            agent_core.remove_hooks(HOOK_AGENT, site.config_dir)
            left = agent_core.hook_commands(HOOK_AGENT, site.config_dir)
        except Exception as exc:  # fail open per directory; the report names it
            removals.append(HookRemoval(site.config_dir, False, str(exc) or type(exc).__name__))
            continue
        if left:
            removals.append(
                HookRemoval(site.config_dir, False, f"{len(left)} aisquare hook(s) still there")
            )
        else:
            removals.append(HookRemoval(site.config_dir, True))
    # A site that could not be read may still hold our hooks, and nothing here can
    # take them out: a failure like any other, so the package stays and the
    # command is still there to run again (review of #253). One inside the home
    # waits for the purge, which deletes it, hooks and all (sweep of #257).
    blocking = plan.blocking
    removals.extend(HookRemoval(site.config_dir, False, site.reason) for site in blocking)
    purged, purge_error = False, None
    hooks_failed = any(not removal.ok for removal in removals)
    if plan.purges and not hooks_failed:
        purged, purge_error = _purge(plan.home)
    elif plan.purge and plan.home_exists and hooks_failed:
        purge_error = "not attempted: hooks were left in a directory above"
    # Outside the home, so asked of --purge, not of the home's delete: a home already gone
    # left them otherwise (round 27 of #257).
    answers_deleted = plan.deletes_answers and not hooks_failed
    answers_errors: list[tuple[Path, str]] = []
    for answers in plan.plugin_answers if answers_deleted else ():
        error = _delete_plugin_answers(answers)
        if error is not None:
            answers_errors.append((answers, error))
    if not purged:
        # The purge that would have taken them did not happen: they are still there.
        removals.extend(
            HookRemoval(site.config_dir, False, site.reason)
            for site in plan.unreadable
            if site not in blocking
        )
    record_error: str | None = None
    if not purged and os.path.isfile(paths.agents_registry_path()):
        # The home survives — no purge, or one not attempted or not finished — so
        # it must not claim a connection that is gone: any recorded site with no
        # aisquare hook left, whether this run removed them or they were already
        # gone. Only when agents.json exists: set_connected creates the home. Asked with
        # os.path, which never raises: a home this user cannot enter has none to read.
        try:
            _unrecord_hookless()
        except Exception as exc:  # a record, not the hooks: reported, never a stop
            record_error = str(exc) or type(exc).__name__
    return UninstallReport(
        plan,
        hooks=tuple(removals),
        record_error=record_error,
        purged=purged,
        purge_error=purge_error,
        answers_deleted=answers_deleted,
        plugin_answers_errors=tuple(answers_errors),
        notes=tuple(
            _uninstall_notes(plan, removals, purged=purged, answers_deleted=answers_deleted)
        ),
    )


def _delete_plugin_answers(answers: Path) -> str | None:
    """Delete the plugin's answers about uv, with any a launcher stopped while writing them
    left beside the file (:func:`_answers_cut_short`), and the folder its launcher made for
    them when nothing else is in it; why not, when it could not. A link is unlinked, never
    followed."""
    try:
        for found in (answers, *_answers_cut_short(answers)):
            found.unlink(missing_ok=True)
    except OSError as exc:
        return str(exc) or type(exc).__name__
    # Something else kept there, or the folder gone already: not ours to delete.
    with contextlib.suppress(OSError):
        answers.parent.rmdir()
    return None


def _answers_cut_short(answers: Path) -> list[Path]:
    """What a launcher stopped between writing its answers beside their file and moving them
    over it left there: ``<file>.<pid>`` (plugins/claude-code/scripts/aisquare-hook)."""
    try:
        names = os.listdir(answers.parent)
    except OSError:
        return []
    prefix = f"{answers.name}."
    return [
        answers.parent / name
        for name in names
        if name.startswith(prefix) and name.removeprefix(prefix).isdigit()
    ]


def _unrecord_hookless() -> None:
    """Drop every recorded agent site that carries no aisquare hook from agents.json.

    Every agent, not only Claude Code: ``agents connect codex`` records a site
    that never had hooks, and an uninstalled aisquare connects nothing. A site
    whose settings.json cannot be read keeps its record — it may still hold ours.
    """
    registry = agent_core.read_json(paths.agents_registry_path())
    names = set(registry.get("connected") or []) | set(registry.get("connections") or {})
    for name in sorted(str(n) for n in names):
        for directory in agent_core.connected_dirs(name, registry):
            binaries, error = hook_binaries(directory) if name == HOOK_AGENT else ([], None)
            if error is None and not binaries:
                agent_core.set_connected(name, False, directory)


def _purge(home: Path) -> tuple[bool, str | None]:
    """Delete the home, after asking the guard again right before the delete.

    ``shutil.rmtree`` never follows a link inside the tree — it removes the link —
    and refuses a link at the top; the guard has already refused that one.
    """
    refusal = purge_refusal(home, custom=custom_home(home))
    if refusal is not None:
        return False, refusal
    try:
        # The markers LAST: a delete that stops partway leaves them, so the retry
        # the report promises is still recognised as an aisquare home (review of
        # #253). A link is unlinked, never followed; rmtree inside does the same.
        for child in sorted(home.iterdir(), key=lambda entry: entry.name in HOME_MARKERS):
            if _is_link(child) or not child.is_dir():
                child.unlink()
            else:
                shutil.rmtree(child)
        home.rmdir()
    except OSError as exc:
        return False, f"{home} was only partly deleted ({exc})"
    return True, None


def _uninstall_notes(
    plan: UninstallPlan,
    removals: Iterable[HookRemoval],
    *,
    purged: bool = False,
    answers_deleted: bool = False,
) -> list[str]:
    notes: list[str] = []
    if any(removal.ok for removal in removals):
        notes.append("open Claude Code sessions keep the hooks they started with — restart them")
    if answers_deleted:
        notes.append(ANSWERS_LOOKED_FOR)
    for entry in plan.lasting_mcp if purged else plan.mcp:
        notes.append(mcp_note(entry))
    for plugin in plan.lasting_plugins if purged else plan.plugins:
        notes.append(plugin_note(plugin))
    notes.append(
        "a running `aisquare serve` keeps running until you stop it; uv, tmux, Node, gh "
        "and Claude Code stay installed"
    )
    return notes


def plugin_note(plugin: agent_core.ClaudePlugin) -> str:
    """What an uninstall leaves running in a directory that enables the aisquare plugin."""
    return (
        f"the aisquare plugin is still enabled in {plugin_place(plugin)}, so Claude Code keeps "
        f"running aisquare there (through uvx once the package is gone) — remove it: "
        f"{plugin_removal(plugin)}"
    )


def remove_package(plan: UninstallPlan, *, stdout_to_stderr: bool) -> None:
    """Hand this process to the package manager. Returns only if it could not start.

    The LAST step on purpose: the hooks are already gone, so no session can fire
    one at the program while it disappears (and if any were left, uninstall
    stopped before this).
    """
    if plan.package_reason is not None:
        return
    install_route.exec_replace(
        plan.package_argv, env=plan.package_env, stdout_to_stderr=stdout_to_stderr
    )
