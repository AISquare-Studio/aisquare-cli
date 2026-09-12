"""Display-only persona contracts: storage, scope, safety, stable wording and commands."""

from __future__ import annotations

import io
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
import typer
from typer.testing import CliRunner

from aisquare.cli.persona import app as persona_app
from aisquare.core.personas import MAX_PACK_BYTES, PersonaPack, caption, pack_bytes, parse_pack
from aisquare.core.state import get_state
from aisquare.models import ProjectInfo, TeamEvent
from aisquare.services import personas


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    return ProjectInfo(id="project-a", root=tmp_path / "repo")


@pytest.fixture
def event() -> TeamEvent:
    return TeamEvent(
        id="e-42",
        project_id="project-a",
        session_id="s-17",
        task_id="T42",
        kind="task_blocked",
        text="Phone check FAILED: error is clipped.",
        created_at=datetime.now(UTC),
    )


def _custom(name: str = "calm-dev") -> PersonaPack:
    return personas.author_draft(name, "Friendly and brief")


def test_both_bundled_packs_cover_every_role_and_failure() -> None:
    assert {p.id for p in personas.list_packs()} == {
        "answer-first",
        "teacher",
        "board-brief",
        "careful-reviewer",
        "proactive",
        "studio",
        "mission-control",
    }
    for reference in ("studio", "mission-control"):
        pack = personas.load_pack(reference)
        assert len(pack.roles) == 8
        assert "blocked" in caption(pack, role="coder", kind="task_blocked", event_id="x")
        assert caption(pack, role="tester", kind="note", event_id="x") != caption(
            pack, role="unknown", kind="note", event_id="x"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"id": "../../escape"},
        {"id": "../"},
        {"version": "1/2/3"},
        {"schema_version": 2},
        {"hooks": {"start": "echo hi"}},
        {"tools": []},
        {"name": "bad\x1b[2J"},
        {"description": "bad\u202e"},
        {"generic": {"default": ["{role.__class__}"]}},
        {"generic": {"default": ["{role[0]}"]}},
        {"generic": {"default": ["{role!r}"]}},
        {"generic": {"default": ["{role:10000}"]}},
        {"generic": {"default": ["{unknown}"]}},
        {"generic": {"default": ["line\nline"]}},
        {"generic": {"default": ["{"]}},
        {"generic": {"default": []}},
        {"generic": {"unknown": ["hi"]}},
        {"generic": {"default": ["hi"] * 6}},
    ],
)
def test_strict_data_schema_rejects_unsafe_or_unsupported_content(change: dict[str, Any]) -> None:
    data = _custom().model_dump()
    data.update(change)
    with pytest.raises(ValueError):
        PersonaPack.model_validate(data)


def test_size_is_bounded() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        parse_pack(b" " * (MAX_PACK_BYTES + 1))


def test_deterministic_variations_and_missing_fact_fallback() -> None:
    pack = _custom()
    data = pack.model_dump()
    data["generic"]["default"] = ["Hello {task_id}", "Waiting for a record"]
    pack = PersonaPack.model_validate(data)
    assert caption(pack, role="new-role", kind="new-kind", event_id="e") == "Waiting for a record"
    result = caption(pack, role="coder", kind="new-kind", event_id="fixed", task_id="T42")
    assert all(
        caption(pack, role="coder", kind="new-kind", event_id="fixed", task_id="T42") == result
        for _ in range(10)
    )


def test_switching_preserves_original_and_numbered_seats(
    project: ProjectInfo,
    event: TeamEvent,
) -> None:
    before = event.model_dump_json()
    personas.select("use", project, reference="studio")
    studio = personas.render_event(event, project, "coder2")
    personas.select("use", project, reference="mission-control")
    mission = personas.render_event(event, project, "coder2")
    assert studio != mission
    for rendered in (studio, mission):
        assert event.text in rendered
        assert "T42" in rendered and "s-17" in rendered and "e-42" in rendered
        assert "task_blocked" in rendered
    personas.select("off", project)
    assert personas.render_caption(event, project, "coder2") == ""
    assert event.text in personas.render_event(event, project, "coder2")
    assert before == event.model_dump_json()


