"""Four shapes of secret that got through the scrub, and the scrub's own failure (review r24 of
#113, findings 2, 3, 9 and 10; task #30).

The owner kept the cross-agent hand-off on by default on the promise that credentials are
scrubbed before the conversation goes to another vendor, and ``redact`` also guards what the
insights spool ships. Each shape here left the machine intact through ``redact``:

- a JWT right after a hyphen (``token-eyJ…``, ``Bearer-eyJ…``), which #25's run-start rule lost;
- a private key cut short (``head -3 id_rsa``): a header and no footer;
- a JSON-quoted name (``{"password": "…"}``), and the same JSON inside a string (a tool call's
  arguments), where its quotes are escaped;
- a key straight after text in another script, where ``\\b`` finds no boundary: ``é`` and
  ``钥`` are word characters.

Each is pinned on the text ``redact`` returns, exactly; the same shapes through the whole
hand-off (the first message and the brief file) are in
``tests/test_a_dialog_hand_off_crosses_agents.py``, and the growth test they must not slow is
``tests/test_a_credential_is_removed_whole.py``.
"""

from __future__ import annotations

import json

import pytest

from aisquare.core import redaction
from aisquare.core.redaction import MARKER, redact
from aisquare.models import RedactionLevel
from tests.test_the_hand_off_brief_keeps_its_head import _brief

#: Assembled, not written out: a scanner takes a realistic shape in a file for a live key.
JWT = ".".join(
    [
        "eyJ" + "hbGciOiJIUzI1NiJ9",
        "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0",
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ]
)
HEADER = "-----BEGIN RSA PRIVATE KEY-----"
PASSWORD = "hunter2" + "hunter2"


def _both(text: str) -> tuple[str, str]:
    return redact(text, RedactionLevel.standard), redact(text, RedactionLevel.strict)


# --- finding 2: a JWT after a hyphen -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (f"token-{JWT}", f"token-{MARKER}"),
        (f"Bearer-{JWT}", f"Bearer-{MARKER}"),
        (f"a-b-{JWT} tail", f"a-b-{MARKER} tail"),
        (f"x--{JWT}", f"x--{MARKER}"),
        (f"x-auth-token-{JWT}\nnext", f"x-auth-token-{MARKER}\nnext"),
        (f"-{JWT}", f"-{MARKER}"),
        (f"a-{JWT} b-{JWT}", f"a-{MARKER} b-{MARKER}"),
        (f"clé-{JWT}", f"clé-{MARKER}"),
        (f"token {JWT}", f"token {MARKER}"),
        (f"({JWT})", f"({MARKER})"),
        (JWT, MARKER),
    ],
)
def test_a_jwt_is_removed_after_a_hyphen_as_after_anything_else(text: str, shown: str) -> None:
    """The prefix before the hyphen is kept, the JWT goes. #25 started a JWT only where the
    character before is outside its alphabet, and the hyphen is inside it (r24 2)."""
    assert _both(text) == (shown, shown)


def test_a_jwt_started_in_the_middle_of_a_word_is_not_one() -> None:
    """The start is a run's start or a hyphen's, not any ``eyJ``: that is what keeps the rule
    linear (``eyJ-eyJ-…`` in the growth test), and what the rule always was."""
    text = f"token_{JWT}"

    assert redact(text, RedactionLevel.standard) == text


# --- finding 3a: a private key cut short ---------------------------------------------------------


def test_a_private_key_cut_short_is_removed_from_its_header_to_the_end() -> None:
    body = "\n".join("MIIEowIBAAKCAQEA" + f"{n:02d}" + "x" * 46 for n in range(4))
    text = f"head -3 id_rsa\n{HEADER}\n{body}\n"

    shown, strict = _both(text)

    assert shown == strict == f"head -3 id_rsa\n{MARKER}"


# --- finding 3b: a JSON-quoted name --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (f'{{"password": "{PASSWORD}"}}', f'{{"password": {MARKER}}}'),
        (f'{{"apiKey": "{PASSWORD}", "x": 1}}', f'{{"apiKey": {MARKER}, "x": 1}}'),
        (f'"api_key":"{PASSWORD}"', f'"api_key":{MARKER}'),
        (f"{{'secret': '{PASSWORD}'}}", f"{{'secret': {MARKER}}}"),
        (
            f'{{"db_password" : "{PASSWORD}", "port": 5432}}',
            f'{{"db_password" : {MARKER}, "port": 5432}}',
        ),
        (
            f'{{"user": "a", "token": "{PASSWORD} with spaces"}}',
            f'{{"user": "a", "token": {MARKER}}}',
        ),
        (f'{{"password":\t"{PASSWORD}"}}', f'{{"password":\t{MARKER}}}'),
        # the same JSON inside a string, where a tool call's arguments hold it: quotes escaped
        (
            f'curl -d \'{{\\"password\\": \\"{PASSWORD}\\"}}\'',
            f"curl -d '{{\\\"password\\\": {MARKER}}}'",
        ),
        (
            json.dumps({"cmd": f"""curl -d '{{"password": "{PASSWORD}"}}' https://x"""}),
            '{"cmd": "curl -d \'{\\"password\\": ' + MARKER + "}' https://x\"}",
        ),
        # a name that is not quoted, with a value that is, inside such a string
        (
            json.dumps({"cmd": f'PASSWORD="{PASSWORD}" ./run'}),
            '{"cmd": "PASSWORD=' + MARKER + ' ./run"}',
        ),
    ],
)
def test_a_json_quoted_name_takes_its_quoted_value_with_it(text: str, shown: str) -> None:
    assert _both(text) == (shown, shown)


