"""The README is the front page twice: on GitHub, and as the PyPI project page.

PyPI renders README.md as the long description (pyproject's ``readme``) and
resolves nothing against the repository. A relative link or image there points
at pypi.org and 404s; the 0.7.0 page carried eleven of them (README lines 6, 21,
100, 147, 157, 158, 167, 860, 861, 876 and 881 at 53385d4f, three of them bare
``#anchors``). And a long description is fixed per release: a broken one stays
on that version's page for good.

It is also the page a stranger reads first, so roadmap 9.5 holds it to 250
lines and moves the long form into ``docs/``. Five rules, each a callable that
the controls below reach with known input (CONTRIBUTING, "Writing a guard that
still guards"):

- ``_too_long`` — more than 250 lines.
- ``_relative_targets`` — every link and image target, markdown or HTML, must
  be an absolute ``http(s)`` or ``mailto`` URL.
- ``_unresolved_repo_links`` — every URL into this repository's ``main`` must
  name a file in this checkout, and a ``#fragment`` on a Markdown page one of
  that page's headings. Absolute links are what PyPI needs and what nothing
  else checks: without this, renaming a docs page leaves the README on ``main``
  pointing at a 404. Fenced code is read here, because the one-liner's URL
  lives in a fence and is the most-copied line on the page.
- ``_stale_pending`` — every excused file is still linked (below).
- ``_unresolved_relative_links`` — the pages the README's long form moved into,
  and every other page under ``docs/`` and CONTRIBUTING, link each other
  relatively; every such link must name a file, and an anchor a heading.

CommonMark IS A PARSER'S JOB, NOT THIS FILE'S. What is code, what is a link,
and where a block ends are read from markdown-it-py's CommonMark rendering, the
same blocks cmark-gfm builds for GitHub and PyPI (markdown-it-py comes with
rich, a core dependency). Review found a blind spot in every hand-written copy:
a fence closed by its list item, a code span wrapped onto the next line.

WHAT THIS CANNOT PROTECT. It reads this checkout. A release's PyPI page keeps
the README it shipped with, and its ``blob/main`` links follow ``main``, so
renaming a page the README links still breaks every older release page. Leave a
stub at the old path when you rename one.

PENDING FILES, AND WHY THE EXCUSE EXPIRES. A link on the README may name a file
another lane of the same release commits (the demo GIF was one until it
landed; the Claude Code plugin page is one now). Such a file is excused only
while pyproject's version is still the one
this branch was cut at, ``_PENDING_WHILE_VERSION``. The release commit bumps
the version, so a slipped GIF or plugin page fails that commit's CI instead of
shipping a broken image or a dead link to PyPI. Each entry must still be
linked, so the list cannot outlive the link it excuses.

ANY bump ends the excuse, on purpose. A release merged in from ``main`` before
both files land (a 0.8.0, say) also turns this test red; that merge commit then
moves ``_PENDING_WHILE_VERSION`` to the merged version. A bound such as "below
0.9.0" would not need that edit, and was rejected: the release's number is the
owner's call, and if it is 0.8.0 that bound would excuse a missing file on the
release commit itself. Ending early is the safe direction: this branch goes
red, never a PyPI page.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from urllib.parse import unquote

import pytest
from markdown_it import MarkdownIt

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"

#: Roadmap 9.5: "README under 250 lines".
MAX_LINES = 250

#: Files the README links before they exist, with who commits them. Excused only
#: while the version is ``_PENDING_WHILE_VERSION`` (see the module docstring).
_PENDING: dict[str, str] = {
    "docs/claude-code-plugin.md": "the plugin route's page; roadmap 9.3 writes it",
}
_PENDING_WHILE_VERSION = "0.7.0"

#: CommonMark with tables, as GitHub and PyPI render a README (see the docstring).
_COMMONMARK = MarkdownIt("commonmark").enable("table")
_ABSOLUTE = re.compile(r"^(?:https?://|mailto:)", re.IGNORECASE)
#: The two spellings of a link into this repository's `main` the README uses.
_BLOB = "https://github.com/AISquare-Studio/aisquare-cli/blob/main"
_RAW = "https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main"
#: A URL into this repository's `main`, on github.com or as a raw download.
_REPO_URL = re.compile(
    r"https://(?:github\.com/AISquare-Studio/aisquare-cli/(?:blob|tree|raw)/main"
    r"|raw\.githubusercontent\.com/AISquare-Studio/aisquare-cli/(?:refs/heads/)?main)"
    r"/([^\s)\"'<>\]`]+)",
    re.IGNORECASE,
)


def _too_long(text: str) -> bool:
    return len(text.splitlines()) > MAX_LINES


class _LinkTargets(HTMLParser):
    """Every ``href`` and ``src`` in rendered HTML, raw HTML in the page included."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.extend(value or "" for name, value in attrs if name in {"href", "src"})


