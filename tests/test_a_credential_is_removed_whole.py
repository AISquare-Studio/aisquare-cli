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
    started = _clock()

    shown = redact(text, RedactionLevel.standard)

    elapsed = _clock() - started
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
    """The tester's case: 7,000 headers and no footer in 2 MB took minutes (16 s in 400 KB).
    A header with no footer is a key cut short, and is removed with the body after it (r24 3a;
    since R9k, the body alone and not the rest of the text)."""
    pad = "x" * 250
    text = "".join(f"{HEADER}\n{pad}\n" for _ in range(7_000))
    assert len(text) > 1_900_000
    started = _clock()

    shown = redact(text, RedactionLevel.standard)

    elapsed = _clock() - started
    assert elapsed < budget(5.0), f"{elapsed:.2f} s"
    assert shown == f"{MARKER}\n" * 7_000, "each header takes its own body, and no more"


def test_headers_after_a_block_whose_footer_is_the_last_cost_no_more_either() -> None:
    pad = "y" * 250
    text = HEADER + "\n" + FOOTER + "\n" + "".join(f"{HEADER}\n{pad}\n" for _ in range(7_000))
    started = _clock()

    shown = redact(text, RedactionLevel.standard)

    assert _clock() - started < budget(5.0)
    assert shown == f"{MARKER}\n" + f"{MARKER}\n" * 7_000, "the block, then each header alone"


def test_many_headers_and_one_footer_are_one_block() -> None:
    text = "before\n" + "".join(f"{HEADER}\nbody {n}\n" for n in range(7_000)) + FOOTER + "\nafter"
    started = _clock()

    shown = redact(text, RedactionLevel.standard)

    assert shown == f"before\n{MARKER}\nafter"
    assert _clock() - started < budget(5.0)


def test_a_block_is_one_marker_whatever_it_holds_inside() -> None:
    text = f"a {HEADER}\nMIIEow\n{HEADER}\nQQ\n{FOOTER} b {FOOTER} c"

    assert redact(text, RedactionLevel.standard) == f"a {MARKER} b {FOOTER} c"


def _as_the_regular_expression_would(text: str) -> str:
    """``PEM_BLOCK.sub`` over ``text``, and then each header it left (one with no footer after
    it) that has a key's body after it, with the body (``_PEM_HEADER_ALONE``): the rule as it
    was, with a block cut short taken to the end of its body, and a header alone kept.

    For a header with nothing, or only a gap, before it on its line. Where text the redactor
    cannot read as a gutter stands there and the next line starts with it too, that line goes
    with the header (reopen 2, P4), which one expression cannot say: none of the seeds below
    makes one (5 in 5,000 do), and ``test_redaction_keeps_ordinary_text.py`` pins it."""
    return redaction._PEM_HEADER_ALONE.sub(MARKER, PEM_BLOCK.sub(MARKER, text))


@pytest.mark.parametrize("seed", range(300))
def test_the_linear_pass_removes_what_the_regular_expression_did(seed: int) -> None:
    """The rule was ``PEM_BLOCK.sub``; its replacement must give the same text for any text
    made of headers, footers, other lines and unrelated dashes, and take a header that has no
    footer with its body."""
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
        "\\n",
        "> ",
        "x" * rng.randrange(1, 40),
        "MIIEowIBAAKCAQEAx7Fk9sQmPq3vN2wZ"[: rng.randrange(1, 33)],
    ]
    text = "".join(
        rng.choice(pieces) + rng.choice(["", "\n", " "]) for _ in range(rng.randrange(0, 40))
    )

    assert redaction._without_pem_blocks(text) == _as_the_regular_expression_would(text), text


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (HEADER, HEADER),  # no body after it: nothing secret, kept as 0.9.2 kept it (H1)
        (f"{HEADER}\n", f"{HEADER}\n"),
        (f"{HEADER}\nMIIEowIBAAKCAQEA", MARKER),
        (f"head -3 id_rsa\n{HEADER}\nMIIEowIBAAKCAQEA\nQ29kZSBpcw==", f"head -3 id_rsa\n{MARKER}"),
        (f"{FOOTER} then {HEADER}\nMIIE", f"{FOOTER} then {MARKER}"),
        (
            f"{HEADER}\na\n{FOOTER}\nkeep {HEADER}\nb\nand this",
            f"{MARKER}\nkeep {MARKER}\nand this",
        ),
        (f"{HEADER}\n{HEADER}\nMIIE", f"{HEADER}\n{MARKER}"),
        ("-----BEGIN PRIVATE KEY-----\nMIIE", MARKER),
        ("-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIE", MARKER),
    ],
)
def test_a_header_with_no_footer_is_removed_with_its_body(text: str, shown: str) -> None:
    """``head -3 id_rsa``, a paste or a tool result clipped, a prompt cut at the contract's
    bound: the header and the first lines of the key. Removing the header alone ships the body
    (r24 3a); removing the rest of the text took the prose after it (R9k)."""
    assert redaction._without_pem_blocks(text) == shown
    assert redact(text, RedactionLevel.standard) == shown
    assert redact(text, RedactionLevel.strict) == shown


