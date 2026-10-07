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

from dataclasses import dataclass
from pathlib import Path

from aisquare.core import agents as agent_core
from aisquare.core import credentials as credentials_store
from aisquare.core import paths
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    load_config,
    save_config,
)
from aisquare.core.store import store_session
from aisquare.core.stubs import stub
from aisquare.core.version import __version__
from aisquare.core.workspace import current_project
from aisquare.models import SetupReport
from aisquare.services import agents as agents_service
from aisquare.services import explainability as explainability_service
from aisquare.services import install_route
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
    """Set up ``~/.aisquare``, register & snapshot the project, and connect agents.

    Idempotent and non-interactive: safe to re-run (``assume_yes`` is therefore
    moot for now). ``reinit`` resets ``config.toml`` to defaults. Agents named via
    ``--agent`` are connected (hooks installed + context ingested); cloud auth is
    not wired yet.
    """
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

    project = current_project(path)
    with store_session() as store:
        store.onboard_project(project)  # init is the deliberate add (#139)

    notes: list[str] = []
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
        if report.snapshot is not None and report.snapshot.status == "ready":
            notes.append(
                f"Snapshot: {report.snapshot.file_count} files, "
                f"{report.snapshot.token_count} tokens packed for fast agent context."
            )
        elif report.snapshot is None:
            notes.append("Codebase snapshot skipped (repomix/Node not available).")

    for agent in agents:
        try:
            connection = agents_service.connect(agent)
        except (KeyError, ValueError) as exc:
            notes.append(f"Could not connect {agent}: {exc}")
            continue
        hook_note = "hooks installed" if connection.hooks_installed else "no hooks for this agent"
        notes.append(f"Connected {agent}: {hook_note}, imported {connection.imported} entries.")

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

    @property
    def runnable(self) -> bool:
        return self.reason is None

    @property
    def command(self) -> str:
        return install_route.command_line(self.argv)

    @property
    def latest_version(self) -> str | None:
        return self.latest.version if self.latest is not None else None

    @property
    def destination(self) -> str:
        """Where the upgrade goes, for a person: a version, or what ``@latest`` means."""
        return self.target or self.latest_version or "the latest release"

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


