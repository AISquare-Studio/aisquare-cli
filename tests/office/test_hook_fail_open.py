"""A hook may never disrupt the agent, and Office evidence may never become a TeamEvent.

Two properties, both load-bearing and neither obvious from reading a call site.

**Fail open.** Every hook command swallows its failure, writes one line to
*stderr* — never stdout, because for two of the five hooks stdout *is* the
model's prompt — and exits 0. A hook that exited non-zero on a damaged store
would turn an Office problem into the user's problem, mid-session.

**Separate paths.** The team event path can inject events into an agent's
context and ships them to Explainability. Office observations are private local
evidence and must stay on the sidecar's side of that line: a raw ``office.*``
row written as a ``TeamEvent`` would be evidence walking into a context window
and out to a platform. The last tests here assert that at the source level,
because the mistake is one import away and nothing else would notice it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.hook import app as hook_app
from aisquare.office import observe
from aisquare.office.providers import claude_observe

SILENT_HOOKS = ("session-end", "stop", "notification")
"""The three hooks whose stdout is not the agent's context, and must stay empty."""

ALL_HOOKS = ("session-start", "user-prompt-submit", *SILENT_HOOKS)

MALFORMED = ("", "   ", "not json at all", "{", "[]", '"a string"', "null", '{"cwd": 5}')

#: Store methods that write. None may appear in this packet's source.
WRITE_METHODS = (
    "add_team_event",
    "add_signal_event",
    "add_prompt",
    "upsert_session",
    "touch_session",
    "mark_attention",
    "end_session",
    "upsert_task",
    "claim_task",
    "set_task_status",
    "upsert_fleet_agent",
    "end_fleet_agent",
    "set_meta",
)

OWNED_SOURCES = (
    Path(observe.__file__),
    Path(claude_observe.__file__),
    Path(claude_observe.__file__).parent / "__init__.py",
)


@pytest.mark.parametrize("hook", ALL_HOOKS)
@pytest.mark.parametrize("payload", MALFORMED)
def test_a_malformed_payload_never_fails_a_hook(runner: CliRunner, hook: str, payload: str) -> None:
    """Every degrade path in ``cli/hook.py``, exercised rather than assumed."""
    result = runner.invoke(hook_app, [hook], input=payload)

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("hook", SILENT_HOOKS)
def test_a_failing_hook_prints_nothing_to_stdout(runner: CliRunner, hook: str) -> None:
    """stdout is the agent's context for two hooks; these three must never print."""
    result = runner.invoke(hook_app, [hook], input="{")

    assert result.exit_code == 0
    assert result.stdout == ""


def test_a_broken_store_still_exits_zero(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this doctrine exists for: an unreadable database mid-session."""
    from aisquare.services import hooks as hooks_service

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(hooks_service, "needs_attention", boom)

    result = runner.invoke(hook_app, ["notification"], input='{"session_id": "ses-1"}')

    assert result.exit_code == 0
    assert result.stdout == ""


def test_a_hook_that_raises_anything_still_exits_zero(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only sqlite: the catch is ``Exception`` on purpose."""
    from aisquare.services import hooks as hooks_service

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("anything at all")

    monkeypatch.setattr(hooks_service, "session_ended", boom)

    result = runner.invoke(hook_app, ["session-end"], input='{"session_id": "ses-1"}')

    assert result.exit_code == 0


def test_no_office_module_calls_a_store_write() -> None:
    """The acceptance criterion, asserted where it can actually be violated.

    A read-only collector is read-only because nothing in it writes — not because
    a docstring says so. This reads the shipped source and fails naming the
    method, which is what makes adding one a visible decision.
    """
    offences: list[str] = []
    for path in OWNED_SOURCES:
        source = path.read_text(encoding="utf-8")
        offences.extend(
            f"{path.name}: {method}" for method in WRITE_METHODS if f".{method}(" in source
        )

    assert not offences, f"this packet's source calls a store write: {offences}"


def test_no_office_module_constructs_a_team_event() -> None:
    """Office evidence never enters the shipping path, even as a well-meant note."""
    offences = [
        path.name for path in OWNED_SOURCES if "TeamEvent(" in path.read_text(encoding="utf-8")
    ]

    assert not offences, f"this packet builds a TeamEvent in: {offences}"


def test_the_board_reader_offers_no_way_to_write() -> None:
    """The collector is typed against five reads, so a write is not reachable."""
    surface = set(observe.BoardReader.__protocol_attrs__)  # type: ignore[attr-defined]

    assert surface == {
        "list_projects",
        "team_sessions",
        "team_tasks",
        "recent_events",
        "latest_seq",
    }
    assert surface.isdisjoint(WRITE_METHODS)