def test_resolution_and_off_gates(project: ProjectInfo) -> None:
    personas.select("use", None, reference="studio")
    assert personas.persona_status(project)["effective"]["coder"] == "studio@1.0.0"
    personas.select("use", project, reference="mission-control")
    personas.select("use", project, reference="studio", role="coder2")
    assert personas.persona_status(project)["effective"]["coder"] == "studio@1.0.0"
    personas.select("off", project, role="reviewer")
    personas.select("off", project)
    receipt = personas.select("use", project, reference="mission-control", role="coder3")
    assert "inactive" in receipt.message
    assert set(personas.persona_status(project)["effective"].values()) == {"off"}
    personas.select("use", project, reference="studio")
    status = personas.persona_status(project)
    assert status["effective"]["coder"] == "mission-control@1.0.0"
    assert status["effective"]["reviewer"] == "off"
    personas.select("reset", project, role="coder")
    assert personas.persona_status(project)["effective"]["coder"] == "studio@1.0.0"
    personas.select("reset", project, reset_roles=True)
    assert not personas.persona_status(project)["role_overrides"]
    with pytest.raises(ValueError, match="global role"):
        personas.select("use", None, reference="studio", role="coder")


def test_import_version_conflict_edit_export_remove(project: ProjectInfo, tmp_path: Path) -> None:
    pack = _custom()
    source = tmp_path / "pack.json"
    source.write_bytes(pack_bytes(pack))
    receipt = personas.import_pack(source)
    assert receipt.data["sha256"]
    assert "already installed" in personas.import_pack(source).message
    changed = pack.model_copy(update={"description": "Changed"})
    with pytest.raises(ValueError, match="different content"):
        personas.install_pack(changed)
    personas.select("use", project, reference=pack.id)
    personas.select("use", project, reference=pack.id, role="coder")
    draft = personas.edit_draft(pack.id)
    assert draft.version == "1.0.1"
    personas.save_draft(draft)
    assert personas.load_pack(pack.id).version == "1.0.1"
    # Choices pin a version; merely importing/editing does not switch a running view.
    assert personas.persona_status(project)["effective"]["coder"] == "calm-dev@1.0.0"
    output = tmp_path / "export.json"
    personas.export_pack(draft.reference, output)
    assert parse_pack(output.read_bytes()) == draft
    with pytest.raises(FileExistsError):
        personas.export_pack(draft.reference, output)
    receipt = personas.remove_pack(pack.reference)
    assert "project-a:coder" in receipt.data["affected"]
    assert personas.persona_status(project)["effective"]["coder"] == "off"
    assert personas.persona_status(project)["effective"]["manager"] == "off"
    assert personas.load_pack(draft.reference) == draft


