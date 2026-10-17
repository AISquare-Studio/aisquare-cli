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
HEAD_SHAKING = f"\U0001f642{ZWJ}\u2194\ufe0f"
"""RGI since Emoji 15.1: the part after its joiner is an arrow (Sm) made an emoji by U+FE0F."""

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
    pytest.param(HEAD_SHAKING, id="head shaking"),
    pytest.param("1\ufe0f\u20e3 ops", id="keycap"),
    pytest.param("\u203c\ufe0f urgent", id="double exclamation"),
    pytest.param("\u2764\ufe0f", id="red heart"),
    # Not symbols (a letter, punctuation), yet each has an emoji presentation (re-check of N3).
    pytest.param("\u2139\ufe0f info", id="information"),
    pytest.param("\u3030\ufe0f", id="wavy dash"),
    pytest.param("\u303d\ufe0e ops", id="part alternation mark"),
    # A selector that picks a glyph where a script has them (review of #259, round 33, 10/10).
    pytest.param("\u845b\U000e0100\u98fe", id="ideographic selector after a han ideograph"),
    pytest.param("\u8fbb\ufe00", id="standardized selector after a han ideograph"),
    pytest.param("\u1820\u180b\u1828", id="mongolian selector after a mongolian letter"),
    # A format character that is drawn: a sign over what follows (review of #259, round 37).
    pytest.param("\u0600\u0661\u0662", id="arabic number sign over a number"),
    pytest.param("ayah \u06dd\u0661", id="end of ayah over its number"),
    pytest.param("\u070f\u0710\u0712", id="syriac abbreviation mark over letters"),
    pytest.param("\U000110bd\u0967", id="kaithi number sign over a digit"),
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
    # A presentation selector after what has no emoji presentation prints as nothing:
    # `work` U+FE0F reads `work` (review of #261, N3).
    pytest.param("work\ufe0f", id="emoji selector after a letter"),
    pytest.param("work\ufe0e", id="text selector after a letter"),
    pytest.param("ops 1\ufe0f", id="digit without its keycap"),
    pytest.param("!\ufe0f", id="punctuation with no emoji"),
    # Every other selector, which is Mn, not Cf, after what it picks no form of (round 33).
    pytest.param("work\ufe00", id="standardized selector after a letter"),
    pytest.param("ops 1\ufe0d", id="standardized selector after a digit"),
    pytest.param("work\U000e0100", id="ideographic selector after a letter"),
    pytest.param("work\u180b", id="mongolian selector after a latin letter"),
    pytest.param("\u845b\ufe0f", id="emoji selector after a han ideograph"),
    pytest.param("\u845b\U000e0100\U000e0101", id="a second selector"),
    pytest.param("\ufe00work", id="selector first"),
    # A prepended mark with nothing after it to be drawn over (round 37, 5/7).
    pytest.param("ops \u0600", id="number sign last"),
    pytest.param("\u06dd ops", id="end of ayah before a space"),
    pytest.param("\u0600\u200b1", id="number sign over a zero-width space"),
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


def test_a_name_that_differs_only_by_a_variation_selector_is_taken(fake_home: Path) -> None:
    """A variation selector after a Han ideograph is not refused — it picks the glyph a name
    is registered in — but it changes how a character is drawn, never which one it is:
    ``work`` U+FE00 beside ``work`` printed as one name, and ``resolve("work")`` picked slot
    3 (review of #261, N3). Compared without it (``core.names.name_key``), it is taken.
    After a letter it is refused outright since round 33 (10/10), and a ref typed with one
    still finds the name."""
    from aisquare.services import claude_accounts as accounts_service

    accounts_core.create_account()  # slot 2
    accounts_core.create_account()  # slot 3
    accounts_service.set_alias("2", "\u845b work")
    with pytest.raises(accounts_service.AccountsError, match="already taken"):
        accounts_service.set_alias("3", "\u845b\U000e0100 work")
    assert accounts_service.resolve("\u845b work").slot == 2
    assert accounts_service.resolve("\u845b\U000e0100 WORK\ufe00").slot == 2  # found all the same


@pytest.mark.parametrize(
    ("name", "why"),
    [
        # Refused as what draws nothing since round 33 (10/10): a selector after a digit or
        # a letter picks no form. Either way the alias is not stored.
        pytest.param("account 3\ufe00", "invisible formatting", id="slot 3's label"),
        pytest.param("ACCOUNT 3\U000e0100", "invisible formatting", id="ideographic selector"),
        pytest.param("Plain Claude\ufe00", "invisible formatting", id="slot 1's label"),
        pytest.param("3\ufe00", "reads as a slot number", id="a slot number"),
    ],
)
def test_an_alias_cannot_be_another_slots_name_behind_a_variation_selector(
    fake_home: Path, name: str, why: str
) -> None:
    """``account 3`` U+FE00 prints as slot 3's label, and ``resolve("account 3")`` then
    landed on slot 2: the label rule compared case alone (re-check of N3). It compares as
    every other name check does now, without the selector (``core.names.name_key``)."""
    from aisquare.services import claude_accounts as accounts_service

    accounts_core.create_account()  # slot 2
    accounts_core.create_account()  # slot 3
    with pytest.raises(ValueError, match=why):
        accounts_core.normalise_alias(name, slot=2)
    with pytest.raises(ValueError, match=why):
        accounts_service.set_alias("2", name)
    assert accounts_service.resolve("2").alias is None, "nothing stored"
    with pytest.raises(accounts_service.NoSuchAccount):
        accounts_service.resolve("account 3")
