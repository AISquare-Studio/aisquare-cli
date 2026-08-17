"""Every command in a copy-pasteable block must exist.

The README's multi-account section documented `aisquare launch coder --account
~/.claude-account1` for a flag that had been deleted one train earlier —
ce6bc46's own message says so ("drop the account convention"), and it replaced
the flag with a per-role `team bind` profile plus `launch --env`. The code
changed, its tests changed, and four sites in the README kept telling an
operator to type a flag that now exits with a usage error. Two of those sites
were inside a ```sh block, so they read as verified.

That is a whole class of defect no existing test could see: the suite exercises
the CLI, and nothing exercises the documentation. Both failures found on this
train were the same shape — a documented invocation that no longer resolves —
one in the cutover runbook's jq path and one in the README's launch flags.

The instrument is deliberately not `--help` text. Rich wraps help output to the
terminal width, and a narrow width truncates flag names mid-line, which
produced two false positives while this was being written by hand. Introspecting
the Typer app's own command tree has no renderer in the loop at all.

Scope, stated rather than implied:

- Only fenced code blocks are read. Prose is excluded on purpose — "aisquare has
  two halves" is a sentence, and a guard that flags sentences gets switched off.
- A line is split on `&&`, `||` and `;`, so the second half of the runbook's
  `which aisquare && aisquare --version` preflight is parsed. Requiring the LINE
  to start with `aisquare` had silently dropped it.
- A command nested inside `eval "$(...)"` is still not extracted. The runbook
  has one. It is not left silent: EVERY fenced line mentioning `aisquare` that
  is not resolved must match a stated reason in `_NOT_AN_INVOCATION`, so a skip
  is a recorded decision and a new invocation in an unrecognised shape FAILS
  rather than joining an invisible pile. Census on the runbook at the time of
  writing: 38 mentions = 11 resolved + 27 classified + 0 unaccounted.
- A flag's VALUE is not validated, only its existence. `--target prod` proves
  nothing about whether a target named prod is configured.
- A word left over at a GROUP is a subcommand that does not exist; a word left
  over at a LEAF is an argument and stays silent. Before that distinction the
  check only fired when the FIRST word failed, so `explainability statuss`
  passed — a renamed subcommand, which is the likeliest drift after a flag.

A FENCED BLOCK IS A SCRIPT; INLINE CODE IS A REFERENCE. That is the convention
this guard already enforces by construction, and @9bbc8ed7 was right that it
needed saying before it mattered rather than after: a checker cannot tell "type
this" from "never type this", so a counter-example inside a fenced block would be
validated as an instruction — and worse, when it failed, the obvious fix would be
to make the bad command VALID, which is the guard corrupting the document it
guards. There are zero counter-examples in the listed documents today (measured
at 22cf599), so no opt-out marker is added; a flag with no user is the
speculative structure this repo's principles forbid. What is added instead is
that every failure message names both resolutions, so the guard cannot push
anyone toward the wrong one. CONTRIBUTING.md's `pip install
'aisquare-cli[explainability]'` — a command that must NOT be run — is inline code
inside a blockquote, which is exactly right and is why it is invisible here.

DIRECTION IS A DECISION, NOT AN ACCIDENT — @9bbc8ed7 was right to ask, since the
sibling guard `test_runbook_json_paths` is deliberately BIDIRECTIONAL and this
one is deliberately not. This guard asserts documented -> exists and never
exists -> documented, because the two cases fail differently: a WRONG entry
costs a failed command on the primary path, while a MISSING entry costs only
discoverability. The runbook case is bidirectional because a payload key the
page never mentions is a surface an operator cannot know to read; a CLI command
absent from a README is normal.

It is also load-bearing that this stays one-directional right now. `team prune`,
`signal`, `signals` and `verify` all exist and are absent from the command tree;
@dfd9a883 ruled at 10:30 that they stay out until the next train, since a
handed-off train should not take a README restructure. A bidirectional assertion
here would fail on exactly those four and force the widening that was declined —
a test enforcing a decision the owner explicitly deferred.
"""

from __future__ import annotations

import functools
import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.main import get_command

from aisquare.cli.app import app

REPO = Path(__file__).resolve().parents[1]

