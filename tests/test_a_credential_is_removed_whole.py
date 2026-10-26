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

HOSTILE: dict[str, Callable[[], str]] = {
    "jwt-starts-inside-a-run": lambda: "eyJ-" * 500_000,
    "jwt-starts-no-dots": lambda: ("eyJ" + "a" * 20 + " ") * 80_000,
    "jwt-one-dot": lambda: "eyJ" + "a" * 20 + "." + "b" * 1_900_000,
    "assignment-unclosed-quote": lambda: 'token="' * 280_000,
    "assignment-unclosed-single": lambda: "secret='" * 250_000,
    "assignment-long-prefix": lambda: ("a" * 48 + "token ") * 150_000,
    "assignment-equals": lambda: "api_key=" * 250_000,
    "bearer-words": lambda: "bearer " * 280_000,
    "basic-words": lambda: "basic ab " * 220_000,
    "authorization": lambda: "authorization:" * 140_000,
    "url-colons": lambda: "://a:b" * 300_000,
    "url-userinfo": lambda: "://" + "u" * 1_000_000 + ":" + "p" * 900_000,
    "prefixes": lambda: "sk-ghp_xoxb-AKIAglpat-aisq_AIzawk_live_" * 50_000,
    "dashes": lambda: "-" * 2_000_000,
    "alnum": lambda: "a" * 2_000_000,
    "headers-only": lambda: (HEADER + "\n") * 60_000,
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_redaction_is_linear_on_hostile_input(name: str) -> None:
    """Seconds, not minutes: each of these made some backtracking engine's cubic or
    quadratic case; a regression is a hang on the hook path."""
    text = HOSTILE[name]()
    started = time.perf_counter()

    redact(text, RedactionLevel.strict)

    elapsed = time.perf_counter() - started
    assert elapsed < budget(8.0), f"{name}: {elapsed:.2f} s for {len(text):,} characters"
