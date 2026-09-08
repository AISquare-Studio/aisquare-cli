"""Static guards on `install.sh`, the one artifact that runs on other people's machines.

A `curl … | sh` installer is the least testable and highest-blast-radius thing a
project can ship (docs/plans/one-line-install.md §8.1), so the properties that
cannot be observed from inside a container test are asserted here, on the text.

WHAT IS HERE RATHER THAN IN THE CONTAINER MATRIX, and why the split is that way:
the matrix (tests/install/matrix.sh) proves the script WORKS; these tests prove
things about how it is written that would still be true of a script that happened
to work today. The `/dev/tty` guard is the clearest case — a script that read
stdin would pass every container cell, because a container has no terminal and
takes the unattended branch either way. It would fail the first time a human
piped it into a shell.

The heavy static tools (shellcheck, shfmt, dash, BusyBox ash) are NOT run from
here. They are not Python-importable, they are not in `.[dev]`, and a test that
silently skips when a binary is missing is a test that is green on every
developer machine and proves nothing. They run in
`.github/workflows/install.yml`, where their absence is a red job rather than a
skip. What is here is what Python can check without a toolchain — and every one
of these fires on a specific mistake that was either made while writing the
script or is one edit away.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "install.sh"
CELL = REPO / "tests/install/cell.sh"
MATRIX = REPO / "tests/install/matrix.sh"

#: Every shell file this project ships that has to run under `dash` and BusyBox
#: `ash`, not just bash.
POSIX_SCRIPTS = (SCRIPT, CELL, MATRIX)


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lines(source: str) -> list[str]:
    return source.splitlines()


def _code_lines(text: str) -> list[tuple[int, str]]:
    """Numbered lines with whole-line comments and blanks dropped.

    Comments are excluded because this file's whole subject is what the script
    DOES, and every one of the patterns below is discussed in a comment
    somewhere — a guard that reads its own explanation as a violation is a guard
    that gets deleted.
    """
    out = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append((number, line))
    return out


def test_the_installer_exists_and_is_executable() -> None:
    """`chmod +x` matters even though `curl | sh` does not need it.

    The documented alternative for anyone who will not pipe into a shell is
    "download it, read it, then run it" (§10), and that path runs `./install.sh`.
    """
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh is not executable"


def test_it_declares_a_posix_shell() -> None:
    """`#!/bin/sh`, not bash. On Debian that is dash and on Alpine BusyBox ash."""
    first = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/bin/sh", f"shebang is {first!r}, must be #!/bin/sh"


# --- §3.3: stdin is the script, not the terminal ----------------------------


def test_no_read_without_dev_tty(lines: list[str]) -> None:
    """THE guard this plan is most worried about (§3.3, §10).

    Under `curl … | sh` stdin is the pipe carrying the script's own bytes, so a
    bare `read -r answer` reads the REST OF THE SCRIPT as the answer — it either
    consumes the script or hits EOF and returns instantly, so the prompt appears
    to answer itself and the run ends.

    Invisible until a human tries it, and invisible to the container matrix too,
    because a container has no terminal and takes the unattended branch whatever
    the code says. Trivially greppable, so it is grepped: every `read` in the
    file must name `/dev/tty` on its own line.
    """
    # Anchored to where a COMMAND can start — line start, or after a separator.
    # `\s` was too loose: it matched the English "could not read the latest
    # version from PyPI" inside a warning message, so the guard's first run
    # reported a prose sentence as a terminal bug. A guard that cries wolf on
    # prose is a guard someone deletes.
    command_read = re.compile(
        r"(?:^\s*|;\s*|&&\s*|\|\|\s*|\|\s*|\{\s*|\bthen\s+|\bdo\s+|\belse\s+)read\s"
    )
    offenders = [
        (number, line.strip())
        for number, line in _code_lines("\n".join(lines))
        if command_read.search(line) and "/dev/tty" not in line
    ]
    assert not offenders, (
        "a `read` with no `< /dev/tty` — under `curl | sh` that reads the "
        f"script's own bytes as the answer (§3.3): {offenders}"
    )


def test_the_handoff_reconnects_the_terminal(source: str) -> None:
    """`exec asq < /dev/tty`, both halves.

    `exec` so the UI REPLACES this shell rather than running as its child with a
    pipe still attached; `< /dev/tty` so its stdin is the terminal rather than
    the remains of this script. Measured on 0.6.0: bare `aisquare` with a piped
    stdin prints the usage page and exits 2, so an installer that got this wrong
    would end by printing a help page and reporting failure.
    """
    assert "exec asq </dev/tty" in source or "exec asq < /dev/tty" in source, (
        "the handoff must be `exec asq < /dev/tty` (§3.3)"
    )


def test_the_terminal_test_opens_the_device_rather_than_stat_ing_it(source: str) -> None:
    """`[ -r /dev/tty ]` is not good enough, and this is why it is not used.

    In a container with no controlling terminal the device node exists and its
    mode bits pass `-r`, while `open(2)` fails with ENXIO — so the permissive
    test says "a human is here" on exactly the unattended machines §0.9 is
    about, and the run blocks on a prompt nobody can see. The script opens it
    instead.
    """
    assert ": </dev/tty" in source or ": < /dev/tty" in source, (
        "tty_available must OPEN /dev/tty, not test its mode bits"
    )
    # CODE lines only. The script explains at length why `[ -r /dev/tty ]` is
    # wrong, and a guard that reads that explanation as the violation fails on
    # the very comment that documents it.
    offenders = [
        (number, line.strip()) for number, line in _code_lines(source) if "-r /dev/tty" in line
    ]
    assert not offenders, (
        "`[ -r /dev/tty ]` passes in a container with no controlling terminal — "
        f"open the device instead: {offenders}"
    )


# --- §3.9.1: the upgrade command that actually moves a pinned install -------


def test_it_never_uses_uv_tool_upgrade(lines: list[str]) -> None:
    """`uv tool upgrade` does not move a pinned install — measured (§3.9.1).

    It prints "Nothing to upgrade" and exits 0 on a machine still running the
    old version: a silent no-op behind a success code, which is the exact
    failure the upgrade requirement exists to prevent. The friendlier-looking
    command is the wrong one, so a future edit must not be able to drift back to
    it quietly.
    """
    offenders = [
        (number, line.strip())
        for number, line in _code_lines("\n".join(lines))
        if "uv tool upgrade" in line
    ]
    assert not offenders, (
        "`uv tool upgrade` does not move an install made with an exact pin "
        "(§3.9.1) — upgrade with `uv tool install --force … @latest`: "
        f"{offenders}"
    )


def test_the_upgrade_restates_the_tiktoken_extra(source: str) -> None:
    """--force replaces the tool env, so `--with tiktoken` has to be re-stated.

    Dropping it would leave a machine whose `tiktoken` doctor line went amber
    only after an UPGRADE — the hardest kind of regression to attribute.
    """
    upgrade = re.search(
        r"uv tool install --force[^\n]*(\n\s+[^\n]*)*?\"\$_spec\"", source
    ) or re.search(r"uv tool install --force.*", source)
    assert upgrade, "no `uv tool install --force` invocation found"
    assert "--with tiktoken" in upgrade.group(0), (
        f"the upgrade command must re-state `--with tiktoken`: {upgrade.group(0)!r}"
    )


def test_init_is_never_called_with_reinit(lines: list[str]) -> None:
    """`--reinit` resets config.toml and discards `team bind` role bindings (§3.4).

    It is exactly the flag a script author reaches for to make a step "clean",
    and here it would silently destroy user configuration on every re-run — the
    worst possible property for something people are told to pipe into a shell.
    """
    offenders = [
        (number, line.strip())
        for number, line in _code_lines("\n".join(lines))
        if "--reinit" in line
    ]
    assert not offenders, f"`aisquare init --reinit` destroys user config on a re-run: {offenders}"


def test_it_never_sudo_npm_installs(lines: list[str]) -> None:
    """`sudo npm install -g` is how ~/.npm ends up root-owned (§3.2)."""
    offenders = [
        (number, line.strip())
        for number, line in _code_lines("\n".join(lines))
        if re.search(r"sudo\s+npm|npm\s+install\s+-g", line)
    ]
    assert not offenders, f"never `sudo npm install -g`: {offenders}"


# --- §0.8/§0.9: safe to pipe, safe to run unattended ------------------------


def test_main_is_called_on_the_last_line(lines: list[str]) -> None:
    """A truncated download must define functions and run nothing.

    `sh` interprets a pipe as it arrives, so a connection that drops mid-stream
    executes whatever prefix landed. Keeping every statement inside a function
    and the single call at the very end is the standard mitigation — and the one
    that is undone by anyone appending a line.
    """
    code = [line for _, line in _code_lines("\n".join(lines))]
    assert code[-1].strip() == "fi", (
        f"last statement is {code[-1]!r}, expected the `fi` of the main guard"
    )
    tail = "\n".join(code[-3:])
    assert 'main "$@"' in tail, f'`main "$@"` must be the last thing the file does, got:\n{tail}'


def _scan_quotes(line: str, in_double: bool, in_single: bool) -> tuple[bool, bool]:
    """Carry shell quote state across one line, character by character.

    Counting quote characters per line is not good enough, and two apostrophes
    proved it while this was being written: a COMMENT reading "the script's own
    bytes" flipped the single-quote state and desynchronised every line after
    it, and `die "uv's installer failed"` did the same from inside a
    double-quoted string where that apostrophe is just a letter.

    So the rules are applied rather than approximated. Inside single quotes
    nothing escapes (POSIX); inside double quotes a backslash escapes the next
    character; outside both, a backslash escapes and an unquoted `#` ends the
    line.
    """
    index = 0
    while index < len(line):
        char = line[index]
        if in_single:
            if char == "'":
                in_single = False
        elif in_double:
            if char == "\\":
                index += 1
            elif char == '"':
                in_double = False
        else:
            if char == "\\":
                index += 1
            elif char == "'":
                in_single = True
            elif char == '"':
                in_double = True
            elif char == "#":
                break
        index += 1
    return in_double, in_single


def _top_level_statements(text: str) -> list[tuple[int, str]]:
    """Lines at column 0 that are STATEMENTS, not continuations or data.

    A line-by-line reading is not enough and the first version of this guard
    proved it: `die "…"` messages span several lines, and their continuation
    lines start at column 0 because that is where the message text belongs. So
    does the body of the `usage` heredoc, and so does the inline `python3 -c`
    program. All three are data; none of them runs anything.

    So the file is walked with two pieces of state — whether a quoted string is
    still open (:func:`_scan_quotes`) and whether a heredoc is still open — and
    only lines that begin a statement outside both are returned.
    """
    statements: list[tuple[int, str]] = []
    in_double = False
    in_single = False
    heredoc: str | None = None

    for number, line in enumerate(text.splitlines(), start=1):
        quoted = in_double or in_single
        if not quoted and heredoc is None and line and line[0] not in " \t#":
            statements.append((number, line))

        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue

        opener = None
        if not quoted:
            # A heredoc opener, quoted or not: <<'X', <<"X", <<X, <<-X.
            opener = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)

        in_double, in_single = _scan_quotes(line, in_double, in_single)

        if opener and not in_double and not in_single:
            heredoc = opener.group(1)

    return statements


def test_every_top_level_statement_is_harmless(source: str) -> None:
    """Nothing at column 0 may DO anything but define, assign or branch.

    The truncation property of note 1 in the script's own header, checked
    directly rather than only at the last line: `sh` interprets a pipe as it
    arrives, so a `curl … | sh` whose connection dies after 200 lines executes
    exactly the prefix that landed. If that prefix contains a command, a
    half-downloaded installer has already changed the machine.

    Assignments, function definitions and the colour-detection `if` are fine —
    none of them touches anything outside this shell.
    """
    allowed = re.compile(
        r"^(?:"
        r"[A-Za-z_][A-Za-z0-9_]*="  # an assignment
        r"|[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{?"  # a function definition
        r"|set -eu"
        r"|if |elif |else$|fi$|then$|\}$"
        r")"
    )
    offenders = [
        (number, line) for number, line in _top_level_statements(source) if not allowed.match(line)
    ]
    assert not offenders, (
        "top-level statements run during a TRUNCATED `curl | sh` download; "
        f"move them into a function: {offenders}"
    )


def test_the_truncation_guard_can_actually_fail(source: str) -> None:
    """The census must be able to see a violation, or it proves nothing.

    A parser that skips everything reports "0 offenders" while inspecting
    nothing — the defect this repo has produced in its own censuses before. So a
    known-bad line is injected and the guard must catch it, and the real file's
    statement count is floored so a parser that stopped reading is visible.
    """
    statements = _top_level_statements(source)
    assert len(statements) >= 20, (
        f"only {len(statements)} top-level statements found — the parser is no "
        "longer reading the file"
    )

    poisoned = source + "\nrm -rf /tmp/whatever\n"
    offenders = [line for _, line in _top_level_statements(poisoned) if line.startswith("rm -rf")]
    assert offenders == ["rm -rf /tmp/whatever"], (
        "the parser cannot see a top-level command, so the guard above is vacuous"
    )


@pytest.mark.parametrize("flag", ["--dry-run", "--yes", "--offline", "--help", "--no-agent"])
def test_the_documented_flags_are_parsed(source: str, flag: str) -> None:
    """Each flag §3.5 documents has a branch in `parse_args`.

    Cheap, and it catches the specific rot of a flag documented in `usage` that
    the parser rejects — which would exit 64 and read as the user's mistake.
    """
    parse = source[source.index("parse_args() {") :]
    assert flag in parse, f"{flag} is documented but has no branch in parse_args"


def test_an_unknown_flag_is_refused_rather_than_ignored(source: str) -> None:
    """A typo'd flag must not be silently dropped.

    `--no-agents` quietly ignored is how someone concludes the flag does not
    work, or worse, that it worked.
    """
    parse = source[source.index("parse_args() {") :]
    assert "unknown option" in parse, "parse_args must reject unknown flags"
    assert "exit 64" in parse, "an unknown flag should exit 64 (EX_USAGE)"


# --- portability, as far as text can show it --------------------------------

#: Constructs that work in bash and fail (or silently differ) in dash/ash. Each
#: was chosen because it is a plausible edit, not to be exhaustive — the real
#: proof is the matrix running the script under both shells.
BASHISMS = (
    (r"\[\[", "`[[` is bash-only; use `[`"),
    (r"^\s*function\s+\w+", "`function name` is bash-only; use `name()`"),
    (r"\bpipefail\b", "`set -o pipefail` is not POSIX and dash rejects it"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*,,", "`${x,,}` case conversion is bash-only"),
    (r"\bdeclare\s", "`declare` is bash-only"),
    (r"=~", "`=~` is bash-only; use `case`"),
    (r"\becho\s+-e\b", "`echo -e` is not portable; use `printf`"),
    (r"\bsort\s+-V\b", "BusyBox `sort` has no -V — it would degrade to a lexical compare"),
)


@pytest.mark.parametrize(("pattern", "why"), BASHISMS, ids=[w.split("`")[1] for _, w in BASHISMS])
@pytest.mark.parametrize("path", POSIX_SCRIPTS, ids=lambda p: p.name)
def test_no_bashisms(path: Path, pattern: str, why: str) -> None:
    """Under `curl … | sh` the interpreter is whatever /bin/sh is.

    On Debian and Ubuntu that is dash and on Alpine it is BusyBox ash, so a
    bashism does not degrade — it fails on the majority of Linux machines.
    """
    offenders = [
        (number, line.strip())
        for number, line in _code_lines(path.read_text(encoding="utf-8"))
        if re.search(pattern, line)
    ]
    assert not offenders, f"{path.name}: {why} — {offenders}"


def test_no_unescaped_backticks_in_double_quotes() -> None:
    """A markdown backtick inside "…" is a COMMAND SUBSTITUTION.

    Found the hard way while writing the container cell: a message reading
    "the `install` check is amber" ran a command called `install`, and shellcheck
    said nothing because `$(install)` is perfectly valid — `install` is a real
    binary. Nothing else in the toolchain catches this, so it is checked here.
    """
    offenders = []
    for path in POSIX_SCRIPTS:
        for number, line in _code_lines(path.read_text(encoding="utf-8")):
            # Strip escaped backticks (`\``), which are literal and fine.
            probe = line.replace("\\`", "")
            if '"' not in probe or "`" not in probe:
                continue
            # A backtick anywhere inside a double-quoted run is the hazard.
            for quoted in re.findall(r'"[^"]*"', probe):
                if "`" in quoted:
                    offenders.append((path.name, number, quoted))
    assert not offenders, (
        "backticks inside a double-quoted string are a command substitution, "
        f"not markdown — escape them or use single quotes: {offenders}"
    )


# --- the constants the script duplicates from Python ------------------------


def test_the_node_floor_matches_the_pythons(source: str) -> None:
    """`install.sh`'s Node floor and `core/snapshot.py`'s MIN_NODE must be equal.

    The number genuinely has to exist twice: `install.sh` runs BEFORE any Python
    is on the machine — that is the whole premise of §3.1 — so it cannot import
    the constant. What it must not do is drift from it. If repomix raises its
    floor and only the Python side is updated, the installer would leave a Node
    the `repomix` check then calls too old, and the container matrix would fail
    on the acceptance criterion with nothing pointing at the cause.
    """
    from aisquare.core.snapshot import MIN_NODE

    match = re.search(r"^MIN_NODE_MAJOR=(\d+)$", source, re.MULTILINE)
    assert match, "install.sh no longer declares MIN_NODE_MAJOR"
    assert int(match.group(1)) == MIN_NODE[0], (
        f"install.sh installs Node {match.group(1)}+ while "
        f"core/snapshot.py's MIN_NODE is {MIN_NODE[0]} — the installer would "
        "leave a Node that `doctor` then calls too old"
    )


def test_the_tmux_floor_matches_the_pythons(source: str) -> None:
    """Same rule for tmux, whose floor lives in `core/tmux.py MIN_VERSION`.

    Included because it is the same class of duplication and the same failure —
    an installer that installs below the floor the product enforces — and
    because tmux's floor is the one a distro package can genuinely fail to
    clear, so the script already has a branch that reports it.
    """
    from aisquare.core.tmux import MIN_VERSION

    major = re.search(r"^MIN_TMUX_MAJOR=(\d+)$", source, re.MULTILINE)
    minor = re.search(r"^MIN_TMUX_MINOR=(\d+)$", source, re.MULTILINE)
    assert major and minor, "install.sh no longer declares the tmux floor"
    assert (int(major.group(1)), int(minor.group(1))) == MIN_VERSION, (
        f"install.sh's tmux floor is {major.group(1)}.{minor.group(1)} while "
        f"core/tmux.py's MIN_VERSION is {MIN_VERSION[0]}.{MIN_VERSION[1]}"
    )