def _targets(text: str) -> list[str]:
    """Every link and image target a renderer resolves: code is text, not a link."""
    reader = _LinkTargets()
    reader.feed(_COMMONMARK.render(text))
    reader.close()
    return reader.found


def _relative_targets(text: str) -> list[str]:
    return [target for target in _targets(text) if not _ABSOLUTE.match(target)]


def _repo_links(text: str) -> list[tuple[str, str]]:
    """(path, fragment) for every URL into this repository's `main`, code included."""
    found: list[tuple[str, str]] = []
    for match in _REPO_URL.finditer(text):
        address = match.group(1).rstrip(".,;:")
        address = address.split("?", 1)[0]
        path, _, fragment = address.partition("#")
        found.append((unquote(path).rstrip("/"), fragment))
    return found


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    return re.sub(r"[^\w\- ]", "", text.replace("`", "").lower()).replace(" ", "-")


def _anchors(markdown: str) -> set[str]:
    """The anchors GitHub gives a page's headings, numbered when they repeat."""
    seen: dict[str, int] = {}
    found: set[str] = set()
    tokens = _COMMONMARK.parse(markdown)
    # A heading is `heading_open`, then the inline token that holds its text.
    for opening, inline in pairwise(tokens):
        if opening.type != "heading_open":
            continue
        slug = _slug(inline.content)
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def _unresolved_repo_links(
    text: str, root: Path, *, version: str, pending: Mapping[str, str]
) -> list[str]:
    """Repo links that name nothing in `root`, or a heading their page lacks."""
    unresolved: list[str] = []
    for path, fragment in _repo_links(text):
        target = root / path
        if not target.exists():
            if path in pending and version == _PENDING_WHILE_VERSION:
                continue
            unresolved.append(f"{path} does not exist")
            continue
        if (
            fragment
            and target.suffix == ".md"
            and fragment not in _anchors(target.read_text(encoding="utf-8"))
        ):
            unresolved.append(f"{path}#{fragment} names no heading on that page")
    return unresolved


def _stale_pending(text: str, pending: Mapping[str, str]) -> list[str]:
    """Excused files the text no longer links: an excuse must not outlive its link."""
    linked = {path for path, _fragment in _repo_links(text)}
    return sorted(path for path in pending if path not in linked)


#: Every page under docs/, and CONTRIBUTING: a glob rather than a list, so a page
#: another lane adds is read the day it lands.
_LINKING_PAGES = sorted([*REPO.glob("docs/**/*.md"), REPO / "CONTRIBUTING.md"])


def _unresolved_relative_links(text: str, page: Path) -> list[str]:
    """Relative targets on `page` that name no file, or a heading their page lacks."""
    unresolved: list[str] = []
    for target in _relative_targets(text):
        address, _, fragment = target.partition("#")
        path = unquote(address.split("?", 1)[0])
        destination = page.parent / path if path else page
        if not destination.exists():
            unresolved.append(f"{target}: no such file")
        elif (
            fragment
            and destination.suffix == ".md"
            and fragment not in _anchors(destination.read_text(encoding="utf-8"))
        ):
            unresolved.append(f"{target}: no such heading")
    return unresolved


