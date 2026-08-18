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
    assert "etime" in section, (
        "§3 names the socket but not its AGE. 'the PID should be the one you "
        "started' is unusable advice to someone who has not started it yet and "
        "has no PID to compare against; ELAPSED needs no prior knowledge."
    )


def test_section_3_warns_that_a_clash_reports_startup_complete_first() -> None:
    """Measured: uvicorn prints three reassuring lines before the bind error.

    `Application startup complete` refers to the ASGI app, not the socket, and
    it precedes `[Errno 98] … address already in use`. Exit code 1, no
    traceback — a clean failure that reads like a success if you stop at the
    third line. That matters more in this section than it would elsewhere,
    because a proxy is already holding 9190 on this box, so a clash is the
    expected case and every later check passes either way.
    """
    section = _section("## 3. Start the proxy")

    # Both the WARNING and the EVIDENCE, scoped separately. A section-wide
    # search for the phrase is satisfied by the headline alone — measured:
    # eliding the line from the transcript left this green, because the
    # sentence above quotes it too. Second time in one cycle that a string
    # appearing twice made an assertion untestable; the fix is the same one.
    transcript = section[section.index("INFO:     Started server process") :]
    transcript = transcript[: transcript.index("```")]

    assert "Errno 98" in transcript, "§3 does not show what an occupied port looks like"
    assert "Application startup complete" in transcript, (
        "§3's transcript no longer shows the success line that precedes the "
        f"bind error; that line is the whole hazard. Transcript:\n{transcript}"
    )
    assert "Application startup complete" in section[: section.index("```text")], (
        "§3 shows the misleading line but never warns about it in prose"
    )


def test_section_3_does_not_gate_the_build_check_on_sudo() -> None:
    """The gate defeated the check it guarded, so it must not come back.

    `sudo -n true && EXE=$(readlink -f /proc/$PID/exe) || EXE=python` takes the
    fallback on any box without passwordless sudo — and the fallback is exactly
    "whatever you last installed", which the block's own comment says it exists
    not to answer for. `/proc/<pid>/exe` is readable without privilege for your
    own processes, and the operator starts this proxy themselves.

    Keyed on the absence of the sudo gate rather than on the presence of the
    replacement, because there is more than one correct way to read that
    symlink and only one wrong way to decide whether to try.
    """
    section = _section("## 3. Start the proxy")
    start = section.index("```bash")
    end = section.index("```", section.index("PID=$("))

    assert "sudo -n true" not in section[start:end], (
        "§3 gates the interpreter choice on sudo again; on a box without "
        "passwordless sudo that silently answers for the wrong Python"
    )


def test_section_3_documents_the_outcome_the_fallback_produces() -> None:
    """Two documented outcomes, three real ones.

    `IN FORCE` and `MISSING` were the only results the block named. Measured on
    this box, the fallback produces neither — it raises ModuleNotFoundError,
    and an operator on the step that decides whether extra Runs pollute the
    dataset got a traceback the document did not mention.
    """
    section = _section("## 3. Start the proxy")

    # Scoped to the comment block beside the command, NOT the whole section:
    # the prose caveat below also says "ModuleNotFoundError", so a section-wide
    # search is satisfied by text the operator reads AFTER the command has
    # already surprised them. Measured — deleting the comment line left a
    # section-wide assertion passing, which is the shadowed-pattern failure
    # @8dd460fb hit on the guard's own reason list.
    outcomes = section[
        section.index("# IN FORCE") : section.index("```", section.index("# IN FORCE"))
    ]

    assert "ModuleNotFoundError" in outcomes, (
        "§3 lists IN FORCE and MISSING beside the command but not the error its "
        f"own fallback raises; the block reads:\n{outcomes}"
    )


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
    assert "etime" in row, (
        "the summary names the socket but not its age, so it gives advice the "
        f"body has already been corrected away from: {row}"
    )


def test_the_preflight_row_does_not_rest_on_provenance_alone() -> None:
    """Row 0 lagged behind the §0 body fix by a whole cycle.

    @8dd460fb corrected §0 because `git fetch` is not `git checkout` — "a
    comparison with one side". The body grew `--ff-only` and `git status
    --short`; the at-a-glance row kept naming `doctor` provenance as the whole
    verification, and provenance is built from `direct_url.json`: an install
    PATH and an editable flag, with no branch and no sha in it. A tree sitting
    on main prints the identical row.

    So the summary a human reads under time pressure still carried the check
    the body had just been fixed for. Keyed on the head comparison, which is
    the half that was missing.
    """
    row = next(
        line
        for line in RUNBOOK.read_text(encoding="utf-8").splitlines()
        if line.startswith("| 0 Preflight |")
    )

    assert "origin" in row, f"row 0 verifies §0 without comparing your head to origin's: {row}"


def _section(heading: str) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]
