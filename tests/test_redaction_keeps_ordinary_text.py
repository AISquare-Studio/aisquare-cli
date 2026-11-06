"""The redactor takes the credential and not what is around it (R9k, after #103's part 2).

0.10.0's redactor took two things 0.9.2 sent, with ``explainability.ship`` or the CI test bed on:
the string value under any quoted name that held a credential's word (``"max_tokens": "8192"``,
``"auth_token_url": "https://…"``), and the rest of the text after a private-key header with no
footer. Now a quoted name takes its value only when it IS a credential's name, and a header with
no footer takes the key's body after it and no more. Removing less than the credential is a leak,
so each pin here goes both ways, and every credential the rule took before is still taken. A
header with no body after it holds nothing secret, and is kept as 0.9.2 kept it (H1).
"""

from __future__ import annotations

import pytest

from aisquare.core import insights
from aisquare.core.redaction import MARKER, redact
from aisquare.models import RedactionLevel
from tests.redaction_corpus import CORPUS, GHP, GOOGLE_ACCESS, HEADER, NPM, WHSEC

SECRET = "hunter2" + "hunter2"
FOOTER = "-----END RSA PRIVATE KEY-----"
#: A key's body: four 64-character lines and a short last one, as PEM writes them.
BODY = [
    "MIIEowIBAAKCAQEAx7Fk9sQmPq3vN2wZqL8r0TmY5bVfJcW1nKd4hEgUoPiAsDfG",
    "hJkLzXcVbNm1Q2w3E4r5T6y7U8i9O0pAaSsDdFfGgHhJjKkLlZzXxCcVvBbNnMm",
    "Qq1Ww2Ee3Rr4Tt5Yy6Uu7Ii8Oo9Pp0AaSsDdFfGgHhJjKkLlZzXxCcVvBbNnMmQq",
    "Ww2Ee3Rr4Tt5Yy6Uu7Ii8Oo9Pp0AaSsDdFfGgHhJjKkLlZzXxCcVvBbNnMm+/9Q",
    "8Q2w3E4r5T6y==",
]


def _both(text: str) -> tuple[str, str]:
    return redact(text, RedactionLevel.standard), redact(text, RedactionLevel.strict)


def _forms(name: str, value: str) -> list[str]:
    """``name`` and ``value`` as JSON, as a Python dict, and as JSON inside a string."""
    return [
        f'{{"{name}": "{value}"}}',
        f"{{'{name}': '{value}'}}",
        f'{{\\"{name}\\": \\"{value}\\"}}',
    ]


# --- 1. a quoted name is a credential's when it IS one ----------------------------------------

#: The ruling's names that hold a credential's word and are not one, and more of the kind.
KEPT_NAMES = [
    "max_tokens",
    "auth_token_url",
    "password_policy",
    "key_count",
    "token_type",
    "num_keys",
    # more of the kind
    "jsonwebtoken",
    "tokenizer",
    "input_tokens",
    "token_endpoint",
    "secret_name",
    "password_hint",
    "api_key_header",
    "ApiKeyName",
    "authProvider",
    "oauth",
    "OAuth",
    "key",
    "PWD",
    "OLDPWD",
]

#: The ruling's credential names, and more of the kind: joined, suffixed, cased each way.
REMOVED_NAMES = [
    "api_key",
    "apiKey",
    "access_token",
    "refresh_token",
    "client_secret",
    "AWS_SECRET_ACCESS_KEY",
    "x-api-key",
    "password",
    "db_password",
    "private_key",
    "secretKey",
    "auth",
    # more of the kind
    "apikey",
    "API_KEY",
    "xApiKey",
    "accessToken",
    "accesstoken",
    "ACCESSTOKEN",
    "dbpassword",
    "_authToken",
    "x-auth-token",
    "basic_auth",
    "basicAuth",
    "endpoint_secret",
    "credentials",
    "passwd",
    "pwd",
    "db_pwd",
    "DB_PWD",
    "SECRET_KEY_BASE",
    "STRIPE_SECRET_KEY_LIVE",
    "DB_PASSWORD_PROD",
    "api_key_v2",
    "apiKeyV2",
    "token2",
    "token_value",
    "password_hash",
    "password_confirmation",
]


