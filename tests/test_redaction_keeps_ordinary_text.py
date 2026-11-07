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
        ("x-auth", "patch:x-auth@1.0.0#./x.patch"),
        ("x-auth", "portal:../x-auth"),
        # reopen 2 (MT2): a range whose second part is x
        ("next-auth", "^4.x"),
        ("@octokit/auth-token", "2.x || 3.x"),
        # reopen 3 (N4): each shape whole
        ("y-auth", "npm:z-auth@^1"),
        ("x-auth", "1.0.0 - 2.0.0"),
        ("x-auth", "1.2.3-rc.1"),
        ("x-auth", "18.3.0-canary-6bc0e3f0-20240101"),
        ("x-auth", "workspace:^1.2.3"),
        ("x-auth", "link:./packages/x-auth"),
        ("x-auth", "file:../x-auth"),
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
        # reopen 3 (N4): a spec's start with a secret after it, and a secret shaped as a version
        "1.2" + SECRET,
        "^1.x" + SECRET,
        "github:" + SECRET,
        "npm:" + SECRET,
        "file:" + SECRET,
        "git+x:" + SECRET,
        "https://h.example/" + SECRET + "#",
        "nightly" + SECRET,
        "9.8n+P7r@-abcdef",
        "3.14159PiDay!hunter",
        "v2.1-Zq8vR2mXk4Lp9Tn3Ab1Cd2Ef",
        "12345678",
        '^1.2.3\\" ' + SECRET,
        # reopen 4 (K2): a token written as a spec URL's user, and a #ref URL with a query
        "git+https://" + SECRET + "@dev.azure.com/org/p/_git/node-jsonwebtoken#v9",
        "git+https://ATBB" + SECRET + "@bitbucket.org/me/api-key-auth.git#v1",
        "https://" + SECRET + "@pkgs.dev.azure.com/o/_packaging/f/npm/registry/t/-/t-1.0.0.tgz",
        "https://" + SECRET + "@github.com/me/repo#main",
        "git+ssh://" + SECRET + "@github.com/me/repo.git",
        "https://user:" + SECRET + "@h.example.com/x-1.0.0.tgz",
        "https://h.example.com/hook?sig=" + SECRET + "#main",
        "git://" + SECRET,
        # reopen 5 (S2): no URL is a spec under a credential's name, a token can sit in its path
        "https://npm-proxy.fury.io/" + SECRET + "/acct/token-lib/-/token-lib-1.0.0.tgz",
        "git+https://github.com/org/my-token.git",
        "git://github.com/org/my-token.git",
        "git+ssh://git@github.com/o/x-token.git#v1",
        "https://registry.example.com/x-auth/-/x-auth-1.0.0.tgz",
        "https://github.com/org/x-auth#v1.2.0",
    ):
        text = f'{{"auth": "{value}"}}'
        shown = redact(text, RedactionLevel.standard)
        assert shown == f'{{"auth": "{MARKER}"}}', value


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
        "${{ github.token }}",
        "${env:API_KEY}",
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
        # a reference with a literal default or a filter's arguments holds a value (N1)
        "${localEnv:PGPASS:" + SECRET + "}",
        "${PGPASS:-" + SECRET + "}",
        "${STRIPE_KEY:-sk_live_" + SECRET + "}",
        "{{ db_pw | default(" + SECRET + ") }}",
        "${VAR=" + SECRET + "}",
        "${VAR-" + SECRET + "}",
        "${VAR:=" + SECRET + "}",
    ],
)
def test_a_value_that_only_starts_or_ends_like_a_placeholder_is_taken(value: str) -> None:
    """The placeholder is the WHOLE value, to its closing quote."""
    for text, shown in zip(_forms("api_key", value), _forms("api_key", MARKER), strict=True):
        assert _both(text) == (shown, shown)


