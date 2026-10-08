"""Documents that described the code backwards (review of #257).

docs/reference.md's StopFailure row said any API error marks a session limited. Each
test reads the document beside the code it describes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from aisquare.models import TeamSession
from aisquare.services.team import TurnFailure

ROOT = Path(__file__).resolve().parents[1]

#: Errors Claude Code's StopFailure hook hands over, and the one the hook files when
#: it names none (``services.team.hook_stop_failure``).
_ERRORS = ("rate_limit", "overloaded", "authentication_failed", "billing_error", "unknown")


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