def test_import_and_storage_reject_symlinks(tmp_path: Path, isolated_home: Path) -> None:
    source = tmp_path / "pack.json"
    source.write_bytes(pack_bytes(_custom()))
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        personas.import_pack(link)
    isolated_home.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    (isolated_home / "personas").symlink_to(escape, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        personas.install_pack(_custom())
    assert list(escape.iterdir()) == []


def test_damaged_selected_pack_and_settings_fall_back(
    project: ProjectInfo,
    event: TeamEvent,
    isolated_home: Path,
) -> None:
    pack = _custom()
    personas.install_pack(pack)
    personas.select("use", project, reference=pack.id)
    (isolated_home / "personas" / pack.id / pack.version / "pack.json").write_text("bad JSON")
    assert personas.render_caption(event, project, "coder") == ""
    assert event.text in personas.render_event(event, project, "coder")
    (isolated_home / "personas" / "selections.json").write_text("bad JSON")
    assert personas.render_caption(event, project, "coder") == ""


def test_shell_and_slash_actions_share_parser(project: ProjectInfo) -> None:
    assert personas.run_persona_command("/persona", project).action == "picker"
    receipt = personas.run_persona_command("/persona use mission-control --role coder2", project)
    assert receipt.data["effective"]["coder"] == "mission-control@1.0.0"
    assert (
        personas.run_persona_command("persona preview studio --role reviewer", project).action
        == "preview"
    )
    with pytest.raises(ValueError):
        personas.run_persona_command("/persona use studio; touch /tmp/pwned", project)
    with pytest.raises(ValueError):
        personas.run_persona_command('/persona add --name foo --text "unterminated', project)
    with pytest.raises(ValueError):
        personas.run_persona_command("/persona off --output ignored", project)
    with pytest.raises(ValueError):
        personas.run_persona_command("/persona use studio --global --project x", project)


def test_authoring_never_generates_voice_or_activates(project: ProjectInfo) -> None:
    receipt = personas.run_persona_command(
        '/persona add --name my-voice --text "Calm and brief"', project
    )
    assert "unchanged" in receipt.message and "no AI generation" in receipt.message
    assert receipt.editor is not None
    assert receipt.editor.generic == personas.load_pack("studio").generic
    assert all(pack.id != "my-voice" for pack in personas.list_packs())
    personas.save_draft(receipt.editor)
    assert set(personas.persona_status(project)["effective"].values()) == {"off"}


def test_registered_cwd_scope_never_uses_unrelated_pinned_project(
    tmp_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.services import project as projects

    monkeypatch.setattr(projects, "list_projects", lambda: [project])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="registered project"):
        personas.run_persona_command("use studio")
    project.root.mkdir()
    monkeypatch.chdir(project.root)
    assert personas.run_persona_command("use studio").data["scope"] == project.id
    assert personas.run_persona_command("use mission-control --global").data["scope"] == "global"


def test_concurrent_role_changes_preserve_each_other(project: ProjectInfo) -> None:
    def update(role: str) -> None:
        personas.select("use", project, reference="studio", role=role)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, ["coder", "reviewer", "tester", "manager"]))
    assert len(personas.persona_status(project)["role_overrides"]) == 4


class _Response(io.BytesIO):
    headers: ClassVar[dict[str, str]] = {}


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 10
        assert request.full_url.startswith("https://")
        return self.response


def test_https_download_is_bounded_and_saves_provenance(
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
) -> None:
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *args: _Opener(_Response(pack_bytes(_custom()))),
    )
    personas.download_pack("https://example.org/pack.json")
    metadata = json.loads((isolated_home / "personas/calm-dev/1.0.0/provenance.json").read_text())
    assert metadata["source"] == "https://example.org/pack.json"
    assert len(metadata["sha256"]) == 64
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *args: _Opener(_Response(b" " * (MAX_PACK_BYTES + 1))),
    )
    with pytest.raises(ValueError, match="size limit"):
        personas.download_pack("https://example.org/too-big.json")
    assert personas.load_pack("calm-dev").version == "1.0.0"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/x",
        "file:///etc/passwd",
        "https://user:pass@example.org/x",
        "https://example.org/x#fragment",
    ],
)
def test_download_rejects_other_schemes_and_credentials(url: str) -> None:
    with pytest.raises(ValueError):
        personas.download_pack(url)


