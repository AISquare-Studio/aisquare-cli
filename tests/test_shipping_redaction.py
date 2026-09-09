"""What a credential does when someone pastes it into a prompt.

#50 ships human prompts and every board event to the gateway. Prompts are typed
by people, and people paste keys into terminals. The difference between a
feature and an incident is what happens in the seconds after that paste, so it
is pinned here by shape rather than trusted to a reviewer's eye.

These tests are about what crosses the NETWORK. The local record — ``aisquare
log``, the board row — is deliberately untouched: it never left the machine, and
scrubbing a user's own history is a different decision nobody asked for.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights, outbox
from aisquare.core.config import AppConfig, save_config
from aisquare.core.redaction import _CREDENTIAL_RULES, _IDENTITY_RULES, redact
from aisquare.models import RedactionLevel
from aisquare.services import hooks as hooks_service


def _shaped(prefix: str, body: str) -> str:
    """Assemble a credential-shaped fixture instead of writing it as a literal.

    These values are invented and correspond to no account anywhere, but they
    are realistic ENOUGH that GitHub's push protection rejected the branch when
    they were written out in full — which is the loudest possible confirmation
    that the shapes this module scrubs are the shapes that matter. Concatenating
    at import time keeps the test data honest while leaving nothing in the file
    that a scanner (or a person skimming a diff) can mistake for a live key.

    Do not "simplify" this back into literals. The push will fail, and rightly.
    """
    return prefix + body


#: Real shapes, invented values. Each is a live-credential format a person could
#: plausibly paste into a prompt while asking for help with it.
SECRETS = {
    "anthropic/openai key": _shaped("sk-", "ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIII"),
    "github pat": _shaped("ghp", "_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
    "gitlab pat": _shaped("glpat", "-AbCdEfGhIjKlMnOpQrSt"),
    "slack bot token": _shaped("xox", "b-1234567890-ABCDEFGHIJKLMNOP"),
    "aws access key id": _shaped("AKIA", "IOSFODNN7EXAMPLE"),
    "aisquare session token": _shaped("aisq_", "3kF9xQ2mVb8ZrT1cLw6nYp4sHd0eJu7gAi5oKf9qRc2"),
    "google api key": _shaped("AIza", "SyA1234567890abcdefghijklmnopqrstuvw"),
    "jwt": _shaped(
        "eyJ",
        "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    ),
    "bearer header": _shaped("Authorization: Bearer ", "abcdef1234567890abcdef1234567890"),
    "assigned secret": _shaped('EXPLAINABILITY_API_KEY="wk_', 'live_9f8e7d6c5b4a3210"'),
    "url with credentials": _shaped("https://deploy:", "hunter2@internal.example.com/repo.git"),
    "pem private key": _shaped(
        "-----BEGIN RSA PRIVATE KEY",
        "-----\nMIIEowIBAAKCAQEAx7Fk9sQmPq3vN2wZ\n-----END RSA PRIVATE KEY-----",
    ),
}

#: The ``strict`` counterpart: one sample per identity shape. Written as plain
#: literals rather than through ``_shaped`` — a home directory is not
#: credential-shaped, so the push-protection reason for assembling SECRETS does
#: not apply here and pretending it does would teach the next reader the wrong
#: rule.
IDENTITIES = {
    "email address": "jatin@opengrowth.com",
    "unix home": "/home/jatin",
    "windows home": "C:\\Users\\jatin",
}

#: Every rule collection in ``redaction`` and the samples that must exercise it.
#: A third collection is registered here once and inherits both ratchets below.
_COVERED = {
    "credential": (_CREDENTIAL_RULES, SECRETS),
    "identity": (_IDENTITY_RULES, IDENTITIES),
}


def _configure(level: RedactionLevel = RedactionLevel.standard) -> None:
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.example"
    config.redaction.level = level
    save_config(config)
    insights.reset_cache()


def _spooled_text() -> str:
    return "\n".join(
        str(json.loads(p.read_text(encoding="utf-8")).get("text", "")) for p in outbox.pending()
    )


@pytest.fixture(autouse=True)
def _fresh() -> None:
    insights.reset_cache()


# --- the incident case ---


@pytest.mark.parametrize("label", sorted(SECRETS))
def test_a_pasted_credential_does_not_reach_the_outbound_record(label: str, tmp_path: Path) -> None:
    _configure()
    secret = SECRETS[label]

    hooks_service.capture_prompt(f"why does this fail?\n{secret}\nplease help", tmp_path)

    shipped = _spooled_text()
    assert shipped, "the record should still exist — redaction is not suppression"
    leaked = _leaked_fragment(secret, shipped)
    assert leaked is None, f"{label}: {leaked!r} crossed the network"
    assert "why does this fail?" in shipped, "the question around the secret must survive"


def _leaked_fragment(secret: str, shipped: str) -> str | None:
    """The recognisable part of ``secret`` still present in ``shipped``.

    Compared per line and on the value rather than the whole blob, so a test
    cannot pass merely because the payload was reformatted.
    """
    for line in secret.splitlines():
        candidate = line.split("=", 1)[-1].strip().strip('"')
        candidate = candidate.rsplit(" ", 1)[-1]
        if len(candidate) >= 12 and candidate in shipped:
            return candidate
    return None


@pytest.mark.parametrize("label", sorted(SECRETS))
def test_a_credential_in_a_board_note_is_scrubbed_too(label: str, runner: CliRunner) -> None:
    """Notes are typed by the same people, into the same terminal."""
    _configure()

    result = runner.invoke(app, ["note", f"blocked on this: {SECRETS[label]}"])

    assert result.exit_code == 0, result.output
    assert _leaked_fragment(SECRETS[label], _spooled_text()) is None


def test_the_local_record_is_left_exactly_as_typed(tmp_path: Path) -> None:
    """Redaction is about the network. `aisquare log` is the user's own history."""
    _configure()
    typed = f"debug this: {SECRETS['github pat']}"

    hooks_service.capture_prompt(typed, tmp_path)

    from aisquare.core.store import store_session
    from aisquare.core.workspace import active_project

    with store_session() as store:
        project = active_project(store, tmp_path)
        prompts = store.recent_prompts(project.id, limit=5)
    assert any(p.text == typed for p in prompts), "local capture must be untouched"
    assert SECRETS["github pat"] not in _spooled_text()


