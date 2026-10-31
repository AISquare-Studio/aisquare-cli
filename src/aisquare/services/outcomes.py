"""``aisquare outcomes``: what the agents achieved in ONE week, what it took, and who steered it.

**The week.** An ISO week, Monday 00:00 to Monday 00:00 on the local clock; the current one runs to
now (:func:`parse_week`). There are no day, month or all-time windows, by design: one week, and the
week before it to compare with — to the same point (Monday 00:00 plus as long as this week has
run) while this one is still in progress, so a Wednesday is not compared with a whole week.

**1. Outcomes, per repository.** A repository is one remote (its clones and worktrees are one),
else one root, named by its real name (``usage_stats.real_name``), and it is in the report when an
agent worked in it that week (a recorded turn's session ran there) or a task moved on its board.

- *Pull requests merged and opened*: ``gh``'s count of the pull requests you authored
  (``author:@me``: your agents open them as you) merged, or opened, in the week, per GitHub
  repository; one GraphQL call. Without ``gh``, or when it fails, a line says so and nothing is
  counted.
- *Commits and lines*: ``git log`` of every branch, remote branch and tag of each checkout the
  agents worked in, merges left out, by your ``user.email``, authored in the week. A commit GitHub
  made (a squash or rebase merge, its committer ``noreply@github.com``) is not your agents' work
  again; a commit rebased or cherry-picked is counted once (same author time, author and subject).
  Lines are ``--numstat``'s, binary files left out.
- *Tasks done and reopened*: the distinct board tasks marked done (``task_done``) and reopened
  (``task_reopened``) in the week, on every board.

**2. Effort.**

- *Agent-hours*: the turns' durations summed. A turn runs from its ``UserPromptSubmit`` (or the
  Stop hook that continued it) to its ``Stop`` (``services.metrics``). One with no Stop ran until
  the session's next turn started, or until the person interrupted it, whichever came first; one
  with neither has no known end, is counted in ``untimed_turns`` and adds nothing.
- *Sessions*, *turns*, *tool calls*, *tokens* and *list-price cost*: as ``metrics stats`` counts
  them, from the same rows, over the week. *Files edited*: the distinct files an ``Edit``,
  ``Write``, ``MultiEdit`` or ``NotebookEdit`` named (``services.transcript_facts``). Cost per
  merged pull request and per done task divide the week's cost by those counts.

**3. Direction: who steered.** Never tokens against tokens: a human prompt directs everything
after it, and its tokens say nothing of that.

- *Turn origin*: each turn's origin, ``human`` or ``agent`` (``core.prompt_origin``): recorded at
  its ``UserPromptSubmit`` since the store's v43 (the release after 0.9.2). An older turn's is
  inferred, and counted as inferred: its prompt in the transcript is the fleet's nudge
  (``agent:wake``) or only Claude Code's notice (``agent:notice``); else it is a fleet agent's
  first message (``agent:spawn``); else a prompt to a fleet worker, which its manager tells
  (``agent:tell``); else a person typed it. So is the turn of a session no hook saw (its
  transcript's prompts, with no duration). A Stop-hook continuation before v43 had no turn row:
  its transcript entry is counted as an inferred ``agent:continuation``, with no duration.
- *Leverage*: agent-minutes, and tool calls, per human prompt.
- *Interventions*: interrupts (Esc while it worked: ``[Request interrupted by user]``) and tool
  uses denied at their permission prompt.
- *Session mode*, after Anthropic's Economic Index (directive vs. iterative use): of the sessions
  with a turn in the week, *directive* had at most one human prompt and no interrupt, *guided* two
  or three (or one and an interrupt), *collaborative* four or more.

**4. Against last week**: the headline figures again for the week before, and the change.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from aisquare.core import paths, pricing, prompt_origin, repo_profile
from aisquare.core import usage as usage_core
from aisquare.core.spawn import git_env
from aisquare.core.store import MAX_OPEN_TURN, ContextStore, store_session
from aisquare.models import (
    OutcomeDelta,
    OutcomeDirection,
    OutcomeEffort,
    OutcomeRepo,
    OutcomeSessionRow,
    OutcomeTotals,
    OutcomeTurnRow,
    RepoProfile,
    TaskEventRow,
    UsageTokens,
    UsageTurnRow,
    WeeklyOutcomes,
)
from aisquare.services import github_prs, transcript_facts, usage_stats
from aisquare.services.transcript_facts import Fact

HUMAN_LED_ROLES = frozenset({"manager", "captain"})
"""Fleet roles a person talks to: their prompts are a person's unless recorded otherwise. Every
other fleet role is a worker, whose prompts after its first message are its manager's tells."""

