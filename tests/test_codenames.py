"""Fleet codenames: two curated word lists and a deterministic picker (plan §5.7).

The lists are data with rules — charset, order, uniqueness — and each rule has
a control here that proves the check would fire, because a per-word rule over
an empty list and a sortedness check that never sees a shuffle both pass for
free (CONTRIBUTING, "Writing a guard that still guards").
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from aisquare.core import codenames
from aisquare.core.codenames import ADJECTIVES, ANIMALS, CODENAME, WORD, codename_for, is_codename

_TOTAL = len(ADJECTIVES) * len(ANIMALS)
_EVERY = frozenset(codenames._pair(index) for index in range(_TOTAL))


def _is_sorted_unique(words: Sequence[str]) -> bool:
    return list(words) == sorted(set(words))


# --- the lists ----------------------------------------------------------------------


@pytest.mark.parametrize("words", [ADJECTIVES, ANIMALS], ids=["adjectives", "animals"])
def test_every_word_matches_the_charset(words: tuple[str, ...]) -> None:
    assert words, "an empty list satisfies any per-word rule — assert there IS a list first"
    offenders = [word for word in words if WORD.match(word) is None]
    assert offenders == [], offenders


def test_the_word_rule_rejects_what_it_claims_to() -> None:
    """Positive control for the charset: uppercase, too short, too long, punctuation."""
    for bad in ("Amber", "ab", "abcdefgh", "am-ber", "amber1", ""):
        assert WORD.match(bad) is None, bad
    assert WORD.match("amber") is not None


@pytest.mark.parametrize("words", [ADJECTIVES, ANIMALS], ids=["adjectives", "animals"])
def test_the_lists_are_sorted_and_unique(words: tuple[str, ...]) -> None:
    assert _is_sorted_unique(words)


def test_the_order_rule_sees_a_shuffle_and_a_duplicate() -> None:
    """Positive control for the sortedness rule, both shapes it must catch."""
    assert _is_sorted_unique(("amber", "azure"))
    assert not _is_sorted_unique(("azure", "amber"))
    assert not _is_sorted_unique(("amber", "amber"))


def test_the_lists_give_thousands_of_codenames() -> None:
    """The plan asks for ~96 words each — about 9,200 names (§5.7)."""
    assert len(ADJECTIVES) >= 90 and len(ANIMALS) >= 90
    assert _TOTAL >= 9000


def test_every_generated_codename_has_the_codename_shape() -> None:
    """Checked on the product itself, not inferred from the two word rules."""
    assert all(CODENAME.match(name) for name in _EVERY)
    assert len(_EVERY) == _TOTAL, "two index positions produced the same name"


# --- the picker ---------------------------------------------------------------------


def test_codename_for_is_deterministic_and_drawn_from_the_lists() -> None:
    seed = "prj_0123456789abcdef"
    first = codename_for(seed)
    assert first == codename_for(seed)
    adjective, animal = first.split("-")
    assert adjective in ADJECTIVES and animal in ANIMALS
    assert is_codename(first)


def test_different_seeds_spread_over_the_space() -> None:
    names = {codename_for(f"prj_{index:04d}") for index in range(50)}
    # 50 draws from 9,000+ names: a handful of collisions is possible, one name is not.
    assert len(names) > 40


def test_a_taken_name_walks_to_the_next_pair() -> None:
    seed = "prj_walk"
    first = codename_for(seed)
    second = codename_for(seed, taken={first})
    assert second != first
    assert second == codenames._pair((codenames._start_index(seed) + 1) % _TOTAL)
    # Negative control: an unrelated taken name changes nothing.
    assert codename_for(seed, taken={"zzz-zzz"}) == first


def test_the_walk_wraps_around_to_the_last_free_name() -> None:
    seed = "prj_wrap"
    last_free = codenames._pair((codenames._start_index(seed) - 1) % _TOTAL)
    assert codename_for(seed, taken=_EVERY - {last_free}) == last_free


def test_every_name_taken_raises() -> None:
    with pytest.raises(RuntimeError, match="every codename is taken"):
        codename_for("prj_full", taken=_EVERY)


# --- the shape ------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["amber-otter", "ruby-fox", "scarlet-narwhal", "zzz-zzz"])
def test_is_codename_accepts_the_shape(text: str) -> None:
    assert is_codename(text)


@pytest.mark.parametrize(
    "text",
    [
        "Amber-Otter",
        "amber_otter",
        "amber-otter-x",
        "ab-otter",
        "amber-otterfish",
        "amber otter",
        "amber",
        "",
    ],
)
def test_is_codename_rejects_other_shapes(text: str) -> None:
    assert not is_codename(text)
