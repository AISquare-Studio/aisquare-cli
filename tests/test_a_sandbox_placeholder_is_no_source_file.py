"""A placeholder Codex's Linux sandbox mounts where the checkout has no file is no source file.

bwrap 0.11.1 (ubuntu-26.04) keeps a read-only path that does not exist by binding a deleted
file from a tmpfs over it: inside the sandbox, ``.claude``, ``.mcp.json``, ``.aisquare`` and
``.aisquare-worktrees`` read as empty files no directory holds (``st_nlink`` 0), on another
filesystem than the checkout, and git lists each as untracked. A report ``asq exec`` saved
there named them, the broker outside found none, and ``brief evidence --report`` refused it
as changed since it ran (CI 42842656612; probe 42849938305 on #114). The identity reads a
placeholder as the host does, as no file, and every real file as it always did.

A unit test cannot mount one, so it stands in through ``os.lstat`` (what
``source_revision.placeholder`` reads); the real one is
``tests/test_codex_native.py::test_real_codex_reads_the_code_index_and_records_evidence_from_its_sandbox``
on the ubuntu Codex legs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aisquare.core.source_revision import changed_paths, source_file_hashes, source_state

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="Codex's Linux sandbox; Windows has no such mount"
)


def _commit(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).write_text(text)
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "one"]):
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=t@t.invalid",
                "-c",
                "user.name=t",
                "-c",
                "commit.gpgsign=false",
                "-C",
                str(root),
                *args,
            ],
            check=True,
            capture_output=True,
        )
    return root


def _mounted_as_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    *paths: Path,
    links: int = 0,
    other_disk: bool = True,
    size: int | None = None,
) -> None:
    """``paths`` read through ``os.lstat`` as bwrap's placeholder reads in its sandbox."""
    real = os.lstat
    targets = {os.fspath(path) for path in paths}

    def lstat(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        status = real(path, *args, **kwargs)  # type: ignore[arg-type]
        if os.fspath(path) not in targets:
            return status
        fields = list(status[:10])
        fields[2] = status.st_dev + 1 if other_disk else status.st_dev
        fields[3] = links
        fields[6] = status.st_size if size is None else size
        return os.stat_result(fields)

    monkeypatch.setattr(os, "lstat", lstat)


@POSIX_ONLY
def test_an_untracked_placeholder_is_no_source_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _commit(
        tmp_path.resolve() / "checkout", {"app.py": "def answer():\n    return 42\n"}
    )
    on_the_host = source_state(checkout)
    for name in (".claude", ".mcp.json", ".aisquare", ".aisquare-worktrees"):
        (checkout / name).write_bytes(b"")
    _mounted_as_placeholder(
        monkeypatch,
        *(checkout / n for n in (".claude", ".mcp.json", ".aisquare", ".aisquare-worktrees")),
    )
    in_the_sandbox = source_state(checkout)
    assert in_the_sandbox == on_the_host
    assert changed_paths(in_the_sandbox[0], on_the_host[0]) == []


@POSIX_ONLY
def test_a_placeholder_over_a_tracked_file_reads_as_its_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _commit(tmp_path.resolve() / "checkout", {"app.py": "x = 1\n", ".mcp.json": "{}\n"})
    (checkout / ".mcp.json").unlink()
    on_the_host = source_file_hashes(checkout)
    assert ".mcp.json" in on_the_host, "a tracked file deleted from the disk still counts"
    (checkout / ".mcp.json").write_bytes(b"")
    _mounted_as_placeholder(monkeypatch, checkout / ".mcp.json")
    assert source_file_hashes(checkout) == on_the_host


@POSIX_ONLY
def test_a_placeholder_in_a_checkout_without_git_is_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path.resolve() / "plain"
    checkout.mkdir()
    (checkout / "app.py").write_text("x = 1\n")
    on_the_host = source_file_hashes(checkout)
    (checkout / ".claude").write_bytes(b"")
    _mounted_as_placeholder(monkeypatch, checkout / ".claude")
    assert source_file_hashes(checkout) == on_the_host


@pytest.mark.parametrize(
    ("links", "other_disk", "size"),
    [
        (1, True, None),  # a real file bound from elsewhere: it has a link
        (0, False, None),  # no link, but on the checkout's own disk
        (0, True, 7),  # not empty
        (1, False, None),  # the plain empty file it is on the disk
    ],
    ids=["linked-elsewhere", "same-disk", "not-empty", "plain-empty-file"],
)
def test_an_empty_file_that_is_no_placeholder_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, links: int, other_disk: bool, size: int | None
) -> None:
    checkout = _commit(tmp_path.resolve() / "checkout", {"app.py": "x = 1\n"})
    (checkout / ".claude").write_bytes(b"")
    _mounted_as_placeholder(
        monkeypatch, checkout / ".claude", links=links, other_disk=other_disk, size=size
    )
    assert ".claude" in source_file_hashes(checkout)


# --- the mount targets bwrap leaves in the checkout while a sandboxed command runs ------------

#: What bwrap 0.11.1 made at the top of the tree for a Codex launch's missing read-only paths
#: while its command ran (probe 42866990895 on #114): an empty file, mode 0444.
STUBS = (".aisquare", ".aisquare-worktrees", ".claude", ".mcp.json")


