"""The redactor takes the credential and not what is around it (R9k, after #103's part 2).

0.10.0's redactor took two things 0.9.2 sent, with ``explainability.ship`` or the CI test bed on:
the string value under any quoted name that held a credential's word (``"max_tokens": "8192"``,
``"auth_token_url": "https://…"``), and the rest of the text after a private-key header with no
footer. What leaves the machine is scrubbed SECURITY FIRST (reopen 2's policy): a quoted name that
holds a credential's word anywhere takes its value, as the PR's rule did, except an exact list of
documented fields and a value shaped as no secret is (a package's spec, a placeholder); a name
that only mentions a word loses an ordinary value with it, which is accepted. A header with no
footer takes the key's body after it and no more, read past a listing's gutter on each line; a
header with no body after it holds nothing secret, and is kept as 0.9.2 kept it (H1).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from aisquare.core import insights
from aisquare.core.redaction import MARKER, redact
from aisquare.models import RedactionLevel
from tests.redaction_corpus import ANTHROPIC, CORPUS, GHP, GOOGLE_ACCESS, HEADER, NPM, WHSEC

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


# --- 1. a quoted name that holds a credential's word takes its value ----------------------------

#: The ONLY names that keep a value by name: documented fields, each as its API spells it.
NOT_CREDENTIALS = [
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "token_type",
    "token_count",
    "tokenizer",
    "tokenizer_class",
    "NextToken",
    "ContinuationToken",
    "NextContinuationToken",
    "continuationToken",
    "pageToken",
    "nextPageToken",
    "syncToken",
    "nextSyncToken",
    "startPageToken",
    "deltaToken",
    "skipToken",
    "password_policy",
    "auth_token_url",
    "token_uri",
    "auth_uri",
    "token_endpoint",
    "token_endpoint_auth_method",
    "apiKeyHelper",
]

#: Names that hold no credential's word: kept, as the PR and 0.9.2 kept them.
NO_CREDENTIAL_WORD = [
    "key_count",
    "num_keys",
    "key",
    "oauth",
    "OAuth",
    "authProvider",
    "author",
    "authority",
    "pass_word",
    # ``auth`` is a segment only where one starts, and not after a digit
    "x509auth",
]

#: Credential names, and names that only hold a word: every one takes its value.
REMOVED_NAMES = [
    # the R9k ruling's
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
    "api_key_v2_prod",
    "DB_PASSWORD_PROD_V2",
    # one word (reopen 1, F1)
    "PGPASSWORD",
    "MYSQLPASSWORD",
    "REDISPASSWORD",
    "newpassword",
    "oldpassword",
    "currentpassword",
    "confirmpassword",
    "JWTSECRET",
    "jwtsecret",
    "WEBHOOKSECRET",
    "OAUTHTOKENSECRET",
    "mytoken",
    "npmtoken",
    "hftoken",
    "GITHUBTOKEN",
    "SecretBinary",
    "secret_binary",
    "SecretBytes",
    # reopen 2, G2: a key compound in one word, a qualifier after the word, a plural, pwd
    "googleapikey",
    "OPENAIAPIKEY",
    "STRIPESECRETKEY",
    "AWSSECRETACCESSKEY",
    "awsaccesskey",
    "sshprivatekey",
    "private_key_passphrase",
    "password_new",
    "password_old",
    "password_repeat",
    "API_KEY_SECONDARY",
    "API_KEY_OLD",
    "GITHUB_TOKEN_READONLY",
    "DB_PASSWORD_ADMIN",
    "secret_id",
    "api_keys",
    "API_KEYS",
    "privateKeyPem",
    "private_key_pem",
    "credentials_json",
    "DBPWD",
    "mysqlpwd",
    "TOKEN_GITHUB",
    "secrets",
    # reopen 2, G3: a token under a cursor's word that is not the documented field
    "FB_PAGE_TOKEN",
    "page_token",
    "PAGE_TOKEN",
    "NEXT_TOKEN",
    "next_page_token",
    "paginationToken",
    "cursorToken",
    "CURSOR_TOKEN",
    "nexttoken",
    "resume_token",
    # a documented field's name in another case or another name: only the exact one is passed by
    "MAX_TOKENS",
    "Max_Tokens",
    "usage.max_tokens",
    "PageToken",
    "pagetoken",
    "TOKEN_TYPE",
    "Tokenizer",
    "tokenizer_type",
    "TokenEndpoint",
    "apikeyhelper",
    "NextTokenSecret",
    "tokenizer_api_key",
    "my max_tokens",
    # names that only mention a word: their ordinary values go too (P3, accepted)
    "input_tokens",
    "secret_name",
    "password_hint",
    "api_key_header",
    "ApiKeyName",
    "PWD",
    "OLDPWD",
    "login.password",
    "gen-token",
]


@pytest.mark.parametrize("name", NOT_CREDENTIALS + NO_CREDENTIAL_WORD)
def test_a_documented_field_and_a_name_with_no_credentials_word_keep_their_value(
    name: str,
) -> None:
    """The exact list passes by its fields whatever their value holds (``"max_tokens":
    "8192"``, a pagination cursor), and a name with no credential's word is not read at all."""
    for text in _forms(name, SECRET):
        assert _both(text) == (text, text)


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_a_quoted_name_that_holds_a_credentials_word_takes_its_value_inside_its_quotes(
    name: str,
) -> None:
    """Every name the PR's rule took, taken again (reopen 2, P1), and the name kept. The marker
    stands inside the value's own quotes, so JSON stays JSON (P5)."""
    for text, shown in zip(_forms(name, SECRET), _forms(name, MARKER), strict=True):
        assert _both(text) == (shown, shown)


