"""Put one CI run in front of a reviewer: screenshots on a per-run ref, one sticky PR comment.

    python -m e2e.report.ci_publish RUN_DIR --repo OWNER/NAME [--pr N | --issue N]
                                    [--summary-file PATH] [--dry-run]

What it does, in order:

1. **Scan** the run for credentials (e2e.report.secret_scan) and refuse on a hit — a
   second, independent check after the workflow's own, because what follows is public.
2. **Evidence → ``refs/e2e/runs/<run-id>``**: an orphan commit holding only this run's
   ``shots/`` and ``run.json``. Deliberately NOT a branch: default clones and fetches take
   ``refs/heads/*`` and ``refs/tags/*`` only, so screenshots never reach a developer's
   machine, and no two runs write the same ref, so concurrent runs cannot race. GitHub
   keeps a commit while a ref names it and serves its files by SHA —
   ``https://raw.githubusercontent.com/OWNER/NAME/<sha>/shots/…`` — so the links in a
   comment are immutable (measured on 2026-10-16: a custom-ref commit's PNG served 200).
3. **summary.md** rebuilt with that SHA-pinned image base (e2e.report.build).
4. **One sticky comment** per PR (or tracking issue) and track: found by a hidden marker
   and edited in place, so a PR shows its latest run rather than a pile of comments. A
   comment is only ever edited if a Bot wrote it — a human quoting the marker is left alone.
5. ``--summary-file``: the same summary, images included, appended to the job summary.

Why not ``gh … --attach`` (images as user-attachments, no git at all): GitHub's docs say it
authenticates with a user's OAuth token or a classic PAT, not the workflow's GITHUB_TOKEN.
This needs nothing but the GITHUB_TOKEN with ``contents: write`` (the ref) and
``pull-requests: write`` (the comment). The token is read from GH_TOKEN or GITHUB_TOKEN,
handed to git through a credential helper that reads the environment (never argv), and
never printed.

Stdlib only, like the rest of e2e/report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from e2e.contracts import load
from e2e.report import build, secret_scan
from e2e.report.facts import Links

STICKY = "<!-- aisquare-e2e-sticky track={track} -->"
"""First line of every comment this posts: how the next run finds the comment to edit."""

REF_PREFIX = "refs/e2e/runs/"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")
_QA = ("QA Bot", "e2e@example.invalid")

#: Reads the token from the environment at push time, so it is never on a command line.
_CREDENTIAL_HELPER = (
    '!f() { echo username=x-access-token; echo "password=${GH_TOKEN:-$GITHUB_TOKEN}"; }; f'
)


class PublishError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Target:
    kind: str
    """``pr`` or ``issue`` — both are issues to the comments API."""
    number: int


def sticky_marker(track: str) -> str:
    return STICKY.format(track=track)


def with_marker(body: str, track: str) -> str:
    """The comment body: the sticky marker first, then the summary."""
    return f"{sticky_marker(track)}\n{body}"


def find_sticky(comments: Sequence[Mapping[str, Any]], track: str) -> int | None:
    """The id of the newest Bot comment carrying this track's marker; ``None`` if there is none."""
    marker = sticky_marker(track)
    found: int | None = None
    for comment in comments:
        body = str(comment.get("body") or "")
        user = comment.get("user") or {}
        if body.startswith(marker) and isinstance(user, Mapping) and user.get("type") == "Bot":
            found = int(comment["id"])
    return found


def ref_for(run_id: str) -> str:
    if not _RUN_ID.match(run_id):
        raise PublishError(f"run id {run_id!r} is not safe to use in a git ref", code=2)
    return REF_PREFIX + run_id


