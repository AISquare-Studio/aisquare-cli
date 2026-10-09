"""What a phone's write may hold, and how each refusal reads (review of #243, round 5).

Text that reaches a pane, a board or a terminal is held to one rule per path: what it
would press, reorder or run there is refused before anything is sent or written. A ref
that names nothing says which field named it, and a path the operating system refuses
is a refusal of the request, never ``write_failed`` or an ambiguity.
"""

from __future__ import annotations

import pytest

from aisquare.services.remote_server import RequestError


def test_a_quick_answer_in_words_refuses_a_tab_as_typed_text_does() -> None:
    """A quick answer's words are typed into the pane as send-keys' text is, and a tab
    there is the Tab key (review of #243, round 5)."""
    from aisquare.services.remote_needs import _needs_answer_body

    with pytest.raises(RequestError) as refused:
        _needs_answer_body({"id": "nd_1", "text": "a\tb"})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == (
        "'text' holds the control character U+0009 — send the pad's Tab key instead"
    )
