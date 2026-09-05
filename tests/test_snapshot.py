"""Codebase snapshot packing — the Repomix mirror (subprocess faked)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
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

# Type of the faked _run_repomix(root, *, compress) -> (pack_text, stdout).
FakeRepomix = Callable[..., tuple[str, str]]


@pytest.fixture
def fake_repomix(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_root: Path, *, compress: bool) -> tuple[str, str]:
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


def test_generate_marks_too_large(fake_repomix: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "_total_tokens", lambda _text, _out: snapshot.MAX_TOKENS + 1)
    meta = snapshot.generate("prj_big", Path("/tmp/repo"))
    assert meta.status == "too_large"
    assert not snapshot.pack_path("prj_big").exists()


def test_generate_falls_back_to_compressed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(_root: Path, *, compress: bool) -> tuple[str, str]:
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
    assert over.status == "too_large"
    assert (over.full_token_count, over.token_count, over.max_tokens) == (9, 4, 3)
    assert not snapshot.pack_path("prj_over").exists()
    assert snapshot.load("prj_over") == over, "the numbers survive the trip through snapshot.json"


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
        "Raise [snapshot] max_tokens (aisquare config set snapshot.max_tokens <n>) or add a "
        ".repomixignore to exclude generated or vendored trees."
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