def _version() -> str:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    version: str = pyproject["project"]["version"]
    return version


def test_the_readme_fits_on_a_front_page() -> None:
    text = README.read_text(encoding="utf-8")

    assert not _too_long(text), (
        f"README.md is {len(text.splitlines())} lines; the front page holds {MAX_LINES}. "
        "Move the long form into a docs/ page and link it with an absolute URL."
    )


def test_every_readme_link_and_image_is_absolute() -> None:
    text = README.read_text(encoding="utf-8")
    assert _targets(text), "no link or image found in the README — the extractor went blind"

    relative = _relative_targets(text)

    assert not relative, (
        f"README.md links {relative} relatively. PyPI resolves these against "
        "pypi.org, so they 404 on the project page; use "
        "https://github.com/AISquare-Studio/aisquare-cli/blob/main/<path>, or "
        "https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/<path> "
        "for an image."
    )


def test_every_repo_link_in_the_readme_resolves() -> None:
    text = README.read_text(encoding="utf-8")
    links = _repo_links(text)
    # Counted a second, independent way: a plain substring count of each URL
    # form. If either half of _REPO_URL goes blind, the regex reads fewer links
    # than the text plainly holds.
    written = text.count(_BLOB + "/") + text.count(_RAW + "/")
    assert written and len(links) >= written, (
        f"the README holds {written} links into this repo but only {len(links)} were "
        "read — the URL pattern went blind, so the check below is vacuous"
    )
    assert "install.sh" in {path for path, _fragment in links}, (
        "the one-liner's URL was not read as a repo link"
    )

    unresolved = _unresolved_repo_links(text, REPO, version=_version(), pending=_PENDING)

    assert not unresolved, (
        "README.md links into this repo at paths that do not resolve:\n  "
        + "\n  ".join(unresolved)
        + "\nOn PyPI and GitHub these are a dead link or a broken image. Either land "
        "the file, or drop the line from the README. A file another lane is still "
        f"committing is excused in _PENDING only while the version is "
        f"{_PENDING_WHILE_VERSION}; if a merge from main moved the version before the "
        "file landed, move _PENDING_WHILE_VERSION in that merge."
    )


def test_each_pending_file_is_still_linked_from_the_readme() -> None:
    """The other direction, so an excuse cannot outlive the link it excuses."""
    stale = _stale_pending(README.read_text(encoding="utf-8"), _PENDING)

    assert not stale, f"_PENDING excuses files the README no longer links: {stale}"


def test_every_relative_link_in_the_docs_resolves() -> None:
    """The long form links itself relatively; a renamed page or heading must not dangle."""
    read = 0
    unresolved: list[str] = []
    for page in _LINKING_PAGES:
        text = page.read_text(encoding="utf-8")
        read += len(_relative_targets(text))
        name = page.relative_to(REPO).as_posix()
        unresolved += [f"{name}: {item}" for item in _unresolved_relative_links(text, page)]
    assert read, "no relative link read under docs/ or in CONTRIBUTING — the reader went blind"

    assert not unresolved, "relative links that resolve to nothing:\n  " + "\n  ".join(unresolved)


# --- controls: synthetic input, so they keep controlling whatever the README says ---


def test_a_readme_over_the_line_budget_is_caught() -> None:
    at_the_budget = "line\n" * MAX_LINES

    assert not _too_long(at_the_budget), "a README exactly at the budget was accused"
    assert _too_long(at_the_budget + "one more\n"), "a README one line over passed"


