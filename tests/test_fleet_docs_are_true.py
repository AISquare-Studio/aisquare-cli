"""What ``docs/fleet.md`` promises, asked of the program rather than of the prose.

Two rounds of review found the same class of defect five times in this one page:
a capability described in the present tense that the code does not have — an
automated permission fallback, a per-project allowlist for check commands, an
``[fleet]`` environment precedence layer, a tester that runs in the coder's
worktree, a codename pool 40% smaller than the lists. None of them could be
caught by reading the document, and ``test_documented_commands.py`` cannot see
them either: it validates that every command and flag in a fenced block EXISTS,
not that a sentence about behaviour is true.

So this file asserts the ARTEFACT for the handful of claims that are mechanically
checkable — what the CLI prints, what the config model holds, what the word lists
count — and deliberately nothing else. It is not a prose checker: a claim that
needs a human to judge it stays a human's job, and the page is full of those.

Every check here failed at least once on this branch before the fix that made it
pass, which is the only reason it is worth its runtime.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from aisquare.core import codenames
from aisquare.core.config import AppConfig, FleetSettings
from aisquare.core.paths import HOME_ENV_VAR

DOC = Path(__file__).resolve().parents[1] / "docs" / "fleet.md"


@pytest.fixture(scope="module")
def page() -> str:
    text = DOC.read_text(encoding="utf-8")
    assert len(text) > 5_000, f"{DOC} is too small to be the guide — the sweep would be vacuous"
    return text


def _cli(*args: str, home: Path) -> subprocess.CompletedProcess[str]:
    """The real CLI in a throwaway home — the artefact, not a rendered helper."""
    return subprocess.run(
        [sys.executable, "-m", "aisquare", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": "/usr/bin:/bin", HOME_ENV_VAR: str(home), "TERM": "dumb", "NO_COLOR": "1"},
        timeout=120,
        check=False,
    )


def test_the_bare_json_invocation_does_what_the_page_says(tmp_path: Path, page: str) -> None:
    """§3.8's contract, and the one the page states: ``--json`` gets JSON, exit 2.

    This is the claim that was wrong in both directions inside one week — the
    code echoed the rich help page under ``--json`` (round 1), and after that was
    fixed the page still described the old behaviour (round 2).
    """
    result = _cli("--json", home=tmp_path / "home")

    assert result.returncode == 2, result.stderr
    payload = json.loads(result.stdout)  # not "the help page", and parseable
    assert payload["error"] == "usage"
    # …and the page says so, in whatever words: it must not promise the help page.
    claim = re.search(r"^.*`--json`.*$", page, re.M | re.I)
    assert claim is not None, "the page no longer mentions --json at all"
    assert "help" not in claim.group(0).lower() or "json" in claim.group(0).lower(), claim.group(0)


def test_the_page_promises_no_automated_permission_fallback(page: str) -> None:
    """Round 1 found this sentence promising a retry the spawn path never had."""
    for forbidden in ("detects the refusal", "falls back to `acceptEdits`", "falls back to auto"):
        assert forbidden not in page, (
            f"docs/fleet.md promises {forbidden!r} — no such detection exists in "
            "services.fleet.spawn; the mode is passed straight to `aisquare launch`"
        )


def test_the_page_does_not_promise_an_environment_layer_for_fleet(page: str) -> None:
    """``[fleet]`` has no env rung: nothing reads a ``[fleet]`` value from the environment.

    Asserted on the precedence CHAIN the page draws, because that is where the
    wrong rung was: the arrow line must not name the environment. The model is
    the control — every ``FleetSettings`` field comes from the file or its
    default, so a chain that mentions the environment describes nothing real.
    """
    chains = [line for line in page.splitlines() if line.count(">") >= 2 and "built-in" in line]
    assert chains, "the page no longer draws a precedence chain — this check inspects nothing"
    for chain in chains:
        assert "environment" not in chain.lower(), (
            f"docs/fleet.md puts the environment in [fleet]'s precedence chain: {chain!r} — "
            "no [fleet] value is read from an env var"
        )
    assert AppConfig().fleet == FleetSettings()


def test_the_codename_pool_is_the_number_the_page_states(page: str) -> None:
    """Round 2's arithmetic finding: the page said ~5,500 for a 96 by 96 pool."""
    pool = len(codenames.ADJECTIVES) * len(codenames.ANIMALS)
    stated = {
        int(m.replace(",", "")) for m in re.findall(r"\b(\d{1,2},?\d{3})\b(?=[^\n]*pair)", page)
    }
    assert stated, "the page no longer states a pool size — this check inspects nothing"
    assert stated == {pool}, f"page says {stated}, the lists give {pool}"


def test_every_fleet_config_key_the_page_names_exists(page: str) -> None:
    """A `[fleet]` key in a toml block that the model does not have is a promise."""
    fields = set(FleetSettings.model_fields)
    from aisquare.core.config import FleetRoleSettings

    role_fields = set(FleetRoleSettings.model_fields)
    named = {
        m.group(1)
        for m in re.finditer(r"^(\w+)\s*=", page, re.M)
        if not m.group(1).startswith("aisquare")
    }
    assert named, "no config keys found in the page — this check inspects nothing"
    unknown = sorted(named - fields - role_fields)
    assert not unknown, f"docs/fleet.md names [fleet] keys that do not exist: {unknown}"


def test_the_guard_would_notice_a_false_claim(page: str) -> None:
    """The negative half: each rule must bite on a page that lies.

    Without this, every assertion above could be reading the wrong thing and
    still pass — CONTRIBUTING's "a rule that has gone blind produces the
    correct-looking answer for free".
    """
    lying = page + "\nThe spawn detects the refusal and retries.\n"
    with pytest.raises(AssertionError):
        test_the_page_promises_no_automated_permission_fallback(lying)

    understated = re.sub(r"\b\d{1,2},?\d{3}\b(?=[^\n]*pair)", "5,500", page)
    with pytest.raises(AssertionError):
        test_the_codename_pool_is_the_number_the_page_states(understated)

    with_env_rung = (
        page + "\n> per-spawn flag > the environment > `[fleet]` config > built-in default\n"
    )
    with pytest.raises(AssertionError):
        test_the_page_does_not_promise_an_environment_layer_for_fleet(with_env_rung)

    invented = page + "\n```toml\n[fleet]\nno_such_key = 1\n```\n"
    with pytest.raises(AssertionError):
        test_every_fleet_config_key_the_page_names_exists(invented)
