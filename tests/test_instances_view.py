"""#20: more than one asq on one home is visible: the chip under the status line,
``aisquare instances`` and a doctor row.

The owner runs asq at the Mac and over SSH into the same Mac at once, and nothing on screen
said so.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rich.text import Text
from typer.testing import CliRunner

from aisquare.cli.app import app as cli_app
from aisquare.cli.ui import app as ui_app
from aisquare.cli.ui.instances_chip import InstancesChip, chip_text, chip_tooltip
from aisquare.core import instances
from aisquare.core.instances import Instance
from aisquare.models import CheckStatus
from aisquare.services import diagnostics
from tests.asq_elsewhere import asq_running
from tests.ui_workers import settle_page

HERE_WORD = "Mac" if sys.platform == "darwin" else "local"
"""What the chip calls an asq at this machine, on the machine the suite runs on."""


@pytest.fixture(autouse=True)
def _no_registration_outlives_its_test(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The beat thread's first look asks tmux or runs `ps` (core.clipboard): not here.
    monkeypatch.setattr(instances, "attached_kind", lambda env=None: "native")
    yield
    registration = instances.this_registration()
    if registration is not None:
        registration.deregister()


def _instance(kind: instances.Kind = "native", **changes: Any) -> Instance:
    return replace(instances.describe_this_process(kind=kind), **changes)


def _registered(kind: instances.Kind = "native") -> instances.Registration:
    me = instances.describe_this_process(kind=kind)
    with instances.gate():
        return instances.register(me)


# --- 1. the chip ------------------------------------------------------------------------------


def test_one_asq_shows_no_chip() -> None:
    assert chip_text([_instance()], None) is None
    assert chip_text([], None) is None


def test_two_asqs_name_where_each_runs_and_mark_this_one() -> None:
    mac = _instance("native", id="mac")
    ssh = _instance("ssh", id="ssh")

    over_ssh = chip_text([mac, ssh], "ssh", host=mac.host)
    at_the_mac = chip_text([ssh, mac], "mac", host=mac.host)

    assert over_ssh is not None and at_the_mac is not None
    assert over_ssh.plain == f"2 asq · {HERE_WORD} + ssh (here)"
    assert at_the_mac.plain == f"2 asq · {HERE_WORD} (here) + ssh"
    # The mark is bold, so it reads at a glance too.
    marked = next(span for span in at_the_mac.spans if span.style == "bold")
    assert at_the_mac.plain[marked.start : marked.end] == f"{HERE_WORD} (here)"


def test_asqs_in_one_place_are_counted_and_another_host_is_named() -> None:
    here = _instance("native", id="a")
    elsewhere = _instance("native", id="b", host="mini.local")
    ssh_one = _instance("ssh", id="c")
    ssh_two = _instance("ssh", id="d")

    text = chip_text([ssh_one, elsewhere, here, ssh_two], "c", host=here.host)

    assert text is not None
    assert text.plain == f"4 asq · {HERE_WORD} + mini + 2 ssh (here)"


def test_an_unregistered_process_marks_nothing() -> None:
    text = chip_text([_instance("native", id="a"), _instance("ssh", id="b")], None)
    assert text is not None
    assert "(here)" not in text.plain


def test_releases_that_differ_are_flagged_in_the_chip_and_its_tooltip() -> None:
    mine = _instance("native", id="a")
    old = _instance("ssh", id="b", version="0.0.1")

    text = chip_text([mine, old], "a", host=mine.host)
    tooltip = chip_tooltip([mine, old], "a")

    assert text is not None and text.plain.endswith(" ⚠")
    assert "different releases" in tooltip
    assert f"asq {mine.version} — {mine.where}  ← this asq" in tooltip
    assert f"asq 0.0.1 — {old.where}" in tooltip


def test_the_chip_shows_while_another_asq_runs_and_hides_when_it_quits() -> None:
    """The real path: this asq registered, another one's record and held lock beside it, the
    frame's read on its thread, the chip under the status line."""
    me = _registered("native")

    async def run() -> None:
        app = ui_app.FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(120, 40)):
            await settle_page(app)
            chip = app.query_one(InstancesChip)
            assert chip.display is False, "alone, there is no chip"

            with asq_running(kind="ssh") as other:
                app.refresh_data()
                await settle_page(app)
                assert chip.display is True
                assert isinstance(chip.content, Text)
                assert chip.content.plain == f"2 asq · {HERE_WORD} (here) + ssh"
                assert {i.id for i in chip.live} == {me.instance.id, other.id}
                assert chip.parent is app.query_one("#others")
                assert "← this asq" in str(chip.tooltip)

            app.refresh_data()
            await settle_page(app)
            assert chip.display is False, "the other asq quit: alone again"

    asyncio.run(run())


def test_a_frame_that_could_not_read_the_registry_leaves_the_chip_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _registered("native")

    def unreadable(*, prune: bool = True) -> list[Instance]:
        raise OSError("no")

    async def run() -> None:
        app = ui_app.FleetApp(refresh_seconds=3600, doctor=lambda: [])
        async with app.run_test(size=(120, 40)):
            await settle_page(app)
            chip = app.query_one(InstancesChip)
            assert chip.display is True

            monkeypatch.setattr(instances, "live_instances", unreadable)
            app.refresh_data()
            await settle_page(app)
            assert chip.display is True

    with asq_running(kind="ssh"):
        asyncio.run(run())