_RELATIVE_SHAPES = {
    "relative link": "See [the guide](docs/fleet.md).",
    "relative image": "![demo](docs/demo.gif)",
    "badge whose link is relative": "[![License](https://img.shields.io/x.svg)](LICENSE)",
    "bare anchor": "Read [part one](#part-1--memory-start-here) first.",
    "reference-style link": "[guide]: docs/fleet.md\n\nSee [guide][].",
    "html image": '<img src="docs/demo.gif" alt="demo">',
    "html image, unquoted": "<img src=docs/demo.gif alt=demo>",
    "html link": '<a href="CONTRIBUTING.md">contributing</a>',
    # What is code is CommonMark's call: misread it, and every link after the
    # block passes unread.
    "after a fence that holds a shorter fence": "````\n```\n````\nSee [x](docs/fleet.md).",
    "after a fence holding a line with an info string": "```\n```sh\n```\nSee [x](docs/fleet.md).",
    "after a line that only looks like a fence": "``` not`a fence\nSee [x](docs/fleet.md).",
    "after a code span wrapped onto the next line": (
        "Run `aisquare\ninit` and see [x](docs/fleet.md) then `asq`."
    ),
    "after a stray backtick and a blank line": "A stray `\n\nSee [x](docs/fleet.md) and `foo`.",
    "after a list item that leaves a fence open": (
        "1. step:\n\n   ```sh\n   asq\n2. See [x](docs/fleet.md).\n"
    ),
    "after a fence line inside an HTML comment": "<!--\n```sh\n-->\nSee [x](docs/fleet.md).\n",
}


@pytest.mark.parametrize("shape", sorted(_RELATIVE_SHAPES))
def test_each_relative_shape_is_caught(shape: str) -> None:
    """Positive control, one per shape, so a failure names the shape that went blind."""
    relative = _relative_targets(_RELATIVE_SHAPES[shape])

    assert relative, f"{shape}: a relative target went unreported"


_ABSOLUTE_SHAPES = {
    "absolute link": "See [the guide](https://github.com/AISquare-Studio/aisquare-cli/blob/main/docs/fleet.md).",
    "absolute image": "![demo](https://raw.githubusercontent.com/AISquare-Studio/aisquare-cli/main/docs/demo.gif)",
    "badge": "[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)",
    "mail link": "[mail us](mailto:security@example.com)",
    "a relative link inside a fence": "```markdown\nSee [the guide](docs/fleet.md).\n```",
    "a relative link inside a fence that holds a shorter fence": (
        "````markdown\n```\nSee [the guide](docs/fleet.md).\n```\n````"
    ),
    "a relative link inside inline code": "Write `[the guide](docs/fleet.md)` in a doc page.",
    "a relative link inside a code span that wraps": (
        "Write `[the guide](docs/fleet.md)\nlike this` in a doc page."
    ),
    "an absolute link whose query says src=": "[x](https://example.com/page?src=readme)",
}


@pytest.mark.parametrize("shape", sorted(_ABSOLUTE_SHAPES))
def test_correct_targets_are_not_accused(shape: str) -> None:
    """Negative control: "accuse everything" must not be a way to pass."""
    relative = _relative_targets(_ABSOLUTE_SHAPES[shape])

    assert relative == [], f"{shape}: a correct target was accused: {relative}"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A checkout with one page, one script and nothing else."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "install.md").write_text(
        "# Install\n\n## Start the GUI\n\n```sh\n# not a heading\n```\n\n"
        "````markdown\n```\n# nested comment\n```\n````\n",
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    return tmp_path


_UNRESOLVED_SHAPES = {
    "a page that does not exist": f"[gone]({_BLOB}/docs/gone.md)",
    "a raw file that does not exist, in a fence": f"```sh\ncurl -fsSL {_RAW}/gone.sh | sh\n```",
    "a heading the page does not have": f"[x]({_BLOB}/docs/install.md#no-such-heading)",
    "a heading that is only a comment in a fence": f"[x]({_BLOB}/docs/install.md#not-a-heading)",
    "a heading that is only a comment in a nested fence": (
        f"[x]({_BLOB}/docs/install.md#nested-comment)"
    ),
}


@pytest.mark.parametrize("shape", sorted(_UNRESOLVED_SHAPES))
def test_each_unresolved_repo_link_is_caught(shape: str, tree: Path) -> None:
    unresolved = _unresolved_repo_links(
        _UNRESOLVED_SHAPES[shape], tree, version="9.9.9", pending={}
    )

    assert unresolved, f"{shape}: went unreported"


