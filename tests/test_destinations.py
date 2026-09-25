"""Where a project's traces land, chosen while signed in (#142).

Every step of ``use`` has its control: the listing goes through the session
(its host, its token, the workspace header); the deployment follows the
session's host and fills only what is empty; the key is minted when the API
allows and the refusal is named when it does not; the roster is bound with the
key and a refusal is reported per identity; ``logout`` forgets minted keys and
leaves hand-attached ones; the resolver prefers the destination's deployment
between the explicit forms and the machine default; ``doctor`` still opens no
store.
"""

from __future__ import annotations

import ast
import json
import shlex
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import aisquare
from aisquare.cli import auth as auth_cli
from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilityTarget, load_config, save_config
from aisquare.core.store import ContextStore, SqliteStore, store_session
from aisquare.core.workspace import pin_project, project_id_for
from aisquare.models import ProjectInfo, TraceDestination
from aisquare.services import destinations as dest
from aisquare.services import explainability as service
from aisquare.services import explainability_ops as ops
from aisquare.services import iam
from tests.idp_stub import IdentityProviderStub

WORKSPACES = [
    {"id": 42, "uid": "ws-uid-42", "name": "acme", "type": "team", "effective_role": "ADMIN"},
    {"id": 7, "uid": "ws-uid-7", "name": "Personal", "type": "personal", "effective_role": "OWNER"},
    {
        "id": 99,
        "uid": "ws-uid-99",
        "name": "guests",
        "effective_role": None,
        "invite_status": "pending",
    },
]
STUDIOS = {
    "42": [
        {"id": 302, "uid": "st-302", "name": "Unassigned", "is_inbox": True, "workspace_id": 42},
        {"id": 301, "uid": "st-301", "name": "Frontend", "workspace_id": 42},
        {"id": 303, "uid": "st-303", "name": "API", "is_default": True, "workspace_id": 42},
    ],
    "7": [],
}


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_cli, "_sleep", lambda _seconds: None)


@pytest.fixture
def idp() -> Iterator[IdentityProviderStub]:
    stub = IdentityProviderStub()
    stub.workspaces = [dict(w) for w in WORKSPACES]
    stub.studios = {k: [dict(s) for s in v] for k, v in STUDIOS.items()}
    yield stub
    stub.close()


@pytest.fixture
def signed_in(runner: CliRunner, idp: IdentityProviderStub, isolated_home: Path) -> iam.Session:
    result = runner.invoke(app, ["login", "--no-browser", "--api-url", idp.url])
    assert result.exit_code == 0, result.output
    session = iam.current_session()
    assert session is not None
    return session


