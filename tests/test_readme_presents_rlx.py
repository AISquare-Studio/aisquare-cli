"""The README presents rlx: the product's name, what is live, and nothing defensive.

The owner's ruling of 2026-11-16: the product is named rlx, short for relax; `rlx`, `relax`,
`asq` and `aisquare` all start it; the README never says "CLI", says nothing of pricing or of
what "stays free" (everything is free, and such lines read as a price to come), and names
only what is live, so nothing experimental and no sign-in that nothing live needs.

Four rules, each a callable the controls below reach with known input (CONTRIBUTING,
"Writing a guard that still guards"):

- ``_title`` — the page opens with ``# rlx``, and says once that the name is short for relax.
- ``_banned`` — no line says what the ruling took out: "CLI", "free", pricing, "experimental",
  the captain, "off by default", "no account", or the AISquare sign-in (``… login``).
  "CLI" is matched in capitals only: the package and the repository stay ``aisquare-cli``
  until the owner renames them, and their URLs are links that must keep working.
- ``_commands_not_written_rlx`` — every command is written ``rlx …``: a fenced shell line or
  an inline code span that runs another of the four names with a word after it is named.
  The one line that lists the other names writes each alone, as `` `asq` ``.
- ``_aliases_unnamed`` — that one line exists: `` `relax` ``, `` `asq` `` and `` `aisquare` ``.

Each rule must also still SEE something in the real README, so a rule gone blind cannot pass
by finding nothing: the title, at least one ``rlx`` command, and the alias line.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"

#: What the ruling took out, each with why, as the failure message names it.
_BANNED = (
    ('"CLI": the product is rlx', re.compile(r"\bCLI\b")),
    ("pricing or what stays free", re.compile(r"\bfree\b|\bpric(?:e|es|ing)\b", re.IGNORECASE)),
    ("an experimental feature", re.compile(r"\bexperimental\b", re.IGNORECASE)),
    ("the captain, which ships off", re.compile(r"\bcaptain\b", re.IGNORECASE)),
    ("a feature that is off by default", re.compile(r"\boff by default\b", re.IGNORECASE)),
    ("defensive messaging", re.compile(r"\bno account\b", re.IGNORECASE)),
    ("the AISquare sign-in", re.compile(r"\b(?:aisquare|asq|rlx|relax) login\b")),
)

#: The other three names, run with a word after them: a command not written as `rlx …`.
_OTHER_NAME_RUN = re.compile(r"^(?:\S*/)?(?:aisquare|asq|relax)\s+[\w-]")
_FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_-]*)\s*$")
_SHELL = {"", "sh", "bash", "shell", "console", "zsh"}
_INLINE = re.compile(r"(`+)(.+?)\1")
_RLX_RUN = re.compile(r"^rlx\s+[\w-]")


def _code(markdown: str) -> list[tuple[int, str]]:
    """Every fenced shell line and every inline code span, with its line number."""
    found: list[tuple[int, str]] = []
    inside = shell = False
    for number, line in enumerate(markdown.splitlines(), start=1):
        fence = _FENCE.match(line)
        if fence:
            inside, shell = (False, False) if inside else (True, fence.group(1) in _SHELL)
            continue
        if inside:
            if shell:
                found.append((number, line.strip()))
            continue
        found.extend((number, span.strip()) for _ticks, span in _INLINE.findall(line))
    return found


def _title(markdown: str) -> list[str]:
    problems: list[str] = []
    first = markdown.splitlines()[0] if markdown else ""
    if first != "# rlx":
        problems.append(f"the page opens with {first!r}, not '# rlx'")
    said = len(re.findall(r"short for relax", markdown, re.IGNORECASE))
    if said != 1:
        problems.append(f"'short for relax' is said {said} times, not once")
    return problems


def _banned(markdown: str) -> list[str]:
    return [
        f"line {number}: {why}: {line.strip()!r}"
        for number, line in enumerate(markdown.splitlines(), start=1)
        for why, pattern in _BANNED
        if pattern.search(line)
    ]


def _commands_not_written_rlx(markdown: str) -> list[str]:
    return [
        f"line {number}: {code!r}"
        for number, code in _code(markdown)
        if _OTHER_NAME_RUN.match(code)
    ]


def _rlx_commands(markdown: str) -> list[str]:
    return [code for _number, code in _code(markdown) if _RLX_RUN.match(code)]


def _aliases_unnamed(markdown: str) -> list[str]:
    spans = {code for _number, code in _code(markdown)}
    return [name for name in ("relax", "asq", "aisquare") if name not in spans]


# --- the README --------------------------------------------------------------------------------


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_readme_is_titled_rlx_short_for_relax() -> None:
    assert not _title(_readme())


def test_the_readme_says_nothing_the_ruling_took_out() -> None:
    offending = _banned(_readme())
    assert not offending, "README.md:\n  " + "\n  ".join(offending)


def test_every_readme_command_is_written_rlx() -> None:
    text = _readme()
    assert _rlx_commands(text), "no `rlx …` command found: the code reader went blind"
    offending = _commands_not_written_rlx(text)
    assert not offending, (
        "README.md writes a command with another name; write it `rlx …`:\n  "
        + "\n  ".join(offending)
    )


def test_the_readme_names_the_other_commands_once_each() -> None:
    assert not _aliases_unnamed(_readme())


# --- controls: synthetic input, so they keep controlling whatever the README says -----------------


def test_a_title_that_is_not_rlx_is_caught() -> None:
    assert _title("# aisquare\n\nrlx is short for relax.\n")
    assert _title("# rlx\n\nNo word on the name.\n")
    assert _title("# rlx\n\nShort for relax. Short for relax.\n")
    assert _title("") == [
        "the page opens with '', not '# rlx'",
        "'short for relax' is said 0 times, not once",
    ]
    assert not _title("# rlx\n\nrlx is short for relax.\n")


def test_each_banned_shape_is_caught_and_plain_words_are_not() -> None:
    accused = {
        "It's a single CLI.": '"CLI"',
        "Everything stays free.": "pricing",
        "There is no pricing.": "pricing",
        "The captain (experimental).": "experimental",
        "Ask the Captain.": "captain",
        "It ships off by default.": "off by default",
        "Needs no account.": "defensive",
        "Sign in with `aisquare login`.": "sign-in",
        "Sign in with `rlx login`.": "sign-in",
    }
    for line, why in accused.items():
        found = _banned(line)
        assert found and why in found[0], (line, found)
    innocent = (
        "[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)",
        "uv tool install --python 3.13 aisquare-cli",
        "If it is signed out, it offers the sign-in.",
        "A Codex account signs in with a device code.",
        "Opt-in, anonymous daily counts of how rlx is used.",
    )
    for line in innocent:
        assert not _banned(line), line


def test_a_command_written_with_another_name_is_caught() -> None:
    for name in ("aisquare", "asq", "relax", "/usr/local/bin/aisquare"):
        fenced = f"Run:\n\n```sh\n{name} fleet ls\n```\n"
        inline = f"Then `{name} doctor` says so.\n"
        assert _commands_not_written_rlx(fenced) == [f"line 4: '{name} fleet ls'"], name
        assert _commands_not_written_rlx(inline) == [f"line 1: '{name} doctor'"], name


def test_rlx_commands_and_the_alias_line_are_not_accused() -> None:
    page = (
        "`relax`, `asq` and `aisquare` start it too.\n\n"
        "```sh\nrlx fleet ls\ncurl -fsSL https://x/aisquare-cli/main/install.sh | sh\n```\n\n"
        "```text\naisquare fleet ls\n```\n"
    )
    assert _commands_not_written_rlx(page) == [], "an rlx command, a URL or a non-shell block"
    assert _rlx_commands(page) == ["rlx fleet ls"]
    assert _aliases_unnamed(page) == []


def test_a_missing_alias_is_named() -> None:
    assert _aliases_unnamed("`relax` and `asq` start it too.\n") == ["aisquare"]
    assert _aliases_unnamed("") == ["relax", "asq", "aisquare"]
