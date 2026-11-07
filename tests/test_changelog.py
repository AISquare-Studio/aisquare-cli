"""What a release's CHANGELOG section says is said once, when it ships (review r23 of #113).

The merge of 0.9.0 kept the PR's rewording of eleven lines that were released as main wrote
them, so the 0.9.0 section said 0.9.0 had ``--agent codex``. A release's section is the
release's: what a later version changes about it goes in that version's own entry.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"

#: ``## [0.9.0] - 2026-10-25`` as ``git show v0.9.0:CHANGELOG.md`` has it, up to the line before
#: the next release's heading. A section only changes with a release's tag; changing it here
#: is a decision, and this digest is where it is made.
RELEASED = {"0.9.0": "51902fe659bf77d93bc25b67220e65f6193041de04214a4d8020f260a984563e"}


def _section(text: str, version: str) -> str:
    found = re.search(rf"^## \[{re.escape(version)}\].*?(?=^## \[)", text, re.DOTALL | re.MULTILINE)
    assert found is not None, f"no ## [{version}] section"
    return found.group(0)


def test_a_released_section_is_the_one_that_was_released() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    for version, digest in RELEASED.items():
        got = hashlib.sha256(_section(text, version).encode("utf-8")).hexdigest()
        assert got == digest, (
            f"the ## [{version}] section is not what v{version} shipped: restore it with "
            f"`git show v{version}:CHANGELOG.md`, and put the new wording in [Unreleased]"
        )


def test_the_unreleased_entry_has_one_blank_line_where_a_blank_line_goes() -> None:
    """Two in a row, as the merge left at the top of it and before the first release, are
    one line too many (main has one)."""
    text = CHANGELOG.read_text(encoding="utf-8")
    unreleased = _section(text, "Unreleased")
    assert "\n\n\n" not in unreleased