@pytest.fixture(autouse=True)
def run_from_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command runs from the ``web`` checkout, the project most tests make.

    Without ``--project``, ``use``, ``studios``, ``status`` and ``whoami`` ask
    about the project a launch here joins — this checkout — and not the
    ``project switch`` pin, which launches ignore (review of #170). So the
    working directory, not a pin, is what makes ``web`` the default.
    """
    web = tmp_path / "web"
    web.mkdir()
    monkeypatch.chdir(web)


def _project(root: Path) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        store.onboard_project(info)
    return info


def _json(runner: CliRunner, *argv: str) -> Any:
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# --- the environment table -------------------------------------------------------------------


def test_the_session_host_names_the_deployment() -> None:
    assert dest.environment_name("https://api.aisquare.studio") == "prod"
    assert dest.environment_name("https://stg-api.aisquare.studio/") == "stg"
    assert dest.environment_name("https://studio-api-dev.aisquare.com") == "dev"
    assert dest.environment_name("http://localhost") == "local"
    assert dest.environment_for("https://api.example.org") is None, "unknown host: nothing"
    assert dest.environment_name("https://api.example.org:8443") == "api.example.org"


def test_ensure_target_fills_only_what_is_empty_and_never_enables(isolated_home: Path) -> None:
    config = AppConfig()
    name, changed = dest.ensure_target(config, "https://stg-api.aisquare.studio")
    stg = config.explainability.targets["stg"]
    assert (name, changed) == ("stg", True)
    assert stg.gateway_url == "https://stg-explainability-api.aisquare.studio"
    assert stg.proxy_url == "https://stg-explainability.api.aisquare.studio:9443"
    assert config.explainability.enabled is False, "picking a destination does not start tracing"
    # A hand-set gateway stays; a second call changes nothing.
    config.explainability.targets["prod"] = ExplainabilityTarget(gateway_url="https://mine.example")
    name, changed = dest.ensure_target(config, "https://api.aisquare.studio")
    assert (name, changed) == ("prod", True), "the proxy was empty and is filled"
    assert config.explainability.targets["prod"].gateway_url == "https://mine.example"
    assert dest.ensure_target(config, "https://api.aisquare.studio") == ("prod", False)
    # An unknown host: a target by host name, nothing filled in.
    name, changed = dest.ensure_target(config, "https://api.example.org")
    assert (name, changed) == ("api.example.org", True)
    assert config.explainability.targets["api.example.org"].gateway_url == ""


# --- listing through the session ---------------------------------------------------------------


def test_workspaces_and_studios_are_listed_through_the_session(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session
) -> None:
    idp.page_size = 2  # three workspaces: two pages, followed by number
    rows = _json(runner, "explainability", "workspaces")
    assert [r["name"] for r in rows] == ["acme", "Personal", "guests"], "members, then invites"
    assert rows[2]["member"] is False and rows[2]["invite_status"] == "pending"
    pages = [r for r in idp.requests if r["path"] == "/api/v2/workspaces/"]
    assert [r["query"]["page"] for r in pages] == ["1", "2"]
    assert all(r["headers"]["authorization"].startswith("Bearer aisq_") for r in pages)

    payload = _json(runner, "explainability", "studios", "--workspace", "acme")
    assert payload["workspace"] == {"id": 42, "uid": "ws-uid-42", "name": "acme"}
    assert [s["name"] for s in payload["studios"]] == ["API", "Frontend", "Unassigned"]
    assert payload["studios"][0]["default"] and payload["studios"][2]["inbox"]
    listing = [r for r in idp.requests if r["path"] == "/api/v2/publications/"]
    assert listing and all(r["headers"]["x-workspace-id"] == "ws-uid-42" for r in listing), (
        "the workspace goes out as the header, by uid"
    )
    assert all(r["query"]["scope"] == "workspace" for r in listing)

    human = runner.invoke(app, ["explainability", "workspaces"])
    assert "acme" in human.output and "invited (pending)" in human.output
    not_there = runner.invoke(app, ["explainability", "studios", "--workspace", "nope"])
    assert not_there.exit_code == 1 and "no workspace matches 'nope'" in not_there.output


def test_listing_needs_a_session(runner: CliRunner, isolated_home: Path) -> None:
    result = runner.invoke(app, ["--json", "explainability", "workspaces"])
    assert result.exit_code == 1 and "not_authenticated" in result.output


def test_picking_by_name_uid_or_id_and_the_ambiguous_and_missing_cases() -> None:
    workspaces = [
        dest.Workspace(id=1, uid="u1", name="Acme", role="OWNER"),
        dest.Workspace(id=2, uid="u2", name="acme", role="OWNER"),
        dest.Workspace(id=3, uid="u3", name="Other", role="OWNER"),
    ]
    assert dest.pick_workspace("u2", workspaces).id == 2
    assert dest.pick_workspace("3", workspaces).name == "Other"
    assert dest.pick_workspace("other", workspaces).id == 3, "names match case-insensitively"
    with pytest.raises(dest.DestinationError) as ambiguous:
        dest.pick_workspace("ACME", workspaces)
    assert ambiguous.value.code == "ambiguous"
    with pytest.raises(dest.DestinationError) as missing:
        dest.pick_workspace("nope", workspaces)
    assert missing.value.code == "not_found" and "Acme, acme, Other" in missing.value.message
    workspace = workspaces[2]
    studios = [
        dest.Studio(id=10, uid="s10", name="Inbox", is_inbox=True),
        dest.Studio(id=11, uid="s11", name="Web", is_default=True),
        dest.Studio(id=12, uid="s12", name="Docs"),
    ]
    assert dest.pick_studio(None, studios, workspace).id == 11, "no ref: the default studio"
    assert dest.pick_studio("docs", studios, workspace).id == 12
    with pytest.raises(dest.DestinationError) as needed:
        dest.pick_studio(None, [studios[0], studios[2]], workspace)
    assert needed.value.code == "studio_required" and "Docs" in needed.value.message


def test_a_destination_is_picked_among_your_workspaces_before_the_invitations() -> None:
    """A pending invitation named like a workspace you belong to made that name
    ``ambiguous`` for ``use``, which could then not choose it by name (review of #172)."""
    workspaces = [
        dest.Workspace(id=1, uid="u1", name="acme", role="ADMIN"),
        dest.Workspace(id=9, uid="u9", name="Acme", invite_status="pending"),
        dest.Workspace(id=5, uid="u5", name="guests", invite_status="pending"),
    ]
    assert dest.pick_workspace("ACME", workspaces, members_only=True).id == 1
    for invited in ("guests", "u9"):
        with pytest.raises(dest.DestinationError) as refused:
            dest.pick_workspace(invited, workspaces, members_only=True)
        assert refused.value.code == "not_a_member", invited
    with pytest.raises(dest.DestinationError) as missing:
        dest.pick_workspace("nope", workspaces, members_only=True)
    assert missing.value.code == "not_found" and "acme, Acme, guests" in missing.value.message
    with pytest.raises(dest.DestinationError) as listed:  # the listing's own view: both
        dest.pick_workspace("acme", workspaces)
    assert listed.value.code == "ambiguous"


# --- use: record, target, key, routing -------------------------------------------------------


def test_use_records_the_choice_targets_the_deployment_mints_a_key_and_binds_the_roster(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    project = _project(tmp_path / "web")
    payload = _json(runner, "explainability", "use", "acme/Frontend")

    # Recorded per project, from the session.
    assert payload["destination"]["workspace"]["name"] == "acme"
    assert payload["destination"]["studio"] == {"id": 301, "uid": "st-301", "name": "Frontend"}
    assert payload["destination"]["environment"] == "local", "the stub is a loopback host"
    assert payload["destination"]["set_by"] == "anmol@example.com"
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.api_url == idp.url and row.key_uid == "key-1"

    # The deployment became a target, gateway and proxy filled, tracing untouched.
    settings = load_config().explainability
    assert settings.targets["local"].gateway_url == "http://localhost:8000"
    assert settings.targets["local"].proxy_url == "http://127.0.0.1:9090"
    assert settings.enabled is False
    assert payload["target"] == {
        "name": "local",
        "gateway": "http://localhost:8000",
        "proxy": "http://127.0.0.1:9090",
        "enabled": False,
    }

    # The key: minted with the ingest scope only, stored the #141 way, never in the output.
    minted = idp.minted[0]
    assert minted["scopes"] == ["ingest:write"] and minted["workspace_id"] == 42
    assert minted["name"].startswith("aisquare-cli ") and minted["name"].endswith(" web")
    key_file = service.project_key_path(project.id)
    assert key_file.read_text() == minted["api_key"]
    if sys.platform != "win32":  # NTFS keeps one bit of the mode: 0o666 or 0o444
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert minted["api_key"] not in json.dumps(payload)
    assert payload["key"] == {
        "source": "project",
        "minted": True,
        "note": f"minted on your behalf → {key_file} (mode 600)",
    }
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.target == "local" and binding.key_path == key_file

    # The roster is bound to the studio, with the key, name by name.
    target = ops.resolve_target(settings, None, project_id=project.id)
    assert target.name == "local" and target.key_source == "project"
    assert target.destination is not None and target.destination.studio_id == 301
    assert [b["agent"] for b in payload["routing"]] == list(target.agent_names)
    assert all(b["bound"] and b["studio_id"] == 301 for b in payload["routing"])
    assert idp.bindings["42"] == dict.fromkeys(target.agent_names, 301)
    puts = [r for r in idp.requests if r["method"] == "PUT"]
    assert puts and all(r["headers"]["x-api-key"] == minted["api_key"] for r in puts)
    assert all("authorization" not in r["headers"] for r in puts), "the key, never the session"

    # The other surfaces say so.
    status = _json(runner, "explainability", "status")
    assert status["destination"]["studio"]["name"] == "Frontend" and status["target"] == "local"
    human = runner.invoke(app, ["explainability", "status"])
    assert (
        "destination: acme / Frontend · local · chosen by anmol@example.com — key minted"
        in human.output
    )
    who = runner.invoke(app, ["whoami"])
    assert "traces: acme / Frontend · local" in who.output
    assert _json(runner, "whoami")["destination"]["workspace"]["id"] == 42

    # Idempotent: the same choice again changes nothing and mints nothing more.
    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert again["key"]["minted"] is False and len(idp.minted) == 1

    # logout forgets the minted key — file, binding, uid — and revokes it first.
    out = _json(runner, "logout")
    assert out["minted_keys_cleared"] == 1 and out["signed_out"] is True
    assert idp.revoked_keys == ["key-1"]
    assert not key_file.exists()
    with store_session() as store:
        assert store.project_explainability(project.id) is None
        kept = store.project_destination(project.id)
    assert kept is not None and kept.key_uid is None, "the destination itself survives a logout"
    revoke_index = idp.paths().index("/api/v2/iam/workspace-api-key/key-1/revoke/")
    assert revoke_index < idp.paths().index("/o/revoke_token/"), "keys go before the session"


def test_use_when_the_api_refuses_the_token_names_the_gap_and_still_records(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    idp.key_mint = "token_not_valid"  # what the real API says today
    project = _project(tmp_path / "web")
    result = runner.invoke(app, ["explainability", "use", "acme"])  # the default studio: API
    assert result.exit_code == 0, result.output
    assert "✓ traces from web land in acme / API (local)" in result.output
    assert "AISquare-Studio-BE#3493" in result.output and "key set --from-env" in result.output
    assert "routing:  not applied — no key" in result.output
    assert not service.project_key_path(project.id).exists()
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.studio_id == 303 and row.key_uid is None
    assert not idp.bindings, "nothing bound without a key"
    status = runner.invoke(app, ["explainability", "status"])
    expected = "destination: acme / API · local · chosen by anmol@example.com — no key yet"
    assert expected in status.output


def test_a_hand_attached_key_binds_the_roster_and_outlives_logout(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idp.key_mint = "token_not_valid"
    idp.accepted_keys.append("AIS_handmade_key")
    project = _project(tmp_path / "web")
    # The #141 way, bound to the deployment the session belongs to.
    config = load_config()
    dest.ensure_target(config, idp.url)
    config.explainability.target = "local"
    save_config(config)
    monkeypatch.setenv("WEB_KEY", "AIS_handmade_key")
    attached = runner.invoke(app, ["explainability", "key", "set", "--from-env", "WEB_KEY"])
    assert attached.exit_code == 0, attached.output

    payload = _json(runner, "explainability", "use", "acme/Frontend")
    assert payload["key"] == {"source": "project", "minted": False, "note": "the project's own key"}
    assert idp.minted == [], "a key at hand is used; none is minted"
    assert all(b["bound"] for b in payload["routing"]) and idp.bindings["42"]
    human = runner.invoke(app, ["explainability", "status"])
    assert "— key attached by hand" in human.output

    out = _json(runner, "logout")
    assert out["minted_keys_cleared"] == 0
    assert service.project_key_path(project.id).read_text() == "AIS_handmade_key"


def test_a_routing_refusal_is_reported_per_identity_not_raised(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    idp.routing = "forbidden"
    _project(tmp_path / "web")
    payload = _json(runner, "explainability", "use", "acme/Frontend")
    assert payload["routing"] and all(b["bound"] is False for b in payload["routing"])
    assert all("OWNER/ADMIN" in b["detail"] for b in payload["routing"])
    assert payload["destination"]["studio"]["name"] == "Frontend", "recorded all the same"


def test_repointing_and_clearing(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    key_file = service.project_key_path(project.id)
    assert key_file.exists() and len(idp.minted) == 1
    # Another studio in the same workspace keeps the minted key.
    again = _json(runner, "explainability", "use", "acme/API")
    assert again["key"]["minted"] is False and key_file.exists()
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.studio_name == "API" and row.key_uid == "key-1"
    # A workspace with no studios cannot be chosen; the old choice stands.
    refused = runner.invoke(app, ["explainability", "use", "Personal"])
    assert refused.exit_code == 1 and "name the studio: Personal has no single default" in (
        refused.output
    )
    with store_session() as store:
        assert store.project_destination(project.id) == row
    # A pending invitation is listed but is not somewhere traces can go.
    invited = runner.invoke(app, ["--json", "explainability", "use", "guests/anything"])
    assert invited.exit_code == 1 and "not_a_member" in invited.output
    # Another workspace: the old workspace's minted key is revoked, not just dropped.
    idp.studios["7"] = [{"id": 701, "uid": "st-701", "name": "Notes", "workspace_id": 7}]
    moved = _json(runner, "explainability", "use", "Personal/Notes")
    assert moved["key"]["minted"] is True and idp.revoked_keys == ["key-1"]
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.workspace_name == "Personal" and row.key_uid == "key-2"
    # Clearing drops the row and the minted key, and revokes it.
    cleared = _json(runner, "explainability", "use", "--clear")
    assert cleared["cleared"]["studio"]["name"] == "Notes"
    assert idp.revoked_keys == ["key-1", "key-2"]
    assert not key_file.exists()
    with store_session() as store:
        assert store.project_destination(project.id) is None
        assert store.project_explainability(project.id) is None
    assert runner.invoke(app, ["explainability", "use", "--clear"]).output.strip() == (
        "web had no destination"
    )
    assert "destination: (none chosen" in runner.invoke(app, ["explainability", "status"]).output


def test_use_needs_a_destination_argument(
    runner: CliRunner, isolated_home: Path, tmp_path: Path
) -> None:
    _project(tmp_path / "web")
    result = runner.invoke(app, ["explainability", "use"])
    assert result.exit_code == 1 and "name a destination" in result.output


# --- the resolver ----------------------------------------------------------------------------


def test_the_destination_names_the_target_between_the_explicit_forms_and_the_default(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path / "web")
    config = AppConfig()
    config.explainability.target = "prod"
    config.explainability.targets = {
        "prod": ExplainabilityTarget(gateway_url="https://prod.example"),
        "stg": ExplainabilityTarget(gateway_url="https://stg.example"),
    }
    save_config(config)
    session = iam.Session(api_url="https://stg-api.aisquare.studio", token="aisq_x", source="env")
    workspace = dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN")
    studio = dest.Studio(id=301, uid="st-301", name="Frontend")
    with store_session() as store:
        dest.choose(store, project, workspace, studio, session)

    settings = config.explainability
    machine = ops.resolve_target(settings, None)
    assert machine.name == "prod" and machine.destination is None, "no project: the machine default"
    projected = ops.resolve_target(settings, None, project_id=project.id)
    assert projected.name == "stg" and projected.gateway_url == "https://stg.example"
    assert projected.destination is not None and projected.destination.workspace_name == "acme"
    assert (machine.target_source, projected.target_source) == ("config", "destination")
    explicit = ops.resolve_target(settings, "prod", project_id=project.id)
    assert (explicit.name, explicit.target_source) == ("prod", "argument"), "--target still wins"
    monkeypatch.setenv(ops.TARGET_ENV_VAR, "prod")
    by_variable = ops.resolve_target(settings, None, project_id=project.id)
    assert (by_variable.name, by_variable.target_source) == ("stg", "destination"), (
        "the variable moves the machine's target, not a project's destination"
    )
    assert by_variable.unused_env_target == "prod", "and the resolution says it is not used"
    assert ops.resolve_target(settings, None).name == "prod", "the machine still follows it"


def test_describe_has_one_voice() -> None:
    assert dest.describe(None).startswith("(none chosen")
    session = iam.Session(
        api_url="https://api.aisquare.studio", token="t", source="env", email="a@b.c"
    )
    row = TraceDestination(
        project_id="p",
        api_url=session.api_url,
        environment="prod",
        workspace_id=1,
        workspace_name="acme",
        studio_id=2,
        studio_name="Web",
        set_at=__import__("datetime").datetime(2026, 9, 13, tzinfo=__import__("datetime").UTC),
        set_by="a@b.c",
    )
    assert dest.describe(row, key_source="unset") == (
        "acme / Web · prod · chosen by a@b.c — no key yet"
    )
    assert dest.describe(row, key_source="file").endswith("— using the machine key")
    minted = row.model_copy(update={"key_uid": "k"})
    assert dest.describe(minted).endswith("— key minted by the CLI")


# --- the key never crosses a deployment or a workspace ---------------------------------------


def test_a_target_use_creates_names_its_own_key_variable(isolated_home: Path) -> None:
    """The unlabelled machine key answers only for the deployment it already served."""
    config = AppConfig()
    dest.ensure_target(config, "https://api.aisquare.studio")
    assert config.explainability.targets["prod"].api_key_env == "EXPLAINABILITY_PROD_API_KEY"
    dest.ensure_target(config, "https://api.example.org")
    created = config.explainability.targets["api.example.org"]
    assert created.api_key_env == "EXPLAINABILITY_API_EXAMPLE_ORG_API_KEY"
    # The single-deployment machine `init --explainability` wrote for staging keeps its key.
    single = AppConfig()
    single.explainability.gateway_url = "https://stg-explainability-api.aisquare.studio/"
    dest.ensure_target(single, "https://stg-api.aisquare.studio")
    assert single.explainability.targets["stg"].api_key_env == service.KEY_ENV_VAR
    # A target the operator wrote is theirs, variable and all.
    mine = AppConfig()
    mine.explainability.targets["prod"] = ExplainabilityTarget(gateway_url="https://mine.example")
    dest.ensure_target(mine, "https://api.aisquare.studio")
    assert mine.explainability.targets["prod"].api_key_env == service.KEY_ENV_VAR


def test_another_deployments_machine_key_is_never_used_for_the_destination(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The machine key file (and the default variable) belong to the machine's deployment."""
    foreign = "AIS_other_deployment_key"
    service.store_api_key(foreign)
    monkeypatch.setenv(service.KEY_ENV_VAR, foreign)
    project = _project(tmp_path / "web")
    payload = _json(runner, "explainability", "use", "acme/Frontend")
    assert payload["key"]["source"] == "project" and payload["key"]["minted"] is True
    assert len(idp.minted) == 1, "the project had no key of its own, so one was minted"
    puts = [r for r in idp.requests if r["method"] == "PUT"]
    assert puts and all(r["headers"]["x-api-key"] == idp.minted[0]["api_key"] for r in puts)
    assert load_config().explainability.targets["local"].api_key_env == (
        "EXPLAINABILITY_LOCAL_API_KEY"
    )
    # Refused mint: the foreign key still does not answer, so nothing is bound with it.
    idp.key_mint = "token_not_valid"
    _json(runner, "explainability", "use", "--clear")
    refused = _json(runner, "explainability", "use", "acme/Frontend")
    assert refused["key"]["source"] == "unset", "the unlabelled key crossed deployments"
    assert refused["routing"] == []
    resolved = ops.resolve_target(load_config().explainability, None, project_id=project.id)
    assert resolved.api_key is None


def test_a_machine_key_named_for_the_deployment_is_used_meanwhile_and_said_unchecked(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the PROJECT's key is the destination's credential: a machine key never skips a mint."""
    idp.accepted_keys.append("AIS_machine_local_key")
    monkeypatch.setenv("EXPLAINABILITY_LOCAL_API_KEY", "AIS_machine_local_key")
    _project(tmp_path / "web")
    minted = _json(runner, "explainability", "use", "acme/Frontend")
    assert minted["key"]["source"] == "project" and len(idp.minted) == 1

    _json(runner, "explainability", "use", "--clear")
    idp.key_mint = "token_not_valid"
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    assert "AISquare-Studio-BE#3493" in result.output
    assert "meanwhile $EXPLAINABILITY_LOCAL_API_KEY from this shell answers" in result.output
    assert "not checked to be acme's" in result.output


def test_a_machine_key_binds_no_roster_for_the_destination(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``use`` said the machine key was "not checked to be acme's" and bound acme's
    roster with it all the same (review of #172)."""
    idp.key_mint = "token_not_valid"
    idp.accepted_keys.append("AIS_machine_local_key")
    monkeypatch.setenv("EXPLAINABILITY_LOCAL_API_KEY", "AIS_machine_local_key")
    _project(tmp_path / "web")
    payload = _json(runner, "explainability", "use", "acme/Frontend")
    assert payload["key"]["source"] == "env" and payload["routing"] == []
    assert not [r for r in idp.requests if r["method"] == "PUT"], "bound with an unchecked key"
    human = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert "routing:  not applied — a machine key is not checked to be acme's" in human.output


def test_the_projects_own_key_with_nothing_to_bind_is_not_called_a_machine_key(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """The routing line said "a machine key is not checked to be acme's" whenever
    nothing was bound, the project's own freshly minted key included (review of
    #172's follow-ups, round 1, F4)."""
    config = load_config()
    config.explainability.agent_name_template = "aisquare-{team}"  # renders no name
    save_config(config)
    _project(tmp_path / "web")
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    assert "key:      minted on your behalf" in result.output
    assert "machine key" not in result.output
    assert "routing:  not applied — the identity template renders no agent" in result.output
    assert not [r for r in idp.requests if r["method"] == "PUT"]


def test_a_hand_key_kept_across_a_workspace_change_is_named_as_such(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idp.key_mint = "token_not_valid"
    idp.studios["7"] = [{"id": 701, "uid": "st-701", "name": "Notes", "workspace_id": 7}]
    _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    monkeypatch.setenv("WEB_KEY", "AIS_handmade_key")
    assert (
        runner.invoke(app, ["explainability", "key", "set", "--from-env", "WEB_KEY"]).exit_code == 0
    )
    moved = _json(runner, "explainability", "use", "Personal/Notes")
    assert moved["key"]["source"] == "project"
    assert "attached by hand while it pointed at acme / Frontend" in moved["key"]["note"]
    assert "if it is not Personal's" in moved["key"]["note"]


# --- the documented fallback: `use`, a refused mint, then `key set` -----------------------------


def test_key_set_after_a_refused_mint_binds_the_destinations_deployment(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Out of the box the machine default (`stg`) is not the session's deployment."""
    idp.key_mint = "token_not_valid"
    project = _project(tmp_path / "web")
    assert load_config().explainability.target == "stg"
    _json(runner, "explainability", "use", "acme/Frontend")
    monkeypatch.setenv("WEB_KEY", "AIS_handmade_key")
    attached = _json(runner, "explainability", "key", "set", "--from-env", "WEB_KEY")
    assert attached["target"] == "local", "the key went to the machine's target, not the project's"
    resolved = ops.resolve_target(load_config().explainability, None, project_id=project.id)
    assert (resolved.name, resolved.key_source) == ("local", "project")
    assert "— key attached by hand" in runner.invoke(app, ["explainability", "status"]).output
    # An explicit --target still wins.
    explicit = _json(
        runner, "explainability", "key", "set", "--from-env", "WEB_KEY", "--target", "stg"
    )
    assert explicit["target"] == "stg"


# --- the next step `use` names is a check that passes for what it set up ------------------------


def _trace_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracing enabled, and the destination's proxy answering (nothing listens in a test)."""
    config = load_config()
    config.explainability.enabled = True
    save_config(config)
    monkeypatch.setattr(ops, "probe_proxy", lambda _url: service.ProxyProbe(True, "proxy healthy"))


def _next_step(output: str) -> list[str]:
    """The command on `use`'s ``next:`` line, as the app's argv: no ``aisquare``, no note."""
    line = next(ln for ln in output.splitlines() if ln.strip().startswith("next:"))
    argv = shlex.split(line.split("next:", 1)[1].split("   (", 1)[0])
    assert argv[0] == "aisquare", line
    return argv[1:]


def _gateway_posts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str | None]]:
    """The gateway as ``doctor --live`` meets it, answering; each span it posts is recorded
    as the project the key was resolved for and the key it carried."""
    posted: list[tuple[str | None, str | None]] = []

    def ingest(target: ops.ResolvedTarget, identity: str) -> ops.HttpVerdict:
        posted.append((target.project_id, target.api_key))
        return ops.HttpVerdict(ok=True, status=202, detail="accepted")

    monkeypatch.setattr(ops, "probe_ready", lambda *_a, **_k: ops.HttpVerdict(True, 200, "ok"))
    monkeypatch.setattr(ops, "probe_ingest", ingest)
    return posted


def test_the_next_step_for_the_projects_key_puts_that_key_to_the_gateway(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`use` named `explainability status`, which resolves the project's key and only
    probes the proxy: a revoked key or another workspace's passed it (review of #172,
    D2 round 2). `doctor --live --project` posts a span with the key the project's
    launches take. The project is named by id whether `use` was given one or not (D7)."""
    _trace_on(monkeypatch)
    posted = _gateway_posts(monkeypatch)
    lib = _project(tmp_path / "lib")
    web = _project(tmp_path / "web")  # the checkout the commands run from
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    step = _next_step(result.output)
    assert step == ["doctor", "--live", "--project", web.id]
    runner.invoke(app, step)
    minted = service.project_key_path(web.id).read_text(encoding="utf-8")
    assert posted == [(web.id, minted)], "the span went out with the project's own key"

    # For a project other than this checkout's, the check is about THAT project.
    other = runner.invoke(app, ["explainability", "use", "--project", "lib", "acme/API"])
    assert other.exit_code == 0, other.output
    step = _next_step(other.output)
    assert step == ["doctor", "--live", "--project", lib.id]
    posted.clear()
    runner.invoke(app, step)
    assert posted == [(lib.id, service.project_key_path(lib.id).read_text(encoding="utf-8"))]


def test_with_no_key_the_next_step_attaches_one_to_the_destination(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not `doctor`: its remedy is a machine key, which never stands in for the project's.
    The step runs as printed, with the key on stdin: it printed `--from-env VAR`, a
    placeholder presented as a command (review of #172, D2 round 2, D8)."""
    _trace_on(monkeypatch)
    idp.key_mint = "token_not_valid"
    project = _project(tmp_path / "web")
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    step = _next_step(result.output)
    assert step == ["explainability", "key", "set", "--project", project.id]
    assert "VAR" not in next(ln for ln in result.output.splitlines() if "next:" in ln)
    assert runner.invoke(app, step, input="AIS_handmade_key\n").exit_code == 0
    resolved = ops.resolve_target(load_config().explainability, None, project_id=project.id)
    assert (resolved.name, resolved.key_source) == ("local", "project")
    again = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert _next_step(again.output) == ["doctor", "--live", "--project", project.id]


def test_the_next_step_for_a_machine_key_is_doctor_and_doctor_resolves_it(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine key is doctor's to put to the gateway: it resolves the same key `use` did."""
    _trace_on(monkeypatch)
    idp.key_mint = "token_not_valid"
    monkeypatch.setenv("EXPLAINABILITY_LOCAL_API_KEY", "AIS_machine_local_key")
    web = _project(tmp_path / "web")
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    step = _next_step(result.output)
    assert step == ["doctor", "--live", "--project", web.id]
    # Offline: the rows --live adds dial the gateway. The config row is the one
    # that failed when this line named doctor for a project's key.
    rows = {check.name: check for check in ops.checks(project_id=step[-1])}
    assert rows["explainability config"].status == "ok", rows["explainability config"].detail


# --- the proxy comes from the same target as the key ---------------------------------------------


def _two_deployments(tmp_path: Path) -> ProjectInfo:
    """Machine default stg; the project's destination, and its key, on prod."""
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    config.explainability.targets = {
        "stg": ExplainabilityTarget(
            gateway_url="https://stg.example",
            proxy_url="https://stg-proxy.example:9443",
            api_key_env="STG_KEY",
        ),
        "prod": ExplainabilityTarget(
            gateway_url="https://prod.example",
            proxy_url="https://prod-proxy.example:9443",
            api_key_env="PROD_KEY",
            agent_name_template="prod-{role}",
        ),
    }
    save_config(config)
    project = _project(tmp_path / "web")
    session = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")
    with store_session() as store:
        dest.choose(
            store,
            project,
            dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN"),
            dest.Studio(id=301, uid="st-301", name="Frontend"),
            session,
        )
        path = service.store_project_api_key(project.id, "AIS_prod_key")
        store.set_project_explainability(project.id, target="prod", key_path=path, set_by=None)
    return project


def test_the_wiring_takes_the_destinations_proxy_with_its_key(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _two_deployments(tmp_path)
    asked: list[str] = []

    def healthy(url: str) -> service.ProxyProbe:
        asked.append(url)
        return service.ProxyProbe(True, "proxy healthy")

    monkeypatch.setattr(service, "probe_proxy", healthy)
    settings = load_config().explainability
    folded = ops.effective_settings(settings, project_id=project.id)
    assert (folded.proxy_url, folded.agent_name_template) == (
        "https://prod-proxy.example:9443",
        "prod-{role}",
    )
    payload = _json(runner, "explainability", "env", "coder")
    exported = payload["env"]
    assert exported["ANTHROPIC_BASE_URL"] == "https://prod-proxy.example:9443", (
        "the prod key was wired to another deployment's proxy"
    )
    assert "X-AISquare-Key: AIS_prod_key" in exported["ANTHROPIC_CUSTOM_HEADERS"]
    assert "X-Agent-Name: prod-coder" in exported["ANTHROPIC_CUSTOM_HEADERS"]
    assert asked == ["https://prod-proxy.example:9443"]


def test_an_untraced_launch_names_the_key_variable_its_target_reads(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target with a variable of its own — every one ``use`` creates — reads that and
    never the key file, and the untraced line sent the operator to export
    EXPLAINABILITY_API_KEY or write the key file: the two places it never reads
    (review of #172)."""
    _two_deployments(tmp_path)
    assert runner.invoke(app, ["explainability", "key", "clear"]).exit_code == 0
    monkeypatch.setattr(service, "probe_proxy", lambda _url: service.ProxyProbe(True, "healthy"))
    result = runner.invoke(app, ["explainability", "env", "coder"])
    assert result.exit_code != 0
    reason = " ".join(result.output.split())
    assert "Set the key: export PROD_KEY=…; or attach this project's own" in reason
    assert service.KEY_ENV_VAR not in reason and "explainability-key" not in reason


def test_every_wiring_folds_the_same_project_it_resolves_the_key_for() -> None:
    """Launch, spawn and `env` each pair `effective_settings` with `resolve_target`.

    A function that resolves the key FOR a project and folds the proxy without
    it wires one deployment's key to another's proxy (#142). Source-level, so
    the launch and spawn paths — which exec an agent — are held to it too.
    """
    root = Path(aisquare.__file__).parent
    offences: list[str] = []
    checked = 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            calls = [c for c in ast.walk(func) if isinstance(c, ast.Call)]
            for_project = any(
                _calls(c, "resolve_target") and _passes(c, "project_id") for c in calls
            )
            folds = [c for c in calls if _calls(c, "effective_settings")]
            if not (for_project and folds):
                continue
            checked += 1
            offences.extend(
                f"{path.relative_to(root)}:{fold.lineno} in {func.name}"
                for fold in folds
                if not _passes(fold, "project_id")
            )
    assert checked >= 3, "launch, spawn and env should all have been examined"
    assert not offences, offences


def _calls(call: ast.Call, name: str) -> bool:
    func = call.func
    return (isinstance(func, ast.Name) and func.id == name) or (
        isinstance(func, ast.Attribute) and func.attr == name
    )


def _passes(call: ast.Call, keyword: str) -> bool:
    return any(k.arg == keyword for k in call.keywords)


# --- the minted key and the hand key share one file ------------------------------------------


def test_a_hand_key_over_a_minted_one_is_the_operators(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    monkeypatch.setenv("WEB_KEY", "AIS_handmade_key")
    attached = runner.invoke(app, ["explainability", "key", "set", "--from-env", "WEB_KEY"])
    assert attached.exit_code == 0, attached.output
    assert idp.revoked_keys == ["key-1"], "the minted key it replaced is revoked"
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.key_uid is None
    assert "— key attached by hand" in runner.invoke(app, ["explainability", "status"]).output

    out = _json(runner, "logout")
    assert out["minted_keys_cleared"] == 0
    assert service.project_key_path(project.id).read_text() == "AIS_handmade_key"


def test_a_hand_key_that_does_not_land_leaves_the_minted_one_live_and_minted(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The minted key is revoked once its replacement is recorded, never before (review of #172).

    ``key set`` revoked it and dropped its uid, then wrote; when the binding
    could not be recorded, the write put the file back as it was — the key just
    revoked, no longer called minted. ``use`` then called that dead key "the
    project's own key" and never minted again, and ``logout`` left it alone.
    """
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    minted = service.project_key_path(project.id).read_text()

    def locked(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "set_project_explainability", locked)
    monkeypatch.setenv("WEB_KEY", "AIS_handmade_key")
    failed = runner.invoke(app, ["explainability", "key", "set", "--from-env", "WEB_KEY"])
    assert failed.exit_code != 0
    assert idp.revoked_keys == [], "a key revoked before its replacement was in place"
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.key_uid == "key-1", "the key in the file stopped being minted"
    assert service.project_key_path(project.id).read_text() == minted


def test_key_clear_retires_a_minted_key(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    assert _json(runner, "explainability", "key", "clear")["cleared"] is True
    assert idp.revoked_keys == ["key-1"]
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.key_uid is None
    assert "— no key yet" in runner.invoke(app, ["explainability", "status"]).output


def test_a_key_clear_that_does_not_land_leaves_the_minted_key_live_and_minted(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``key clear`` revokes once the binding is gone: a failed clear keeps a working key.

    Revoked first, a clear whose row would not delete left the binding on a
    dead key with no uid — the state a failed ``key set`` left (review of #172).
    """
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")

    def locked(*_args: object, **_kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "clear_project_explainability", locked)
    assert runner.invoke(app, ["explainability", "key", "clear"]).exit_code != 0
    assert idp.revoked_keys == []
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.key_uid == "key-1"
    assert service.project_key_path(project.id).read_text() == idp.minted[0]["api_key"]


def test_the_cli_never_mints_over_a_hand_key_bound_to_another_target(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path / "web")
    monkeypatch.setenv("WEB_KEY", "AIS_stg_handmade_key")
    attached = runner.invoke(
        app, ["explainability", "key", "set", "--from-env", "WEB_KEY", "--target", "stg"]
    )
    assert attached.exit_code == 0, attached.output
    payload = _json(runner, "explainability", "use", "acme/Frontend")
    assert idp.minted == [], "no key is created only to overwrite the operator's"
    assert "attached by hand for target stg" in payload["key"]["note"]
    assert service.project_key_path(project.id).read_text() == "AIS_stg_handmade_key"
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.target == "stg"


def test_the_ui_attach_leaves_a_minted_key_to_the_cli(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """The tab's one project-key writer never overwrites a key the CLI minted.

    Revoking it is a network call and the tab's handlers run on the UI thread,
    so the refusal names ``key set``, which revokes it. Which deployment the
    form binds a key to is the form's question (tests/test_ui_project.py).
    """
    from aisquare.cli.ui.views import explainability as view

    idp.key_mint = "token_not_valid"
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    attached = view.attach_project_key("AIS_pasted_key", project, "local")
    assert attached.severity == "information" and "for target local" in attached.message
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.target == "local"

    idp.key_mint = "ok"
    runner.invoke(app, ["explainability", "key", "clear"])
    _json(runner, "explainability", "use", "acme/Frontend")
    assert view.minted_key_refusal(project) is not None
    refused = view.attach_project_key("AIS_pasted_again", project, "local")
    assert refused.severity == "warning" and "minted by the CLI" in refused.message
    assert service.project_key_path(project.id).read_text() == idp.minted[0]["api_key"]


def test_the_tabs_minted_key_check_creates_no_store_on_a_fresh_machine(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The Setup form asks before any write; its store session created ``context.db`` on a
    machine where nothing had been saved yet — a read that creates (review of #172)."""
    from aisquare.cli.ui.views import explainability as view

    root = tmp_path / "fresh"
    fresh = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    assert not paths.db_path().exists()
    assert view.minted_key_refusal(fresh) is None
    assert not paths.db_path().exists()


def test_the_tabs_attach_asks_about_a_minted_key_inside_the_writers_own_session(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One store session per attach, on the UI thread: the check opened one of its own
    before the writer opened another (review of #172). Refused all the same."""
    from aisquare.cli.ui.views import explainability as view
    from aisquare.core import store as store_module

    project = _project(tmp_path / "web")
    # The deployment the key is bound to must be one this machine has (the
    # writer's known-target rule): `use` creates it, without minting here.
    _json(runner, "explainability", "use", "acme/Frontend", "--no-key")
    opened: list[int] = []
    real_open = store_module.open_store

    def counted() -> ContextStore:
        opened.append(1)
        return real_open()

    with monkeypatch.context() as counting:
        counting.setattr(store_module, "open_store", counted)
        attached = view.attach_project_key("AIS_pasted_key", project, "local")
    assert attached.severity == "information", attached.message
    assert len(opened) == 1, "a store session for the check, and another for the write"

    runner.invoke(app, ["explainability", "key", "clear"])
    _json(runner, "explainability", "use", "acme/Frontend")
    opened.clear()
    with monkeypatch.context() as counting:
        counting.setattr(store_module, "open_store", counted)
        refused = view.attach_project_key("AIS_pasted_again", project, "local")
    assert refused.severity == "warning" and "minted by the CLI" in refused.message
    assert len(opened) == 1
    assert service.project_key_path(project.id).read_text() == idp.minted[0]["api_key"]


def test_use_status_and_whoami_ask_about_this_checkout_not_the_pin(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """Without ``--project`` every surface here names the project a launch from here joins.

    ``use`` and ``status`` default to it through the key commands' resolver
    (review of #170); ``whoami`` read the ``project switch`` pin, so with
    another project pinned it reported that one's destination — none — right
    after ``use`` had recorded this checkout's.
    """
    web = _project(tmp_path / "web")
    lib = _project(tmp_path / "lib")
    pin_project(lib.id)
    _json(runner, "explainability", "use", "acme/Frontend", "--no-key")
    with store_session() as store:
        assert store.project_destination(web.id) is not None
        assert store.project_destination(lib.id) is None, "the pin is not where launches go"
    assert _json(runner, "explainability", "status")["destination"]["studio"]["name"] == "Frontend"
    assert _json(runner, "whoami")["destination"]["studio"]["name"] == "Frontend"
    assert "traces: acme / Frontend" in runner.invoke(app, ["whoami"]).output


# --- revocation goes where the key was minted ---------------------------------------------------


def test_a_repoint_keeps_a_key_only_on_the_same_api(isolated_home: Path, tmp_path: Path) -> None:
    """Workspace ids are per deployment: staging 42 and production 42 are two workspaces."""
    project = _project(tmp_path / "web")
    stg = iam.Session(api_url="https://stg-api.aisquare.studio", token="aisq_x", source="env")
    prod = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")
    workspace = dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN")
    studio = dest.Studio(id=301, uid="st-301", name="Frontend")
    with store_session() as store:
        dest.choose(store, project, workspace, studio, stg)
        store.set_project_destination_key(project.id, "key-stg")
        previous = store.project_destination(project.id)
        moved = dest.choose(store, project, workspace, studio, prod, previous=previous)
    assert moved.key_uid is None, "a staging key's uid carried onto a production destination"


def _minted_by_hand(
    store: ContextStore, project: ProjectInfo, session: iam.Session, uid: str
) -> None:
    """``project`` pointed at acme on ``session``'s API, its file holding the key ``uid`` names."""
    workspace = dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN")
    dest.choose(
        store, project, workspace, dest.Studio(id=301, uid="st-301", name="Frontend"), session
    )
    path = service.store_project_api_key(project.id, f"AIS_{uid}_{'x' * 20}")
    store.set_project_explainability(
        project.id, target="local", key_path=path, set_by=None, minted=uid
    )


def test_logout_revokes_where_each_key_was_minted_and_keeps_owing_the_rest(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every minted key is detached; one this session cannot revoke is owed, not forgotten.

    A key minted on another host was dropped from the store unrevoked — a live
    ``ingest:write`` key nothing on this machine remembered (review of #172). It
    is detached like the others, stays owed, and ``logout`` names it. A key file
    that will not delete does not keep the other projects' keys, and its own key
    is revoked all the same: nothing binds the file any more.
    """
    here = _project(tmp_path / "here")
    elsewhere = _project(tmp_path / "elsewhere")
    stuck = _project(tmp_path / "stuck")
    other = iam.Session(api_url="https://stg-api.aisquare.studio", token="aisq_y", source="env")
    with store_session() as store:
        _minted_by_hand(store, here, signed_in, "key-here")
        _minted_by_hand(store, elsewhere, other, "key-elsewhere")
        _minted_by_hand(store, stuck, signed_in, "key-stuck")
    real_clear = service.clear_project_api_key

    def refuse_one(project_id: str) -> bool:
        if project_id == stuck.id:
            raise PermissionError("read-only")
        return real_clear(project_id)

    monkeypatch.setattr(dest, "clear_project_api_key", refuse_one)
    out = _json(runner, "logout")

    assert out["minted_keys_cleared"] == 3
    assert sorted(idp.revoked_keys) == ["key-here", "key-stuck"]
    assert "key-elsewhere" not in idp.revoked_keys, "a key uid sent to a host that never minted it"
    [owed] = out["minted_keys_still_live"]
    assert owed["key_uid"] == "key-elsewhere" and owed["api_url"] == other.api_url
    assert f"signed in to {idp.url}, not {other.api_url}" in owed["reason"]
    with store_session() as store:
        assert [record.key_uid for record in store.pending_revocations()] == ["key-elsewhere"]
        assert not any(d.key_uid for d in store.project_destinations())
        assert store.project_explainability_all() == []
    assert not service.project_key_path(here.id).exists()
    assert service.project_key_path(stuck.id).exists(), "left on disk, bound to nothing"


def test_logout_counts_a_minted_uid_whose_binding_was_already_gone(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """``detach`` answered whether a binding existed, not whether a uid was detached:
    a uid with no binding left was detached, owed and revoked, and ``logout`` said
    it had forgotten none (review of #172's follow-ups, round 1, F5)."""
    project = _project(tmp_path / "web")
    workspace = dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN")
    with store_session() as store:
        dest.choose(
            store, project, workspace, dest.Studio(id=301, uid="st-301", name="Frontend"), signed_in
        )
        store.set_project_destination_key(project.id, "key-1")  # no binding names it
    out = _json(runner, "logout")
    assert out["minted_keys_cleared"] == 1
    assert idp.revoked_keys == ["key-1"]


def test_a_revoke_names_the_workspace_the_key_was_minted_in(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """The revoke went out with no ``X-Workspace-Id``: an endpoint that took its
    context from the header would have answered from the caller's personal workspace,
    where a 404 settles nothing (review of #172's follow-ups, round 1, F1)."""
    _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    cleared = _json(runner, "explainability", "use", "--clear")
    assert cleared["revocations"]["revoked"] == ["key-1"]
    [revoke] = [r for r in idp.requests if r["path"].endswith("/revoke/")]
    assert revoke["headers"].get("x-workspace-id") == "42"


def test_a_revoke_whose_answer_is_cut_short_keeps_that_key_owed_after_logout(
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_revoke`` tolerates an unreachable server as ``IamError``, and ``http.client``'s
    own exceptions escaped ``iam._http`` past it: one truncated answer to a revoke ended
    ``logout``'s loop over the minted keys, and every key after it stayed on disk after
    the sign-out (review of the accounts stack's fold, round 1, F2). The key whose
    answer never arrived may or may not be revoked, so it stays owed rather than
    forgotten (review of #172), and the next pass settles it either way."""
    import urllib.request
    from http.client import IncompleteRead

    projects = [_project(tmp_path / name) for name in ("first", "second")]
    with store_session() as store:
        for project in projects:
            _minted_by_hand(store, project, signed_in, f"key-{project.root.name}")
    real_urlopen = urllib.request.urlopen
    cut: list[str] = []

    def first_revoke_cut_short(request: urllib.request.Request, timeout: float) -> Any:
        # Raised where `response.read()` raises it: inside `_http`'s one `try`.
        if request.full_url.endswith("/revoke/") and not cut:
            cut.append(request.full_url)
            raise IncompleteRead(b'{"rev', expected=40)
        return real_urlopen(request, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", first_revoke_cut_short)
    forgotten = dest.forget_minted_keys(signed_in)

    assert len(cut) == 1
    assert forgotten.detached == 2
    assert not any(service.project_key_path(project.id).exists() for project in projects)
    assert len(idp.revoked_keys) == 1, "the other key's revoke reached the server"
    [owed] = forgotten.revocations.owed
    assert owed.key_uid != idp.revoked_keys[0] and "IncompleteRead" in (owed.last_error or "")
    again = dest.revoke_owed(signed_in)
    assert [record.key_uid for record in again.revoked] == [owed.key_uid]
    with store_session() as store:
        assert store.pending_revocations() == []


def test_a_key_moved_onto_another_deployment_stays_owed_until_its_own_host_revokes_it(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """A move revoked the old key with the NEW deployment's session, which ``_revoke``
    refuses to send anywhere but the host that minted it — so the key was dropped from
    the store and left live, while the CHANGELOG said it was revoked (review of #172)."""
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    prod = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")
    workspace = dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN")
    with store_session() as store:
        previous = store.project_destination(project.id)
        dest.choose(
            store,
            project,
            workspace,
            dest.Studio(id=1, uid=None, name="Web"),
            prod,
            previous=previous,
        )
        moved = store.project_destination(project.id)
    assert moved is not None and moved.key_uid is None
    assert not service.project_key_path(project.id).exists()

    with_prod = dest.revoke_owed(prod)
    assert idp.revoked_keys == [] and [r.key_uid for r in with_prod.owed] == ["key-1"]
    assert with_prod.owed[0].last_error == f"signed in to {prod.api_url}, not {idp.url}"

    back_home = dest.revoke_owed(signed_in)
    assert [record.key_uid for record in back_home.revoked] == ["key-1"]
    assert idp.revoked_keys == ["key-1"]
    with store_session() as store:
        assert store.pending_revocations() == []


def test_a_clear_while_signed_out_keeps_the_key_owed_and_the_next_use_revokes_it(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``use --clear`` with no session to revoke with dropped the uid and kept nothing."""
    _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    with monkeypatch.context() as signed_out:
        signed_out.setattr(iam, "signed_in_quietly", lambda: None)
        cleared = _json(runner, "explainability", "use", "--clear")
    assert idp.revoked_keys == []
    [owed] = cleared["revocations"]["still_live"]
    assert (owed["key_uid"], owed["reason"]) == ("key-1", "signed out")

    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert again["revocations"] == {"revoked": ["key-1"], "still_live": []}
    assert idp.revoked_keys == ["key-1"] and len(idp.minted) == 2


def test_a_refused_revoke_is_said_kept_owed_and_retried_by_doctor_live(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """A 4xx to the revoke was tolerated and the uid dropped, silently (review of #172)."""
    from aisquare.services import diagnostics

    _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    idp.key_mint = "token_not_valid"  # the key endpoints answer the sign-in token 401
    cleared = runner.invoke(app, ["explainability", "key", "clear"])
    assert cleared.exit_code == 0, cleared.output
    assert "⚠ 1 key the CLI minted is still live on the server — acme for web" in cleared.output
    assert "HTTP 401" in cleared.output and "doctor --live" in cleared.output
    assert idp.revoked_keys == []

    offline = diagnostics._minted_keys_check(live=False)
    assert offline is not None and offline.status == "warn" and "HTTP 401" in offline.detail
    idp.key_mint = "ok"
    live = diagnostics._minted_keys_check(live=True)
    assert live is not None and live.status == "ok" and idp.revoked_keys == ["key-1"]
    assert diagnostics._minted_keys_check(live=False) is None, "nothing owed: no row"


def test_a_pass_that_could_not_ask_keeps_the_reason_the_server_gave(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """Every pass wrote its reason over the last one: a signed-out pass replaced the
    refusal the server had given, and the ``minted-keys`` row sent the operator to sign
    in when the blocker was the endpoint (review of #172's follow-ups, round 1, F6).
    The pass's own report still says why it did not ask."""
    from aisquare.services import diagnostics

    _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    idp.key_mint = "token_not_valid"  # the key endpoints answer the sign-in token 401
    cleared = _json(runner, "explainability", "key", "clear")
    [refused] = cleared["revocations"]["still_live"]
    assert "HTTP 401" in refused["reason"]

    signed_out = dest.revoke_owed(None)
    assert [record.last_error for record in signed_out.owed] == ["signed out"]
    offline = diagnostics._minted_keys_check(live=False)
    assert offline is not None and "HTTP 401" in offline.detail, offline
    assert "signed out" not in offline.detail


def test_a_store_that_cannot_be_read_for_the_revokes_costs_them_not_the_command(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``revoke_owed`` read the records owed unguarded: a locked store turned a
    ``key clear`` that had already committed into a traceback (review of #172's
    follow-ups, round 1, F3). The key stays owed, and the next pass revokes it."""
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")

    def locked() -> Iterator[ContextStore]:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patched:
        patched.setattr(dest, "store_session", locked)
        cleared = runner.invoke(app, ["explainability", "key", "clear"])
    assert cleared.exit_code == 0, cleared.output
    assert "✓ key cleared for web" in cleared.output
    assert idp.revoked_keys == [] and not service.project_key_path(project.id).exists()
    again = dest.revoke_owed(signed_in)
    assert [record.key_uid for record in again.revoked] == ["key-1"]
    assert idp.revoked_keys == ["key-1"]


def test_an_interrupted_key_set_keeps_the_minted_key_minted_and_in_its_file(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl-C inside ``key set`` dropped the minted key's uid for good.

    The uid was dropped first and put back only for an ``Exception``, and the file
    was put back only for one too: interrupted, the hand key sat in the minted key's
    file, no longer called minted, and the minted key was live and forgotten
    (review of #172). The binding and the detachment are one commit now.
    """
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    minted = service.project_key_path(project.id).read_text()

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setenv("WEB_KEY", "AIS_handmade_key")
    with monkeypatch.context() as patched:
        patched.setattr(SqliteStore, "set_project_explainability", interrupted)
        runner.invoke(app, ["explainability", "key", "set", "--from-env", "WEB_KEY"])
    assert service.project_key_path(project.id).read_text() == minted
    with store_session() as store:
        row = store.project_destination(project.id)
        assert store.pending_revocations() == []
    assert row is not None and row.key_uid == "key-1"
    assert idp.revoked_keys == []


def test_the_minted_keys_own_value_attached_again_stays_minted_and_live(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``key set`` with the value already in the file revoked the key it had just attached."""
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    monkeypatch.setenv("SAME_KEY", service.project_key_path(project.id).read_text())
    attached = _json(runner, "explainability", "key", "set", "--from-env", "SAME_KEY")
    assert attached["revocations"] == {"revoked": [], "still_live": []}
    assert idp.revoked_keys == []
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.key_uid == "key-1"


def test_a_mint_that_answers_with_the_same_uid_owes_nothing(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """An idempotent mint revoked the uid it had just stored: the project's key, dead."""
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    service.project_key_path(project.id).unlink()  # `use` mints again when the file is gone
    idp.minted.pop()  # the stub numbers keys by count: the next one is key-1 again
    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert again["key"]["minted"] is True and idp.minted[0]["uid"] == "key-1"
    assert idp.revoked_keys == []
    with store_session() as store:
        row = store.project_destination(project.id)
        assert store.pending_revocations() == []
    assert row is not None and row.key_uid == "key-1"


def test_a_mint_that_answers_with_a_uid_still_owed_takes_it_back_and_revokes_nothing(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uid still owed that a mint answers again is the project's key once more.

    Its record stayed owed, so the same ``use`` stored the key and then revoked it:
    the project's key, dead on the server (review of #172's follow-ups, round 1, F2).
    """
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    with monkeypatch.context() as signed_out:
        signed_out.setattr(iam, "signed_in_quietly", lambda: None)
        cleared = _json(runner, "explainability", "key", "clear")
    assert [owed["key_uid"] for owed in cleared["revocations"]["still_live"]] == ["key-1"]
    idp.minted.pop()  # the stub numbers keys by count: the next one is key-1 again
    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert again["key"]["minted"] is True and idp.minted[0]["uid"] == "key-1"
    assert again["revocations"] == {"revoked": [], "still_live": []}
    assert idp.revoked_keys == []
    with store_session() as store:
        row = store.project_destination(project.id)
        assert store.pending_revocations() == []
    assert row is not None and row.key_uid == "key-1"


def test_every_write_that_puts_an_owed_uid_back_on_its_row_owes_it_no_more(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The other half of the primitive (``_owe_no_revocation``): a uid a write stores
    on its row is the project's key, and nothing owed revokes it from under it (review
    of #172's follow-ups, round 1, F2)."""
    project = _project(tmp_path / "web")
    session = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")

    def carried_over(store: SqliteStore) -> None:
        row = store.project_destination(project.id)
        assert row is not None
        store.set_project_destination(row.model_copy(update={"key_uid": "key-1"}))

    writes: dict[str, Callable[[SqliteStore], object]] = {
        "a mint": lambda store: store.set_project_explainability(
            project.id, target="local", key_path=tmp_path / "key", set_by=None, minted="key-1"
        ),
        "a re-point that carries it": carried_over,
        "the uid set": lambda store: store.set_project_destination_key(project.id, "key-1"),
    }
    for name, write in writes.items():
        with store_session() as store:
            assert isinstance(store, SqliteStore)
            _minted_by_hand(store, project, session, "key-1")
            store.detach_minted_key(project.id)
            assert [record.key_uid for record in store.pending_revocations()] == ["key-1"], name
            write(store)
            owed = store.pending_revocations()
            row = store.project_destination(project.id)
        assert owed == [], name
        assert row is not None and row.key_uid == "key-1", name


def test_a_minted_key_the_store_cannot_record_is_revoked_and_leaves_no_file(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded nowhere, the key it made was live and unknown, and its file unbound."""
    project = _project(tmp_path / "web")

    def locked(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "set_project_explainability", locked)
    failed = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert failed.exit_code != 0
    assert [key["uid"] for key in idp.minted] == ["key-1"] and idp.revoked_keys == ["key-1"]
    assert not service.project_key_path(project.id).exists()


def test_a_mint_whose_put_back_fails_leaves_no_revoked_key_under_the_earlier_binding(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mint's put-back was suppressed, so when the earlier key could not be written
    back the file kept the key just minted, revoked a moment later, under the earlier
    binding, which may be for another deployment. ``key set``'s put-back removes the file
    and says so (0d79f861); the mint's did not (review of #170's follow-ups, round 1, F4)."""
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    earlier = service.project_key_path(project.id).read_text()
    writes = service.store_project_api_key

    def locked(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    def full_disk_for_the_put_back(project_id: str, key: str) -> Path:
        if key == earlier:
            raise OSError(28, "No space left on device")
        return writes(project_id, key)

    monkeypatch.setattr(SqliteStore, "set_project_explainability", locked)
    monkeypatch.setattr(dest, "store_project_api_key", full_disk_for_the_put_back)
    monkeypatch.setattr(ops, "store_project_api_key", full_disk_for_the_put_back)
    with store_session() as store:
        row = store.project_destination(project.id)
        assert row is not None
        with pytest.raises(sqlite3.OperationalError, match="database is locked") as refused:
            dest.mint_key(store, project, row, signed_in)

    assert idp.revoked_keys == ["key-2"], "the key recorded nowhere is revoked"
    assert any("could not be put back" in note for note in refused.value.__notes__)
    assert not service.project_key_path(project.id).exists(), "no revoked key under the binding"


def test_every_write_that_takes_a_minted_uid_off_its_row_owes_it_in_the_same_commit(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The one primitive (``_owe_revocation``): no store write can detach a minted key
    without recording it, and one whose transaction fails detaches nothing.

    Each of these took the uid off (or deleted the row) and left the revoke to its
    caller, which a lock, an interrupt or a missing session skipped — the key live,
    and nothing here naming it (review of #172). The refusal below fails the write
    after the uid is off in its transaction: a second write putting it back, as
    ``retiring_minted_key`` did, lost it when that write failed too.
    """
    project = _project(tmp_path / "web")
    session = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")
    other_path = tmp_path / "hand-key"

    def moved(store: SqliteStore) -> None:
        row = store.project_destination(project.id)
        assert row is not None
        store.set_project_destination(row.model_copy(update={"workspace_id": 7, "key_uid": None}))

    writes: dict[str, Callable[[SqliteStore], object]] = {
        "a hand key over it": lambda store: store.set_project_explainability(
            project.id, target="prod", key_path=other_path, set_by=None
        ),
        "its binding cleared": lambda store: store.clear_project_explainability(project.id),
        "a move": moved,
        "another uid": lambda store: store.set_project_destination_key(project.id, "key-2"),
        "no uid": lambda store: store.set_project_destination_key(project.id, None),
        "detached": lambda store: store.detach_minted_key(project.id),
        "the row cleared": lambda store: store.clear_project_destination(project.id),
        "a purge": lambda store: store.purge_project(project.id),
    }
    for name, write in writes.items():
        with store_session() as store:
            assert isinstance(store, SqliteStore)
            store.ensure_project(project)
            _minted_by_hand(store, project, session, "key-1")
            for earlier in store.pending_revocations():  # the previous round's
                store.settle_revocation(earlier.key_uid)
            # Refused inside the write's own transaction, as the uid comes off:
            # nothing may change, the uid least of all.
            store._conn.execute(
                "CREATE TEMP TRIGGER refuse BEFORE UPDATE OF key_uid ON project_destination "
                "BEGIN SELECT RAISE(ABORT, 'refused'); END"
            )
            with pytest.raises(sqlite3.IntegrityError):
                write(store)
            row = store.project_destination(project.id)
            assert row is not None and row.key_uid == "key-1", name
            assert store.pending_revocations() == [], name
            store._conn.execute("DROP TRIGGER refuse")
            write(store)
            owed = store.pending_revocations()
            row = store.project_destination(project.id)
        assert [record.key_uid for record in owed] == ["key-1"], name
        assert owed[0].api_url == session.api_url and owed[0].project_name == "web", name
        assert row is None or row.key_uid != "key-1", name


def test_an_exported_target_neither_makes_use_mint_again_nor_splits_it_from_the_launches(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether the project has this destination's key is asked of the destination's deployment,
    and so is the key its launches take.

    ``use`` resolved the destination's deployment by name while launches let an
    exported ``$AISQUARE_EXPLAINABILITY_TARGET`` win over the destination, so
    ``use`` reported the project's key on ``local`` and every launch went to
    ``stg`` with the machine key (review of #172, D2 round 2). This test pinned
    the half ``use`` saw.
    """
    monkeypatch.setenv(ops.TARGET_ENV_VAR, "stg")
    project = _project(tmp_path / "web")
    first = _json(runner, "explainability", "use", "acme/Frontend")
    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert again["key"] == {"source": "project", "minted": False, "note": "the project's own key"}
    assert len(idp.minted) == 1 and idp.revoked_keys == [], "a second key minted over the first"
    assert first["target"]["name"] == again["target"]["name"] == "local"
    assert again["routing"] and all(b["bound"] for b in again["routing"])
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.key_uid == "key-1"
    # What `launch` and `team spawn` resolve for the project, under the same shell.
    launched = ops.resolve_target(load_config().explainability, project_id=project.id)
    assert (launched.name, launched.key_source) == ("local", "project")
    status = _json(runner, "explainability", "status", "--project", project.id)
    assert (status["target"], status["target_source"], status["key_source"]) == (
        "local",
        "destination",
        "project",
    )
    said = runner.invoke(app, ["explainability", "status", "--project", project.id])
    assert f"${ops.TARGET_ENV_VAR}=stg applies to projects without one" in said.output


def test_under_an_exported_target_the_key_set_use_advises_is_the_one_its_next_run_finds(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API refuses the mint, and `use` advises `key set --from-env VAR`. With the
    variable exported, `key set` bound that key to the exported target, the next `use`
    did not see it and refused to mint over a hand key, and launches took it to the
    other deployment (review of #172, D2 round 2, on the PR). One precedence now:
    the key lands on the destination's deployment, and the next `use` finds it."""
    monkeypatch.setenv(ops.TARGET_ENV_VAR, "stg")
    idp.key_mint = "token_not_valid"
    project = _project(tmp_path / "web")
    first = _json(runner, "explainability", "use", "acme/Frontend")
    assert first["key"]["source"] == "unset" and "key set --from-env VAR" in first["key"]["note"]

    monkeypatch.setenv("VAR", "AIS_from_the_dashboard")
    attached = runner.invoke(app, ["explainability", "key", "set", "--from-env", "VAR"])
    assert attached.exit_code == 0, attached.output
    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert (again["target"]["name"], again["key"]["source"]) == ("local", "project")
    launched = ops.resolve_target(load_config().explainability, project_id=project.id)
    assert (launched.name, launched.api_key) == ("local", "AIS_from_the_dashboard")


def test_a_mint_over_a_minted_key_revokes_the_one_it_replaces(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    """Its uid is overwritten, so unrevoked it would be a live key nothing remembers."""
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    service.project_key_path(project.id).unlink()  # the minted key's file is gone
    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert again["key"]["minted"] is True and len(idp.minted) == 2
    assert idp.revoked_keys == ["key-1"]
    with store_session() as store:
        row = store.project_destination(project.id)
    assert row is not None and row.key_uid == "key-2"


def test_a_purge_revokes_the_minted_key_it_leaves_nothing_to_find(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``forget --purge`` and ``prune --purge`` delete the row ``logout`` finds the key by.

    The row, its ``key_uid`` and the key file went, the key stayed live on the
    server, and nothing on this machine could revoke it any more (review of
    #172). Revoked once the purge is done: one that fails keeps a working key.
    A plain forget keeps the row, hidden, and so keeps it for ``logout``.
    """
    web = _project(tmp_path / "web")
    gone = _project(tmp_path / "gone")
    kept = _project(tmp_path / "kept")
    for project in (web, gone, kept):
        _json(runner, "explainability", "use", "acme/Frontend", "--project", project.id)
    assert [m["uid"] for m in idp.minted] == ["key-1", "key-2", "key-3"]

    def locked(*_args: object, **_kwargs: object) -> dict[str, int]:
        raise sqlite3.OperationalError("database is locked")

    purge = SqliteStore.purge_project
    monkeypatch.setattr(SqliteStore, "purge_project", locked)
    assert runner.invoke(app, ["project", "forget", str(web.root), "--purge"]).exit_code != 0
    assert idp.revoked_keys == [], "revoked for a purge that did not happen"
    monkeypatch.setattr(SqliteStore, "purge_project", purge)

    forgotten = runner.invoke(app, ["project", "forget", str(web.root), "--purge"])
    assert forgotten.exit_code == 0, forgotten.output
    assert idp.revoked_keys == ["key-1"]

    gone.root.rmdir()
    pruned = runner.invoke(app, ["project", "prune", "--missing", "--purge", "--yes"])
    assert pruned.exit_code == 0, pruned.output
    assert idp.revoked_keys == ["key-1", "key-2"]

    assert runner.invoke(app, ["project", "forget", str(kept.root)]).exit_code == 0
    assert idp.revoked_keys == ["key-1", "key-2"], "a forget without --purge keeps the row"
    assert _json(runner, "logout")["minted_keys_cleared"] == 1
    assert idp.revoked_keys == ["key-1", "key-2", "key-3"]


def test_a_purge_signed_out_keeps_its_keys_owed_says_so_and_revokes_after_the_sweep(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signed out, offline or on another host, a purge revoked nothing and deleted the
    only record of the key, silently; ``prune --purge`` made one blocking revoke per
    project inside its loop and the store session (review of #172). The keys stay
    owed and are reported, the sweep revokes once after the store is closed, and the
    next ``use`` revokes what is still owed.
    """
    one, two, three = (_project(tmp_path / name) for name in ("one", "two", "three"))
    for project in (one, two, three):
        _json(runner, "explainability", "use", "acme/Frontend", "--project", project.id)
    passes: list[object] = []
    real = dest.revoke_owed

    def counted(session: iam.Session | None, **kwargs: Any) -> dest.Revocations:
        passes.append(kwargs.get("project_ids"))
        return real(session, **kwargs)

    one.root.rmdir()
    two.root.rmdir()
    with monkeypatch.context() as signed_out:
        signed_out.setattr(iam, "signed_in_quietly", lambda: None)
        signed_out.setattr(dest, "revoke_owed", counted)
        pruned = runner.invoke(app, ["--json", "project", "prune", "--missing", "--purge", "--yes"])
        forgotten = runner.invoke(app, ["project", "forget", str(three.root), "--purge"])
    assert pruned.exit_code == 0, pruned.output
    still_live = json.loads(pruned.stdout)["keys_still_live"]
    assert sorted(key["key_uid"] for key in still_live) == ["key-1", "key-2"]
    assert {key["last_error"] for key in still_live} == {"signed out"}
    assert passes[0] == {one.id, two.id}, "one pass after the sweep, not a revoke per project"
    assert forgotten.exit_code == 0, forgotten.output
    assert "⚠ 1 key the CLI minted is still live on the server — acme for three" in (
        forgotten.output
    )
    assert idp.revoked_keys == []

    again = _json(runner, "explainability", "use", "acme/Frontend")
    assert sorted(again["revocations"]["revoked"]) == ["key-1", "key-2", "key-3"]
    assert sorted(idp.revoked_keys) == ["key-1", "key-2", "key-3"]


# --- the store and the directory ----------------------------------------------------------------


def test_use_in_a_directory_nothing_registered_captures_it(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend", "--no-key"])
    assert result.exit_code == 0, result.output
    with store_session() as store:
        row = store.project_destination(project_id_for(fresh.resolve()))
        registered = store.get_project(project_id_for(fresh.resolve()))
    assert row is not None and row.studio_name == "Frontend"
    assert registered is not None and registered.onboarded_at is None, "captured, not onboarded"


def test_purging_a_project_takes_its_destination_with_it(
    runner: CliRunner, isolated_home: Path, tmp_path: Path
) -> None:
    project = _project(tmp_path / "web")
    session = iam.Session(api_url="https://api.aisquare.studio", token="aisq_x", source="env")
    with store_session() as store:
        dest.choose(
            store,
            project,
            dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN"),
            dest.Studio(id=301, uid="st-301", name="Frontend"),
            session,
        )
        removed = store.purge_project(project.id)
        assert store.project_destination(project.id) is None
    assert removed["project_destination"] == 1 and removed["project"] == 1


# --- a workspace the user has not joined -------------------------------------------------------


def test_studios_of_another_workspace_are_not_listed_under_this_one(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session
) -> None:
    """The API answers an unhonoured header with the personal workspace's studios."""
    idp.studios["99"] = [{"id": 701, "uid": "st-701", "name": "Notes", "workspace_id": 7}]
    payload = _json(runner, "explainability", "studios", "--workspace", "guests")
    assert payload["studios"] == []
    refused = runner.invoke(app, ["explainability", "use", "guests/Notes"])
    assert refused.exit_code == 1 and "not a member yet" in refused.output
