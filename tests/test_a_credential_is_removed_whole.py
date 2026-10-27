"""A credential is removed whole at any length, and the scrub stays linear (task #25; the
tester's follow-ups on #15).

The owner kept the cross-agent hand-off on by default on the promise that credentials are
scrubbed before the conversation goes to another vendor, and ``redact`` also guards what the
insights spool ships. Every rule capped its value (200, 256, 400 characters): a 283-character
JWT left 83 characters of its signature, a 1,000-character bearer token 619, a 300-character
key 176, and Google's and Azure's access tokens run 1,000 to 1,500. And the private-key rule was
quadratic: 7,000 headers and no footer took 16 s in 400 KB, on the hook path.

``no window of the secret survives`` is the test: every run of ten consecutive characters of
the secret is looked for in what ``redact`` returned.
"""

from __future__ import annotations

import random
import string
import sys
import time
from collections.abc import Callable

import pytest

from aisquare.core import redaction
from aisquare.core.redaction import MARKER, PEM_BLOCK, redact
from aisquare.models import RedactionLevel
from tests.budgets import budget

LENGTHS = [50, 200, 400, 1_000, 1_500]
WINDOW = 10


def _run(rng: random.Random, alphabet: str, length: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(length))


ALNUM = string.ascii_letters + string.digits
B64URL = ALNUM + "_-"
UPPER = string.ascii_uppercase + string.digits


def _assignment(rng: random.Random, length: int) -> tuple[str, str]:
    value = _run(rng, ALNUM + "+/_-", length)
    return value, f"export ANTHROPIC_API_KEY={value} # rotate it"


def _bearer(rng: random.Random, length: int) -> tuple[str, str]:
    value = _run(rng, ALNUM + "_.+/=~-", length)
    return value, f"curl -H 'Authorization: Bearer {value}' https://api.example/v1"


def _basic(rng: random.Random, length: int) -> tuple[str, str]:
    value = _run(rng, ALNUM + "+/=", length)
    return value, f"Authorization: Basic {value}"


def _sk(rng: random.Random, length: int) -> tuple[str, str]:
    value = "sk-" + _run(rng, B64URL, length - 3)
    return value, f"my key is {value} and it fails"


def _jwt(rng: random.Random, length: int) -> tuple[str, str]:
    each = (length - 2) // 3
    value = ".".join(
        ["eyJ" + _run(rng, B64URL, each - 3), _run(rng, B64URL, each), _run(rng, B64URL, each)]
    )
    return value, f"this token keeps failing: {value} what now"


def _aws(rng: random.Random, length: int) -> tuple[str, str]:
    value = "AKIA" + _run(rng, UPPER, length - 4)
    return value, f"aws_access_key_id was {value} until Tuesday"


def _github(rng: random.Random, length: int) -> tuple[str, str]:
    value = "ghp_" + _run(rng, ALNUM, length - 4)
    return value, f"my github token is {value} and the push is refused"


def _github_fine_grained(rng: random.Random, length: int) -> tuple[str, str]:
    value = "github_pat_" + _run(rng, ALNUM + "_", length - 11)
    return value, f"GH={value}"


def _slack(rng: random.Random, length: int) -> tuple[str, str]:
    value = "xoxb-" + _run(rng, ALNUM + "-", length - 5)
    return value, f"the bot posts with {value} to #deploys"


def _slack_app(rng: random.Random, length: int) -> tuple[str, str]:
    value = "xapp-" + _run(rng, ALNUM + "-", length - 5)
    return value, f"socket mode: {value}"


def _google(rng: random.Random, length: int) -> tuple[str, str]:
    value = "AIza" + _run(rng, B64URL, length - 4)
    return value, f"key={value}&alt=json"


def _gitlab(rng: random.Random, length: int) -> tuple[str, str]:
    value = "glpat-" + _run(rng, B64URL, length - 6)
    return value, f"gitlab token {value}"


def _url(rng: random.Random, length: int) -> tuple[str, str]:
    value = _run(rng, ALNUM, length)
    return value, f"git clone https://deploy:{value}@internal.example.com/repo.git"