def test_text_with_no_header_is_returned_as_it_is() -> None:
    for text in ("", "nothing here", f"x {FOOTER}", "-----BEGIN CERTIFICATE-----\nMIIE"):
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
    "jwt-after-hyphens": lambda size: ("a-" * 24 + "eyJ" + "b" * 10 + " ") * (size // 62),
    "jwt-hyphen-pieces": lambda size: "ab-" * (size // 3),
    "jwt-hyphen-pieces-then-a-start": lambda size: "ab-" * (size // 3 - 1) + "eyJ" + "a" * 20,
    "jwt-chain-of-segments": lambda size: ("eyJaaaaaaaa." * (size // 12))[:-1],
    "assignment-unclosed-quote": lambda size: 'token="' * (size // 7),
    "assignment-unclosed-single": lambda size: "secret='" * (size // 8),
    "assignment-long-prefix": lambda size: ("a" * 48 + "token ") * (size // 54),
    "assignment-quoted-names": lambda size: '"token":' * (size // 8),
    "assignment-quoted-names-unclosed": lambda size: '"token": "' * (size // 10),
    "assignment-quoted-name-long-prefix": lambda size: ("a" * 48 + 'token" ') * (size // 55),
    "assignment-name-then-backslashes": lambda size: "token" + "\\" * (size - 5),
    "assignment-escaped-quotes": lambda size: 'token=\\"' * (size // 8),
    "assignment-escaped-quotes-unclosed": lambda size: 'token="' + '\\"' * (size // 2),
    "assignment-equals": lambda size: "api_key=" * (size // 8),
    "bearer-words": lambda size: "bearer " * (size // 7),
    "basic-words": lambda size: "basic ab " * (size // 9),
    "authorization": lambda size: "authorization:" * (size // 14),
    "url-colons": lambda size: "://a:b" * (size // 6),
    "url-userinfo": lambda size: "://" + "u" * (size // 2) + ":" + "p" * (size // 2),
    "prefixes": lambda size: "sk-ghp_xoxb-AKIAglpat-aisq_AIzawk_live_" * (size // 40),
    "non-ascii-before-prefixes": lambda size: "日sk-ghp_AKIAxoxb-" * (size // 14),
    "assignment-escaped-quotes-closing-late": lambda size: 'token="' + 'a\\"' * (size // 4) + '"',
    "assignment-escaped-delimiters-with-inner-quotes": lambda size: (
        'token=\\"' + 'a\\\\\\"' * (size // 6)
    ),
    "assignment-values-ending-in-a-backslash": lambda size: 'password="abcd\\" ' * (size // 15),
    "assignment-json-values-with-escaped-quotes": lambda size: (
        '{"password": "ab\\"cd1234", ' * (size // 28)
    ),
    "dashes": lambda size: "-" * size,
    "dots": lambda size: "." * size,
    "dashes-and-dots": lambda size: "-." * (size // 2),
    "alnum": lambda size: "a" * size,
    "headers-only": lambda size: (HEADER + "\n") * (size // (len(HEADER) + 1)),
    # R9k: a header with no footer takes its key's body, run by run
    "pem-lone-header-short-lines": lambda size: HEADER + "\nabcdefgh" * (size // 9),
    "pem-lone-header-escaped-short-lines": lambda size: HEADER + "\\nabcdefgh" * (size // 10),
    "pem-lone-header-spaced-short-runs": lambda size: HEADER + " abcdefgh" * (size // 9),
    "pem-lone-headers-with-bodies": lambda size: (HEADER + "\n" + "A" * 64 + "\n") * (size // 97),
    "pem-lone-header-gaps": lambda size: HEADER + "\n" + "\"' +,>" * (size // 6) + "abc",
    # R9k: a quoted name is a credential's by the word that ends it
    "quoted-name-suffix-runs": lambda size: ('"api_key' + "_prod" * 8 + '" ') * (size // 50),
    "quoted-names-joined-unclosed": lambda size: '"accesstoken": "' * (size // 16),
    "quoted-names-camel": lambda size: "aToken" * (size // 6),
    "quoted-name-version-values": lambda size: '"auth": "^1.' * (size // 12),
    # reopen 2: the broad name, the exact list, the placeholders, and a key's lines in a listing
    "quoted-names-near-the-list": lambda size: '"max_tokensX": "' * (size // 16),
    "quoted-names-long-prefix": lambda size: (
        ('"' + "a." * 30 + 'token": "ab' + "\n") * (size // 76)
    ),
    "quoted-placeholders-unclosed": lambda size: '"api_key": "${{<[' * (size // 17),
    "pem-numbered-lines": lambda size: f"     1\t{HEADER}" + "\n     2\tabcdefgh" * (size // 16),
    "pem-numbered-headers": lambda size: f"     1\t{HEADER}\n" * (size // 39),
    "pem-filed-lines": lambda size: f"id_rsa:1:{HEADER}" + "\nid_rsa:2:abcdefgh" * (size // 18),
    "pem-filed-long-names": lambda size: ("a" * 250 + ":1:" + HEADER + "\n") * (size // 285),
    "pem-prefixed-run": lambda size: f"# {HEADER}" + "\n# abcdefgh" * (size // 11),
    "pem-prefixed-headers-on-one-line": lambda size: "# " + (HEADER + " ") * (size // 32),
    "pem-prefixed-headers-alone": lambda size: ("x" * 200 + HEADER + "\ny\n") * (size // 234),
    # the ruling after reopen 2: a quoted value the clip cut is taken to the clip's ellipsis
    "quoted-values-unclosed-under-many-names": lambda size: (
        ("a." * 20 + 'token="' + "x" * 40 + " ") * (size // 88)
    ),
    "quoted-values-escaped-quotes-then-a-clip": lambda size: 'token=\\"' * (size // 8) + "\u2026",
    "quoted-values-unclosed-then-a-clip": lambda size: '"password": "ab ' * (size // 16) + "\u2026",
    # short values between escaped quotes close no quoted form, and a run of a cut value that
    # went through escaped quotes read from every name to the end of the text
    "quoted-values-short-between-escaped-quotes": lambda size: 'token=\\"ab\\"' * (size // 12),
}

#: Four sizes, each four times the one before, the last 2 MB.
SIZES = (31_250, 125_000, 500_000, 2_000_000)
#: What a size four times as long may cost, as a multiple of the one before: linear is 4, a
#: quadratic rule 16. Not 4 itself, because the cost per character differs a little by size
#: and a shared runner adds noise; not 16, which would pass a rule that is nearly quadratic.
#: 8 is the middle of the two on a log scale (4 x 2 = 16 / 2), and the runner has shown the
#: noise: 6.3 times on ``jwt-one-dot`` (py3.13, run 40428675826), and 6.1 on
#: ``jwt-hyphen-pieces-then-a-start`` (py3.12 captain off, run 40458712660: 2.2 s for 500 KB, 13.3
#: for 2 MB, the least of three runs; 3.3 s and exactly 4.0 times here). The two rules this must
#: fail run at 12 times and more (below).
GROWTH = 8.0
#: Seconds added to that bar. A run of 30 ms is mostly what is around it: CI's py3.13 leg took
#: 0.03 s for 125 KB of ``jwt-one-dot`` and 0.18 s for 500 KB, 6.3 times where linear is 4
#: (run 40428675826). A tenth of a second is nothing beside what a quadratic rule costs from
#: the next size on: the two this must fail took 1.3 s at 125 KB here.
SLACK = 0.1
#: A run shorter than this says too little about the next one: the ratio is not asked of it.
#: Windows' CPU clock ticks every 15.6 ms, so a reading there is two ticks for 31 KB (0.031 s,
#: 1.0 microseconds a character, where 125 KB read 0.172 s and 1.4: run 42212286157), and is
#: asked from a tenth of a second.
FLOOR = 0.1 if sys.platform == "win32" else 0.02
#: This thread's CPU time, on every platform, not the wall clock: a run that is not on a core
#: (pytest's other workers, a child they started, the OS) is not the rule's cost. Not the
#: process's either, which counts a thread another test left running in this worker. CPU time
#: alone is not enough on CI's 4-vCPU runners, where the other workers share the cores' caches
#: and pipelines: under ``pytest -n auto`` 2 MB of one text took 2.5 to 4.9 times the CPU of
#: 500 KB, where alone it takes 4.0 (run 42212286157). That is what :func:`_step` is for.
_clock = time.thread_time
#: How many times a step is read again, both of its sizes, while it is over the bar.
PAIRS = 3
#: Seconds. Not a bound on speed (a loaded runner takes four times what a laptop does for the
#: same text, which is what a tight absolute budget failed on), only on a rule that has stopped
#: being a scrub: 30 microseconds a character.
CEILING = budget(60.0)


def _least(text: str) -> float:
    """The least of several runs of ``redact`` over ``text``: five while they are short (a short
    run is mostly noise), three up to half a second, one beyond that, where a repeat would only
    cost."""
    best = float("inf")
    runs = 0
    while True:
        started = _clock()
        redact(text, RedactionLevel.strict)
        best = min(best, _clock() - started)
        runs += 1
        if best > 40.0 or runs >= (5 if best <= 0.1 else 3 if best <= 0.5 else 1):
            return best


def _step(before: str, after: str, read: float) -> tuple[float, float]:
    """The cost of ``before`` (``read`` just now) and of ``after``, four times as long, read as a
    PAIR: one right after the other, so a load on the runner that comes or goes is on both or on
    neither. While ``after`` is over the bar, both are read again, up to :data:`PAIRS` times, and
    the pair that passes (or the last) is returned.

    Not the least of each over every attempt: CI's Windows shard read 500 KB at 1.95 s and then
    2 MB at 19.3 s three times over, by the wall clock, while ``test_delivery_bulk``'s storm of
    writers ran beside it for 169 s (9.9 times; run 42205940486, attempt 1). That runner's image
    takes 0.69 and 2.73 s for them alone (3.98 times; run 42212286157). The load had come in
    between, and stayed, so 2 MB read again could not pass against the 500 KB read before it.
    Read again as a pair, both are slowed alike. A rule that is not linear is over the bar in
    every pair, a stall in one."""
    small = read
    large = _least(after)
    for _ in range(PAIRS - 1):
        if large <= small * GROWTH + SLACK or large >= CEILING:
            break
        small = _least(before)
        large = _least(after)
    return small, large


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_redaction_is_linear_on_hostile_input(name: str) -> None:
    """Each of these made some backtracking engine's cubic or quadratic case, and a regression
    is a hang on the hook path. Asked as growth, not as speed: four times the text costs under
    ``GROWTH`` times the CPU time, at every size from 31 KB to 2 MB, so a runner that is slow
    all over still passes and a rule that is quadratic fails at the first size that can show it
    (the next size is not run), where 16 times the time is 16 and not 4. Each size is read
    against the one before as a pair (:func:`_step`), so a runner whose load changes between
    the two is not read as a rule that grows. A text that costs under ``FLOOR`` at every size
    (``assignment-equals``) has only the ceiling.

    Quadratic rules this test fails: the private-key pattern ``BEGIN .*? END`` (headers-only)
    and a JWT that starts at every ``eyJ`` after ``\\b`` (jwt-starts-inside-a-run)."""
    before: str | None = None
    previous = 0.0
    for size in SIZES:
        text = HOSTILE[name](size)
        asked = before is not None and previous >= FLOOR
        if before is not None and asked:
            previous, took = _step(before, text, previous)
        else:
            took = _least(text)

        assert took < CEILING, f"{name}: {took:.2f} s for {len(text):,} characters"
        if asked:
            assert took <= previous * GROWTH + SLACK, (
                f"{name}: {len(text):,} characters took {took:.2f} s of CPU, "
                f"{took / previous:.1f} times the {previous:.2f} s of the {size // 4:,} read "
                f"before it (linear is 4, quadratic 16; the bar is {GROWTH:g} times and "
                f"{SLACK:g} s, and each of {PAIRS} pairs was over it)"
            )
        before, previous = text, took