# Every document that shows a reader a command to type. A new .md with commands
# in it should be added here; the test that guards this list is below.
DOCUMENTED = (
    "README.md",
    "docs/explainability-tracing-boundary.md",
    "docs/runbooks/explainability-prod-cutover.md",
)

FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_-]*)\s*$")
SHELL_LANGUAGES = {"", "sh", "bash", "shell", "console", "zsh"}

# Used only by the bite-checks below: a name the CLI must never have.
BOGUS = "definitelynotacommand"


class Invocation:
    """One documented command, with where it came from."""

    def __init__(self, document: str, line: int, text: str) -> None:
        self.document = document
        self.line = line
        self.text = text

    @property
    def where(self) -> str:
        return f"{self.document}:{self.line}"

    def __repr__(self) -> str:  # pragma: no cover - test identifiers only
        return f"{self.where} {self.text[:60]}"


def _subcommands(node: Any) -> dict[str, Any]:
    """A node's children, empty for a leaf.

    Typed as Any on purpose, and the reason is worth writing down: typer returns
    its own VENDORED click, so `get_command(app)` is a
    `typer._click.core.Command` and not the `click.core.Command` that `import
    click` names. mypy rejects passing one where the other is expected, and
    `isinstance(get_command(app), click.Group)` is False at runtime for the same
    reason — which is misleading enough that a walk over the tree should not
    depend on either name. `.commands` is also absent from the static type even
    when the object is a Group, so the attribute is reached through getattr.
    """
    children: dict[str, Any] = getattr(node, "commands", {}) or {}
    return children


def _shell_lines(markdown: str) -> list[tuple[int, str]]:
    """Lines inside shell-ish fenced blocks, with continuations joined.

    Fence state is tracked for EVERY block, not just shell ones. Tracking only
    shell blocks inverts the state at the first ```json or ```aisquare block,
    because that block's closing fence carries no language and then reads as an
    opener — which silently made this guard parse prose and skip the code. It
    passed green over a README that had a deleted flag in it.
    """
    lines = markdown.splitlines()
    collected: list[tuple[int, str]] = []
    inside = False
    shell = False
    index = 0
    while index < len(lines):
        fence = FENCE.match(lines[index])
        if fence:
            if inside:
                inside, shell = False, False
            else:
                inside, shell = True, fence.group(1).lower() in SHELL_LANGUAGES
            index += 1
            continue
        if inside and shell:
            number = index + 1
            text = lines[index].strip()
            # A blockquoted block ("> ```bash") keeps its quote marker per line.
            text = re.sub(r"^>\s?", "", text).strip().removeprefix("$ ").strip()
            while text.endswith("\\") and index + 1 < len(lines):
                index += 1
                continuation = re.sub(r"^>\s?", "", lines[index].strip()).strip()
                text = text[:-1].strip() + " " + continuation
            collected.append((number, text))
        index += 1
    return collected


#: Shell operators that end one command and begin another on the same line.
_SEQUENCERS = re.compile(r"\s*(?:&&|\|\||;)\s*")


def _from_text(document: str, markdown: str) -> list[Invocation]:
    """Every `aisquare …` command in a fenced block, including after `&&`.

    Requiring the LINE to start with `aisquare` missed real commands: the
    runbook's §0 preflight is `which aisquare && aisquare --version`, and its
    second half is an invocation Jatin runs. Splitting on shell sequencers finds
    it; a command nested inside `$(…)` still does not, which is accounted for by
    the skip audit below rather than left silent.
    """
    found: list[Invocation] = []
    for number, text in _shell_lines(markdown):
        for segment in _SEQUENCERS.split(text):
            if re.match(r"^aisquare(\s|$)", segment.strip()):
                found.append(Invocation(document, number, segment.strip()))
    return found


def _invocations() -> list[Invocation]:
    found: list[Invocation] = []
    for name in DOCUMENTED:
        path = REPO / name
        if not path.exists():
            continue
        found.extend(_from_text(name, path.read_text(encoding="utf-8")))
    return found


@functools.lru_cache(maxsize=1)
def _root() -> Any:
    """The built command tree.

    Cached because `_split` now consults it per token: rebuilding the Typer app
    into click for every documented line took this file from 15s to 50s, and a
    gate three sessions run on a loop should not pay that for a tree that cannot
    change mid-run.
    """
    return get_command(app)