MATCH_PROMPT = timedelta(seconds=90)
"""How far a turn's start and its prompt's transcript entry may be apart and still be one."""
MATCH_CONTINUATION = timedelta(seconds=120)
"""How far a recorded continuation row and a transcript's Stop-hook entry may be apart."""
LOOKAHEAD = timedelta(days=1)
"""How far past a past week's end turns are read, to end its last turns that had no Stop."""

GITHUB_COMMITTER = "noreply@github.com"
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_WEEK = re.compile(r"^(\d{4})-?W(\d{1,2})$", re.IGNORECASE)


class WeekError(ValueError):
    """``--week`` named no week this report can give."""


# --- the week ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Week:
    """One ISO week on the local clock."""

    year: int
    number: int
    start: datetime
    """Monday 00:00, local."""
    end: datetime
    """The next Monday 00:00, local."""

    @property
    def label(self) -> str:
        return f"{self.year}-W{self.number:02d}"

    @property
    def monday(self) -> date:
        return date.fromisocalendar(self.year, self.number, 1)


def week_of(day: date, tz: tzinfo | None) -> Week:
    """The ISO week ``day`` is in."""
    year, number, weekday = day.isocalendar()
    monday = day - timedelta(days=weekday - 1)
    return Week(
        year,
        number,
        usage_stats._day_start(monday, tz),
        usage_stats._day_start(monday + timedelta(days=7), tz),
    )


def weeks_in(year: int) -> int:
    """52 or 53: the ISO weeks of ``year`` (28 December is always in the last)."""
    return date(year, 12, 28).isocalendar()[1]


def parse_week(text: str | None, *, now: datetime, tz: tzinfo | None) -> Week:
    """The week ``--week`` names: none (or ``this``) is the current one, ``last`` the one before,
    ``2026-W43`` (``2026W43``, ``2026-w43``) that ISO week. A week not begun yet, a week its year
    does not have, and anything else raise :class:`WeekError` saying what is accepted."""
    today = usage_stats._local(now, tz).date()
    current = week_of(today, tz)
    word = (text or "").strip()
    if word.lower() in ("", "this", "current"):
        return current
    if word.lower() in ("last", "previous"):
        return week_of(current.monday - timedelta(days=7), tz)
    match = _WEEK.match(word)
    if match is None:
        raise WeekError(
            f"{word!r} is not a week: give 'last', or an ISO week such as {current.label} "
            "(year, W, week number)"
        )
    year, number = int(match.group(1)), int(match.group(2))
    if not 1 <= number <= weeks_in(year):
        raise WeekError(f"{year} has no week {number}: its weeks are W01 to W{weeks_in(year):02d}")
    week = week_of(date.fromisocalendar(year, number, 1), tz)
    if week.start > now:
        raise WeekError(f"{week.label} has not begun: this week is {current.label}")
    return week


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime

    def holds(self, moment: datetime) -> bool:
        return self.since <= moment < self.until


# --- what is read --------------------------------------------------------------------------------


@dataclass
class _Usage:
    session_id: str
    repo: str
    """The repository it is counted under (``usage_stats._identity``)."""
    member: str
    """The checkout it ran in (the id its session recorded)."""
    at: datetime
    models: dict[str, usage_core.ModelUsage]
    tool_calls: int


@dataclass
class _Turn:
    session_id: str
    started: datetime
    origin: str
    inferred: bool
    seconds: float | None = None
    """How long it ran, ``None`` when its end is not known."""


@dataclass
class _Commit:
    at: datetime
    added: int
    deleted: int


@dataclass
class _Repo:
    key: str
    members: set[str] = field(default_factory=set)
    roots: list[Path] = field(default_factory=list)
    name: str = ""
    remote: str | None = None
    """Its normalised remote (lower case): what identifies it, not what names it."""
    github: str | None = None
    commits: list[_Commit] = field(default_factory=list)
    prs: dict[tuple[str, str], int] = field(default_factory=dict)
    """``(window, "merged" | "opened")`` → count, for the GitHub repositories ``gh`` answered."""


@dataclass
class _Read:
    usage: list[_Usage]
    turns: list[OutcomeTurnRow]
    sessions: dict[str, OutcomeSessionRow]
    facts: dict[str, list[Fact]]
    """Session id → its transcript's facts."""
    tasks: list[TaskEventRow]
    repos: dict[str, _Repo]
    identify: Callable[[str], str]
    prs_note: str | None = None


# --- the report ----------------------------------------------------------------------------------


