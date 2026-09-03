"""Turn snapshots: the exact tree a prompt was submitted against, kept alive for replay."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.spawn import TRACING_ENV_VARS
from aisquare.services import ci_snapshot
from aisquare.services.ci_contract import HookRequest
from tests.ci_support import git, repo, request

TRACE = "trc_01j9q8p3k7zr4m2n6v0c1d8e5f"


def test_a_clean_tree_snapshots_as_head(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    snapshot = ci_snapshot.capture(root, TRACE)
    assert snapshot is not None
    assert snapshot.object_id == git(root, "rev-parse", "HEAD")
    assert snapshot.dirty is False
    assert snapshot.ref is None


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
    recent = _dated_commit(root, "2026-09-02T00:00:00+0000")
    git(root, "update-ref", ci_snapshot.WIP_REF_PREFIX + "old", old)
    git(root, "update-ref", ci_snapshot.WIP_REF_PREFIX + "recent", recent)
    (root / "tracked.txt").write_text("edited\n", encoding="utf-8")

    snapshot = ci_snapshot.capture(root, "trc_new")

    assert snapshot is not None and snapshot.ref == ci_snapshot.WIP_REF_PREFIX + "new"
    refs = set(git(root, "for-each-ref", "--format=%(refname)", ci_snapshot.WIP_REF_PREFIX).split())
    assert refs == {ci_snapshot.WIP_REF_PREFIX + "recent", ci_snapshot.WIP_REF_PREFIX + "new"}
    assert git(root, "cat-file", "-t", old) == "commit", "pruning drops the ref, not the object"


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


def test_a_trace_id_that_cannot_be_a_ref_still_yields_the_object(tmp_path: Path) -> None:
    root = repo(tmp_path / "r")
    (root / "tracked.txt").write_text("x\n", encoding="utf-8")
    snapshot = ci_snapshot.capture(root, "trc_has space")
    assert snapshot is not None
    assert snapshot.ref is None
    assert len(snapshot.object_id) == 40