_RESOLVED_SHAPES = {
    "a page that exists": f"[install]({_BLOB}/docs/install.md)",
    "a heading the page has": f"[gui]({_BLOB}/docs/install.md#start-the-gui)",
    "a raw file that exists, in a fence": f"```sh\ncurl -fsSL {_RAW}/install.sh | sh\n```",
    "a directory": "[docs](https://github.com/AISquare-Studio/aisquare-cli/tree/main/docs)",
    "a link outside the repo": "[uv](https://docs.astral.sh/uv/gone.md)",
}


@pytest.mark.parametrize("shape", sorted(_RESOLVED_SHAPES))
def test_resolving_repo_links_are_not_accused(shape: str, tree: Path) -> None:
    unresolved = _unresolved_repo_links(_RESOLVED_SHAPES[shape], tree, version="9.9.9", pending={})

    assert unresolved == [], f"{shape}: was accused: {unresolved}"


def test_a_pending_file_is_excused_only_until_the_release(tree: Path) -> None:
    """The expiry is the point: a slipped file must fail the release commit.

    Whatever the release is numbered. 0.8.0 is pinned as NOT excused: it is
    both "a release merged in from main" and a possible number for this
    branch's own release, and only failing closed is safe for the second.
    """
    gif = f"![demo]({_RAW}/docs/demo.gif)"
    pending = {"docs/demo.gif": "another lane commits it"}

    def missing(version: str, excuses: dict[str, str]) -> list[str]:
        return _unresolved_repo_links(gif, tree, version=version, pending=excuses)

    assert missing(_PENDING_WHILE_VERSION, pending) == [], "accused on the base version"
    for released in ("0.8.0", "0.9.0rc1", "0.9.0"):
        assert missing(released, pending), f"still excused at {released}"
    assert missing(_PENDING_WHILE_VERSION, {}), "a missing file nothing excuses went unreported"


def test_an_excuse_whose_link_is_gone_is_caught() -> None:
    gif = f"![demo]({_RAW}/docs/demo.gif)"

    stale = _stale_pending(gif, {"docs/demo.gif": "x", "docs/gone.md": "y"})

    assert stale == ["docs/gone.md"], f"an excuse whose README line was dropped: {stale}"
    assert _stale_pending(gif, {"docs/demo.gif": "x"}) == [], "a linked excuse was accused"


_UNRESOLVED_RELATIVE = {
    "a page that does not exist": "[x](gone.md)",
    "a page above that does not exist": "[x](../gone.md)",
    "a heading the page does not have": "[x](install.md#no-such-heading)",
    "a heading that is only a comment in a fence": "[x](install.md#not-a-heading)",
    "an anchor this page does not have": "[x](#nowhere)",
}


@pytest.mark.parametrize("shape", sorted(_UNRESOLVED_RELATIVE))
def test_each_dangling_relative_link_is_caught(shape: str, tree: Path) -> None:
    unresolved = _unresolved_relative_links(
        _UNRESOLVED_RELATIVE[shape], tree / "docs" / "install.md"
    )

    assert unresolved, f"{shape}: went unreported"


_RESOLVED_RELATIVE = {
    "a page that exists": "[x](install.md)",
    "a heading the page has": "[x](install.md#start-the-gui)",
    "a file above": "[x](../install.sh)",
    "a heading on this page": "[x](#start-the-gui)",
    "an absolute link": "[x](https://example.com/gone.md#nowhere)",
}


@pytest.mark.parametrize("shape", sorted(_RESOLVED_RELATIVE))
def test_resolving_relative_links_are_not_accused(shape: str, tree: Path) -> None:
    unresolved = _unresolved_relative_links(_RESOLVED_RELATIVE[shape], tree / "docs" / "install.md")

    assert unresolved == [], f"{shape}: was accused: {unresolved}"
