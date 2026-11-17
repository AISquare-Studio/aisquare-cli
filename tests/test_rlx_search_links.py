"""PyPI's sidebar and the README's first lines point at rlx's product page and docs (RLX R11).

The owner wants rlx found first for "rlx", "RLX", "relax ai" and "relax ai agents". PyPI's
project page lists ``[project.urls]`` in its sidebar and indexes the keywords, and the README is
that page's body. So:

- Homepage is rlx's product page and Documentation its docs: exactly these two URLs;
- the keywords name rlx and relax;
- the README links the product page once, near its top, with the anchor text "rlx".

Both URLs answer 404 until AISquare-Homepage-Next #190 deploys and AISquare-Studio/docs#98
merges, which the owner does at release, before #503. Nothing here fetches them: the pins are
the text.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PRODUCT_PAGE = "https://aisquare.studio/rlx"
DOCS = "https://docs.aisquare.studio/docs/rlx/overview/"

#: A markdown link, inline form: its text and its target.
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _project() -> dict[str, object]:
    project: dict[str, object] = tomllib.loads(
        (REPO / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    return project


def _product_links(markdown: str) -> list[tuple[int, str]]:
    """Each link to the product page, as ``(line number, anchor text)``."""
    return [
        (number, text)
        for number, line in enumerate(markdown.splitlines(), start=1)
        for text, target in _LINK.findall(line)
        if target.rstrip("/") == PRODUCT_PAGE
    ]


def _opening(markdown: str) -> int:
    """How many lines come before the first section heading: the README's first lines."""
    for number, line in enumerate(markdown.splitlines(), start=1):
        if line.startswith("## "):
            return number - 1
    return len(markdown.splitlines())


# --- pyproject ---------------------------------------------------------------------------------


def test_pypi_s_sidebar_links_rlx_s_product_page_and_docs() -> None:
    assert _project()["urls"] == {
        "Homepage": PRODUCT_PAGE,
        "Documentation": DOCS,
        "Repository": "https://github.com/AISquare-Studio/rlx",
        "Issues": "https://github.com/AISquare-Studio/rlx/issues",
        "Changelog": "https://github.com/AISquare-Studio/rlx/blob/main/CHANGELOG.md",
    }


def test_the_keywords_name_rlx_and_relax() -> None:
    keywords = _project()["keywords"]
    assert isinstance(keywords, list)
    assert {"rlx", "relax"} <= set(keywords) and "cli" not in keywords


# --- the README --------------------------------------------------------------------------------


def test_the_readme_links_the_product_page_once_as_rlx_in_its_first_lines() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    links = _product_links(readme)
    assert len(links) == 1, f"one link to {PRODUCT_PAGE}, not {links}"
    ((line, text),) = links
    assert text == "rlx", f"its anchor text is {text!r}"
    assert line <= _opening(readme), f"line {line} is past the README's opening lines"


# --- controls: synthetic input, so they keep controlling whatever the README says ---------------


def test_the_product_link_is_found_by_its_target_and_read_with_its_text() -> None:
    page = (
        "# rlx\n\n[rlx](https://aisquare.studio/rlx) is short for relax.\n\n## Install\n\n"
        "See [the site](https://aisquare.studio/rlx/) or [AISquare](https://aisquare.studio).\n"
    )
    assert _product_links(page) == [(3, "rlx"), (7, "the site")]
    assert _opening(page) == 4
    assert _product_links("[rlx](https://aisquare.studio)\n[docs](" + DOCS + ")\n") == []
    assert _opening("no heading\n") == 1
