"""Claude Code accounts on a real Claude Code: two slots, their order and default, ``accounts
run``, the hooks of each, ``fleet switch`` between them, and ``uninstall``.

Run ``AISQUARE_TEST_CLAUDE=1 pytest tests/test_claude_native_accounts.py``, in the pattern of
``tests/test_claude_native.py`` (its loopback Messages stub and its ``native`` fixture). Slot
1 is the plain config dir the fixture made; slot 2 is a managed one, made as ``accounts add``
makes it (``begin_sign_in``, then ``complete_sign_in``, which connects its hooks), its sign-in
being what one leaves in the slot less the login itself: every slot runs on the stub through
its ``settings.json`` (its requests under a prefix of its own, ``/s2``), never on a token. No
account, no network and no spend.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from aisquare.cli.app import app
from aisquare.core import selfcli
from aisquare.models import ClaudeAccount
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import install_route
from tests.test_claude_native import (  # noqa: F401 - `native` and `native_options` are fixtures
    AGENT,
    Native,
    claude_settings,
    native,
    native_options,
    options,
    pytestmark,
    write_config_dir,
)
from tests.test_claude_native_fleet import asq, asq_json, runner

SECOND = "second@native.test"


def add_slot(on: Native) -> ClaudeAccount:
    """A second account, as ``accounts add`` makes one: a fresh slot, what its sign-in leaves
    in it (on the stub, under ``/s2``), and its hooks connected."""
    account = accounts_service.begin_sign_in(None, agent=AGENT)
    assert account.slot == 2 and account.managed, account
    write_config_dir(account.config_dir, on.stub, on.workspace, prefix="/s2", email=SECOND)
    described = accounts_service.complete_sign_in(account)
    assert described.hooks_installed, described
    return account


def slot_rows() -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """``accounts list --json``: what it says of Claude Code's install, and its rows by slot."""
    listed = asq_global("--json", "accounts", "list")
    return listed["claude"], {row["account"]["slot"]: row for row in listed["accounts"]}


def asq_global(*args: str) -> Any:
    """``aisquare <args>``, as typed, its exit code checked; JSON read where ``--json``."""
    result = runner.invoke(app, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout) if "--json" in args else result.output


@options()
def test_real_claude_code_on_two_slots_runs_each_with_its_hooks_in_order_and_by_default(
    native: Native,  # noqa: F811
) -> None:
    second = add_slot(native)
    install, rows = slot_rows()
    assert install["installed"] and install["binary"] == native.binary, install
    assert set(rows) == {1, 2}, rows
    assert all(row["hooks_installed"] for row in rows.values()), rows
    assert Path(rows[1]["account"]["config_dir"]) == native.config_dir
    assert rows[2]["identity"]["email"] == SECOND
    # The order, then the project's default: a spawn that names no account runs on it.
    order = asq_global("--json", "accounts", "order", "2", "1")
    assert [account["slot"] for account in order["order"]] == [2, 1], order
    chosen = asq_global("--json", "accounts", "default", "2", "--project", ".")
    assert chosen["default"]["slot"] == 2, chosen
    receipt = native.spawn(prompt="spawned by default")
    turn = native.heard("spawned by default")
    assert turn.path == "/s2/v1/messages", "slot 2's own settings"
    assert receipt.agent.account_slot == 2
    session = native.until(lambda: native.session(receipt.agent.session_id), 20, "its session")
    assert session.account == str(second.config_dir)
    assert session.transcript_path and session.transcript_path.startswith(str(second.config_dir))
    # `accounts run 1` in a terminal: Claude Code on slot 1's config dir, whose hooks put
    # its session, its prompt and its turn's end on the board.
    command = shlex.join([*selfcli.argv_for(["accounts", "run", "1"]), "--", "run on one"])
    native.srv.run(
        "new-session", "-d", "-s", "run1", "-x", "200", "-y", "50",
        "-c", str(native.workspace), command,
    )  # fmt: skip
    assert native.heard("run on one").path == "/v1/messages"
    on_one = native.until(
        lambda: next(
            (
                s
                for s in native.sessions()
                if s.account == str(native.config_dir) and s.state == "waiting"
            ),
            None,
        ),
        20,
        "slot 1's session at its turn's end",
        "run1",
    )
    assert on_one.role == "unassigned" and on_one.agent == AGENT
    assert "run on one" in native.prompts()


@options()
def test_real_claude_code_switched_to_another_slot_resumes_its_conversation_there(
    native: Native,  # noqa: F811
) -> None:
    second = add_slot(native)
    receipt = native.spawn(prompt="said on one", account="1")
    row = receipt.agent
    native.heard("said on one")
    native.until(lambda: native.state(row.session_id) == "waiting", 30, "idle", row.pane_id)
    switched = asq_json(native, "fleet", "switch", row.label, "--to", "2")
    started = switched.get("started") or switched.get("agent") or switched
    assert started["account_slot"] == 2, switched
    native.until(lambda: native.ended(row.session_id) is False, 30, "the session resumed")
    turn = native.until(
        lambda: next((t for t in native.stub.turns if t.path.startswith("/s2/")), None),
        40,
        "a turn on slot 2",
    )
    assert "said on one" in turn.everything, "the conversation moved with it"
    sessions = [s for s in native.sessions() if s.account == str(second.config_dir)]
    assert sessions, [s.account for s in native.sessions()]


@options()
def test_real_claude_code_uninstall_leaves_each_slots_settings_as_it_found_them(
    native: Native,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = add_slot(native)
    theirs = {
        native.config_dir: native.settings,
        second.config_dir: claude_settings(native.stub, prefix="/s2"),
    }
    receipt = native.spawn(prompt="before the uninstall")
    native.heard("before the uninstall")
    asq(native, "fleet", "stop", receipt.agent.label)
    native.until(lambda: native.ended(receipt.agent.session_id), 30, "stopped")
    package: list[list[str]] = []
    monkeypatch.setattr(
        install_route,
        "exec_replace",
        lambda argv, **kwargs: package.append(list(argv)),
    )
    asq_global("uninstall", "--yes")
    for config_dir, settings in theirs.items():
        after = json.loads((config_dir / "settings.json").read_text(encoding="utf-8"))
        assert after == settings, (config_dir, after)
    # And Claude Code runs there with no hook of aisquare's: nothing reaches the board.
    before = len(native.sessions())
    command = shlex.join([native.binary, "--", "after the uninstall"])
    native.srv.run(
        "new-session", "-d", "-s", "after", "-x", "200", "-y", "50",
        "-c", str(native.workspace), command,
    )  # fmt: skip
    turn = native.heard("after the uninstall")
    assert "aisquare-team" not in turn.everything
    assert len(native.sessions()) == before
