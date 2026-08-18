"""The handoff cites eleven test files by name. Nothing checked they exist.

"Each of these has a test behind it" is the strongest form the doctrine section
can state, and the most fragile: the citations point into a directory people
rename and consolidate. A rename turns every one of them into a lie, silently,
in the document read cold and first.

Measured at 1481f19 before this existed, and nothing was broken — this is a
guard, not a fix:

    MORNING-HANDOFF.md            13 references, 0 unresolvable
    explainability-prod-cutover   4 references,  1 unresolvable (external)
    store-migration-race.md       0
    README.md                     3 references, 0 unresolvable

THE ONE UNRESOLVABLE IS WHY THIS FILE HAS AN ALLOW LIST. ``claude_proxy.py`` in
§5 belongs to the SDK — the sentence is about ``_has_valid_correlation`` being
byte-identical in ``aisquare>=1.1.0`` on PyPI. It *should* not resolve here, and
a naive "every path must exist" check would fail a correct sentence, which is
the check-that-misdiagnoses class this runbook has already produced twice. So an
external reference is ALLOWED WITH A STATED REASON rather than skipped quietly.

The allow list is checked in both directions and the extraction has a floor,
because a census that can be narrowed to nothing reports "0 unaccounted" while
inspecting nothing — the exact defect found in this repo's other census.

Deliberately NOT added to ``DOCUMENTED`` in ``test_documented_commands.py``:
that guard resolves COMMANDS against the Typer tree, and the handoff has no
fenced blocks by design. This one reads paths, not commands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: Docs whose repo-path references must resolve. The handoff is here precisely
#: because the command guard excludes it — it is a reference, not a script, so
#: nothing else looks at it at all.
DOCS = [
    Path("docs/runbooks/MORNING-HANDOFF.md"),
    Path("docs/runbooks/explainability-prod-cutover.md"),
    Path("docs/store-migration-race.md"),
    Path("README.md"),
]

#: References that name a file OUTSIDE this repo, each with the reason it
#: cannot resolve here. Anything not listed must exist; anything listed must
#: still be cited, so this cannot rot into a stale allow list.
EXTERNAL = {
    "claude_proxy.py": "ships in the aisquare SDK, not this repo — §5 pins "
    "_has_valid_correlation as byte-identical in aisquare>=1.1.0",
}

#: A file that stops matching passes every assertion below while inspecting
#: nothing. Measured: 20 references across the four documents.
_FLOOR = 15

_REFERENCE = re.compile(r"`((?:tests|src|docs)/[A-Za-z0-9_./-]+|[a-z][a-z0-9_]*\.py)`")


def _references() -> dict[str, list[Path]]:
    """Every repo-path-looking citation, mapped to the docs that make it."""
    found: dict[str, list[Path]] = {}
    for doc in DOCS:
        assert doc.exists(), f"the guard cites a document that does not exist: {doc}"
        for ref in _REFERENCE.findall(doc.read_text(encoding="utf-8")):
            found.setdefault(ref, []).append(doc)
    return found


def _resolves(ref: str) -> bool:
    """A citation may be repo-relative or bare (a test or module filename)."""
    return any(
        candidate.exists()
        for candidate in (Path(ref), Path("tests") / ref, Path("src/aisquare") / ref)
    )


def test_the_extraction_still_finds_references() -> None:
    """Guard the guard: a regex that stops matching satisfies everything else.

    This is the narrowing found in this repo's other census — one broad
    exclusion made it report "0 unaccounted" while inspecting nothing. A floor
    is the cheapest defence, and it is why the number is here rather than in a
    comment.
    """
    found = _references()

    assert len(found) >= _FLOOR, (
        f"only {len(found)} references extracted, below the floor of {_FLOOR} — "
        "the pattern has stopped matching and every check below is vacuous"
    )


def test_every_cited_repo_path_exists() -> None:
    """The claim itself: a citation points at something real, or is declared."""
    unresolvable = {
        ref: docs
        for ref, docs in _references().items()
        if ref not in EXTERNAL and not _resolves(ref)
    }

    assert not unresolvable, "documents cite paths that do not exist: " + ", ".join(
        f"{ref} (in {', '.join(str(d) for d in docs)})"
        for ref, docs in sorted(unresolvable.items())
    )


def test_the_external_list_names_only_live_citations() -> None:
    """The other direction, so the allow list cannot outlive what it excuses.

    An entry for a reference nobody makes any more is an unexamined claim that
    some path is external — and it would silently excuse that name if a real
    file of the same name were cited later.
    """
    cited = set(_references())

    stale = sorted(name for name in EXTERNAL if name not in cited)

    assert not stale, f"EXTERNAL names references no document makes: {stale}"


@pytest.mark.parametrize("name", sorted(EXTERNAL))
def test_each_external_entry_really_is_external(name: str) -> None:
    """An entry that DOES resolve is not an exclusion, it is a mistake.

    Without this, adding a real repo file to EXTERNAL would exclude it from the
    check that matters while looking like documentation.
    """
    assert not _resolves(name), (
        f"{name} resolves in this repo, so it must not be excused as external"
    )


def test_the_doctrine_citations_specifically_resolve() -> None:
    """The bullets that say "each of these has a test behind it".

    Called out separately from the sweep above because this is the sentence
    with the most weight on it: eleven files, named, in the section that tells
    the next person what not to break.
    """
    handoff = Path("docs/runbooks/MORNING-HANDOFF.md").read_text(encoding="utf-8")
    section = handoff[handoff.index("## Doctrine this integration holds to") :]
    cited = sorted(set(re.findall(r"`(test_[A-Za-z0-9_]+\.py)`", section)))

    assert len(cited) >= 8, f"only {len(cited)} test files cited; the section changed shape"
    missing = [name for name in cited if not (Path("tests") / name).exists()]
    assert not missing, f"the doctrine section cites tests that do not exist: {missing}"
