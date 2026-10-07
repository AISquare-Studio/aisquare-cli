"""Codebase snapshot packing — the Repomix mirror (subprocess faked)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import snapshot
from aisquare.models import Snapshot

FULL = (
    '<files>\n<file path="a.py">\nprint("hi")\n</file>\n'
    '<file path="b.py">\ny = 1\n</file>\n</files>\n'
)
SKEL = '<files>\n<file path="a.py">\nprint ⋮\n</file>\n<file path="b.py">\ny ⋮\n</file>\n</files>\n'

# Type of the faked _run_repomix(root, *, compress, ignore) -> (pack_text, stdout).
FakeRepomix = Callable[..., tuple[str, str]]

# The real one, taken before the autouse ``no_repomix`` fixture swaps it for a raiser,
# for the one test that checks the argv it builds.
_REAL_RUN_REPOMIX: FakeRepomix = snapshot._run_repomix


@pytest.fixture
def fake_repomix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_root: Path, *, compress: bool, ignore: Sequence[str] = ()) -> tuple[str, str]:
        return (SKEL, "Total Tokens: 4") if compress else (FULL, "Total Tokens: 9")

    monkeypatch.setattr(snapshot, "_run_repomix", _fake)


def test_build_index_maps_each_file_block() -> None:
    index = snapshot._build_index(FULL)
    assert [entry["path"] for entry in index] == ["a.py", "b.py"]
    for entry in index:
        assert 0 <= entry["start"] < entry["end"] <= len(FULL)
        block = FULL[entry["start"] : entry["end"]]
        assert block.startswith("<file path=")
        assert block.rstrip().endswith("</file>")
        assert entry["token_count"] >= 1


def test_generate_full_pack(fake_repomix: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: 100)
    meta = snapshot.generate("prj_test", Path("/tmp/repo"))
    assert meta.status == "ready"
    assert meta.compressed is False
    assert meta.file_count == 2
    assert snapshot.pack_path("prj_test").read_text(encoding="utf-8") == FULL
    assert snapshot.skeleton_path("prj_test").read_text(encoding="utf-8") == SKEL
    assert snapshot.index_path("prj_test").exists()
    assert snapshot.load("prj_test") == meta


def test_generate_over_budget_keeps_the_skeleton_and_the_index(
    fake_repomix: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even the compressed pack is over: the skeleton and its index are kept, the full pack is not.

    The budget mirrors a server cap on a pack read INTO a context; the CLI hands
    agents paths, so the cap gates only the full pack. The old ``too_large``
    verdict stored nothing, which left the repos that most need a skeleton —
    the big ones — as the only repos without one.
    """
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: snapshot.MAX_TOKENS + 1)
    meta = snapshot.generate("prj_big", Path("/tmp/repo"))
    assert meta.status == "skeleton_only"
    assert not snapshot.pack_path("prj_big").exists()
    assert snapshot.skeleton_path("prj_big").read_text(encoding="utf-8") == SKEL
    index = json.loads(snapshot.index_path("prj_big").read_text(encoding="utf-8"))
    assert [entry["path"] for entry in index] == ["a.py", "b.py"]
    assert meta.file_count == 2
    assert meta.skeleton_token_count == meta.token_count == snapshot.MAX_TOKENS + 1
    assert snapshot.load("prj_big") == meta


def test_generate_falls_back_to_compressed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_root: Path, *, compress: bool, ignore: Sequence[str] = ()) -> tuple[str, str]:
        return (SKEL, "") if compress else (FULL, "")

    monkeypatch.setattr(snapshot, "_run_repomix", _fake)
    # Full overflows, compressed fits → the stored pack is the compressed one.
    monkeypatch.setattr(
        snapshot,
        "_total_tokens",
        lambda text, _out: 10 if text == SKEL else snapshot.MAX_TOKENS + 1,
    )
    meta = snapshot.generate("prj_mid", Path("/tmp/repo"))
    assert meta.status == "ready"
    assert meta.compressed is True
    assert snapshot.pack_path("prj_mid").read_text(encoding="utf-8") == SKEL


