"""Local usage stats: every Claude Code session on this machine, recorded and summed on read.

**What is recorded.** Tokens are read off Claude Code's transcripts, exactly and once per API
request (``core.usage``). Two paths write them to the store's ``usage_session`` and
``usage_turn`` tables (``services.metrics`` says how the first works):

- the **Stop hook** of a session aisquare launched, on the hot path, as before; and
- the **sweep** (:func:`sweep`), which reads the transcripts of every Claude Code session in
  every repository on every account directory — ``~/.claude`` and each registered slot's —
  whether or not aisquare launched it. It runs off the hot path: when the dashboard opens or the
  TUI ticks, at most every :data:`SWEEP_EVERY` (:func:`maybe_sweep`), and as ``aisquare metrics
  backfill``. Claude Code deletes a transcript after ``cleanupPeriodDays`` (30 by default), so a
  first sweep records the history that still exists and later ones keep it.

The two cannot count a session twice: they share one scan state per session (the byte offset
reached in each transcript file, the message ids counted), kept on its ``usage_session`` row
and written with a compare-and-set on the state each started from (``store.record_usage``). A
session the hook counted to byte N is read by the sweep from byte N; a session read by both
at once is counted by the one that wins the write, and the other reads the rest next time.

**What is read.** :func:`stats` sums the turn rows of a range (``today``, ``7d``, ``30d``,
``lifetime``) into a :class:`~aisquare.models.UsageStats`, and :func:`daily_rollups` cuts the
same rows into one :class:`~aisquare.models.DailyRollup` per local day, which is what the
opt-in sharing path ships. Both are aggregations on read of the same loader, so the figures
are the same wherever they are shown, and the totals are the ones ``aisquare metrics tokens``
reports for the same sessions. Days are LOCAL days; a turn belongs to the day it ended on
(a sweep cuts a turn that crosses midnight in two), and none is dated after the moment it was
read. A repository is its normalised git remote, else its root, so the clones of one remote are
one (:func:`_identity`).

**Known limitation.** Requests are deduplicated per session, by message id: the same messages in
two session files (a transcript copied or forked) would be counted twice, as the Stop hook always
did. None of the transcripts this was developed on share a message id; a machine-wide index of
ids would cost a lookup per request for a case not seen.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as day_time
from pathlib import Path
from typing import Any

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import feature_use, locking, paths, pricing, repo_profile
from aisquare.core import usage as usage_core
from aisquare.core.ids import new_usage_id
from aisquare.core.store import USAGE_PURGED_PREFIX, ContextStore, store_session
from aisquare.core.workspace import current_project
from aisquare.models import (
    USAGE_RANGES,
    ClaudeAccount,
    DailyRollup,
    DayAccounts,
    DayRepo,
    DayTokens,
    FleetDay,
    RepoProfile,
    RepoTraits,
    SessionUsage,
    TokenCounts,
    TurnUsage,
    UsageAccountRow,
    UsageAccounts,
    UsageDay,
    UsageFeatureRow,
    UsageModelRow,
    UsageRange,
    UsageRepoRow,
    UsageRepos,
    UsageStats,
    UsageTokens,
    UsageTurnRow,
)
from aisquare.services import metrics as metrics_service

SWEEP_EVERY = timedelta(minutes=10)
"""How often :func:`maybe_sweep` reads the transcripts again."""
PROFILE_MAX_AGE = timedelta(days=1)
"""How old a repository's cached profile may be (:func:`refresh_profiles`)."""
ACCOUNT_TYPES_MAX_AGE = timedelta(hours=6)

TOP_REPOS = 10
TOP_DAYS = 5
TOP_FEATURES = 10
ROLLUP_REPOS = 25
"""Repositories a day's rollup keeps (the telemetry contract's cap)."""
_PROFILE_BUDGET_S = 8.0
"""How long one sweep spends profiling repositories (each asks git a few times); the rest wait for
the next."""

_SWEPT_KEY = "usage.sweep.at"
_TYPES_KEY = "usage.account_types"
_HEAD_BYTES = 262_144
"""How much of a transcript's head is read to find the directory the session ran in."""
_WORKTREES = ".aisquare-worktrees"

UNKNOWN_TYPE = "unknown"

FLEET_ROLES = frozenset(
    {
        "manager", "planner", "coder", "tester", "runner", "reviewer", "validator",
        "ui-tester", "agent", "captain",
    }
)  # fmt: skip
"""The roles a rollup names: the built-in ones. A role an operator invented may be any text, so
it is counted as ``other``."""

TEMP_REPO = "tmp"
"""The one repository id every throwaway directory is counted under (:func:`repo_id_of`)."""
TEMP_NAME = "temporary directories"


# --- local time ----------------------------------------------------------------------------


def _local(moment: datetime, tz: tzinfo | None) -> datetime:
    """``moment`` on the local clock: ``tz``'s, or the system's with the offset it had THEN."""
    return moment.astimezone(tz)