def _takes_a_value(nodes: list[Any], flag: str) -> bool | None:
    """Does this option consume the next token? None when nothing declares it.

    Searched from the deepest resolved node outwards, because a subcommand may
    legitimately redefine a name its parent also uses.
    """
    for node in reversed(nodes):
        for param in node.params:
            if flag in param.opts or flag in getattr(param, "secondary_opts", []):
                return not getattr(param, "is_flag", False)
    return None


def _split(command: str) -> tuple[list[str], list[str]]:
    """(bare words in order, long flags). Pipelines and comments are dropped.

    ARITY COMES FROM THE COMMAND TREE, NOT FROM THE TEXT. This used to assume
    every `--flag` without `=` takes a value, which made a BOOLEAN flag swallow
    the word after it: `aisquare --json explainability status` parsed as
    ['status'] — a real root command, so it resolved, and the guard reported
    green having checked something else entirely. Exactly one documented
    invocation was affected and it was the worst available one: §5b's
    split-brain check, which is also the single command the morning handoff
    quotes.

    An option nothing declares is assumed to take NO value. The two mistakes are
    not symmetric: assuming a value eats a subcommand and validates the wrong
    command silently, while assuming none leaves a stray word that `_walk`
    treats as a positional and ignores. An undeclared flag is separately
    reported by the stale-flag test, so it never passes unnoticed either way.
    """
    command = command.split("#", 1)[0]
    for terminator in ("|", "&&", ";", ">"):
        command = command.split(terminator, 1)[0]
    try:
        tokens = shlex.split(command.strip())[1:]  # drop "aisquare"
    except ValueError:
        return [], []

    node = _root()
    resolved: list[Any] = [node]
    words: list[str] = []
    flags: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            if token.startswith("--"):
                flags.append(name)
            if "=" not in token and _takes_a_value(resolved, name):
                index += 1  # the next token is this option's value
            continue
        words.append(token)
        children = _subcommands(node)
        if token in children:
            node = children[token]
            resolved.append(node)
    return words, flags


def _walk(words: list[str]) -> tuple[list[str], set[str], str | None]:
    """Walk the real command tree as far as the words go.

    Returns the chain that resolved, every long flag legal on it (including the
    options inherited from each parent), and a DANGLING word if the walk stopped
    somewhere a word could only have been a subcommand.

    Telling an argument from a misspelt subcommand is the whole difficulty, and
    the tree answers it: a group dispatches to children and takes no positional
    of its own, so a leftover word at a GROUP is a subcommand that does not
    exist. A leftover word at a leaf is an argument — `task claim <id>`,
    `launch coder` — and must stay silent.
    """
    node = _root()
    chain: list[str] = []
    flags = {opt for param in node.params for opt in param.opts if opt.startswith("--")}
    for index, word in enumerate(words):
        children = getattr(node, "commands", None)
        if not children:
            return chain, flags, None  # a leaf; the rest are arguments
        if word not in children:
            return chain, flags, word  # a group, so this had to be a subcommand
        node = children[word]
        chain.append(word)
        flags |= {opt for param in node.params for opt in param.opts if opt.startswith("--")}
        flags |= {
            opt for param in node.params for opt in param.secondary_opts if opt.startswith("--")
        }
        del index
    return chain, flags, None


def _resolve(words: list[str]) -> tuple[list[str], set[str]]:
    """The chain and its legal flags. One walk backs both views of it."""
    chain, flags, _dangling = _walk(words)
    return chain, flags


@pytest.fixture(scope="module")
def documented() -> list[Invocation]:
    return _invocations()


def test_the_extractor_found_the_documented_commands(documented: list[Invocation]) -> None:
    """Guard the guard: a parser that matches nothing would pass every assertion.

    EVERY listed document must be represented, not just the busy ones. An earlier
    version named only the README and the runbook, which left a hole @9bbc8ed7
    measured: a fence-state inversion that swallowed the tracing-boundary page
    whole would still have passed, because nothing asserted that page yields
    anything. Yields today are 55 / 2 / 10.
    """
    assert len(documented) >= 30, f"only {len(documented)} invocations found — parser broke"
    by_document = {invocation.document for invocation in documented}
    for name in DOCUMENTED:
        assert name in by_document, (
            f"{name} yielded no commands — either it lost them all, or the fence "
            "state inverted and the whole document is being skipped silently"
        )


