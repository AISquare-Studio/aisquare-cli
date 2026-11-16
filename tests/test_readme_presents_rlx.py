"""The README presents rlx: the product's name, what is live, and nothing defensive.

The owner's ruling of 2026-11-16: the product is named rlx, short for relax; `rlx`, `relax`,
`asq` and `aisquare` all start it; the README never says "CLI", says nothing of pricing or of
what "stays free" (everything is free, and such lines read as a price to come), and names
only what is live, so nothing experimental and no sign-in that nothing live needs.

Five rules, each a callable the controls below reach with known input (CONTRIBUTING,
"Writing a guard that still guards"):

- ``_title`` — the page opens with ``# rlx``, and says once that the name is short for relax.
- ``_says_cli`` — no line says "cli" as a word, in any case. URLs and the package name
  ``aisquare-cli`` are taken out of the line first: the package and the repository keep that
  name until the owner renames them, and their links must keep working.
- ``_banned`` — no line says what else the ruling took out: "free", pricing ("costs nothing",
  "paid plan"), "experimental", the captain, "off by default", "no account", or the AISquare
  sign-in (``… login``, "AISquare account").
- ``_commands_not_written_rlx`` — every command is written ``rlx …``. Read in every fenced
  line, whatever the fence's language, and every inline code span, each split at ``&&``,
  ``||``, ``;`` and ``|``, with a console prompt (``$``, ``%``, ``>``), ``VAR=value``s,
  ``exec``, ``sudo``, ``python -m``, ``uvx [--from PKG]`` and ``uv run`` read past: another of
  the four names run with a word after it is named. The one line that lists the other names
  writes each alone, as `` `asq` ``.
- ``_aliases_unnamed`` — that one line exists: `` `relax` ``, `` `asq` `` and `` `aisquare` ``.

SECURITY.md, which the README links, is held to the two rules that fit an inventory whose
technical content must stay exact: no "cli", and commands written ``rlx …``. It names the
AISquare sign-in on purpose (what ``rlx login`` stores and sends).

Each rule must also still SEE something in the real pages, so a rule gone blind cannot pass
by finding nothing: the title, ``rlx`` commands on both pages, and the alias line.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
SECURITY = REPO / "SECURITY.md"

#: What a line keeps of the old name until the owner renames them: URLs and the package.
_KEPT_NAMES = re.compile(r"https?://\S+|aisquare-cli")
_CLI = re.compile(r"\bcli\b", re.IGNORECASE)

#: What the ruling took out, each with why, as the failure message names it.
_BANNED = (
    (
        "pricing or what stays free",
        re.compile(
            r"\bfree\b|\bpric(?:e|es|ing)\b|\bcosts? nothing\b|\bno cost\b|\bpaid plans?\b",
            re.IGNORECASE,
        ),
    ),
    ("an experimental feature", re.compile(r"\bexperimental\b", re.IGNORECASE)),
    ("the captain, which ships off", re.compile(r"\bcaptain\b", re.IGNORECASE)),
    ("a feature that is off by default", re.compile(r"\boff by default\b", re.IGNORECASE)),
    ("defensive messaging", re.compile(r"\bno account\b", re.IGNORECASE)),
    (
        "the AISquare sign-in",
        re.compile(
            r"\b(?:aisquare|asq|rlx|relax) login\b|\baisquare (?:account|sign-?in|login)\b",
            re.IGNORECASE,
        ),
    ),
)

_FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_-]*)\s*$")
_INLINE = re.compile(r"(`+)(.+?)\1")
_PROMPT = re.compile(r"^[$%>]\s+")
_SEQUENCE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
#: What may stand before the program's name on a line that runs it.
_LAUNCHER = (
    r"^(?:\w+=\S*\s+)*"
    r"(?:(?:exec|sudo)\s+)?"
    r"(?:python[\d.]*\s+-m\s+|uvx\s+(?:--from\s+\S+\s+)?|uv\s+run\s+)?"
    r"(?:\S*/)?"
)
#: The other three names, run with a word after them: a command not written as `rlx …`.
_OTHER_NAME_RUN = re.compile(_LAUNCHER + r"(?:aisquare|asq|relax)\s+[\w-]")
_RLX_RUN = re.compile(_LAUNCHER + r"rlx\s+[\w-]")


def _code(markdown: str) -> list[tuple[int, str]]:
    """Every fenced line, whatever the fence's language, and every inline code span outside
    a fence, with its line number."""
    found: list[tuple[int, str]] = []
    inside = False
    for number, line in enumerate(markdown.splitlines(), start=1):
        if _FENCE.match(line):
            inside = not inside
            continue
        if inside:
            found.append((number, line.strip()))
        else:
            found.extend((number, span.strip()) for _ticks, span in _INLINE.findall(line))
    return found


def _segments(markdown: str) -> list[tuple[int, str]]:
    """Each command a piece of code holds: its prompt dropped, split at the shell's
    sequencers and pipes."""
    return [
        (number, segment)
        for number, code in _code(markdown)
        for segment in _SEQUENCE.split(_PROMPT.sub("", code))
        if segment
    ]


def _title(markdown: str) -> list[str]:
    problems: list[str] = []
    first = markdown.splitlines()[0] if markdown else ""
    if first != "# rlx":
        problems.append(f"the page opens with {first!r}, not '# rlx'")
    said = len(re.findall(r"short for relax", markdown, re.IGNORECASE))
    if said != 1:
        problems.append(f"'short for relax' is said {said} times, not once")
    return problems


def _says_cli(markdown: str) -> list[str]:
    return [
        f"line {number}: {line.strip()!r}"
        for number, line in enumerate(markdown.splitlines(), start=1)
        if _CLI.search(_KEPT_NAMES.sub("", line))
    ]


def _banned(markdown: str) -> list[str]:
    return [
        f"line {number}: {why}: {line.strip()!r}"
        for number, line in enumerate(markdown.splitlines(), start=1)
        for why, pattern in _BANNED
        if pattern.search(_KEPT_NAMES.sub("", line))
    ]


def _commands_not_written_rlx(markdown: str) -> list[str]:
    return [
        f"line {number}: {segment!r}"
        for number, segment in _segments(markdown)
        if _OTHER_NAME_RUN.match(segment)
    ]


def _rlx_commands(markdown: str) -> list[str]:
    return [segment for _number, segment in _segments(markdown) if _RLX_RUN.match(segment)]


def _aliases_unnamed(markdown: str) -> list[str]:
    spans = {code for _number, code in _code(markdown)}
    return [name for name in ("relax", "asq", "aisquare") if name not in spans]


# --- the README and SECURITY.md ----------------------------------------------------------------


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_readme_is_titled_rlx_short_for_relax() -> None:
    assert not _title(_readme())


def test_the_readme_says_nothing_the_ruling_took_out() -> None:
    offending = _says_cli(_readme()) + _banned(_readme())
    assert not offending, "README.md:\n  " + "\n  ".join(offending)


def test_every_readme_command_is_written_rlx() -> None:
    text = _readme()
    assert len(_rlx_commands(text)) >= 10, "the README's `rlx …` commands went unseen"
    offending = _commands_not_written_rlx(text)
    assert not offending, (
        "README.md writes a command with another name; write it `rlx …`:\n  "
        + "\n  ".join(offending)
    )


def test_the_readme_names_the_other_commands_once_each() -> None:
    assert not _aliases_unnamed(_readme())


def test_security_md_says_rlx_not_cli() -> None:
    text = SECURITY.read_text(encoding="utf-8")
    assert "rlx --version" in _rlx_commands(text), "SECURITY.md's commands went unseen"
    offending = _says_cli(text) + _commands_not_written_rlx(text)
    assert not offending, "SECURITY.md:\n  " + "\n  ".join(offending)


# --- controls: synthetic input, so they keep controlling whatever the pages say ------------------


def test_a_title_that_is_not_rlx_is_caught() -> None:
    assert _title("# aisquare\n\nrlx is short for relax.\n")
    assert _title("# rlx\n\nNo word on the name.\n")
    assert _title("# rlx\n\nShort for relax. Short for relax.\n")
    assert _title("") == [
        "the page opens with '', not '# rlx'",
        "'short for relax' is said 0 times, not once",
    ]
    assert not _title("# rlx\n\nrlx is short for relax.\n")


def test_cli_is_caught_in_any_case_and_urls_and_the_package_are_not() -> None:
    for line in ("It's a single CLI.", "a small cli tool", "The Cli, then.", "rlx (cli)"):
        assert _says_cli(line), line
    innocent = (
        "[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)",
        "uv tool install --python 3.13 aisquare-cli",
        "codex plugin add aisquare@aisquare-cli",
        "`uvx --from aisquare-cli==<version>`",
        "[Codex](https://learn.chatgpt.com/docs/codex/cli)",
        "a remote client, a climb, a clinic",
    )
    for line in innocent:
        assert not _says_cli(line), line


def test_each_banned_shape_is_caught_and_plain_words_are_not() -> None:
    accused = {
        "Everything stays free.": "pricing",
        "There is no pricing.": "pricing",
        "It costs nothing.": "pricing",
        "A paid plan may come later.": "pricing",
        "The captain (experimental).": "experimental",
        "Ask the Captain.": "captain",
        "It ships off by default.": "off by default",
        "Needs no account.": "defensive",
        "Sign in with `aisquare login`.": "sign-in",
        "Sign in with `rlx login`.": "sign-in",
        "Sign in to your AISquare account.": "sign-in",
    }
    for line, why in accused.items():
        found = _banned(line)
        assert found and why in found[0], (line, found)
    innocent = (
        "[![PyPI](https://img.shields.io/pypi/v/aisquare-cli.svg)](https://pypi.org/project/aisquare-cli/)",
        "If it is signed out, it offers the sign-in.",
        "A Codex account signs in with a device code.",
        "Plans with only a weekly limit count too.",
        "Sign in several accounts per agent.",
        "Opt-in, anonymous daily counts of how rlx is used.",
    )
    for line in innocent:
        assert not _banned(line), line


#: Each shape a command can be written in, with another of the names at its head.
_OTHER_NAME_SHAPES = (
    "{name} fleet ls",
    "$ {name} fleet ls",
    "% {name} fleet ls",
    "cd repo && {name} init",
    "make build; {name} doctor",
    "echo hi | {name} remember --user x",
    "FOO=1 {name} doctor",
    "exec /usr/local/bin/{name} doctor",
    "python -m {name} doctor",
    "python3.12 -m {name} doctor",
    "uvx --from aisquare-cli {name} doctor",
    "uv run {name} doctor",
)


def test_a_command_written_with_another_name_is_caught_in_every_shape() -> None:
    for name in ("aisquare", "asq", "relax"):
        for shape in _OTHER_NAME_SHAPES:
            command = shape.format(name=name)
            for language in ("sh", "console", "text", ""):
                fenced = f"Run:\n\n```{language}\n{command}\n```\n"
                assert _commands_not_written_rlx(fenced), (language, command)
            assert _commands_not_written_rlx(f"Then `{command}` says so.\n"), command


def test_the_same_shapes_written_rlx_are_seen_and_not_accused() -> None:
    for shape in _OTHER_NAME_SHAPES:
        command = shape.format(name="rlx")
        page = f"```console\n{command}\n```\n\nThen `{command}`.\n"
        assert _commands_not_written_rlx(page) == [], command
        assert len(_rlx_commands(page)) == 2, command


def test_names_that_run_nothing_are_not_accused() -> None:
    page = (
        "`relax`, `asq` and `aisquare` start it too.\n\n"
        "```sh\n"
        "curl -fsSL https://x/aisquare-cli/main/install.sh | sh\n"
        "UV_PYTHON_DOWNLOADS=automatic uv tool install --python 3.13 aisquare-cli\n"
        "# aisquare doctor said so before\n"
        "```\n\n"
        "```text\n2 asq · Mac + ssh (here)\n```\n\n"
        "Kept in `~/.aisquare/instances/` and `refs/aisquare/wip/`.\n"
    )
    assert _commands_not_written_rlx(page) == []
    assert _rlx_commands(page) == []
    assert _aliases_unnamed(page) == []


def test_a_missing_alias_is_named() -> None:
    assert _aliases_unnamed("`relax` and `asq` start it too.\n") == ["aisquare"]
    assert _aliases_unnamed("") == ["relax", "asq", "aisquare"]
