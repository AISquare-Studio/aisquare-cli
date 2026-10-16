"""``python -m e2e.report.publish <run_dir> --repo OWNER/NAME [...]``: show a run to people.

**Dry run unless told otherwise.** Without ``--push``, ``--create-issue`` or
``--comment-issue N`` it prints, step by step, exactly what it would do and
touches nothing: no clone, no network, no file written.

What it does when told:

1. Rebuilds ``report/`` (:mod:`e2e.report.build`), so the gallery it pushes is
   this builder's and ``summary.md``'s ``<img>`` tags point under
   ``https://raw.githubusercontent.com/<repo>/<branch>/runs/<run_id>``.
2. ``--push``: copies ``shots/``, ``report/gallery.html``, ``run.json`` and
   ``video/`` into ``runs/<run_id>/`` on ``--branch`` (default ``e2e-artifacts``)
   of ``--repo``, from a throwaway clone in a temporary directory, as
   ``QA Bot <e2e@example.invalid>``, and pushes. The branch is an ORPHAN, sharing
   no history with the code. It is created (with a README and a marker file) when
   missing; an existing branch without the marker is refused, so ``--branch main``
   cannot put screenshots on a code branch. The clone is sparse and blob-less, so
   publishing night 300 does not download nights 1 to 299.
3. Writes ``report/issue-body.md``: the ``--preamble`` file, then the summary.
   ``--create-issue`` posts it as a new issue (``gh issue create``);
   ``--comment-issue N`` as a comment on issue N. The URL is printed last.

Refusals, before anything happens. Exit 2: a ``--repo`` (or remote) owned by
``AISquare-Studio`` without ``--allow-org`` (the org's public repositories are
not a scratchpad, and posting there is a person's call); a run.json the report
cannot trust; a ``runs/<run_id>/`` already on the branch with other content (a
run id must be unique per leg, or legs overwrite each other's evidence). Exit 3:
a credential-shaped string in the run (:mod:`e2e.report.secret_scan`), because
everything published here is public. Exit 1: git or gh failed.

The caller's own checkout is never touched: every git command runs with ``-C`` on
the temporary clone, with ``GIT_DIR`` and friends removed from its environment
and hooks switched off. Against ``https://github.com/`` it authenticates with
the gh CLI's token only (``credential.helper=!gh auth git-credential``, after an
empty value that drops any helper the machine configured).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

from e2e.report import build, markdown, secret_scan
from e2e.report.facts import Facts, Links, env_line, headline, of
from e2e.report.gallery import human_bytes

PROTECTED_OWNER = "aisquare-studio"
"""Compared case-insensitively: GitHub owners are, so ``aisquare-studio/x`` is the org too."""
DEFAULT_BRANCH = "e2e-artifacts"
MARKER = ".e2e-artifacts"
AUTHOR = ("QA Bot", "e2e@example.invalid")
VIDEO_LIMIT = 50 * 1024 * 1024
"""GitHub rejects files over 100 MB; a video past half that is left out, and said so."""

_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_ORG_REMOTE = re.compile(r"github\.com[:/]+aisquare-studio/", re.IGNORECASE)
# Variables that would point git at a repository other than the temporary clone.
_GIT_LOCATION = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)

README = """# e2e-artifacts

Screenshots, galleries, run.json and videos from aisquare's end-to-end runs, one
directory per run under `runs/<run_id>/`. Issues and pull-request comments show
the pictures from here, so do not rewrite this branch while they are wanted.