def test_no_documented_command_uses_prose(documented: list[Invocation]) -> None:
    """Only fenced blocks are read, so a sentence must never be extracted.

    The README opens a paragraph with "aisquare has two halves"; if that ever
    starts being parsed as a command, this guard's failures stop being trusted.
    """
    for invocation in documented:
        words, _ = _split(invocation.text)
        assert "has" not in words[:1], f"{invocation.where} parsed prose as a command"


def _unknown_commands(invocations: list[Invocation]) -> list[str]:
    """Documented invocations whose subcommand does not resolve."""
    unknown: list[str] = []
    for invocation in invocations:
        words, _ = _split(invocation.text)
        if not words:
            continue  # bare `aisquare`, or only flags
        chain, _flags, dangling = _walk(words)
        if not chain:
            unknown.append(f"{invocation.where}  {invocation.text}")
        elif dangling is not None:
            unknown.append(
                f"{invocation.where}  `aisquare {' '.join(chain)}` has no subcommand "
                f"{dangling!r}\n      {invocation.text}"
            )
    return unknown


def test_every_documented_subcommand_exists(documented: list[Invocation]) -> None:
    unknown = _unknown_commands(documented)
    assert not unknown, (
        "documented commands that do not exist:\n  "
        + "\n  ".join(unknown)
        + "\n\nUsually the document is stale and the command should be corrected. "
        "If one of these is a COUNTER-EXAMPLE — shown so a reader avoids typing "
        "it — do NOT make it valid to satisfy this test: move it out of the "
        "fenced block into inline code. A fenced block reads as a script."
    )


def test_every_documented_flag_exists(documented: list[Invocation]) -> None:
    """The README's `--account` defect. A deleted flag stays copy-pasteable."""
    missing: list[str] = []
    for invocation in documented:
        words, used = _split(invocation.text)
        chain, legal = _resolve(words)
        for flag in used:
            if flag not in legal:
                missing.append(
                    f"{invocation.where}  `aisquare {' '.join(chain) or '<root>'}` "
                    f"has no {flag}\n      {invocation.text}"
                )
    assert not missing, (
        "documented flags that do not exist:\n  "
        + "\n  ".join(missing)
        + "\n\nSame caution as above: if the line is a counter-example, move it to "
        "inline code rather than making the flag real."
    )


def test_the_guard_bites_at_the_end_of_every_document() -> None:
    """@9bbc8ed7's bite-check, made permanent and per-document.

    Appending at EOF is the load-bearing detail. A mid-document fence inversion
    corrupts the running state, so a block at the very end parses only if the
    state stayed balanced through the WHOLE file. That is the failure this
    guard's own history earned: it once passed green over a README containing a
    deleted flag because the state inverted early and never recovered.

    Checked by hand it proves the parser for one commit. Checked here it proves
    it for every commit, in every listed document, including the ones too short
    to notice going quiet.
    """
    for name in DOCUMENTED:
        text = (REPO / name).read_text(encoding="utf-8")
        spiked = f"{text}\n```sh\naisquare {BOGUS} --nosuchflag\n```\n"

        found = [i for i in _from_text(name, spiked) if BOGUS in i.text]
        assert found, (
            f"a command appended to the END of {name} was not extracted — the "
            "fence state does not stay balanced through this document, so some "
            "region of it is being skipped silently"
        )

        words, used = _split(found[0].text)
        chain, legal = _resolve(words)
        assert not chain, f"{BOGUS!r} resolved as a real command"
        assert "--nosuchflag" not in legal
        assert used == ["--nosuchflag"], f"the flag was not extracted from {found[0].text!r}"


