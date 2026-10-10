"""Two documents that described the code backwards (review of #257).

docs/reference.md's StopFailure row said any API error marks a session limited, and
CONTRIBUTING's stub-to-service flow said to move a graduated command OFF the stub
test's skip-list. Each test reads the document beside the code it describes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from aisquare.models import TeamSession
from aisquare.services.team import TurnFailure
from tests.test_stubs import IMPLEMENTED, _is_stubbed

ROOT = Path(__file__).resolve().parents[1]

#: Errors Claude Code's StopFailure hook hands over, and the one the hook files when
#: it names none (``services.team.hook_stop_failure``).
_ERRORS = ("rate_limit", "overloaded", "authentication_failed", "billing_error", "unknown")


def _flat(path: str) -> str:
    """A document's text with its line breaks and indents read as single spaces."""
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def test_the_stopfailure_row_says_only_a_usage_limit_marks_a_session_limited() -> None:
    """The row said any API error marks the session limited ("a usage limit above all")
    and left out the hand-over that ``[accounts] on_limit = "switch"`` makes."""
    page = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    row = next(line for line in page.splitlines() if line.startswith("| `StopFailure` |"))
    now = datetime.now(tz=UTC)
    session = TeamSession(id="ses_x", project_id="prj_x", started_at=now, last_seen_at=now)
    limited = [kind for kind in _ERRORS if TurnFailure(session, kind, None).limited]

    assert limited == ["rate_limit"], "the code's rule, which the row describes"
    assert "a usage limit (`rate_limit`) marks the session `limited`" in row, row
    assert "any other error marks it `waiting`" in row
    assert 'with `[accounts] on_limit = "switch"` a fleet agent is handed to another' in row
    assert "above all" not in row


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