@dataclass(frozen=True)
class UpgradeReport:
    """What an upgrade did. ``problem`` is set when the new version could not be confirmed."""

    plan: UpgradePlan
    exit_code: int
    version: str | None = None
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
    refused offline, and a pin needs no lookup.
    """
    if target is not None:
        pinned = install_route.version_argument(target)
        if pinned is None:
            raise InvalidVersion(target)
        target = pinned
    route = install_route.detect()
    reason = install_route.not_automated(route)
    latest = install_route.fetch_latest() if check or (reason is None and target is None) else None
    refresh: tuple[HookSite, ...] = ()
    left: tuple[HookSite, ...] = ()
    if reason is None:
        refresh, left = refresh_sites(route.facts)
    return UpgradePlan(
        route=route,
        current=__version__,
        target=target,
        latest=latest,
        argv=tuple(install_route.upgrade_argv(route, target)),
        env=install_route.installer_env(route),
        reason=reason,
        refresh=refresh,
        left=left,
    )


def _identity(path: Path) -> Path:
    """One spelling for the several a directory can have (``~``, symlinks)."""
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


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
        return _identity(binary.program.parent) == _identity(found.executable.parent)
    program = _identity(binary.program)
    prefix = _identity(found.prefix)
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
        key = _identity(directory)
        if key in seen:
            continue
        seen.add(key)
        try:
            commands = agent_core.hook_commands(HOOK_AGENT, directory)
        except OSError as exc:
            left.append(HookSite(directory, reason=f"its settings.json could not be read ({exc})"))
            continue
        binaries: list[agent_core.HookBinary] = []
        for command in commands:
            binary = agent_core.hook_binary(command)
            if binary is not None and binary not in binaries:
                binaries.append(binary)
        if not binaries:
            continue
        programs = tuple(str(binary.program) for binary in binaries)
        foreign = next(
            (b for b in binaries if b.program.exists() and not runs_this_install(b, found)),
            None,
        )
        if foreign is not None:
            left.append(
                HookSite(
                    directory,
                    programs,
                    f"its hooks run {foreign.program}, another install of aisquare",
                )
            )
        else:
            refresh.append(HookSite(directory, programs))
    return tuple(refresh), tuple(left)


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
        return UpgradeReport(plan, exit_code=code, problem=f"{plan.argv[0]} exited {code}")
    version, problem = _verify(plan)
    if problem is not None:
        return UpgradeReport(plan, exit_code=code, version=version, problem=problem)
    notes: list[str] = []
    latest = plan.latest_version
    moved_elsewhere = latest is not None and not install_route.same_version(version or "", latest)
    if plan.target is None and version is not None and moved_elsewhere:
        notes.append(f"PyPI's latest is {latest}; your package index served {version}")
    notes.append(
        f"asq and `aisquare serve` processes that were already running keep {plan.current} "
        "until they are restarted"
    )
    hooks = tuple(_refresh(site, plan.route.facts) for site in plan.refresh)
    return UpgradeReport(plan, exit_code=code, version=version, hooks=hooks, notes=tuple(notes))


def _first_line(*texts: str) -> str:
    """The last non-empty line of the first text that has one — where a CLI says why."""
    for text in texts:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return lines[-1].removeprefix("✗ ")
    return ""


def _verify(plan: UpgradePlan) -> tuple[str | None, str | None]:
    """``(version, problem)`` from asking the NEW install its version in a new process.

    The exit code of the installer is not the evidence: §3.9.1's failure was a
    success code over an unchanged version. So success is a version the new
    process reports — the pin when one was asked for, otherwise any MOVE. An
    unchanged version is a failure exactly when PyPI said there is something
    newer; when PyPI could not be asked it means the index had nothing newer.
    """
    argv = [str(plan.route.facts.executable), "-P", "-m", "aisquare", "--version"]
    answer = install_route.run_captured(argv, timeout=VERSION_CHECK_TIMEOUT_SECONDS)
    if answer.error is not None:
        return None, f"the new install could not be started ({answer.error})"
    if answer.returncode != 0:
        said = _first_line(answer.stderr, answer.stdout) or f"exit {answer.returncode}"
        return None, f"the new install could not be started ({said})"
    found = install_route.version_in(answer.stdout)
    if found is None:
        return None, "the new install did not report a version"
    if plan.target is not None:
        if install_route.same_version(found, plan.target):
            return found, None
        return found, f"{plan.target} was asked for, but the new install reports {found}"
    latest = plan.latest_version
    if not install_route.same_version(found, plan.current) or latest is None:
        return found, None
    return found, (
        f"uv reported success but aisquare still reports {found}, not {latest} — the "
        "silent no-op docs/plans/one-line-install.md §3.9.1 describes"
    )


def _refresh(site: HookSite, found: install_route.Facts) -> HookRefresh:
    """Re-connect one site BY the new install, so its hooks name the new install.

    The console script beside the interpreter, not ``python -m aisquare``: hooks
    are written for whatever program ``agents connect`` ran as, and the module
    form would fall back to whichever ``aisquare`` comes first on PATH.
    """
    script = found.executable.with_name("aisquare.exe" if found.platform == "win32" else "aisquare")
    if not script.exists():
        return HookRefresh(site.config_dir, False, f"the new install has no {script}")
    argv = [str(script), "agents", "connect", HOOK_AGENT, "--config-dir", str(site.config_dir)]
    answer = install_route.run_captured(argv, timeout=HOOK_REFRESH_TIMEOUT_SECONDS)
    if answer.error is not None:
        return HookRefresh(site.config_dir, False, answer.error)
    if answer.returncode != 0:
        said = _first_line(answer.stderr, answer.stdout) or f"exit {answer.returncode}"
        return HookRefresh(site.config_dir, False, said)
    return HookRefresh(site.config_dir, True)


def uninstall() -> None:
    """Remove agent hooks and optionally wipe local data."""
    stub("uninstall")
