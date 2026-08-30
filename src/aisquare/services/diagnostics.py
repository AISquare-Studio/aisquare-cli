"""Health checks and introspection."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from aisquare.core import agents as agent_core
from aisquare.core import brain as brain_core
from aisquare.core import harness, orchestrator, paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core import tmux as tmux_core
from aisquare.core.config import load_config
from aisquare.core.injection import load_last
from aisquare.core.store import damaged_store_recovery, store_session
from aisquare.core.stubs import stub
from aisquare.core.workspace import active_project
from aisquare.models import (
    CheckStatus,
    DoctorCheck,
    FleetAgent,
    InjectionRecord,
    PromptRecord,
    ShippingStatus,
    StatusReport,
)
from aisquare.services import distill as distill_service
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops
from aisquare.services import fleet as fleet_service


def status() -> StatusReport:
    """Summarise installation health, pools, the active project and agents."""
    # Compute "initialized" before opening the store, which would create the db.
    initialized = paths.config_path().exists() or paths.db_path().exists()
    agents = agent_core.detect_all()
    with store_session() as store:
        project = active_project(store)
        report = StatusReport(
            home=paths.aisquare_home(),
            initialized=initialized,
            user_entries=len(store.entries("user")),
            project_entries=len(store.entries("project", project_id=project.id)),
            active_project=project,
            project_count=len(store.list_projects()),
            agents_detected=[agent.name for agent in agents if agent.detected],
            agents_connected=[agent.name for agent in agents if agent.connected],
            shipping=_shipping_status(),
        )
    return report


def _shipping_status() -> ShippingStatus | None:
    """Explainability shipping, or ``None`` when this install never opted in.

    Silent by construction on an untouched machine: the line only appears once
    someone configured shipping, or while records from a previous configuration
    are still buffered — a queue that is quietly filling must never be
    invisible. Never raises; ``status`` reporting on a broken spool is more
    useful than ``status`` dying with it.
    """
    try:
        state = explainability_service.shipping_state()
    except Exception:  # diagnostics must never crash
        return None
    if not state.configured and not (state.queued or state.dead):
        return None
    return ShippingStatus(
        configured=state.configured,
        queued=state.queued,
        sent=state.sent,
        dead=state.dead,
        reason=state.reason,
    )


def doctor(
    *, live: bool = False, target: str | None = None, cwd: Path | None = None
) -> list[DoctorCheck]:
    """Run health checks over the install, dependencies and integration.

    ``live`` opts into the checks that leave this machine (today: the
    explainability gateway round-trip). Everything else stays offline, so a
    plain ``aisquare doctor`` still answers on a train.

    ``cwd`` is the directory whose PROJECT the three project-scoped checks
    (snapshot, brain, harness) report on; ``None`` is the process cwd, which is
    what the CLI means. The fleet UI hosts many projects in one process and
    must not ``os.chdir`` (docs/plans/fleet-tui.md §5.6), so it passes the
    selected project's root here and gets that project's report in-process.
    The machine-wide checks ignore it — they are about this machine.
    """
    return [
        _check_python(),
        _check_install(),
        _check_provenance(),
        _check_home(),
        _check_home_filesystem(),
        _check_config(),
        _check_database(),
        _check_repomix(),
        _check_tiktoken(),
        _check_claude_code(),
        _check_tmux(),
        _check_gh(),
        _check_snapshot(cwd),
        _check_brain(cwd),
        _check_harness(cwd),
        _check_fleet(),
        *explainability_ops.checks(live=live, target_name=target),
    ]


def _ok(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.ok, detail=detail)


def _warn(name: str, detail: str, fix: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.warn, detail=detail, fix=fix)


def _fail(name: str, detail: str, fix: str) -> DoctorCheck:
    return DoctorCheck(name=name, status=CheckStatus.fail, detail=detail, fix=fix)


def _check_python() -> DoctorCheck:
    info = sys.version_info
    return _ok("python", f"Python {info.major}.{info.minor}.{info.micro}")


def _check_install() -> DoctorCheck:
    """Where aisquare runs from — the Claude Code hook needs a stable path."""
    binary = shutil.which("aisquare")
    if binary is None:
        return _warn(
            "install",
            "aisquare is not on your PATH",
            "Install as a global tool: pipx install aisquare",
        )
    if {".venv", "venv"} & set(Path(binary).parts):
        return _warn(
            "install",
            f"aisquare runs from a virtualenv ({binary})",
            "For stable Claude Code hooks, install globally: pipx install aisquare",
        )
    return _ok("install", f"aisquare at {binary}")


def _check_provenance() -> DoctorCheck:
    """Which SOURCE the installed build came from, not just which binary runs.

    ``--version`` cannot answer this: a build from this checkout and a build
    from a sibling worktree both report the same version string, which is how a
    stale install survived on this machine for a whole shift while five separate
    mechanisms were blamed for "which build am I running". pip records the answer
    in ``direct_url.json`` for anything installed from a path, and nothing was
    reading it.

    A DETECTOR: it reports so an operator can decide, and it never fails a
    machine. The one case worth a warning is a source directory that no longer
    exists — the install cannot be verified against it, cannot be reinstalled
    from it, and is by definition not the tree anyone is working in.
    """
    try:
        record = metadata.distribution("aisquare-cli").read_text("direct_url.json")
    except Exception:
        return _ok("provenance", "install source not recorded")
    if not record:
        return _ok("provenance", "installed from a package index")
    try:
        parsed = json.loads(record)
        url = str(parsed.get("url", ""))
        editable = bool(parsed.get("dir_info", {}).get("editable"))
    except (ValueError, AttributeError):
        return _ok("provenance", "install source not readable")
    if not url.startswith("file://"):
        return _ok("provenance", f"installed from {url}")

    source = Path(url[len("file://") :])
    kind = "editable" if editable else "non-editable"
    if not source.exists():
        return _warn(
            "provenance",
            f"installed ({kind}) from {source}, WHICH NO LONGER EXISTS",
            "This build cannot be checked against its source or reinstalled from "
            "it, and it is not the tree you are working in. Reinstall from the "
            "repo by absolute path: python3 -m pip install '/path/to/aisquare-cli[dev]'",
        )
    return _ok("provenance", f"installed ({kind}) from {source}")


def _check_home() -> DoctorCheck:
    home = paths.aisquare_home()
    if home.exists():
        return _ok("home", f"{home} exists")
    return _fail("home", f"{home} is missing", "Set it up: aisquare init")


#: Filesystems where ``os.replace`` is the kernel's own rename and the
#: durable-replace guarantee in ``core.config.save_config`` holds as measured.
_NATIVE_FILESYSTEMS = frozenset(
    {"ext2", "ext3", "ext4", "btrfs", "xfs", "zfs", "f2fs", "apfs", "hfs", "tmpfs", "overlay"}
)

#: Filesystems reached through a translation layer or a network. Nothing here is
#: known-broken; the point is that the atomicity of a rename is somebody else's
#: promise and this project has never measured it. 9p is how WSL exposes Windows
#: drives (/mnt/c), which is the reachable case: AISQUARE_HOME is taken verbatim.
_TRANSLATED_FILESYSTEMS = frozenset({"9p", "cifs", "smb3", "nfs", "nfs4", "fuseblk", "virtiofs"})

_MOUNTINFO = Path("/proc/self/mountinfo")


def filesystem_of(path: Path, mountinfo: Path = _MOUNTINFO) -> str | None:
    """Filesystem type ``path`` lives on, or None when it cannot be determined.

    Reads ``/proc/self/mountinfo`` and takes the LONGEST mount point that is a
    prefix of the path — mounts nest, and the first match is not the deepest, so
    a shorter match would report the parent's filesystem for anything mounted
    underneath it.

    Never raises. This feeds a diagnostic line, and a machine must not fail
    ``doctor`` because it has no /proc or an unreadable one; unknown is an
    honest answer and is reported as such.
    """
    try:
        target = path.resolve()
        best: tuple[int, str] | None = None
        for line in mountinfo.read_text(encoding="utf-8").splitlines():
            head, _, tail = line.partition(" - ")
            fields = head.split()
            rest = tail.split()
            if len(fields) < 5 or not rest:
                continue
            point = Path(fields[4])
            if target == point or point in target.parents:
                depth = len(point.parts)
                if best is None or depth > best[0]:
                    best = (depth, rest[0])
        return best[1] if best else None
    except Exception:
        return None


def _config_file_kind() -> str:
    """How config.toml exists: a plain file, a symlink and where to, or absent.

    Offered by @9bbc8ed7 into this same line rather than as a separate check, and
    it belongs here for the reason the filesystem does: it is invisible, chosen by
    the user, and it changes how a write behaves. Since save_config follows links,
    this is also the line that tells an operator their dotfiles link IS being
    honoured — the fact is only reassuring once it is visible.

    Never raises; a path we cannot stat is reported as unknown.
    """
    config = paths.config_path()
    try:
        if config.is_symlink():
            destination = os.path.realpath(config)
            if not os.path.exists(destination):
                # A dangling link is the one shape that makes a write reach
                # outside anything the operator named: save_config follows the
                # link and CREATES the missing directories at the target —
                # measured at four levels deep, and on a mounted Windows drive
                # if that is where the link points. Saying so before the write
                # is the whole value of this line.
                return f"symlink -> {destination} (TARGET MISSING)"
            return f"symlink -> {destination}"
        if config.is_file():
            return "regular file"
        return "not created yet"
    except OSError:
        return "unreadable"


def _check_home_filesystem() -> DoctorCheck:
    """Say out loud which filesystem the config lives on.

    ``save_config`` publishes changes with write-temp/fsync/rename/fsync-parent,
    and the atomicity of that rename is a property of the FILESYSTEM. It was
    measured on a native disk, which is where the default ``~/.aisquare`` sits.
    ``AISQUARE_HOME`` is taken verbatim, so a Windows-backed path is one export
    away — and until now nothing in the code could tell an operator which kind of
    path they were on.

    A DETECTOR, not a checker: it reports so the operator can decide, and it
    never fails. Being on a translated filesystem is not known-broken, it is
    unmeasured, and turning "unmeasured" into "broken" would be the mandate this
    project has been careful not to write into its own tools.
    """
    home = paths.aisquare_home()
    shape = _config_file_kind()
    kind = filesystem_of(home)
    if kind is None:
        return _ok("filesystem", f"{home} — filesystem not determined; config: {shape}")
    if kind in _TRANSLATED_FILESYSTEMS:
        return _warn(
            "filesystem",
            f"{home} is on {kind}, where atomic config writes are unverified (config: {shape})",
            "Config writes rely on os.replace being atomic. That holds on a local "
            "disk and has never been measured through a translation layer. If two "
            "sessions may write config at once, point AISQUARE_HOME at a native "
            "filesystem.",
        )
    if kind in _NATIVE_FILESYSTEMS:
        return _ok("filesystem", f"{home} on {kind}, config: {shape} — atomic writes hold")
    return _ok("filesystem", f"{home} on {kind} ({shape}) — atomicity unmeasured here")


def _check_config() -> DoctorCheck:
    try:
        load_config()
    except Exception as exc:  # diagnostics must never crash
        return _fail(
            "config", f"config.toml is invalid: {exc}", "Fix or reset: aisquare init --reinit"
        )
    return _ok("config", "config.toml is valid")


def _uncreated_home(name: str) -> DoctorCheck | None:
    """``None`` when the store can be opened without bringing a home into being.

    ``store_session`` calls ``ensure_home``, so a check that opens the store on
    a machine with no home CREATES the thing the ``home`` check is at that
    moment reporting as missing — and makes the next run exit 0 for no reason
    but that this one ran. Diagnosis must not be a side effect.

    Reported at ok status because ``home`` owns that verdict and already fails:
    a second failure here would send an operator to look at the database when
    the answer is that nothing has been set up yet.
    """
    if paths.aisquare_home().exists():
        return None
    return _ok(name, "not created yet — set it up: aisquare init")


def _check_database() -> DoctorCheck:
    absent = _uncreated_home("database")
    if absent is not None:
        return absent
    try:
        with store_session() as store:
            count = len(store.entries("user"))
    except Exception as exc:  # diagnostics must never crash
        # "Re-initialise: aisquare init" was measured CRASHING on every state
        # that reaches this line — 59 lines of traceback on a corrupt file, 72
        # on a store wedged mid-migration — and repairing neither. It is not
        # narrowed to corruption because THIS LINE HAS ALREADY ESTABLISHED THAT
        # THE STORE WILL NOT OPEN: whatever the cause, no history in it is
        # reachable by this CLI, and the recovery MOVES the file rather than
        # deleting it, so the bytes survive for a later fix. Shared with the
        # error the CLI prints, so the two cannot drift apart again — a
        # remediation nobody re-runs is how this one rotted.
        return _fail("database", f"context.db is unreadable: {exc}", damaged_store_recovery())
    marker = paths.truncation_marker_path()
    if marker.exists():
        # The store opens and is perfectly valid — it is simply not the one this
        # machine had. §0b teaches "an empty board with a green doctor means the
        # file was truncated"; without this, doctor is the green half of that
        # sentence and can never supply the other half, because by the time it
        # runs the schema is back and nothing distinguishes the two cases.
        when = _read_line(marker) or "an earlier run"
        return _warn(
            "database",
            f"context.db is readable ({count} user entries) — but it was found "
            f"TRUNCATED and rebuilt at {when}; the sessions, tasks and notes it "
            "held are gone",
            f"Nothing to repair — the history was lost before this. "
            f"Acknowledge it with: rm {marker}",
        )
    return _ok("database", f"context.db is readable ({count} user entries)")


def _read_line(path: Path) -> str:
    """First line of a small marker file, or empty when unreadable.

    Never raises: a diagnostic that crashes on its own breadcrumb is worse than
    one that says a little less.
    """
    try:
        return path.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return ""


def _check_repomix() -> DoctorCheck:
    if shutil.which("repomix"):
        return _ok("repomix", "repomix found — codebase snapshots enabled")
    if shutil.which("npx"):
        return _ok("repomix", "repomix available on demand via npx")
    return _warn(
        "repomix",
        "repomix not found — codebase snapshots are disabled",
        "Install Node.js, then: npm install -g repomix",
    )


def _check_tiktoken() -> DoctorCheck:
    if _has_module("tiktoken"):
        return _ok("tiktoken", "exact snapshot token counts enabled")
    return _warn(
        "tiktoken",
        "tiktoken not installed — snapshot token counts are estimated",
        "Install it: pip install tiktoken (or: pipx inject aisquare tiktoken)",
    )


_STALE_HOOKS = (
    "hooks are missing or outdated (older installs lack the Stop/Notification/SessionEnd events)"
)
_RECONNECT = "(Re)connect it: aisquare agents connect claude-code"


_CLAUDE_VERSION_DIR = re.compile(r"^\d+\.\d+\.\d+\S*$")


def claude_code_version(binary: str | None = None) -> str | None:
    """The installed Claude Code version, read from the install layout on disk.

    NO PROCESS IS STARTED: ``claude --version`` from a diagnostic would be a new
    spawn seam, and the answer is already on the filesystem for both layouts
    the installer produces. The native installer links ``claude`` to
    ``…/share/claude/versions/<version>`` (the target's name IS the version);
    an npm install links it to ``…/node_modules/@anthropic-ai/claude-code/cli.js``
    with a ``package.json`` beside it. Anything else is ``None`` — an honest
    blank in the detail beats a guess, and the check is unchanged without it.

    ``binary`` is the resolved executable for tests; the default is PATH.
    """
    found = binary if binary is not None else shutil.which("claude")
    if found is None:
        return None
    try:
        real = Path(found).resolve()
        if _CLAUDE_VERSION_DIR.match(real.name):
            return real.name
        package = real.parent / "package.json"
        if package.is_file():
            version = json.loads(package.read_text(encoding="utf-8")).get("version")
            if isinstance(version, str) and version:
                return version
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _check_claude_code() -> DoctorCheck:
    info = agent_core.detect("claude-code")
    if info is None or not info.detected:
        return _ok("claude-code", "Claude Code not detected on this machine")
    version = claude_code_version()
    product = f"Claude Code {version}" if version else "Claude Code"
    # Checked: recorded sites UNION the ambient dir. Parallel installs
    # (CLAUDE_CONFIG_DIR=~/.claude2) each own a settings.json, so registry
    # health alone hid unhooked siblings — and site health alone says nothing
    # about the AMBIENT dir, the one a `claude` from this shell actually
    # starts from, when it was never registered.
    health = {site.config_dir: site.hooks_installed for site in info.sites}
    ambient = agent_core.ambient_hook_dir("claude-code")
    if ambient is not None and ambient not in health:
        health[ambient] = agent_core.hooks_installed("claude-code")
    if not health:
        return _warn("claude-code", f"{product} {_STALE_HOOKS}", _RECONNECT)
    broken = [path for path, hooked in health.items() if not hooked]
    if not broken:
        where = f" in {len(health)} config dirs" if len(health) > 1 else ""
        return _ok("claude-code", f"{product} connected{where} (all lifecycle hooks installed)")
    listed = ", ".join(str(path) for path in broken)
    return _warn(
        "claude-code",
        f"{product} {_STALE_HOOKS} in: {listed}",
        "; ".join(f"aisquare agents connect claude-code --config-dir {p}" for p in broken),
    )


# --- system tools the fleet needs (docs/plans/fleet-tui.md §5 "Doctor", §8.2) ---------

_OS_RELEASE = Path("/etc/os-release")
_APT_FAMILY = frozenset({"debian", "ubuntu", "linuxmint", "pop", "raspbian", "kali", "elementary"})
_DNF_FAMILY = frozenset({"fedora", "rhel", "centos", "rocky", "almalinux", "amzn", "nobara", "ol"})


def install_hint(
    package: str, *, platform: str = sys.platform, os_release: Path = _OS_RELEASE
) -> str:
    """The install command for THIS machine's package manager — or all three when unsure.

    Read from ``/etc/os-release`` (``ID`` and ``ID_LIKE``), which is a file, not
    a process. A distribution this table does not know gets every hint rather
    than a wrong one; Windows gets the plan's answer (§3.9): the fleet runs in
    WSL2. Never raises — an unreadable os-release is "unsure", not a crash.
    """
    if platform == "darwin":
        return f"brew install {package}"
    if platform == "win32":
        return f"on Windows the fleet runs inside WSL2 — there: apt install {package}"
    families: set[str] = set()
    try:
        for line in os_release.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in {"ID", "ID_LIKE"}:
                families.update(value.strip().strip('"').lower().split())
    except OSError:
        pass
    if families & _APT_FAMILY:
        return f"apt install {package}"
    if families & _DNF_FAMILY:
        return f"dnf install {package}"
    return f"apt install {package} / dnf install {package} / brew install {package}"


def _check_tmux(server: tmux_core.TmuxServer | None = None) -> DoctorCheck:
    """tmux: present and new enough to be the fleet's substrate (§8.2).

    Absent or too old is a WARNING, never a failure: the fleet is the one
    feature that needs it, and a machine that runs every other command is not
    unhealthy. ``version()`` reads ``tmux -V`` through the one tmux seam and
    never touches the home, so this runs on an uninitialised machine too.
    ``server`` is for tests; the default is the real binary on PATH.

    This line RECOMMENDS NOTHING above ``tmux_core.MIN_VERSION``, and that is a
    measurement rather than an omission. It used to advise 3.4, on the claim
    that ``S-Enter`` "reaches an agent pane from 3.4" — false in both halves:
    ``send-keys S-Enter`` puts the seven characters ``S-Enter`` into a raw pane
    on 3.4 exactly as it does on 3.2 (on 3.5a it arrives as a bare CR, which an
    agent cannot tell from Enter), and the chord reaches an agent on every one
    of them because ``aisquare.core.keys`` writes ``ESC [ 13 ; 2 u`` itself and
    :meth:`~aisquare.core.tmux.TmuxServer.send_literal` delivers those bytes
    unchanged. These answered the same on all five: the bundled conf applies and
    ``source-file`` re-reads it clean, ``new-session -e``, per-window
    ``window-size manual`` plus ``resize-window``, ``capture-pane -e``,
    ``load-buffer -`` + ``paste-buffer -p`` bracketed paste, and the two answers
    :func:`_check_fleet` reads — a dead pane's ``pane_dead``, and a vanished
    target's exit 0 with every field empty. Measured 2026-08-30 on builds of
    tmux 3.2, 3.2a, 3.3a, 3.4 and 3.5a, under the fleet's own configuration.
    Individual KEY encodings do differ across those builds
    (``aisquare.core.keys``); none of it is anything this check could report.

    One such difference at the floor: tmux 3.2 has no ``C-/`` in its key table
    and types those three characters into the pane, which 3.2a fixed
    (``aisquare.core.keys.CTRL_US_ALIASES``) — and ``tmux -V`` for 3.2 and 3.2a
    both parse to ``(3, 2)``, so no version test on this side could tell the two
    users apart.
    """
    name = "tmux"
    try:
        server = server or tmux_core.TmuxServer()
        if not server.available():
            return _warn(
                name,
                "tmux not found — the fleet is unavailable; everything else works",
                f"Install it: {install_hint('tmux')}",
            )
        version = server.version()
        minimum = f"{tmux_core.MIN_VERSION[0]}.{tmux_core.MIN_VERSION[1]}"
        if version is None:
            return _ok(
                name,
                f"tmux at {server.binary()} — version not readable, so untested against "
                f"the {minimum} minimum; fleet available",
            )
        found = f"{version[0]}.{version[1]}"
        if version < tmux_core.MIN_VERSION:
            return _warn(
                name,
                f"tmux {found} is too old — the fleet needs {minimum} or newer; "
                "everything else works",
                f"Upgrade it: {install_hint('tmux')}",
            )
        return _ok(name, f"tmux {found} — fleet available")
    except Exception as exc:  # diagnostics must never crash
        # Failing open costs this line its verdict, not the operator anything
        # else: the fleet re-checks tmux on its first spawn and says so then.
        return _ok(name, f"not evaluated ({exc}) — the fleet checks again on its first spawn")


def _gh_config_dir() -> Path:
    """Where ``gh`` keeps ``hosts.yml`` — its own precedence, mirrored so we read the right one."""
    explicit = os.environ.get("GH_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "gh"


def _gh_login_note() -> str:
    """Empty when a login is visible; otherwise the one command that supplies it.

    ``gh`` records logins in ``hosts.yml`` and also honours ``GH_TOKEN`` /
    ``GITHUB_TOKEN``; this reads the file and the env, never ``gh auth status``
    (a process, and one that goes to the network). Unreadable is treated as
    logged in: a wrong "log in" nag is worse than a missing one, and the PR
    step itself says clearly when it is refused.
    """
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return ""
    try:
        hosts = _gh_config_dir() / "hosts.yml"
        if hosts.is_file() and hosts.read_text(encoding="utf-8").strip():
            return ""
    except OSError:
        return ""
    return " (no login found: gh auth login)"


def _check_gh() -> DoctorCheck:
    """GitHub CLI: the fleet's coder and reviewer open and review PRs through it (§3.5)."""
    found = shutil.which("gh")
    if found is None:
        return _warn(
            "gh",
            "gh not found — the fleet's PR flow for coder/reviewer needs it; everything else works",
            f"Install GitHub CLI: {install_hint('gh')}, then: gh auth login",
        )
    return _ok("gh", f"gh at {found} — PR flow available{_gh_login_note()}")


def _check_snapshot(cwd: Path | None = None) -> DoctorCheck:
    """The active project's codebase snapshot; ``cwd`` picks the project (default: process cwd)."""
    absent = _uncreated_home("snapshot")
    if absent is not None:
        return absent
    try:
        with store_session() as store:
            project = active_project(store, cwd)
        snap = snapshot_core.load(project.id)
    except Exception as exc:  # diagnostics must never crash
        return _warn(
            "snapshot", f"could not check the snapshot: {exc}", "Try: aisquare project onboard"
        )
    if snap is not None and snap.status == "ready":
        return _ok(
            "snapshot", f"snapshot ready ({snap.file_count} files, {snap.token_count} tokens)"
        )
    return _warn(
        "snapshot",
        "no codebase snapshot for the active project",
        "Pack one: aisquare project onboard",
    )


def _check_brain(cwd: Path | None = None) -> DoctorCheck:
    """The team's long-term memory: gbrain presence, brain state, distill lag.

    ``cwd`` picks the project the way the team hooks do (``team_project``);
    ``None`` is the process cwd.
    """
    absent = _uncreated_home("brain")
    if absent is not None:
        return absent
    if not brain_core.brain_enabled():
        return _ok("brain", "brain layer disabled (AISQUARE_BRAIN=0)")
    version = brain_core.gbrain_version()
    if version is None:
        return _warn(
            "brain",
            "gbrain not found — team decisions/results are not distilled",
            "Optional: install the AISquare gbrain CLI to enable long-term memory "
            "(not the unrelated 'gbrain' on public npm)",
        )
    try:
        project = orchestrator.team_project(cwd)
        with store_session() as store:
            if not store.team_active(project.id):
                return _ok("brain", f"gbrain {version} ready (orchestrator not active here)")
            lag = distill_service.pending(store, project.id)
    except Exception as exc:  # diagnostics must never crash
        return _warn("brain", f"could not check the brain: {exc}", "Try: aisquare team distill")
    if not brain_core.brain_ready(project.id):
        return _warn(
            "brain",
            f"gbrain {version} found but this project's brain is not initialised",
            "It initialises on the first distill: aisquare team distill",
        )
    # The embedding schema is fixed at create time, and the knob lives in
    # per-shell env (never persisted), so BOTH mismatch directions are real and
    # invisible without a signal. Surface either rather than reporting healthy.
    knob = brain_core.embeddings_enabled()
    embeds = brain_core.brain_embeds(project.id)
    if knob and not embeds:
        return _warn(
            "brain",
            f"gbrain {version}, brain ready — but it was created WITHOUT embeddings, "
            "so AISQUARE_BRAIN_EMBED has no effect here (recall stays keyword-only)",
            "Rebuild embedding-capable: remove ~/.aisquare/projects/<id>/brain, then "
            "AISQUARE_BRAIN_EMBED=1 aisquare team distill --all",
        )
    if embeds and not knob:
        return _warn(
            "brain",
            f"gbrain {version}, brain has embeddings but AISQUARE_BRAIN_EMBED is off — "
            "recall stays keyword-only and new pages are distilled without vectors",
            "Export AISQUARE_BRAIN_EMBED=1 (and OPENAI_API_KEY) in the shells that run "
            "aisquare, or add them to your shell profile",
        )
    embed = " (embeddings on)" if embeds else ""
    if lag > 0:
        return _ok(
            "brain", f"gbrain {version}, brain ready{embed} ({lag} pipe events awaiting distill)"
        )
    return _ok("brain", f"gbrain {version}, brain ready and fully distilled{embed}")


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def last_injection() -> InjectionRecord | None:
    """Return the most recent injection record (backs ``why``), or None."""
    return load_last()


def show_log(limit: int = 20) -> list[PromptRecord]:
    """Recent captured user prompts for the active project (newest first)."""
    with store_session() as store:
        project = active_project(store)
        return store.recent_prompts(project.id, limit=limit)


def open_home() -> None:
    """Open the aisquare home directory or web dashboard."""
    stub("open")


def _check_harness(cwd: Path | None = None) -> DoctorCheck:
    """Role→model harness health: env interference, mismatched live sessions, cache.

    Read-only and offline by design (doctor makes no network calls): ladder
    availability comes from the probe cache; live verification is
    ``aisquare team spawn <role>``. Warns, never fails — a stale cache or an
    off-ladder session is advice, not breakage. ``cwd`` picks the project
    (``team_project``); ``None`` is the process cwd.
    """
    absent = _uncreated_home("harness")
    if absent is not None:
        return absent
    name = "agent harness"
    try:
        # Not-applicable = ok: a repo that never opted into the orchestrator must
        # see nothing from the harness, not even an env-hygiene warning.
        if not orchestrator.team_enabled():
            return _ok(name, "orchestrator disabled")
        with store_session() as store:
            project = orchestrator.team_project(cwd)
            if not store.team_active(project.id):
                return _ok(name, "not activated for this project")
            live = [s for s in store.team_sessions(project.id) if s.ended_at is None]
        interference = harness.interfering_env()
        mismatches: list[str] = []
        for session in live:
            complaint = harness.model_mismatch(session.role, session.model)
            if complaint:
                mismatches.append(f"{session.id[:8]} ({session.role}): {complaint}")
        problems: list[str] = []
        if interference:
            problems.append(f"env overrides model selection: {', '.join(interference)}")
        if mismatches:
            problems.append("off-ladder sessions: " + "; ".join(mismatches))
        if problems:
            return _warn(
                name,
                " — ".join(problems),
                "unset the interfering env vars and relaunch off-ladder roles with "
                "`aisquare team spawn <role>`",
            )
        fable = harness.cached_probe("fable")
        if fable is not None and not fable.available:
            return _warn(
                name,
                f"fable probed unavailable ({fable.reason}) — top-tier roles fall back to opus",
                "expected on non-enterprise accounts; re-check with "
                "`aisquare team spawn planner --refresh`",
            )
        detail = "role ladders clean"
        if fable is not None and fable.resolved_id:
            detail = f"role ladders clean; fable available ({fable.resolved_id})"
        return _ok(name, detail)
    except Exception:  # diagnostics must never crash
        return _ok(name, "not evaluated")


def _fleet_conf() -> Path:
    """The bundled tmux conf's PATH, without writing it.

    ``TmuxServer.argv`` resolves the conf through ``conf_path``, which calls
    ``ensure_home`` and rewrites the file when it drifts — right for the fleet,
    wrong for a diagnostic (``_uncreated_home`` says why). Handing the server
    the path up front skips both. tmux only reads ``-f`` when it STARTS a
    server, and nothing here starts one, so a missing file costs nothing.
    """
    return paths.aisquare_home() / tmux_core.CONF_NAME


def _fleet_server(socket: str) -> tmux_core.TmuxServer:
    return tmux_core.TmuxServer(socket, conf=_fleet_conf())


def _fleet_labels(agents: list[FleetAgent], names: dict[str, str], *, limit: int = 6) -> str:
    """``label (project), …`` — capped, so one runaway fleet cannot flood a doctor line."""
    shown = [f"{a.label} ({names.get(a.project_id, a.project_id[:12])})" for a in agents[:limit]]
    if len(agents) > limit:
        shown.append(f"+{len(agents) - limit} more")
    return ", ".join(shown)


def _check_fleet(
    server_for: Callable[[str], tmux_core.TmuxServer] | None = None,
) -> DoctorCheck:
    """``fleet_agent`` rows against tmux: every row recorded live must still have its pane.

    A row outlives its process in three ways — the pane was killed outside the
    fleet, the private server was stopped (a reboot ends every agent), or the
    agent exited and ``remain-on-exit`` kept the pane — and each one leaves a
    label taken and a project looking staffed. ``aisquare fleet reap`` is the
    reconciliation; this check is where an operator learns it is due. Read-only:
    the store is opened, tmux is asked, nothing is written or started.

    Machine-wide on purpose: a stale row in ANY project is this machine's, and
    ``fleet reap`` with no project reaps them all. Each row is checked on the
    socket IT was spawned on — the socket is a default the user may change
    (§3.10), and a row must not read as lost because the config moved after it.
    ``server_for`` builds the server for a socket; tests hand in fakes.
    """
    absent = _uncreated_home("fleet")
    if absent is not None:
        return absent
    name = "fleet"
    server_for = server_for or _fleet_server
    try:
        socket = fleet_service.settings().tmux_socket
        servers: dict[str, tmux_core.TmuxServer] = {socket: server_for(socket)}
        if not servers[socket].available():
            # Failing open costs this line: nothing here can be stale in a way
            # that matters when nothing can run, and the tmux check has the verdict.
            return _ok(name, "not evaluated — tmux is not installed (see the tmux check)")
        with store_session() as store:
            projects = store.list_projects()
            names = {p.id: p.codename or p.root.name or p.id for p in projects}
            live = [a for p in projects for a in store.fleet_agents(p.id, live_only=True)]
        gone: list[FleetAgent] = []
        exited: list[FleetAgent] = []
        for agent in live:
            server = servers.setdefault(agent.tmux_socket, server_for(agent.tmux_socket))
            try:
                facts = server.pane_facts(agent.pane_id)
            except tmux_core.TmuxError:
                facts = None
            # tmux 3.7c answers a vanished target with exit 0 and every field
            # empty, so "gone" is "no facts OR facts about no pane", not only None.
            if facts is None or facts.pane_id != agent.pane_id:
                gone.append(agent)
            elif facts.dead:
                exited.append(agent)
        problems: list[str] = []
        by_socket: dict[str, list[FleetAgent]] = {}
        for agent in gone:
            by_socket.setdefault(agent.tmux_socket, []).append(agent)
        for sock, agents in by_socket.items():
            listed = _fleet_labels(agents, names)
            if servers[sock].list_sessions():
                problems.append(f"{len(agents)} recorded live but the tmux pane is gone: {listed}")
            else:
                problems.append(
                    f"{len(agents)} recorded live but the private tmux server "
                    f"'{sock}' is not running: {listed}"
                )
        if exited:
            problems.append(
                f"{len(exited)} exited but still recorded live: {_fleet_labels(exited, names)}"
            )
        if problems:
            return _warn(
                name,
                "; ".join(problems),
                "Reconcile the rows with tmux (ended, lost, merged worktrees): aisquare fleet reap",
            )
        sessions = servers[socket].list_sessions()
        if live:
            return _ok(
                name,
                f"{len(live)} live agent(s) across {len(sessions)} session(s) on the private "
                f"tmux server '{socket}'",
            )
        if sessions:
            return _ok(
                name,
                f"private tmux server '{socket}' running with {len(sessions)} session(s); "
                "no agents recorded live",
            )
        return _ok(name, f"no fleet agents; the private tmux server '{socket}' is not running")
    except Exception as exc:  # diagnostics must never crash
        # A store that will not open is the database check's verdict. Failing
        # open here costs the stale-row report — a label can stay taken and a
        # project can look staffed until the store opens — and the line says so.
        return _ok(
            name,
            f"not evaluated ({exc}) — stale fleet rows, if any, go unreported until "
            "the store opens",
        )
