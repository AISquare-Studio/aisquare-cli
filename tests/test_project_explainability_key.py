"""A workspace key per project (#141): stored, resolved first, bound to one deployment.

Every claim has its control: the project key wins over the machine key for ITS
project and deployment only; a binding for another deployment is not a key for
this one (the rule ``test_key_never_crosses_deployments.py`` pins for the file,
applied to the project); the value never reaches stdout; the proxy header of a
real ``launch`` and ``team spawn --exec`` carries the project's key; `doctor`
still opens no store.

"The project" is ONE answer on every surface — the project a launch from here
joins (``AISQUARE_TEAM_HUB``, else this checkout), never the ``project switch``
pin — and the pinned-elsewhere controls below are the review of #170's
cross-workspace leak, measured.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.core.store import SqliteStore, store_session
from aisquare.core.workspace import pin_project, project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import explainability as service
from aisquare.services import explainability_ops as ops
from aisquare.services import iam

PROJECT_KEY = "pk-live-0123456789abcdef"
MACHINE_KEY = "mk-machine-fedcba9876543210"


def _settings(*, default_var: bool = True) -> AppConfig:
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    config.explainability.targets = {
        "stg": ExplainabilityTarget(
            gateway_url="https://stg.example", proxy_url="http://127.0.0.1:9199"
        ),
        "prod": ExplainabilityTarget(
            gateway_url="https://prod.example",
            proxy_url="http://127.0.0.1:9199",
            **({} if default_var else {"api_key_env": "PROD_KEY"}),
        ),
    }
    save_config(config)
    return config


def _project(root: Path, *, pin: bool = False) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    info = ProjectInfo(id=project_id_for(root.resolve()), root=root.resolve(), linked_repos=[])
    with store_session() as store:
        store.onboard_project(info)
    if pin:
        pin_project(info.id)
    return info


def _attach(project: ProjectInfo, value: str = PROJECT_KEY, *, target: str = "stg") -> Path:
    """A binding written at the store level — what ``key set`` leaves behind."""
    path = service.store_project_api_key(project.id, value)
    with store_session() as store:
        store.set_project_explainability(project.id, target=target, key_path=path, set_by=None)
    return path


def _sent_key(env: dict[str, str]) -> str | None:
    """The workspace key a proxy would authenticate this session with, if any."""
    found = re.search(r"X-AISquare-Key: ([^\n]+)", env.get("ANTHROPIC_CUSTOM_HEADERS", ""))
    return found.group(1).strip() if found else None


@pytest.fixture
def home(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    paths.ensure_home()
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)
    monkeypatch.delenv("PROD_KEY", raising=False)
    return isolated_home


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``launch`` and ``team spawn --exec`` run for real up to the exec, which is captured.

    The proxy answers healthy and the Run root is accepted without a socket, so
    what is measured is the key the real command handed ``wire_session``.
    """
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    monkeypatch.setattr(
        "aisquare.cli.team.os.execvpe",
        lambda file, argv, env: captured.update(argv=argv, env=env),
    )
    monkeypatch.setattr(service, "probe_proxy", lambda url: service.ProxyProbe(True, "healthy"))
    monkeypatch.setattr(
        service, "_post_run_root", lambda *_args: service.RootReceipt(True, "HTTP 202")
    )
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        service.PIPELINE_ID_ENV_VAR,
        service.TRACE_AGENT_NAME_ENV_VAR,
        service.RUN_TRACE_ID_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    return captured


# --- the store and the file -------------------------------------------------------------------