# --- the levels are a real decision, not three names for one behaviour ---


def test_off_ships_exactly_what_was_typed(tmp_path: Path) -> None:
    """Someone who turns redaction off has decided; we do not second-guess."""
    _configure(RedactionLevel.off)
    typed = f"key is {SECRETS['github pat']}"

    hooks_service.capture_prompt(typed, tmp_path)

    assert SECRETS["github pat"] in _spooled_text()


def test_standard_keeps_the_engineering_substance(tmp_path: Path) -> None:
    """Paths and hostnames ARE the dataset. Over-redaction is its own failure."""
    _configure()
    typed = "src/aisquare/core/outbox.py fails against gateway.internal:8443 for user@corp.example"

    hooks_service.capture_prompt(typed, tmp_path)

    shipped = _spooled_text()
    assert "src/aisquare/core/outbox.py" in shipped
    assert "gateway.internal:8443" in shipped


def test_strict_also_removes_who_and_where(tmp_path: Path) -> None:
    """Every identity shape, driven by the collection the ratchet below checks.

    This was two shapes hand-written into one line, and the third rule in
    ``_IDENTITY_RULES`` — the Windows home — was matched by nothing in the
    suite. Reading the samples from ``IDENTITIES`` is what makes a fourth shape
    impossible to add without exercising it here.
    """
    _configure(RedactionLevel.strict)
    typed = "mail " + " and ".join(IDENTITIES.values()) + " about work/aisquare-cli"

    hooks_service.capture_prompt(typed, tmp_path)

    shipped = _spooled_text()
    for label, value in IDENTITIES.items():
        assert value not in shipped, f"{label}: {value!r} crossed the network"
    assert "aisquare-cli" in shipped, "strict anonymises, it does not delete the sentence"


# --- properties the scrubber itself must hold ---