KINDS: dict[str, Callable[[random.Random, int], tuple[str, str]]] = {
    "assignment": _assignment,
    "bearer": _bearer,
    "basic": _basic,
    "sk": _sk,
    "jwt": _jwt,
    "aws": _aws,
    "github": _github,
    "github-fine-grained": _github_fine_grained,
    "slack": _slack,
    "slack-app": _slack_app,
    "google": _google,
    "gitlab": _gitlab,
    "url-password": _url,
}


def surviving(secret: str, shown: str) -> list[str]:
    """Every window of ``secret`` of :data:`WINDOW` characters that ``shown`` still holds."""
    return [
        secret[at : at + WINDOW]
        for at in range(len(secret) - WINDOW + 1)
        if secret[at : at + WINDOW] in shown
    ]


# --- no tail survives at any length ----------------------------------------------------------


@pytest.mark.parametrize("length", LENGTHS)
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_no_window_of_a_credential_survives_at_any_length(kind: str, length: int) -> None:
    secret, text = KINDS[kind](random.Random(f"{kind}{length}"), length)
    assert len(secret) >= min(length, 50) - 1

    shown = redact(text, RedactionLevel.standard)

    assert surviving(secret, shown) == [], (kind, length, shown[:200])
    assert MARKER in shown


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_words_around_a_credential_are_kept(kind: str) -> None:
    secret, text = KINDS[kind](random.Random(kind), 400)
    before = text.split(secret)[0]
    after = text.split(secret)[1]

    shown = redact(text, RedactionLevel.standard)

    assert shown.startswith(before.rstrip()[:12]) and shown.endswith(after.lstrip()[-12:])


@pytest.mark.parametrize("length", [100_000, 1_900_000])
@pytest.mark.parametrize("kind", ["assignment", "bearer", "sk", "jwt", "github", "slack", "aws"])
def test_a_credential_is_removed_whole_up_to_what_a_brief_reads(kind: str, length: int) -> None:
    """``handoff.SCAN_BUDGET`` is 2,000,000 bytes: a token as long as a record can be."""
    secret, text = KINDS[kind](random.Random(f"long{kind}"), length)
    started = time.perf_counter()

    shown = redact(text, RedactionLevel.standard)

    elapsed = time.perf_counter() - started
    assert len(shown) < 300, (kind, len(shown))
    assert surviving(secret[:5_000], shown) == [] and surviving(secret[-5_000:], shown) == []
    assert elapsed < budget(10.0), f"{kind} {length}: {elapsed:.2f} s"


def test_a_value_shorter_than_its_floor_is_left_alone() -> None:
    """A floor is a shape, and prose is not shaped like it: the guard against creep."""
    for text in ("sk-short", "ghp_abc", "AKIA123", "xoxb-1", "bearer x", "token=abc", "AIzaSy"):
        assert redact(text, RedactionLevel.standard) == text, text


def test_an_assignment_whose_value_is_quoted_is_removed_to_its_closing_quote() -> None:
    for quote in ('"', "'"):
        value = "p" * 700 + " with spaces " + "q" * 700
        text = f"password = {quote}{value}{quote} # then more"

        shown = redact(text, RedactionLevel.standard)

        assert shown == f"password = {MARKER} # then more", shown


# --- a private-key block ----------------------------------------------------------------------

HEADER = "-----BEGIN RSA PRIVATE KEY-----"
FOOTER = "-----END RSA PRIVATE KEY-----"


def test_a_header_with_no_footer_costs_no_more_than_reading_the_text() -> None:
    """The tester's case: 7,000 headers and no footer in 2 MB took minutes (16 s in 400 KB)."""
    pad = "x" * 250
    text = "".join(f"{HEADER}\n{pad}\n" for _ in range(7_000))
    assert len(text) > 1_900_000
    started = time.perf_counter()

    shown = redact(text, RedactionLevel.standard)

    elapsed = time.perf_counter() - started
    assert elapsed < budget(5.0), f"{elapsed:.2f} s"
    assert shown.count(HEADER) == 7_000, "a header with no footer is not a block"