def test_the_binding_round_trips_and_the_value_lives_in_a_600_file_not_the_store(
    home: Path, tmp_path: Path
) -> None:
    project = _project(tmp_path / "api")
    path = service.store_project_api_key(project.id, f"  {PROJECT_KEY}\n")
    assert path == service.project_key_path(project.id)
    assert (
        path.read_text(encoding="utf-8") == PROJECT_KEY and (path.stat().st_mode & 0o777) == 0o600
    )
    with store_session() as store:
        assert store.project_explainability(project.id) is None
        binding = store.set_project_explainability(
            project.id, target="stg", key_path=path, set_by="me@example.com"
        )
        assert (
            binding.target == "stg"
            and binding.key_path == path
            and binding.set_by == "me@example.com"
        )
        assert [b.project_id for b in store.project_explainability_all()] == [project.id]
        # re-pointing keeps one row per project
        moved = store.set_project_explainability(
            project.id, target="prod", key_path=path, set_by=None
        )
        assert moved.target == "prod" and len(store.project_explainability_all()) == 1
        assert store.clear_project_explainability(project.id) is True
        assert store.clear_project_explainability(project.id) is False
    assert PROJECT_KEY not in paths.db_path().read_bytes().decode("latin-1"), "never in context.db"
    assert service.clear_project_api_key(project.id) is True and not path.exists()
    assert service.clear_project_api_key(project.id) is False


