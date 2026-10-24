"""A persona's directory and SKILL.md are made as any file the user makes: by the umask.

Review of #267, item (e), and the owner's decision D6 (2026-10-23). ``_publish`` staged a
persona in ``mkdtemp``'s 0700 directory and renamed it into place, and ``save`` wrote
SKILL.md through ``mkstemp``'s 0600 file: a persona the user had written could not be read
by the other accounts and tools that read the rest of their files. The staging stays
atomic; only the modes change.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core import personas as core
from aisquare.services import personas as personas_service

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


@pytest.fixture
def umask() -> Iterator[None]:
    """Run with ``0o022``, the common default, whatever this process was started under."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_new_persona_is_made_by_the_umask(umask: None) -> None:
    made = personas_service.new("reader", layer="user", root=None)

    assert _mode(made) == 0o755
    assert _mode(made / core.SKILL_FILE) == 0o644
    assert not [p for p in made.parent.iterdir() if p.name.startswith(".reader.")], "staged"


def test_a_saved_skill_keeps_the_mode_it_has(umask: None) -> None:
    made = personas_service.new("reader", layer="user", root=None)
    skill = made / core.SKILL_FILE
    text = skill.read_text(encoding="utf-8")

    personas_service.save("reader", text, root=None, layer="user")
    assert _mode(skill) == 0o644, "never narrowed to 0600"

    skill.chmod(0o600)
    personas_service.save("reader", text, root=None, layer="user")
    assert _mode(skill) == 0o600, "a mode the user set is theirs"


def test_a_tighter_umask_is_followed_too() -> None:
    old = os.umask(0o077)
    try:
        made = personas_service.new("private", layer="user", root=None)
    finally:
        os.umask(old)
    assert _mode(made) == 0o700 and _mode(made / core.SKILL_FILE) == 0o600
