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
- A BLOCKQUOTED fence is a fence. `> ```bash` styles an aside; the fence still
  says "this is a script", and the runbook puts real operator commands in them.
  Inline code inside a blockquote stays invisible — that is inline code, not a
  fenced block, which is why CONTRIBUTING.md's must-not-run `pip install` is
  still correctly unseen.
- An invocation by ABSOLUTE PATH is an invocation: a cron wrapper's
  `exec /usr/local/bin/aisquare …` is extracted and normalised. A path with no
  word after it — `~/.config/aisquare/prod.env` — is still a path.
- A command nested inside `eval "$(...)"` is still not extracted. The runbook
  has one. It is not left silent: EVERY fenced line mentioning `aisquare` that
  is not resolved must match a stated reason in `_NOT_AN_INVOCATION`, so a skip
  is a recorded decision and a new invocation in an unrecognised shape FAILS
  rather than joining an invisible pile. Census on the runbook at the time of
  writing: 40 mentions = 12 resolved + 28 classified + 0 unaccounted.
- A flag's VALUE is not validated, only its existence. `--target prod` proves
  nothing about whether a target named prod is configured.
- An absolute PATH is not validated either. `exec /usr/local/bin/aisquare
  explainability ship --strict` normalises to a real command and passes here,
  and that literal did not exist on this machine — the §5b wrapper exited 127
  before the CLI was reached. This guard answers "is this a command the CLI
  has", never "will this line run on this box".
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
    # Found by widening the staleness sweep past root-plus-`docs/`. The template
    # asks a reporter to run `aisquare doctor` and paste the output, so by this
    # guard's own rule — commands meant to be RUN get the document listed — it
    # belongs here. Trivially stable today; listed because the resolution rule
    # is the rule, not because this command is likely to drift.
    ".github/ISSUE_TEMPLATE/bug_report.md",
    "docs/runbooks/MORNING-HANDOFF.md",
)

#: Directories the staleness sweep never enters. Everything else under the repo
#: is swept, because "which directories hold documentation" is precisely the
#: judgement that was wrong before: `.github/ISSUE_TEMPLATE` holds a page that
#: asks a user to run a command and sat outside a root-plus-docs sweep.
_SWEEP_EXCLUDES = frozenset({".venv", ".git", "node_modules", "site-packages", "build", "dist"})

FENCE = re.compile(r"^\s*(?:>\s*)*```+\s*([A-Za-z0-9_-]*)\s*$")
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

#: `aisquare`, or any path ending in it, at the head of a segment. The optional
#: `exec` is there because a cron wrapper's real line is
#: `exec /usr/local/bin/aisquare explainability ship --strict` — cron has no
#: useful PATH, so the absolute path is the correct thing to document and
#: `exec` is how a wrapper hands the process over. `aisquare-runner` and
#: `.../aisquare/file.env` do not match: the name must be followed by a space
#: or the end of the segment.
_INVOCATION_HEAD = re.compile(r"^(?:exec\s+)?(\S*/)?aisquare(?=\s|$)")


def _as_invocation(segment: str) -> str | None:
    """The command as typed, normalised to start with `aisquare`, or None.

    Normalising means the rest of this file needs no knowledge of how the
    command was reached — `_split` drops the first token either way.
    """
    text = segment.strip()
    match = _INVOCATION_HEAD.match(text)
    if match is None:
        return None
    return "aisquare" + text[match.end() :]


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
            invocation = _as_invocation(segment)
            if invocation is not None:
                found.append(Invocation(document, number, invocation))
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


def _forwards_unknown_flags(words: list[str]) -> bool:
    """Does the resolved command hand flags it does not know to another program?

    `launch` declares ``ignore_unknown_options`` and its help says so: "Extra
    arguments are passed to the agent." So `aisquare launch coder --headless`
    is not a defect — the flag belongs to `claude`, and the CLI never sees it.
    Without this, the flag test reports a documented, working invocation as a
    stale flag, and the obvious repair is to EDIT THE DOCUMENT — which is this
    guard corrupting the file it guards, the failure this module's own header
    warns about for counter-examples.

    Read from ``context_settings`` rather than a list of command names, so a
    second forwarding command is covered the day it is added rather than the
    day someone notices.

    THE EXEMPTION COVERS EVERY FLAG ON THE COMMAND, INCLUDING A TYPO IN ONE OF
    ITS OWN. That is not a shortcut, it is what the CLI does: measured,
    ``aisquare launch coder -c /bin/echo --envv FOO=bar`` exits 0 and hands
    ``--envv FOO=bar`` to the program, so a misspelt ``--env`` sets nothing and
    reports nothing. The parser cannot distinguish it from an agent flag, so
    neither can a guard that asks the parser. The residual is real and stated
    rather than papered over: a typo in ``--env`` or ``--command`` inside a
    fenced block is invisible to this module.
    """
    node = _root()
    for word in words:
        children = getattr(node, "commands", None)
        if not children or word not in children:
            break
        node = children[word]
    return bool((getattr(node, "context_settings", None) or {}).get("ignore_unknown_options"))


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
    # Derived from CENSUS rather than typed, for the same reason: a bare `>= 30`
    # can be lowered to `>= 0` without failing anything, measured.
    floor = sum(resolved for resolved, _classified in CENSUS.values()) * 0.8
    assert len(documented) >= floor, (
        f"only {len(documented)} invocations found across every document, "
        f"expected at least {floor:.0f} — the parser broke or a page lost its "
        "commands"
    )
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