def test_generate_raises_when_repomix_unavailable() -> None:
    # The autouse no_repomix fixture makes _run_repomix raise.
    with pytest.raises(snapshot.RepomixUnavailableError):
        snapshot.generate("prj_none", Path("/tmp/repo"))


def test_repomix_base_runs_the_resolved_path_not_a_bare_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare name is unrunnable on Windows even when the tool is on PATH.

    ``CreateProcess`` does not apply ``PATHEXT``, so ``subprocess`` cannot find
    the ``npx.CMD``/``repomix.CMD`` shims by name and raises FileNotFoundError.
    ``shutil.which`` has already resolved them, so pass what it found.
    """
    npx = r"C:\Program Files\nodejs\npx.CMD"
    monkeypatch.setattr(
        "aisquare.core.snapshot.shutil.which", lambda name: npx if name == "npx" else None
    )
    assert snapshot._repomix_base() == [npx, "--yes", "repomix"]

    direct = "/usr/local/bin/repomix"
    monkeypatch.setattr(
        "aisquare.core.snapshot.shutil.which", lambda name: direct if name == "repomix" else None
    )
    assert snapshot._repomix_base() == [direct]


def test_repomix_base_still_reports_when_nothing_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("aisquare.core.snapshot.shutil.which", lambda _name: None)
    with pytest.raises(snapshot.RepomixUnavailableError):
        snapshot._repomix_base()


def _on_path(monkeypatch: pytest.MonkeyPatch, *present: str) -> None:
    monkeypatch.setattr(
        "aisquare.core.snapshot.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in present else None,
    )


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        # A packer and a Node to run it on: the two ways a pack can run.
        (("node", "repomix"), True),
        (("node", "npx"), True),
        (("node", "npx", "repomix"), True),
        # Either packer alone is a `#!/usr/bin/env node` script with no Node.
        (("repomix",), False),
        (("npx",), False),
        (("npx", "repomix"), False),
        # A Node with nothing to run on it.
        (("node",), False),
        # The memory-only machine.
        ((), False),
    ],
)
def test_can_pack_needs_a_packer_and_a_node(
    monkeypatch: pytest.MonkeyPatch, present: tuple[str, ...], expected: bool
) -> None:
    _on_path(monkeypatch, *present)
    assert snapshot.can_pack() is expected


def test_can_pack_starts_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH lookups only: the doctor asks it once per row, so it must stay cheap."""

    def no_process(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("can_pack started a process")

    monkeypatch.setattr("aisquare.core.snapshot.subprocess.run", no_process)
    _on_path(monkeypatch, "node", "npx")
    assert snapshot.can_pack() is True


def _node_reads(monkeypatch: pytest.MonkeyPatch, version: tuple[int, ...] | None) -> None:
    """What `node --version` answers, without running a node (the PATH above is fake)."""
    monkeypatch.setattr(snapshot, "node_version", lambda: version)


def test_a_missing_snapshot_is_off_without_node_and_failed_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sentences `init` and `project onboard` print when no snapshot came back."""
    _node_reads(monkeypatch, (26, 7, 0))
    monkeypatch.setattr(snapshot, "installed_repomix_floor", lambda: None)
    _on_path(monkeypatch)
    off = snapshot.skipped_detail()
    _on_path(monkeypatch, "node")
    no_packer = snapshot.skipped_detail()
    _on_path(monkeypatch, "node", "repomix")
    failed = snapshot.skipped_detail()
    assert off == snapshot.OFF_DETAIL
    assert failed == snapshot.FAILED_DETAIL
    # Telling someone who has Node to install Node is the confusion this split removes:
    # a pack that failed blames no cause, and a Node with no packer names the packer.
    assert "Node" not in failed
    assert no_packer == snapshot.NO_PACKER_DETAIL
    assert "Node.js" not in no_packer and "repomix" in no_packer


@pytest.mark.parametrize(
    ("present", "node", "installed_floor", "named"),
    [
        # npx fetches the latest repomix, whose floor is MIN_NODE: Ubuntu 22.04's 12.
        (("node", "npx"), (12, 22, 9), (16,), "Node 12.22.9 is older than repomix needs (22+)"),
        # An installed repomix is judged by the floor it declares, higher or lower.
        (("node", "repomix"), (22, 1, 0), (24,), "Node 22.1.0 is older than repomix needs (24+)"),
    ],
    ids=["npx-path", "installed-repomix"],
)
def test_a_node_too_old_for_the_repomix_that_ran_is_named(
    monkeypatch: pytest.MonkeyPatch,
    present: tuple[str, ...],
    node: tuple[int, ...],
    installed_floor: tuple[int, ...],
    named: str,
) -> None:
    """Debian 12 and Ubuntu 22.04's packaged Node: the pack fails, and the line says why."""
    _on_path(monkeypatch, *present)
    _node_reads(monkeypatch, node)
    monkeypatch.setattr(snapshot, "installed_repomix_floor", lambda: installed_floor)

    detail = snapshot.skipped_detail()

    assert detail.startswith(f"skipped — {named}"), detail
    assert detail.endswith("run: aisquare doctor")


@pytest.mark.parametrize(
    ("present", "node", "installed_floor"),
    [
        # A pinned repomix that declares a LOWER floor packs on Node 18: no accusation.
        (("node", "repomix"), (18, 19, 1), (16,)),
        # Unreadable is not presumed old, as in the doctor's repomix row.
        (("node", "npx"), None, None),
    ],
    ids=["installed-floor-met", "unreadable"],
)
def test_a_node_that_is_not_known_to_be_too_old_is_not_blamed(
    monkeypatch: pytest.MonkeyPatch,
    present: tuple[str, ...],
    node: tuple[int, ...] | None,
    installed_floor: tuple[int, ...] | None,
) -> None:
    _on_path(monkeypatch, *present)
    _node_reads(monkeypatch, node)
    monkeypatch.setattr(snapshot, "installed_repomix_floor", lambda: installed_floor)

    assert snapshot.skipped_detail() == snapshot.FAILED_DETAIL


def test_child_output_is_decoded_as_utf8_not_the_locale_codec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tool output is UTF-8, whatever the machine's locale happens to be.

    ``subprocess`` with ``text=True`` and no explicit encoding decodes using
    the locale codec. On Windows that is the ANSI codepage (cp1252), so the
    UTF-8 these tools emit raised UnicodeDecodeError inside subprocess's reader
    thread — repomix's own token count was lost that way, and the traceback was
    printed straight at the user mid-pack.
    """
    seen: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr("aisquare.core.snapshot.subprocess.run", _capture)

    assert snapshot.head_sha(tmp_path) == "deadbeef"
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


# --- the token budget is a parameter, and the verdict names its numbers (#82) ---------------


def test_generate_holds_both_packs_to_the_budget_it_is_given(
    fake_repomix: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full 9, compressed 4: three budgets, three verdicts, each recording what it compared."""
    monkeypatch.setattr(snapshot, "_total_tokens", lambda text, _out: 4 if text == SKEL else 9)

    fits = snapshot.generate("prj_fits", Path("/tmp/repo"), max_tokens=9)
    assert (fits.status, fits.compressed) == ("ready", False)
    assert (fits.full_token_count, fits.token_count, fits.max_tokens) == (9, 9, 9)

    squeezed = snapshot.generate("prj_squeezed", Path("/tmp/repo"), max_tokens=5)
    assert (squeezed.status, squeezed.compressed) == ("ready", True)
    assert (squeezed.full_token_count, squeezed.token_count, squeezed.max_tokens) == (9, 4, 5)

    over = snapshot.generate("prj_over", Path("/tmp/repo"), max_tokens=3)
    assert over.status == "skeleton_only"
    assert (over.full_token_count, over.token_count, over.max_tokens) == (9, 4, 3)
    assert over.skeleton_token_count == 4
    assert not snapshot.pack_path("prj_over").exists()
    assert snapshot.skeleton_path("prj_over").read_text(encoding="utf-8") == SKEL
    assert snapshot.load("prj_over") == over, "the numbers survive the trip through snapshot.json"


def test_a_shrunken_budget_removes_the_stale_full_pack(
    fake_repomix: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skeleton-only re-pack must not leave an earlier run's full pack beside it."""
    monkeypatch.setattr(snapshot, "_total_tokens", lambda text, _out: 4 if text == SKEL else 9)
    assert snapshot.generate("prj_shrink", Path("/tmp/repo"), max_tokens=9).status == "ready"
    assert snapshot.pack_path("prj_shrink").exists()

    again = snapshot.generate("prj_shrink", Path("/tmp/repo"), max_tokens=3)
    assert again.status == "skeleton_only"
    assert not snapshot.pack_path("prj_shrink").exists()
    assert snapshot.skeleton_path("prj_shrink").exists()


def test_skeleton_only_detail_names_the_counts_and_the_budget() -> None:
    meta = _verdict(token_count=2_030_000, full_token_count=10_990_000, max_tokens=150_000)
    meta.status = "skeleton_only"
    meta.skeleton_token_count = 2_030_000
    meta.file_count = 1234
    assert snapshot.skeleton_only_detail(meta) == (
        "skeleton only: 2030000 tokens, 1234 files indexed; "
        "full pack skipped over budget 150000 (10990000 tokens)"
    )


def test_generate_without_a_budget_uses_the_built_in_default(
    fake_repomix: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody who has not set the knob sees a change: the default IS the old constant."""
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: snapshot.MAX_TOKENS)
    meta = snapshot.generate("prj_default", Path("/tmp/repo"))
    assert meta.status == "ready"
    assert meta.max_tokens == snapshot.MAX_TOKENS == 150_000


def _verdict(
    *, token_count: int, full_token_count: int | None = None, max_tokens: int | None = None
) -> Snapshot:
    return Snapshot(
        project_id="prj_x",
        generated_at=datetime.now(tz=UTC),
        pack_path=Path("/tmp/pack"),
        skeleton_path=Path("/tmp/skel"),
        index_path=Path("/tmp/index"),
        token_count=token_count,
        compressed=True,
        status="too_large",
        full_token_count=full_token_count,
        max_tokens=max_tokens,
    )


def test_too_large_detail_names_all_three_numbers_and_both_ways_out() -> None:
    verdict = _verdict(token_count=203_991, full_token_count=412_318, max_tokens=150_000)
    assert snapshot.too_large_detail(verdict) == (
        "codebase too large: full 412318 tokens, compressed 203991 tokens, budget 150000. "
        "Raise [snapshot] max_tokens (aisquare config set snapshot.max_tokens <n>), or exclude "
        "generated or vendored trees with [snapshot] ignore (aisquare config set snapshot.ignore "
        "'<glob>,<glob>') or a .repomixignore at the repo root."
    )


def test_too_large_detail_for_a_pre_knob_snapshot_says_the_numbers_were_not_recorded() -> None:
    """A snapshot.json written by 0.6.0 carries neither the full count nor the budget.

    Loaded today both come back None — and the sentence must say so, never print
    ``full 0 tokens, budget 0``, which would be a measurement that never happened.
    Exercised through the JSON an old build wrote, not through the constructor.
    """
    written_by_0_6_0 = {
        "project_id": "prj_old",
        "generated_at": "2026-09-01T00:00:00Z",
        "pack_path": "/tmp/pack",
        "skeleton_path": "/tmp/skel",
        "index_path": "/tmp/index",
        "token_count": 203991,
        "compressed": True,
        "status": "too_large",
    }
    verdict = Snapshot.model_validate(written_by_0_6_0)
    assert (verdict.full_token_count, verdict.max_tokens) == (None, None)

    detail = snapshot.too_large_detail(verdict)
    assert "before the numbers were recorded" in detail
    assert " 0 tokens" not in detail
    assert "budget 0" not in detail


# --- what a pack leaves out: defaults, nested checkouts, the operator's list (#82) ----------


def test_ignore_patterns_start_with_the_defaults_and_extend_with_the_operators(
    tmp_path: Path,
) -> None:
    """The operator's list EXTENDS the built-ins: setting one pattern never loses node_modules.

    Blanks drop, whitespace is trimmed, a default repeated by the operator is not
    sent twice, and the order is defaults first so a reader of the argv sees
    the same shape every time.
    """
    patterns = snapshot.ignore_patterns(
        tmp_path, ["docs/generated/**", " **/fixtures/** ", "", "**/dist/**"]
    )
    assert patterns[: len(snapshot.DEFAULT_IGNORE)] == list(snapshot.DEFAULT_IGNORE)
    assert patterns[len(snapshot.DEFAULT_IGNORE) :] == ["docs/generated/**", "**/fixtures/**"]
    for name in (
        "node_modules",
        ".venv",
        "venv",
        ".git",
        "__pycache__",
        "dist",
        "build",
        "coverage",
        ".aisquare-worktrees",
        "*.worktrees",
    ):
        assert f"**/{name}/**" in snapshot.DEFAULT_IGNORE, name


def test_nested_repos_below_the_root_are_excluded_and_not_walked(tmp_path: Path) -> None:
    """Another project's checkout inside this one is that project's snapshot, not ours.

    A repository (``.git`` directory) and a worktree (``.git`` FILE) are both
    found; the root's own ``.git`` is not "nested"; a checkout inside a
    default-ignored tree is never even visited; and a found checkout is not
    descended, so a repo inside a repo yields one pattern, not two.
    """
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "vendor" / "lib" / ".git").mkdir(parents=True)
    (root / "vendor" / "lib" / "deeper" / ".git").mkdir(parents=True)
    (root / "checkouts" / "wt").mkdir(parents=True)
    (root / "checkouts" / "wt" / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    (root / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    (root / "proj.worktrees" / "feature" / ".git").mkdir(parents=True)

    assert snapshot.nested_repos(root) == ["checkouts/wt/**", "vendor/lib/**"]


def test_run_repomix_passes_the_ignore_list_comma_joined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--ignore a,b`` is how repomix takes it; no list, no flag."""
    seen: list[list[str]] = []

    def _capture(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("aisquare.core.snapshot.subprocess.run", _capture)
    monkeypatch.setattr(snapshot, "_repomix_base", lambda: ["repomix"])

    _REAL_RUN_REPOMIX(tmp_path, compress=True, ignore=["**/node_modules/**", "docs/generated/**"])
    assert seen[0][:2] == ["repomix", "--compress"]
    assert seen[0][seen[0].index("--ignore") + 1] == "**/node_modules/**,docs/generated/**"

    _REAL_RUN_REPOMIX(tmp_path, compress=False)
    assert "--ignore" not in seen[1]


def test_generate_hands_the_operators_patterns_to_repomix_after_the_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both runs — full pack and skeleton — get one list: defaults, nested repos, operator's."""
    seen: list[list[str]] = []

    def _fake(_root: Path, *, compress: bool, ignore: Sequence[str] = ()) -> tuple[str, str]:
        seen.append(list(ignore))
        return (SKEL, "") if compress else (FULL, "")

    monkeypatch.setattr(snapshot, "_run_repomix", _fake)
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: 1)
    (tmp_path / "vendor" / "fork" / ".git").mkdir(parents=True)

    snapshot.generate("prj_ign", tmp_path, ignore=["docs/generated/**"])

    expected = [*snapshot.DEFAULT_IGNORE, "vendor/fork/**", "docs/generated/**"]
    assert len(seen) == 2, "the full pack and the best-effort skeleton"
    assert seen == [expected, expected]