def week_report(
    week: str | None = None,
    *,
    now: datetime | None = None,
    tz: tzinfo | None = None,
    store: ContextStore | None = None,
    use_gh: bool = True,
) -> WeeklyOutcomes:
    """The week ``week`` names (:func:`parse_week`), measured. ``use_gh=False`` counts no pull
    request (the report says it was not asked)."""
    when = now or datetime.now(UTC)
    chosen = parse_week(week, now=when, tz=tz)
    if store is not None:
        return _week_report_in(store, chosen, when, tz, use_gh)
    with store_session() as owned:
        return _week_report_in(owned, chosen, when, tz, use_gh)


def _week_report_in(
    store: ContextStore, week: Week, now: datetime, tz: tzinfo | None, use_gh: bool
) -> WeeklyOutcomes:
    this = Window(week.start, min(week.end, now))
    before = week_of(week.monday - timedelta(days=7), tz)
    last = Window(before.start, min(before.end, before.start + (this.until - this.since)))
    result = WeeklyOutcomes(
        week=week.label,
        since=this.since,
        until=this.until,
        in_progress=now < week.end,
        tz_offset_minutes=usage_stats._offset_minutes(week.monday, tz),
        compared_with=before.label,
        compared_since=last.since,
        compared_until=last.until,
        prices_as_of=pricing.PRICES_AS_OF,
    )
    read = _read_week(store, this, last, now, use_gh)
    if not _anything_in(read, this):
        result.empty = True
        return result
    result.outcomes, result.effort, result.direction = _measure_window(read, this, "this")
    previous = _measure_window(read, last, "last")
    result.vs_last_week = _week_deltas((result.outcomes, result.effort, result.direction), previous)
    return result


def _anything_in(read: _Read, window: Window) -> bool:
    return (
        any(window.holds(row.at) for row in read.usage)
        or any(window.holds(row.started_at) for row in read.turns)
        or any(window.holds(event.created_at) for event in read.tasks)
    )


def _read_week(
    store: ContextStore, this: Window, last: Window, now: datetime, use_gh: bool
) -> _Read:
    """Everything both windows need, read once: the usage and turn rows, the sessions and their
    transcripts' facts, the board's task events, and — at once, beside the rest — each
    repository's commits and pull requests."""
    profiles = store.repo_profiles()
    identify = usage_stats._identity(profiles)
    since, until = last.since, this.until
    usage = [_usage_row(row, identify) for row in store.usage_turn_rows(since, until)]
    turns = store.outcome_turns(since, min(now, until + LOOKAHEAD))
    tasks = store.task_events(since, until, ("task_done", "task_reopened"))
    repos = _week_repos(store, profiles, identify, usage, tasks, (this, last))
    with ThreadPoolExecutor(max_workers=8) as pool:
        prs: Future[str | None] | None = (
            pool.submit(_week_pull_requests, repos, this, last) if use_gh else None
        )
        lines = _Lines()
        roots = sorted({root for repo in repos.values() for root in repo.roots})
        listing = {root: pool.submit(_commits_listed, root, since) for root in roots}
        naming = [pool.submit(_name_repo, repo) for repo in repos.values()]
        ids = {row.session_id for row in usage} | {
            row.session_id for row in turns if row.session_id
        }
        sessions = store.outcome_sessions(ids)
        facts = _session_facts(sessions, since, now)
        listed = {root: job.result() for root, job in listing.items()}
        counting = [
            pool.submit(_week_commits, repo, listed, since, until, lines) for repo in repos.values()
        ]
        for job in [*counting, *naming]:
            job.result()
        lines.save_lines()
        note = prs.result() if prs is not None else "not asked"
    return _Read(usage, turns, sessions, facts, tasks, repos, identify, prs_note=note)


def _usage_row(row: UsageTurnRow, identify: Callable[[str], str]) -> _Usage:
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
    tools = row.usage.get("tools")
    calls = sum(n for n in tools.values() if isinstance(n, int)) if isinstance(tools, dict) else 0
    repo = identify(row.repo_id)
    return _Usage(row.session_id, repo, row.repo_id, row.recorded_at, models, calls)


def _session_facts(
    sessions: dict[str, OutcomeSessionRow], since: datetime, now: datetime
) -> dict[str, list[Fact]]:
    paths = {
        session_id: Path(row.transcript_path)
        for session_id, row in sessions.items()
        if row.transcript_path
    }
    found = transcript_facts.read_facts(sorted(set(paths.values())), since=since, now=now)
    return {session_id: found.get(path, []) for session_id, path in paths.items()}


# --- repositories, commits, pull requests --------------------------------------------------------


