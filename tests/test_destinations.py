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

import json
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import auth as auth_cli
from aisquare.cli.app import app
from aisquare.core.config import AppConfig, ExplainabilityTarget, load_config, save_config
from aisquare.core.store import store_session
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


def _project(root: Path) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        store.onboard_project(info)
    pin_project(info.id)
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
    # A pending invitation is listed but is not somewhere traces can go (no studios visible).
    invited = runner.invoke(app, ["explainability", "use", "guests/anything"])
    assert invited.exit_code == 1 and "no studio matches" in invited.output
    # Clearing drops the row and the minted key.
    cleared = _json(runner, "explainability", "use", "--clear")
    assert cleared["cleared"]["studio"]["name"] == "API"
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
    explicit = ops.resolve_target(settings, "prod", project_id=project.id)
    assert explicit.name == "prod", "--target still wins"
    monkeypatch.setenv(ops.TARGET_ENV_VAR, "prod")
    by_variable = ops.resolve_target(settings, None, project_id=project.id)
    assert by_variable.name == "prod", "so does the variable"


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
