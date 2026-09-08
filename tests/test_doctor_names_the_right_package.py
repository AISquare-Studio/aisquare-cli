"""Every package `doctor` tells you to install must be one of OURS.

Three fix strings shipped in 0.6.0 naming the wrong distribution. Found by
measuring the new-user path for docs/plans/one-line-install.md §6, not by
reading the code:

    install    "Install as a global tool: pipx install aisquare"
    install    "install globally: pipx install aisquare"
    tiktoken   "pip install tiktoken (or: pipx inject aisquare tiktoken)"

`aisquare` on PyPI is a DIFFERENT PROJECT — the Explainability SDK, 1.2.0,
"Explainability SDK for tracing, graphing, and policy auditing of AI agents".
This CLI is `aisquare-cli`. So `doctor`, the command a confused user runs, told
them to install someone else's package.

WHY THAT IS WORSE THAN A TYPO, and the reason this guard exists rather than a
one-line fix: the SDK ships its own `aisquare/__init__.py` into the same
top-level package DIRECTORY this distribution occupies. pip's RECORD for the two
overlaps on exactly that file and the last writer wins it, silently. That shape
is not hypothetical to this repo — `pyproject.toml` carries a twelve-line
comment about it on the `explainability` extra, and `tests/test_sdk_coexistence.py`
exists because a build that read a name out of the top-level `__init__` already
died of it once. The bad fix string therefore did not merely fail to install
this CLI; it walked the reader into the one dependency shape this project
documents at length as a hazard.

`pipx inject aisquare tiktoken` is the same error wearing different clothes:
`inject` takes the name of an installed pipx ENVIRONMENT, and the documented
install makes that `aisquare-cli`. It fails on every machine that followed the
docs.

TWO MECHANISMS, DELIBERATELY. The sweep below reads the fix strings off a LIVE
`doctor()` run, so it covers all seventeen checks including any added later —
that is the regression guard, and it is what makes the class fixed rather than
the three instances. The two exact-wording tests are narrower on purpose: they
pin the fix strings a reader of §6 will come looking for, and they would still
fail if the sweep's parser were ever loosened into uselessness. A census that can
be narrowed to nothing reports "0 unaccounted" while inspecting nothing, which is
a defect this repo has already produced elsewhere — so the sweep asserts a floor
on how many install commands it found.
"""

from __future__ import annotations

import re

import pytest

from aisquare.services import diagnostics
from aisquare.services.diagnostics import doctor

#: The install verbs a fix string can plausibly use. `pipx inject` is here
#: because its first positional is an environment name, which is the same
#: mistake in the same place.
_VERB = re.compile(
    r"(?:pipx\s+install|pipx\s+inject|uv\s+tool\s+install|"
    r"(?:python3?\s+-m\s+)?pip\s+install)",
)

#: Where an install command ENDS inside an English sentence. Without this the
#: scan runs on into the prose and "pipx install aisquare-cli, then run aisquare
#: doctor" reads as naming the SDK — a false positive that would push the next
#: person to "fix" a correct string.
_CLAUSE_END = re.compile(r"[(),;.]|\s—\s|\bthen\b")

#: The distribution this project must never tell anyone to install. Compared
#: against whole tokens: `aisquare-cli` and `aisquare-cli[dev]` are fine,
#: `/path/to/aisquare-cli` is a path, and a bare `aisquare` is the bug.
_SDK = "aisquare"


def _install_arguments(fix: str) -> list[str]:
    """Every non-flag token that an install command in `fix` would pass along.

    Every token rather than "the first positional", because a flag's VALUE is
    positional-shaped: in `uv tool install --with tiktoken aisquare-cli` the
    first non-flag token is `tiktoken`. Checking all of them needs no table of
    which flags take values, and over-checking is safe here — the assertion is
    only ever "none of these is the string `aisquare`".
    """
    arguments: list[str] = []
    for verb in _VERB.finditer(fix):
        tail = fix[verb.end() :]
        stop = _CLAUSE_END.search(tail)
        for token in tail[: stop.start() if stop else len(tail)].split():
            token = token.strip("'\"`")
            if token.startswith("-"):
                continue
            # Strip an extra and a version specifier, so `aisquare[extra]` and
            # `aisquare==1.2.0` are both caught rather than slipping past as
            # tokens that merely start with the name.
            token = re.split(r"[\[=<>~!@]", token, maxsplit=1)[0]
            arguments.append(token)
    return arguments


def _fixes() -> list[tuple[str, str]]:
    """`(check name, fix)` for every check in a live run that carries a fix."""
    return [(check.name, check.fix) for check in doctor() if check.fix]


def test_no_doctor_fix_tells_you_to_install_the_sdk() -> None:
    """The class, over every check `doctor` runs — not the three known instances."""
    offenders = [
        (name, fix)
        for name, fix in _fixes()
        for argument in _install_arguments(fix)
        if argument == _SDK
    ]
    assert not offenders, (
        "a doctor fix names the Explainability SDK (`aisquare`) where it means "
        f"this CLI (`aisquare-cli`): {offenders}. Following it installs a "
        "different project into the shared-__init__ shape pyproject.toml warns "
        "about at length."
    )


def test_the_sweep_actually_inspects_something() -> None:
    """A census that inspects nothing passes. So the floor is asserted.

    Two install commands is the floor because two checks own one each: `install`
    and `tiktoken`. If a refactor moves the wording somewhere this parser cannot
    see, this fails rather than going quietly green.
    """
    found = [(name, arg) for name, fix in _fixes() for arg in _install_arguments(fix)]
    assert len(found) >= 2, (
        f"the parser found only {found} install arguments across doctor's fixes — "
        "it is no longer reading the strings it is supposed to guard"
    )
    assert any(arg == "aisquare-cli" for _, arg in found), (
        f"no doctor fix names this distribution at all: {found}"
    )


@pytest.mark.parametrize(
    "fix",
    [
        diagnostics._INSTALL_GLOBALLY,
        diagnostics._check_tiktoken().fix or "",
    ],
    ids=["install", "tiktoken"],
)
def test_the_two_repaired_fixes_name_the_distribution_and_the_uv_form(fix: str) -> None:
    """Exact wording, for the two strings §6.1 and §6.2 point at.

    `uv tool install` is asserted and not merely allowed: it is what install.sh
    runs, so it is the form that matches the machine the reader is most likely
    to be on, and for `tiktoken` it is one command where pipx needs two.
    """
    assert "aisquare-cli" in fix, fix
    assert "uv tool install" in fix, fix


def test_the_bad_wording_is_gone_from_the_source() -> None:
    """A grep, because the sweep only sees strings a check RETURNS.

    `_check_install`'s virtualenv branch needs a machine running from a venv to
    be reached, and the suite's own environment is one — but a fix string is a
    literal in a file whatever the branch conditions are, so the file is the
    honest place to assert the wording is gone.
    """
    source = diagnostics.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for bad in ("pipx install aisquare\n", "pipx install aisquare ", "pipx inject aisquare "):
        assert bad not in text, (
            f"{source} still contains {bad!r} — that names the Explainability "
            "SDK, not this CLI (docs/plans/one-line-install.md §6.1, §6.2)"
        )