@pytest.mark.parametrize("name", KEPT_NAMES)
def test_a_quoted_name_that_holds_a_credentials_word_and_is_not_one_keeps_its_value(
    name: str,
) -> None:
    """0.9.2 kept the value under every quoted name; 0.10.0 took it under any that held one of
    the words (``"max_tokens": "8192"``). A name whose last segment is not a credential's word
    names something about one, or nothing of one."""
    for text in _forms(name, "8192 or a sentence"):
        assert _both(text) == (text, text)


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_a_quoted_credential_name_takes_its_value_and_keeps_the_name(name: str) -> None:
    for text in _forms(name, SECRET):
        shown, strict = _both(text)

        assert SECRET not in shown and SECRET not in strict, text
        assert name in shown and MARKER in shown, (text, shown)


@pytest.mark.parametrize(
    ("package", "version"),
    [
        ("next-auth", "^4.24.5"),
        ("@octokit/auth-token", "~4.0.0"),
        ("generate-password", "1.7.1"),
        ("better-auth", ">=1.0.0 <2"),
        ("next-auth", "latest"),
        ("next-auth", "workspace:*"),
    ],
)
def test_a_package_whose_name_ends_in_a_credentials_word_keeps_its_version(
    package: str, version: str
) -> None:
    """A ``package.json`` names its packages as quoted names, and ``next-auth`` ends in one of
    the words: its value is a version, which no secret is shaped as."""
    text = f'{{"dependencies": {{"{package}": "{version}", "react": "^19.0.0"}}}}'

    assert _both(text) == (text, text)


def test_a_credential_name_with_a_value_that_is_not_a_version_is_still_taken() -> None:
    """The version's shape is the start of the value, not a value that holds digits."""
    for value in ("hunter2.0rocks", "4abc.def", "v" + SECRET):
        text = f'{{"auth": "{value}"}}'
        assert redact(text, RedactionLevel.standard) == f'{{"auth": {MARKER}}}', value


# --- 2. a header with no footer takes its key's body, and no more -----------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (
            f"why does grep '{HEADER}' match nothing in my repo? Here is the output: none",
            f"why does grep '{HEADER}' match nothing in my repo? Here is the output: none",
        ),
        (f"{HEADER} is the first line of the file", f"{HEADER} is the first line of the file"),
        (f"what is {HEADER} here", f"what is {HEADER} here"),
        (HEADER, HEADER),
        (f"{HEADER}\n\nthen what?", f"{HEADER}\n\nthen what?"),
        (
            f"{HEADER}\n{BODY[0]}\n\nWhat does this mean? It fails.",
            f"{MARKER}\n\nWhat does this mean? It fails.",
        ),
        (f"{HEADER}\n{BODY[0]}\nthen prose after it", f"{MARKER}\nthen prose after it"),
        (f"KEY={HEADER} {BODY[0]} end of it", f"KEY={MARKER} end of it"),
        (f"./docs/keys.md:3:{HEADER}\n./b.sh:12:if", f"./docs/keys.md:3:{HEADER}\n./b.sh:12:if"),
    ],
)
def test_a_header_with_no_footer_loses_its_body_and_the_prose_after_it_stays(
    text: str, shown: str
) -> None:
    """The ruling's example and its kind: a header quoted, grepped for or named in a sentence has
    no body after it, holds nothing secret, and is kept as 0.9.2 kept it (H1). A key's lines after
    a header go with it, and never the words after them."""
    assert _both(text) == (shown, shown)