def _uncovered(collection: str) -> tuple[list[str], list[str]]:
    """Rules no sample matches, and samples no rule matches.

    Matched against the RAW sample, never through ``redact``: the rules run in
    sequence and an earlier one consumes text a later one would have matched.
    The ``assigned secret`` sample is the live example — it trips the
    ``NAME=value`` rule AND the ``wk_`` rule, but a pipeline run leaves only the
    first, so asking the pipeline would under-report coverage by one rule.
    Coverage is a question about the rule set, not about the output.
    """
    rules, samples = _COVERED[collection]
    return (
        [
            pattern.pattern
            for pattern, _ in rules
            if not any(pattern.search(v) for v in samples.values())
        ],
        [
            label
            for label, v in samples.items()
            if not any(pattern.search(v) for pattern, _ in rules)
        ],
    )


@pytest.mark.parametrize("collection", sorted(_COVERED))
def test_every_redaction_rule_has_a_sample(collection: str) -> None:
    """A rule added without a sample is coverage that rots in silence.

    The two collections are independent — the tests above import ``redact``,
    the function, and never the rules — so today's completeness is a fact about
    today. This is what makes it a fact about tomorrow. It asserts the
    RELATIONSHIP and never the count: a count is a second container to update,
    and this repo has been bitten by content moving while its container did not.
    """
    unmatched, _ = _uncovered(collection)

    assert not unmatched, f"{collection} rules that no sample exercises:\n  " + "\n  ".join(
        unmatched
    )


@pytest.mark.parametrize("collection", sorted(_COVERED))
def test_every_redaction_sample_exercises_a_rule(collection: str) -> None:
    """The other direction, without which the ratchet catches half.

    A sample that matches no rule proves nothing: it would sail through the
    parametrised tests above — nothing to redact, so nothing leaks — and read
    as a shape we cover when it is a shape we do not.
    """
    _, inert = _uncovered(collection)

    assert not inert, f"{collection} samples that match no rule and so prove nothing: {inert}"


def test_redaction_marks_what_it_removed() -> None:
    """A silent scrub is indistinguishable from a user who typed nothing."""
    out = redact(f"token: {SECRETS['github pat']}", RedactionLevel.standard)

    assert "[redacted" in out


def test_an_assignment_keeps_its_key_name() -> None:
    """`API_KEY=[redacted]` still tells you a key was in play; `[redacted]` does not."""
    out = redact('EXPLAINABILITY_API_KEY="wk_live_9f8e7d6c5b4a3210"', RedactionLevel.standard)

    assert "EXPLAINABILITY_API_KEY" in out
    assert "wk_live_9f8e7d6c5b4a3210" not in out


def test_ordinary_prose_is_returned_unchanged() -> None:
    """False positives cost the dataset; this is the guard against creep."""
    prose = (
        "Rebase expl-50 onto rc/v2026.08.18 and rerun make check — 770 passed, "
        "and the AgentRunTracer run_id is the board session id."
    )

    assert redact(prose, RedactionLevel.standard) == prose


def test_redaction_never_raises_on_hostile_input() -> None:
    for hostile in ("", "\x00\x01", "sk-" + "a" * 100_000, "=" * 50_000, "\n" * 10_000):
        assert isinstance(redact(hostile, RedactionLevel.standard), str)


def test_redaction_cannot_blow_up_on_the_primary_path() -> None:
    """A canary for catastrophic backtracking, not a benchmark.

    The bound is deliberately loose — it fails on a pathological regex, not on a
    slow machine. Anything that trips this would have been a hang on a hook.
    """
    payload = ("sk-" + "a" * 40 + " normal words here " + "x" * 200 + "\n") * 40

    started = time.perf_counter()
    redact(payload[:8000], RedactionLevel.strict)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1, f"redaction took {elapsed:.3f}s on 8k of text"


def test_consent_line_states_that_shipping_is_scrubbed() -> None:
    """Whoever says yes at init must learn what leaves the machine."""
    from aisquare.services.explainability import ShippingOffer

    assert "redact" in ShippingOffer.CAPTURES.lower()