def test_real_cli_family_and_noninteractive_authoring() -> None:
    app = typer.Typer()
    app.add_typer(persona_app, name="persona")
    runner = CliRunner()
    assert runner.invoke(app, ["persona", "list"]).exit_code == 0
    assert runner.invoke(app, ["persona", "use", "studio", "--global"]).exit_code == 0
    result = runner.invoke(app, ["persona", "add", "--name", "x", "--text", "friendly"])
    assert result.exit_code == 2
    assert "interactive terminal" in result.output
    get_state().json_output = True
    result = runner.invoke(app, ["persona", "status", "--global"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["global_default"] == "studio@1.0.0"


def test_duplicate_keys_and_boolean_schema_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_pack(b'{"id":"safe","id":"different"}')
    data = _custom().model_dump()
    data["schema_version"] = True
    with pytest.raises(ValueError, match="schema_version"):
        PersonaPack.model_validate(data)


def test_persistence_across_a_fresh_process() -> None:
    import subprocess
    import sys

    personas.install_pack(_custom())
    personas.select("use", None, reference="calm-dev")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from aisquare.services.personas import persona_status; "
            "print(persona_status()['effective']['coder'])",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "calm-dev@1.0.0"


def test_failed_atomic_publish_preserves_selected_version(
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
) -> None:
    import os

    personas.install_pack(_custom())
    personas.select("use", project, reference="calm-dev")
    before = (isolated_home / "personas/selections.json").read_bytes()

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError("simulated full disk")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="full disk"):
        personas.select("use", project, reference="mission-control")
    assert (isolated_home / "personas/selections.json").read_bytes() == before
    assert personas.persona_status(project)["effective"]["coder"] == "calm-dev@1.0.0"
    assert not list((isolated_home / "personas").glob(".persona-*"))


def test_real_https_download_and_redirect_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    import ssl
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("local HTTPS fixture needs openssl")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    content = pack_bytes(_custom("downloaded"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://example.org/unsafe")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_builder = urllib.request.build_opener
    client_context = ssl.create_default_context(cafile=str(cert))

    def trusted_local_builder(*handlers: Any) -> urllib.request.OpenerDirector:
        return original_builder(*handlers, urllib.request.HTTPSHandler(context=client_context))

    monkeypatch.setattr(urllib.request, "build_opener", trusted_local_builder)
    try:
        base = f"https://127.0.0.1:{server.server_port}"
        result = personas.download_pack(f"{base}/pack.json")
        assert result.data["reference"] == "downloaded@1.0.0"
        with pytest.raises(ValueError, match="redirect"):
            personas.download_pack(f"{base}/redirect")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_actual_author_role_is_resolved_not_recipient(
    project: ProjectInfo, event: TeamEvent
) -> None:
    from aisquare.core.store import store_session
    from aisquare.models import TeamSession

    with store_session() as store:
        store.ensure_project(project)
        store.upsert_session(
            TeamSession(
                id="s-17",
                project_id=project.id,
                role="coder2",
                started_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
    event = event.model_copy(update={"to_role": "reviewer", "kind": "task_claimed"})
    personas.select("use", project, reference="studio")
    rendered = personas.render_caption(event, project)
    assert "coder2" in rendered and "I've picked up" in rendered
    assert "reviewer" not in rendered


def test_selected_project_relative_import_and_text_file(project: ProjectInfo) -> None:
    project.root.mkdir()
    (project.root / "pack.json").write_bytes(pack_bytes(_custom("relative")))
    result = personas.run_persona_command("/persona add ./pack.json", project)
    assert result.data["reference"] == "relative@1.0.0"
    (project.root / "description.txt").write_text("Calm and clear")
    result = personas.run_persona_command(
        "/persona add --name readable --text-file ./description.txt", project
    )
    assert result.editor is not None and result.editor.description == "Calm and clear"
    assert personas.persona_status(project)["enabled"] is False


def test_edit_preserves_original_bundled_pack() -> None:
    original = personas.load_pack("studio@1.0.0")
    draft = personas.edit_draft("studio")
    data = draft.model_dump()
    data["generic"]["task_blocked"] = ["A blocker needs attention; inspect the original record."]
    personas.save_draft(PersonaPack.model_validate(data))
    assert personas.load_pack("studio@1.0.0") == original
    assert personas.load_pack("studio").version == "1.0.1"


def test_manual_editor_uses_local_argv_without_click_or_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shlex
    import sys

    from aisquare.cli.persona import _edit_pack

    script = tmp_path / "my editor.py"
    script.write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "assert 'AISQUARE_PIPELINE_ID' not in os.environ\n"
        "path = Path(sys.argv[1])\npath.write_text(path.read_text().replace('Friendly', 'Calm'))\n"
    )
    monkeypatch.setenv("VISUAL", shlex.join([sys.executable, str(script)]))
    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "test-parent-trace")
    result = _edit_pack(pack_bytes(_custom()).decode())
    assert result is not None
    assert parse_pack(result.encode()).description == "Calm and brief"
    # Nonzero editor exits never save a partial pack.
    script.write_text("raise SystemExit(3)\n")
    with pytest.raises(ValueError, match="editor exited"):
        _edit_pack(pack_bytes(_custom()).decode())
