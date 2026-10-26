"""P2-W2 (#284): pull requests from GitHub, for ``aisquare watch`` and the captain's queue.

Every request is ``gh api`` with the last answer's ETag as ``If-None-Match``. A ``304`` costs
no rate limit and makes no further call, and an approval is one ``pr_approved``. The fake
``gh`` below plays GitHub: it answers by path, keeps an ETag per answer, and says 304 when
the request carries it. Never the network.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.config import load_config, save_config
from aisquare.models import ProjectInfo
from aisquare.services import github_prs
from aisquare.services.captain import queue

REPO = "acme/app"
HEAD = "abc123"


class FakeGitHub:
    """``gh api -i`` over a dict of path → JSON, with ETags and 304s."""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.calls: list[list[str]] = []

    def set(self, path: str, body: Any) -> None:
        self.answers[path] = body

    def etag(self, path: str) -> str:
        return f'"{abs(hash(json.dumps(self.answers[path], sort_keys=True)))}"'

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        path = args[1]
        if path not in self.answers:
            return subprocess.CompletedProcess(args, 1, "HTTP/2.0 404 Not Found\n\n{}", "")
        etag = self.etag(path)
        sent = [a for a in args if a.startswith("If-None-Match: ")]
        if sent and sent[0] == f"If-None-Match: {etag}":
            return subprocess.CompletedProcess(args, 0, "HTTP/2.0 304 Not Modified\n\n", "")
        body = json.dumps(self.answers[path])
        return subprocess.CompletedProcess(
            args, 0, f"HTTP/2.0 200 OK\nEtag: {etag}\nContent-Type: json\n\n{body}", ""
        )


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github_prs, "_gh", fake)
    return fake


def _pr(updated: str = "2026-10-24T10:00:00Z") -> dict[str, Any]:
    return {
        "number": 12,
        "title": "wire auth",
        "html_url": f"https://github.com/{REPO}/pull/12",
        "head": {"sha": HEAD},
        "updated_at": updated,
    }


def _github(gh: FakeGitHub, *, reviews: list[dict[str, Any]], runs: list[dict[str, Any]]) -> None:
    gh.set(f"repos/{REPO}/pulls?state=open&per_page=100", [_pr()])
    gh.set(f"repos/{REPO}/pulls/12/reviews?per_page=100", reviews)
    gh.set(f"repos/{REPO}/commits/{HEAD}/check-runs?per_page=100", {"check_runs": runs})


APPROVED = [{"id": 7, "state": "APPROVED", "user": {"login": "reviewer-a"}}]
DONE = [{"status": "completed", "conclusion": "success"}]


def test_an_approval_is_one_pr_approved_and_a_304_makes_no_further_call(gh: FakeGitHub) -> None:
    _github(gh, reviews=APPROVED, runs=DONE)
    cache = github_prs.Cache()

    [state] = github_prs.open_pull_requests(REPO, cache)
    kinds = [item.kind for item in github_prs.pr_items(state)]
    assert kinds == ["pr_approved", "pr_checks_done"]
    assert len(gh.calls) == 3, "the list, the reviews, the checks"
    assert not any("If-None-Match" in " ".join(call) for call in gh.calls), "nothing known yet"

    gh.calls.clear()
    [again] = github_prs.open_pull_requests(REPO, cache)
    assert [call[1] for call in gh.calls] == [f"repos/{REPO}/pulls?state=open&per_page=100"]
    assert any(arg.startswith("If-None-Match: ") for arg in gh.calls[0])
    assert cache.not_modified == 1 and again == state, "a 304, and no further call"


def test_a_new_review_without_a_new_head_is_read(gh: FakeGitHub) -> None:
    """An approval moves the pull request's ``updated_at``, not its head: the reviews are read
    again on that, or an approval after the last push was never seen."""
    _github(gh, reviews=[], runs=DONE)
    cache = github_prs.Cache()
    [before] = github_prs.open_pull_requests(REPO, cache)
    assert not before.approved
    gh.set(f"repos/{REPO}/pulls?state=open&per_page=100", [_pr("2026-10-24T11:00:00Z")])
    gh.set(f"repos/{REPO}/pulls/12/reviews?per_page=100", APPROVED)
    [after] = github_prs.open_pull_requests(REPO, cache)
    assert after.approved and after.head == before.head


def test_a_change_request_after_an_approval_is_what_counts(gh: FakeGitHub) -> None:
    reviews = [
        *APPROVED,
        {"id": 8, "state": "COMMENTED", "user": {"login": "reviewer-a"}},
        {"id": 9, "state": "CHANGES_REQUESTED", "user": {"login": "reviewer-b"}},
    ]
    _github(gh, reviews=reviews, runs=[{"status": "in_progress", "conclusion": None}])
    [state] = github_prs.open_pull_requests(REPO, github_prs.Cache())
    assert [item.kind for item in github_prs.pr_items(state)] == ["pr_changes_requested"]


def test_the_etags_survive_a_restart(gh: FakeGitHub) -> None:
    _github(gh, reviews=APPROVED, runs=DONE)
    first = github_prs.Cache.load()
    github_prs.open_pull_requests(REPO, first)
    first.save()
    gh.calls.clear()
    github_prs.open_pull_requests(REPO, github_prs.Cache.load())
    assert len(gh.calls) == 1 and "If-None-Match" in " ".join(gh.calls[0])


def test_the_captain_queue_lists_the_approved_pull_request(
    gh: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[watch] github = true`` plugs the provider into the queue's default sources (the
    captain's Phase 2 ``gh`` provider); off, the queue never leaves the machine."""
    _github(gh, reviews=APPROVED, runs=DONE)
    monkeypatch.setattr(github_prs, "repo_of", lambda root: REPO)
    project = ProjectInfo(id="prj_app", root=tmp_path)
    assert queue._default_sources().pull_requests(project) == [], "off by default"
    config = load_config()
    config.watch.github = True
    save_config(config)
    assert queue._default_sources().pull_requests(project) == [
        queue.PullRequest(12, "wire auth", f"https://github.com/{REPO}/pull/12")
    ]


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("https://github.com/acme/app.git", "acme/app"),
        ("git@github.com:acme/app.git", "acme/app"),
        ("ssh://git@github.com/acme/app", "acme/app"),
        ("https://gitlab.com/acme/app.git", None),
    ],
)
def test_the_repository_is_read_from_origin(tmp_path: Path, url: str, repo: str | None) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url], check=True)
    assert github_prs.repo_of(tmp_path) == repo