def test_headers_after_a_block_whose_footer_is_the_last_cost_no_more_either() -> None:
    pad = "y" * 250
    text = HEADER + "\n" + FOOTER + "\n" + "".join(f"{HEADER}\n{pad}\n" for _ in range(7_000))
    started = time.perf_counter()

    shown = redact(text, RedactionLevel.standard)

    assert time.perf_counter() - started < budget(5.0)
    assert shown.startswith(MARKER + "\n") and shown.count(HEADER) == 7_000


def test_many_headers_and_one_footer_are_one_block() -> None:
    text = "before\n" + "".join(f"{HEADER}\nbody {n}\n" for n in range(7_000)) + FOOTER + "\nafter"
    started = time.perf_counter()

    shown = redact(text, RedactionLevel.standard)

    assert shown == f"before\n{MARKER}\nafter"
    assert time.perf_counter() - started < budget(5.0)


def test_a_block_is_one_marker_whatever_it_holds_inside() -> None:
    text = f"a {HEADER}\nMIIEow\n{HEADER}\nQQ\n{FOOTER} b {FOOTER} c"

    assert redact(text, RedactionLevel.standard) == f"a {MARKER} b {FOOTER} c"


@pytest.mark.parametrize("seed", range(300))
def test_the_linear_pass_removes_what_the_regular_expression_did(seed: int) -> None:
    """The rule was ``PEM_BLOCK.sub``; its replacement must give the same text for any text
    made of headers, footers, other lines and unrelated dashes."""
    rng = random.Random(seed)
    pieces = [
        HEADER,
        FOOTER,
        "-----BEGIN PRIVATE KEY-----",
        "-----END PRIVATE KEY-----",
        "-----BEGIN CERTIFICATE-----",
        "-----END CERTIFICATE-----",
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        "-----END OPENSSH PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "----- BEGIN PRIVATE KEY-----",
        "body line",
        "\n",
        " ",
        "x" * rng.randrange(1, 40),
    ]
    text = "".join(
        rng.choice(pieces) + rng.choice(["", "\n", " "]) for _ in range(rng.randrange(0, 40))
    )

    assert redaction._without_pem_blocks(text) == PEM_BLOCK.sub(MARKER, text), text


def test_text_with_no_header_or_no_footer_is_returned_as_it_is() -> None:
    for text in ("", "nothing here", HEADER, f"x {FOOTER}", f"{FOOTER} then {HEADER}"):
        assert (
            redaction._without_pem_blocks(text) is text
            or redaction._without_pem_blocks(text) == text
        )


# --- every other rule is linear on hostile input ------------------------------------------------

