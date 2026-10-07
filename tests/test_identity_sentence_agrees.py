"""The identity sentence is said in four places, and they must say the same thing.

The README's first line, the PyPI summary (pyproject's ``description``), the
header of ``aisquare --help`` (the root callback's docstring) and the package
docstring. They had drifted into two identities before 0.9: PyPI and ``--help``
said "portable memory layer", while the README led with the terminal. The owner
picks the final words, and this guard turns that choice into one edit per place
that fails when a place is missed.

PyPI's summary is the reference, because it is the one a release freezes.
Wrapping and capitals are not disagreements: a docstring wraps at 100 columns,
and the package docstring starts with "aisquare — ".
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


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()


def _readme_sentence(markdown: str) -> str:
    """The README's identity line: its first paragraph set in bold."""
    match = re.search(r"^\*\*(.+?)\*\*$", markdown, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _first_paragraph(text: str) -> str:
    return text.strip().split("\n\n", 1)[0]


def _sentences(readme: str, pyproject: str, help_text: str, docstring: str) -> dict[str, str]:
    """What each place says, read the way a reader meets it."""
    return {
        "README.md": _readme_sentence(readme),
        REFERENCE: tomllib.loads(pyproject)["project"]["description"],
        "aisquare --help": _first_paragraph(help_text),
        "aisquare/__init__.py": _first_paragraph(docstring).removeprefix("aisquare — "),
    }


def _disagreements(sentences: Mapping[str, str]) -> list[str]:
    """The places whose sentence differs from PyPI's, or that say nothing."""
    expected = _normal(sentences[REFERENCE])
    return sorted(
        where for where, said in sentences.items() if not said or _normal(said) != expected
    )


def test_the_identity_sentence_agrees_everywhere() -> None:
    sentences = _sentences(
        (REPO / "README.md").read_text(encoding="utf-8"),
        (REPO / "pyproject.toml").read_text(encoding="utf-8"),
        get_command(app).help or "",
        aisquare.__doc__ or "",
    )
    assert sentences[REFERENCE], "pyproject has no description to compare against"

    disagreeing = _disagreements(sentences)

    assert not disagreeing, (
        f"these places do not say PyPI's sentence ({sentences[REFERENCE]!r}): "
        + "; ".join(f"{where}: {sentences[where]!r}" for where in disagreeing)
    )


# --- controls: synthetic input, so they keep controlling whatever the words become ---


def test_a_place_that_says_something_else_is_named() -> None:
    same = "One terminal over X: task a manager."
    sentences = {
        "README.md": same,
        REFERENCE: same,
        "aisquare --help": "Portable memory layer for coding agents.",
        "aisquare/__init__.py": "",
    }

    assert _disagreements(sentences) == ["aisquare --help", "aisquare/__init__.py"]


def test_wrapping_and_capitals_are_not_disagreements() -> None:
    sentences = {
        "README.md": "One terminal\nover X: task a manager.",
        REFERENCE: "One terminal over X: task a manager.",
        "aisquare --help": "One terminal over X:   task a\n    manager.",
        "aisquare/__init__.py": "one terminal over X: task a manager.",
    }

    assert _disagreements(sentences) == []


def test_each_place_is_read_where_a_reader_meets_it() -> None:
    readme = (
        "# aisquare\n\n[![b](https://x/b.svg)](https://x)\n\n**One\nterminal.**\n\nMore **bold**.\n"
    )
    pyproject = '[project]\ndescription = "One terminal."\n'
    sentences = _sentences(
        readme, pyproject, "One terminal.\n\nMore.", "aisquare — One\nterminal.\n\nMore."
    )

    assert sentences == {
        "README.md": "One\nterminal.",
        REFERENCE: "One terminal.",
        "aisquare --help": "One terminal.",
        "aisquare/__init__.py": "One\nterminal.",
    }
