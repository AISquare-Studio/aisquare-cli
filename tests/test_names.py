"""One rule for the names a person gives: a project's nickname and a Claude account's alias.

The human: names are "just a string, no convention", "rename them in place into anything
I want". Refusing every format character (Unicode's Cf) for the reviewer's real risk —
a right-to-left override, a zero-width look-alike — also refused the zero-width joiner
that holds a compound emoji together, so no technologist, rainbow flag or family. Both
callers go through ``core.names.name_flaw``; each case here runs through both.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from aisquare.core import claude_accounts as accounts_core
from aisquare.core.names import name_flaw
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import project as project_service

ZWJ = "\u200d"
TECHNOLOGIST = f"\U0001f469{ZWJ}\U0001f4bb"
RAINBOW_FLAG = f"\U0001f3f3\ufe0f{ZWJ}\U0001f308"
FAMILY = f"\U0001f468{ZWJ}\U0001f469{ZWJ}\U0001f467"
SCOTLAND = "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"
GEAR_OPS = "\u2699\ufe0e ops"

ACCEPTED = [
    pytest.param(TECHNOLOGIST, id="technologist"),
    pytest.param(RAINBOW_FLAG, id="rainbow flag"),
    pytest.param(FAMILY, id="family"),
    pytest.param(SCOTLAND, id="scotland"),
    pytest.param(GEAR_OPS, id="text gear"),
    pytest.param(f"\U0001f469\U0001f3fd{ZWJ}\U0001f4bb on call", id="skin tone"),
    pytest.param(f"{TECHNOLOGIST} payments {RAINBOW_FLAG}", id="in words"),
    pytest.param(f"\u0dc1\u0dca{ZWJ}\u0dbb\u0dd3", id="sinhala conjunct"),
    pytest.param("\u0645\u06cc\u200c\u0634\u0648\u062f", id="persian non-joiner"),
]
REFUSED = [
    pytest.param("evil\u202egnp.exe", id="right-to-left override"),
    pytest.param("a\u200bb", id="zero-width space"),
    pytest.param("\ufeffwork", id="byte-order mark"),
    pytest.param(f"{ZWJ}\U0001f469", id="joiner first"),
    pytest.param(f"\U0001f469{ZWJ}", id="joiner last"),
    pytest.param(f"w{ZWJ}ork", id="joiner between letters"),
    pytest.param(f"\U0001f469{ZWJ}{ZWJ}\U0001f4bb", id="two joiners"),
    pytest.param("work\u2066", id="bidi isolate"),
    pytest.param("a\u200eb", id="direction mark"),
    pytest.param("co\u00adop", id="soft hyphen"),
    pytest.param("work\U000e0067", id="tag outside a flag"),
    pytest.param("\u3164", id="hangul filler"),
]


@pytest.fixture(params=["nickname", "account name"])
def give(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[str], str | None]:
    """Name something by each of the two callers; what it stored (or would store) is returned."""
    if request.param == "account name":
        return lambda name: accounts_core.normalise_alias(name, slot=2)
    root = (tmp_path / "work").resolve()
    root.mkdir()
    with store_session() as store:
        project = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
    return lambda name: project_service.set_nickname(project, name).nickname


@pytest.mark.parametrize("name", ACCEPTED)
def test_any_emoji_is_a_name(give: Callable[[str], str | None], name: str) -> None:
    assert give(name) == name, "kept as typed, joiners and selectors included"


@pytest.mark.parametrize("name", REFUSED)
def test_a_character_that_hides_or_turns_the_text_is_refused(
    give: Callable[[str], str | None], name: str
) -> None:
    with pytest.raises(ValueError, match="invisible formatting character"):
        give(name)


@pytest.mark.parametrize(
    ("name", "flaw"),
    [
        ("two\nlines", "control"),
        ("a\u2028b", "control"),  # a line separator breaks the line as a newline does
        ("caf\udce9", "undecodable"),
        ("evil\u202e\nx", "control"),  # a line break is named first, wherever it is
        ("\u202ecaf\udce9", "undecodable"),
        (TECHNOLOGIST, None),
    ],
)
def test_the_rule_names_what_is_wrong(name: str, flaw: str | None) -> None:
    assert name_flaw(name) == flaw
