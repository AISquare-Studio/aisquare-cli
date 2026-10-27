"""A document that described the code backwards (review of #257).

CONTRIBUTING's stub-to-service flow said to move a graduated command OFF the stub
test's skip-list. The test reads the document beside the code it describes.
"""

from __future__ import annotations

from pathlib import Path

from tests.test_stubs import IMPLEMENTED, _is_stubbed

ROOT = Path(__file__).resolve().parents[1]


def _flat(path: str) -> str:
    """A document's text with its line breaks and indents read as single spaces."""
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def test_contributing_adds_a_graduated_command_to_implemented_as_the_template_does() -> None:
    """The release fixed the PR template's line and left CONTRIBUTING's step 3 saying the
    opposite. ``IMPLEMENTED`` lists the commands that are real, so a stub that graduates
    is added to it; left out, the stub test expects it to exit 70."""
    contributing = _flat("CONTRIBUTING.md")
    template = _flat(".github/PULL_REQUEST_TEMPLATE.md")

    assert ("doctor",) in IMPLEMENTED and not _is_stubbed(["doctor"]), "real ones are listed"
    assert "**Add the command to `IMPLEMENTED`** in `tests/test_stubs.py`" in contributing
    assert "off the stub skip-list" not in contributing
    assert "added to `IMPLEMENTED` in `tests/test_stubs.py`" in template
