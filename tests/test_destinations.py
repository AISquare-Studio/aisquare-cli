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
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import aisquare
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


def test_the_next_step_for_the_projects_key_resolves_the_projects_key(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`doctor` opens no store, so a minted key is invisible to it; `status` resolves it."""
    _trace_on(monkeypatch)
    _project(tmp_path / "lib")
    _project(tmp_path / "web")  # pinned last, so the active one
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    step = _next_step(result.output)
    assert step == ["explainability", "status", "--target", "local"]
    followed = runner.invoke(app, ["--json", *step])
    assert followed.exit_code == 0, followed.output
    shown = json.loads(followed.stdout)
    assert (shown["key_source"], shown["key_set"]) == ("project", True)

    # For a project that is not the active one, the check is about THAT project.
    other = runner.invoke(app, ["explainability", "use", "--project", "lib", "acme/API"])
    assert other.exit_code == 0, other.output
    step = _next_step(other.output)
    assert step == ["explainability", "status", "--target", "local", "--project", "lib"]
    shown = _json(runner, *step)
    assert shown["destination"]["studio"]["name"] == "API", "the active project was checked"
    assert (shown["key_source"], shown["key_set"]) == ("project", True)


def test_with_no_key_the_next_step_attaches_one_to_the_destination(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not `doctor`: its remedy is a machine key, which never stands in for the project's."""
    _trace_on(monkeypatch)
    idp.key_mint = "token_not_valid"
    project = _project(tmp_path / "web")
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    step = _next_step(result.output)
    assert step == ["explainability", "key", "set", "--from-env", "VAR", "--target", "local"]
    monkeypatch.setenv("VAR", "AIS_handmade_key")
    assert runner.invoke(app, step).exit_code == 0
    resolved = ops.resolve_target(load_config().explainability, None, project_id=project.id)
    assert (resolved.name, resolved.key_source) == ("local", "project")
    again = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert _next_step(again.output) == ["explainability", "status", "--target", "local"]


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
    _project(tmp_path / "web")
    result = runner.invoke(app, ["explainability", "use", "acme/Frontend"])
    assert result.exit_code == 0, result.output
    step = _next_step(result.output)
    assert step == ["doctor", "--live", "--target", "local"]
    # Offline: the rows --live adds dial the gateway. The config row is the one
    # that failed when this line named doctor for a project's key.
    rows = {check.name: check for check in ops.checks(target_name=step[-1])}
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


def test_the_ui_attach_resolves_the_project_and_leaves_a_minted_key_to_the_cli(
    runner: CliRunner, idp: IdentityProviderStub, signed_in: iam.Session, tmp_path: Path
) -> None:
    from aisquare.cli.ui.views import explainability as view

    idp.key_mint = "token_not_valid"
    project = _project(tmp_path / "web")
    _json(runner, "explainability", "use", "acme/Frontend")
    attached = view.attach_project_key("AIS_pasted_key")
    assert attached.severity == "information" and "for target local" in attached.message
    with store_session() as store:
        binding = store.project_explainability(project.id)
    assert binding is not None and binding.target == "local"

    idp.key_mint = "ok"
    runner.invoke(app, ["explainability", "key", "clear"])
    _json(runner, "explainability", "use", "acme/Frontend")
    refused = view.attach_project_key("AIS_pasted_again")
    assert refused.severity == "warning" and "minted by the CLI" in refused.message
    assert service.project_key_path(project.id).read_text() == idp.minted[0]["api_key"]


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


def test_logout_revokes_only_on_the_host_that_minted_and_survives_a_stuck_file(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    here = _project(tmp_path / "here")
    elsewhere = _project(tmp_path / "elsewhere")
    stuck = _project(tmp_path / "stuck")
    other = iam.Session(api_url="https://stg-api.aisquare.studio", token="aisq_y", source="env")
    workspace = dest.Workspace(id=42, uid="ws-uid-42", name="acme", role="ADMIN")
    studio = dest.Studio(id=301, uid="st-301", name="Frontend")
    with store_session() as store:
        for project, session, uid in (
            (here, signed_in, "key-here"),
            (elsewhere, other, "key-elsewhere"),
            (stuck, signed_in, "key-stuck"),
        ):
            dest.choose(store, project, workspace, studio, session)
            store.set_project_destination_key(project.id, uid)
    real_clear = service.clear_project_api_key

    def refuse_one(project_id: str) -> bool:
        if project_id == stuck.id:
            raise PermissionError("read-only")
        return real_clear(project_id)

    monkeypatch.setattr(dest, "clear_project_api_key", refuse_one)
    with store_session() as store:
        # `stuck` has a key file that will not delete; the projects after it still clear.
        store.set_project_explainability(
            stuck.id, target="local", key_path=service.project_key_path(stuck.id), set_by=None
        )
        cleared = dest.revoke_minted_keys(store, signed_in)
    assert sorted(cleared) == sorted([here.id, elsewhere.id])
    assert "key-elsewhere" not in idp.revoked_keys, "a key uid sent to a host that never minted it"
    assert sorted(idp.revoked_keys) == ["key-here", "key-stuck"]


def test_an_exported_target_does_not_make_use_mint_again(
    runner: CliRunner,
    idp: IdentityProviderStub,
    signed_in: iam.Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whether the project has this destination's key is asked of the destination's deployment."""
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
