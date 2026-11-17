"""The README presents rlx: the product's name, what is live, and nothing defensive.

The owner's ruling of 2026-11-16: the product is named rlx; `rlx`, `relax`, `asq` and
`aisquare` all start it; the README never says "CLI", says nothing of pricing or of
what "stays free" (everything is free, and such lines read as a price to come), and names
only what is live, so nothing experimental. The AISquare integration is AISquare's and keeps
its name (the owner's ruling, also 2026-11-16): the sign-in (``aisquare login``, ``logout``,
``whoami``, ``auth``), ``aisquare init --api-key``, Explainability (``aisquare
explainability …``) and the connectors' sync stub (``aisquare connectors …``) are never
written as rlx. And the owner's rules for marketing copy, also 2026-11-16 (RLX R14): "stop
saying short for relax, dont treat the consumer as stupid, use less words", and "Never print a
version number in marketing copy."

Seven rules, each a callable the controls below reach with known input (CONTRIBUTING,
"Writing a guard that still guards"):

- ``_title`` — the page opens with ``# rlx``, and never explains the name ("short for relax").
  Nor does any page the README links, or PyPI's summary (``NAMED_PAGES``).
- ``_versions`` — no version number (``v0.10.0``, ``0.9.0``) outside the Requirements section
  and fenced code: a pin is written ``vX.Y.Z``. Requirements' minimums (Codex, tmux, Node,
  Python) are what a machine needs, not copy, and a fence is a command as it is typed.
- ``_says_cli`` — no line says "cli" as a word, in any case. URLs and the package name
  ``aisquare-cli`` are taken out of the line first: the package and the repository keep that
  name until the owner renames them, and their links must keep working.
- ``_banned`` — no line says what else the ruling took out: "free", pricing ("costs nothing",
  "paid plan"), "experimental", the captain, "off by default" or "no account".
- ``_commands_not_written_rlx`` — every command is written ``rlx …``. Read in every fenced
  line, whatever the fence's language, and every inline code span, each split at ``&&``,
  ``||``, ``;`` and ``|``, with a console prompt (``$``, ``%``, ``>``), ``VAR=value``s,
  ``exec``, ``sudo``, ``uvx [--from PKG]`` and ``uv run`` read past: another of the four names
  run with a word after it is named, unless what it runs is the AISquare integration, which
  stays ``aisquare …``. ``python -m`` is not read past: the module is ``aisquare`` whatever
  the command is called, and ``python -m rlx`` does not exist.
- ``_integration_renamed`` — the AISquare integration's commands are never run as ``rlx``,
  ``relax`` or ``asq``: ``rlx login`` is named wherever a line says it, in code or prose.
- ``_aliases_unnamed`` — that one line exists: `` `relax` ``, `` `asq` `` and `` `aisquare` ``.

SECURITY.md, which the README links, is held to the three rules that fit an inventory whose
technical content must stay exact: no "cli", commands written ``rlx …``, and the AISquare
integration written ``aisquare …``, as what ``aisquare login`` stores and sends is.

Each rule must also still SEE something in the real pages, so a rule gone blind cannot pass
by finding nothing: the title, ``rlx`` commands on both pages, the alias line, and
SECURITY.md's ``aisquare login``. ``_integration_renamed`` also reads every other page
whose product wording this train changed (``RENAMED_PAGES``).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
SECURITY = REPO / "SECURITY.md"
#: Every page whose product wording the rlx train changed: none may rename the integration.
RENAMED_PAGES = (
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "docs/multi-instance.md",
    "docs/usage-telemetry.md",
    "plugins/codex/README.md",
)

#: Where the name could be explained: every renamed page, and PyPI's summary in pyproject.
NAMED_PAGES = (*RENAMED_PAGES, "pyproject.toml")
_SHORT_FOR = re.compile(r"short\s+for\s+relax", re.IGNORECASE)

#: A version number. Copy names none; the one section that may is Requirements.
_VERSION = re.compile(r"(?<![\w.])v?\d+\.\d+\.\d+\b")
_VERSIONS_KEPT_IN = "## Requirements"

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
)

#: What the AISquare integration's commands run, after the program's name: they keep
#: `aisquare`, the AISquare product's name, under every name the program has.
_INTEGRATION = (
    r"(?:(?:login|logout|whoami|auth|explainability|connectors)\b|init\b[^`|;&]*--api-key\b)"
)
#: The integration run under one of the product's other names, anywhere on a line.
_INTEGRATION_RENAMED = re.compile(r"(?<![\w/-])(?:rlx|relax|asq)\s+" + _INTEGRATION)

_FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_-]*)\s*$")
_INLINE = re.compile(r"(`+)(.+?)\1")
_PROMPT = re.compile(r"^[$%>]\s+")
_SEQUENCE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
#: What may stand before the program's name on a line that runs it.
_LAUNCHER = (
    r"^(?:\w+=\S*\s+)*"
    r"(?:(?:exec|sudo)\s+)?"
    r"(?:uvx\s+(?:--from\s+\S+\s+)?|uv\s+run\s+)?"
    r"(?:\S*/)?"
)
#: The other three names, run with a word after them: a command not written as `rlx …`.
_OTHER_NAME_RUN = re.compile(_LAUNCHER + r"(?:aisquare|asq|relax)\s+[\w-]")
#: The AISquare integration as it is written: `aisquare login`, and so on.
_INTEGRATION_KEPT = re.compile(_LAUNCHER + r"aisquare\s+" + _INTEGRATION)
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
    said = len(_SHORT_FOR.findall(markdown))
    if said:
        problems.append(f"'short for relax' is said {said} times: the name needs no explaining")
    return problems


def _versions(markdown: str) -> list[str]:
    """Every version number outside fenced code and the Requirements section."""
    found: list[str] = []
    inside = False
    section = ""
    for number, line in enumerate(markdown.splitlines(), start=1):
        if _FENCE.match(line):
            inside = not inside
            continue
        if inside:
            continue
        if line.startswith("#"):
            section = line.strip()
        if section == _VERSIONS_KEPT_IN:
            continue
        found.extend(f"line {number}: {v!r} in {line.strip()!r}" for v in _VERSION.findall(line))
    return found


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
        if _OTHER_NAME_RUN.match(segment) and not _INTEGRATION_KEPT.match(segment)
    ]


def _integration_renamed(markdown: str) -> list[str]:
    return [
        f"line {number}: {line.strip()!r}"
        for number, line in enumerate(markdown.splitlines(), start=1)
        if _INTEGRATION_RENAMED.search(_KEPT_NAMES.sub("", line))
    ]


def _integration_kept(markdown: str) -> list[str]:
    return [segment for _number, segment in _segments(markdown) if _INTEGRATION_KEPT.match(segment)]


def _rlx_commands(markdown: str) -> list[str]:
    return [segment for _number, segment in _segments(markdown) if _RLX_RUN.match(segment)]


def _aliases_unnamed(markdown: str) -> list[str]:
    spans = {code for _number, code in _code(markdown)}
    return [name for name in ("relax", "asq", "aisquare") if name not in spans]


# --- the README and SECURITY.md ----------------------------------------------------------------


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_readme_is_titled_rlx_and_does_not_explain_the_name() -> None:
    assert not _title(_readme())


def test_no_page_explains_the_name() -> None:
    offending = [page for page in NAMED_PAGES if _SHORT_FOR.search((REPO / page).read_text())]
    assert not offending, f"'short for relax' (the owner: say less): {offending}"


def test_the_readme_names_no_version_outside_requirements_and_code() -> None:
    text = _readme()
    offending = _versions(text)
    assert not offending, (
        "README.md prints a version number in its copy (the owner: never); a pin is "
        "written vX.Y.Z:\n  " + "\n  ".join(offending)
    )
    # The rule still sees: Requirements' own minimums are found once it is not exempt.
    assert _versions(text.replace(_VERSIONS_KEPT_IN, "## Needs")), "Requirements went unseen"


def test_the_readme_says_nothing_the_ruling_took_out() -> None:
    offending = _says_cli(_readme()) + _banned(_readme())
    assert not offending, "README.md:\n  " + "\n  ".join(offending)


def _security_problems(text: str) -> list[str]:
    return _says_cli(text) + _commands_not_written_rlx(text) + _integration_renamed(text)


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


def test_security_md_says_rlx_not_cli_and_keeps_the_aisquare_integration() -> None:
    text = SECURITY.read_text(encoding="utf-8")
    assert "rlx --version" in _rlx_commands(text), "SECURITY.md's commands went unseen"
    kept = _integration_kept(text)
    assert {"aisquare login", "aisquare init --api-key"} <= set(kept), (
        f"SECURITY.md's AISquare sign-in went unseen, or was renamed: {kept}"
    )
    offending = _security_problems(text)
    assert not offending, "SECURITY.md:\n  " + "\n  ".join(offending)


def test_no_page_runs_the_aisquare_integration_under_another_name() -> None:
    offending = [
        f"{page}: {found}"
        for page in RENAMED_PAGES
        for found in _integration_renamed((REPO / page).read_text(encoding="utf-8"))
    ]
    assert not offending, (
        "the AISquare integration keeps its name (the owner, 2026-11-16); write it "
        "`aisquare …`:\n  " + "\n  ".join(offending)
    )


# --- controls: synthetic input, so they keep controlling whatever the pages say ------------------


def test_a_title_that_is_not_rlx_or_explains_the_name_is_caught() -> None:
    assert _title("# aisquare\n\nMade by a developer.\n")
    assert _title("# rlx\n\nrlx is short for relax.\n")
    assert _title("# rlx\n\nrlx, Short\nfor relax.\n")
    assert _title("") == ["the page opens with '', not '# rlx'"]
    assert not _title("# rlx\n\nMade by a developer, for developers.\n")
    assert not _title("# rlx\n\n`relax` starts it too.\n")


def test_a_version_in_copy_is_caught_and_requirements_and_code_are_not() -> None:
    for line in (
        "To pin one, use v0.10.0.",
        "Every release from 0.9.0 on carries one.",
        "`curl -fsSL https://github.com/x/releases/download/v0.10.0/install.sh | sh`",
        "rlx 0.10.1 is out.",
    ):
        assert _versions(f"# rlx\n\n{line}\n"), line
    page = (
        "# rlx\n\nPin it: `download/vX.Y.Z/install.sh`, on Python 3.13, at 85% used.\n\n"
        "```sh\nuv tool install aisquare-cli==0.10.0\n```\n\n"
        "## Requirements\n\n- Codex 0.164.0 or newer, tmux 3.2+.\n\n"
        "## License\n\nMIT\n"
    )
    assert _versions(page) == []
    after = len(page.splitlines()) + 2  # past the section Requirements' exemption ended with
    assert _versions(page + "\nSince 0.9.0.\n") == [f"line {after}: '0.9.0' in 'Since 0.9.0.'"]


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
        "Sign in to your AISquare account with `aisquare login`.",
    )
    for line in innocent:
        assert not _banned(line), line


def test_the_aisquare_integration_renamed_is_caught_and_kept_is_not() -> None:
    renamed = (
        "Sign in with `{name} login`.",
        "`{name} logout` signs out.",
        "{name} whoami",
        "$ {name} auth status",
        "cd repo && {name} login",
        "Note that `{name} explainability doctor --fix",
        "`{name} connectors list`",
        "`{name} init --api-key KEY`",
        "`{name} init --local --api-key KEY`",
    )
    for shape in renamed:
        for name in ("rlx", "relax", "asq"):
            line = shape.format(name=name)
            assert _integration_renamed(line) == [f"line 1: {line!r}"], line
            assert _security_problems(line), line
        kept = shape.format(name="aisquare")
        assert _integration_renamed(kept) == [], kept
        assert _commands_not_written_rlx(kept) == [], kept
        assert _security_problems(kept) == [], kept
    for innocent in (
        "rlx init --local --agent claude-code",
        "`rlx serve` on a non-loopback address",
        "an SSH login can't start it",
        "the asq logins",
        "~/.aisquare/rlx login",
    ):
        assert _integration_renamed(innocent) == [], innocent


def test_security_md_s_own_sign_in_lines_pass_as_kept_and_fail_renamed() -> None:
    kept = (
        "- `credentials` (from `aisquare login`, `aisquare init --api-key` and\n"
        "  `rlx serve`) are restricted to your user.\n"
        "- **Signing in.** After `aisquare login`, which sends this machine's hostname,\n"
        "  rlx calls the AISquare API with your session. Note that `aisquare explainability"
        " doctor --fix\n  --target <name>` turns Explainability on.\n"
    )
    assert _security_problems(kept) == []
    assert _integration_kept(kept) == [
        "aisquare login",
        "aisquare init --api-key",
        "aisquare login",
    ]
    for name in ("rlx", "relax", "asq"):
        renamed = kept.replace("aisquare login", f"{name} login", 1)
        assert _security_problems(renamed), name
    assert _security_problems(kept.replace("aisquare explainability", "rlx explainability"))


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
    "uvx --from aisquare-cli {name} doctor",
    "uv run {name} doctor",
)
#: `python -m` runs the module, which is `aisquare` whatever the command is called: neither
#: written as rlx (`python -m rlx` does not exist) nor accused as another name.
_MODULE_RUNS = (
    "python -m aisquare doctor",
    "python3.12 -m aisquare doctor",
    "python -m rlx doctor",
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
    for command in _MODULE_RUNS:
        module = f"```sh\n{command}\n```\n\nThen `{command}`.\n"
        assert _commands_not_written_rlx(module) == [], command
        assert _rlx_commands(module) == [], command


def test_a_missing_alias_is_named() -> None:
    assert _aliases_unnamed("`relax` and `asq` start it too.\n") == ["aisquare"]
    assert _aliases_unnamed("") == ["relax", "asq", "aisquare"]
