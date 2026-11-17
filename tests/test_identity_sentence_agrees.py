"""What rlx is, said in four places in the page's voice, and they must agree.

The page (aisquare.studio/rlx, src/config/rlx.ts) leads with an H1, "Tell the manager the
goal. Then relax.", and a pitch, ``RLX_PITCH``: "rlx is a manager for your AI coding agents.
Tell it the goal: it runs Claude Code and Codex on accounts with room. Then relax." (RLX R14b,
the devrel review's launch gate, item 1).

- The PITCH is said word for word in three places: PyPI's summary (pyproject's
  ``description``), the header of ``rlx --help`` (the root callback's docstring) and the
  package docstring. PyPI's summary is the reference, because it is the one a release
  freezes.
- The README leads as the page does: its bold line is the H1, and its lede, the paragraph
  under it, says what rlx is in the pitch's first sentence ("rlx is a manager for your AI
  coding agents"), its product link read as the link's text.
- The H1 and the pitch end on the same words ("Then relax."), so the README's headline and
  PyPI's summary cannot drift into two voices, as the README and PyPI once did before 0.8
  ("portable memory layer" against the terminal).

Wrapping and capitals are not disagreements: a docstring wraps at 100 columns.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from typer.main import get_command

import aisquare
from aisquare.cli.app import app

REPO = Path(__file__).resolve().parents[1]
REFERENCE = "pyproject.toml description"
README_H1 = "README.md bold line"
README_LEDE = "README.md lede"
#: The page's H1 (src/config/rlx.ts, feat/rlx-page), which the README's bold line is.
HEADLINE = "Tell the manager the goal. Then relax."

_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()


def _readme_sentence(markdown: str) -> str:
    """The README's identity line: its first paragraph set in bold."""
    match = re.search(r"^\*\*(.+?)\*\*$", markdown, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _readme_lede(markdown: str) -> str:
    """The paragraph under the bold line, its links read as their text."""
    match = re.search(r"^\*\*.+?\*\*$\n\n(.+?)(?:\n\n|\Z)", markdown, re.MULTILINE | re.DOTALL)
    return _LINK.sub(r"\1", match.group(1)) if match else ""


def _first_paragraph(text: str) -> str:
    return text.strip().split("\n\n", 1)[0]


def _first_sentence(text: str) -> str:
    """Up to the first full stop, without it."""
    return text.split(".", 1)[0]


def _last_sentence(text: str) -> str:
    return text.rstrip().rstrip(".").rsplit(".", 1)[-1].strip()


def _sentences(readme: str, pyproject: str, help_text: str, docstring: str) -> dict[str, str]:
    """What each place says, read the way a reader meets it."""
    return {
        README_H1: _readme_sentence(readme),
        README_LEDE: _readme_lede(readme),
        REFERENCE: tomllib.loads(pyproject)["project"]["description"],
        "rlx --help": _first_paragraph(help_text),
        "aisquare/__init__.py": _first_paragraph(docstring),
    }


def _disagreements(sentences: Mapping[str, str], headline: str = HEADLINE) -> list[str]:
    """The places that do not agree with PyPI's pitch, or that say nothing."""
    pitch = _normal(sentences[REFERENCE])
    wrong = {
        where
        for where in ("rlx --help", "aisquare/__init__.py")
        if not sentences[where] or _normal(sentences[where]) != pitch
    }
    if _normal(sentences[README_H1]) != _normal(headline):
        wrong.add(README_H1)
    if not _normal(sentences[README_LEDE]).startswith(_first_sentence(pitch)):
        wrong.add(README_LEDE)
    if _last_sentence(_normal(headline)) != _last_sentence(pitch):
        wrong.add(REFERENCE)
    return sorted(wrong)


def test_what_rlx_is_agrees_everywhere() -> None:
    sentences = _sentences(
        (REPO / "README.md").read_text(encoding="utf-8"),
        (REPO / "pyproject.toml").read_text(encoding="utf-8"),
        get_command(app).help or "",
        aisquare.__doc__ or "",
    )
    assert sentences[REFERENCE], "pyproject has no description to compare against"

    disagreeing = _disagreements(sentences)

    assert not disagreeing, (
        f"these places do not agree with PyPI's pitch ({sentences[REFERENCE]!r}) and the "
        f"page's H1 ({HEADLINE!r}): "
        + "; ".join(f"{where}: {sentences[where]!r}" for where in disagreeing)
    )


# --- controls: synthetic input, so they keep controlling whatever the words become ---

PITCH = "X is a tool for Y. Tell it the goal. Then relax."
AGREEING = {
    README_H1: "Do the thing. Then relax.",
    README_LEDE: "X is a tool for Y: A and B.",
    REFERENCE: PITCH,
    "rlx --help": PITCH,
    "aisquare/__init__.py": PITCH,
}


def test_places_that_agree_pass_and_each_kind_of_drift_is_named() -> None:
    assert _disagreements(AGREEING, headline="Do the thing. Then relax.") == []
    drifted = {
        **AGREEING,
        "rlx --help": "Portable memory layer for coding agents.",
        "aisquare/__init__.py": "",
        README_H1: "Something else entirely.",
        README_LEDE: "Y is what X is for.",
    }
    assert _disagreements(drifted, headline="Do the thing. Then relax.") == [
        README_H1,
        README_LEDE,
        "aisquare/__init__.py",
        "rlx --help",
    ]


def test_a_headline_that_ends_in_another_voice_than_the_pitch_is_named() -> None:
    other = {**AGREEING, README_H1: "Do the thing. Ship it."}
    assert _disagreements(other, headline="Do the thing. Ship it.") == [REFERENCE]


def test_wrapping_and_capitals_are_not_disagreements() -> None:
    sentences = {
        **AGREEING,
        README_H1: "Do the\nthing. Then relax.",
        README_LEDE: "x is a tool\nfor y: a and b.",
        "rlx --help": "X is a tool for Y.   Tell it the\n    goal. Then relax.",
        "aisquare/__init__.py": "x is a tool for y. tell it the goal. then relax.",
    }
    assert _disagreements(sentences, headline="Do the thing. Then relax.") == []


def test_each_place_is_read_where_a_reader_meets_it() -> None:
    readme = (
        "# rlx\n\n[![b](https://x/b.svg)](https://x)\n\n**Do the\nthing.**\n\n"
        "[X](https://x/x) is a tool for Y.\n\n```sh\ncurl x | sh\n```\n\nMore **bold**.\n"
    )
    pyproject = '[project]\ndescription = "X is a tool."\n'
    sentences = _sentences(readme, pyproject, "X is a tool.\n\nMore.", "X is a\ntool.\n\nMore.")

    assert sentences == {
        README_H1: "Do the\nthing.",
        README_LEDE: "X is a tool for Y.",
        REFERENCE: "X is a tool.",
        "rlx --help": "X is a tool.",
        "aisquare/__init__.py": "X is a\ntool.",
    }
