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
from tests.fsperms import can_deny_writes, can_symlink, unwritable

CHANGED = "https://after.example"

# A capability probe, not a platform check: creating a symlink on Windows needs
# SeCreateSymbolicLinkPrivilege, which the CI runner holds and an ordinary
# developer account does not. Applied per test rather than to the module,
# because the two tests guarding the COMMON path (a plain file, and a first
# write into a missing home) need no link and are the last ones that should
# stop running on a developer's machine.
_can_deny = pytest.mark.skipif(
    not can_deny_writes(), reason="writes cannot be denied here (running as root?)"
)
_needs_symlink = pytest.mark.skipif(
    not can_symlink(), reason="this machine cannot create symlinks (needs privilege on Windows)"
)


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


@_needs_symlink
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


@_needs_symlink
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


@_needs_symlink
def test_the_returned_path_is_the_one_the_caller_asked_for(tmp_path: Path) -> None:
    """Commands echo this back to an operator.

    Where the bytes physically land is our business; the path a user typed is
    the path they should be told about. Returning the resolved file would make
    `team bind` print a dotfiles path the operator never mentioned.
    """
    link, _real = _linked(tmp_path)

    assert save_config(_changed(), link) == link


@_needs_symlink
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


@_needs_symlink
@_can_deny
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
    with unwritable(real.parent), pytest.raises(OSError):
        save_config(_changed(), link)

    assert link.is_symlink(), "a failed write severed the link anyway"
    assert real.read_text(encoding="utf-8") == before, "a failed write modified the original"


@_needs_symlink
@_can_deny
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
    with unwritable(real.parent), pytest.raises(PermissionError) as caught:
        save_config(_changed(), link)

    message = str(caught.value)
    # The resolved FILE is asserted through the structured `filename` attribute
    # rather than as a substring of the rendered message, because `OSError`
    # renders that field through `repr()` — which DOUBLES every backslash in a
    # Windows path. The path is genuinely there and an operator reads it fine;
    # it is simply never there as `str(real)`. POSIX paths contain no
    # backslashes, so the escaping is a no-op and hid this completely.
    assert caught.value.filename == str(real), (
        f"the resolved path is not named: {caught.value.filename!r} != {str(real)!r}"
    )
    # The DIRECTORY needing permission is in the detail text, which is not
    # escaped, so this one is a plain substring on every platform.
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


def test_a_first_write_still_creates_a_missing_aisquare_home(tmp_path: Path) -> None:
    """THE regression that matters most, pinned before anything else.

    ``mkdir(parents=True)`` is load-bearing for the first write a machine ever
    does. Restricting directory creation must not touch the path that has
    nothing to do with symlinks.
    """
    target = tmp_path / "never" / "existed" / "config.toml"

    save_config(AppConfig(), target)

    assert load_config(target).profile == "default"


@_needs_symlink
def test_a_link_into_an_existing_directory_still_writes_there(tmp_path: Path) -> None:
    """The "cloned my dotfiles, config not written yet" case.

    The directory is there and only the FILE is missing, which is the ordinary
    state after a fresh clone. Nothing is invented: the file lands where the
    link says and no directory is created.
    """
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    home.mkdir()
    dotfiles.mkdir()
    link = home / "config.toml"
    link.symlink_to(dotfiles / "config.toml")
    before = {path for path in tmp_path.rglob("*")}

    save_config(_changed(), link)

    assert link.is_symlink()
    assert CHANGED in (dotfiles / "config.toml").read_text(encoding="utf-8")
    created = {path for path in tmp_path.rglob("*")} - before
    assert all(path.is_file() for path in created), f"a directory was invented: {created}"


@_needs_symlink
def test_a_link_into_a_missing_directory_refuses_to_invent_it(tmp_path: Path) -> None:
    """The decision: follow the link, do not materialise what it points at.

    Against the current behaviour this FAILS — the whole tree is created, exit 0.
    A broken link pointing at a mounted Windows drive would have the CLI create
    directories there, silently, from a command that named none of it. Following
    a link honours stated intent; materialising a tree the user never created
    invents it.
    """
    home = tmp_path / "home"
    home.mkdir()
    missing = tmp_path / "not" / "cloned" / "yet"
    link = home / "config.toml"
    link.symlink_to(missing / "config.toml")

    with pytest.raises(OSError) as caught:
        save_config(_changed(), link)

    assert not missing.exists(), "the directory tree was created anyway"
    assert not (tmp_path / "not").exists(), "a partial tree was left behind"
    assert link.is_symlink(), "the failed write severed the link"

    message = str(caught.value)
    assert str(missing) in message, f"the missing directory is not named: {message}"
    assert "symlink" in message, "nothing explains why another path is involved"
    assert caught.value.errno is not None, "errno was dropped"


@_needs_symlink
def test_the_refusal_says_what_to_do_about_it(tmp_path: Path) -> None:
    """A refusal that does not name the remedy just relocates the confusion."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").symlink_to(tmp_path / "gone" / "config.toml")

    with pytest.raises(OSError) as caught:
        save_config(_changed(), home / "config.toml")

    message = str(caught.value).lower()
    assert "clone" in message or "create" in message, "no remedy offered"
    assert "link" in message, "the user is not told the link is an option to change"