def test_a_project_key_goes_into_a_file_already_restricted_to_this_account(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The machine key's recipe since the #65 fold (round 2, F5), for #141's key: written in
    place and chmodded afterwards, a first project key sat under the umask mode (0644), or
    on Windows under the DACL the data directory hands down, until the chmod ran — and
    ``chmod`` restricts nothing on NTFS. The spy records what each restriction was applied
    to: a temp, still empty, for a new file and over an existing one. A restriction that
    fails is said out loud, and the key still lands."""
    project = _project(tmp_path / "api")
    real = paths.restrict_to_owner
    applied: list[tuple[str, int]] = []
    holds = True

    def spy(path: Path) -> bool:
        applied.append((path.name, path.stat().st_size))
        real(path)
        return holds

    monkeypatch.setattr(paths, "restrict_to_owner", spy)
    target = service.store_project_api_key(project.id, f" {PROJECT_KEY}\n")
    service.store_project_api_key(project.id, "second-project-key")
    assert capsys.readouterr().err == ""
    holds = False
    service.store_project_api_key(project.id, "third-project-key")
    assert len(applied) == 3, applied
    for name, size in applied:
        assert name.startswith(".explainability-key.") and name.endswith(".tmp"), applied
        assert size == 0, f"restricted with the key already in it: {applied}"
    assert target == service.project_key_path(project.id)
    assert target.read_text(encoding="utf-8") == "third-project-key"
    assert "warning: could not restrict" in capsys.readouterr().err
    assert not list(target.parent.glob(".explainability-key.*")), "a temp was left behind"


def test_forget_purge_takes_an_attached_key_with_it(
    home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding is a FOREIGN KEY to the project row, so the purge must delete it
    first: left out, the whole purge rolled back with ``FOREIGN KEY constraint
    failed`` and a traceback (review of #170)."""
    api = _project(tmp_path / "api")
    bystander = _project(tmp_path / "web")
    path = _attach(api)
    kept = _attach(bystander)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["--json", "project", "forget", "api", "--purge"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed"]["project_explainability"] == 1
    with store_session() as store:
        assert store.get_project(api.id) is None
        assert store.project_explainability(api.id) is None
        assert store.project_explainability(bystander.id) is not None, "only the purged one"
    assert not path.exists(), "the key file goes with the project's data directory"
    assert kept.exists()


def test_prune_purge_drops_every_keyed_candidate_and_their_directories(
    home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``prune --purge`` loops over the candidates: the FK failure escaped halfway,
    after earlier projects had lost their rows but before any data directory was
    removed or the pin moved."""
    gone = [_project(tmp_path / name) for name in ("one", "two")]
    files = [_attach(project) for project in gone]
    for project in gone:
        shutil.rmtree(project.root)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["project", "prune", "--missing", "--purge", "--yes"])

    assert result.exit_code == 0, result.output
    assert "purged 2 registrations" in result.stdout
    with store_session() as store:
        assert all(store.project_explainability(project.id) is None for project in gone)
    assert not any(path.parent.exists() for path in files)


# --- the resolver ------------------------------------------------------------------------------


def test_the_project_key_wins_for_its_project_and_deployment_only(
    home: Path, tmp_path: Path
) -> None:
    settings = _settings().explainability
    service.store_api_key(MACHINE_KEY)
    api = _project(tmp_path / "api")
    other = _project(tmp_path / "other")
    path = _attach(api)

    mine = ops.resolve_target(settings, project_id=api.id)
    assert (mine.api_key, mine.key_source, mine.project_id) == (PROJECT_KEY, "project", api.id)
    assert "the project's own key" in mine.key_origin and str(path) in mine.key_origin
    theirs = ops.resolve_target(settings, project_id=other.id)
    assert (theirs.api_key, theirs.key_source) == (
        MACHINE_KEY,
        "file",
    )  # another project: the machine
    nobody = ops.resolve_target(settings)
    assert (nobody.api_key, nobody.key_source) == (MACHINE_KEY, "file")  # no project: as before
    # Bound to stg: a prod resolve for the same project never sees it.
    prod = ops.resolve_target(settings, "prod", project_id=api.id)
    assert prod.key_source != "project" and prod.api_key != PROJECT_KEY
    # The target's variable still outranks the machine file, and the project key both.
    os.environ["EXPLAINABILITY_API_KEY"] = "env-key"
    try:
        assert ops.resolve_target(settings, project_id=other.id).key_source == "env"
        assert ops.resolve_target(settings, project_id=api.id).key_source == "project"
    finally:
        del os.environ["EXPLAINABILITY_API_KEY"]
    # A binding whose file is gone reads as no project key: the next rung answers.
    path.unlink()
    assert ops.resolve_target(settings, project_id=api.id).key_source == "file"


# --- the CLI -----------------------------------------------------------------------------------


def test_key_set_show_clear_never_print_the_value(
    home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings()
    api = _project(tmp_path / "api")
    monkeypatch.chdir(api.root)

    refused = runner.invoke(app, ["explainability", "key", "set", PROJECT_KEY])
    assert refused.exit_code != 0, "argv is never a key"

    result = runner.invoke(app, ["explainability", "key", "set"], input=PROJECT_KEY + "\n")
    assert result.exit_code == 0, result.output
    assert "✓ key attached to api for target stg" in result.stdout and "(mode 600)" in result.stdout
    # The step a key for another workspace needs before anything traces, named.
    assert "aisquare explainability register --target stg" in result.stdout
    assert PROJECT_KEY not in result.stdout
    path = service.project_key_path(api.id)
    assert path.read_text(encoding="utf-8") == PROJECT_KEY

    shown = runner.invoke(app, ["--json", "explainability", "key", "show"])
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)
    assert (
        payload["attached"] is True
        and payload["target"] == "stg"
        and payload["key_source"] == "project"
    )
    assert payload["key_set"] is True and PROJECT_KEY not in shown.stdout
    plain = runner.invoke(app, ["explainability", "key", "show"])
    assert "project key for target stg" in plain.stdout and PROJECT_KEY not in plain.stdout

    status = runner.invoke(app, ["--json", "explainability", "status"])
    status_payload = json.loads(status.stdout)
    assert (status_payload["key_source"], status_payload["key_project"]) == ("project", api.id)

    os.environ["NEW_KEY"] = "pk-rotated"
    try:
        rotated = runner.invoke(
            app, ["explainability", "key", "set", "--from-env", "NEW_KEY", "--target", "prod"]
        )
    finally:
        del os.environ["NEW_KEY"]
    assert rotated.exit_code == 0, rotated.output
    assert path.read_text(encoding="utf-8") == "pk-rotated"
    for_stg = runner.invoke(app, ["explainability", "key", "show"])
    assert "not used for target stg" in for_stg.stdout  # bound to prod now

    cleared = runner.invoke(app, ["explainability", "key", "clear"])
    assert cleared.exit_code == 0 and "✓ key cleared for api" in cleared.stdout
    assert not path.exists()
    again = runner.invoke(app, ["--json", "explainability", "key", "clear"])
    assert json.loads(again.stdout)["cleared"] is False
    missing = runner.invoke(app, ["explainability", "key", "set", "--from-env", "UNSET_VAR_X"])
    assert missing.exit_code != 0


def test_key_set_registers_a_directory_nothing_had_registered(
    home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docs' own first example, from a fresh checkout: the binding is a FOREIGN
    KEY to the project row, and an unregistered directory used to get the key
    file written, then an uncaught ``IntegrityError`` — the key left on disk with
    no binding (review of #170). Attaching is deliberate, so it registers."""
    _settings()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    fresh_id = project_id_for(fresh.resolve())

    result = runner.invoke(app, ["explainability", "key", "set"], input=PROJECT_KEY + "\n")

    assert result.exit_code == 0, result.output
    assert "✓ key attached to fresh for target stg" in result.stdout
    with store_session() as store:
        registered = store.get_project(fresh_id)
        binding = store.project_explainability(fresh_id)
    assert registered is not None and registered.onboarded_at is not None, "a deliberate add"
    assert binding is not None and binding.key_path.read_text(encoding="utf-8") == PROJECT_KEY


def test_a_binding_that_cannot_be_recorded_leaves_no_key_on_disk(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal half: whatever stops the row, the credential written for it goes."""
    project = ProjectInfo(
        id=project_id_for((tmp_path / "api").resolve()), root=tmp_path / "api", linked_repos=[]
    )

    def refuse(self: SqliteStore, *_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "set_project_explainability", refuse)
    with pytest.raises(sqlite3.OperationalError):
        ops.attach_project_key(project, PROJECT_KEY, target="stg")
    assert not service.project_key_path(project.id).exists()


def test_a_re_attach_that_cannot_be_recorded_leaves_the_earlier_bindings_key_in_place(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is written before the row, and a refused re-attach used to keep the NEW
    file under the OLD row — stg's binding answering with prod's key, the
    cross-deployment rule broken by a failure (review of #170)."""
    config = _settings()
    project = _project(tmp_path / "api")
    ops.attach_project_key(project, "stg-key-aaaa", target="stg")

    def refuse(self: SqliteStore, *_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "set_project_explainability", refuse)
    with pytest.raises(sqlite3.OperationalError):
        ops.attach_project_key(project, "prod-key-bbbb", target="prod")

    stg = ops.resolve_target(config.explainability, "stg", project_id=project.id)
    assert (stg.api_key, stg.key_source) == ("stg-key-aaaa", "project")
    path = service.project_key_path(project.id)
    assert (path.stat().st_mode & 0o777) == 0o600
    # An earlier binding whose file was already gone gets it gone again, not
    # the refused key: the row is left exactly as `key show` reported it.
    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        ops.attach_project_key(project, "prod-key-bbbb", target="prod")
    assert not path.exists()


def test_key_set_refuses_a_target_this_machine_does_not_have(
    home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolve_target`` answers for any name, so ``--target prdo`` bound the key to
    a deployment nothing resolves and printed success (review of #170)."""
    _settings()
    api = _project(tmp_path / "api")
    monkeypatch.chdir(api.root)

    typo = runner.invoke(
        app, ["explainability", "key", "set", "--target", "prdo"], input=PROJECT_KEY + "\n"
    )

    assert typo.exit_code != 0
    assert "no target 'prdo'" in typo.output and "prod, stg" in typo.output
    assert not service.project_key_path(api.id).exists(), "refused before anything is written"
    # Control: a real target is accepted, the default target too.
    assert (
        runner.invoke(
            app, ["explainability", "key", "set", "--target", "prod"], input=PROJECT_KEY + "\n"
        ).exit_code
        == 0
    )


def test_key_set_records_the_signed_in_email_as_who_attached_it(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signed_in = iam.Session(
        api_url="https://api.example", token="t", source="file", email="me@example.com"
    )
    monkeypatch.setattr(iam, "stored_session", lambda: signed_in)
    project = _project(tmp_path / "api")

    binding = ops.attach_project_key(project, PROJECT_KEY, target="stg")

    assert binding.set_by == "me@example.com"


# --- ONE project on every surface: the one a launch from here joins ----------------------------


def test_a_launch_carries_its_projects_key_and_a_launch_elsewhere_the_machines(
    home: Path,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    """Through the REAL commands (review of #170: the old test called the resolver
    and ``wire_session`` itself, so reverting ``project_id=`` at either call site
    left the suite green)."""
    _settings()
    service.store_api_key(MACHINE_KEY)
    api = _project(tmp_path / "api")
    web = _project(tmp_path / "web")
    _attach(api)

    monkeypatch.chdir(api.root)
    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    assert _sent_key(launched["env"]) == PROJECT_KEY

    # Cleared between commands, so each assertion reads the env THAT command
    # exec'd — a spawn that never reached exec would otherwise pass on the
    # env the launch above left behind.
    launched.clear()
    spawned = runner.invoke(app, ["team", "spawn", "coder", "--exec", "--no-probe"])
    assert spawned.exit_code == 0, spawned.output
    assert _sent_key(launched["env"]) == PROJECT_KEY

    launched.clear()
    monkeypatch.chdir(web.root)
    elsewhere = runner.invoke(app, ["launch", "coder"])
    assert elsewhere.exit_code == 0, elsewhere.output
    assert _sent_key(launched["env"]) == MACHINE_KEY, "another project: the machine key"


def test_a_pin_elsewhere_never_hands_this_projects_agents_its_key(
    home: Path,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    """Project X pinned (``project switch``), work in Y: ``env`` — and so the printed
    ``team spawn`` line that evals it — used to export X's key for Y's agent while
    ``launch`` in the same directory used Y's, and ``status`` reported X's as in
    use. Every surface now answers for Y, the board a launch here joins."""
    _settings()
    service.store_api_key(MACHINE_KEY)
    pinned = _project(tmp_path / "api", pin=True)
    here = _project(tmp_path / "web")
    _attach(pinned)
    monkeypatch.chdir(here.root)

    exported = runner.invoke(app, ["--json", "explainability", "env", "coder"])
    assert exported.exit_code == 0, exported.output
    assert _sent_key(json.loads(exported.stdout)["env"]) == MACHINE_KEY

    status = json.loads(runner.invoke(app, ["--json", "explainability", "status"]).stdout)
    assert (status["key_source"], status["key_project"]) == ("file", here.id)
    shown = json.loads(runner.invoke(app, ["--json", "explainability", "key", "show"]).stdout)
    assert (shown["project"], shown["attached"]) == (here.id, False)

    launch = runner.invoke(app, ["launch", "coder"])
    assert launch.exit_code == 0, launch.output
    assert _sent_key(launched["env"]) == MACHINE_KEY

    # Control: from the pinned project's own checkout, its key is the answer.
    monkeypatch.chdir(pinned.root)
    own = runner.invoke(app, ["--json", "explainability", "env", "coder"])
    assert _sent_key(json.loads(own.stdout)["env"]) == PROJECT_KEY
    # And `--project` still names another project explicitly.
    monkeypatch.chdir(here.root)
    named = runner.invoke(app, ["--json", "explainability", "env", "coder", "--project", "api"])
    assert _sent_key(json.loads(named.stdout)["env"]) == PROJECT_KEY


def test_under_a_hub_the_key_is_the_hub_projects_everywhere(
    home: Path,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    launched: dict[str, Any],
) -> None:
    """``AISQUARE_TEAM_HUB`` puts every launch on the hub's board, so ``key set`` from
    a repo under it binds the hub project — the key those launches really use —
    rather than the repo's, which ``status`` then reported as in use while no
    launch ever read it."""
    _settings()
    service.store_api_key(MACHINE_KEY)
    hub = tmp_path / "hub"
    hub.mkdir()
    repo = _project(tmp_path / "api")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.resolve()))
    monkeypatch.chdir(repo.root)

    attached = runner.invoke(app, ["explainability", "key", "set"], input=PROJECT_KEY + "\n")
    assert attached.exit_code == 0, attached.output
    assert "✓ key attached to hub" in attached.stdout
    with store_session() as store:
        assert store.project_explainability(project_id_for(hub.resolve())) is not None
        assert store.project_explainability(repo.id) is None

    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    assert _sent_key(launched["env"]) == PROJECT_KEY


def test_register_declares_the_roster_under_the_projects_key(
    home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project key for ANOTHER workspace traces nothing until that workspace knows
    the agent identities (409 ``agent_not_registered``), and ``register`` resolved
    at machine level only (review of #170)."""
    _settings()
    service.store_api_key(MACHINE_KEY)
    api = _project(tmp_path / "api")
    web = _project(tmp_path / "web")
    _attach(api)
    used: list[str | None] = []

    def register_roster(target: ops.ResolvedTarget, names: tuple[str, ...]) -> ops.HttpVerdict:
        used.append(target.api_key)
        return ops.HttpVerdict(ok=True, status=200, detail="HTTP 200", payload={"agents": []})

    monkeypatch.setattr(ops, "register_roster", register_roster)
    monkeypatch.chdir(api.root)
    own = runner.invoke(app, ["explainability", "register"])
    assert own.exit_code == 0, own.output
    assert "under the project's own key" in own.stdout, "whose workspace, said"
    monkeypatch.chdir(web.root)
    machine = runner.invoke(app, ["explainability", "register"])
    assert machine.exit_code == 0 and "under" not in machine.stdout.splitlines()[0]
    assert runner.invoke(app, ["explainability", "register", "--project", "api"]).exit_code == 0

    assert used == [PROJECT_KEY, MACHINE_KEY, PROJECT_KEY]


# --- the reads a launch is prepared with stay reads -------------------------------------------


def test_env_still_prints_the_exports_when_the_store_is_damaged(
    home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before #141 ``env`` never opened ``context.db``; the key lookup made a damaged
    store exit 1 — ``eval "$(aisquare explainability env coder)"`` then started
    the session untraced (review of #170). The project's key is decoration here."""
    _settings()
    service.store_api_key(MACHINE_KEY)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr(service, "probe_proxy", lambda url: service.ProxyProbe(True, "healthy"))
    paths.db_path().write_bytes(b"this is not a SQLite database, and never was one")
    work = tmp_path / "api"
    work.mkdir()
    monkeypatch.chdir(work)

    result = runner.invoke(app, ["--json", "explainability", "env", "coder"])

    assert result.exit_code == 0, result.output
    assert _sent_key(json.loads(result.stdout)["env"]) == MACHINE_KEY
    status = runner.invoke(app, ["--json", "explainability", "status"])
    assert json.loads(status.stdout)["key_source"] == "file", status.output


def test_env_on_a_machine_with_no_store_creates_none(
    isolated_home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``context.db`` means no binding, answered without opening one — opening
    creates the file, and ``env`` is a read."""
    _settings()
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr(service, "probe_proxy", lambda url: service.ProxyProbe(True, "healthy"))
    work = tmp_path / "api"
    work.mkdir()
    monkeypatch.chdir(work)

    result = runner.invoke(app, ["explainability", "env", "coder"])

    assert result.exit_code == 0, result.output
    assert not paths.db_path().exists()


def test_doctor_still_opens_no_store_to_resolve_a_key(isolated_home: Path) -> None:
    from aisquare.services import diagnostics

    assert not isolated_home.exists()
    diagnostics.doctor()
    assert not isolated_home.exists(), "a machine-level resolve must not create the home"