def test_the_document_list_has_not_gone_stale() -> None:
    """A new .md full of commands must not silently escape this guard.

    Repo-root pages are swept as well as `docs/`, because that is where the
    coverage edge actually sits: CONTRIBUTING.md now carries a command in its
    text, and until this sweep reached the root, a root page that gained a
    fenced command would simply have been unguarded with nothing saying so.
    Measured at 22cf599 — of CHANGELOG, CODE_OF_CONDUCT, CONTRIBUTING, README
    and SECURITY, only README has fenced commands (55), so widening the sweep
    pulls in nothing today. It is a detector for tomorrow, not a change of scope.
    """
    unlisted: list[str] = []
    for path in [*sorted(REPO.glob("*.md")), *sorted((REPO / "docs").rglob("*.md"))]:
        relative = path.relative_to(REPO).as_posix()
        if relative in DOCUMENTED:
            continue
        if any(
            re.match(r"^aisquare(\s|$)", text)
            for _number, text in _shell_lines(path.read_text(encoding="utf-8"))
        ):
            unlisted.append(relative)
    assert not unlisted, (
        f"these documents show commands in a fenced block but are not covered by "
        f"this guard: {unlisted}.\n"
        "Two ways to resolve this, and they are not interchangeable:\n"
        "  - the commands are meant to be RUN -> add the document to DOCUMENTED\n"
        "  - a command is a COUNTER-EXAMPLE, shown so a reader avoids it -> keep "
        "it out of a fenced block (inline code reads as a reference, a fenced "
        "block reads as a script), because listing the document would make this "
        "guard demand that the command be valid"
    )


def _tree_lines(markdown: str) -> list[tuple[int, str]]:
    """Lines of a fenced block that draws the command tree.

    The tree is fenced with a BARE ``` and so cannot be found by its info
    string; it is identified by content. It is not the only bare block — the
    ~/.aisquare directory layout is drawn the same way — so a block counts only
    when one of its branches names a real top-level command.
    """
    lines = markdown.splitlines()
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] | None = None
    for index, line in enumerate(lines):
        if FENCE.match(line):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append((index + 1, line))
    commands = set(_subcommands(get_command(app)))
    kept: list[tuple[int, str]] = []
    for block in blocks:
        heads = {
            match.group(1)
            for _number, text in block
            if (match := re.match(r"^[├└]──\s+([a-z][a-z0-9-]*)", text.strip()))
        }
        if heads & commands:
            kept.extend(block)
    return kept


def _flags_under(command_name: str | None) -> set[str]:
    """Long options legal on a command or any of its subcommands, plus root's."""
    root = get_command(app)
    nodes = [root]
    if command_name is not None:
        command = _subcommands(root).get(command_name)
        if command is None:
            return set()
        nodes = [command, *_subcommands(command).values()]
    legal = {
        opt
        for node in nodes
        for param in node.params
        for opt in (*param.opts, *param.secondary_opts)
        if opt.startswith("--")
    }
    return legal | {o for p in root.params for o in p.opts if o.startswith("--")}


def _stale_tree_flags(text: str) -> tuple[list[str], int]:
    """(stale flag reports, flags examined) for a document's command tree."""
    stale: list[str] = []
    checked = 0
    current: str | None = None
    for number, line in _tree_lines(text):
        head = re.match(r"^[├└]──\s+([a-z][a-z0-9-]*)", line.strip())
        if head:
            current = head.group(1)
        legal = _flags_under(current)
        for flag in re.findall(r"--[a-z][a-z0-9-]*", line):
            checked += 1
            if flag not in legal:
                stale.append(f"line {number}  {flag} is not under `{current}`  | {line.strip()}")
    return stale, checked


def test_inline_code_is_not_read_as_an_instruction() -> None:
    """The convention, pinned on the case that motivated it.

    CONTRIBUTING.md warns a contributor not to install the explainability extra
    into an editable checkout, and states the command so they recognise it. That
    command must stay invisible to this guard: it is a reference, not a step.
    Asserted on the real page rather than a fixture, because the thing that could
    break is someone later moving that line into a ```sh block — which would
    turn a warning into a script and, once the document were listed, make this
    guard demand the command be runnable.
    """
    contributing = REPO / "CONTRIBUTING.md"
    text = contributing.read_text(encoding="utf-8")
    assert "aisquare-cli[explainability]" in text, (
        "CONTRIBUTING no longer names the extra — if the warning moved, this "
        "pin needs to move with it rather than being deleted"
    )

    fenced = [command for _number, command in _shell_lines(text)]
    assert not any("aisquare-cli[explainability]" in command for command in fenced), (
        "the do-not-run command is now inside a fenced block, where it reads as "
        "a step rather than a warning"
    )
    assert not _from_text("CONTRIBUTING.md", text), (
        "CONTRIBUTING gained fenced aisquare commands; decide whether they are "
        "instructions (add the page to DOCUMENTED) or warnings (unfence them)"
    )