def _day_start(day: date, tz: tzinfo | None) -> datetime:
    """The first instant of local ``day`` (a naive datetime reads as the system's zone)."""
    naive = datetime.combine(day, day_time.min)
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def _offset_minutes(day: date, tz: tzinfo | None) -> int:
    offset = _day_start(day, tz).utcoffset()
    return int(offset.total_seconds() // 60) if offset is not None else 0


def _today(now: datetime | None, tz: tzinfo | None) -> date:
    return _local(now or datetime.now(UTC), tz).date()


def window(range_: UsageRange, today: date) -> tuple[date | None, date]:
    """The first and last local day ``range_`` covers; no first day for ``lifetime``."""
    if range_ == "today":
        return today, today
    if range_ == "7d":
        return today - timedelta(days=6), today
    if range_ == "30d":
        return today - timedelta(days=29), today
    return None, today


# --- the transcripts -------------------------------------------------------------------------


@dataclass(frozen=True)
class Transcript:
    """One session's transcript file, and the account directory it was found under."""

    session_id: str
    path: Path
    slot: int | None


def transcript_roots() -> list[tuple[int | None, Path]]:
    """Every directory Claude Code keeps transcripts under, with the slot it belongs to: the
    plain ``claude``'s (slot 1), each registered slot's, and ``~/.claude`` when the plain
    ``claude`` is redirected elsewhere (``CLAUDE_CONFIG_DIR``)."""
    roots: list[tuple[int | None, Path]] = [
        (account.slot, account.config_dir) for account in accounts_core.list_accounts()
    ]
    home = accounts_core.home_config_dir()
    if all(not _same_path(home, directory) for _, directory in roots):
        roots.append((accounts_core.DEFAULT_SLOT, home))
    return roots


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except (OSError, RuntimeError):
        return a == b


def discover(roots: Iterable[tuple[int | None, Path]]) -> list[Transcript]:
    """The session transcripts under ``roots`` (``<dir>/projects/<folder>/<session>.jsonl``),
    newest first, each session once: a directory reached twice (a shared ``projects`` link, the
    default slot named by both the variable and ``~/.claude``) is read once."""
    found: list[tuple[float, Transcript]] = []
    seen_files: set[Path] = set()
    seen_sessions: set[str] = set()
    for slot, directory in roots:
        projects = directory / "projects"
        try:
            folders = [entry.path for entry in os.scandir(projects) if entry.is_dir()]
        except OSError:
            continue
        for folder in sorted(folders):
            try:
                entries = [e for e in os.scandir(folder) if e.name.endswith(".jsonl")]
            except OSError:
                continue
            for entry in entries:
                try:
                    real = Path(entry.path).resolve()
                    mtime = entry.stat().st_mtime
                except (OSError, RuntimeError):
                    continue
                session_id = entry.name.removesuffix(".jsonl")
                if real in seen_files or session_id in seen_sessions:
                    continue
                seen_files.add(real)
                seen_sessions.add(session_id)
                found.append((mtime, Transcript(session_id, Path(entry.path), slot)))
    found.sort(key=lambda item: (-item[0], item[1].session_id))
    return [item[1] for item in found]


def account_slot(transcript: Path) -> int | None:
    """The slot whose config directory holds ``transcript``
    (``<dir>/projects/<folder>/<session>.jsonl``); ``None`` for a directory none names."""
    try:
        directory = transcript.parents[2]
    except IndexError:
        return None
    if transcript.parents[1].name != "projects":
        return None
    slot = accounts_core.managed_slot(directory)
    if slot is not None:
        return slot
    for candidate in (accounts_core.default_config_dir(), accounts_core.home_config_dir()):
        if _same_path(directory, candidate):
            return accounts_core.DEFAULT_SLOT
    return None


def session_cwd(transcript: Path) -> Path | None:
    """The directory the session ran in: the first ``cwd`` in the head of its transcript."""
    try:
        with transcript.open("rb") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return None
    for line in head.split(b"\n"):
        if b'"cwd"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        cwd = entry.get("cwd") if isinstance(entry, dict) else None
        if isinstance(cwd, str) and cwd:
            return Path(cwd)
    return None


def repo_of(cwd: Path | None) -> tuple[str, Path] | None:
    """The repository ``cwd`` is in — its id and root — as the rest of aisquare resolves
    projects (worktrees to their principal checkout). A fleet worktree that has been removed is
    still its repository's: the folder was ``<repo>/.aisquare-worktrees/<name>``."""
    if cwd is None:
        return None
    start = cwd
    if not start.is_dir() and _WORKTREES in start.parts:
        start = Path(*start.parts[: start.parts.index(_WORKTREES)])
    try:
        project = current_project(start)
    except (OSError, RuntimeError):
        return None
    return project.id, project.root


def _ephemeral_roots() -> tuple[Path, ...]:
    """Directories whose repositories are throwaways: the system's temporary folders and the
    scratch space aisquare and Claude Code keep under the home (the cache, benchmark runs)."""
    candidates = {
        Path(tempfile.gettempdir()),
        Path("/tmp"),
        Path("/var/tmp"),
        paths.aisquare_home() / "cache",
        paths.aisquare_home() / "bench",
    }
    roots: set[Path] = set()
    for candidate in candidates:
        roots.add(candidate)
        with contextlib.suppress(OSError, RuntimeError):
            roots.add(candidate.resolve())
    return tuple(roots)


def repo_id_of(repo_id: str, root: Path) -> str:
    """The id a repository is COUNTED under: its own, or :data:`TEMP_REPO` for one that lives in
    a temporary directory. A scratch repository an agent makes to try something is a repository
    for a minute: hundreds of them would be hundreds of "repositories" in the page and in the
    counts the sharing path sends, and none of them a project. Their tokens still count."""
    if any(root.is_relative_to(base) for base in _ephemeral_roots()):
        return TEMP_REPO
    return repo_id


# --- the account's subscription -----------------------------------------------------------


def _subscription_of(account: ClaudeAccount) -> str:
    """The subscription an account's login holds: ``pro``, ``max``, ``team``, ``enterprise`` …
    as Claude Code stores it; ``api`` for an account with an approved API key and no login;
    ``unknown`` otherwise. Reads the Keychain: never on a hook's path."""
    creds = accounts_core.credentials(account)
    if creds is not None and creds.subscription_type:
        return creds.subscription_type.strip().lower()
    data = accounts_core.claude_json(account)
    if data is not None:
        oauth = data.get("oauthAccount")
        if isinstance(oauth, dict):
            kind = oauth.get("organizationType")
            if isinstance(kind, str) and kind:
                return kind.removeprefix("claude_").strip().lower()
        elif data.get("customApiKeyResponses"):
            return "api"
    return UNKNOWN_TYPE


def account_types(store: ContextStore, *, refresh: bool = True) -> dict[int, str]:
    """Each account slot's subscription type, cached for :data:`ACCOUNT_TYPES_MAX_AGE`.

    ``refresh=False`` is the hook's call: the cache as it stands (empty before a sweep has made
    it), never a Keychain read.
    """
    cached: dict[int, str] = {}
    read_at: datetime | None = None
    raw = store.get_meta(_TYPES_KEY)
    if raw:
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(raw)
            cached = {int(slot): str(kind) for slot, kind in data["types"].items()}
            read_at = datetime.fromisoformat(data["at"])
    now = datetime.now(UTC)
    if not refresh or (read_at is not None and now - read_at < ACCOUNT_TYPES_MAX_AGE):
        return cached
    types: dict[int, str] = {}
    for account in accounts_core.list_accounts():
        try:
            types[account.slot] = _subscription_of(account)
        except Exception:  # an account whose login cannot be read is one of unknown type
            types[account.slot] = cached.get(account.slot, UNKNOWN_TYPE)
    with contextlib.suppress(Exception):
        store.set_meta(
            _TYPES_KEY,
            json.dumps({"at": now.isoformat(), "types": {str(s): k for s, k in types.items()}}),
        )
    return types


def hook_attribution(
    store: ContextStore,
    transcript: Path,
    cwd: Path | None,
    *,
    repo: tuple[str, Path] | None = None,
) -> tuple[int | None, str | None, str | None]:
    """``(account slot, its subscription type, repository id)`` for a session the Stop hook
    records the first time: cheap, nothing read from the Keychain (the sweep fills what is
    missing). ``repo`` is the repository's id and root when the caller has resolved them already
    (the hook has, from the same working directory); otherwise the directory is asked."""
    slot = account_slot(transcript)
    kind = account_types(store, refresh=False).get(slot) if slot is not None else None
    if repo is None:
        repo = repo_of(cwd if cwd is not None else session_cwd(transcript))
    return slot, kind, repo_id_of(*repo) if repo is not None else None


# --- the sweep -----------------------------------------------------------------------------


@dataclass
class SweepReport:
    """What one sweep did."""

    transcripts: int = 0
    """Session transcripts found."""
    sessions_new: int = 0
    sessions_updated: int = 0
    turns: int = 0
    """Turn rows written."""
    unchanged: int = 0
    failed: int = 0
    attributed: int = 0
    """Sessions whose account or repository was filled in."""
    profiles: int = 0
    features: int = 0
    """Feature uses folded into the store."""
    seconds: float = 0.0
    skipped: str | None = None
    """Why nothing was swept (another sweep is running, or it was swept recently)."""

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def maybe_sweep(
    *,
    store: ContextStore | None = None,
    now: datetime | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SweepReport:
    """:func:`sweep`, unless one ran in the last :data:`SWEEP_EVERY` (the report says so)."""
    when = now or datetime.now(UTC)

    def run(active: ContextStore) -> SweepReport:
        last = active.get_meta(_SWEPT_KEY)
        if last:
            with contextlib.suppress(ValueError):
                if when - datetime.fromisoformat(last) < SWEEP_EVERY:
                    return SweepReport(skipped="swept recently")
        return sweep(store=active, now=when, progress=progress)

    if store is not None:
        return run(store)
    with store_session() as owned:
        return run(owned)


def sweep(
    *,
    store: ContextStore | None = None,
    now: datetime | None = None,
    roots: list[tuple[int | None, Path]] | None = None,
    tz: tzinfo | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> SweepReport:
    """Read every Claude Code transcript on this machine that has grown since it was last read,
    and record what it added. Idempotent, incremental, and safe to run beside the hooks.

    One sweep at a time per machine (a lock file); a second asking meanwhile returns at once
    with ``skipped`` set. Never raises for a transcript it cannot read: that one is counted in
    ``failed`` and the rest go on.
    """
    began = time.perf_counter()
    try:
        paths.ensure_home()  # the lock file lives in it, and the store may not have made it yet
        with locking.holding(paths.usage_sweep_lock_path(), wait_s=0):
            if store is not None:
                report = _sweep(store, now, roots, tz, progress)
            else:
                with store_session() as owned:
                    report = _sweep(owned, now, roots, tz, progress)
    except TimeoutError:
        return SweepReport(skipped="another sweep is running")
    except OSError:
        return SweepReport(skipped="the sweep's lock could not be taken")
    report.seconds = round(time.perf_counter() - began, 3)
    return report


def _sweep(
    store: ContextStore,
    now: datetime | None,
    roots: list[tuple[int | None, Path]] | None,
    tz: tzinfo | None,
    progress: Callable[[int, int], None] | None,
) -> SweepReport:
    report = SweepReport()
    found = discover(roots if roots is not None else transcript_roots())
    report.transcripts = len(found)
    known = store.usage_transcripts()
    purged = purged_repos(store)
    repos: dict[Path, tuple[str, Path] | None] = {}
    seen_repos: dict[str, Path] = {}
    types = account_types(store)
    for index, transcript in enumerate(found):
        if progress is not None:
            progress(index, len(found))
        try:
            outcome = _sweep_one(store, transcript, known, repos, types, tz, now, purged)
        except Exception as exc:  # one unreadable transcript costs that one
            metrics_service.log_failure(f"usage sweep failed for {transcript.path}", exc)
            report.failed += 1
            continue
        report.turns += outcome.turns
        report.sessions_new += outcome.new
        report.sessions_updated += outcome.updated
        report.unchanged += outcome.unchanged
        if outcome.repo is not None and repo_id_of(*outcome.repo) != TEMP_REPO:
            seen_repos.setdefault(outcome.repo[0], outcome.repo[1])
    if progress is not None:
        progress(len(found), len(found))
    report.attributed = _attribute(store, types, repos)
    report.profiles = refresh_profiles(store, seen_repos, now=now)
    report.features = feature_use.fold(store)
    store.set_meta(_SWEPT_KEY, (now or datetime.now(UTC)).isoformat())
    return report


@dataclass
class _Outcome:
    turns: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    repo: tuple[str, Path] | None = None


def _transcript_stamp(value: str) -> datetime | None:
    """A transcript timestamp (``2026-10-25T04:12:33.123Z``) as an aware datetime."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _sweep_one(
    store: ContextStore,
    found: Transcript,
    known: dict[str, str],
    repos: dict[Path, tuple[str, Path] | None],
    types: dict[int, str],
    tz: tzinfo | None,
    now: datetime | None,
    purged: frozenset[str] = frozenset(),
) -> _Outcome:
    """Record what one transcript added since the session's row was last written. A session of a
    repository whose usage was purged (``purged``) is not read at all."""
    outcome = _Outcome()
    session_id = known.get(str(found.path), found.session_id)
    existing = store.session_usage(session_id)
    transcript = found.path
    if existing is not None and existing.transcript_path:
        recorded = Path(existing.transcript_path)
        if recorded != transcript:
            if not recorded.exists():
                return outcome  # the file moved: its offsets name one that is gone
            transcript = recorded  # the offsets are keyed by this spelling of the path
    cwd = session_cwd(transcript) if existing is None or not existing.repo_id else None
    if cwd is not None and cwd not in repos:
        repos[cwd] = repo_of(cwd)
    repo = repos.get(cwd) if cwd is not None else None
    if repo is not None and repo[0] in purged:
        outcome.unchanged = 1  # forgotten on purpose: its usage does not come back from here
        return outcome
    state = usage_core.ScanState.parse(existing.state if existing else None)
    scanned, state = usage_core.scan_turns(
        transcript, state, day_of=lambda stamp: _day_key(stamp, tz)
    )
    if not scanned.chunks:
        # Nothing that is a turn: no row to write, and none whose state is worth saving. (A
        # transcript of only a session-start hook's attachment has a non-empty delta, no turn,
        # and nothing to take the first turn's time from.)
        outcome.unchanged = 1
        return outcome
    delta = scanned.total()
    recorded_at = now or datetime.now(UTC)  # no turn is recorded as later than this
    outcome.repo = repo
    total = usage_core.Usage.from_dict(existing.usage) if existing else usage_core.Usage()
    total.add(delta)
    totals = total.totals()
    before = existing.turns if existing else 0
    project_id = existing.project_id if existing else (repo[0] if repo else found.session_id)
    rows: list[TurnUsage] = []
    for seq, chunk in enumerate(scanned.chunks, start=before + 1):
        figures = chunk.usage.totals()
        rows.append(
            TurnUsage(
                id=new_usage_id(),
                session_id=session_id,
                project_id=project_id,
                seq=seq,
                trigger="stop",
                recorded_at=min(_transcript_stamp(chunk.ended_at) or recorded_at, recorded_at),
                usage=chunk.usage.to_dict(),
                total_tokens=figures.total,
                weighted=figures.weighted,
                cost_usd=figures.cost_usd,
                source="sweep",
            )
        )
    last_seen = max((row.recorded_at for row in rows), default=recorded_at)
    slot = account_slot(transcript)
    if slot is None:
        slot = found.slot
    session = SessionUsage(
        session_id=session_id,
        project_id=project_id,
        transcript_path=str(transcript),
        state=state.to_json(),
        usage={**total.to_dict(), "first_edit": state.first_edit()},
        turns=before + len(rows),
        total_tokens=totals.total,
        weighted=totals.weighted,
        cost_usd=totals.cost_usd,
        started_at=existing.started_at if existing else min(r.recorded_at for r in rows),
        updated_at=max(last_seen, existing.updated_at) if existing else last_seen,
        ended_at=None,
        account_slot=slot,
        account_type=types.get(slot) if slot is not None else None,
        repo_id=repo_id_of(*repo) if repo else None,
    )
    written = store.record_usage(
        session,
        None,
        previous_state=existing.state if existing else None,
        more_turns=rows,
    )
    if not written:
        outcome.unchanged = 1  # a hook or another sweep won this one: the rest is read next time
        return outcome
    known[str(transcript)] = session_id
    outcome.turns = len(rows)
    if existing is None:
        outcome.new = 1
    else:
        outcome.updated = 1
    return outcome


def purged_repos(store: ContextStore) -> frozenset[str]:
    """The repositories whose usage was purged and that the sweep must leave alone: a project
    forgotten with its data (``project forget --purge``) must not come back from the transcripts
    Claude Code keeps for a month. A tombstone ends when the project is added back on purpose
    (it has a row and is onboarded): from then on its usage is counted again, from whatever
    transcripts remain."""
    marks = store.list_meta(USAGE_PURGED_PREFIX)
    held: set[str] = set()
    for key in marks:
        project_id = key.removeprefix(USAGE_PURGED_PREFIX)
        project = store.get_project(project_id)
        if project is not None and project.onboarded_at is not None:
            store.delete_meta(key)  # added back
        else:
            held.add(project_id)
    return frozenset(held)


def _day_key(stamp: str, tz: tzinfo | None) -> str:
    """The local day of a transcript timestamp, ``YYYY-MM-DD`` (the cut ``scan_turns`` makes)."""
    moment = _transcript_stamp(stamp)
    return _local(moment, tz).strftime("%Y-%m-%d") if moment is not None else ""


def _attribute(
    store: ContextStore, types: dict[int, str], repos: dict[Path, tuple[str, Path] | None]
) -> int:
    """Fill the account and repository of sessions recorded without them (by hooks before this
    existed): from the directory the transcript lives in and the one the session ran in. A
    repository that cannot be told (the transcript is gone, or names no directory) is the
    session's project, which is what the page falls back to; it is set all the same, so that
    the session is not asked about again."""
    done = 0
    for session in store.unattributed_usage():
        slot = session.account_slot
        path = Path(session.transcript_path) if session.transcript_path else None
        if slot is None and path is not None:
            slot = account_slot(path)
        kind = session.account_type or (types.get(slot) if slot is not None else None)
        repo_id = session.repo_id
        if repo_id is None:
            repo_id = session.project_id
            cwd = session_cwd(path) if path is not None and path.exists() else None
            if cwd is not None:
                if cwd not in repos:
                    repos[cwd] = repo_of(cwd)
                found = repos[cwd]
                if found is not None:
                    repo_id = repo_id_of(*found)
        store.attribute_usage(
            session.session_id, account_slot=slot, account_type=kind, repo_id=repo_id
        )
        done += 1
    return done


# --- repository profiles -----------------------------------------------------------------------


def refresh_profiles(
    store: ContextStore,
    roots: dict[str, Path],
    *,
    now: datetime | None = None,
    budget_s: float = _PROFILE_BUDGET_S,
) -> int:
    """Profile the repositories in ``roots`` (id → root) that have none, or one older than
    :data:`PROFILE_MAX_AGE`, for at most ``budget_s`` seconds (the rest wait for the next
    call). A root that is gone keeps the profile it had. Returns how many were written."""
    when = now or datetime.now(UTC)
    profiles = store.repo_profiles()
    deadline = time.monotonic() + budget_s
    done = 0
    for repo_id, root in roots.items():
        current = profiles.get(repo_id)
        if current is not None and when - current.refreshed_at < PROFILE_MAX_AGE:
            continue
        if not root.is_dir():
            if current is None:
                store.save_repo_profile(_bare_profile(store, repo_id, root, when))
                done += 1
            continue
        if time.monotonic() >= deadline:
            break
        store.save_repo_profile(profile_repo(store, repo_id, root, when))
        done += 1
    return done


def _display_name(store: ContextStore, repo_id: str, root: Path) -> str:
    project = store.get_project(repo_id)
    return (project.nickname if project is not None and project.nickname else None) or (
        root.name or repo_id
    )


def _bare_profile(store: ContextStore, repo_id: str, root: Path, when: datetime) -> RepoProfile:
    return RepoProfile(
        repo_id=repo_id,
        name=_display_name(store, repo_id, root),
        root=str(root),
        refreshed_at=when,
    )


def profile_repo(store: ContextStore, repo_id: str, root: Path, when: datetime) -> RepoProfile:
    """The profile of the repository at ``root``, read from git."""
    remote = repo_profile.read_remote(root)
    traits, files = repo_profile.read_traits(root, remote)
    return RepoProfile(
        repo_id=repo_id,
        name=_display_name(store, repo_id, root),
        root=str(root),
        remote=remote,
        traits=traits,
        files=files,
        refreshed_at=when,
    )


# --- reading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Turn:
    """A recorded turn, placed on the local clock."""

    day: date
    hour: int
    weekday: int
    session_id: str
    repo_id: str
    """The repository it is COUNTED under (:func:`_identity`): clones of one remote share it."""
    member: str
    """The checkout it ran in (the id the session recorded)."""
    slot: int | None
    kind: str | None
    models: dict[str, usage_core.ModelUsage]


@dataclass
class _Sums:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    cost: float = 0.0
    turns: int = 0
    sessions: set[str] = field(default_factory=set)
    last: date | None = None

    def add(self, turn: _Turn) -> None:
        for usage in turn.models.values():
            self.input += usage.input
            self.cache_write += usage.cache_write
            self.cache_read += usage.cache_read
            self.output += usage.output
            if usage.priced:
                self.cost += usage.cost_usd
        self.turns += 1
        self.sessions.add(turn.session_id)
        if self.last is None or turn.day > self.last:
            self.last = turn.day

    def counts(self) -> TokenCounts:
        return TokenCounts(
            input=self.input,
            cache_write=self.cache_write,
            cache_read=self.cache_read,
            output=self.output,
        )

    def tokens(self) -> UsageTokens:
        return UsageTokens(**self.counts().model_dump())

    def day_tokens(self) -> DayTokens:
        return DayTokens(**self.counts().model_dump())

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output


def _identity(profiles: dict[str, RepoProfile]) -> Callable[[str], str]:
    """How a recorded repository id is counted: a repository is identified by its normalised
    git remote, else by its root. So the clones and checkouts of one remote (``ws2`` to ``ws9``
    of the same project) are ONE repository on the page, in ``repos_active`` and in a day's
    rollup, and one with no remote stays its own. A repository not profiled yet is counted as
    it was recorded, and merges once it is."""

    def identify(repo_id: str) -> str:
        profile = profiles.get(repo_id)
        if repo_id != TEMP_REPO and profile is not None and profile.remote:
            return f"remote:{profile.remote}"
        return repo_id

    return identify


def _load(
    store: ContextStore,
    first: date | None,
    last: date | None,
    tz: tzinfo | None,
    identify: Callable[[str], str],
) -> list[_Turn]:
    """The turns of local days ``first`` to ``last``, oldest first; no ``first`` is from the
    beginning and no ``last`` is to the end, whenever a turn is dated (``lifetime``)."""
    since = _day_start(first, tz) if first is not None else None
    until = _day_start(last + timedelta(days=1), tz) if last is not None else None
    return [_place(row, tz, identify) for row in store.usage_turn_rows(since, until)]


def _place(row: UsageTurnRow, tz: tzinfo | None, identify: Callable[[str], str]) -> _Turn:
    local = _local(row.recorded_at, tz)
    raw = row.usage.get("models")
    models = (
        {
            str(name): usage_core.ModelUsage.from_dict(value)
            for name, value in raw.items()
            if isinstance(value, dict)
        }
        if isinstance(raw, dict)
        else {}
    )
    return _Turn(
        day=local.date(),
        hour=local.hour,
        weekday=local.weekday(),
        session_id=row.session_id,
        repo_id=identify(row.repo_id),
        member=row.repo_id,
        slot=row.account_slot,
        kind=row.account_type,
        models=models,
    )


def _sums_by(turns: Iterable[_Turn], key: Callable[[_Turn], Any]) -> dict[Any, _Sums]:
    out: dict[Any, _Sums] = {}
    for turn in turns:
        out.setdefault(key(turn), _Sums()).add(turn)
    return out


def _models_of(turns: Iterable[_Turn]) -> dict[str, usage_core.ModelUsage]:
    out: dict[str, usage_core.ModelUsage] = {}
    for turn in turns:
        for name, usage in turn.models.items():
            out.setdefault(name, usage_core.ModelUsage()).add(usage)
    return out


def _model_tokens(usage: usage_core.ModelUsage) -> TokenCounts:
    return TokenCounts(
        input=usage.input,
        cache_write=usage.cache_write,
        cache_read=usage.cache_read,
        output=usage.output,
    )


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _labels(store: ContextStore) -> dict[int, str]:
    """Slot → how the account is called: its alias, else ``plain claude`` / ``account N``."""
    labels = {
        account.slot: accounts_core.default_label(account.slot)
        for account in accounts_core.list_accounts()
    }
    for record in store.claude_accounts():
        if record.alias:
            labels[record.slot] = record.alias
    return labels


def _main_members(turns: Iterable[_Turn]) -> dict[str, str]:
    """For each repository counted, the checkout that spent the most in it (the one whose
    profile names and describes the repository when it has several)."""
    spent: dict[str, Counter[str]] = {}
    for turn in turns:
        counts = spent.setdefault(turn.repo_id, Counter())
        counts[turn.member] += sum(
            u.input + u.cache_write + u.cache_read + u.output for u in turn.models.values()
        )
    return {
        repo: min(members, key=lambda member: (-members[member], member))
        for repo, members in spent.items()
    }


def _repo_names(
    store: ContextStore, turns: Iterable[_Turn], profiles: dict[str, RepoProfile]
) -> dict[str, str]:
    """Each repository counted → its name: its main checkout's profile name (the project's
    nickname, else the folder's), else the project's, else the end of its id."""
    names: dict[str, str] = {}
    for repo, member in _main_members(turns).items():
        if repo == TEMP_REPO:
            names[repo] = TEMP_NAME
            continue
        profile = profiles.get(member)
        if profile is not None:
            names[repo] = profile.name
            continue
        project = store.get_project(member)
        names[repo] = (
            (project.nickname or project.root.name) if project is not None else ""
        ) or member[-8:]
    return names


def _stats_in(
    store: ContextStore, range_: UsageRange, now: datetime | None, tz: tzinfo | None
) -> UsageStats:
    feature_use.fold(store)
    today = _today(now, tz)
    first, last = window(range_, today)
    profiles = store.repo_profiles()
    # ``lifetime`` is everything recorded, so it has no end: a turn dated after today (a clock
    # that was ahead, a transcript synced from another machine) is in it, as in ``metrics tokens``.
    turns = _load(store, first, None if range_ == "lifetime" else last, tz, _identity(profiles))
    start = first if first is not None else (turns[0].day if turns else today)
    total = _Sums()
    for turn in turns:
        total.add(turn)
    by_day = _sums_by(turns, lambda t: t.day)

    days: list[UsageDay] = []
    cursor = start
    while cursor <= last and (turns or first is not None):
        days.append(_usage_day(cursor, by_day.get(cursor, _Sums())))
        cursor += timedelta(days=1)
    later = sorted(day for day in by_day if day > last)
    days += [_usage_day(day, by_day[day]) for day in later]  # only the days that have a turn
    busiest = sorted((d for d in days if d.tokens.total), key=lambda d: (-d.tokens.total, d.date))
    stats = UsageStats(
        range=range_,
        since=start.isoformat() if turns or first is not None else None,
        until=(later[-1] if later else last).isoformat(),
        recorded_turns=store.usage_turn_count(),
        tz_offset_minutes=_offset_minutes(today, tz),
        tokens=total.tokens(),
        cost_usd=total.cost,
        sessions=len(total.sessions),
        turns=total.turns,
        active_days=len(by_day),
        days=days,
        busiest_days=busiest[:TOP_DAYS],
        prices_as_of=pricing.PRICES_AS_OF,
    )
    for turn in turns:
        stats.by_weekday[turn.weekday] += 1
        stats.by_hour[turn.hour] += 1
    models = _models_of(turns)
    stats.unpriced_models = sorted(name for name, usage in models.items() if not usage.priced)
    stats.models = sorted(
        (
            UsageModelRow(
                model=name,
                tokens=UsageTokens(**_model_tokens(usage).model_dump()),
                cost_usd=usage.cost_usd if usage.priced else 0.0,
                priced=usage.priced,
                share=_share(usage.total, total.total),
            )
            for name, usage in models.items()
        ),
        key=lambda row: (-row.tokens.total, row.model),
    )
    stats.accounts = _accounts_of(store, turns, total)
    stats.repos = _repos_of(store, turns, profiles)
    uses: Counter[str] = Counter()
    for _, name, count in store.feature_uses(
        first.isoformat() if first else None, last.isoformat()
    ):
        uses[name] += count
    stats.features = [
        UsageFeatureRow(name=name, count=count)
        for name, count in sorted(uses.items(), key=lambda item: (-item[1], item[0]))[:TOP_FEATURES]
    ]
    return stats


def _usage_day(day: date, sums: _Sums) -> UsageDay:
    return UsageDay(
        date=day.isoformat(),
        tokens=sums.tokens(),
        cost_usd=sums.cost,
        sessions=len(sums.sessions),
        turns=sums.turns,
    )


def _accounts_of(store: ContextStore, turns: list[_Turn], total: _Sums) -> UsageAccounts:
    labels = _labels(store)
    types = account_types(store, refresh=False)
    by_slot = _sums_by(turns, lambda t: t.slot)
    rows: list[UsageAccountRow] = []
    for slot, sums in by_slot.items():
        kinds = Counter(t.kind for t in turns if t.slot == slot and t.kind)
        kind = (
            kinds.most_common(1)[0][0]
            if kinds
            else (types.get(slot, UNKNOWN_TYPE) if slot is not None else UNKNOWN_TYPE)
        )
        rows.append(
            UsageAccountRow(
                slot=slot,
                label=labels.get(slot, accounts_core.default_label(slot))
                if slot is not None
                else "other directories",
                subscription=kind,
                tokens=sums.tokens(),
                cost_usd=sums.cost,
                sessions=len(sums.sessions),
                turns=sums.turns,
                share=_share(sums.total, total.total),
            )
        )
    rows.sort(key=lambda row: (-row.tokens.total, row.slot if row.slot is not None else 1 << 30))
    registered = [row for row in rows if row.slot is not None]
    return UsageAccounts(
        registered=len(accounts_core.list_accounts()),
        active=len(registered),
        by_type=dict(sorted(Counter(row.subscription for row in registered).items())),
        top=registered[0] if registered else None,
        rows=rows,
    )


def _repos_of(
    store: ContextStore, turns: list[_Turn], profiles: dict[str, RepoProfile]
) -> UsageRepos:
    by_repo = _sums_by(turns, lambda t: t.repo_id)
    names = _repo_names(store, turns, profiles)
    rows = [
        UsageRepoRow(
            repo_id=repo_id,
            name=names[repo_id],
            tokens=sums.tokens(),
            cost_usd=sums.cost,
            sessions=len(sums.sessions),
            turns=sums.turns,
            last_active=sums.last.isoformat() if sums.last else None,
        )
        for repo_id, sums in by_repo.items()
    ]
    rows.sort(key=lambda row: (-row.tokens.total, row.name, row.repo_id))
    return UsageRepos(active=len(rows), top=rows[0] if rows else None, rows=rows[:TOP_REPOS])


def stats(
    range_: UsageRange = "7d",
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    store: ContextStore | None = None,
) -> UsageStats:
    """What this machine's Claude Code sessions spent in ``range_``, summed on read.

    ``today``, ``7d`` and ``30d`` are the last 1, 7 and 30 LOCAL days, today included;
    ``lifetime`` is everything recorded. Reads what the hooks and the sweep recorded — it does
    not sweep (:func:`maybe_sweep`, :func:`sweep`) — and folds the feature counters in.
    """
    if range_ not in USAGE_RANGES:
        raise ValueError(f"unknown range {range_!r}: one of {', '.join(USAGE_RANGES)}")
    if store is not None:
        return _stats_in(store, range_, now, tz)
    with store_session() as owned:
        return _stats_in(owned, range_, now, tz)


def daily_rollups(
    since: date,
    until: date,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    store: ContextStore | None = None,
) -> list[DailyRollup]:
    """One record per local day in ``[since, until]`` that had any activity (a turn or a
    counted feature), oldest first, shaped as the telemetry contract's ``days[]`` before
    hashing (the sharing path replaces ``repo_id`` with a salted hash and drops the rest).

    Built from the same loader as :func:`stats`, so ``tokens.total`` of a day is the day's
    figure on the dashboard and in ``aisquare metrics tokens``."""

    def run(active: ContextStore) -> list[DailyRollup]:
        feature_use.fold(active)
        profiles = active.repo_profiles()
        turns = _load(active, since, until, tz, _identity(profiles))
        uses: dict[date, dict[str, int]] = {}
        for day_text, name, count in active.feature_uses(since.isoformat(), until.isoformat()):
            with contextlib.suppress(ValueError):
                uses.setdefault(date.fromisoformat(day_text), {})[name] = count
        by_day: dict[date, list[_Turn]] = {}
        for turn in turns:
            by_day.setdefault(turn.day, []).append(turn)
        registered = len(accounts_core.list_accounts())
        known_types = account_types(active, refresh=False)
        fleet = _fleet_days(active, since, until, tz)
        return [
            _rollup(
                day,
                by_day.get(day, []),
                uses.get(day, {}),
                fleet.get(day, FleetDay()),
                profiles,
                known_types,
                registered,
                tz,
            )
            for day in sorted(set(by_day) | set(uses) | set(fleet))
        ]

    if store is not None:
        return run(store)
    with store_session() as owned:
        return run(owned)


def _fleet_days(
    store: ContextStore, since: date, until: date, tz: tzinfo | None
) -> dict[date, FleetDay]:
    """The agents started and tasks done on each local day of ``[since, until]``."""
    agents, done = store.fleet_activity(
        _day_start(since, tz), _day_start(until + timedelta(days=1), tz)
    )
    days: dict[date, FleetDay] = {}
    for started, role in agents:
        spawned = days.setdefault(_local(started, tz).date(), FleetDay()).agents_spawned
        name = role if role in FLEET_ROLES else "other"
        spawned[name] = spawned.get(name, 0) + 1
    for finished in done:
        days.setdefault(_local(finished, tz).date(), FleetDay()).tasks_done += 1
    return days


def _rollup(
    day: date,
    turns: list[_Turn],
    features: dict[str, int],
    fleet: FleetDay,
    profiles: dict[str, RepoProfile],
    known_types: dict[int, str],
    registered: int,
    tz: tzinfo | None,
) -> DailyRollup:
    total = _Sums()
    for turn in turns:
        total.add(turn)
    by_slot = _sums_by((t for t in turns if t.slot is not None), lambda t: t.slot)
    kinds: Counter[str] = Counter()
    for slot in by_slot:
        stamped = Counter(t.kind for t in turns if t.slot == slot and t.kind)
        kinds[stamped.most_common(1)[0][0] if stamped else known_types.get(slot, UNKNOWN_TYPE)] += 1
    top = max((sums.total for sums in by_slot.values()), default=0)
    by_repo = _sums_by(turns, lambda t: t.repo_id)
    mains = _main_members(turns)
    ranked = sorted(by_repo.items(), key=lambda item: (-item[1].total, item[0]))
    repos: list[DayRepo] = []
    for repo_id, sums in ranked[:ROLLUP_REPOS]:
        profile = profiles.get(mains[repo_id])
        repos.append(
            DayRepo(
                repo_id=repo_id,
                key=(profile.remote or profile.root) if profile is not None else repo_id,
                tokens_total=sums.total,
                sessions=len(sums.sessions),
                turns=sums.turns,
                traits=profile.traits if profile is not None else RepoTraits(),
            )
        )
    return DailyRollup(
        date=day.isoformat(),
        tz_offset_minutes=_offset_minutes(day, tz),
        tokens=total.day_tokens(),
        cost_usd_list=total.cost,
        sessions=len(total.sessions),
        turns=total.turns,
        models={name: _model_tokens(usage) for name, usage in sorted(_models_of(turns).items())},
        accounts=DayAccounts(
            active=len(by_slot),
            registered=registered,
            by_type=dict(sorted(kinds.items())),
            top_account_share=_share(top, total.total),
        ),
        repos_active=len(by_repo),
        repos=repos,
        features=dict(sorted(features.items())),
        fleet=FleetDay(
            agents_spawned=dict(sorted(fleet.agents_spawned.items())), tasks_done=fleet.tasks_done
        ),
    )