def _week_repos(
    store: ContextStore,
    profiles: dict[str, RepoProfile],
    identify: Callable[[str], str],
    usage: list[_Usage],
    tasks: list[TaskEventRow],
    windows: Iterable[Window],
) -> dict[str, _Repo]:
    """The repositories agents worked in, or whose board moved, in either window, each with its
    checkouts' roots, its real name and its GitHub ``owner/name``."""
    spans = list(windows)
    repos: dict[str, _Repo] = {}
    for row in usage:
        if row.repo != usage_stats.TEMP_REPO and any(w.holds(row.at) for w in spans):
            repos.setdefault(row.repo, _Repo(row.repo)).members.add(row.member)
    for event in tasks:
        key = identify(event.project_id)
        repos.setdefault(key, _Repo(key)).members.add(event.project_id)
    for repo in repos.values():
        remote: str | None = None
        seen: set[Path] = set()
        for member in sorted(repo.members):
            profile = profiles.get(member)
            root: Path | None = Path(profile.root) if profile is not None else None
            if profile is not None and profile.remote:
                remote = remote or profile.remote
            if root is None:
                project = store.get_project(member)
                root = project.root if project is not None else None
            if root is not None and root not in seen:
                seen.add(root)
                repo.roots.append(root)
        repo.remote = remote
        repo.name = usage_stats.real_name(remote, repo.roots[0] if repo.roots else None)
        if remote and remote.startswith("github.com/"):
            repo.github = remote.removeprefix("github.com/")
    return repos


def _name_repo(repo: _Repo) -> None:
    """Name ``repo`` from its remote's URL as a checkout spells it
    (``git@github.com:Org/MyRepo.git`` is ``MyRepo``): the profile's remote is normalised to
    lower case, which identifies a repository but is not its name. A repository with no remote
    keeps its folder's name, and one whose checkouts are all gone the normalised remote's."""
    if repo.remote is None:
        return
    for root in repo.roots:
        url = _remote_url_of(root) if root.is_dir() else None
        if url:
            repo.name = usage_stats.real_name(url, root) or repo.name
            return


def _remote_url_of(root: Path) -> str | None:
    """The URL of ``root``'s ``origin``, else of its first remote, as git has it (never stored:
    it may carry a token)."""
    out = repo_profile.git_output(root, "config", "--get", "remote.origin.url")
    if not out:
        listed = (repo_profile.git_output(root, "remote", "-v") or b"").split()
        out = listed[1] if len(listed) > 1 else None
    if not out:
        return None
    return out.decode("utf-8", "replace").strip() or None


def _commits_listed(root: Path, since: datetime) -> list[tuple[str, int, str, str, str]] | None:
    """The commits ``root``'s branches, remote branches and tags hold, committed from ``since``,
    merges left out, as ``(sha, author time, author, committer, subject)``: only those whose
    author is this checkout's ``user.email`` when it has one. ``None`` when git
    cannot say (not a repository any more, git missing)."""
    if not root.is_dir():
        return None
    email = repo_profile.git_output(root, "config", "user.email")
    me = email.decode("utf-8", "replace").strip().lower() if email else None
    out = repo_profile.git_output(
        root,
        "log",
        "--branches",
        "--remotes",
        "--tags",
        "--no-merges",
        f"--since={since.isoformat()}",
        "--format=%H%x1f%at%x1f%ae%x1f%ce%x1f%s",
    )
    if out is None:
        return None
    found: list[tuple[str, int, str, str, str]] = []
    for line in out.decode("utf-8", "replace").splitlines():
        fields = line.split("\x1f")
        if len(fields) < 5 or not fields[1].isdigit():
            continue
        sha, stamp, author, committer, subject = fields[:5]
        if me is not None and author.strip().lower() != me:
            continue
        found.append((sha, int(stamp), author.strip().lower(), committer.strip().lower(), subject))
    return found


def _week_commits(
    repo: _Repo,
    listed: dict[Path, list[tuple[str, int, str, str, str]] | None],
    since: datetime,
    until: datetime,
    lines: _Lines,
) -> None:
    """``repo``'s commits you authored in ``[since, until)``, once each, into ``repo.commits``,
    from its checkouts' lists (:func:`_commits_listed`): a commit GitHub made is left out, and one
    rebased or cherry-picked (the same author time, author and subject) counted once. Their lines
    are counted once per commit and kept (:class:`_Lines`): every clone lists the same commits, and
    the diff is the expensive part of ``git log --numstat``, seconds a clone on a busy machine."""
    seen: set[tuple[int, str, str]] = set()
    found: list[tuple[str, Path, datetime]] = []
    for root in repo.roots:
        for sha, stamp, author, committer, subject in listed.get(root) or ():
            key = (stamp, author, subject)
            if committer == GITHUB_COMMITTER or key in seen:
                continue
            seen.add(key)
            at = datetime.fromtimestamp(stamp, UTC)
            if since <= at < until:
                found.append((sha, root, at))
    lines.count_missing([(sha, root) for sha, root, _ in found])
    for sha, _, at in found:
        added, deleted = lines.lines_of(sha)
        repo.commits.append(_Commit(at, added, deleted))


