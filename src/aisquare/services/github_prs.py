"""Pull requests from GitHub, for ``aisquare watch`` and the captain's queue (P2-W2, #284).

One reader, two consumers. ``aisquare watch`` turns each open pull request's state into
items (:func:`pr_items`: approved, changes requested, checks done), and the captain's
attention queue lists the approved ones as ``pr`` items (:func:`queue_provider`, plugged
into ``StoreSources`` when ``[watch] github = true``, which closes the captain's Phase 2
``gh`` provider).

Every request goes through ``gh api`` (:data:`_gh`, the seam tests replace) and is
conditional: the ETag of the last answer rides as ``If-None-Match``, and a ``304`` costs
no rate limit (GitHub's REST best practices) and makes no further call. Requests are
serial. Per pull request, the reviews are read again only when its ``updated_at`` moved (a
review moves it; an approval does not move the head), and its check runs only until they
have all completed for its head. The ETags and the answers they stand for are kept in
``~/.aisquare/watch/gh-etags.json``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aisquare.core import paths
from aisquare.core.spawn import git_env, untraced_env
from aisquare.core.workspace import git_argv

_GH_TIMEOUT = 30.0
"""Seconds one ``gh`` call may take before it is given up on."""


def _run_gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
        check=False,
        env=untraced_env(),
    )


_gh: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run_gh
"""The one way out to GitHub: ``gh`` with these arguments. Tests replace it."""


class GhError(RuntimeError):
    """``gh`` could not answer: not installed, not logged in, or GitHub said no."""


def watch_dir() -> Path:
    """Where ``aisquare watch`` keeps its state, its detail files and the ETags."""
    return paths.aisquare_home() / "watch"


def _etags_path() -> Path:
    return watch_dir() / "gh-etags.json"


_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?/?$"
)


def repo_of(root: Path) -> str | None:
    """``owner/name`` of the GitHub repository ``root``'s ``origin`` points at, or ``None``
    (no git, no ``origin``, or not GitHub)."""
    try:
        done = subprocess.run(
            git_argv("-C", str(root), "remote", "get-url", "origin"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = _GITHUB_REMOTE.match(done.stdout.strip()) if done.returncode == 0 else None
    return found.group("repo") if found else None


class Cache:
    """The ETags and the answers they stand for, and each pull request's derived state,
    read from and written back to ``gh-etags.json`` whole."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, Any] = data or {}
        self.data.setdefault("etags", {})
        self.data.setdefault("prs", {})
        self.requests = 0
        self.not_modified = 0

    @classmethod
    def load(cls) -> Cache:
        try:
            raw = json.loads(_etags_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        return cls(raw if isinstance(raw, dict) else None)

    def save(self) -> None:
        path = _etags_path()
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, path)

    def get(self, path: str) -> Any:
        """``gh api <path>``'s JSON, asked with the last ETag; a ``304`` answers from the
        cache. Raises :class:`GhError`."""
        known = self.data["etags"].get(path)
        args = ["api", path, "-i"]
        if isinstance(known, dict) and known.get("etag"):
            args += ["-H", f"If-None-Match: {known['etag']}"]
        try:
            done = _gh(args)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GhError(f"gh could not run: {exc}") from exc
        self.requests += 1
        status, headers, body = _split_response(done.stdout)
        if status == 304 and isinstance(known, dict):
            self.not_modified += 1
            return known.get("body")
        if status != 200:
            said = (done.stderr or body).strip().splitlines()
            raise GhError(f"gh api {path}: {status or done.returncode} {said[0] if said else ''}")
        try:
            parsed = json.loads(body) if body.strip() else None
        except ValueError as exc:
            raise GhError(f"gh api {path}: not JSON ({exc})") from exc
        etag = headers.get("etag")
        if etag:
            self.data["etags"][path] = {"etag": etag, "body": parsed}
        return parsed


def _split_response(text: str) -> tuple[int, dict[str, str], str]:
    """``gh api -i``'s output: the status line, the headers (lower-cased names), the body."""
    head, _, body = text.replace("\r\n", "\n").partition("\n\n")
    lines = head.splitlines()
    status = 0
    if lines:
        found = re.match(r"HTTP/\S+\s+(\d{3})", lines[0])
        status = int(found.group(1)) if found else 0
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers, body


@dataclass(frozen=True)
class PrState:
    """One open pull request, as the watcher judges it."""

    repo: str
    number: int
    title: str
    url: str
    head: str
    approved: bool
    changes_requested: bool
    review_id: int | None
    """The newest review that set either of the two, which names an item."""
    checks: str | None
    """``success`` or ``failure`` once every check run on the head completed; ``None``
    while any runs, or when it has none."""