def test_the_convention_survives_a_widening_in_both_directions() -> None:
    """The half that makes the convention trustworthy: it must still CATCH things.

    Proving only that a prohibition goes unflagged is exactly what would let a
    relaxed guard look correct — a checker that ignores everything satisfies that
    half perfectly. So this simulates the widening rather than performing it:
    take the real CONTRIBUTING page, which carries a real prohibition as inline
    code, append a genuinely stale command in a fenced block, and check the two
    outcomes TOGETHER in ONE document.

    The stale command used is the actual defect this guard was written for —
    `launch --account`, deleted in ce6bc46 — so the catch being asserted is one
    that really happened rather than an invented shape.

    Scope is NOT widened here, per the task's boundary: DOCUMENTED is untouched
    and this test builds its own invocation list.
    """
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "aisquare-cli[explainability]" in text, "the page no longer carries a prohibition"

    widened = f"{text}\n```sh\naisquare launch coder --account ~/.claude-account1\n```\n"
    invocations = _from_text("CONTRIBUTING.md", widened)

    # Direction 1: the fenced stale command IS caught.
    unknown_or_missing = _unknown_commands(invocations) + [
        flag
        for invocation in invocations
        for flag in _split(invocation.text)[1]
        if flag not in _resolve(_split(invocation.text)[0])[1]
    ]
    assert unknown_or_missing, (
        "a stale command in a fenced block went uncaught — if the convention "
        "reaches this state, widening the guard buys nothing"
    )
    assert any("--account" in item for item in unknown_or_missing)

    # Direction 2: the inline prohibition is still invisible, in the SAME pass.
    assert not any("aisquare-cli[explainability]" in i.text for i in invocations), (
        "the inline prohibition was extracted — a warning would be graded as an "
        "instruction the moment this document is listed"
    )


def test_the_command_tree_has_no_deleted_flags() -> None:
    """The README's reference tree is not copy-pasteable, and was still wrong.

    `launch <planner|coder|runner> [--account DIR]` named a flag deleted a train
    earlier. The fenced-code guard above cannot see it: tree lines start with a
    box-drawing character, not with `aisquare`.

    Attribution is deliberately loose — a tree line names several subcommands at
    once, so a flag counts as valid anywhere under its top-level command. That
    is still enough to catch a flag that exists nowhere, which is the defect.
    """
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert _tree_lines(text), "no command tree in the README — this guard sees nothing"

    stale, checked = _stale_tree_flags(text)

    assert checked >= 20, f"only {checked} flags found in the tree — the parser broke"
    assert not stale, "the command tree documents flags that do not exist:\n  " + "\n  ".join(stale)


def test_the_tree_guard_bites_on_a_flag_that_does_not_exist() -> None:
    """Prove it can fail, on the document it actually guards.

    Verified by hand once against the pre-fix README, where it named
    `--account not under launch`. Doing it here means the ability to bite is
    re-earned on every commit rather than resting on one afternoon's check.
    """
    text = (REPO / "README.md").read_text(encoding="utf-8")
    spiked = text.replace("├── serve ", "├── serve [--nosuchflag] ", 1)
    assert spiked != text, "the anchor line moved — this bite-check is not spiking anything"

    stale, _checked = _stale_tree_flags(spiked)

    assert any("--nosuchflag" in report for report in stale), (
        "a bogus flag added to the command tree went unreported"
    )


def test_typer_is_the_instrument_not_the_help_renderer() -> None:
    """Pin the choice, because reverting it reintroduces a false-positive class.

    Rich wraps `--help` to the terminal width and truncates flag names mid-line,
    so a grep over help text reports flags missing that are present. The command
    tree has no renderer in it.
    """
    assert isinstance(get_command(app), typer.core.TyperGroup)
    _chain, flags = _resolve(["launch"])
    assert "--command" in flags and "--env" in flags
    assert "--verbose" in flags, "parent options must be inherited into the legal set"


