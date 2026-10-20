"""The Doctor view's agent rows in asq: Connect for Claude Code, no button for the rest.

The report is the real doctor's, on a machine with Claude Code installed and not
connected and Codex and Cursor on disk. The runner does what ``aisquare --json
agents connect`` would, in process, so the click's effect on ``settings.json`` and
on the re-run report is the real one. Negative controls: Codex and Cursor have
rows and no button, before and after; and the Connect button is gone once the
shared check says the directory is connected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from aisquare.cli.ui.views.doctor import DoctorRefreshed, DoctorView, FixApplied
from aisquare.core.selfcli import CliResult
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import agents as agents_service
from aisquare.services import diagnostics
from tests.ui_workers import settle_page


@dataclass
class InProcessConnect:
    """``aisquare --json agents connect …`` without the subprocess, recording each call."""

    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        argv = list(args)
        self.calls.append(argv)
        words = [word for word in argv if word != "--json"]
        if words[:3] != ["agents", "connect", "claude-code"] or words[3:4] != ["--config-dir"]:
            return CliResult(argv=argv, returncode=1, stdout='{"error":"unexpected"}', stderr="")
        connection = agents_service.connect("claude-code", Path(words[4]))
        return CliResult(argv=argv, returncode=0, stdout=connection.model_dump_json(), stderr="")


class Host(App[None]):
    def __init__(self, view: DoctorView) -> None:
        super().__init__()
        self.view = view
        self.received: list[Message] = []

    def compose(self) -> ComposeResult:
        yield self.view

    def on_doctor_refreshed(self, message: DoctorRefreshed) -> None:
        self.received.append(message)

    def on_fix_applied(self, message: FixApplied) -> None:
        self.received.append(message)


def _labels(view: DoctorView) -> dict[str, str]:
    """Button id → label, for the fix buttons on screen now."""
    return {
        button.id or "": str(button.label)
        for button in view.query("#doctor-fixes Button").results(Button)
    }


def test_connect_is_one_click_and_the_planned_agents_have_no_button(
    isolated_agent_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = isolated_agent_home / ".claude"
    for name in (".claude", ".codex", ".cursor"):
        (isolated_agent_home / name).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    run = InProcessConnect()
    connect_label = f"aisquare agents connect claude-code --config-dir {claude}"

    async def drive() -> tuple[dict[str, str], dict[str, str], str, list[Message]]:
        view = DoctorView(run=run, refresh=lambda cwd: diagnostics.doctor(cwd=cwd))
        host = Host(view)
        async with host.run_test(size=(160, 60)) as pilot:
            view.show(diagnostics.doctor())
            await settle_page(host)
            before = _labels(view)
            button_id = next(key for key, label in before.items() if label == connect_label)
            await pilot.click(f"#{button_id}")
            await settle_page(host)
            report = str(view.query_one("#doctor-report", Static).render())
            return before, _labels(view), report, host.received

    before, after, report, received = asyncio.run(drive())

    assert connect_label in before.values(), before
    assert not any("codex" in label or "cursor" in label for label in before.values()), before
    assert run.calls == [
        ["--json", "agents", "connect", "claude-code", "--config-dir", str(claude)]
    ]
    assert agents_service.claude_code_connected(claude) is True, "the click connected it"
    assert connect_label not in after.values(), "a connected directory has no Connect left"
    assert not any("codex" in label or "cursor" in label for label in after.values()), after
    # By line, not by sentence: the row names the version of whatever `claude` is on PATH.
    lines = {line.split(":")[0]: line for line in report.splitlines() if ": " in line}
    assert "connected (all lifecycle hooks installed" in lines["✓ claude-code"], report
    assert f"Codex detected at {isolated_agent_home / '.codex'}" in lines["✓ codex"], report
    refreshed = [m for m in received if isinstance(m, DoctorRefreshed)]
    assert len(refreshed) == 1, received
    rows: dict[str, DoctorCheck] = {check.name: check for check in refreshed[0].checks}
    assert rows["claude-code"].status is CheckStatus.ok, rows["claude-code"]
    assert rows["cursor"].status is CheckStatus.ok and rows["cursor"].fix is None
