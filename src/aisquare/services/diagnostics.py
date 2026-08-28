"""Health checks and introspection."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit

from aisquare.core import agents as agent_core
from aisquare.core import brain as brain_core
from aisquare.core import harness, orchestrator, paths
from aisquare.core import snapshot as snapshot_core
from aisquare.core.config import load_config
from aisquare.core.injection import load_last
from aisquare.core.store import damaged_store_recovery, store_session
from aisquare.core.stubs import stub
from aisquare.core.workspace import active_project
from aisquare.models import (
    CheckStatus,
    DoctorCheck,
    InjectionRecord,
    PromptRecord,
    ShippingStatus,
    StatusReport,
)
from aisquare.services import ci_client, ci_descriptor, explainability_ops
from aisquare.services import distill as distill_service
from aisquare.services import explainability as explainability_service


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


def doctor(*, live: bool = False, target: str | None = None) -> list[DoctorCheck]:
    """Run health checks over the install, dependencies and integration.

    ``live`` opts into the checks that leave this machine (today: the
    explainability gateway round-trip). Everything else stays offline, so a
    plain ``aisquare doctor`` still answers on a train.
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
        _check_snapshot(),
        _check_brain(),
        _check_harness(),
        *_experiment_checks(),
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


def _check_claude_code() -> DoctorCheck:
    info = agent_core.detect("claude-code")
    if info is None or not info.detected:
        return _ok("claude-code", "Claude Code not detected on this machine")
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
        return _warn("claude-code", f"Claude Code {_STALE_HOOKS}", _RECONNECT)
    broken = [path for path, hooked in health.items() if not hooked]
    if not broken:
        where = f" in {len(health)} config dirs" if len(health) > 1 else ""
        return _ok("claude-code", f"Claude Code connected{where} (all lifecycle hooks installed)")
    listed = ", ".join(str(path) for path in broken)
    return _warn(
        "claude-code",
        f"Claude Code {_STALE_HOOKS} in: {listed}",
        "; ".join(f"aisquare agents connect claude-code --config-dir {p}" for p in broken),
    )


def _check_snapshot() -> DoctorCheck:
    absent = _uncreated_home("snapshot")
    if absent is not None:
        return absent
    try:
        with store_session() as store:
            project = active_project(store)
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


def _check_brain() -> DoctorCheck:
    """The team's long-term memory: gbrain presence, brain state, distill lag."""
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
        project = orchestrator.team_project()
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


def _experiment_checks() -> list[DoctorCheck]:
    """The CI test bed's state, one line per question a developer would ask.

    Off is reported as ``ok`` rather than as a warning: off is the intended
    state for everyone who has not been asked to run the experiment, and a
    permanent warning trains people to ignore the one line that matters.

    Switched on, the questions are asked in the order the hooks would hit
    them: is the URL usable, is there a token, is there a run; can the server
    be reached at all (``GET /ready``, public); and does the descriptor come
    back — which is the real question, because it is where a bad token, an
    unknown run, an expired run or a contract skew each show up with their own
    answer. The descriptor is fetched without caching it: a diagnostic must not
    create state. Every probe is bounded by the transport's own deadline.
    """
    name = "ci test bed"
    if not ci_client.enabled():
        return [_ok(name, "off — no requests, no added latency (AISQUARE_CI=1 enables)")]
    raw = ci_client.raw_endpoint()
    if not raw:
        return [
            _warn(
                name,
                "enabled but no endpoint configured — every prompt records not_configured",
                "Point it at the server: export AISQUARE_CI_URL=https://…",
            )
        ]
    base = ci_client.endpoint()
    if not base:
        return [
            _warn(
                name,
                f"enabled, but {_display_url(raw)} is not a usable URL — it needs an "
                "http(s):// scheme; every prompt records not_configured",
                "Give it a scheme: export AISQUARE_CI_URL=https://…",
            )
        ]
    shown = _display_url(base)
    key = ci_client.api_key()
    raw_run = ci_client.raw_run_id()
    run = ci_client.run_id()
    checks: list[DoctorCheck] = []
    if not key:
        checks.append(
            _warn(
                name,
                f"enabled for {shown}, but no bearer token — the server will reject every request",
                "Set the experiment token: export AISQUARE_CI_KEY=…",
            )
        )
    elif not raw_run:
        checks.append(
            _warn(
                name,
                f"enabled for {shown}, but no run id — every prompt records no_run",
                "Export the run the controller published: export AISQUARE_CI_RUN=run_…",
            )
        )
    elif not run:
        checks.append(
            _warn(
                name,
                f"enabled for {shown}, but {raw_run!r} is not a run id (run_…) — "
                "every prompt records no_run",
                "Export the run the controller published: export AISQUARE_CI_RUN=run_…",
            )
        )
    else:
        checks.append(_ok(name, f"enabled for {shown}, run {run}"))
    checks.append(_check_ci_endpoint(base, shown))
    if key and run:
        checks.append(_check_ci_descriptor(base, key, run))
    return checks


