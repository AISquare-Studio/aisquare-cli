"""Turn snapshots: the exact tree a prompt was submitted against, kept alive for replay."""

from __future__ import annotations

import ast
import inspect
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.spawn import TRACING_ENV_VARS
from aisquare.services import ci_snapshot
from aisquare.services.ci_contract import HookRequest
from tests.ci_support import git, repo, request

TRACE = "trc_01j9q8p3k7zr4m2n6v0c1d8e5f"


#: The product's budget, as written: the bound test runs on it, not on the suite's.
PRODUCT_BUDGET = ci_snapshot.new_budget

#: Each git call's own ceiling in the suite. The product's 2 s per call was not enough
#: either: one ``git stash create`` on a loaded Windows runner took longer (rc/p1 push
#: run 40074661474, attempt 2), and the capture failed open as designed.
SUITE_GIT_SECONDS = 30.0


@pytest.fixture(autouse=True)
def unhurried_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turn's budget on a clock that does not move, for every ``capture`` and
    ``project_ref`` here that asks for none of its own.

    ``GIT_BUDGET_SECONDS`` is a product bound: two seconds in front of a developer who
    has just hit enter, past which the hook fails open. A loaded Windows runner spent it
    across one capture's four git spawns, and the test read the product's fail-open as a
    failure of its own (CI of #259, run 38482592483, attempt 1). The budget is judged by
    the clock it is given, and this one does not run: every git call gets
    :data:`SUITE_GIT_SECONDS` as its own ceiling, and nothing accumulates across calls.
    The product's bound is pinned on the product's budget
    (``test_git_runs_without_the_traced_identity_and_without_optional_locks``), and the
    tests of the budget itself make their own.
    """
    monkeypatch.setattr(
        ci_snapshot,
        "new_budget",
        lambda: ci_snapshot._Budget(SUITE_GIT_SECONDS, clock=lambda: 0.0),
    )


def test_a_clean_tree_snapshots_as_head(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    snapshot = ci_snapshot.capture(root, TRACE)
    assert snapshot is not None
    assert snapshot.object_id == git(root, "rev-parse", "HEAD")
    assert snapshot.dirty is False
    assert snapshot.ref is None


def _inside_retention() -> str:
    """A git-parseable date that is inside the retention window, computed now.

    NOT a literal, and that is the whole point. A ref this test expects to
    SURVIVE pruning has to be younger than :data:`ci_snapshot.WIP_REF_TTL_DAYS`,
    and a literal that was recent on the day it was written stops being recent:
    ``2026-09-02`` sat five days inside a seven-day window when this landed,
    and on 2026-09-09 it was exactly seven days old, so the surviving ref was
    pruned and the suite went red on ``main`` with no code change at all — the
    one failure mode a test about a retention window can have and never report
    honestly.

    The ``old`` fixtures below stay literal on purpose: they only ever need to
    be OUTSIDE the window, and 2026-01-01 always will be.
    """
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _dated_commit(root: Path, when: str) -> str:
    """A commit object with the given author and committer date, on HEAD's tree."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    result = subprocess.run(
        ["git", "-C", str(root), "commit-tree", "HEAD^{tree}", "-m", f"snapshot at {when}"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def test_snapshot_refs_older_than_the_retention_are_pruned_when_a_new_one_is_taken(
    tmp_path: Path,
) -> None:
    """One ref per dirty-tree prompt and nothing ever deleted them: .git grew
    forever and a secret from one turn stayed recoverable behind a ref the
    developer did not know existed."""
    root = repo(tmp_path / "r")
    old = _dated_commit(root, "2026-01-01T00:00:00+0000")
    recent = _dated_commit(root, _inside_retention())
    git(root, "update-ref", ci_snapshot.WIP_REF_PREFIX + "old", old)
    git(root, "update-ref", ci_snapshot.WIP_REF_PREFIX + "recent", recent)
    (root / "tracked.txt").write_text("edited\n", encoding="utf-8")

    snapshot = ci_snapshot.capture(root, "trc_new")

    assert snapshot is not None and snapshot.ref == ci_snapshot.WIP_REF_PREFIX + "new"
    refs = set(git(root, "for-each-ref", "--format=%(refname)", ci_snapshot.WIP_REF_PREFIX).split())
    assert refs == {ci_snapshot.WIP_REF_PREFIX + "recent", ci_snapshot.WIP_REF_PREFIX + "new"}
    assert git(root, "cat-file", "-t", old) == "commit", "pruning drops the ref, not the object"


def test_a_clean_tree_turn_still_prunes_expired_refs(tmp_path: Path) -> None:
    """The retention is a promise about the refs on disk, not about what this
    turn wrote. The prune used to sit in the success arm of the dirty-tree
    ``update-ref``, so a developer who spent a week on dirty trees and then
    worked from clean checkouts never pruned again — and the README promises
    refs older than seven days go "the next time a snapshot is taken"."""
    root = repo(tmp_path / "r")
    old = _dated_commit(root, "2026-01-01T00:00:00+0000")
    git(root, "update-ref", ci_snapshot.WIP_REF_PREFIX + "old", old)

    snapshot = ci_snapshot.capture(root, "trc_clean")  # tree is clean: no stash, no new ref

    assert snapshot is not None and not snapshot.dirty and snapshot.ref is None
    refs = git(root, "for-each-ref", "--format=%(refname)", ci_snapshot.WIP_REF_PREFIX).split()
    assert refs == [], "a clean-tree snapshot must still drop what has expired"


def test_capture_and_project_ref_share_one_budget_when_given_one(tmp_path: Path) -> None:
    """The module docstring promises "every git call shares one small time
    budget". Two separately-constructed budgets made that two allowances, so a
    slow repository could spend twice the stated bound in front of a developer.
    """
    root = repo(tmp_path / "r")
    spent = ci_snapshot._Budget(0.0)
    assert spent.spent()

    # Both accept the turn's budget; an exhausted one is still usable (remaining()
    # floors) but neither may quietly start a second allowance.
    ci_snapshot.capture(root, "trc_shared", spent)
    ci_snapshot.project_ref(root, spent)

    source = inspect.getsource(ci_snapshot)
    assert source.count("_Budget(GIT_BUDGET_SECONDS)") == 1, (
        "only new_budget() may construct the turn's allowance"
    )
    assert "def capture(root: Path, trace_id: str, budget: _Budget | None = None)" in source
    assert "def project_ref(root: Path, budget: _Budget | None = None)" in source


def test_pruning_fails_open_and_reports_what_it_dropped(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    git(
        root,
        "update-ref",
        ci_snapshot.WIP_REF_PREFIX + "old",
        _dated_commit(root, "2026-01-01T00:00:00+0000"),
    )
    not_a_repo = tmp_path / "elsewhere"
    not_a_repo.mkdir()
    assert ci_snapshot._prune(not_a_repo, ci_snapshot._Budget(2.0)) == 0, "no git, no pruning"
    assert ci_snapshot._prune(root, ci_snapshot._Budget(2.0)) == 1
    assert ci_snapshot._prune(root, ci_snapshot._Budget(2.0)) == 0, "nothing old is left"


def test_pruning_a_backlog_stops_at_the_budget_and_finishes_on_later_turns(
    tmp_path: Path,
) -> None:
    """The first prune after an unbounded stretch has thousands of refs to drop,
    one ``update-ref`` spawn each. It used to run them all: 6.6 s of a 2 s
    budget, on the synchronous prompt path. Now it stops at the budget and the
    next turn takes the rest — the work is idempotent, so nothing is lost."""
    root = repo(tmp_path / "r")
    old = _dated_commit(root, "2026-01-01T00:00:00+0000")
    for n in range(40):
        git(root, "update-ref", f"{ci_snapshot.WIP_REF_PREFIX}stale{n}", old)

    spent = ci_snapshot._Budget(0.0)  # already gone before the first deletion
    assert spent.spent(), "a zero budget must read as spent even though remaining() floors"
    assert ci_snapshot._prune(root, spent) == 0, "no ref is dropped once the budget is gone"

    remaining = len(
        git(root, "for-each-ref", "--format=%(refname)", ci_snapshot.WIP_REF_PREFIX).split()
    )
    assert remaining == 40, "and the backlog is still there to drop next turn"
    assert ci_snapshot._prune(root, ci_snapshot._Budget(30.0)) == 40, "a real budget drains it"


def test_a_dirty_tree_becomes_a_stash_object_kept_alive_by_a_ref(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    (root / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    snapshot = ci_snapshot.capture(root, TRACE)
    assert snapshot is not None
    assert snapshot.dirty is True
    assert snapshot.object_id != git(root, "rev-parse", "HEAD")
    assert len(snapshot.object_id) == 40
    assert snapshot.ref == "refs/aisquare/wip/01j9q8p3k7zr4m2n6v0c1d8e5f"
    assert git(root, "rev-parse", snapshot.ref) == snapshot.object_id
    # The object carries the edit, so a replay can rebuild the tree.
    assert "two" in git(root, "show", f"{snapshot.object_id}:tracked.txt")


def test_capturing_leaves_the_developers_tree_and_stash_list_untouched(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    (root / "tracked.txt").write_text("edited\n", encoding="utf-8")
    ci_snapshot.capture(root, TRACE)
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "edited\n"
    assert git(root, "stash", "list") == ""
    assert git(root, "branch", "--list") == "* main"
    assert git(root, "status", "--porcelain").strip() == "M tracked.txt"


def test_untracked_files_are_not_in_the_snapshot_and_the_row_will_say_so(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    (root / "new.txt").write_text("untracked\n", encoding="utf-8")
    snapshot = ci_snapshot.capture(root, TRACE)
    assert snapshot is not None
    assert snapshot.dirty is False, "an untracked file alone is not a dirty tree to git stash"
    assert snapshot.untracked_excluded is True


def test_the_object_id_is_what_travels_and_it_fits_the_contract(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    (root / "tracked.txt").write_text("x\n", encoding="utf-8")
    snapshot = ci_snapshot.capture(root, TRACE)
    assert snapshot is not None
    built = request(snapshot_ref=snapshot.object_id)
    assert built.snapshot_ref == snapshot.object_id
    with pytest.raises(Exception, match="snapshot_ref"):
        HookRequest.model_validate({**built.to_wire(), "snapshot_ref": snapshot.ref})


def test_not_a_repository_is_no_snapshot(tmp_path: Path) -> None:
    assert ci_snapshot.capture(tmp_path, TRACE) is None
    assert ci_snapshot.project_ref(tmp_path) is None


def test_a_missing_git_is_no_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", gone)
    assert ci_snapshot.capture(tmp_path, TRACE) is None


def test_a_hung_git_is_no_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", slow)
    assert ci_snapshot.capture(tmp_path, TRACE) is None


def test_git_runs_without_the_traced_identity_and_without_optional_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child of a traced hook is not the agent, and a hook's read must not
    fight the developer's own git for the index lock."""
    seen: dict[str, Any] = {}
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9190")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Agent-Name: x")

    def record(argv: list[str], **kwargs: Any) -> Any:
        seen.update(argv=argv, env=kwargs["env"], timeout=kwargs["timeout"])
        raise OSError("stop here")

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(ci_snapshot, "new_budget", PRODUCT_BUDGET)
    ci_snapshot.capture(tmp_path, TRACE)
    for name in TRACING_ENV_VARS:
        assert name not in seen["env"]
    assert seen["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert seen["env"]["PATH"] == os.environ["PATH"]
    assert 0 < seen["timeout"] <= ci_snapshot.GIT_BUDGET_SECONDS


def test_project_ref_names_the_repository_and_branch_without_credentials(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    git(
        root,
        "remote",
        "add",
        "origin",
        "https://user:s3cr3t@github.com/AISquare-Studio/aisquare-cli.git",
    )
    ref = ci_snapshot.project_ref(root)
    assert ref == "AISquare-Studio/aisquare-cli@main"
    assert "s3cr3t" not in ref


def test_project_ref_without_a_remote_uses_the_directory_name(tmp_path: Path) -> None:
    root = repo(tmp_path / "my-project")
    assert ci_snapshot.project_ref(root) == "my-project@main"


def test_project_ref_on_a_detached_head_says_so(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    git(root, "checkout", "-q", "--detach")
    assert ci_snapshot.project_ref(root) == "r@detached"


def test_project_ref_is_a_valid_request_field(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    built = request(project_ref=ci_snapshot.project_ref(root))
    assert built.project_ref is not None and len(built.project_ref) <= 500


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:AISquare-Studio/aisquare-cli.git", "AISquare-Studio/aisquare-cli"),
        ("https://github.com/AISquare-Studio/aisquare-cli", "AISquare-Studio/aisquare-cli"),
        ("https://user:token@github.com/o/r.git", "o/r"),
        ("ssh://git@host.example:2222/deep/path/o/r.git", "o/r"),
        ("file:///srv/repos/thing.git", "repos/thing"),
        ("/srv/repos/thing", "repos/thing"),
        ("thing", "thing"),
        ("", None),
        ("https://host/", None),
    ],
)
def test_repo_slug_reads_only_the_path(url: str, slug: str | None) -> None:
    assert ci_snapshot.repo_slug(url) == slug


def test_the_budget_never_hands_git_a_zero_timeout() -> None:
    budget = ci_snapshot._Budget(0)
    assert budget.remaining() >= 0.05


def test_the_budget_is_judged_by_the_clock_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up of #259: the budget read the wall clock itself, so what a capture could
    do depended on how loaded the runner was (``unhurried_git`` says how that failed). It
    runs on the clock it is given, and on the module's otherwise: with that one racing
    three seconds a read, the product's own allowance is spent at its first look, while a
    capture on the suite's frozen one lands."""
    now = 0.0
    budget = ci_snapshot._Budget(2.0, clock=lambda: now)
    assert budget.remaining() == 2.0 and not budget.spent()
    now = 1.5
    assert budget.remaining() == pytest.approx(0.5) and not budget.spent()
    now = 2.0
    assert budget.spent() and budget.remaining() == 0.05, "floored, and spent"

    racing = iter(range(0, 10_000, 3))
    monkeypatch.setattr(ci_snapshot, "_monotonic", lambda: float(next(racing)))
    root = repo(tmp_path / "r")
    (root / "tracked.txt").write_text("x\n", encoding="utf-8")
    assert ci_snapshot.capture(root, "trc_slow") is not None, "the suite's budget: frozen"
    assert ci_snapshot._Budget(ci_snapshot.GIT_BUDGET_SECONDS).spent(), "the product's: racing"


def test_a_trace_id_that_cannot_be_a_ref_still_yields_the_object(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    (root / "tracked.txt").write_text("x\n", encoding="utf-8")
    snapshot = ci_snapshot.capture(root, "trc_has space")
    assert snapshot is not None
    assert snapshot.ref is None
    assert len(snapshot.object_id) == 40


def test_the_snapshots_git_runs_no_hook_of_the_repository(tmp_path: Path) -> None:
    """Review r12 of #113, finding 2: the snapshot's ``stash create`` runs from the agent's
    hooks outside any sandbox, and with ``core.hooksPath`` in the tree (husky's layout) it
    ran the tree's ``post-index-change``, which a sandboxed agent may write."""
    if os.name == "nt":
        pytest.skip("a POSIX hook script")
    root = repo(tmp_path / "r")
    hooks = root / ".husky"
    hooks.mkdir()
    marker = tmp_path / "ran"
    hook = hooks / "post-index-change"
    hook.write_text(f"#!/bin/sh\necho x >> {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    git(root, "config", "core.hooksPath", ".husky")
    (root / "tracked.txt").write_text("edited\n", encoding="utf-8")

    snapshot = ci_snapshot.capture(root, TRACE)

    assert snapshot is not None and snapshot.dirty
    assert not marker.exists(), "the snapshot ran the tree's hook"
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "stash", "create")
    assert marker.exists(), "the repository's own git still runs it"


#: What runs a program, by the name it is called by however it was imported: subprocess's,
#: os's, asyncio's and shutil.which.
_RUNNERS = frozenset(
    {
        "run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
        "system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execlp",
        "execle", "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn",
        "posix_spawnp", "create_subprocess_exec", "create_subprocess_shell", "which",
    }
)  # fmt: skip


#: A call whose name says it runs or splits a command, as a helper's often does
#: (``_run``, ``run_git``, ``_exec``, ``shlex.split``): matched on its name's words.
_RUNNER_WORDS = re.compile(r"(?:^|_)(?:run|call|exec\w*|spawn\w*|popen|system|split|git)(?:_|$)")


def _names_git(node: ast.AST | None) -> bool:
    """A string that is ``git`` or starts a git command line (``"git status"``), however it
    is built: an f-string, ``"git " + args``, ``"git %s" % args``, ``"git {}".format(args)``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "git" or node.value.startswith("git ")
    if isinstance(node, ast.JoinedStr) and node.values:
        return _names_git(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add | ast.Mod):
        return _names_git(node.left)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _names_git(node.func.value)
    return False


def _git_programs(tree: ast.AST) -> list[ast.AST]:
    """Each place in ``tree`` that names git as a program to run, in these shapes: the
    first element of a list or tuple (however it is laid out, ``["git"] + args`` and
    ``("git",)`` included); an argument of a call that runs a program (:data:`_RUNNERS`:
    ``subprocess.*``, ``os.system``/``os.exec*``, a ``shell=True`` string, ``which`` however
    imported, ``asyncio.create_subprocess_*``), a string or an f-string starting
    ``git``; a parameter's default; a name bound to it (``GIT = "git"``)."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple) and node.elts and _names_git(node.elts[0]):
            found.append(node)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if (name in _RUNNERS or _RUNNER_WORDS.search(name)) and any(
                _names_git(arg) for arg in [*node.args, *(k.value for k in node.keywords)]
            ):
                found.append(node)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            defaults = [*node.args.defaults, *node.args.kw_defaults]
            found += [d for d in defaults if d is not None and _names_git(d)]
        elif (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and node.value is not None
            and _names_git(node.value)
        ):
            found.append(node)
    return found


def test_the_git_guard_sees_each_shape_it_lists() -> None:
    """Review r13 of #113, finding 11, r14, finding 10, and r15, finding 10: the guard was a
    one-line regex, then blind to a command line in a string, an f-string, ``which``
    imported by name, asyncio's runners, a parameter's default, a helper of its own
    (``_run("git", ...)``), ``shlex.split`` and a command built by ``+``, ``%`` or
    ``.format``. Each shape it reads is here, and only these are claimed."""
    shapes = [
        'run(\n    [\n        "git",\n        "status",\n    ]\n)',
        'run(["git"] + args)',
        'run(("git",))',
        'GIT = "git"',
        'subprocess.run(argv, executable="git")',
        'shutil.which("git")',
        'os.system("git status")',
        'subprocess.run("git status --porcelain", shell=True)',
        'subprocess.run(f"git -C {root} status", shell=True)',
        'from shutil import which\nwhich("git")',
        'await asyncio.create_subprocess_exec("git", "status")',
        'def run_git(program="git"): ...',
        '_run("git", "status")',
        'run_git("git", "status")',
        'shlex.split("git status --porcelain")',
        'subprocess.run("git -C " + str(root) + " status", shell=True)',
        'os.system("git %s" % args)',
        'subprocess.run("git {}".format(args), shell=True)',
    ]
    for shape in shapes:
        assert _git_programs(ast.parse(shape)), shape
    for innocent in ('KEYS = ("editable", "git", "url")', 'raise Error("git is not installed")'):
        assert not _git_programs(ast.parse(innocent)), innocent


def test_every_git_aisquare_runs_is_its_own_hookless_one() -> None:
    """Review r12 of #113, finding 2: aisquare runs git through ``workspace.git_argv`` alone
    (no hook, no fsmonitor of the repository's), never a git argv of its own; the others
    are command lines printed for the user to run (``install_route.command_line``): the
    pull of an editable install, and ``fleet reap``'s way to drop a branch it kept. Read by
    the syntax tree (r13, 11)."""
    source = Path(__file__).resolve().parents[1] / "src" / "aisquare"
    found = []
    for path in sorted(source.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for node in _git_programs(ast.parse(text, filename=str(path))):
            found.append(
                f"{path.relative_to(source).as_posix()}: {ast.get_source_segment(text, node)}"
            )
    assert found == [
        'cli/fleet.py: ["git", "-C", str(root), "branch", "-D", branch]',
        'core/workspace.py: ["git", *GIT_OWN, *args]',
        'services/install_route.py: ["git", "-C", route.source or ".", "pull"]',
    ], found
