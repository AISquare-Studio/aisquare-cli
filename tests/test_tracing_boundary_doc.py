"""The tracing-boundary page must stay true, not merely stay present.

``docs/explainability-tracing-boundary.md`` exists to stop an experiment being
designed against attribution that does not exist. Its whole argument rests on
one mechanical fact — identity rides in exactly these process-level environment
variables — and if the code ever carries identity somewhere else, the page's
reasoning silently stops applying while every word of it still reads fine.
A doc that can rot without anything going red is a doc nobody can trust at
08:00, so the load-bearing claim is pinned against the code here.

Prose is deliberately NOT asserted on: a test that pins wording makes the page
harder to improve without making it more correct.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.services.explainability import RESERVED_ENV_VARS

DOC = Path(__file__).resolve().parents[1] / "docs" / "explainability-tracing-boundary.md"


@pytest.fixture(scope="module")
def page() -> str:
    assert DOC.exists(), f"{DOC.name} is missing — the boundary is undocumented again"
    return DOC.read_text(encoding="utf-8")


def test_the_page_names_exactly_the_variables_identity_actually_rides_in(page: str) -> None:
    """The mechanism named in the page is the mechanism in the code.

    If a third variable ever joins the pair, or one is renamed, the page's
    "an in-process agent inherits it verbatim" argument needs re-checking
    against the new shape rather than assuming.
    """
    for name in RESERVED_ENV_VARS:
        assert name in page, (
            f"{name} carries tracing identity in wire_session but the boundary page "
            "does not mention it — the page explains a mechanism it no longer describes"
        )
    documented = {
        line.strip()
        for line in page.splitlines()
        if "ANTHROPIC_" in line and line.strip().startswith("-")
    }
    assert len(documented) == len(RESERVED_ENV_VARS), (
        "the page lists a different number of identity variables than the code sets"
    )


def test_the_page_marks_what_nobody_has_run(page: str) -> None:
    """Unverified claims stay labelled — the runner's convention, kept.

    The two separation mechanisms people reach for (per-subagent header
    override, proxy-side fingerprinting) are speculation. A reader must not be
    able to mistake either for an option, and the same goes for the two-lane
    merge nobody has yet seen happen.
    """
    assert "[unverified]" in page
    for speculation in ("header override", "fingerprinting"):
        assert speculation in page, f"the page stopped naming {speculation} as speculation"


def test_the_module_points_at_the_page() -> None:
    """A reader who lands in the code first must be sent to it."""
    from aisquare.services import explainability

    docstring = explainability.__doc__ or ""
    assert "explainability-tracing-boundary.md" in docstring
    assert "a Run is a process" in docstring