class _Lines:
    """Lines added and deleted per commit, by sha, kept in ``cache/commit-lines.json``: a commit's
    diff never changes, so each is counted once (``git diff-tree --numstat``) and read after.
    Shared by the threads that list each repository's commits."""

    KEEP = 20_000
    """How many commits are kept: the most recent, by when they were first counted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._known: dict[str, list[int]] = {}
        self._changed = False
        try:
            data = json.loads(self.lines_path().read_text(encoding="utf-8"))
            commits = data.get("commits") if isinstance(data, dict) else None
            if isinstance(commits, dict):
                self._known = {
                    str(sha): [int(v) for v in value[:2]]
                    for sha, value in commits.items()
                    if isinstance(value, list) and len(value) >= 2
                }
        except (OSError, ValueError, TypeError):
            self._known = {}

    @staticmethod
    def lines_path() -> Path:
        return paths.cache_dir() / "commit-lines.json"

    def lines_of(self, sha: str) -> tuple[int, int]:
        with self._lock:
            added, deleted = self._known.get(sha, [0, 0])
        return added, deleted

    def count_missing(self, commits: list[tuple[str, Path]]) -> None:
        """Count the lines of the ``(sha, checkout)`` not known yet: one ``diff-tree`` a
        checkout. A commit git cannot diff counts no lines."""
        by_root: dict[Path, list[str]] = {}
        with self._lock:
            for sha, root in commits:
                if sha not in self._known:
                    by_root.setdefault(root, []).append(sha)
        for root, shas in by_root.items():
            counted = _commit_lines(root, shas)
            with self._lock:
                for sha in shas:
                    self._known[sha] = list(counted.get(sha, (0, 0)))
                self._changed = True

    def save_lines(self) -> None:
        with self._lock:
            if not self._changed:
                return
            kept = dict(list(self._known.items())[-self.KEEP :])
        path = self.lines_path()
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(f".{os.getpid()}.tmp")
            temp.write_text(json.dumps({"commits": kept}, separators=(",", ":")), encoding="utf-8")
            temp.replace(path)


def _commit_lines(root: Path, shas: list[str]) -> dict[str, tuple[int, int]]:
    """``{sha: (added, deleted)}`` for ``shas`` in the repository at ``root``: ``git diff-tree
    --stdin --numstat`` (binary files count none), the first commit diffed against nothing."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "diff-tree", "--stdin", "--numstat", "-r", "--root"],
            input="\n".join(shas) + "\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[str, tuple[int, int]] = {}
    current: str | None = None
    for line in done.stdout.splitlines():
        if _SHA.match(line):
            current = line.strip()
            out.setdefault(current, (0, 0))
            continue
        parts = line.split("\t")
        if current is not None and len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            added, deleted = out[current]
            out[current] = (added + int(parts[0]), deleted + int(parts[1]))
    return out


