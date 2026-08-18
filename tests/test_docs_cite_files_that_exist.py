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

#: A cheap, independent signal that a document OUGHT to yield references: it
#: mentions a test or a repo path in plain text. Deliberately not the same
#: machinery as ``_REFERENCE`` — two copies of one parser agreeing proves
#: nothing, and the whole job here is catching that parser going quiet.
_MENTIONS_A_PATH = re.compile(r"test_[a-z0-9_]+\.py|tests/|src/aisquare/")

_REFERENCE = re.compile(r"`((?:tests|src|docs)/[A-Za-z0-9_./-]+|[a-z][a-z0-9_]*\.py)`")


def _references() -> dict[str, list[Path]]:
    """Every repo-path-looking citation, mapped to the docs that make it."""
    found: dict[str, list[Path]] = {}
    for doc in DOCS:
        assert doc.exists(), f"the guard cites a document that does not exist: {doc}"
        for ref in _REFERENCE.findall(doc.read_text(encoding="utf-8")):
            found.setdefault(ref, []).append(doc)
    return found


#: Where a citation is allowed to live. Data rather than a hardcoded tuple so
#: the control below can point at the same roots the rule uses.
_SEARCH_ROOTS = (Path("."), Path("tests"), Path("src/aisquare"))


def _resolves(ref: str, roots: tuple[Path, ...] = _SEARCH_ROOTS) -> bool:
    """A citation may be repo-relative or bare (a test or module filename).

    ``roots`` is injectable so the controls can drive this against a temporary
    tree instead of the repo — a control anchored to real cited files stops
    controlling anything the day one of them is renamed.
    """
    return any((root / ref).exists() for root in roots)


def _unresolvable(references: dict[str, list[Path]]) -> dict[str, list[Path]]:
    """The citations that point at nothing and are not declared external.

    A CALLABLE rule rather than a comprehension inside a test, and that is the
    whole change. Measured before it existed: adding ``if False and`` to the
    inline condition left ALL FIVE TESTS PASSING — the guard reported that every
    cited path resolves while checking none of them.

    ``_resolves`` was already extracted and that was not enough. Nothing ever
    called it with a path known to be missing, so nothing noticed when the rule
    stopped consulting it. EXTRACTION IS NOT THE POINT; REACHABILITY BY A
    CONTROL IS — the lesson @8dd460fb and I each arrived at from our own files.
    """
    return {
        ref: docs for ref, docs in references.items() if ref not in EXTERNAL and not _resolves(ref)
    }


def test_the_extraction_still_finds_references() -> None:
    """Guard the guard, WITH NO NUMBER IN IT.

    This was ``len(found) >= 15`` — a typed constant, which becomes ``>= 0`` in
    one keystroke inside the artifact whose whole job is stopping a guard from
    inspecting nothing. @8dd460fb escaped that category structurally and this
    follows them: a document that plainly mentions a test file or a repo path
    MUST yield at least one structured reference. Two independent methods, so a
    silent parser is caught by the other one, and nothing to lower.

    The per-document condition is EARNED rather than assumed. Copying their
    "every file yields one" shape directly would fail correct input here:
    docs/store-migration-race.md cites nothing at all today, and a guard that
    fails a correct document is the too-broad rule this shift has produced
    three times.
    """
    found = _references()
    citing = {doc for docs in found.values() for doc in docs}

    silent = [
        doc
        for doc in DOCS
        if _MENTIONS_A_PATH.search(doc.read_text(encoding="utf-8")) and doc not in citing
    ]

    assert not silent, (
        f"these documents plainly mention repo paths but the extractor found "
        f"none in them: {[str(d) for d in silent]} — the pattern has stopped "
        "matching and every check below is vacuous"
    )


def test_every_cited_repo_path_exists() -> None:
    """The claim itself: a citation points at something real, or is declared."""
    unresolvable = _unresolvable(_references())

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

    # No count: the section is REQUIRED to cite something, and every citation
    # must resolve. A section that stopped citing entirely fails the first
    # assertion; one that cites a renamed file fails the second.
    assert cited, "the doctrine section cites no test files at all; it changed shape"
    missing = [name for name in cited if not (Path("tests") / name).exists()]
    assert not missing, f"the doctrine section cites tests that do not exist: {missing}"


#: One synthetic citation per shape the rule decides, plus the shape it must
#: NOT accuse. Synthetic and driven against a temporary tree, so the control
#: does not stop controlling the day a real cited file is renamed.
_RESOLVING_SHAPES = {
    "repo-relative path": "docs/made_up_note.md",
    "bare test filename": "test_made_up_guard.py",
    "bare module filename": "made_up_module.py",
}


@pytest.mark.parametrize("shape", sorted(_RESOLVING_SHAPES))
def test_the_rule_still_decides_each_shape_it_claims(shape: str, tmp_path: Path) -> None:
    """Positive control, per shape, so a failure names which one went blind."""
    ref = _RESOLVING_SHAPES[shape]
    roots = (tmp_path, tmp_path / "tests", tmp_path / "src/aisquare")
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)

    assert not _resolves(ref, roots), f"{shape}: the control's tree already contains it"

    target = tmp_path / ref if "/" in ref else tmp_path / "tests" / ref
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")

    assert _resolves(ref, roots), f"the rule no longer decides: {shape}"


def test_the_rule_reports_a_citation_that_points_at_nothing() -> None:
    """The rule itself, driven with known-bad input it cannot reach in the repo."""
    missing = "definitely_not_a_real_file_in_this_repo.py"

    reported = _unresolvable({missing: [Path("docs/synthetic.md")]})

    assert missing in reported, "the rule no longer reports an unresolvable citation"


def test_the_rule_does_not_report_a_declared_external() -> None:
    """Negative control: "make the rule always fire" is not a fix.

    ``claude_proxy.py`` is the live instance — it ships in the SDK and must stay
    excusable through EXTERNAL rather than by the rule going blind.
    """
    external = next(iter(EXTERNAL))

    reported = _unresolvable({external: [Path("docs/synthetic.md")]})

    assert external not in reported, f"a declared external was reported: {external}"