#: Ways a fenced line can mention "aisquare" without being a command to run.
#: Each entry is a REASON, so an unclassified mention fails rather than passing
#: quietly — a silent skip makes this file read as covering everything while
#: covering less, which is worse than not having it.
_NOT_AN_INVOCATION = (
    ("a path segment, not a command word", re.compile(r"[/\w-]/aisquare|aisquare[/-]")),
    ("a dotted Python module path", re.compile(r"aisquare\.\w")),
    ("a comment", re.compile(r"^\s*#")),
    ("nested in a command substitution the extractor cannot see into", re.compile(r"\$\(")),
    # `key: value` is how every status/doctor block in this runbook prints. Those
    # blocks quote real commands ("next: aisquare doctor --live") as SAMPLE
    # OUTPUT — drift there is a stale transcript, which is a doc bug, not a
    # broken command Jatin runs.
    ("a `key: value` line of sample CLI output", re.compile(r"^\s*[\w. -]+:\s")),
    ("a prose line inside an output block", re.compile(r"^\s*[→✓✗•]")),
    ("a URL", re.compile(r"https?://")),
    ("a pip requirement — the package name, not the command", re.compile(r"pip\s+install")),
)


#: Resolved / classified counts measured per document, so a collapse in either
#: number is visible instead of averaging out across the three.
CENSUS = {
    "README.md": (55, 5),
    "docs/explainability-tracing-boundary.md": (2, 0),
    "docs/runbooks/explainability-prod-cutover.md": (11, 27),
}


@pytest.mark.parametrize("document", DOCUMENTED)
def test_every_aisquare_mention_is_classified(document: str) -> None:
    """A skip must be a decision, not an omission.

    Measured at the time of writing — resolved + classified + 0 unaccounted:
    README 60 = 55 + 5, tracing-boundary 2 = 2 + 0, cutover runbook 38 = 11 + 27
    (paths, dotted module paths, sample output, a pip requirement and one nested
    `eval $(…)`). Nothing asserted that, so a NEW invocation written in a shape
    the extractor cannot see would join the skip set and this file would keep
    reporting green over a line nobody checks.

    Every mention must therefore match a stated reason for not being a command.
    An unclassified one fails and names the line — which is exactly the case
    where somebody has written a real invocation the extractor cannot see.

    Run over ALL THREE documents rather than the runbook alone. The runbook is
    where the silent skips were found, but the README is the page that grows,
    and scoping an audit to the document whose defect prompted it is how the
    next instance goes unnoticed.
    """
    text = (REPO / document).read_text(encoding="utf-8")
    extracted = {invocation.line for invocation in _from_text(document, text)}

    unexplained: list[str] = []
    classified = 0
    for number, line in _shell_lines(text):
        if "aisquare" not in line or number in extracted:
            continue
        if any(pattern.search(line) for _reason, pattern in _NOT_AN_INVOCATION):
            classified += 1
            continue
        unexplained.append(f"{document}:{number}  {line}")

    was_resolved, was_classified = CENSUS[document]
    assert len(extracted) >= was_resolved * 0.8, (
        f"{document}: only {len(extracted)} commands extracted, was {was_resolved} "
        "— the parser lost lines it used to read"
    )
    assert classified >= was_classified * 0.6, (
        f"{document}: only {classified} mentions classified, was {was_classified} "
        "— the reason patterns have stopped matching, so this audit is asserting "
        "over a set it no longer inspects"
    )
    assert not unexplained, (
        "these lines mention aisquare, are not resolved as commands, and "
        "match no stated reason for being skipped:\n  " + "\n  ".join(unexplained) + "\n"
        "If one is a real invocation, the extractor cannot see its shape and is "
        "reporting green over a line Jatin will run. If it is not, add a reason "
        "to _NOT_AN_INVOCATION so the skip is recorded rather than silent."
    )