@pytest.mark.parametrize("start", ["<a>", "${x}", "true", "null"])
@pytest.mark.parametrize("after", [" ", ", ", ""])
def test_a_whole_value_shape_then_an_escaped_quote_is_not_the_whole_value(
    start: str, after: str
) -> None:
    """N3: the shape's closing quote has as many backslashes as its opening quote, so a
    ``\\"`` after it is a quote the value holds, and the value goes on past it."""
    for text, shown in (
        (f'{{"api_key": "{start}\\"{after}{SECRET}"}}', f'{{"api_key": "{MARKER}"}}'),
        (
            f'{{\\"api_key\\": \\"{start}\\\\\\"{after}{SECRET}\\"}}',
            f'{{\\"api_key\\": \\"{MARKER}\\"}}',
        ),
    ):
        assert _both(text) == (shown, shown), text


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
    upper = "<P>" + "<BR>".join(lines) + "</P><P>why?</P>"
    mixed = "<p>" + "<Br/>".join(lines) + "</p><p>why?</p>"
    read = '{"content": "' + _listed(GUTTERS["Read tool"], lines, "\\n") + '", "is_error": false}'

    assert _both(html) == (f"<p>{MARKER}</p><p>why?</p>",) * 2
    assert _both(upper) == (f"<P>{MARKER}</P><P>why?</P>",) * 2
    assert _both(mixed) == (f"<p>{MARKER}</p><p>why?</p>",) * 2
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


def test_a_sentence_after_a_keys_lines_is_kept_whatever_its_first_word() -> None:
    """A word that reads as base64 (``OpenSSL``) ends a key only with no word after it."""
    text = f"{HEADER}\n{BODY[0]}\nOpenSSL says bad decrypt"

    assert _both(text) == (f"{MARKER}\nOpenSSL says bad decrypt",) * 2


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


#: A key's lines as other tools print them (N2): a prefix that changes on every line, a suffix,
#: or both; (the line ``n`` of the listing, the header line 1, as it prints ``line``, joint).
WRAPS: dict[str, tuple[Callable[[int, str], str], str]] = {
    "cat -A, sed -n l": (lambda n, line: f"{line}$", "\n"),
    "cat -A of CRLF": (lambda n, line: f"{line}^M$", "\n"),
    "git blame": (
        lambda n, line: f"^a1b2c3d (Anmol Majithia 2026-11-06 10:00:00 +0000 {n:2}) {line}",
        "\n",
    ),
    "journalctl": (lambda n, line: f"Nov 06 10:00:{n:02d} host app[4242]: {line}", "\n"),
    "a table": (lambda n, line: f"| {n} | {line} |", "\n"),
    "a YAML list": (lambda n, line: f"  - {line}", "\n"),
    "a JSON array": (lambda n, line: f'"{line}"', ", "),
    "a Python list": (lambda n, line: repr(line), ",\n "),
}


def _wrapped(layout: str, lines: list[str]) -> str:
    wrap, joint = WRAPS[layout]
    return joint.join(wrap(n, line) for n, line in enumerate(lines, start=1))


@pytest.mark.parametrize("layout", sorted(WRAPS))
def test_a_keys_lines_whatever_wraps_them_are_its_body(layout: str) -> None:
    """N2 (the ruling after reopen 2): ``cat -A`` ends each line in ``$``, ``git blame`` and
    ``journalctl`` put a prefix that changes on every line, a table pipes: a body read as lines
    of base64 after a gap stopped at the first, and shipped the rest. Each line with a run that
    reads as a key's goes with the header, its last short line too, and the line after the key
    stays."""
    text = "[" * (layout == "a Python list") + _wrapped(layout, [HEADER, *BODY])
    text += "]" * (layout == "a Python list") + "\nsee the error below"

    shown, strict = _both(text)

    assert shown == strict and MARKER in shown and shown.endswith("\nsee the error below")
    for line in BODY:
        assert not any(line[at : at + 6] in shown for at in range(len(line) - 5)), (layout, shown)


#: What may join a key's lines that no reader here knows as a line break (reopen 4, K1).
SEPARATORS = {
    "&#10;": "&#10;",
    "&#xA;": "&#xA;",
    "&#x0A;": "&#x0A;",
    "&#13;&#10;": "&#13;&#10;",
    "&#xD;&#xA;": "&#xD;&#xA;",
    "</div><div>": "</div><div>",
    "</p><p>": "</p><p>",
    "U+2028": "\u2028",
    "U+0085": "\u0085",
    "form feed": "\x0c",
    "vertical tab": "\x0b",
    "backslash u000a": "\\u000a",
    "backslash 012": "\\012",
    "%0A": "%0A",
    # at the bound: 20 and 24 characters of what is not markup (reopen 5, MV1)
    "20 hyphens": "-" * 20,
    "24 hyphens": "-" * 24,
    # an ellipsis a reader printed between two runs is no clip (reopen 5, MV2)
    "an ellipsis": "&#10;\u2026&#10;",
}