@pytest.mark.parametrize(
    "text",
    [
        '{"input_tokens": 12345, "output_tokens": 678}',
        '{"token_type": 99999, "max_tokens": null}',
        '{"password": "abc"}',  # under the floor of any value: a shape, and prose is not shaped so
        'if kind == "token":\n    return value_here',
        'the "password" field is required: set it',
        'headers["token"] = value_of_it',
        '{"password":\n  "hunter2hunter2"}',  # the value of a quoted name is on its line
        '{"name": "token", "kind": "plain words"}',
    ],
)
def test_a_json_quoted_name_without_a_quoted_value_is_left_alone(text: str) -> None:
    """A secret in JSON is a string. A count, a comparison and the end of a string in code are
    not, and a rule that took them would take them out of the dataset for nothing."""
    assert _both(text) == (text, text)


# --- finding 3c: a key after text in another script ----------------------------------------------

TOKEN = "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
PREFIXES = [
    "sk-",
    "ghp_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "xapp-",
    "AKIA",
    "aisq_",
    "aisqr_",
    "AIza",
    "wk_live_",
]


@pytest.mark.parametrize("lead", ["é", "我的密钥", "ß", "Ω", "日本語"])
@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_key_straight_after_text_in_another_script_is_removed(prefix: str, lead: str) -> None:
    """``\\b`` is Unicode-aware, so ``é`` and ``钥`` are word characters and there is no boundary
    between them and ``sk-``. The start of a key is: not after an ASCII letter, digit or ``_``."""
    text = f"{lead}{prefix}{TOKEN} and then"

    assert _both(text) == (f"{lead}{MARKER} and then",) * 2


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("密钥token=abcdefgh1234", f"密钥token={MARKER}"),
        ('密钥"token": "abcdefgh1234"', f'密钥"token": {MARKER}'),
        ("密钥password: hunter2hunter2", f"密钥password: {MARKER}"),
        ("头bearer abcdefghijkl1234", f"头bearer {MARKER}"),
        ("头Authorization: Bearer abcdefghijkl1234", f"头Authorization: Bearer {MARKER}"),
        ("头Authorization: abcdefghijkl1234", f"头Authorization: {MARKER}"),
    ],
)
def test_a_name_or_a_header_straight_after_another_script_is_taken_too(
    text: str, shown: str
) -> None:
    assert _both(text) == (shown, shown)


@pytest.mark.parametrize(
    "text",
    [
        "task-management-system-overview",
        "risk-assessment-framework-v2-final",
        "desk-" + "x" * 20,
        "mysecretsk-" + "a" * 20,
        "highp_" + "a" * 20,
        "TOKENAKIA" + "A" * 20,
    ],
)
def test_a_prefix_inside_an_ascii_word_is_not_a_key(text: str) -> None:
    """Half of the boundary stays: ``sk-`` in ``task-`` is a word, not a key."""
    assert redact(text, RedactionLevel.standard) == text


# --- finding 9: the scrub fails closed ---------------------------------------------------------


def _fails(*_: object, **__: object) -> object:
    raise RuntimeError("the scrub broke")


class _BrokenPattern:
    """A compiled pattern whose ``sub`` raises: what ``re`` does not do, and a bug could."""

    pattern = "broken"

    def sub(self, *_: object, **__: object) -> str:
        raise RuntimeError("the scrub broke")

    def search(self, *_: object, **__: object) -> None:
        return None


SECRET_TEXT = "my key is sk-ant-api03-" + "Zq9xW3mK" * 6 + " and the rest of the prompt"


@pytest.mark.parametrize(
    ("stage", "level"),
    [
        ("the private-key pass", RedactionLevel.standard),
        ("the private-key pass", RedactionLevel.strict),
        ("a credential rule", RedactionLevel.standard),
        ("a credential rule", RedactionLevel.strict),
        ("an identity rule", RedactionLevel.strict),  # the identity rules run at strict only
    ],
)
def test_when_the_scrub_fails_what_comes_back_is_the_marker_never_the_text(
    stage: str, level: RedactionLevel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback must not be the raw text: the scrubber's one job is that a secret does not
    leave, and a bug in it is the case where it matters most (r24 9: the mutant that returns the
    text lived)."""
    if stage == "the private-key pass":
        monkeypatch.setattr(redaction, "_without_pem_blocks", _fails)
    elif stage == "a credential rule":
        monkeypatch.setattr(redaction, "_CREDENTIAL_RULES", ((_BrokenPattern(), MARKER),))
    else:
        monkeypatch.setattr(redaction, "_IDENTITY_RULES", ((_BrokenPattern(), MARKER),))

    shown = redact(SECRET_TEXT, level)

    assert shown == MARKER
    assert "sk-ant" not in shown and "Zq9xW3mK" not in shown


def test_off_asks_for_the_text_as_typed_whatever_the_scrub_would_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redaction, "_without_pem_blocks", _fails)

    assert redact(SECRET_TEXT, RedactionLevel.off) == SECRET_TEXT


def test_a_brief_made_while_the_scrub_fails_holds_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hand-off's one exit is ``HandoffBrief.text()``: a scrub that breaks costs the
    conversation's words, and the key stays here."""
    monkeypatch.setattr(redaction, "_without_pem_blocks", _fails)
    brief = _brief(
        1,
        conversation=("> it fails", f"  ANTHROPIC_API_KEY={SECRET_TEXT}", "> and then"),
        task=None,
        entries=(),
    )

    text = brief.text()

    assert "sk-ant" not in text and "Zq9xW3mK" not in text