#: A text of about ``size`` characters, built to make some backtracking engine's case.
HOSTILE: dict[str, Callable[[int], str]] = {
    "jwt-starts-inside-a-run": lambda size: "eyJ-" * (size // 4),
    "jwt-starts-no-dots": lambda size: ("eyJ" + "a" * 20 + " ") * (size // 24),
    "jwt-one-dot": lambda size: "eyJ" + "a" * 20 + "." + "b" * (size - 24),
    "assignment-unclosed-quote": lambda size: 'token="' * (size // 7),
    "assignment-unclosed-single": lambda size: "secret='" * (size // 8),
    "assignment-long-prefix": lambda size: ("a" * 48 + "token ") * (size // 54),
    "assignment-equals": lambda size: "api_key=" * (size // 8),
    "bearer-words": lambda size: "bearer " * (size // 7),
    "basic-words": lambda size: "basic ab " * (size // 9),
    "authorization": lambda size: "authorization:" * (size // 14),
    "url-colons": lambda size: "://a:b" * (size // 6),
    "url-userinfo": lambda size: "://" + "u" * (size // 2) + ":" + "p" * (size // 2),
    "prefixes": lambda size: "sk-ghp_xoxb-AKIAglpat-aisq_AIzawk_live_" * (size // 40),
    "dashes": lambda size: "-" * size,
    "alnum": lambda size: "a" * size,
    "headers-only": lambda size: (HEADER + "\n") * (size // (len(HEADER) + 1)),
}

#: Four sizes, each four times the one before, the last 2 MB.
SIZES = (31_250, 125_000, 500_000, 2_000_000)
#: What a size four times as long may cost, as a multiple of the one before: linear is 4, a
#: quadratic rule 16. Not 4 itself, because the cost per character differs a little by size
#: and a shared runner adds noise; not 16, which would pass a rule that is nearly quadratic.
GROWTH = 6.0
#: Seconds added to that bar. A run of 30 ms is mostly what is around it: CI's py3.13 leg took
#: 0.03 s for 125 KB of ``jwt-one-dot`` and 0.18 s for 500 KB, 6.3 times where linear is 4
#: (run 40428675826). A tenth of a second is nothing beside what a quadratic rule costs from
#: the next size on: the two this must fail took 1.3 s at 125 KB here.
SLACK = 0.1
#: A run shorter than this says too little about the next one: the ratio is not asked of it.
FLOOR = 0.1 if sys.platform == "win32" else 0.02
#: The process's CPU time, not the wall clock: on a shared runner (several pytest workers on two
#: cores) a long run is stalled by its neighbours more than a short one, which is a ratio that
#: is not the rule's. Windows' CPU clock ticks every 15.6 ms, too coarse for the short runs, so
#: it keeps the wall clock.
_clock = time.perf_counter if sys.platform == "win32" else time.process_time
#: Seconds. Not a bound on speed (a loaded runner takes four times what a laptop does for the
#: same text, which is what a tight absolute budget failed on), only on a rule that has stopped
#: being a scrub: 30 microseconds a character.
CEILING = budget(60.0)


def _best_of(text: str, bar: float | None) -> float:
    """The least of several runs of ``redact`` over ``text``: five while they are short (a short
    run is mostly noise), three up to half a second, one beyond that, where a repeat would only
    cost. Three at least while the least is over ``bar``: a stall on a shared runner looks like a
    regression once, and a regression looks like one every time."""
    best = float("inf")
    runs = 0
    while True:
        started = _clock()
        redact(text, RedactionLevel.strict)
        best = min(best, _clock() - started)
        runs += 1
        if best > 40.0:
            return best
        wanted = 5 if best <= 0.1 else 3 if best <= 0.5 else 1
        if bar is not None and best > bar:
            wanted = max(wanted, 3)
        if runs >= wanted:
            return best


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_redaction_is_linear_on_hostile_input(name: str) -> None:
    """Each of these made some backtracking engine's cubic or quadratic case, and a regression
    is a hang on the hook path. Asked as growth, not as speed: four times the text costs under
    ``GROWTH`` times the time, at every size from 31 KB to 2 MB, so a runner that is slow all
    over still passes and a rule that is quadratic fails at the first size that can show it
    (the next size is not run), where 16 times the time is 16 and not 4. A text that costs
    under ``FLOOR`` at every size (``assignment-equals``) has only the ceiling.

    Quadratic rules this test fails: the private-key pattern ``BEGIN .*? END`` (headers-only)
    and a JWT that starts at every ``eyJ`` after ``\\b`` (jwt-starts-inside-a-run)."""
    previous: float | None = None
    for size in SIZES:
        text = HOSTILE[name](size)
        bar = previous * GROWTH + SLACK if previous is not None and previous >= FLOOR else None

        took = _best_of(text, bar)

        assert took < CEILING, f"{name}: {took:.2f} s for {len(text):,} characters"
        if bar is not None and previous is not None:
            assert took <= bar, (
                f"{name}: {len(text):,} characters took {took:.2f} s, {took / previous:.1f} "
                f"times the {previous:.2f} s of the {size // 4:,} before (linear is 4, "
                f"quadratic 16; the bar is {GROWTH:g} times and {SLACK:g} s)"
            )
        previous = took
