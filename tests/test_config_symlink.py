"""A symlinked config.toml must keep being the config after a write.

Symlinking a dotfile into a version-controlled directory is a mainstream
pattern, and ``AISQUARE_HOME`` accepts any path, so nothing in this project ever
assumed config.toml is a plain regular file.

``os.replace`` swaps the NAME it is given. Pointed at a symlink it replaces the
LINK with a regular file, where the plain ``write_text`` this used to be wrote
THROUGH it. @9bbc8ed7 found that when the atomic write landed, and measured both
builds to show it was a behaviour change rather than a pre-existing wart.

WHAT MAKES IT WORTH FIXING RATHER THAN DOCUMENTING is that severing is silent in
the way that costs most: nothing is lost, the tracked file simply keeps its old
contents. ``git status`` shows nothing, the next machine sync restores settings
that stopped being live, and no command of ours says a word.

THE COST OF THE FIX, measured rather than waved through: a symlink pointing into
a read-only directory now FAILS the write, where before it succeeded by quietly
replacing the link. That is a new way for a config write to fail — and it is the
right trade, because the failure is loud, leaves the link and the original file
untouched, and the "success" it replaces was the tracked file silently ceasing
to be the config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.config import AppConfig, load_config, save_config

CHANGED = "https://after.example"


def _linked(tmp_path: Path) -> tuple[Path, Path]:
    """A dotfiles layout: ``home/config.toml`` -> ``dotfiles/config.toml``."""
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    home.mkdir()
    dotfiles.mkdir()
    real = dotfiles / "config.toml"
    save_config(AppConfig(), real)
    link = home / "config.toml"
    link.symlink_to(real)
    return link, real


def _changed() -> AppConfig:
    config = AppConfig()
    config.explainability.gateway_url = CHANGED
    return config


def test_a_symlinked_config_survives_a_write(tmp_path: Path) -> None:
    """The defect: against the unresolved write this FAILS on the first assert."""
    link, real = _linked(tmp_path)

    save_config(_changed(), link)

    assert link.is_symlink(), (
        "the symlink was replaced by a regular file — the user's tracked config "
        "is now orphaned and nothing said so"
    )
    assert CHANGED in real.read_text(encoding="utf-8"), (
        "the write did not reach the file the link points at"
    )
    assert load_config(link).explainability.gateway_url == CHANGED


def test_the_write_is_still_atomic_through_the_link(tmp_path: Path) -> None:
    """Following the link must not cost the property the temp file exists for.

    The temp is created beside the RESOLVED file, so temp and target still share
    a directory and therefore a filesystem. Asserted on st_dev because that is
    what the kernel compares.
    """
    link, real = _linked(tmp_path)
    seen: list[tuple[int, int]] = []
    real_replace = os.replace

    def _watch(src: Any, dst: Any, **kwargs: Any) -> None:
        seen.append((os.stat(src).st_dev, os.stat(Path(dst).parent).st_dev))
        real_replace(src, dst, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", _watch)
        save_config(_changed(), link)

    assert seen, "no replace happened"
    assert seen[-1][0] == seen[-1][1], "temp and resolved target are on different filesystems"
    assert real.read_text(encoding="utf-8"), "the real file is empty"


def test_the_returned_path_is_the_one_the_caller_asked_for(tmp_path: Path) -> None:
    """Commands echo this back to an operator.

    Where the bytes physically land is our business; the path a user typed is
    the path they should be told about. Returning the resolved file would make
    `team bind` print a dotfiles path the operator never mentioned.
    """
    link, _real = _linked(tmp_path)

    assert save_config(_changed(), link) == link


def test_a_symlinked_home_directory_is_unaffected(tmp_path: Path) -> None:
    """The other dotfiles shape, and it never had the problem.

    When the DIRECTORY is the link, config.toml inside it is an ordinary file;
    the temp lands in the linked-to directory and ``os.replace`` never touches a
    link. @9bbc8ed7 expected this and did not measure it — measured here, both
    before and after the fix.
    """
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    save_config(AppConfig(), real_home / "config.toml")
    linked_home = tmp_path / "home"
    linked_home.symlink_to(real_home)

    save_config(_changed(), linked_home / "config.toml")

    assert linked_home.is_symlink(), "the home link itself was disturbed"
    assert CHANGED in (real_home / "config.toml").read_text(encoding="utf-8")


def test_an_unwritable_link_target_fails_loudly_and_changes_nothing(tmp_path: Path) -> None:
    """The cost of following the link, pinned so it is a decision and not a surprise.

    Before this change the write succeeded here by replacing the link with a
    local file — and the user's tracked config silently stopped being used. Now
    it raises. Both the link and the original file must come through untouched,
    because a failed write that half-applies would be worse than either
    behaviour.
    """
    link, real = _linked(tmp_path)
    before = real.read_text(encoding="utf-8")
    os.chmod(real.parent, 0o500)
    try:
        with pytest.raises(OSError):
            save_config(_changed(), link)
    finally:
        os.chmod(real.parent, 0o700)

    assert link.is_symlink(), "a failed write severed the link anyway"
    assert real.read_text(encoding="utf-8") == before, "a failed write modified the original"


def test_the_failure_names_the_resolved_path_not_the_link(tmp_path: Path) -> None:
    """@dfd9a883's condition on the ruling, and it is the difference between an
    honest error and a confusing one.

    The path that cannot be written is the REAL file's directory. Reported
    against the link the caller typed, an operator goes looking at a directory
    that is perfectly writable — we would have swapped a silent sever for a
    misdirecting error and lost either way. The class and errno are preserved so
    anything matching on PermissionError or errno still does.
    """
    link, real = _linked(tmp_path)
    os.chmod(real.parent, 0o500)
    try:
        with pytest.raises(PermissionError) as caught:
            save_config(_changed(), link)
    finally:
        os.chmod(real.parent, 0o700)

    message = str(caught.value)
    assert str(real) in message, f"the resolved path is not named: {message}"
    assert str(real.parent) in message, "the directory needing permission is not named"
    assert "symlink" in message, "nothing explains why a different path is involved"
    assert caught.value.errno is not None, "errno was dropped"


def test_a_plain_file_config_is_unchanged_by_all_of_this(tmp_path: Path) -> None:
    """The overwhelmingly common case must not have moved."""
    target = tmp_path / "config.toml"
    save_config(AppConfig(), target)

    save_config(_changed(), target)

    assert not target.is_symlink()
    assert load_config(target).explainability.gateway_url == CHANGED
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"], "temp file left behind"