def _layouts() -> dict[str, tuple[str, str]]:
    """A key with no footer as each place writes it, and the text it must leave: (text, shown)."""
    lines, short = BODY[:4], BODY[4]
    return {
        "lines": (f"{HEADER}\n" + "\n".join(BODY) + "\nthe next line", f"{MARKER}\nthe next line"),
        "crlf": (
            f"{HEADER}\r\n" + "\r\n".join(BODY) + "\r\nthe next line",
            f"{MARKER}\r\nthe next line",
        ),
        "indented": (
            "key: |\n  " + HEADER + "\n  " + "\n  ".join(BODY) + "\nnext: value",
            f"key: |\n  {MARKER}\nnext: value",
        ),
        "json escaped": (
            'cfg = "' + HEADER + "\\n" + "\\n".join(BODY) + '" # then prose here',
            f'cfg = "{MARKER}" # then prose here',
        ),
        "json escaped twice": (
            '\\"' + HEADER + "\\\\n" + "\\\\n".join(BODY) + '\\" and so',
            f'\\"{MARKER}\\" and so',
        ),
        "one line": (f"K={HEADER} " + " ".join(BODY), f"K={MARKER}"),
        "concatenated": (
            'k = (\n  "' + HEADER + '\\n"\n' + "".join(f'  "{line}\\n"\n' for line in BODY) + ")",
            f'k = (\n  "{MARKER}\\n"\n)',
        ),
        "quoted mail": (
            f"> {HEADER}\n" + "".join(f"> {line}\n" for line in BODY) + "\nis this ok?",
            f"> {MARKER}\n\nis this ok?",
        ),
        "encrypted": (
            f"{HEADER}\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF\n\n"
            + "\n".join(lines)
            + f"\n{short}\nand prose",
            f"{MARKER}\nand prose",
        ),
        "head -3": (
            f"$ head -3 id_rsa\n{HEADER}\n{lines[0]}\n{lines[1]}\n$ why?",
            f"$ head -3 id_rsa\n{MARKER}\n$ why?",
        ),
        "the end of the text": (f"look:\n{HEADER}\n{lines[0]}\nMIIEow", f"look:\n{MARKER}"),
    }


@pytest.mark.parametrize("layout", sorted(_layouts()))
def test_a_keys_body_with_no_footer_is_taken_in_every_layout(layout: str) -> None:
    text, shown = _layouts()[layout]

    assert _both(text) == (shown, shown)
    for line in BODY:
        assert not any(line[at : at + 6] in shown for at in range(len(line) - 5)), (layout, line)


def test_a_line_of_one_word_after_a_keys_lines_is_read_as_its_last_line() -> None:
    """A key's last line is short (``8Q2w3E4r5T6y==``), and a line of one word is shaped as one:
    the rule takes it, where leaving it would ship the end of the key. A line with a space in it,
    a question, is prose."""
    text = f"{HEADER}\n{BODY[0]}\nthanks\nwhat now?"

    assert _both(text) == (f"{MARKER}\nwhat now?",) * 2