def _verdicts(reviews: Sequence[Mapping[str, Any]]) -> tuple[bool, bool, int | None]:
    """Approved, changes requested, and the newest deciding review, by each reviewer's
    LAST approving or requesting review (a comment does not undo an approval)."""
    last: dict[str, Mapping[str, Any]] = {}
    for review in reviews:
        state = review.get("state")
        who = (review.get("user") or {}).get("login") or ""
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            last[who] = review
    states = [r.get("state") for r in last.values()]
    newest = max((int(r.get("id") or 0) for r in last.values()), default=0) or None
    changes = "CHANGES_REQUESTED" in states
    return ("APPROVED" in states and not changes), changes, newest


def _checks(runs: Mapping[str, Any] | None) -> str | None:
    items = (runs or {}).get("check_runs") or []
    if not items or any(run.get("status") != "completed" for run in items):
        return None
    good = {"success", "neutral", "skipped"}
    return "success" if all(run.get("conclusion") in good for run in items) else "failure"


def open_pull_requests(repo: str, cache: Cache) -> list[PrState]:
    """Every open pull request of ``repo``, judged (:class:`PrState`). Raises
    :class:`GhError`."""
    listed = cache.get(f"repos/{repo}/pulls?state=open&per_page=100") or []
    out: list[PrState] = []
    for pr in listed:
        number = int(pr["number"])
        head = str((pr.get("head") or {}).get("sha") or "")
        key = f"{repo}#{number}"
        kept = cache.data["prs"].get(key) or {}
        updated = str(pr.get("updated_at") or "")
        if kept.get("head") != head or kept.get("updated_at") != updated:
            reviews = cache.get(f"repos/{repo}/pulls/{number}/reviews?per_page=100") or []
            approved, changes, review_id = _verdicts(reviews)
            kept = {
                "head": head,
                "updated_at": updated,
                "approved": approved,
                "changes_requested": changes,
                "review_id": review_id,
                "checks": None if kept.get("head") != head else kept.get("checks"),
            }
        if kept.get("checks") is None and head:
            kept["checks"] = _checks(
                cache.get(f"repos/{repo}/commits/{head}/check-runs?per_page=100")
            )
        cache.data["prs"][key] = kept
        out.append(
            PrState(
                repo=repo,
                number=number,
                title=str(pr.get("title") or ""),
                url=str(pr.get("html_url") or ""),
                head=head,
                approved=bool(kept["approved"]),
                changes_requested=bool(kept["changes_requested"]),
                review_id=kept.get("review_id"),
                checks=kept.get("checks"),
            )
        )
    return out


@dataclass(frozen=True)
class PrItem:
    """One thing about a pull request worth a manager's look: its id is stable for what it
    says (the head, the review, the checks' outcome), so the same news is never new twice."""

    id: str
    kind: str
    summary: str
    url: str


def pr_items(state: PrState) -> list[PrItem]:
    """What ``state`` says that the watcher reports: ``pr_approved``,
    ``pr_changes_requested`` and ``pr_checks_done``."""
    name = f"{state.repo}#{state.number}"
    items = []
    if state.approved:
        items.append(
            PrItem(
                f"pr_approved:{name}@{state.head}:{state.review_id}",
                "pr_approved",
                f"{name} approved: {state.title}",
                state.url,
            )
        )
    if state.changes_requested:
        items.append(
            PrItem(
                f"pr_changes_requested:{name}@{state.head}:{state.review_id}",
                "pr_changes_requested",
                f"{name} changes requested: {state.title}",
                state.url,
            )
        )
    if state.checks is not None:
        items.append(
            PrItem(
                f"pr_checks_done:{name}@{state.head}:{state.checks}",
                "pr_checks_done",
                f"{name} checks {state.checks}: {state.title}",
                state.url,
            )
        )
    return items


def queue_provider() -> Callable[[Any], list[Any]] | None:
    """The captain queue's ``pull_requests`` provider, or ``None`` while ``[watch] github``
    is off: the approved pull requests of a project's repository, waiting on the owner."""
    from aisquare.core.config import load_config

    try:
        if not load_config().watch.github:
            return None
    except Exception:
        return None

    def provide(project: Any) -> list[Any]:
        from aisquare.services.captain.queue import PullRequest

        repo = repo_of(Path(project.root))
        if repo is None:
            return []
        cache = Cache.load()
        try:
            states = open_pull_requests(repo, cache)
        except GhError:
            return []
        finally:
            cache.save()
        return [PullRequest(s.number, s.title, s.url) for s in states if s.approved]

    return provide
