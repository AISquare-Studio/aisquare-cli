"""A folder or a home reached through a link is the one it points at (``codex_prompts``, r24 9).

``prompts_since`` compares paths three times, each by ``os.path.realpath``: the folder asked
about, the folder a rollout's ``session_meta`` names, and the rollout itself against the files
the pane's processes hold (the system names those by where they are: ``/var`` on macOS is
``/private/var``). Dropped from any of the three, nothing failed: a row working under a linked
folder or a linked home read as having run no prompt, or as not holding its own rollout. Each is
pinned here, with a table of held files made up.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core.agent_adapters import codex_prompts
from aisquare.core.process_table import PaneProcesses
from tests.test_codex_first_message_never_taken import _rollout

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="a link needs a privilege on NTFS: the shape is POSIX's"
)

PROMPT = "the row's own task"


class _Holding(PaneProcesses):
    """A pane whose processes hold ``files`` (what ``lsof`` would name), or nothing is told."""

    def __init__(self, files: frozenset[str] | None) -> None:
        super().__init__(lambda: None)
        self._files = files

    def held_below(self, folder: Path) -> frozenset[str] | None:
        return self._files


@pytest.fixture(autouse=True)
def fresh() -> None:
    codex_prompts._KEPT.clear()
    codex_prompts._FOLDERS.clear()


def _since() -> datetime:
    return datetime.now(tz=UTC) - timedelta(minutes=1)


def _linked(tmp_path: Path) -> tuple[Path, Path]:
    """A folder and a link to it."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    return real, link


def test_a_folder_asked_about_through_a_link_is_the_folder_a_rollout_names(
    tmp_path: Path,
) -> None:
    real, link = _linked(tmp_path)
    home = tmp_path / "home"
    _rollout(home, real, [PROMPT])

    record = codex_prompts.prompts_since(home, link, _since(), _Holding(None))

    assert record.prompts == (PROMPT,), "the row's folder is the link, the rollout's the real one"


def test_a_folder_a_rollout_names_through_a_link_is_the_folder_asked_about(
    tmp_path: Path,
) -> None:
    real, link = _linked(tmp_path)
    home = tmp_path / "home"
    _rollout(home, link, [PROMPT])

    record = codex_prompts.prompts_since(home, real, _since(), _Holding(None))

    assert record.prompts == (PROMPT,), (
        "the rollout names the link, the row's folder is the real one"
    )


def test_a_home_reached_through_a_link_has_its_rollout_held_by_its_real_path(
    tmp_path: Path,
) -> None:
    real_home, link_home = _linked(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    rollout = _rollout(real_home, work, [PROMPT])
    held = frozenset({os.path.realpath(rollout)})  # as lsof and /proc name it

    record = codex_prompts.prompts_since(link_home, work, _since(), _Holding(held))

    assert record.own is True and record.prompts == (PROMPT,), "held, though listed by the link"


def test_a_rollout_the_pane_does_not_hold_is_not_the_rows_through_a_link_either(
    tmp_path: Path,
) -> None:
    real_home, link_home = _linked(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _rollout(real_home, work, [PROMPT])

    record = codex_prompts.prompts_since(
        link_home, work, _since(), _Holding(frozenset({"/elsewhere/rollout.jsonl"}))
    )

    assert record.own is True and record.prompts == ()