def _iso_second(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _gh_installed() -> bool:
    """Whether ``gh`` is on ``PATH`` (the seam the tests replace)."""
    return shutil.which("gh") is not None


def _week_pull_requests(repos: dict[str, _Repo], this: Window, last: Window) -> str | None:
    """Ask GitHub, in one GraphQL call, how many pull requests you authored were merged and opened
    in each window in each GitHub repository; into ``repo.prs``. Why it was not asked, or not
    answered, when it was not; ``None`` when it was."""
    asked = [repo for repo in repos.values() if repo.github]
    if not asked:
        return "no GitHub repository among them" if repos else None
    if not _gh_installed():
        return "skipped: gh is not installed"
    parts: list[str] = []
    aliases: dict[str, tuple[_Repo, str, str]] = {}
    for index, repo in enumerate(asked):
        for name, window in (("this", this), ("last", last)):
            if window.until <= window.since:
                continue
            span = (
                f"{_iso_second(window.since)}..{_iso_second(window.until - timedelta(seconds=1))}"
            )
            for kind, qualifier in (("merged", "merged"), ("opened", "created")):
                alias = f"r{index}_{name}_{kind}"
                aliases[alias] = (repo, name, kind)
                query = json.dumps(f"repo:{repo.github} is:pr author:@me {qualifier}:{span}")
                parts.append(
                    f"{alias}: search(query: {query}, type: ISSUE, first: 0) {{ issueCount }}"
                )
    try:
        done = github_prs._gh(["api", "graphql", "-f", "query=query { " + " ".join(parts) + " }"])
    except (OSError, ValueError) as exc:
        return f"skipped: gh could not be run ({exc})"
    except Exception as exc:  # a timeout: gh did not answer
        return f"skipped: gh did not answer ({type(exc).__name__})"
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        return f"skipped: gh failed ({said[0] if said else f'exit {done.returncode}'})"
    try:
        data = json.loads(done.stdout).get("data") or {}
    except (ValueError, AttributeError):
        return "skipped: gh answered something that is not JSON"
    missing = 0
    for alias, (repo, name, kind) in aliases.items():
        found = data.get(alias)
        count = found.get("issueCount") if isinstance(found, dict) else None
        if isinstance(count, int):
            repo.prs[(name, kind)] = count
        else:
            missing += 1
    return f"{missing} of GitHub's counts were not given" if missing else None


# --- measuring one window ------------------------------------------------------------------------


def _measure_window(
    read: _Read, window: Window, name: str
) -> tuple[OutcomeTotals, OutcomeEffort, OutcomeDirection]:
    usage = [row for row in read.usage if window.holds(row.at)]
    turns = _window_turns(read, window)
    outcomes = _window_outcomes(read, window, name, usage)
    effort = _window_effort(read, window, usage, turns, outcomes)
    direction = _window_direction(read, window, turns, effort)
    return outcomes, effort, direction


def _window_outcomes(read: _Read, window: Window, name: str, usage: list[_Usage]) -> OutcomeTotals:
    active = {row.repo for row in usage if row.repo != usage_stats.TEMP_REPO}
    done: dict[str, set[str]] = {}
    reopened: dict[str, set[str]] = {}
    for event in read.tasks:
        if not window.holds(event.created_at):
            continue
        bucket = done if event.kind == "task_done" else reopened
        board = read.identify(event.project_id)
        bucket.setdefault(board, set()).add(event.task_id or event.created_at.isoformat())
        active.add(board)
    rows: list[OutcomeRepo] = []
    for key in active:
        repo = read.repos.get(key) or _Repo(key)
        commits = [c for c in repo.commits if window.holds(c.at)]
        if not repo.name and not commits and key not in done and key not in reopened:
            continue  # no root, no remote, nothing done: a directory no repository was told of
        rows.append(
            OutcomeRepo(
                name=repo.name or key[-8:],
                key=key.removeprefix("remote:"),
                github=repo.github,
                prs_merged=repo.prs.get((name, "merged")),
                prs_opened=repo.prs.get((name, "opened")),
                commits=len(commits),
                lines_added=sum(c.added for c in commits),
                lines_deleted=sum(c.deleted for c in commits),
                tasks_done=len(done.get(key, ())),
                tasks_reopened=len(reopened.get(key, ())),
            )
        )
    rows.sort(
        key=lambda r: (-(r.prs_merged or 0), -r.commits, -r.tasks_done, r.name.lower(), r.key)
    )
    merged = [r.prs_merged for r in rows if r.prs_merged is not None]
    opened = [r.prs_opened for r in rows if r.prs_opened is not None]
    return OutcomeTotals(
        prs_merged=sum(merged) if merged else None,
        prs_opened=sum(opened) if opened else None,
        prs_note=read.prs_note,
        commits=sum(r.commits for r in rows),
        lines_added=sum(r.lines_added for r in rows),
        lines_deleted=sum(r.lines_deleted for r in rows),
        tasks_done=sum(r.tasks_done for r in rows),
        tasks_reopened=sum(r.tasks_reopened for r in rows),
        repos=rows,
    )


def _window_effort(
    read: _Read,
    window: Window,
    usage: list[_Usage],
    turns: list[_Turn],
    outcomes: OutcomeTotals,
) -> OutcomeEffort:
    tokens = UsageTokens()
    cost = 0.0
    for row in usage:
        for model in row.models.values():
            tokens.input += model.input
            tokens.cache_write += model.cache_write
            tokens.cache_read += model.cache_read
            tokens.output += model.output
            if model.priced:
                cost += model.cost_usd
    timed = [turn.seconds for turn in turns if turn.seconds is not None]
    edited = {
        fact.detail
        for facts in read.facts.values()
        for fact in facts
        if fact.kind == transcript_facts.EDIT and window.holds(fact.at)
    }
    return OutcomeEffort(
        agent_hours=round(sum(timed) / 3600, 2),
        timed_turns=len(timed),
        untimed_turns=len(turns) - len(timed),
        sessions=len(
            {row.session_id for row in usage} | {t.session_id for t in turns if t.session_id}
        ),
        turns=len(turns),
        tool_calls=sum(row.tool_calls for row in usage),
        files_edited=len(edited),
        tokens=tokens,
        cost_usd=round(cost, 4),
        cost_per_merged_pr=round(cost / outcomes.prs_merged, 2) if outcomes.prs_merged else None,
        cost_per_done_task=round(cost / outcomes.tasks_done, 2) if outcomes.tasks_done else None,
    )


def _window_direction(
    read: _Read, window: Window, turns: list[_Turn], effort: OutcomeEffort
) -> OutcomeDirection:
    origins = Counter(turn.origin for turn in turns)
    human = sum(n for origin, n in origins.items() if origin.startswith(f"{prompt_origin.HUMAN}:"))
    interrupts: Counter[str] = Counter()
    denied = 0
    for session_id, facts in read.facts.items():
        for fact in facts:
            if not window.holds(fact.at):
                continue
            if fact.kind == transcript_facts.INTERRUPT:
                interrupts[session_id] += 1
            elif fact.kind == transcript_facts.DENIAL:
                denied += 1
    prompts: Counter[str] = Counter()
    for turn in turns:
        if not turn.session_id:
            continue  # a row no session wrote has no session to give a mode
        human_prompt = turn.origin.startswith(f"{prompt_origin.HUMAN}:")
        prompts[turn.session_id] = prompts.get(turn.session_id, 0) + int(human_prompt)
    modes: Counter[str] = Counter()
    for session_id, count in prompts.items():
        if count >= 4:
            modes["collaborative"] += 1
        elif count <= 1 and not interrupts[session_id]:
            modes["directive"] += 1
        else:
            modes["guided"] += 1
    return OutcomeDirection(
        turns=len(turns),
        human_turns=human,
        agent_turns=len(turns) - human,
        human_share=round(human / len(turns), 4) if turns else None,
        by_origin=dict(sorted(origins.items(), key=lambda item: (-item[1], item[0]))),
        inferred_turns=sum(1 for turn in turns if turn.inferred),
        agent_minutes_per_human_prompt=round(effort.agent_hours * 60 / human, 1) if human else None,
        tool_calls_per_human_prompt=round(effort.tool_calls / human, 1) if human else None,
        interrupts=sum(interrupts.values()),
        denied_permissions=denied,
        sessions=len(prompts),
        directive=modes["directive"],
        guided=modes["guided"],
        collaborative=modes["collaborative"],
    )


# --- turns: their durations and their origins ----------------------------------------------------


def _window_turns(read: _Read, window: Window) -> list[_Turn]:
    """The window's turns: every turn row started in it, timed and with its origin (recorded, or
    inferred), and the turns no row recorded that the transcripts show: a Stop hook's continuation
    before rows were opened for them, and the prompts of a session no hook watched."""
    by_session: dict[str, list[OutcomeTurnRow]] = {}
    for row in read.turns:
        by_session.setdefault(row.session_id or "", []).append(row)
    out: list[_Turn] = []
    for session_id, rows in by_session.items():
        rows.sort(key=lambda row: row.started_at)
        facts = read.facts.get(session_id, [])
        stops = [f.at for f in facts if f.kind == transcript_facts.INTERRUPT]
        prompts = [f for f in facts if f.kind == transcript_facts.PROMPT]
        times = [f.at for f in prompts]
        session = read.sessions.get(session_id)
        for index, row in enumerate(rows):
            if not window.holds(row.started_at):
                continue
            following = rows[index + 1].started_at if index + 1 < len(rows) else None
            seconds = _turn_seconds(row, following, stops)
            if row.origin:
                out.append(_Turn(session_id, row.started_at, row.origin, False, seconds))
                continue
            origin = _infer_origin(row.started_at, prompts, session, times)
            out.append(_Turn(session_id, row.started_at, origin, True, seconds))
        recorded = [
            row.started_at for row in rows if row.origin == prompt_origin.AGENT_CONTINUATION
        ]
        for fact in facts:
            if fact.kind != transcript_facts.CONTINUATION or not window.holds(fact.at):
                continue
            if any(abs(fact.at - at) <= MATCH_CONTINUATION for at in recorded):
                continue
            out.append(_Turn(session_id, fact.at, prompt_origin.AGENT_CONTINUATION, True))
    for session_id, facts in read.facts.items():
        if session_id in by_session:
            continue  # its turns are rows: its transcript only adds to them, above
        session = read.sessions.get(session_id)
        prompts = [f for f in facts if f.kind == transcript_facts.PROMPT]
        times = [f.at for f in prompts]
        for fact in facts:
            if not window.holds(fact.at):
                continue
            if fact.kind == transcript_facts.PROMPT:
                origin = _infer_origin(fact.at, prompts, session, times)
                out.append(_Turn(session_id, fact.at, origin, True))
            elif fact.kind == transcript_facts.CONTINUATION:
                out.append(_Turn(session_id, fact.at, prompt_origin.AGENT_CONTINUATION, True))
    out.sort(key=lambda turn: turn.started)
    return out


def _turn_seconds(
    row: OutcomeTurnRow, following: datetime | None, interrupts: list[datetime]
) -> float | None:
    """How long a turn row ran: to its Stop; with none, to the person's interrupt or the
    session's next turn, whichever came first; never past the next turn, nor longer than a
    Stop would close (``store.MAX_OPEN_TURN``). ``None`` when nothing says when it ended."""
    end = row.ended_at
    if end is None:
        cut = [at for at in interrupts if at > row.started_at]
        if following is not None:
            cut.append(following)
        end = min(cut) if cut else None
    elif following is not None:
        end = min(end, following)
    if end is None or end < row.started_at:
        return None
    return min(end - row.started_at, MAX_OPEN_TURN).total_seconds()


def _nearest_prompt(prompts: list[Fact], times: list[datetime], started: datetime) -> Fact | None:
    """The prompt (``prompts``, oldest first; ``times`` their moments) nearest ``started``, if
    within :data:`MATCH_PROMPT`."""
    index = bisect.bisect_left(times, started)
    near = [prompts[i] for i in (index - 1, index) if 0 <= i < len(prompts)]
    best = min(near, key=lambda fact: abs(fact.at - started), default=None)
    return best if best is not None and abs(best.at - started) <= MATCH_PROMPT else None


def _infer_origin(
    started: datetime,
    prompts: list[Fact],
    session: OutcomeSessionRow | None,
    times: list[datetime] | None = None,
) -> str:
    """The origin of a turn none was recorded for (see the module's doc). ``prompts`` are the
    session's, oldest first, and ``times`` their moments (made here when not given)."""
    nearest = _nearest_prompt(
        prompts, times if times is not None else [f.at for f in prompts], started
    )
    if nearest is not None and nearest.detail == transcript_facts.WAKE:
        return prompt_origin.compose_origin(prompt_origin.AGENT, prompt_origin.WAKE)
    if nearest is not None and nearest.detail == transcript_facts.NOTICE:
        return prompt_origin.AGENT_NOTICE
    if session is not None and session.fleet:
        first = prompts[0] if prompts else None
        if session.first_prompt and first is not None and nearest is first:
            return prompt_origin.compose_origin(prompt_origin.AGENT, prompt_origin.SPAWN)
        if (session.role or "") not in HUMAN_LED_ROLES:
            return prompt_origin.compose_origin(prompt_origin.AGENT, prompt_origin.TELL)
    return prompt_origin.HUMAN_TYPED


# --- against last week ---------------------------------------------------------------------------


def _week_deltas(
    this: tuple[OutcomeTotals, OutcomeEffort, OutcomeDirection],
    last: tuple[OutcomeTotals, OutcomeEffort, OutcomeDirection],
) -> list[OutcomeDelta]:
    def figures(
        week: tuple[OutcomeTotals, OutcomeEffort, OutcomeDirection],
    ) -> dict[str, float | None]:
        outcomes, effort, direction = week
        return {
            "prs_merged": outcomes.prs_merged,
            "commits": outcomes.commits,
            "tasks_done": outcomes.tasks_done,
            "agent_hours": effort.agent_hours,
            "turns": effort.turns,
            "cost_usd": effort.cost_usd,
            "human_share": direction.human_share,
            "agent_minutes_per_human_prompt": direction.agent_minutes_per_human_prompt,
            "interrupts": direction.interrupts,
        }

    now, then = figures(this), figures(last)
    out: list[OutcomeDelta] = []
    for name, value in now.items():
        before = then[name]
        change = None if value is None or before is None else round(value - before, 4)
        out.append(
            OutcomeDelta(
                name=name,
                this_week=value,
                last_week=before,
                change=change,
                change_pct=round(change / before, 4) if change is not None and before else None,
            )
        )
    return out


def outcomes_json(result: WeeklyOutcomes) -> dict[str, Any]:
    """The report as ``--json`` prints it."""
    return result.model_dump(mode="json")
