r"""What r25 of #113 left in ``redact`` (task #35, D): a quoted value that holds an escaped quote,
and the cost of a long run of ``-`` or ``.``.

A value is taken to the quote that closes it, and the first quote is not always that one:
``{"password": "ab\"cd1234"}`` is one value, and a pattern that stopped at the quote after ``ab\``
shipped ``cd1234"}`` (``"sk\"tail…"``, with fewer than four characters before the quote, all of
it). In JSON, in a shell, in Python, and in the JSON a tool call's arguments hold inside a string,
where the quotes are escaped once and a quote of the value is escaped again.

And a secret's name may start at a ``.`` or a ``-`` of a long run of them, which cost each of its
characters the whole look ahead for a keyword: 65 times what a run of letters costs.
"""

from __future__ import annotations

import random
import sys
import time

import pytest

from aisquare.core.redaction import MARKER, redact
from aisquare.models import RedactionLevel


def _shown(text: str) -> str:
    return redact(text, RedactionLevel.standard)


# --- a value that holds an escaped quote is removed whole ----------------------------------------

#: (the text, what ``redact`` leaves of it; ``<R>`` is the marker)
HELD_QUOTES: list[tuple[str, str]] = [
    # JSON, the name quoted.
    (r'{"password": "abcd\"efgh1234"}', '{"password": <R>}'),
    (r'{"api_key":"sk\"tail-of-the-key-xyz","user":"bob"}', '{"api_key":<R>,"user":"bob"}'),
    (r'{"token": "ab\"cd"}  tail', '{"token": <R>}  tail'),
    # An assignment, double and single quoted; the text after the value is kept.
    (r"PASSWORD='ab\'cd1234'", "PASSWORD=<R>"),
    (r'PASSWORD="ab\"cd1234 and more" next="x"', 'PASSWORD=<R> next="x"'),
    (r'secret = "\"\"\"\"" # why', "secret = <R> # why"),
    # An escaped backslash before the closing quote is not an escaped quote.
    (r'{"password": "abcd\\", "next": "keep"}', '{"password": <R>, "next": "keep"}'),
    # JSON inside a string: its quotes escaped, and a quote of the value escaped again.
    (r"{\"password\": \"abcd1234\"}", r"{\"password\": <R>}"),
    (r"{\"password\": \"abcd\\\"efgh1234\"}", r"{\"password\": <R>}"),
    (
        r"{\"password\": \"abcd\\nefgh\\\\ijkl\", \"k\": 1}",
        r"{\"password\": <R>, \"k\": 1}",
    ),
    (r"PASSWORD=\"ab\\\"cd1234\" rest", "PASSWORD=<R> rest"),
    # Each place the value may end: the end of the text, ``;``, ``)``, ``]``.
    (r'PASSWORD="ab\"cd1234"', "PASSWORD=<R>"),
    (r'PASSWORD="ab\"cd1234";', "PASSWORD=<R>;"),
    (r'connect(password="ab\"cd1234")', "connect(password=<R>)"),
    (r'[password="ab\"cd1234"]', "[password=<R>]"),
]


@pytest.mark.parametrize(("text", "expected"), HELD_QUOTES)
def test_a_quoted_value_with_an_escaped_quote_is_removed_to_the_quote_that_closes_it(
    text: str, expected: str
) -> None:
    assert _shown(text) == expected.replace("<R>", MARKER)


@pytest.mark.parametrize("text", [r'password="a\"b"', r"token = 'x\'y'", r'{"secret": "\"\""}'])
def test_a_value_of_fewer_than_four_characters_is_left_alone_with_its_escapes_too(
    text: str,
) -> None:
    """The floor counts what the value holds, an escaped quote as one character."""
    assert _shown(text) == text


#: A value that ends in a backslash does not close at the quote after it. These keep what the rule
#: did, to the first quote: the value is not read into the text after it, whose own rules still run.
UNCLOSED_ESCAPES: list[tuple[str, str]] = [
    (r'{"password": "C:\dir\", "other": "x"}', '{"password": <R>, "other": "x"}'),
    (r'password="abc\" api_key="sk_live_abcdefgh1234" ', "password=<R> api_key=<R> "),
    (r"password='abc\' token='tok_live_abcdefgh1234' ", "password=<R> token=<R> "),
]


@pytest.mark.parametrize(("text", "expected"), UNCLOSED_ESCAPES)
def test_a_value_that_ends_in_a_backslash_is_not_read_into_the_text_after_it(
    text: str, expected: str
) -> None:
    assert _shown(text) == expected.replace("<R>", MARKER)