This branch shares no history with the code. `e2e/report/publish.py` writes it,
and only adds to a branch that carries the `.e2e-artifacts` marker file.
"""


class PublishError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Item:
    source: Path
    dest: str
    """Where it goes on the branch, ``runs/<run_id>/shots/J01/….png``."""
    size: int


@dataclass(frozen=True)
class Plan:
    repo: str
    remote: str
    branch: str
    run_id: str
    facts: Facts
    items: tuple[Item, ...]
    left_out: tuple[str, ...]
    image_base: str
    title: str
    body: str
    run_dir: Path
    scanned: int

    @property
    def github(self) -> bool:
        return self.remote.startswith("https://github.com/")


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    env = dict(os.environ if environ is None else environ)
    args = _parser().parse_args(argv)
    try:
        return _publish(args, env, stdout)
    except PublishError as exc:
        print(f"e2e.report.publish: {exc}", file=stderr)
        return exc.code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m e2e.report.publish",
        description="Publish a run's screenshots to an orphan branch and post its summary. "
        "Dry run unless --push / --create-issue / --comment-issue is given.",
    )
    parser.add_argument("run_dir", type=Path, help="a run directory with run.json and report/")
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="orphan branch for the files")
    parser.add_argument("--issue-title", metavar="TITLE")
    parser.add_argument(
        "--preamble",
        type=Path,
        metavar="FILE",
        help="markdown placed before the summary in the issue body",
    )
    parser.add_argument("--push", action="store_true", help="commit and push the files")
    posting = parser.add_mutually_exclusive_group()
    posting.add_argument("--create-issue", action="store_true", help="post a new issue")
    posting.add_argument("--comment-issue", type=int, metavar="N", help="comment on issue N")
    parser.add_argument(
        "--allow-org", action="store_true", help="allow a repository owned by AISquare-Studio"
    )
    parser.add_argument(
        "--remote-url",
        metavar="URL",
        help="clone and push here instead of https://github.com/OWNER/NAME.git (tests, mirrors)",
    )
    return parser


def _publish(args: argparse.Namespace, env: dict[str, str], out: TextIO) -> int:
    plan = _plan(args, env)
    dry = not (args.push or args.create_issue or args.comment_issue is not None)
    _print_plan(plan, args, out, dry=dry)
    if dry:
        print(
            "\nDRY RUN: nothing was cloned, committed, pushed, written or posted. "
            "Add --push and --create-issue (or --comment-issue N) to do it.",
            file=out,
        )
        return 0
    # Rebuilt here, not trusted from disk: what gets pushed is this builder's gallery, and
    # summary.md gains the image base. Deterministic, so an unchanged run rebuilds the same.
    built = build.build(plan.run_dir, image_base=plan.image_base, links=Links.from_env(env))
    print(f"rebuilt {built.report_dir} (summary.md with the image base)", file=out)
    if args.push:
        sha = _push(plan, env, out)
        print(f"pushed {sha[:12]} to {plan.branch}: {plan.image_base}/", file=out)
    else:
        print(
            f"not pushing; the pictures show only if runs/{plan.run_id}/ is already on "
            f"{plan.branch}",
            file=out,
        )
    body_file = built.report_dir / "issue-body.md"
    body_file.write_text(plan.body, encoding="utf-8")
    print(f"wrote {body_file}", file=out)
    if args.create_issue or args.comment_issue is not None:
        url = _post(plan, body_file, args, env)
        print(f"issue: {url}", file=out)
    return 0


# -- the plan --------------------------------------------------------------------------


def _plan(args: argparse.Namespace, env: Mapping[str, str]) -> Plan:
    repo = str(args.repo)
    if not _REPO.match(repo):
        raise PublishError(f"--repo {repo!r} is not OWNER/NAME", 2)
    remote = str(args.remote_url or f"https://github.com/{repo}.git")
    owner_is_org = repo.split("/", 1)[0].lower() == PROTECTED_OWNER
    if (owner_is_org or _ORG_REMOTE.search(remote)) and not args.allow_org:
        raise PublishError(
            f"refusing to publish to {repo} ({_redact(remote)}): it belongs to AISquare-Studio. "
            "Posting there is a person's call; pass --allow-org once they have made it.",
            2,
        )
    branch = str(args.branch)
    if not _BRANCH.match(branch) or ".." in branch or branch.endswith((".lock", "/")):
        raise PublishError(f"--branch {branch!r} is not a usable branch name", 2)
    if args.comment_issue is not None and args.comment_issue <= 0:
        raise PublishError("--comment-issue takes an issue number", 2)
    preamble = ""
    if args.preamble is not None:
        try:
            preamble = Path(args.preamble).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PublishError(f"--preamble {args.preamble}: {exc}", 2) from exc

    run_dir = Path(args.run_dir)
    try:
        facts = of(build.read_run(run_dir))
    except build.InvalidRun as exc:
        raise PublishError(str(exc), 2) from exc
    run_id = facts.run.run_id
    if not _RUN_ID.match(run_id):
        raise PublishError(f"run id {run_id!r} cannot name a directory on the branch", 2)

    items, left_out = _items(run_dir, run_id)
    hits = secret_scan.scan(run_dir)
    if hits:
        listed = "\n  ".join(str(hit) for hit in hits[:20])
        raise PublishError(
            f"refusing to publish: {len(hits)} credential-shaped string(s) in the run "
            f"(file:offset: rule; the values are not printed)\n  {listed}",
            3,
        )
    image_base = f"https://raw.githubusercontent.com/{repo}/{quote(branch, safe='/')}/runs/{run_id}"
    links = Links.from_env(env)
    budget = markdown.BUDGET - (markdown.size(preamble) + 2 if preamble else 0)
    summary = markdown.summary(facts, image_base=image_base, links=links, budget=budget)
    body = f"{preamble}\n\n{summary}" if preamble else summary
    env_ = facts.run.env
    title = args.issue_title or (
        f"E2E {facts.verdict}: {headline(facts)} · {env_.track} · {env_.os} · run {run_id}"
    )
    return Plan(
        repo=repo,
        remote=remote,
        branch=branch,
        run_id=run_id,
        facts=facts,
        items=items,
        left_out=left_out,
        image_base=image_base,
        title=str(title)[:200],
        body=body,
        run_dir=run_dir,
        scanned=len(secret_scan.scanned_files(run_dir)),
    )


def _items(run_dir: Path, run_id: str) -> tuple[tuple[Item, ...], tuple[str, ...]]:
    gallery = run_dir / "report" / "gallery.html"  # (re)built in step 1 before any copy
    sources = [run_dir / "run.json", gallery]
    for folder in ("shots", "video"):
        root = run_dir / folder
        if root.is_dir():
            sources.extend(sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()))
    items: list[Item] = []
    left_out: list[str] = []
    for source in sources:
        relative = source.relative_to(run_dir).as_posix()
        size = source.stat().st_size if source.is_file() else 0
        if relative.startswith("video/") and size > VIDEO_LIMIT:
            left_out.append(f"{relative} ({human_bytes(size)}: over {human_bytes(VIDEO_LIMIT)})")
            continue
        items.append(Item(source, f"runs/{run_id}/{relative}", size))
    return tuple(items), tuple(left_out)


def _print_plan(plan: Plan, args: argparse.Namespace, out: TextIO, *, dry: bool) -> None:
    facts = plan.facts
    site = "<tmp>/site"
    git = "git -C " + site
    total = sum(item.size for item in plan.items)
    auth = " (pushes with gh auth git-credential)" if plan.github else ""
    lines = [
        f"e2e publish: run {plan.run_id} · {facts.verdict} · {len(facts.journeys)} journeys, "
        f"{len(facts.watch)} watch items",
        f"  repository   {plan.repo}",
        f"  remote       {_redact(plan.remote)}{auth}",
        f"  branch       {plan.branch} (orphan; created with README.md and {MARKER} if missing;",
        f"               an existing branch without {MARKER} is refused)",
        f"  image base   {plan.image_base}",
        f"  secret scan  {plan.scanned} files clean",
        "",
        "plan:",
        "  1. build    "
        + shlex.join(
            [
                "python",
                "-m",
                "e2e.report.build",
                str(plan.run_dir),
                "--image-base",
                plan.image_base,
            ]
        )
        + ("   [skipped: dry run]" if dry else ""),
        "               (a fresh gallery.html to push; summary.md with the image base)",
        "  2. look     "
        + shlex.join(
            ["git", "ls-remote", "--exit-code", "--heads", _redact(plan.remote), _ref(plan)]
        ),
        "  3. clone    if the branch exists:",
        "                "
        + shlex.join(
            [
                "git",
                "clone",
                "--quiet",
                "--sparse",
                "--filter=blob:none",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                plan.branch,
                _redact(plan.remote),
            ]
        )
        + f" {site}",
        f"                {git} sparse-checkout add runs/{plan.run_id}",
        "              else (a new orphan branch):",
        f"                git init --quiet --initial-branch {shlex.quote(plan.branch)} {site}"
        f"   + README.md, {MARKER}",
        f"  4. copy     {len(plan.items)} files, {human_bytes(total)} -> runs/{plan.run_id}/",
    ]
    shown = plan.items if len(plan.items) <= 30 else plan.items[:30]
    lines.extend(
        f"               {item.dest}  {human_bytes(item.size) if item.size else 'built in step 1'}"
        for item in shown
    )
    if len(plan.items) > len(shown):
        lines.append(f"               … and {len(plan.items) - len(shown)} more")
    lines.extend(f"               left out: {entry}" for entry in plan.left_out)
    message = _commit_message(plan)
    lines += [
        f"  5. commit   {git} commit -m {shlex.quote(message)}",
        f"               as {AUTHOR[0]} <{AUTHOR[1]}>; hooks off, no signing",
        f"  6. push     {git} push origin HEAD:{_ref(plan)}"
        + ("" if args.push else "   [skipped: no --push]"),
        f"  7. write    {plan.run_dir / 'report' / 'issue-body.md'} "
        f"({markdown.size(plan.body):,} chars"
        + (f"; --preamble {args.preamble} first" if args.preamble else "")
        + ")"
        + ("   [skipped: dry run]" if dry else ""),
    ]
    body_file = str(plan.run_dir / "report" / "issue-body.md")
    if args.comment_issue is not None:
        post = ["gh", "issue", "comment", str(args.comment_issue)]
    else:
        post = ["gh", "issue", "create"]
    post += ["--repo", plan.repo]
    if args.comment_issue is None:
        post += ["--title", plan.title]
    post += ["--body-file", body_file]
    posting = args.create_issue or args.comment_issue is not None
    lines.append(
        "  8. post     "
        + shlex.join(post)
        + ("" if posting else "   [skipped: no --create-issue / --comment-issue]")
    )
    print("\n".join(lines), file=out)


def _commit_message(plan: Plan) -> str:
    return f"e2e: run {plan.run_id} {plan.facts.verdict} ({env_line(plan.facts.run)})"


def _ref(plan: Plan) -> str:
    return f"refs/heads/{plan.branch}"


def _redact(url: str) -> str:
    """A remote as it may be printed: never a credential embedded in it."""
    return re.sub(r"//[^/@]+@", "//***@", url)


# -- git ------------------------------------------------------------------------------


def _git(plan: Plan, *args: str, repo: Path | None = None) -> list[str]:
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
    if plan.github:
        # The gh CLI's token and nothing else: the empty value clears any helper the
        # machine configured, so a keychain cannot answer for a different account.
        command += ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    if repo is not None:
        command += ["-C", str(repo)]
    return [*command, *args]


def _git_env(env: Mapping[str, str]) -> dict[str, str]:
    clean = {key: value for key, value in env.items() if key not in _GIT_LOCATION}
    clean["GIT_TERMINAL_PROMPT"] = "0"  # a missing credential fails; it never waits on a prompt
    clean.update(
        GIT_AUTHOR_NAME=AUTHOR[0],
        GIT_AUTHOR_EMAIL=AUTHOR[1],
        GIT_COMMITTER_NAME=AUTHOR[0],
        GIT_COMMITTER_EMAIL=AUTHOR[1],
    )
    return clean


def _run(
    command: list[str], env: Mapping[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, env=dict(env), capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise PublishError(f"{command[0]} is not installed: {exc}") from exc
    if check and result.returncode != 0:
        shown = shlex.join(_redact(part) for part in command)
        raise PublishError(f"{shown} exited {result.returncode}: {result.stderr.strip()[-800:]}")
    return result


def _push(plan: Plan, env: Mapping[str, str], out: TextIO) -> str:
    """Commit the run onto the branch and push; start over if another leg pushed first."""
    git_env = _git_env(env)
    for attempt in range(1, 4):
        with tempfile.TemporaryDirectory(prefix="e2e-publish-") as temp:
            site = Path(temp) / "site"
            sha, changed = _commit(plan, site, git_env)
            if not changed:
                print(f"runs/{plan.run_id}/ is already on {plan.branch}, unchanged", file=out)
                return sha
            push = ["push", "--quiet", "--no-verify", "origin", f"HEAD:{_ref(plan)}"]
            result = _run(_git(plan, *push, repo=site), git_env, check=False)
            if result.returncode == 0:
                return sha
            if "[rejected]" in result.stderr and attempt < 3:
                print("the branch moved while publishing; starting again from it", file=out)
                continue
            raise PublishError(
                f"git push exited {result.returncode}: {result.stderr.strip()[-800:]}"
            )
    raise PublishError("the branch kept moving; gave up after 3 attempts")


def _commit(plan: Plan, site: Path, env: Mapping[str, str]) -> tuple[str, bool]:
    look = _git(plan, "ls-remote", "--exit-code", "--heads", plan.remote, _ref(plan))
    found = _run(look, env, check=False)
    if found.returncode not in (0, 2):  # 2: the remote answered, without that branch
        raise PublishError(f"cannot read {_redact(plan.remote)}: {found.stderr.strip()[-800:]}")
    added = [f"runs/{plan.run_id}"]
    if found.returncode == 0:
        clone = [
            "clone",
            "--quiet",
            "--sparse",
            "--filter=blob:none",
            "--depth",
            "1",
            "--single-branch",
            "--branch",
            plan.branch,
            plan.remote,
            str(site),
        ]
        _run(_git(plan, *clone), env)
        if not (site / MARKER).is_file():
            raise PublishError(
                f"branch {plan.branch} of {plan.repo} exists but has no {MARKER}: it is not "
                "an e2e artifacts branch, and nothing is added to it",
                2,
            )
        _run(_git(plan, "sparse-checkout", "add", f"runs/{plan.run_id}", repo=site), env)
    else:
        _run(_git(plan, "init", "--quiet", "--initial-branch", plan.branch, str(site)), env)
        _run(_git(plan, "remote", "add", "origin", plan.remote, repo=site), env)
        (site / "README.md").write_text(README, encoding="utf-8")
        (site / MARKER).write_text(
            "Published E2E artifacts: e2e/report/publish.py adds only to branches with this.\n",
            encoding="utf-8",
        )
        added += ["README.md", MARKER]

    target = site / "runs" / plan.run_id
    if target.exists():
        if _same(target, plan):
            head = _run(_git(plan, "rev-parse", "HEAD", repo=site), env).stdout.strip()
            return head, False
        raise PublishError(
            f"runs/{plan.run_id}/ is already on {plan.branch} with different content; a run id "
            "must be unique per leg (and per night), or runs overwrite each other's evidence",
            2,
        )
    for item in plan.items:
        destination = site / item.dest
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item.source, destination)
    _run(_git(plan, "add", "--", *added, repo=site), env)
    commit = ["commit", "--quiet", "--no-verify", "-m", _commit_message(plan)]
    _run(_git(plan, *commit, repo=site), env)
    return _run(_git(plan, "rev-parse", "HEAD", repo=site), env).stdout.strip(), True


def _same(target: Path, plan: Plan) -> bool:
    """Whether the branch already holds exactly these files, byte for byte."""
    prefix = f"runs/{plan.run_id}/"
    present = {p.relative_to(target).as_posix(): p for p in target.rglob("*") if p.is_file()}
    wanted = {item.dest.removeprefix(prefix): item.source for item in plan.items}
    if present.keys() != wanted.keys():
        return False
    return all(present[name].read_bytes() == wanted[name].read_bytes() for name in wanted)


# -- gh -------------------------------------------------------------------------------


def _post(plan: Plan, body_file: Path, args: argparse.Namespace, env: Mapping[str, str]) -> str:
    gh_env = {**env, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    if args.comment_issue is not None:
        command = ["gh", "issue", "comment", str(args.comment_issue), "--repo", plan.repo]
    else:
        command = ["gh", "issue", "create", "--repo", plan.repo, "--title", plan.title]
    result = _run([*command, "--body-file", str(body_file)], gh_env)
    urls = [line for line in result.stdout.split() if line.startswith("https://")]
    if not urls:
        raise PublishError(f"gh did not print a URL: {result.stdout.strip()[-400:]!r}")
    return urls[-1]


if __name__ == "__main__":
    raise SystemExit(main())
