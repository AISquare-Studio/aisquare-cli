"""A stale ``ANTHROPIC_BASE_URL``: found wherever it hides, removed by one command (#51).

Tracing routes an agent through a proxy by setting ``ANTHROPIC_BASE_URL`` and
identity headers. The same pair can also sit in a Claude settings ``env`` block,
a role binding, an env file or the fleet tmux server's global environment. Once
tracing is off, or that proxy is gone, every model call of an agent launched
afterwards fails. These tests are the doctor that finds them, and the reset
that removes them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts
from aisquare.core.config import ExplainabilityTarget, load_config, save_config
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import diagnostics
from aisquare.services import explainability_ops as ops

PROXY = "https://stg-proxy.example:9443"
FOREIGN = "https://llm-gateway.corp.example"
HEADERS = "X-Agent-Name: aisquare-coder\nX-Pipeline-Id: sess-1\nX-AISquare-Key: wk-SECRET"


class FakeServer:
    """The fleet tmux server, as far as the scan and the reset ask it."""

    def __init__(self, env: dict[str, str] | None = None, *, running: bool = True) -> None:
        self.socket = "asq-test"
        self.env = dict(env or {})
        self.running = running
        self.calls: list[tuple[str, ...]] = []

    def available(self) -> bool:
        return True

    def server_absent(self) -> bool:
        return not self.running

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        self.calls.append(args)
        if args[:2] == ("show-environment", "-g") and len(args) == 3:
            if args[2] not in self.env:
                raise RuntimeError(f"unknown variable: {args[2]}")  # tmux exits 1
            return f"{args[2]}={self.env[args[2]]}\n"
        if args[:3] == ("set-environment", "-g", "-u"):
            self.env.pop(args[3], None)
            return ""
        if args[:2] == ("set-environment", "-g") and len(args) == 4:
            self.env[args[2]] = args[3]
            return ""
        raise AssertionError(f"unexpected tmux call {args}")


def _configure(*, enabled: bool = False) -> None:
    """A machine that once traced through ``PROXY`` (the stg target), now as ``enabled``."""
    config = load_config()
    config.explainability.enabled = enabled
    config.explainability.target = "stg"
    config.explainability.targets["stg"] = ExplainabilityTarget(
        gateway_url="https://stg-gateway.example", proxy_url=PROXY
    )
    save_config(config)


def _slot_settings(env: dict[str, str]) -> Path:
    account = claude_accounts.create_account()
    path = account.config_dir / "settings.json"
    path.write_text(json.dumps({"model": "opus", "env": env}, indent=2), encoding="utf-8")
    return path


def _row(server: FakeServer | None = None) -> DoctorCheck:
    return diagnostics._check_anthropic_routing(server=server or FakeServer(running=False))


@pytest.fixture
def scan(fake_home: Path) -> Callable[..., list[ops.RoutingSource]]:
    def run(**kwargs: Any) -> list[ops.RoutingSource]:
        kwargs.setdefault("server", FakeServer(running=False))
        kwargs.setdefault("env", {})
        return ops.routing_sources(**kwargs)

    return run


# ── the doctor finds it ──────────────────────────────────────────────────────


def test_a_stale_proxy_in_a_slots_settings_makes_doctor_warn(fake_home: Path) -> None:
    """The failing-first case: on the base no row read a settings file at all."""
    _configure(enabled=False)
    path = _slot_settings({"ANTHROPIC_BASE_URL": PROXY, "ANTHROPIC_CUSTOM_HEADERS": HEADERS})

    checks = diagnostics.doctor()
    [row] = [c for c in checks if c.name == "anthropic routing"]

    assert row.status is CheckStatus.warn
    assert str(path) in row.detail
    assert row.fix is not None and "aisquare explainability reset" in row.fix
    assert "wk-SECRET" not in row.detail + (row.fix or "")


def test_the_row_is_there_without_the_orchestrator(fake_home: Path) -> None:
    """`_check_harness` saw the variable only with the team active; this row does not ask."""
    _configure(enabled=False)
    _slot_settings({"ANTHROPIC_BASE_URL": PROXY})
    assert _row().status is CheckStatus.warn


def test_the_proxy_of_the_target_tracing_uses_is_live_not_stale(fake_home: Path) -> None:
    _configure(enabled=True)
    _slot_settings({"ANTHROPIC_BASE_URL": PROXY})
    assert _row().status is CheckStatus.ok


def test_our_identity_headers_mark_a_source_ours_whatever_the_url(
    scan: Callable[..., list[ops.RoutingSource]],
) -> None:
    _configure(enabled=False)
    _slot_settings(
        {"ANTHROPIC_BASE_URL": "https://old-proxy.example", "ANTHROPIC_CUSTOM_HEADERS": HEADERS}
    )
    [source] = scan()
    assert source.verdict == "ours-stale"


def test_the_shipped_default_proxy_is_ours(scan: Callable[..., list[ops.RoutingSource]]) -> None:
    _slot_settings({"ANTHROPIC_BASE_URL": "http://127.0.0.1:9090/"})
    [source] = scan()
    assert source.verdict == "ours-stale"


def test_a_binding_env_file_and_the_tmux_server_are_read(
    fake_home: Path, tmp_path: Path, scan: Callable[..., list[ops.RoutingSource]]
) -> None:
    _configure(enabled=False)
    holder = tmp_path / "proxy-url"
    holder.write_text(PROXY + "\n", encoding="utf-8")
    config = load_config()
    config.team.profiles["coder"] = _profile()
    config.team.profiles["coder"].env["ANTHROPIC_BASE_URL"] = PROXY
    config.team.profiles["reviewer"] = _profile()
    config.team.profiles["reviewer"].env_files["ANTHROPIC_BASE_URL"] = str(holder)
    save_config(config)
    server = FakeServer({"ANTHROPIC_BASE_URL": PROXY, "ANTHROPIC_CUSTOM_HEADERS": HEADERS})

    found = scan(server=server)

    kinds = sorted((s.kind, s.verdict) for s in found)
    assert kinds == [
        ("binding", "ours-stale"),
        ("env-file", "ours-stale"),
        ("tmux", "ours-stale"),
    ]
    row = _row(server)
    assert row.status is CheckStatus.warn
    assert "[team.profiles.coder]" in row.detail
    assert str(holder) in row.detail
    assert "tmux -L asq-test" in row.detail


def test_a_foreign_gateway_is_info_and_never_a_warning(fake_home: Path) -> None:
    _configure(enabled=False)
    _slot_settings({"ANTHROPIC_BASE_URL": FOREIGN})
    row = _row()
    assert row.status is CheckStatus.ok
    assert FOREIGN in row.detail


def test_an_unparsable_url_warns(scan: Callable[..., list[ops.RoutingSource]]) -> None:
    _slot_settings({"ANTHROPIC_BASE_URL": "stg-proxy.example:9443"})
    [source] = scan()
    assert source.verdict == "unparsable"
    assert _row().status is CheckStatus.warn


def test_the_process_env_is_read(scan: Callable[..., list[ops.RoutingSource]]) -> None:
    _configure(enabled=False)
    [source] = scan(env={"ANTHROPIC_BASE_URL": PROXY})
    assert (source.kind, source.verdict) == ("process", "ours-stale")
    assert "unset ANTHROPIC_BASE_URL" in source.fix


def test_a_projects_settings_files_are_read(
    fake_home: Path, tmp_path: Path, scan: Callable[..., list[ops.RoutingSource]]
) -> None:
    from aisquare.core.store import store_session
    from aisquare.core.workspace import project_id_for
    from aisquare.models import ProjectInfo

    _configure(enabled=False)
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": PROXY}}), encoding="utf-8"
    )
    with store_session() as store:
        store.ensure_project(
            ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
        )
    [source] = scan()
    assert source.path == root.resolve() / ".claude" / "settings.local.json"


# ── reset removes it ─────────────────────────────────────────────────────────


def test_reset_dry_run_changes_nothing_and_yes_applies_with_a_backup(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(enabled=True)
    path = _slot_settings(
        {"ANTHROPIC_BASE_URL": PROXY, "ANTHROPIC_CUSTOM_HEADERS": HEADERS, "FOO": "1"}
    )
    before = path.read_bytes()
    server = FakeServer({"ANTHROPIC_BASE_URL": PROXY})
    monkeypatch.setattr(ops, "_fleet_tmux_server", lambda: server)

    dry = runner.invoke(app, ["--json", "explainability", "reset", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    plan = json.loads(dry.output)
    assert plan["dry_run"] is True
    assert {s["where"] for s in plan["removed"]} >= {str(path)}
    assert "wk-SECRET" not in dry.output
    assert path.read_bytes() == before
    assert load_config().explainability.enabled is True
    assert "ANTHROPIC_BASE_URL" in server.env

    done = runner.invoke(app, ["explainability", "reset", "--yes"])
    assert done.exit_code == 0, done.output
    assert "wk-SECRET" not in done.output
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"model": "opus", "env": {"FOO": "1"}}
    [backup] = path.parent.glob("settings.json.bak-*")
    assert backup.read_bytes() == before
    assert load_config().explainability.enabled is False
    assert load_config().explainability.targets["stg"].proxy_url == PROXY, "targets kept"
    assert server.env == {}
    assert _row(server).status is CheckStatus.ok

    again = runner.invoke(app, ["explainability", "reset", "--yes"])
    assert again.exit_code == 0, again.output
    assert "nothing to reset" in again.output


def test_reset_leaves_a_foreign_gateway_alone(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(enabled=False)
    path = _slot_settings({"ANTHROPIC_BASE_URL": FOREIGN})
    before = path.read_bytes()
    monkeypatch.setattr(ops, "_fleet_tmux_server", lambda: FakeServer(running=False))
    result = runner.invoke(app, ["explainability", "reset", "--yes"])
    assert result.exit_code == 0, result.output
    assert path.read_bytes() == before
    assert "nothing to reset" in result.output


def test_reset_removes_a_binding_and_names_the_shell_but_never_edits_an_rc(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(enabled=False)
    config = load_config()
    config.team.profiles["coder"] = _profile()
    config.team.profiles["coder"].env.update({"ANTHROPIC_BASE_URL": PROXY, "X": "y"})
    save_config(config)
    rc = fake_home / ".zshrc"
    rc.write_text(f"export ANTHROPIC_BASE_URL={PROXY}\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_rc_home", lambda: fake_home)
    monkeypatch.setattr(ops, "_fleet_tmux_server", lambda: FakeServer(running=False))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", PROXY)

    result = runner.invoke(app, ["--json", "explainability", "reset", "--yes"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert load_config().team.profiles["coder"].env == {"X": "y"}
    assert report["shell_unset"] == "unset ANTHROPIC_BASE_URL", "only what this shell sets"
    assert report["rc_files"] == [str(rc)]
    assert rc.read_text(encoding="utf-8") == f"export ANTHROPIC_BASE_URL={PROXY}\n"


def test_reset_keeps_a_foreign_gateway_beside_our_headers_and_other_headers(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #321, minor 1: our header lines made the whole place ours, and reset
    removed the operator's gateway URL and their own auth header with them."""
    _configure(enabled=False)
    mixed = f"Authorization: Bearer corp-SECRET\n{HEADERS}"
    path = _slot_settings({"ANTHROPIC_BASE_URL": FOREIGN, "ANTHROPIC_CUSTOM_HEADERS": mixed})
    config = load_config()
    config.team.profiles["coder"] = _profile()
    config.team.profiles["coder"].env.update(
        {"ANTHROPIC_BASE_URL": FOREIGN, "ANTHROPIC_CUSTOM_HEADERS": HEADERS}
    )
    save_config(config)
    server = FakeServer({"ANTHROPIC_BASE_URL": FOREIGN, "ANTHROPIC_CUSTOM_HEADERS": mixed})
    monkeypatch.setattr(ops, "_fleet_tmux_server", lambda: server)

    result = runner.invoke(app, ["explainability", "reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert "SECRET" not in result.output
    env = json.loads(path.read_text(encoding="utf-8"))["env"]
    assert env == {
        "ANTHROPIC_BASE_URL": FOREIGN,
        "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer corp-SECRET",
    }
    assert load_config().team.profiles["coder"].env == {"ANTHROPIC_BASE_URL": FOREIGN}
    assert server.env == {
        "ANTHROPIC_BASE_URL": FOREIGN,
        "ANTHROPIC_CUSTOM_HEADERS": "Authorization: Bearer corp-SECRET",
    }
    assert _row(server).status is CheckStatus.ok


def test_reset_leaves_an_env_file_that_holds_more_than_ours(
    fake_home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is never edited, so a binding to one that also holds the operator's
    header is not dropped either: reset says what to do instead."""
    _configure(enabled=False)
    holder = tmp_path / "headers"
    holder.write_text(f"Authorization: Bearer corp-SECRET\n{HEADERS}\n", encoding="utf-8")
    config = load_config()
    config.team.profiles["coder"] = _profile()
    config.team.profiles["coder"].env_files["ANTHROPIC_CUSTOM_HEADERS"] = str(holder)
    save_config(config)
    monkeypatch.setattr(ops, "_fleet_tmux_server", lambda: FakeServer(running=False))

    result = runner.invoke(app, ["explainability", "reset", "--yes"])

    assert result.exit_code == 1
    assert f"{holder} holds more than ours" in result.output
    assert "SECRET" not in result.output
    assert load_config().team.profiles["coder"].env_files == {
        "ANTHROPIC_CUSTOM_HEADERS": str(holder)
    }


def test_a_url_is_shown_without_userinfo_or_query(fake_home: Path) -> None:
    """Review of #321, minor 2: the doctor's line and --json carried a URL verbatim."""
    _configure(enabled=False)
    _slot_settings({"ANTHROPIC_BASE_URL": "https://bob:tok-SECRET@corp.example/v1?key=SECRET#x"})
    row = _row()
    assert "SECRET" not in row.detail
    assert "https://corp.example/v1" in row.detail
    [source] = ops.routing_sources(env={}, server=FakeServer(running=False))
    assert source.as_json()["base_url"] == "https://corp.example/v1"


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        # A password holding '@': the userinfo runs to the LAST '@' of the authority.
        ("https://bob:p@ss-SECRET@corp.example/v1", "https://corp.example/v1"),
        # No scheme: the authority starts the text.
        ("bob:tok-SECRET@corp.example:8443/v1", "corp.example:8443/v1"),
        ("//bob:tok-SECRET@corp.example/v1", "//corp.example/v1"),
        # Nothing to cut: an '@' past the authority is the path's, and stays.
        ("http://localhost:8080/v1/@team", "http://localhost:8080/v1/@team"),
        ("localhost:8080", "localhost:8080"),
    ],
)
def test_a_shown_url_loses_all_its_userinfo_whatever_its_shape(url: str, shown: str) -> None:
    """Review of #356: '[^/?#@]*@' stopped at a password's own '@' and left the rest of it,
    and a URL with no scheme kept its 'user:token@' whole."""
    assert ops._shown_url(url) == shown


def test_disable_names_the_leftovers_and_the_reset(
    fake_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(enabled=True)
    path = _slot_settings({"ANTHROPIC_BASE_URL": PROXY})
    monkeypatch.setattr(ops, "_fleet_tmux_server", lambda: FakeServer(running=False))
    result = runner.invoke(app, ["explainability", "disable"])
    assert result.exit_code == 0, result.output
    assert str(path) in result.output
    assert "aisquare explainability reset" in result.output
    as_json = runner.invoke(app, ["--json", "explainability", "disable"])
    assert json.loads(as_json.output)["leftovers"][0]["where"] == str(path)


# ── doctor --fix no longer undoes a Disable ──────────────────────────────────


def test_fix_without_a_target_leaves_tracing_off() -> None:
    actions = ops.apply_fixes(confirm=lambda _: False)
    assert load_config().explainability.enabled is False
    assert any("--target" in action for action in actions)


def _profile() -> Any:
    from aisquare.core.config import RoleLaunchProfile

    return RoleLaunchProfile()