def test_a_renamed_subcommand_is_caught_below_the_top_level() -> None:
    """THE hole this task was filed against, and it survived the first fix.

    The check used to be `if not chain` — it only fired when the FIRST word
    failed. `aisquare explainability statuss` resolved `explainability`, left
    `statuss` dangling, and passed: a leftover word was assumed to be an
    argument. Measured by doctoring the runbook's real line 407 and watching the
    suite stay green.

    That is the likeliest drift after a renamed flag. Every runbook command but
    two is `aisquare <group> <subcommand>`, so a fold that renames a subcommand
    was exactly the case the guard could not see.
    """
    unknown = _unknown_commands([Invocation("doc.md", 407, "aisquare explainability statuss")])

    assert unknown, "a misspelt depth-2 subcommand resolved as an argument"
    assert "statuss" in unknown[0] and "doc.md:407" in unknown[0]


def test_a_positional_argument_is_not_read_as_a_misspelt_subcommand() -> None:
    """The other half, and the reason the rule is group-versus-leaf.

    `task claim <id>` and `launch <role>` end in a word that is NOT a
    subcommand and must stay silent. A check that flagged every leftover word
    would fail on the board commands in the README — noise that would get the
    whole guard deleted rather than fixed.
    """
    for command in ("aisquare task claim tsk_01abc", "aisquare launch coder1"):
        assert not _unknown_commands([Invocation("doc.md", 1, command)]), (
            f"{command!r} was reported — a positional argument is being read as a subcommand"
        )


def test_the_runbook_is_treated_as_a_contract_in_the_failure_message() -> None:
    """A flag-renamer must learn WHERE the dependency is, not just that one exists.

    The person who breaks this is renaming a flag with no idea a document
    depends on it, so the failure has to name the document and say it is
    executed rather than illustrative.
    """
    unknown = _unknown_commands(
        [Invocation("docs/runbooks/explainability-prod-cutover.md", 42, "aisquare notacommand")]
    )

    assert unknown and "explainability-prod-cutover.md:42" in unknown[0], (
        "the failure must name the document and the line, because that is the "
        "only way the flag-renamer finds what depends on them"
    )


def test_a_boolean_flag_before_a_subcommand_does_not_eat_it() -> None:
    """The guard was checking a DIFFERENT, EXISTING command and passing.

    `_split` assumed every `--flag` without `=` takes a value, so a boolean
    global flag swallowed the word after it. `aisquare --json explainability
    status` parsed as words ['status'] — which resolves, because `status` is a
    real root command — so the line went green while nothing about
    `explainability status` was checked.

    Measured across the documented pages: exactly one invocation was validated
    against the wrong command, and it is runbook §5b's SPLIT-BRAIN ASSERTION —
    the check that catches the proxy lane and the client lane pointing at two
    different deployments. The one command the morning handoff quotes, and the
    one the guard was silently not covering.

    Which flags take a value is not guessable from the text; it is a property of
    the command tree, so the tree is asked.
    """
    words, flags = _split("aisquare --json explainability status")

    assert words == ["explainability", "status"], (
        f"a boolean flag ate the subcommand: parsed {words}"
    )
    assert "--json" in flags
    chain, _legal, dangling = _walk(words)
    assert chain == ["explainability", "status"] and dangling is None


def test_a_value_taking_flag_still_consumes_its_value() -> None:
    """The other half — the reason the naive rule existed at all.

    `--target prod` must not leave `prod` looking like a subcommand, or the
    runbook's own enable line starts failing as an unknown command.
    """
    words, flags = _split("aisquare explainability enable --target prod --key-env KEY")

    assert words == ["explainability", "enable"], f"a flag value leaked into words: {words}"
    assert "--target" in flags and "--key-env" in flags


def test_the_split_brain_line_is_actually_covered() -> None:
    """Point the assertion at the real runbook line, not a hand-typed copy.

    A test that retypes the command proves the parser works on a string I
    chose. This one fails if the runbook's line stops being resolved — which is
    what "covered" has to mean.
    """
    runbook = "docs/runbooks/explainability-prod-cutover.md"
    text = (REPO / runbook).read_text(encoding="utf-8")
    resolved = {
        tuple(_walk(_split(inv.text)[0])[0])
        for inv in _from_text(runbook, text)
        if "--json" in inv.text
    }

    assert ("explainability", "status") in resolved, (
        "the runbook's --json split-brain check is not being resolved as "
        f"`explainability status`; resolved chains were {sorted(resolved)}"
    )