# --- 2. aisquare instances ----------------------------------------------------------------------


def test_instances_lists_nothing_on_a_home_with_no_asq(runner: CliRunner) -> None:
    result = runner.invoke(cli_app, ["instances"])
    assert result.exit_code == 0, result.output
    assert "No asq is running on this home." in result.output

    as_json = runner.invoke(cli_app, ["instances", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout) == {"instances": []}


def test_instances_lists_each_live_asq(runner: CliRunner) -> None:
    beat = datetime.now(UTC) - timedelta(seconds=30)  # another host's: live under a minute
    with (
        asq_running(kind="native") as mac,
        asq_running(kind="ssh", host="mini.local", heartbeat=beat) as ssh,
    ):
        result = runner.invoke(cli_app, ["instances"])

    assert result.exit_code == 0, result.output
    header, *rows = result.output.splitlines()
    assert header.split() == ["KIND", "HOST", "PID", "VERSION", "SINCE", "HEARTBEAT"]
    native = next(line for line in rows if line.startswith("native"))
    remote = next(line for line in rows if line.startswith("ssh"))
    since = mac.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    assert native.split()[:6] == ["native", mac.host, str(mac.pid), mac.version, *since.split()]
    assert remote.split()[:4] == ["ssh", "mini.local", str(ssh.pid), ssh.version]
    assert any(f"{age} s ago" in remote for age in (30, 31, 32))


def test_instances_json_carries_every_field(runner: CliRunner) -> None:
    with asq_running(kind="ssh") as ssh:
        result = runner.invoke(cli_app, ["--json", "instances"])
        after = runner.invoke(cli_app, ["instances", "--json"])

    assert result.exit_code == 0, result.output
    # Either place of the flag; the age may tick between the two runs.
    assert [
        {k: v for k, v in row.items() if k != "heartbeat_age_s"}
        for listing in (result, after)
        for row in json.loads(listing.stdout)["instances"]
    ] == [{**ssh.to_record(), "this": False}] * 2
    (row,) = json.loads(result.stdout)["instances"]
    assert row["id"] == ssh.id
    assert row["kind"] == "ssh"
    assert row["host"] == ssh.host
    assert row["pid"] == ssh.pid
    assert row["version"] == ssh.version
    assert row["schema"] == ssh.schema
    assert row["tty"] == "/dev/ttys009"
    assert datetime.fromisoformat(row["started_at"]) == ssh.started_at
    assert datetime.fromisoformat(row["heartbeat"]) == ssh.heartbeat
    assert isinstance(row["heartbeat_age_s"], int) and 0 <= row["heartbeat_age_s"] < 60
    assert row["this"] is False


def test_instances_says_so_when_releases_differ(runner: CliRunner) -> None:
    with asq_running(kind="native"), asq_running(kind="ssh", version="0.0.1"):
        result = runner.invoke(cli_app, ["instances"])
    assert result.exit_code == 0
    assert "different releases" in result.stderr


def test_instances_is_a_look_that_prunes_and_makes_nothing(runner: CliRunner) -> None:
    folder = instances.registry_dir()
    assert runner.invoke(cli_app, ["instances"]).exit_code == 0
    assert not folder.exists(), "a look made the registry folder"

    with asq_running(kind="ssh", hold_lock=False) as crashed:
        result = runner.invoke(cli_app, ["--json", "instances"])
        assert json.loads(result.stdout) == {"instances": []}, "a crashed asq is not live"
        assert (folder / f"{crashed.id}.json").exists(), "a look pruned a record"


# --- 3. the doctor row --------------------------------------------------------------------------


def test_doctor_says_none_runs() -> None:
    row = diagnostics._check_instances()
    assert row.name == "asq instances"
    assert row.status is CheckStatus.ok
    assert row.detail == "no asq is running on this home"
    assert not instances.registry_dir().exists()


def test_doctor_lists_asqs_on_this_release_at_ok() -> None:
    with asq_running(kind="native") as mac, asq_running(kind="ssh") as ssh:
        row = diagnostics._check_instances()
    assert row.status is CheckStatus.ok
    assert row.detail.startswith(f"2 running, all on {mac.version}: ")
    assert mac.label in row.detail and ssh.label in row.detail


def test_doctor_marks_this_asq_inside_asq() -> None:
    me = _registered("native")
    row = diagnostics._check_instances()
    assert row.status is CheckStatus.ok
    assert row.detail == f"1 running: {me.instance.label} (this one)"


@pytest.mark.parametrize("differs", ["version", "schema"])
def test_doctor_warns_on_another_release_or_schema(differs: str) -> None:
    change: dict[str, Any] = {"version": "0.0.1"} if differs == "version" else {"schema": 1}
    with asq_running(kind="native"), asq_running(kind="ssh", **change) as old:
        row = diagnostics._check_instances()
    assert row.status is CheckStatus.warn
    assert old.label in row.detail
    assert row.detail.startswith("2 running, 1 on another release than this aisquare")
    assert row.fix is not None and "q in that asq" in row.fix


def test_the_row_is_in_the_doctor() -> None:
    with asq_running(kind="ssh", version="0.0.1"):
        rows = {check.name: check for check in diagnostics.doctor()}
    assert rows["asq instances"].status is CheckStatus.warn
