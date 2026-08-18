"""`/health` identifies a PAYLOAD, never a process — and §3 verifies with it.

@8dd460fb established this on the doctor row: walking the runbook with §3
deliberately skipped, the `explainability proxy` line still read healthy because
another process on the box was serving 9190. §5 carries that warning and tells
the reader to run `ss -ltnp`.

THE WARNING IS IN THE WRONG SECTION, AND IN ONLY ONE OF THE THREE PLACES THE
CHECK APPEARS. §3 is where the operator STARTS the proxy and where its `/health`
is verified for the first time — `service=aisquare-proxy`, `mode=claude_code` —
and §3 said nothing about whose process answered. Neither did the at-a-glance
table, which is what a human reads under time pressure. So the caveat existed
for the row that is a consequence and not for the step that is the cause.

THE COUNTEREXAMPLE IS IN THIS REPO AND IS THE TEST BELOW. `tests/proxy_stub` is
forty lines of `http.server` written to exercise the launcher. Measured: it
satisfies §3's verification completely and `probe_proxy` calls it healthy. If a
test fixture passes the check, so does a proxy left running by yesterday's
cutover, and so does the wrong mode on the right port started by someone else.

THIS IS NOT A DEFECT IN `probe_proxy`. A payload check is the correct check for
"is the thing on this port the interface I expect" — the alternative is asking
the kernel who owns the socket, which is `ss -ltnp`'s job and not a CLI
diagnostic's. What was missing was the sentence saying so, at the step where it
matters. If that ever changes — if the CLI learns to verify the process — the
first assertion here fails, and that is the signal to go delete the caveat from
the runbook rather than leave it as folklore.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.services.explainability import probe_proxy
from tests.proxy_stub import healthy_proxy

RUNBOOK = Path(__file__).resolve().parent.parent / "docs/runbooks/explainability-prod-cutover.md"


def test_a_forty_line_fixture_passes_the_check_section_3_verifies_with() -> None:
    """The uncomfortable one. It should stay uncomfortable."""
    with healthy_proxy() as proxy_url:
        probe = probe_proxy(proxy_url)

    assert probe.healthy, probe.reason
    assert "healthy" in probe.reason


def test_the_check_rejects_a_process_that_answers_with_the_wrong_contract() -> None:
    """The control: the payload check is not a rubber stamp either.

    Without this, "a fixture passes" could be read as "anything passes", which
    would make the caveat sound like an argument for distrusting the row rather
    than for confirming the PID alongside it.
    """
    with healthy_proxy({"status": "ok", "service": "something-else", "mode": "claude_code"}) as url:
        wrong_service = probe_proxy(url)
    with healthy_proxy({"status": "ok", "service": "aisquare-proxy", "mode": "passthrough"}) as url:
        wrong_mode = probe_proxy(url)

    assert not wrong_service.healthy
    assert not wrong_mode.healthy


def test_section_3_says_a_healthy_answer_is_not_proof_of_ownership() -> None:
    """The sentence, pinned where the operator starts the proxy.

    Keyed on `ss -ltnp` because that is the actionable half — a caveat that
    warns without naming the command leaves the reader with a doubt and no way
    to resolve it at 08:00.
    """
    section = _section("## 3. Start the proxy")

    assert "ss -ltnp" in section, "§3 verifies /health without saying how to confirm whose it is"


def test_the_at_a_glance_table_carries_the_same_caveat() -> None:
    """The summary is what gets read under time pressure.

    §5 had the warning and the table did not, which is the shape this file
    exists for: a caveat parked in the section that discusses the consequence,
    absent from the one-line summary of the step that causes it.
    """
    row = next(
        line
        for line in RUNBOOK.read_text(encoding="utf-8").splitlines()
        if line.startswith("| 3 Proxy |")
    )

    assert "ss -ltnp" in row, f"the at-a-glance row still verifies by payload alone: {row}"


def _section(heading: str) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]