def raw_base(repo: str, sha: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{sha}"


# --- the GitHub REST API (comments) -------------------------------------------------------


class GitHub:
    def __init__(self, repo: str, token: str, api: str = "https://api.github.com") -> None:
        self.repo = repo
        self._token = token
        self._api = api.rstrip("/")

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(f"{self._api}{path}", data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # The status and GitHub's message only: a request never echoes the token.
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise PublishError(f"GitHub API {method} {path}: HTTP {exc.code} {detail}") from None
        return json.loads(text) if text else None

    def comments(self, number: int) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        page = 1
        while True:
            batch = self._call(
                "GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}"
            )
            if not batch:
                return out
            out.extend(batch)
            page += 1

    def upsert(self, number: int, body: str, track: str) -> str:
        existing = find_sticky(self.comments(number), track)
        if existing is None:
            made = self._call(
                "POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body}
            )
        else:
            made = self._call(
                "PATCH", f"/repos/{self.repo}/issues/comments/{existing}", {"body": body}
            )
        return str(made.get("html_url", "")) if isinstance(made, Mapping) else ""


# --- the evidence ref ---------------------------------------------------------------------


def _git(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> str:
    clean = {k: v for k, v in env.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=clean, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise PublishError(f"git {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def push_evidence(run_dir: Path, repo: str, ref: str, env: Mapping[str, str]) -> str:
    """Commit this run's shots/ and run.json as an orphan commit, push it to ``ref``; its SHA."""
    name, email = _BOT if env.get("GITHUB_ACTIONS") == "true" else _QA
    # Overridable for the self-tests, which push to a local bare repository.
    remote = env.get("E2E_PUBLISH_REMOTE") or f"https://github.com/{repo}.git"
    with tempfile.TemporaryDirectory(prefix="e2e-evidence-") as tmp:
        site = Path(tmp)
        shutil.copytree(run_dir / "shots", site / "shots")
        shutil.copy2(run_dir / "run.json", site / "run.json")
        identity = [
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
        ]
        _git(["init", "-q"], site, env)
        _git([*identity, "add", "-A"], site, env)
        _git(
            [*identity, "commit", "-q", "-m", f"e2e evidence: {ref.removeprefix(REF_PREFIX)}"],
            site,
            env,
        )
        sha = _git(["rev-parse", "HEAD"], site, env)
        _git(
            [
                "-c",
                "credential.helper=",
                "-c",
                f"credential.helper={_CREDENTIAL_HELPER}",
                "push",
                "-q",
                remote,
                f"HEAD:{ref}",
            ],
            site,
            env,
        )
    return sha


# --- the command --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m e2e.report.ci_publish", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--repo", required=True, help="OWNER/NAME (the workflow's GITHUB_REPOSITORY)"
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--pr", type=int, help="pull request to post/update the sticky comment on")
    target.add_argument("--issue", type=int, help="tracking issue (nightly) to post/update on")
    parser.add_argument(
        "--summary-file", type=Path, help="append the summary here ($GITHUB_STEP_SUMMARY)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan; push and post nothing"
    )
    return parser


def run(argv: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    args = _parser().parse_args(argv)
    run_dir: Path = args.run_dir
    if not _REPO.match(args.repo):
        raise PublishError(f"--repo {args.repo!r} is not OWNER/NAME", code=2)
    if not (run_dir / "run.json").is_file() or not (run_dir / "shots").is_dir():
        raise PublishError(f"{run_dir} has no run.json or no shots/ — nothing to publish", code=2)
    record = load(run_dir / "run.json")
    track = record.env.track
    ref = ref_for(record.run_id)
    target = (
        Target("pr", args.pr) if args.pr else Target("issue", args.issue) if args.issue else None
    )
    hits = secret_scan.scan(run_dir)
    if hits:
        for hit in hits:
            print(f"ci_publish: {hit}", file=out)
        raise PublishError(
            f"{len(hits)} credential-shaped string(s) in the run — not publishing", code=3
        )
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
    print(f"e2e ci_publish: run {record.run_id} · {record.verdict} · track {track}", file=out)
    print(f"  evidence   {ref} on {args.repo} (shots/ + run.json, orphan commit)", file=out)
    target_text = f"{target.kind} #{target.number}" if target else "none (job summary only)"
    print(f"  comment    {target_text} · sticky marker: {sticky_marker(track)}", file=out)
    if args.dry_run:
        print("  dry run: nothing pushed, nothing posted", file=out)
        return 0
    if not token:
        raise PublishError("no GH_TOKEN / GITHUB_TOKEN in the environment", code=2)
    sha = push_evidence(run_dir, args.repo, ref, env)
    base = raw_base(args.repo, sha)
    print(f"  pushed     {sha} → images at {base}/shots/…", file=out)
    summary = build.write_summary(run_dir, image_base=base, links=Links.from_env(env))
    body = with_marker(summary.read_text(encoding="utf-8"), track)
    if args.summary_file:
        with args.summary_file.open("a", encoding="utf-8") as handle:
            handle.write(body + "\n")
        print(f"  summary    appended to {args.summary_file}", file=out)
    if target:
        api = env.get("GITHUB_API_URL") or "https://api.github.com"
        url = GitHub(args.repo, token, api).upsert(target.number, body, track)
        print(f"  comment    {url}", file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(list(sys.argv[1:] if argv is None else argv), os.environ, sys.stdout)
    except PublishError as exc:
        print(f"ci_publish: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