def _stale_flags(invocations: list[Invocation]) -> list[str]:
    """The rule itself, so a control can put a known input in front of it.

    Called from inside the assert below rather than through a variable the test
    could shadow, and reachable from the controls with synthetic invocations —
    a rule no control can address is a rule nothing has ever checked.
    """
    missing: list[str] = []
    for invocation in invocations:
        words, used = _split(invocation.text)
        chain, legal = _resolve(words)
        if _forwards_unknown_flags(words):
            continue  # the flags belong to the program it launches
        for flag in used:
            if flag not in legal:
                missing.append(
                    f"{invocation.where}  `aisquare {' '.join(chain) or '<root>'}` "
                    f"has no {flag}\n      {invocation.text}"
                )
    return missing


def test_every_documented_flag_exists(documented: list[Invocation]) -> None:
    """The README's `--account` defect. A deleted flag stays copy-pasteable."""
    missing = _stale_flags(documented)
    assert not missing, (
        "documented flags that do not exist:\n  "
        + "\n  ".join(missing)
        + "\n\nSame caution as above: if the line is a counter-example, move it to "
        "inline code rather than making the flag real."
    )


def test_a_forwarding_command_may_document_flags_the_cli_never_declared() -> None:
    """`launch` forwards to the agent, so its unknown flags are not defects.

    Synthetic invocations, because there is no such line in the documents today
    — this is a false positive waiting for the first person to document
    `aisquare launch coder --headless`, and the repair it invites is to change
    the document rather than the guard. Verified against the running CLI rather
    than against a reading of click: `aisquare launch coder -c /bin/echo -p x`
    exits 0 and the agent receives `-p x`.
    """
    forwarded = Invocation("probe.md", 1, "aisquare launch coder --headless")

    assert _stale_flags([forwarded]) == []


def test_the_forwarding_exemption_does_not_leak_to_ordinary_commands() -> None:
    """The other half: an exemption that covers everything checks nothing.

    Without this, `_forwards_unknown_flags` could return True unconditionally
    and every test in this module would still pass.
    """
    ordinary = [
        Invocation("probe.md", 1, "aisquare doctor --lives"),
        Invocation("probe.md", 2, "aisquare explainability status --nope"),
    ]

    stale = _stale_flags(ordinary)

    assert len(stale) == 2, f"the exemption swallowed a real stale flag: {stale}"
    assert "--lives" in stale[0]
    assert "--nope" in stale[1]