_CI_PROBE_MS = 3_000
"""Doctor must stay fast; an unreachable endpoint is the common case here, and
the transport's wall-clock deadline is what bounds each probe."""


def _check_ci_endpoint(base: str, shown: str) -> DoctorCheck:
    """``GET /ready`` — public, cheap, and proof of a live server rather than a
    listener. It follows the same proxies the hook does, because it is the
    same transport."""
    result = ci_client.exchange(
        f"{base}/ready", method="GET", deadline_ms=_CI_PROBE_MS, max_body=4096
    )
    if result.reason is None and result.status == 200:
        return _ok("ci endpoint", f"{shown}/ready answered 200 in {result.elapsed_ms} ms")
    why = result.detail if result.reason is not None else f"http {result.status}"
    return _warn(
        "ci endpoint",
        f"{shown}/ready did not answer ({why}) — prompts still work; every turn records "
        "descriptor_unavailable or transport_error",
        "Check the server is up, or turn the test bed off: export AISQUARE_CI=0",
    )


def _check_ci_descriptor(base: str, key: str, run: str) -> DoctorCheck:
    """The question that matters: will the hooks be told how to deliver?"""
    result = ci_descriptor.fetch(run, base=base, key=key, cache=False, deadline_ms=_CI_PROBE_MS)
    descriptor = result.descriptor
    if descriptor is None:
        detail = result.detail
        if "token" in detail:
            fix = "Check AISQUARE_CI_KEY is the experiment token for this server"
        elif "not found" in detail:
            fix = "Check AISQUARE_CI_RUN names a run this server has published"
        elif "contract_version" in detail:
            fix = "Upgrade aisquare-cli, or ask for a run published for this contract"
        elif "expired" in detail:
            fix = "Ask the experiment controller for a fresh run"
        else:
            fix = "Check the server, or turn the test bed off: export AISQUARE_CI=0"
        return _warn(
            "ci descriptor", f"run {run}: {detail} — every turn records descriptor_unavailable", fix
        )
    modes = []
    push = descriptor.hook_push
    if push is not None:
        modes.append(f"hook_push on {', '.join(push.triggers)}")
    if descriptor.mcp_pull is not None:
        modes.append(f"mcp_pull ({descriptor.mcp_pull.tool})")
    if not modes:
        modes.append("direct_api only — the hooks will not call")
    return _ok(
        "ci descriptor",
        f"run {run}: {'; '.join(modes)}; ceiling {descriptor.client_safety_ms} ms; "
        f"expires {descriptor.expires_at}",
    )


def _display_url(url: str) -> str:
    """A URL as ``doctor`` may print it: scheme and host, never ``user:secret@``.

    ``doctor`` output is the most pasteable artefact there is, and a credential
    in the URL would leak by being ordinary.
    """
    try:
        parts = urlsplit(url if "://" in url else f"//{url}", scheme="")
    except ValueError:
        return re.sub(r"[^@/]*@", "", url)
    host = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://{host}" if parts.scheme else host


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


def _check_harness() -> DoctorCheck:
    """Role→model harness health: env interference, mismatched live sessions, cache.

    Read-only and offline by design (doctor makes no network calls): ladder
    availability comes from the probe cache; live verification is
    ``aisquare team spawn <role>``. Warns, never fails — a stale cache or an
    off-ladder session is advice, not breakage.
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
            project = orchestrator.team_project(None)
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