#: Each form, as the name and the quote that opens and closes the value, and how the form writes
#: a quote and a backslash of the secret itself (JSON inside a string escapes its own quotes, so a
#: quote of the secret is three backslashes and a quote, and a backslash is four).
BS = "\\"
FORMS: dict[str, tuple[str, str, str, str]] = {
    "assignment": ("PASSWORD=", '"', BS + '"', BS * 2),
    "colon": ("password: ", '"', BS + '"', BS * 2),
    "json": ('{"password": ', '"', BS + '"', BS * 2),
    "json-quoted-name": ('{"api_key":', '"', BS + '"', BS * 2),
    "single-quoted": ("secret = ", "'", BS + "'", BS * 2),
    "json-in-a-string": ('{\\"password\\": ', BS + '"', BS * 3 + '"', BS * 4),
    "assignment-in-a-string": ("PASSWORD=", BS + '"', BS * 3 + '"', BS * 4),
}


def _a_secret_that_holds_quotes(rng: random.Random, quote: str) -> str:
    """A secret of 12 to 60 characters with one to four quotes or backslashes inside it."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_.:/ "
    chars = [rng.choice(alphabet) for _ in range(rng.randint(12, 60))]
    for _ in range(rng.randint(1, 4)):
        chars.insert(rng.randrange(1, len(chars)), rng.choice([quote, quote, BS]))
    return "".join(chars)


@pytest.mark.parametrize("form", sorted(FORMS))
def test_no_window_of_a_secret_that_holds_quotes_survives(form: str) -> None:
    """A hundred secrets a form, each with quotes and backslashes inside it, written as the form
    writes them: not ten consecutive characters of one is in what ``redact`` returns, and the text
    around it is."""
    name, delimiter, escaped_quote, escaped_backslash = FORMS[form]
    quote = delimiter[-1]
    rng = random.Random(f"r25-{form}")
    for _ in range(100):
        secret = _a_secret_that_holds_quotes(rng, quote)
        written = secret.replace(BS, escaped_backslash).replace(quote, escaped_quote)
        text = f"before {name}{delimiter}{written}{delimiter}, after"

        shown = _shown(text)

        assert shown.startswith("before ") and shown.endswith(", after"), (text, shown)
        survivors = [
            w for w in (written[i : i + 10] for i in range(len(written) - 9)) if w in shown
        ]
        assert not survivors, (text, shown, survivors)


# --- a long run of '-' or '.' costs what a run of letters does -----------------------------------

#: CPU time where it is not too coarse to read (Windows' ticks every 15.6 ms), the wall clock there.
_clock = time.perf_counter if sys.platform == "win32" else time.process_time


def _best_of(text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        started = _clock()
        redact(text, RedactionLevel.standard)
        best = min(best, _clock() - started)
    return best


@pytest.mark.parametrize("run", ["-", ".", "-.", ".-."])
def test_a_long_run_of_hyphens_or_dots_costs_per_character_what_letters_do(run: str) -> None:
    """r25 measured it: 500 KB of ``-`` took 2.35 s and 500 KB of ``a`` 0.035 s, 65 times, because a
    secret's name was tried from every character of the run, each with the whole 48-character look
    ahead for a keyword. It is linear either way (``test_redaction_is_linear_on_hostile_input``
    asks the growth); this asks the constant, at 8 times where the cost was 50 and more, and where
    a shared runner's noise is well inside."""
    size = 250_000
    letters = _best_of("a" * size)
    in_run = _best_of((run * size)[:size])
    assert in_run <= 8 * max(letters, 0.01), (
        f"{size:,} characters of {run!r} took {in_run:.3f} s, {in_run / letters:.1f} times the "
        f"{letters:.3f} s of the same many letters"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("--api-key=abcd1234", "--api-key=<R>"),
        (".api_key=abcd1234", ".api_key=<R>"),
        ("-.-.token: abcd1234 x", "-.-.token: <R> x"),
        ("a" * 49 + ".password=hunter22", "a" * 49 + ".password=<R>"),
        ("x." + "-" * 40 + '.secret = "hunter22"', "x." + "-" * 40 + ".secret = <R>"),
    ],
)
def test_a_name_that_starts_after_hyphens_or_dots_is_still_removed(
    text: str, expected: str
) -> None:
    """What a name's first character being a letter, a digit or an underscore must not lose: a name
    after ``--``, after a ``.`` or after any run of them, and one more than 48 characters from the
    start of its word."""
    assert _shown(text) == expected.replace("<R>", MARKER)