@pytest.mark.parametrize(
    "layout", ["lines", "crlf", "indented", "json escaped", "json escaped twice", "one line"]
)
def test_a_key_the_8000_character_clip_cuts_leaves_no_byte_of_its_body(
    layout: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``core.insights`` clips a prompt to 8,000 characters and THEN scrubs it, so the scrub sees a
    key cut wherever the clip fell, and the clip's ``…`` after it. At every cut from the end of
    the header to the footer, nothing but the marker is left of the key, or the header alone where
    the cut left no byte of the body after it."""
    monkeypatch.setattr(insights, "redaction_level", lambda: RedactionLevel.standard)
    joint = {
        "lines": "\n",
        "crlf": "\r\n",
        "indented": "\n    ",
        "json escaped": "\\n",
        "json escaped twice": "\\\\n",
        "one line": " ",
    }[layout]
    key = HEADER + joint + joint.join(BODY) + joint + FOOTER
    mark = insights._TRUNCATION_MARK
    limit = insights._MAX_TEXT - len(mark)
    for cut in range(len(HEADER), len(key) - len(FOOTER)):
        text = "#" * (limit - cut) + key + "\nand what now?"

        shown = insights._outbound(text)

        assert shown.endswith(mark), cut
        # what may be left: the padding, a separator, and half of an escaped one (``\``)
        left = shown[: -len(mark)].replace(MARKER, "").replace(HEADER, "").replace(joint, "")
        assert set(left) <= {"#", "\n", "\r", " ", "\\"}, (layout, cut, shown[-150:])


# --- 3. ordinary prompts and code ------------------------------------------------------------

#: What ``redact`` makes of each entry of the corpus that it changes, at ``standard``; every other
#: entry comes back as it was given. Each line names how 0.9.2 differs:
#:   = 0.9.2 makes the same of it (an unquoted name, read as 0.9.2 read it);
#:   C a credential 0.9.2 missed.
#: A private-key header with no key after it (``grep for headers``) reads as 0.9.2's (H1).
CHANGED = {
    # C: an access token in an OAuth token response
    "oauth token response": (
        f'{{"access_token": {MARKER}, "token_type": "Bearer", "expires_in": 3599, '
        '"scope": "openid email"}'
    ),
    # =: an unquoted name's value
    "k8s secret manifest": (
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db-secret\ntype: Opaque\n"
        f"stringData:\n  password: {MARKER}"
    ),
    # C: a registry login (base64 of user:password)
    "docker config.json": f'{{"auths": {{"https://index.docker.io/v1/": {{"auth": {MARKER}}}}}}}',
    # =: ``secretsmanager:`` reads as an unquoted name holding ``secret``
    "iam policy": (
        '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
        f'"Action": "secretsmanager:{MARKER}", "Resource": "*"}}]}}'
    ),
    # C: a webhook's signing secret
    "stripe webhook": f'{{"endpoint_secret": {MARKER}}}',
    "docs env": f"Set `API_KEY` in your environment, e.g. `export API_KEY={MARKER}",  # =
    "env grep": f"$ env | grep -i token\nGITHUB_TOKEN={MARKER}\nNO_TOKENS=1",  # =
    # C: a key's first lines, which 0.9.2 sent whole for want of a footer
    "head of a key": f"$ head -3 ~/.ssh/id_rsa\n{MARKER}\n$ why is this key rejected?",
    "psql url": f'$ psql "postgresql://{MARKER}@db.internal:5432/app"',  # =
    "npmrc": f"Here is my .npmrc: //registry.npmjs.org/:_authToken={MARKER}",  # =
    # C: values under credential names; a fixture's are indistinguishable from a real one's
    "python fixture": f'FAKE = {{"api_key": {MARKER}, "token": {MARKER}}}',
    "json pair": f'{{"key": "value", "secret": {MARKER}}}',  # C
    # =: unquoted names read as 0.9.2 read them
    "python sdk call": (
        f"def ask(client, prompt, *, max_tokens={MARKER}\n"
        f"    return client.messages.create(max_tokens={MARKER} messages=prompt)"
    ),
    "ts interface": (
        f"interface TokenResponse {{\n  access_token: {MARKER}\n  token_type: {MARKER};\n"
        "  expires_in: number;\n}"
    ),
    "sql": f"SELECT id FROM users WHERE reset_token = {MARKER};",
}


def test_the_corpus_names_only_entries_it_has() -> None:
    assert set(CHANGED) <= {label for label, _ in CORPUS}


@pytest.mark.parametrize(("label", "text"), CORPUS, ids=[label for label, _ in CORPUS])
def test_an_ordinary_prompt_loses_its_credentials_and_nothing_else(label: str, text: str) -> None:
    """JSON configs, API docs, shell sessions, stack traces and code that names its tokens, keys
    and secrets: what is not a credential comes back as it was typed."""
    shown = CHANGED.get(label, text)

    assert redact(text, RedactionLevel.standard) == shown
    for secret in (GHP, GOOGLE_ACCESS, NPM, WHSEC):
        assert secret not in redact(text, RedactionLevel.standard)
