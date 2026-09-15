"""``aisquare persona`` end to end, through the CLI, in an isolated home.

docs/plans/spawn-personas.md §7 "P1". ``CLAUDE_CONFIG_DIR`` points at a temp
directory, so no test reads or writes the developer's own Claude skills, and
every test runs outside any repository unless it asks for one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import personas as core

BUNDLED = ["careful", "mentor", "minimalist", "skeptic"]


@pytest.fixture(autouse=True)
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return config


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    monkeypatch.chdir(path)
    return path.resolve()


def _user_layer() -> Path:
    return dict(core.layer_dirs(None))["user"]


def _skill(
    base: Path,
    name: str,
    *,
    description: str = "A skill for tests.",
    files: dict[str, bytes] | None = None,
) -> Path:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nWork carefully.\n", encoding="utf-8"
    )
    for relative, content in (files or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return directory


def _tree(directory: Path) -> dict[str, bytes]:
    """Every file under ``directory`` by relative path, the sidecar aside."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != ".persona.json"
    }


def _json(runner: CliRunner, *argv: str, input: str | None = None) -> tuple[int, Any]:
    result = runner.invoke(app, ["--json", "persona", *argv], input=input)
    return result.exit_code, json.loads(result.stdout)


def _editor(tmp_path: Path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_list_prints_the_bundled_personas_with_layer_and_description(runner: CliRunner) -> None:
    result = runner.invoke(app, ["persona", "list"])

    assert result.exit_code == 0, result.output
    rows = result.stdout.splitlines()
    assert [row.split()[0] for row in rows] == BUNDLED
    assert rows[3].split(None, 2) == ["skeptic", "bundled", core.resolve("skeptic").description]


def test_list_json_carries_each_field_and_the_directories_that_did_not_load(
    runner: CliRunner,
) -> None:
    (_user_layer() / "broken").mkdir(parents=True)

    code, payload = _json(runner, "list")

    assert code == 0
    assert set(payload) == {"personas", "invalid"}
    assert [entry["name"] for entry in payload["personas"]] == BUNDLED
    skeptic = payload["personas"][3]
    assert set(skeptic) >= {"name", "description", "roles", "tags", "layer", "path", "files"}
    assert skeptic["layer"] == "bundled"
    assert skeptic["roles"] == ["tester", "reviewer", "runner"]
    assert payload["invalid"] == [{"path": str(_user_layer() / "broken"), "reason": "no SKILL.md"}]


def test_show_prints_exactly_the_briefing_then_provenance_and_files(runner: CliRunner) -> None:
    result = runner.invoke(app, ["persona", "show", "skeptic"])

    expected = "\n".join(core.briefing(core.resolve("skeptic")))
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(expected + "\n\n")
    assert expected.splitlines()[-1] == core.guard_sentence("skeptic")
    tail = result.stdout[len(expected) :].splitlines()
    assert tail[2].startswith("provenance: ")
    assert tail[3] == "files: SKILL.md only"


def test_show_json_carries_frontmatter_body_briefing_and_provenance(runner: CliRunner) -> None:
    code, payload = _json(runner, "show", "skeptic")

    persona = core.resolve("skeptic")
    assert code == 0
    assert payload["frontmatter"] == persona.frontmatter
    assert payload["body"] == persona.body
    assert payload["briefing"] == "\n".join(core.briefing(persona))
    assert payload["provenance"] is None


def test_show_of_an_unknown_name_is_one_line_not_a_traceback(runner: CliRunner) -> None:
    human = runner.invoke(app, ["persona", "show", "nope"])
    code, payload = _json(runner, "show", "nope")

    assert human.exit_code == 1
    assert isinstance(human.exception, SystemExit)
    assert "known: careful, mentor, minimalist, skeptic" in human.stderr
    assert (code, payload["error"]) == (1, "unknown_persona")
    assert "no persona named 'nope'" in payload["detail"]


def test_import_copies_a_directory_refuses_a_second_time_and_renames_only_the_directory(
    runner: CliRunner, tmp_path: Path
) -> None:
    source = _skill(tmp_path / "src", "some-skill", files={"references/guide.md": b"guide\n"})

    first = runner.invoke(app, ["persona", "import", str(source), "--user"])

    dest = _user_layer() / "some-skill"
    assert first.exit_code == 0, first.output
    assert _tree(dest) == _tree(source)
    sidecar = json.loads((dest / ".persona.json").read_text(encoding="utf-8"))
    assert sidecar["engine"] == "copy"
    assert len(sidecar["source_sha256"]) == 64

    code, payload = _json(runner, "import", str(source), "--user")
    assert (code, payload["error"]) == (1, "persona_exists")

    renamed = runner.invoke(app, ["persona", "import", str(source), "--user", "--name", "other"])
    assert renamed.exit_code == 0, renamed.output
    assert _tree(_user_layer() / "other") == _tree(source)
    assert "differs from the directory 'other'" in renamed.stderr


def test_an_agent_shaped_markdown_file_lands_as_its_stem_skill_md(
    runner: CliRunner, tmp_path: Path
) -> None:
    agent = tmp_path / "agents" / "reviewer.md"
    agent.parent.mkdir()
    data = (
        b"---\nname: reviewer\ndescription: Reviews diffs for correctness.\n"
        b"tools: Read, Grep\nmodel: sonnet\n---\nYou review code.\n"
    )
    agent.write_bytes(data)

    result = runner.invoke(app, ["persona", "import", str(agent)])

    assert result.exit_code == 0, result.output
    assert (_user_layer() / "reviewer" / "SKILL.md").read_bytes() == data
    assert "'tools' is not a documented Claude Code skill key" in result.stderr


def test_import_dash_reads_stdin(runner: CliRunner) -> None:
    data = "---\nname: piped\ndescription: From a pipe.\n---\nBody.\n"

    plain = runner.invoke(app, ["persona", "import", "-"], input=data)
    named = runner.invoke(app, ["persona", "import", "-", "--name", "renamed"], input=data)
    code, payload = _json(runner, "import", "-", input="")

    assert plain.exit_code == 0, plain.output
    assert (_user_layer() / "piped" / "SKILL.md").read_bytes() == data.encode()
    assert named.exit_code == 0, named.output
    assert (_user_layer() / "renamed" / "SKILL.md").is_file()
    assert (code, payload["error"]) == (1, "source_empty")


def test_plain_text_goes_to_the_llm_path_and_no_llm_refuses_it(
    runner: CliRunner, tmp_path: Path
) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("remember to be kind\n", encoding="utf-8")

    human = runner.invoke(app, ["persona", "import", str(notes), "--no-llm"])
    code, payload = _json(runner, "import", str(notes), "--no-llm")
    engines_code, engines = _json(runner, "import", str(notes))
    missing_code, missing = _json(runner, "import", str(tmp_path / "absent.md"))

    assert human.exit_code == 1
    assert isinstance(human.exception, SystemExit)
    assert "not a recognised skill" in human.stderr
    assert "--no-llm forbids it" in human.stderr
    assert (code, payload["error"]) == (1, "not_recognised")
    # conftest's no_real_llm_import: without --no-llm the engines are tried, and none can run.
    assert (engines_code, engines["error"]) == (1, "no_import_engine")
    assert "aisquare-cli[llm]" in engines["detail"]
    assert (missing_code, missing["error"]) == (1, "source_not_found")
    assert not _user_layer().exists()


def test_import_list_shows_claude_skills_and_marks_the_imported_ones(
    runner: CliRunner, repo: Path, claude_dir: Path
) -> None:
    _skill(claude_dir / "skills", "personal-one", description="Mine.")
    _skill(repo / ".claude" / "skills", "project-one", description="Ours.")
    _skill(claude_dir / "skills" / "synced", "from-claude-ai")

    code, payload = _json(runner, "import", "--list")

    assert code == 0
    by_name = {skill["name"]: skill for skill in payload["skills"]}
    assert set(by_name) == {"personal-one", "project-one"}
    assert (by_name["personal-one"]["scope"], by_name["personal-one"]["description"]) == (
        "user",
        "Mine.",
    )
    assert by_name["project-one"]["scope"] == "project"
    assert not any(skill["imported"] for skill in payload["skills"])

    imported = runner.invoke(app, ["persona", "import", "project-one"])
    assert imported.exit_code == 0, imported.output
    assert _tree(_user_layer() / "project-one") == _tree(
        repo / ".claude" / "skills" / "project-one"
    )

    listed = runner.invoke(app, ["persona", "import", "--list"])
    rows = {row.split()[0]: row for row in listed.stdout.splitlines()}
    assert "imported" in rows["project-one"]
    assert "imported" not in rows["personal-one"]


def test_a_skill_that_only_shares_a_bundled_name_is_not_imported_and_says_why(
    runner: CliRunner, claude_dir: Path
) -> None:
    """The validator's case: a gstack `careful` skill beside the bundled `careful` persona."""
    _skill(claude_dir / "skills", "careful", description="Safety guardrails.")

    code, payload = _json(runner, "import", "--list")
    listed = runner.invoke(app, ["persona", "import", "--list"])

    assert code == 0
    (skill,) = payload["skills"]
    assert (skill["name"], skill["imported"], skill["taken_by"]) == ("careful", False, "bundled")
    (row,) = listed.stdout.splitlines()
    assert "imported" not in row
    assert row.endswith("Safety guardrails.  (name taken by bundled careful)")


def test_imported_is_decided_by_provenance_even_under_another_name(
    runner: CliRunner, claude_dir: Path
) -> None:
    _skill(claude_dir / "skills", "careful", description="Safety guardrails.")
    _skill(claude_dir / "skills", "notes", description="Mine.")

    renamed = runner.invoke(app, ["persona", "import", "careful", "--name", "guardrails"])
    same = runner.invoke(app, ["persona", "import", "notes"])
    code, payload = _json(runner, "import", "--list")

    assert renamed.exit_code == 0, renamed.output
    assert same.exit_code == 0, same.output
    assert code == 0
    by_name = {skill["name"]: skill for skill in payload["skills"]}
    assert set(by_name) == {"careful", "notes"}, "the renamed persona is not listed as a skill"
    assert (by_name["careful"]["imported"], by_name["careful"]["taken_by"]) == (True, "bundled")
    assert (by_name["notes"]["imported"], by_name["notes"]["taken_by"]) == (True, None)


def test_a_same_named_persona_from_another_source_does_not_mark_the_skill_imported(
    runner: CliRunner, claude_dir: Path, tmp_path: Path
) -> None:
    _skill(claude_dir / "skills", "notes", description="Mine.")
    elsewhere = _skill(tmp_path / "elsewhere", "notes", description="Someone else's.")

    imported = runner.invoke(app, ["persona", "import", str(elsewhere), "--user"])
    code, payload = _json(runner, "import", "--list")

    assert imported.exit_code == 0, imported.output
    assert code == 0
    (skill,) = payload["skills"]
    assert (skill["imported"], skill["taken_by"]) == (False, "user")


def test_export_prints_writes_a_directory_and_refuses_an_existing_target(
    runner: CliRunner, tmp_path: Path, claude_dir: Path
) -> None:
    bundled = (core.BUNDLED_DIR / "skeptic" / "SKILL.md").read_bytes()
    out = tmp_path / "out"

    printed = runner.invoke(app, ["persona", "export", "skeptic"])
    written = runner.invoke(app, ["persona", "export", "skeptic", "--to", str(out)])
    code, payload = _json(runner, "export", "skeptic", "--to", str(out))
    forced = runner.invoke(app, ["persona", "export", "skeptic", "--to", str(out), "--force"])

    assert printed.stdout.encode("utf-8") == bundled
    assert written.exit_code == 0, written.output
    assert (out / "skeptic" / "SKILL.md").read_bytes() == bundled
    sidecar = json.loads((out / "skeptic" / ".persona.json").read_text(encoding="utf-8"))
    assert sidecar["source"] == str(core.BUNDLED_DIR / "skeptic")
    assert (code, payload["error"]) == (1, "target_exists")
    assert forced.exit_code == 0, forced.output


def test_export_skill_user_writes_into_claudes_skills(
    runner: CliRunner, claude_dir: Path, repo: Path
) -> None:
    result = runner.invoke(app, ["persona", "export", "skeptic", "--skill", "--user"])
    project = runner.invoke(app, ["persona", "export", "mentor", "--skill", "--project"])
    code, payload = _json(runner, "export", "skeptic", "--skill")

    assert result.exit_code == 0, result.output
    assert (claude_dir / "skills" / "skeptic" / "SKILL.md").read_bytes() == (
        core.BUNDLED_DIR / "skeptic" / "SKILL.md"
    ).read_bytes()
    assert "/skeptic in Claude Code" in result.stdout
    assert project.exit_code == 0, project.output
    assert (repo / ".claude" / "skills" / "mentor" / "SKILL.md").is_file()
    assert (code, payload["error"]) == (1, "usage")


def test_a_skill_imported_then_exported_is_byte_identical(
    runner: CliRunner, tmp_path: Path
) -> None:
    files = {
        "SKILL.md": (
            b"---\n"
            b"name: reviewer-pro\n"
            b"description: >\n"
            b"  Reviews a diff the way a careful senior engineer would,\n"
            b"  naming the risk before the style.\n"
            b"allowed-tools: Read, Grep, Bash(git diff:*)\n"
            b"metadata:\n"
            b"  persona-roles: reviewer\n"
            b"  persona-tags: review, quality\n"
            b"  author: someone\n"
            b"---\n"
            b"# Reviewer\n"
            b"\n"
            b"Read the diff twice.\n"
            b"\n"
            b"    an indented line, kept\n"
        ),
        "references/checklist.md": b"- correctness\n- tests\n",
        "scripts/diff.sh": b"#!/bin/sh\ngit diff\n",
    }
    source = tmp_path / "src" / "reviewer-pro"
    for relative, content in files.items():
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_bytes(content)
    out = tmp_path / "out"

    imported = runner.invoke(app, ["persona", "import", str(source), "--user"])
    exported = runner.invoke(app, ["persona", "export", "reviewer-pro", "--to", str(out)])

    assert imported.exit_code == 0, imported.output
    assert exported.exit_code == 0, exported.output
    assert _tree(out / "reviewer-pro") == files
    assert (out / "reviewer-pro" / ".persona.json").is_file()


def test_new_in_a_repository_scaffolds_the_project_layer_and_opens_the_editor(
    runner: CliRunner, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "opened.log"
    monkeypatch.setenv("EDITOR", _editor(tmp_path, "log.sh", f'echo "$1" >> "{log}"'))

    result = runner.invoke(app, ["persona", "new", "pair", "--project"])

    assert result.exit_code == 0, result.output
    assert (repo / ".aisquare" / "personas" / "pair" / "SKILL.md").is_file()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    listed = runner.invoke(app, ["persona", "list"]).stdout.splitlines()
    assert next(row for row in listed if row.startswith("pair")).split()[1] == "project"


def test_new_with_editor_true_leaves_the_valid_template(
    runner: CliRunner, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDITOR", "true")

    result = runner.invoke(app, ["persona", "new", "pair", "--project"])

    assert result.exit_code == 0, result.output
    persona = core.resolve("pair", repo)
    assert (persona.layer, persona.frontmatter["name"]) == ("project", "pair")


def test_new_refuses_the_project_layer_outside_a_repository(runner: CliRunner) -> None:
    code, payload = _json(runner, "new", "pair", "--project")

    assert (code, payload["error"]) == (1, "no_project")


def test_new_without_an_editor_scaffolds_and_says_how_to_edit(runner: CliRunner) -> None:
    result = runner.invoke(app, ["persona", "new", "pair"])

    assert result.exit_code == 0, result.output
    assert "aisquare persona edit pair" in result.stdout
    assert core.resolve("pair").layer == "user"


def test_edit_refuses_a_bundled_persona_and_names_the_copy_commands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["persona", "edit", "skeptic"])

    assert result.exit_code == 1
    assert "bundled persona" in result.stderr
    assert "aisquare persona export skeptic --to DIR" in result.stderr
    assert "aisquare persona import DIR/skeptic --user" in result.stderr


def test_edit_that_writes_invalid_text_leaves_the_old_file_and_exits_1(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _skill(_user_layer(), "mine") / "SKILL.md"
    before = skill.read_bytes()
    monkeypatch.setenv("EDITOR", _editor(tmp_path, "break.sh", "printf 'plain words\\n' > \"$1\""))

    result = runner.invoke(app, ["persona", "edit", "mine"])

    assert result.exit_code == 1
    assert "no frontmatter" in result.stderr
    assert "not saved" in result.stderr
    assert skill.read_bytes() == before


def test_edit_reports_unchanged_and_refuses_without_an_editor(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skill(_user_layer(), "mine")

    code, payload = _json(runner, "edit", "mine")
    monkeypatch.setenv("EDITOR", "true")
    unchanged = runner.invoke(app, ["persona", "edit", "mine"])

    assert (code, payload["error"]) == (1, "no_editor")
    assert unchanged.exit_code == 0, unchanged.output
    assert unchanged.stdout.startswith("unchanged: ")


def test_validate_passes_with_warnings(runner: CliRunner, tmp_path: Path) -> None:
    directory = tmp_path / "check" / "dir-name"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: label\ndescription: fine\nversion: 3\n---\n" + "w" * 4_001 + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["persona", "validate", str(directory)])

    assert result.exit_code == 0, result.output
    assert "'version' is not a documented Claude Code skill key" in result.stderr
    assert "frontmatter name 'label' differs" in result.stderr
    assert "4,001 characters, over the 4,000 soft cap" in result.stderr


@pytest.mark.parametrize(
    ("dirname", "text", "fragment"),
    [
        ("bad-yaml", "---\nname: bad-yaml\ndescription: fine: colon\n---\nbody\n", "line 3: "),
        (
            "big-front",
            "---\ndescription: fine\npad: '" + "p" * 16_400 + "'\n---\nbody\n",
            "over the 16,384-byte cap",
        ),
        ("no-description", "---\nname: no-description\n---\nbody\n", "no description"),
        ("empty-description", "---\ndescription: ''\n---\nbody\n", "no description"),
        ("empty-body", "---\ndescription: fine\n---\n\n", "the body is empty"),
        ("huge-body", "---\ndescription: fine\n---\n" + "h" * 12_001 + "\n", "12,001 characters"),
        ("Bad_Name", "---\ndescription: fine\n---\nbody\n", "skill-name rule"),
        ("synced", "---\ndescription: fine\n---\nbody\n", "reserved"),
    ],
)
def test_validate_refuses_with_the_rule(
    runner: CliRunner, tmp_path: Path, dirname: str, text: str, fragment: str
) -> None:
    directory = tmp_path / "check" / dirname
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")

    result = runner.invoke(app, ["persona", "validate", str(directory)])

    assert result.exit_code == 1
    assert fragment in result.stderr


def test_rm_removes_a_user_persona_and_refuses_a_bundled_one(runner: CliRunner) -> None:
    directory = _skill(_user_layer(), "x")

    removed = runner.invoke(app, ["persona", "rm", "x", "--user"])
    bundled = runner.invoke(app, ["persona", "rm", "skeptic"])
    code, payload = _json(runner, "rm", "x", "--user")

    assert removed.exit_code == 0, removed.output
    assert not directory.exists()
    assert bundled.exit_code == 1
    assert "bundled persona" in bundled.stderr
    assert (core.BUNDLED_DIR / "skeptic" / "SKILL.md").is_file()
    assert (code, payload["error"]) == (1, "unknown_persona")


def test_a_project_persona_shadows_bundled_and_a_broken_one_is_listed_not_fatal(
    runner: CliRunner, repo: Path
) -> None:
    _skill(repo / ".aisquare" / "personas", "skeptic", description="Our own skeptic.")
    (_user_layer() / "broken").mkdir(parents=True)

    result = runner.invoke(app, ["persona", "list"])

    assert result.exit_code == 0, result.output
    rows = result.stdout.splitlines()
    assert [row.split()[0] for row in rows if not row.startswith("✗")] == BUNDLED
    skeptic = next(row for row in rows if row.startswith("skeptic"))
    assert skeptic.split()[1] == "project"
    assert skeptic.endswith("Our own skeptic.  (shadows bundled)")
    assert f"✗ {_user_layer() / 'broken'}: no SKILL.md" in rows


# --- persona attach (docs/plans/spawn-personas.md §7 "P8") ---------------------------------


def _attach_fakes(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> list[tuple[object, str, str, str | None]]:
    """The fleet service faked on the module the CLI calls through; returns the calls."""
    from datetime import UTC, datetime

    from aisquare.models import FleetAgent, ProjectInfo
    from aisquare.services import fleet as fleet_service

    project = ProjectInfo(id="prj_attach", root=Path("/tmp/attach"), codename="amber-otter")
    calls: list[tuple[object, str, str, str | None]] = []

    def attach(
        target: ProjectInfo, label: str, name: str, *, sender: str | None = None
    ) -> fleet_service.AttachReceipt:
        calls.append((target, label, name, sender))
        if isinstance(outcome, Exception):
            raise outcome
        agent = FleetAgent(
            id="agt_coderx",
            project_id=project.id,
            label=label,
            role="coder",
            pane_id="%7",
            cwd=project.root,
            created_at=datetime(2026, 9, 15, tzinfo=UTC),
            persona=name,
        )
        return fleet_service.AttachReceipt(
            agent=agent,
            persona=name,
            replaced="minimalist",
            delivered="typed",
            how="typed into its pane (it was waiting)",
        )

    monkeypatch.setattr(fleet_service, "resolve_project", lambda ref=None, **_: project)
    monkeypatch.setattr(fleet_service, "attach_persona", attach)
    return calls


def test_attach_prints_the_receipt_and_json_carries_delivered(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _attach_fakes(monkeypatch, None)

    human = runner.invoke(app, ["persona", "attach", "skeptic", "--to", "coder-x", "--as", "mgr-1"])
    code, payload = _json(runner, "attach", "skeptic", "--to", "coder-x")

    assert human.exit_code == 0, human.output
    assert human.stdout.splitlines()[0] == "✓ attached skeptic to coder-x (typed)"
    assert calls[0][1:] == ("coder-x", "skeptic", "mgr-1")
    assert code == 0
    assert (payload["delivered"], payload["replaced"], payload["label"]) == (
        "typed",
        "minimalist",
        "coder-x",
    )


def test_attach_refusals_report_the_fleets_error_codes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import fleet as fleet_service

    _attach_fakes(monkeypatch, fleet_service.NoSuchAgent("no live agent 'coder-9' in api"))

    code, payload = _json(runner, "attach", "skeptic", "--to", "coder-9")

    assert (code, payload["error"]) == (1, "no_such_agent")
    assert "no live agent 'coder-9'" in payload["detail"]


def test_import_help_names_the_config_table_behind_both_defaults(runner: CliRunner) -> None:
    """Rich reads a bare `[persona.import]` as a style tag and drops it: `(default:  engine)`."""
    result = runner.invoke(
        app, ["persona", "import", "--help"], env={"NO_COLOR": "1", "COLUMNS": "200"}
    )

    assert result.exit_code == 0, result.output
    page = " ".join(result.output.split())
    assert "(default: [persona.import] engine)" in page
    assert "(default: [persona.import] api_model)" in page