def test_which_commands_forward_is_read_from_the_cli() -> None:
    """Keyed on the parser, so this fails if `launch` stops forwarding.

    A hardcoded name list would keep excusing `launch`'s flags forever after
    the contract changed, which is the same staleness this module exists to
    catch — one level up, in the guard instead of the document.
    """
    assert _forwards_unknown_flags(["launch"]), "launch no longer forwards agent args"
    assert not _forwards_unknown_flags(["doctor"])
    assert not _forwards_unknown_flags([]), "the root must not forward"


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

    THE WHOLE REPOSITORY IS SWEPT, not a guessed list of documentation
    directories. Root-plus-`docs/` was that guess and it was wrong:
    `.github/ISSUE_TEMPLATE/bug_report.md` asks a user to run `aisquare doctor`
    and sat outside it, so the mechanism whose only job is "a new .md must not
    silently escape" could not see a whole directory.

    IT ALSO ASKS THE EXTRACTOR RATHER THAN REIMPLEMENTING IT. This used to match
    `^aisquare` itself — the rule from before absolute paths, `exec`, sequencers
    and blockquoted fences were handled — so a page whose commands all looked
    like `exec /usr/local/bin/aisquare …` read as containing none, and would
    have escaped while the detector reported everything covered. Two copies of
    one rule, and the copy went stale; there is one now.
    """
    unlisted = _staleness_sweep(REPO)
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
    # `…/aisquare` with a WORD after it is an invocation by absolute path, not a
    # path. Without the lookahead this reason silently swallowed a cron
    # wrapper's `exec /usr/local/bin/aisquare explainability ship --strict`.
    ("a path segment, not a command word", re.compile(r"[/\w-]/aisquare(?!\s+\S)|aisquare[/-]")),
    ("a dotted Python module path", re.compile(r"aisquare\.\w")),
    ("a comment", re.compile(r"^\s*#")),
    ("nested in a command substitution the extractor cannot see into", re.compile(r"\$\(")),
    # `key: value` is how every status/doctor block in this runbook prints. Those
    # blocks quote real commands ("next: aisquare doctor --live") as SAMPLE
    # OUTPUT — drift there is a stale transcript, which is a doc bug, not a
    # broken command Jatin runs.
    ("a `key: value` line of sample CLI output", re.compile(r"^\s*[\w. -]+:\s")),
    ("a prose line inside an output block", re.compile(r"^\s*[→✓✗•]")),
    # SHADOWED, measured 2026-08-18: across all four documents this reason is
    # never the FIRST to classify a line — all eight URL mentions are already
    # caught by `aisquare[/-]` or the dotted-module pattern above. Kept rather
    # than deleted, because it stops being dead the moment either of those is
    # narrowed, and annotated rather than left silent, because a reader would
    # otherwise believe it is what classifies URLs.
    ("a URL", re.compile(r"https?://")),
    ("a pip requirement — the package name, not the command", re.compile(r"pip\s+install")),
    # `command -v aisquare` and `which aisquare` name the binary to LOCATE it;
    # the CLI is never run. Both arrived from §0/§5b edits that stopped guessing
    # a path, and `which aisquare` had been INVISIBLE here until then only
    # because it shared a line with `&& aisquare --version` — the resolvable
    # half was carrying the whole line. Splitting them exposed it, which is the
    # guard behaving correctly on a line it had never really checked.
    (
        "the argument to a locator (`command -v`, `which`), not an invocation",
        re.compile(r"(?:command\s+-v|which)\s+aisquare"),
    ),
)


#: Resolved / classified counts measured per document, so a collapse in either
#: number is visible instead of averaging out across the three.
#:
#: RE-MEASURE THESE WHEN A DOCUMENT GROWS. They are floors (0.8 and 0.6 below),
#: and a floor taken from a smaller document decays into decoration: the runbook
#: entry read (12, 29) while the file had reached (18, 37), so the guard would
#: have tolerated the extractor losing EIGHT of eighteen commands and fifteen of
#: thirty-seven classified mentions without a word. Measured 2026-08-18 by
#: raising each entry to an absurd value and reading the counts out of the
#: failure message, which is the only way this file reports them.
CENSUS = {
    ".github/ISSUE_TEMPLATE/bug_report.md": (1, 0),
    "docs/runbooks/MORNING-HANDOFF.md": (1, 0),
    "README.md": (55, 5),
    "docs/explainability-tracing-boundary.md": (2, 0),
    "docs/runbooks/explainability-prod-cutover.md": (18, 37),
}


@pytest.mark.parametrize("document", DOCUMENTED)
def test_every_aisquare_mention_is_classified(document: str) -> None:
    """A skip must be a decision, not an omission.

    Measured at the time of writing — resolved + classified + 0 unaccounted:
    README 60 = 55 + 5, tracing-boundary 2 = 2 + 0, cutover runbook 40 = 12 + 28
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
    # THE RECORD IS ALSO A FLOOR, SO THE RECORD MUST STAY TRUE. The two
    # assertions above compare reality against CENSUS, which means CENSUS is the
    # guard — and a hand-typed number can be lowered. Measured: replacing every
    # entry with (0, 0) leaves all 28 tests green, because a floor of zero is
    # satisfied by anything. You cannot defend a constant by adding another
    # constant, so this asserts the record has not drifted far BELOW reality
    # either: it must stay roughly true in both directions, and a document that
    # genuinely grows makes this fail on purpose, as a prompt to re-measure.
    assert was_resolved >= len(extracted) * 0.5, (
        f"{document}: CENSUS records {was_resolved} resolved but the extractor "
        f"finds {len(extracted)}. The recorded number is the floor every other "
        "assertion here leans on; a record this far below reality makes them "
        "vacuous. Re-measure and update CENSUS."
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


def test_a_blockquoted_fence_is_still_a_fence() -> None:
    """@dfd9a883 found this by writing an artifact the guard could not see.

    The runbook uses `> ```bash` for asides that carry REAL commands — line 512
    is `aisquare --json explainability status | jq -c .shipping`, a check an
    operator runs. FENCE required the backticks at the start of the line, so the
    open never matched, `inside` never flipped, and the whole block was prose.

    The give-away that this was half-built rather than declined: `_shell_lines`
    ALREADY strips a `> ` marker per line, with a comment naming the exact
    `> ```bash` shape. Line handling existed; fence detection did not.

    Ruling, since it was handed to me: A BLOCKQUOTED FENCE IS A FENCE. The
    blockquote styles an aside; the fence still says "this is a script". Inline
    code inside a blockquote — CONTRIBUTING.md's `pip install` that must NOT be
    run — stays invisible, because that is inline code, not a fenced block, and
    the convention that separates them is unchanged.
    """
    markdown = "\n".join(
        [
            "Some prose.",
            "",
            "> ```bash",
            "> aisquare --json explainability status | jq -c .shipping",
            "> ```",
            "",
        ]
    )

    found = _from_text("doc.md", markdown)

    # The pipeline stays in the stored text: a single `|` is not a sequencer, so
    # the segment is the whole line, and `_split` drops the tail later. Asserting
    # the trimmed form here would be asserting `_split`'s job in `_from_text`'s
    # test — which is how the first version of this assertion was wrong.
    expected = "aisquare --json explainability status | jq -c .shipping"
    assert [invocation.text for invocation in found] == [expected], (
        f"a blockquoted fence was read as prose: {found}"
    )
    chain, _legal, dangling = _walk(_split(found[0].text)[0])
    assert chain == ["explainability", "status"] and dangling is None


def test_the_runbooks_own_blockquoted_command_is_seen() -> None:
    """Against the real file, not a hand-built string.

    A synthetic markdown fixture proves the parser; only the document proves the
    coverage. This is the invocation that was invisible on the train.
    """
    runbook = "docs/runbooks/explainability-prod-cutover.md"
    text = (REPO / runbook).read_text(encoding="utf-8")
    lines = text.splitlines()

    # Keyed on the BLOCKQUOTE, not on the command: `explainability status` also
    # appears in an ordinary fence, so asserting the chain resolved would have
    # passed with this whole feature reverted. Asked instead: is any extracted
    # invocation sitting on a line that starts with a quote marker?
    quoted = [
        inv for inv in _from_text(runbook, text) if lines[inv.line - 1].lstrip().startswith(">")
    ]

    assert quoted, (
        "no invocation was extracted from a blockquoted fence, though the runbook "
        "has one — the coverage this test exists for is absent"
    )
    for invocation in quoted:
        chain, _legal, dangling = _walk(_split(invocation.text)[0])
        assert chain and dangling is None, f"{invocation.where} did not resolve: {invocation.text}"


def test_an_invocation_by_absolute_path_is_not_dismissed_as_a_path() -> None:
    """The second blind spot, and this one was WORSE than not matching.

    A cron wrapper runs `exec /usr/local/bin/aisquare explainability ship
    --strict` — an absolute path because cron has no useful PATH. The extractor
    did not match it, which alone would be a stated boundary. But the census
    then CLASSIFIED it as "a path segment, not a command word" and skipped it
    silently, so the one mechanism meant to make skips visible was hiding it.

    A path segment has no space after `aisquare`; an invocation does. That is
    the whole distinction and it is enough.
    """
    invocations = _from_text(
        "doc.md",
        "```bash\nexec /usr/local/bin/aisquare explainability ship --strict\n```\n",
    )

    assert invocations, "an absolute-path invocation was not extracted"
    chain, _legal, dangling = _walk(_split(invocations[0].text)[0])
    assert chain == ["explainability", "ship"] and dangling is None, (
        f"resolved to {chain}, dangling {dangling!r}"
    )


def test_a_real_path_is_still_read_as_a_path() -> None:
    """The boundary the fix must not cross.

    The runbook's §2 writes `/home/work/.config/aisquare/explainability-prod.env`.
    Nothing follows `aisquare` there but a slash, so it stays a path and stays
    classified — widening the invocation rule must not turn every configured
    file path into a command the guard tries to resolve.
    """
    line = "install -m 600 /dev/null /home/work/.config/aisquare/explainability-prod.env"

    assert not _from_text("doc.md", f"```bash\n{line}\n```\n"), (
        "a filesystem path was extracted as a command"
    )
    assert any(pattern.search(line) for _reason, pattern in _NOT_AN_INVOCATION), (
        "the path stopped being classified, so it would now fail the census"
    )


def _staleness_sweep(root: Path) -> list[str]:
    """The documents the staleness detector would flag under `root`."""
    unlisted: list[str] = []
    for path in sorted(root.rglob("*.md")):
        if any(part in _SWEEP_EXCLUDES for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in DOCUMENTED:
            continue
        if _from_text(relative, path.read_text(encoding="utf-8")):
            unlisted.append(relative)
    return unlisted


def test_the_staleness_detector_sweeps_where_documents_actually_live(tmp_path: Path) -> None:
    """The detector that catches an unguarded page had a smaller universe than
    the guard it protects.

    It swept the repo root and `docs/` only. `.github/ISSUE_TEMPLATE/` holds a
    page that asks a user to run a command, and it sat outside the sweep — so
    the mechanism whose entire job is "a new .md must not silently escape"
    could not see a whole directory. Same shape as the census blind spot
    @dfd9a883 ruled on, one level further out: there the SKIPS were invisible,
    here a DIRECTORY was.
    """
    (tmp_path / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True)
    (tmp_path / ".github" / "ISSUE_TEMPLATE" / "bug.md").write_text(
        "Run this and paste it:\n\n```sh\naisquare doctor\n```\n", encoding="utf-8"
    )

    assert ".github/ISSUE_TEMPLATE/bug.md" in _staleness_sweep(tmp_path), (
        "a page under .github showing a command is invisible to the detector"
    )


def test_the_staleness_detector_uses_the_same_rule_as_the_extractor(tmp_path: Path) -> None:
    """It had its own copy of the extraction rule, and the copy went stale.

    The detector asked `^aisquare` of each fenced line — the rule this file used
    BEFORE absolute paths, `exec`, sequencers and blockquoted fences were
    handled. So a new page whose commands are all
    `exec /usr/local/bin/aisquare …` — the shape of the cron wrapper §5b now
    ships — would have been reported as containing no commands at all, and
    escaped coverage while the detector said everything was listed.

    Two implementations of one rule is the defect; the fix is that there is now
    one, and this test fails if a second ever appears.
    """
    page = tmp_path / "docs" / "timers.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "> ```bash\n> exec /usr/local/bin/aisquare explainability ship --strict\n> ```\n",
        encoding="utf-8",
    )

    assert "docs/timers.md" in _staleness_sweep(tmp_path), (
        "a page whose only commands use an absolute path inside a blockquoted "
        "fence is invisible to the detector, though the extractor reads both"
    )


def test_no_skip_reason_can_excuse_a_plain_command() -> None:
    """The census can be narrowed to nothing and nothing currently says so.

    `_NOT_AN_INVOCATION` is what makes "0 unaccounted" mean something: every
    fenced mention must be resolved OR match a stated reason. Add one reason
    broad enough to match anything — `re.compile(r".")` is enough — and every
    skip is excused, the census passes, and the guard checks nothing. Measured:
    that sabotage leaves all 27 tests green.

    Same category as the damage-shape deletion in
    `test_no_traceback_on_a_damaged_store.py`: a change that breaks no
    assertion and simply makes every assertion cover less. @dfd9a883 asked for
    the pattern to be generalised to this file and this is that.

    THE PROPERTY, and it is narrower than "no reason matches a documented line":
    two REAL runbook invocations already match a reason — one carries a path in
    a comment, one carries a URL — and that is harmless, because extraction wins
    and classification never sees a line that resolved. What must never happen
    is a reason matching a BARE COMMAND, which is the shape every excuse would
    have to swallow to hide a real invocation.

    The corpus is built from the command tree, so it cannot go stale as commands
    are renamed.
    """
    commands = [f"aisquare {' '.join(chain)}" for chain in _leaf_chains()[:40]]
    assert len(commands) >= 20, f"only {len(commands)} commands to test against"

    excused = [
        (command, reason)
        for command in commands
        for reason, pattern in _NOT_AN_INVOCATION
        if pattern.search(command)
    ]

    assert not excused, (
        "these skip reasons match a plain command, so they can excuse a real "
        f"invocation from the census: {excused[:5]}\n"
        "A reason must describe why some text is NOT a command. One that also "
        "matches commands makes the '0 unaccounted' assertion vacuous."
    )


def _leaf_chains() -> list[list[str]]:
    """Every runnable command in the tree, deepest names included."""
    found: list[list[str]] = []

    def walk(node: Any, chain: list[str]) -> None:
        children = _subcommands(node)
        if not children:
            found.append(chain)
            return
        for name, child in sorted(children.items()):
            walk(child, [*chain, name])

    walk(_root(), [])
    return found
