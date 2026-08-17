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
- Only the command that starts a line is parsed. A command nested inside
  `eval "$(...)"` is not extracted; the runbook has one, and it was checked by
  hand instead.
- A flag's VALUE is not validated, only its existence. `--target prod` proves
  nothing about whether a target named prod is configured.
"""

from __future__ import annotations

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


def _invocations() -> list[Invocation]:
    found: list[Invocation] = []
    for name in DOCUMENTED:
        path = REPO / name
        if not path.exists():
            continue
        for number, text in _shell_lines(path.read_text(encoding="utf-8")):
            if re.match(r"^aisquare(\s|$)", text):
                found.append(Invocation(name, number, text))
    return found


def _split(command: str) -> tuple[list[str], list[str]]:
    """(bare words in order, long flags). Pipelines and comments are dropped."""
    command = command.split("#", 1)[0]
    for terminator in ("|", "&&", ";", ">"):
        command = command.split(terminator, 1)[0]
    try:
        tokens = shlex.split(command.strip())
    except ValueError:
        return [], []
    words: list[str] = []
    flags: list[str] = []
    skip_value = False
    for token in tokens[1:]:  # drop "aisquare"
        if token.startswith("--"):
            flags.append(token.split("=", 1)[0])
            skip_value = "=" not in token
            continue
        if token.startswith("-"):
            skip_value = True
            continue
        if skip_value:
            skip_value = False
            continue
        words.append(token)
    return words, flags


def _resolve(words: list[str]) -> tuple[list[str], set[str]]:
    """Walk the real command tree as far as the words go.

    Returns the chain that resolved and every long flag legal on it, including
    the options inherited from each parent. Stops at the first word that is not
    a subcommand — that word is an argument (`task claim <id>`, `launch coder`).
    """
    node = get_command(app)
    chain: list[str] = []
    flags = {opt for param in node.params for opt in param.opts if opt.startswith("--")}
    for word in words:
        children = getattr(node, "commands", None)
        if not children or word not in children:
            break
        node = children[word]
        chain.append(word)
        flags |= {opt for param in node.params for opt in param.opts if opt.startswith("--")}
        flags |= {
            opt for param in node.params for opt in param.secondary_opts if opt.startswith("--")
        }
    return chain, flags


@pytest.fixture(scope="module")
def documented() -> list[Invocation]:
    return _invocations()


def test_the_extractor_found_the_documented_commands(documented: list[Invocation]) -> None:
    """Guard the guard: a parser that matches nothing would pass every assertion."""
    assert len(documented) >= 30, f"only {len(documented)} invocations found — parser broke"
    by_document = {invocation.document for invocation in documented}
    assert "README.md" in by_document
    assert "docs/runbooks/explainability-prod-cutover.md" in by_document


def test_no_documented_command_uses_prose(documented: list[Invocation]) -> None:
    """Only fenced blocks are read, so a sentence must never be extracted.

    The README opens a paragraph with "aisquare has two halves"; if that ever
    starts being parsed as a command, this guard's failures stop being trusted.
    """
    for invocation in documented:
        words, _ = _split(invocation.text)
        assert "has" not in words[:1], f"{invocation.where} parsed prose as a command"


def test_every_documented_subcommand_exists(documented: list[Invocation]) -> None:
    unknown: list[str] = []
    for invocation in documented:
        words, _ = _split(invocation.text)
        if not words:
            continue  # bare `aisquare`, or only flags
        chain, _flags = _resolve(words)
        if not chain:
            unknown.append(f"{invocation.where}  {invocation.text}")
    assert not unknown, "documented commands that do not exist:\n  " + "\n  ".join(unknown)


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
    assert not missing, "documented flags that do not exist:\n  " + "\n  ".join(missing)


def test_the_document_list_has_not_gone_stale() -> None:
    """A new .md full of commands must not silently escape this guard."""
    unlisted: list[str] = []
    for path in [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]:
        relative = path.relative_to(REPO).as_posix()
        if relative in DOCUMENTED:
            continue
        if any(
            re.match(r"^aisquare(\s|$)", text)
            for _number, text in _shell_lines(path.read_text(encoding="utf-8"))
        ):
            unlisted.append(relative)
    assert not unlisted, (
        "these documents show commands but are not covered by this guard: "
        f"{unlisted} — add them to DOCUMENTED"
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
    lines = _tree_lines(text)
    assert lines, "no command tree found in the README — this guard is not looking at anything"

    stale: list[str] = []
    checked = 0
    current: str | None = None
    for number, line in lines:
        head = re.match(r"^[├└]──\s+([a-z][a-z0-9-]*)", line.strip())
        if head:
            current = head.group(1)
        legal = _flags_under(current)
        for flag in re.findall(r"--[a-z][a-z0-9-]*", line):
            checked += 1
            if flag not in legal:
                stale.append(
                    f"README.md:{number}  {flag} is not under `{current}`  | {line.strip()}"
                )

    assert checked >= 20, f"only {checked} flags found in the tree — the parser broke"
    assert not stale, "the command tree documents flags that do not exist:\n  " + "\n  ".join(stale)


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