def _stubbed(checkout: Path, names: tuple[str, ...] = STUBS, mode: int = 0o444) -> list[Path]:
    made = []
    for name in names:
        path = checkout / name
        path.write_bytes(b"")
        path.chmod(mode)
        made.append(path)
    return made


@POSIX_ONLY
def test_a_named_mount_target_is_no_source_file(tmp_path: Path) -> None:
    from aisquare.core.source_revision import mount_targets

    checkout = _commit(tmp_path.resolve() / "checkout", {"app.py": "x = 1\n"})
    on_the_disk = source_state(checkout)
    stubs = _stubbed(checkout)
    assert sorted(source_file_hashes(checkout)) == sorted([*on_the_disk[0], *STUBS]), (
        "unnamed, they count"
    )
    with mount_targets(stubs):
        assert source_state(checkout) == on_the_disk


@POSIX_ONLY
def test_a_mount_target_over_a_tracked_file_reads_as_its_deletion(tmp_path: Path) -> None:
    from aisquare.core.source_revision import mount_targets

    checkout = _commit(tmp_path.resolve() / "checkout", {"app.py": "x = 1\n", ".mcp.json": "{}\n"})
    (checkout / ".mcp.json").unlink()
    on_the_disk = source_file_hashes(checkout)
    with mount_targets(_stubbed(checkout, (".mcp.json",))):
        assert source_file_hashes(checkout) == on_the_disk


@POSIX_ONLY
@pytest.mark.parametrize(
    ("mode", "text"),
    [(0o644, b""), (0o444, b"{}")],
    ids=["writable", "not-empty"],
)
def test_a_named_file_that_is_no_mount_target_still_counts(
    tmp_path: Path, mode: int, text: bytes
) -> None:
    from aisquare.core.source_revision import mount_targets

    checkout = _commit(tmp_path.resolve() / "checkout", {"app.py": "x = 1\n"})
    path = checkout / ".claude"
    path.write_bytes(text)
    path.chmod(mode)
    with mount_targets([path]):
        assert ".claude" in source_file_hashes(checkout)


@POSIX_ONLY
def test_only_a_brokered_call_reads_its_sandboxs_mount_targets_as_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import store_broker

    checkout = _commit(tmp_path.resolve() / "checkout", {"app.py": "x = 1\n"})
    on_the_disk = source_file_hashes(checkout)
    _stubbed(checkout)
    with store_broker.sandbox_mount_targets(checkout):
        assert sorted(source_file_hashes(checkout)) == sorted([*on_the_disk, *STUBS])
    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    with store_broker.sandbox_mount_targets(checkout):
        assert source_file_hashes(checkout) == on_the_disk
    (checkout / "added.py").write_text("y = 2\n")
    with store_broker.sandbox_mount_targets(checkout):
        assert "added.py" in source_file_hashes(checkout), "a real new file still counts"


# --- an agent that does not sandbox reads its checkout exactly as before ---------------------

#: ``source_fingerprint`` of :func:`_a_plain_checkout`, as 10a69773 (before the placeholder and
#: mount-target rules) computed it, with its own source_revision.py: a Claude Code launch,
#: which has no broker, must hash its checkout byte for byte the same.
BEFORE_THE_RULES = "sha256:c4c546e4742266a44ea1e6b9ae7eeed2eed5c8a0741a89a5dacee1513f329006"


def _a_plain_checkout(root: Path) -> Path:
    """Tracked, untracked, ignored, a link, an executable, a ``.gitignore``, and an empty file
    no one may write at ``.claude``, as a mount target reads: every mode and byte fixed."""
    root.mkdir(parents=True)
    git = ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t", "-c",
           "commit.gpgsign=false", "-C", str(root)]  # fmt: skip
    subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
    tracked = {
        "app.py": ("def answer():\n    return 42\n", 0o644),
        "pkg/run.sh": ("#!/bin/sh\necho ok\n", 0o755),
        ".gitignore": ("build/\n", 0o644),
        ".mcp.json": ("{}\n", 0o644),
    }
    for name, (text, mode) in tracked.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
        (root / name).chmod(mode)
    subprocess.run([*git, "add", "."], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", "one"], check=True, capture_output=True)
    (root / "notes.txt").write_text("untracked\n")
    (root / "notes.txt").chmod(0o644)
    (root / ".claude").write_bytes(b"")
    (root / ".claude").chmod(0o444)
    (root / "build").mkdir()
    (root / "build" / "out.txt").write_text("ignored\n")
    os.symlink("app.py", root / "link")
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text("# fixed\n")
    exclude.chmod(0o644)
    return root


@POSIX_ONLY
def test_a_checkout_outside_any_broker_hashes_as_it_did_before_the_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.source_revision import source_fingerprint
    from aisquare.services import store_broker

    monkeypatch.delenv(store_broker.CALLER_ENV, raising=False)
    checkout = _a_plain_checkout(tmp_path.resolve() / "checkout")
    assert ".claude" in source_file_hashes(checkout), "an empty file no one may write is source"
    assert source_fingerprint(checkout) == BEFORE_THE_RULES
    with store_broker.sandbox_mount_targets(checkout):
        assert source_fingerprint(checkout) == BEFORE_THE_RULES
