"""Codebase snapshot packing — the Repomix mirror (subprocess faked)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from aisquare.core import snapshot

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