def _separated(separator: str, lines: list[str], in_an_attribute: bool) -> tuple[str, str]:
    """A key whose lines ``separator`` joins, bare or as an XML attribute's value, and what is
    left of the attribute's text with the key gone ("" for the bare one)."""
    key = separator.join(lines)
    if in_an_attribute:
        return (
            f'<add key="SigningKey" value="{key}" />\nwhy?',
            f'<add key="SigningKey" value="{MARKER}" />\nwhy?',
        )
    return f"{key}{separator}why is this key rejected?", ""


@pytest.mark.parametrize("in_an_attribute", [False, True], ids=["bare", "in an attribute"])
@pytest.mark.parametrize("separator", sorted(SEPARATORS))
def test_a_keys_lines_joined_by_a_separator_no_reader_knows_are_its_body(
    separator: str, in_an_attribute: bool
) -> None:
    """K1: an XML writer stores a newline in an attribute as ``&#10;``, a rich-text editor
    joins lines with ``</div><div>``, Word with a vertical tab. No reader here knows those as
    line breaks, and the body stopped at the first: the key shipped. A run that reads as a key's
    with only a short text before it is the key's, whatever that text is."""
    text, shown = _separated(SEPARATORS[separator], [HEADER, *BODY], in_an_attribute)

    redacted, strict = _both(text)

    assert redacted == strict and MARKER in redacted
    if shown:
        assert redacted == shown
    for line in BODY:
        assert not any(line[at : at + 6] in redacted for at in range(len(line) - 5)), redacted


