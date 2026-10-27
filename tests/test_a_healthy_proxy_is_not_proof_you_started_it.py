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

The runbook and the pinned sentences in its §0, §3 and at-a-glance table were removed
with `docs/` on 2026-10-26. What stays is the counterexample and its control: the CLI
checks a payload, and a fixture that serves the payload passes it.
"""

from __future__ import annotations

from aisquare.services.explainability import probe_proxy
from tests.proxy_stub import healthy_proxy


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