def test_json_with_its_credentials_taken_is_json_still() -> None:
    """``"password": "[redacted]"``, as 0.9.2 wrote a token it took from inside a value: a
    reader that parses what was shipped still can (P5)."""
    document = {"user": "bob", "password": 'a"b\\c1234', "api_key": SECRET, "max_tokens": "8192"}
    for text in (json.dumps(document), json.dumps({"cmd": json.dumps(document)})):
        shown = redact(text, RedactionLevel.standard)

        parsed = json.loads(shown)
        inner = json.loads(parsed["cmd"]) if "cmd" in parsed else parsed
        assert inner == {**document, "password": MARKER, "api_key": MARKER}


@pytest.mark.parametrize(
    ("package", "version"),
    [
        ("next-auth", "^4.24.5"),
        ("@octokit/auth-token", "~4.0.0"),
        ("generate-password", "1.7.1"),
        ("better-auth", ">=1.0.0 <2"),
        ("next-auth", "latest"),
        ("next-auth", "workspace:*"),
        ("jsonwebtoken", "^9.0.2"),
        # reopen 1, F2: dist-tags and the spec protocols
        ("next-auth", "beta"),
        ("next-auth", "canary"),
        ("next-auth", "nightly"),
        ("next-auth", "experimental"),
        ("better-auth", "github:better-auth/better-auth#main"),
        ("@octokit/auth-token", "catalog:"),
        ("my-token", "git+https://github.com/org/my-token.git"),
        ("my-token", "git://github.com/org/my-token.git"),
        ("x-auth", "https://registry.example.com/x-auth/-/x-auth-1.0.0.tgz"),
        ("x-auth", "https://github.com/org/x-auth#v1.2.0"),
        ("x-auth", "patch:x-auth@1.0.0#./x.patch"),
        ("x-auth", "portal:../x-auth"),
        # reopen 2 (MT2): a range whose second part is x
        ("next-auth", "^4.x"),
        ("@octokit/auth-token", "2.x || 3.x"),
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
    for value in (
        "hunter2.0rocks",
        "4abc.def",
        "v" + SECRET,
        "beta" + SECRET,
        "github-" + SECRET,
        # an http(s) URL that is neither a tarball's nor a git ref's: a webhook, a signed download
        "https://hooks.slack.com/services/T0000/B0000/" + SECRET,
        "https://cdn.example.com/x-1.0.0.tgz?X-Amz-Signature=" + SECRET,
    ):
        text = f'{{"auth": "{value}"}}'
        assert redact(text, RedactionLevel.standard) == f'{{"auth": "{MARKER}"}}', value


@pytest.mark.parametrize(
    "value",
    [
        # a placeholder, and a model's special tokens (Hugging Face's tokenizer_config.json)
        "<YOUR_API_KEY>",
        "<your api key>",
        "</s>",
        "<unk>",
        "<|endoftext|>",
        "[CLS]",
        "[MASK]",
        "[SEP]",
        # a template's reference: a devcontainer, a GitHub Actions workflow, Postman, Jinja
        "${localEnv:GITHUB_TOKEN}",
        "${GITHUB_TOKEN}",
        "${{ secrets.NPM_TOKEN }}",
        "{{access_token}}",
        "{{ api_key }}",
        # a boolean, and null
        "true",
        "false",
        "null",
        "True",
        "False",
        "None",
        "TRUE",
        "FALSE",
        "NULL",
    ],
)
def test_a_value_that_stands_for_one_is_kept_under_any_name(value: str) -> None:
    """A whole value that is a placeholder, a reference or a boolean is shaped as no issued
    credential is (reopen 2, P2a): kept under ``api_key`` as under any name."""
    for name in ("api_key", "eos_token", "GITHUB_TOKEN", "verify_password"):
        for text in _forms(name, value):
            assert _both(text) == (text, text)


@pytest.mark.parametrize(
    "value",
    [
        "<YOUR_API_KEY>" + SECRET,
        SECRET + "<x>",
        "<a> " + SECRET,
        "[Cls]" + SECRET,
        "[CLS]x",
        "${unclosed" + SECRET,
        "${a}" + SECRET,
        "{{x}" + SECRET,
        "trueish" + SECRET,
        "nullable",
        "Nonesuch",
        "[hunter2hunter2]",
    ],
)
def test_a_value_that_only_starts_or_ends_like_a_placeholder_is_taken(value: str) -> None:
    """The placeholder is the WHOLE value, to its closing quote."""
    for text, shown in zip(_forms("api_key", value), _forms("api_key", MARKER), strict=True):
        assert _both(text) == (shown, shown)


def test_a_placeholder_and_an_escaped_quote_are_not_the_whole_value() -> None:
    """``"<YOUR_KEY>\\"…"`` is a value that holds a quote, read to the quote that closes it."""
    text = '{"api_key": "<YOUR_KEY>\\"' + SECRET + '", "n": 1}'

    assert _both(text) == (f'{{"api_key": "{MARKER}", "n": 1}}',) * 2


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
        # reopen 1: a long word, or a path, after a header is prose: it does not read as base64
        (
            f"{HEADER}\ninternationalization breaks when I paste the key",
            f"{HEADER}\ninternationalization breaks when I paste the key",
        ),
        (
            f"{HEADER}\n/srv/projects/deploy/keys is where it lives",
            f"{HEADER}\n/srv/projects/deploy/keys is where it lives",
        ),
        # and a run of 30 that does, with prose after it on its line, is the key's
        (f"{HEADER}\n{BODY[0][:30]} is the start of it", f"{MARKER} is the start of it"),
        # reopen 2 (MT3): lower case and digits read as base64, as an EC key's hex dump does
        (
            "-----BEGIN EC PRIVATE KEY-----\n3077020101042b8a9f0c6d1e5a7b2c4f3e9d8c7b is what "
            "head -2 shows",
            f"{MARKER} is what head -2 shows",
        ),
        # ACCEPTED (reopen 2): an identifier in both cases that starts the line after a header
        # reads as base64, and goes with it; the rest of its line stays
        (
            f"{HEADER}\nAuthenticationFailedException: key rejected",
            f"{MARKER}: key rejected",
        ),
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


#: A listing's gutter for each line, by the line's number (the header's is line 1): a number,
#: a file's name, or a prefix that cannot be read (a comment's, a diff's).
GUTTERS: dict[str, Callable[[int], str]] = {
    "cat -n": lambda n: f"{n:6}\t",
    "nl": lambda n: f"{n:6}  ",
    "less -N": lambda n: f"{n:>7} ",
    "Read tool": lambda n: f"{n:6}\u2192",
    "editor gutter": lambda n: f"{n:>3} | ",
    "bat": lambda n: f"{n:>3} \u2502 ",
    "grep -n": lambda n: f"id_rsa:{n}:",
    "grep -n -A": lambda n: f"keys/my-key.pem:{n}:" if n == 1 else f"keys/my-key.pem-{n}-",
    "grep -n, one file": lambda n: f"{n}:",
    "grep -A": lambda n: "id_rsa:" if n == 1 else "id_rsa-",
    "a comment": lambda n: "# ",
    "a diff's removal": lambda n: "-",
}
#: The gutters that cannot be read: the lines that start with one go with the header.
UNREAD = {"a comment", "a diff's removal"}


def _listed(gutter: Callable[[int], str], lines: list[str], joint: str = "\n") -> str:
    return joint.join(gutter(n) + line for n, line in enumerate(lines, start=1))


@pytest.mark.parametrize("layout", sorted(GUTTERS))
def test_a_keys_lines_after_a_listings_gutter_are_its_body(layout: str) -> None:
    """G1 (reopen 2, P4): ``cat -n``, ``nl``, ``less -N``, Claude Code's Read tool, an editor's
    gutter and ``grep -n`` print each line of a key after a gutter, and the body, read as lines
    that start with base64, stopped at the first: R9k shipped the key whole, the PR took it.
    Where the header's line has a number or a file's name before it, the key's lines may have
    one too, and the line after the key, prose with its own number, stays. Any other prefix
    cannot be read, and the lines that start with it after the header go with it."""
    gutter = GUTTERS[layout]
    after = ("" if layout in UNREAD else gutter(len(BODY) + 2)) + "see the error below"
    text = _listed(gutter, [HEADER, *BODY]) + "\n" + after

    assert _both(text) == (gutter(1) + MARKER + "\n" + after,) * 2


def test_a_key_in_html_lines_and_a_read_result_in_json_are_taken() -> None:
    lines = [HEADER, *BODY]
    html = "<p>" + "<br>".join(lines) + "</p><p>why?</p>"
    read = '{"content": "' + _listed(GUTTERS["Read tool"], lines, "\\n") + '", "is_error": false}'

    assert _both(html) == (f"<p>{MARKER}</p><p>why?</p>",) * 2
    assert _both(read) == (f'{{"content": "     1\u2192{MARKER}", "is_error": false}}',) * 2


def test_the_lines_after_a_header_that_start_with_a_prefix_it_cannot_read_go_with_it() -> None:
    """ACCEPTED (P4): what starts as the header's line did is taken whatever it holds."""
    text = f"# {HEADER}\n# {BODY[0]}\n# generated by ssh-keygen\nprint(key)"

    assert _both(text) == (f"# {MARKER}\nprint(key)",) * 2


@pytest.mark.parametrize(
    "text",
    [
        f"     5\t{HEADER}\n     6\tis how the file starts, and then\n     7\tthe body follows",
        f"README.md:40:  {HEADER}\nREADME.md-41-  is the first line of the key",
        f"  12 | {HEADER}\n  13 | ",
        f"     1\u2192{HEADER}\n     2\u2192",
        f"# {HEADER} is the first line\nprint(key)",
    ],
)
def test_a_listed_header_with_no_body_after_it_is_kept(text: str) -> None:
    """H1 where a listing numbered the lines: a header quoted in a file, with prose after it."""
    assert _both(text) == (text, text)


def test_a_line_of_one_word_after_a_keys_lines_is_read_as_its_last_line() -> None:
    """A key's last line is short (``8Q2w3E4r5T6y==``), and a line of one word is shaped as one:
    the rule takes it, where leaving it would ship the end of the key. A line with a space in it,
    a question, is prose."""
    text = f"{HEADER}\n{BODY[0]}\nthanks\nwhat now?"

    assert _both(text) == (f"{MARKER}\nwhat now?",) * 2


def test_the_clips_notice_stays_after_a_dek_info_line_it_cut() -> None:
    """An RFC 1421 line runs to its end, and the clip's notice is not the line's: the text still
    says it was cut (reopen 1)."""
    mark = insights._TRUNCATION_MARK
    text = f"{HEADER}\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CB{mark}"

    assert _both(text) == (f"{MARKER}{mark}",) * 2


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


@pytest.mark.parametrize("layout", [*sorted(GUTTERS), "html", "Read result in JSON"])
def test_a_listed_key_the_8000_character_clip_cuts_leaves_no_byte_of_its_body(
    layout: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each listing's layout (reopen 2, P4) cut by ``core.insights``' clip at every point from
    the end of the header's line to the footer: what is left of the key is its header's gutter,
    the marker (or the header, where no byte of the body came before the cut), and at most the
    gutter of the line the clip cut, or a piece of a line break."""
    monkeypatch.setattr(insights, "redaction_level", lambda: RedactionLevel.standard)
    wrapper, joint = "", "\n"
    gutter: Callable[[int], str] = GUTTERS.get(layout, lambda n: "")
    if layout == "html":
        joint = "<br>"
    elif layout == "Read result in JSON":
        wrapper, joint, gutter = '{"content": "', "\\n", GUTTERS["Read tool"]
    lines = [HEADER, *BODY, FOOTER]
    key = wrapper + _listed(gutter, lines, joint)
    allowed = {""} | {joint[:at] for at in range(len(joint))}
    allowed |= {joint + gutter(n)[:at] for n in range(1, len(lines) + 1) for at in range(99)}
    mark = insights._TRUNCATION_MARK
    limit = insights._MAX_TEXT - len(mark)
    for cut in range(len(wrapper + gutter(1) + HEADER), len(key) - len(FOOTER)):
        pad = "#" * (limit - cut - 1) + "\n"

        shown = insights._outbound(pad + key + "\nand what now?")

        assert shown.startswith(pad) and shown.endswith(mark), (layout, cut)
        left = shown[len(pad) : -len(mark)]
        for line in BODY:
            assert not any(line[at : at + 6] in left for at in range(len(line) - 5)), (layout, cut)
        lead = wrapper + gutter(1)
        assert left.startswith((lead + MARKER, lead + HEADER)), (layout, cut, left)
        tail = (
            left[len(lead + MARKER) :]
            if left.startswith(lead + MARKER)
            else left[len(lead + HEADER) :]
        )
        assert tail in allowed, (layout, cut, left)


#: A quoted credential as each place writes it: (the text up to the value, the value's quote).
QUOTED_FORMS = {
    "json": ('{"password": "', '"'),
    "python": ("{'api_key': '", "'"),
    "json in a string": ('{\\"password\\": \\"', '\\"'),
    "assignment": ('PASSWORD="', '"'),
    "assignment, single": ("export TOKEN='", "'"),
}


@pytest.mark.parametrize("form", sorted(QUOTED_FORMS))
def test_a_quoted_value_the_8000_character_clip_cuts_is_taken_to_the_cut(
    form: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``core.insights`` clips, then scrubs: a value cut before its closing quote has none, and
    neither rule took it, so its start shipped, in 0.9.2 and the PR too (the ruling after reopen
    2). Taken to the clip's ``…``, the notice kept, at every cut that leaves 4 characters of it
    or more (the floor every quoted value has)."""
    monkeypatch.setattr(insights, "redaction_level", lambda: RedactionLevel.standard)
    lead, quote = QUOTED_FORMS[form]
    secret = "sk_live_" + "51HqZ8vR2mXk4Lp9Tn3A"
    key = lead + secret + quote + ", and more" + " after it" * 20  # longer than the notice
    mark = insights._TRUNCATION_MARK
    limit = insights._MAX_TEXT - len(mark)
    for cut in range(len(lead) + 4, len(lead) + len(secret) + 1):
        pad = "#" * (limit - cut - 1) + "\n"

        shown = insights._outbound(pad + key)

        name = lead[: -len(quote)] if form.startswith("assignment") else lead
        assert shown == pad + name + MARKER + mark, (form, cut, shown[-80:])


def test_a_value_the_clip_cut_leaves_its_quote_and_not_a_character_of_it() -> None:
    """The marker takes the value's opening quote and no closing one: what the clip left of
    the value is all gone, its last character too."""
    mark = insights._TRUNCATION_MARK
    for text, shown in (
        ('{"password": "hunter2' + mark, f'{{"password": "{MARKER}' + mark),
        ("{'api_key': 'sk_live_51Hq" + mark, f"{{'api_key': '{MARKER}" + mark),
        ('{\\"password\\": \\"hunter2\\' + mark, f'{{\\"password\\": \\"{MARKER}' + mark),
        ('PASSWORD="hunter2' + mark, f"PASSWORD={MARKER}" + mark),
        # the other quote is a character of the value
        ('{"password": "it' + "'" + "s-hunter2" + mark, f'{{"password": "{MARKER}' + mark),
        ("{'api_key': 'say \"hi\" sk_live" + mark, f"{{'api_key': '{MARKER}" + mark),
        # under the floor every quoted value has, as '{"password": "abc"}' is (r24)
        ('{"password": "hun' + mark, '{"password": "hun' + mark),
    ):
        assert _both(text) == (shown, shown), text


# --- 3. ordinary prompts and code ------------------------------------------------------------

#: What ``redact`` makes of each entry of the corpus that it changes, at ``standard``; every other
#: entry comes back as it was given. Each line names how 0.9.2 differs:
#:   = 0.9.2 makes the same of it (an unquoted name, read as 0.9.2 read it);
#:   C a credential 0.9.2 missed;
#:   O an ordinary value under a quoted name that mentions a token, a secret, a password or a
#:     key, taken with it: the over-match reopen 2's policy accepts (P3), named in the R4 line.
#: A private-key header with no key after it (``grep for headers``) reads as 0.9.2's (H1), and so
#: do a documented field (``max_tokens``, ``token_type``, ``NextToken``), a package's spec and a
#: placeholder (``<YOUR_API_KEY>``, ``</s>``, ``${localEnv:GITHUB_TOKEN}``), and, since the ruling
#: after reopen 2, OpenID Connect's ``token_endpoint``, Hugging Face's ``tokenizer_class`` and
#: Claude Code's ``apiKeyHelper``.
CHANGED = {
    # C: an access token in an OAuth token response
    "oauth token response": (
        f'{{"access_token": "{MARKER}", "token_type": "Bearer", "expires_in": 3599, '
        '"scope": "openid email"}'
    ),
    # =: an unquoted name's value
    "k8s secret manifest": (
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: db-secret\ntype: Opaque\n"
        f"stringData:\n  password: {MARKER}"
    ),
    # C: a registry login (base64 of user:password)
    "docker config.json": f'{{"auths": {{"https://index.docker.io/v1/": {{"auth": "{MARKER}"}}}}}}',
    # =: ``secretsmanager:`` reads as an unquoted name holding ``secret``
    "iam policy": (
        '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
        f'"Action": "secretsmanager:{MARKER}", "Resource": "*"}}]}}'
    ),
    # O: a secret's name and a token's lifetime
    "jwt config": (
        f'{{"jwt": {{"secret_name": "{MARKER}", "token_ttl": "{MARKER}", "algorithm": "RS256"}}}}'
    ),
    # O: a password's hint
    "password policy": f'{{"password_policy": {{"min_length": 12}}, "password_hint": "{MARKER}"}}',
    # C: a webhook's signing secret
    "stripe webhook": f'{{"endpoint_secret": "{MARKER}"}}',
    # O: the shell's working directories, under names that hold ``pwd``
    "env dump": (
        f'{{"PWD": "{MARKER}", "OLDPWD": "{MARKER}", "SHELL": "/bin/zsh", '
        '"TERM": "xterm-256color"}'
    ),
    # O: a header's name
    "api config": (
        '{"auth_token_url": "https://login.example.com/token", "token_type": "Bearer", '
        f'"key_count": "3", "num_keys": "4", "api_key_header": "{MARKER}"}}'
    ),
    "docs env": f"Set `API_KEY` in your environment, e.g. `export API_KEY={MARKER}",  # =
    "env grep": f"$ env | grep -i token\nGITHUB_TOKEN={MARKER}\nNO_TOKENS=1",  # =
    # C: a key's first lines, which 0.9.2 sent whole for want of a footer
    "head of a key": f"$ head -3 ~/.ssh/id_rsa\n{MARKER}\n$ why is this key rejected?",
    "psql url": f'$ psql "postgresql://{MARKER}@db.internal:5432/app"',  # =
    "npmrc": f"Here is my .npmrc: //registry.npmjs.org/:_authToken={MARKER}",  # =
    # C: values under credential names; a fixture's are indistinguishable from a real one's
    "python fixture": f'FAKE = {{"api_key": "{MARKER}", "token": "{MARKER}"}}',
    "json pair": f'{{"key": "value", "secret": "{MARKER}"}}',  # C
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
    # C, reopen 1 (F1): names written as one word, and Secrets Manager's binary secret
    "psql env": f'subprocess.run(["psql", url], env={{**os.environ, "PGPASSWORD": "{MARKER}"}})',
    "launch.json env": f'{{"env": {{"PGPASSWORD": "{MARKER}", "PGUSER": "postgres"}}}}',
    "npm token by name": f'{{"mytoken": "{MARKER}", "hftoken": "{MARKER}"}}',
    "secrets manager binary": (
        # =: ``secretsmanager:`` reads as an unquoted name, as in "iam policy"
        f'{{"ARN": "arn:aws:secretsmanager:{MARKER}", '
        f'"SecretBinary": "{MARKER}", "VersionStages": ["AWSCURRENT"]}}'
    ),
    # reopen 2 (G4), the tester's prompts
    # O: an app's labels
    "i18n en.json": (
        f'{{"login.title": "Sign in", "login.password": "{MARKER}", '
        f'"login.forgot_password": "{MARKER}", "settings.api_key": "{MARKER}", '
        f'"settings.token": "{MARKER}"}}'
    ),
    # =: the token, taken as 0.9.2 took it; apiKeyHelper, a script's path, is on the list
    "claude settings.json": (
        f'{{"apiKeyHelper": "~/bin/get-key.sh", "env": {{"ANTHROPIC_AUTH_TOKEN": "{MARKER}"}}, '
        '"permissions": {"allow": ["Bash(npm run test:*)"]}}'
    ),
    # O: a script's command
    "package.json scripts": (
        f'{{"scripts": {{"build": "tsc -p .", "gen-token": "{MARKER}", '
        f'"rotate-secret": "{MARKER}"}}}}'
    ),
    # =: an unquoted name inside a connection string
    "appsettings.json": (
        '{"ConnectionStrings": {"Default": "Server=.;Database=app;User Id=sa;'
        f'Password={MARKER}"}}, "Jwt": {{"Key": "dev-only-signing-key-0123456789", '
        '"Issuer": "app"}}'
    ),
}


def test_the_corpus_names_only_entries_it_has() -> None:
    assert set(CHANGED) <= {label for label, _ in CORPUS}


@pytest.mark.parametrize(("label", "text"), CORPUS, ids=[label for label, _ in CORPUS])
def test_an_ordinary_prompt_loses_its_credentials_and_nothing_else(label: str, text: str) -> None:
    """JSON configs, API docs, shell sessions, stack traces and code that names its tokens, keys
    and secrets: what is not a credential comes back as it was typed."""
    shown = CHANGED.get(label, text)

    assert redact(text, RedactionLevel.standard) == shown
    for secret in (GHP, GOOGLE_ACCESS, NPM, WHSEC, ANTHROPIC):
        assert secret not in redact(text, RedactionLevel.standard)