@pytest.mark.parametrize("in_an_attribute", [False, True], ids=["bare", "in an attribute"])
@pytest.mark.parametrize("separator", sorted(SEPARATORS))
def test_a_separated_key_the_8000_character_clip_cuts_leaves_no_byte_of_its_body(
    separator: str, in_an_attribute: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each K1 separator, bare and in an attribute, cut by the clip at every point from the
    header to the footer: no four characters of a body line are left."""
    monkeypatch.setattr(insights, "redaction_level", lambda: RedactionLevel.standard)
    text, _ = _separated(SEPARATORS[separator], [HEADER, *BODY, FOOTER], in_an_attribute)
    mark = insights._TRUNCATION_MARK
    limit = insights._MAX_TEXT - len(mark)
    for cut in range(text.index(HEADER) + len(HEADER), text.index(FOOTER)):
        pad = "#" * (limit - cut - 1) + "\n"

        shown = insights._outbound(pad + text + "\n" + "and what now? " * 4)

        assert shown.startswith(pad) and shown.endswith(mark), (separator, cut)
        left = shown[len(pad) : -len(mark)]
        for line in BODY:
            assert not any(line[at : at + 4] in left for at in range(len(line) - 3)), (
                separator,
                cut,
                left[-120:],
            )


#: What joins a key's lines where it was copied out of an editor, a page or a chat as HTML or
#: XML: markup longer than any separator, which does not count against the gap (reopen 5, S1).
MARKUPS = {
    "VS Code's HTML": '</span></div><div><span style="color: #cccccc;">',
    "a highlighted code block": '</span><span class="line">',
    # a tag of 129 to 256 characters: Word's HTML, a styled span on every line (reopen 6, MZ2)
    "Word's HTML": (
        "</span></p><p class=MsoNormal><span style='font-size:10.0pt;line-height:107%;"
        'font-family:"Courier New";mso-fareast-font-family:"Times New Roman";color:#1F3864;'
        "mso-themecolor:accent1;mso-themeshade:128;mso-ansi-language:EN-US'>"
    ),
    "Google Docs' HTML": '</span></p><p dir="ltr"><span style="font-size:11pt;">',
    "Slack's export": '</div><div class="p-rich_text_section">',
    "a table row with a line number": '</td></tr><tr><td class="num">2</td><td class="code">',
    "a pretty-printed XML attribute": "&#xD;&#xA;" + " " * 16,
    "&#10; and 24 spaces": "&#10;" + " " * 24,
    "GitHub's blob rows": (
        '</td>\n      </tr>\n      <tr>\n        <td id="L2" class="blob-num js-line-number'
        ' js-code-nav-line-number" data-line-number="2"></td>\n        <td id="LC2"'
        ' class="blob-code blob-code-inner js-file-line">'
    ),
}


@pytest.mark.parametrize("markup", sorted(MARKUPS))
def test_a_keys_lines_joined_by_markup_of_any_length_are_its_body(markup: str) -> None:
    """S1: a separator was bounded by its length, and HTML with attributes is longer: VS Code's
    copy as HTML, Google Docs', Slack's, a highlighted block's, a table's, GitHub's. Tags,
    character references and whitespace do not count against the gap, and the prose after the
    key, outside any tag, is kept."""
    text = MARKUPS[markup].join([HEADER, *BODY]) + MARKUPS[markup] + "why is this rejected?"

    shown, strict = _both(text)

    assert shown == strict and MARKER in shown and shown.endswith("why is this rejected?")
    for line in BODY:
        assert not any(line[at : at + 6] in shown for at in range(len(line) - 5)), shown


@pytest.mark.parametrize("markup", sorted(MARKUPS))
def test_a_key_joined_by_markup_the_8000_character_clip_cuts_leaves_no_byte_of_its_body(
    markup: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each S1 layout cut by the clip at every point from the header to the footer: no four
    characters of a body line are left."""
    monkeypatch.setattr(insights, "redaction_level", lambda: RedactionLevel.standard)
    text = MARKUPS[markup].join([HEADER, *BODY, FOOTER])
    mark = insights._TRUNCATION_MARK
    limit = insights._MAX_TEXT - len(mark)
    for cut in range(len(HEADER), text.index(FOOTER)):
        pad = "#" * (limit - cut - 1) + "\n"

        shown = insights._outbound(pad + text + "\n" + "and what now? " * 4)

        assert shown.startswith(pad) and shown.endswith(mark), (markup, cut)
        left = shown[len(pad) : -len(mark)]
        for line in BODY:
            assert not any(line[at : at + 4] in left for at in range(len(line) - 3)), (
                markup,
                cut,
                left[-120:],
            )


def test_a_last_line_of_one_case_goes_with_the_key_before_the_clips_notice() -> None:
    """A key's short last line can be letters of one case (``ovcxxr``), which do not read as
    base64, and before ``</span></div>`` and the clip's notice it shipped (the tester's clip
    fuzz, seed 61, at reopen 6). Before the notice nothing but the key stands there, so it goes;
    before prose a word of one case is still kept."""
    markup = MARKUPS["VS Code's HTML"]
    mark = insights._TRUNCATION_MARK
    cut = markup.join([HEADER, BODY[0], "ovcxxrab"]) + "</span></div>" + mark
    prose = markup.join([HEADER, BODY[0], "thanks"]) + "</span></div>\nwhy is this rejected?"

    assert "ovcxxrab" not in redact(cut, RedactionLevel.standard)
    assert redact(cut, RedactionLevel.standard).endswith("</span></div>" + mark)
    assert "thanks</span></div>\nwhy is this rejected?" in redact(prose, RedactionLevel.standard)


def test_markup_past_the_cap_ends_the_key() -> None:
    """A gap of markup alone is capped (about 512 characters): past it the key has ended, and
    what comes after, a run or not, is the text's."""
    far = "<span></span>" * 50  # 650 characters of markup that is no line break
    text = f"{HEADER}\n{BODY[0]}{far}{BODY[1]}"

    shown, _ = _both(text)

    assert shown == f"{MARKER}{far}{BODY[1]}"


def test_a_keys_lines_that_journalctl_cut_with_an_ellipsis_are_its_body() -> None:
    """``journalctl`` cuts a line too long for the terminal with ``…``, and the key's next
    line comes after it: an ellipsis is no clip, and the lines after it are still the key's."""
    lines = [HEADER, *BODY]
    text = "\n".join(
        f"Nov 06 10:00:{n:02d} host app[4242]: {line[:40]}\u2026" for n, line in enumerate(lines)
    )

    assert _both(text + "\nwhy?") == (f"Nov 06 10:00:00 host app[4242]: {MARKER}\u2026\nwhy?",) * 2
    # and an ellipsis in the prefix, before the run: a unit name cut short
    text = "\n".join(f"Nov 06 10:00:{n:02d} host app\u2026: {line}" for n, line in enumerate(lines))
    assert _both(text + "\nwhy?") == (f"Nov 06 10:00:00 host app\u2026: {MARKER}\nwhy?",) * 2


@pytest.mark.parametrize(
    "text",
    [
        "^a1b2c3d (A 2026-11-06 10:00:00 +0000  5) " + HEADER + " is how it starts\n"
        "^a1b2c3d (A 2026-11-06 10:00:00 +0000  6) and the body follows below",
        f"| 1 | {HEADER} |\n| 2 | the first line of a key, above |",
        f"{HEADER}$\nis what cat -A printed for the header line$",
        # K1: what a separator joins after a header, when it is prose
        f"<p>{HEADER}</p><p>is the first line of a key</p>",
        f"{HEADER}&#10;is the first line of a key",
        f"The header {HEADER} is followed by Proc-Type in old keys",
    ],
)
def test_a_wrapped_header_with_no_body_after_it_is_kept(text: str) -> None:
    """H1 where a tool wraps the lines: prose after the header, a word ending each line."""
    assert _both(text) == (text, text)


@pytest.mark.parametrize("layout", sorted(WRAPS))
def test_a_wrapped_key_the_8000_character_clip_cuts_leaves_no_byte_of_its_body(
    layout: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each N2 layout cut by the clip at every point from the header to the footer: no four
    characters of a body line are left, before the marker or after it."""
    monkeypatch.setattr(insights, "redaction_level", lambda: RedactionLevel.standard)
    lead, close = ("[", "]") if layout == "a Python list" else ("", "")
    key = lead + _wrapped(layout, [HEADER, *BODY, FOOTER]) + close
    mark = insights._TRUNCATION_MARK
    limit = insights._MAX_TEXT - len(mark)
    for cut in range(key.index(HEADER) + len(HEADER), key.index(FOOTER)):
        pad = "#" * (limit - cut - 1) + "\n"

        shown = insights._outbound(pad + key + "\nand what now?")

        assert shown.startswith(pad) and shown.endswith(mark), (layout, cut)
        left = shown[len(pad) : -len(mark)]
        for line in BODY:
            assert not any(line[at : at + 4] in left for at in range(len(line) - 3)), (
                layout,
                cut,
                left[-120:],
            )


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


#: How each form writes a quote its value holds: one level of escape in from its own quote.
HELD_QUOTE = {
    "json": '\\"',
    "python": "\\'",
    "json in a string": '\\\\\\"',
    "assignment": '\\"',
    "assignment, single": "\\'",
}


@pytest.mark.parametrize("held", [False, True], ids=["plain", "holding a quote"])
@pytest.mark.parametrize("form", sorted(QUOTED_FORMS))
def test_a_quoted_value_the_8000_character_clip_cuts_is_taken_to_the_cut(
    form: str, held: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``core.insights`` clips, then scrubs: a value cut before its closing quote has none, and
    neither rule took it, so its start shipped, in 0.9.2 and the PR too (the ruling after reopen
    2). Taken to the clip's ``…``, the notice kept, at every cut that leaves 4 characters of it
    or more (the floor every quoted value has). A value that holds a quote, escaped as its form
    escapes one, is cut as well: the forms that close at the first quote closed it there, and
    what the clip left after the quote shipped (reopen 3, D1)."""
    monkeypatch.setattr(insights, "redaction_level", lambda: RedactionLevel.standard)
    lead, quote = QUOTED_FORMS[form]
    secret = "sk_live_" + ("51Hq" + HELD_QUOTE[form] if held else "") + "51HqZ8vR2mXk4Lp9Tn3A"
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
        # a closed value's ellipsis is no clip: the value is taken whole, to its quote
        ('{"password": "wait\u2026 what"}', f'{{"password": "{MARKER}"}}'),
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
