"""End-to-end CLI behaviour for the implemented context commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app


@pytest.fixture(autouse=True)
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run each test inside a fresh project directory so the project pool is scoped."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _json(result_output: str) -> Any:
    return json.loads(result_output)


def _add(runner: CliRunner, text: str, *flags: str) -> str:
    """Add an entry via the CLI and return its id."""
    result = runner.invoke(app, ["--json", "context", "add", text, *flags])
    assert result.exit_code == 0, result.output
    entry_id = _json(result.stdout)["id"]
    assert isinstance(entry_id, str)
    return entry_id


def test_remember_defaults_to_the_project_pool(runner: CliRunner) -> None:
    result = runner.invoke(app, ["remember", "use uv for envs"])
    assert result.exit_code == 0, result.output
    assert "✓ remembered (project): use uv for envs" in result.stdout


def test_add_to_user_pool(runner: CliRunner) -> None:
    result = runner.invoke(app, ["context", "add", "prefer tabs", "--user"])
    assert result.exit_code == 0, result.output
    assert "✓ added (user): prefer tabs" in result.stdout


def test_add_json_returns_the_entry(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "context", "add", "json shape", "--user", "--tag", "x"])
    assert result.exit_code == 0, result.output
    payload = _json(result.stdout)
    assert payload["pool"] == "user"
    assert payload["project_id"] is None
    assert payload["text"] == "json shape"
    assert payload["tags"] == ["x"]
    assert payload["id"].startswith("ctx_")
    assert payload["deleted_at"] is None
    assert payload["created_at"] == payload["updated_at"]


def test_add_then_list_round_trips(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "alpha", "--user", "--tag", "t1"])
    result = runner.invoke(app, ["--json", "context", "list"])
    assert result.exit_code == 0, result.output
    entries = _json(result.stdout)
    assert [e["text"] for e in entries] == ["alpha"]
    assert entries[0]["tags"] == ["t1"]


def test_list_empty_is_an_empty_json_array(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "context", "list"])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout) == []


def test_list_human_shows_entries(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "tabs", "--user"])
    result = runner.invoke(app, ["context", "list"])
    assert result.exit_code == 0, result.output
    assert "tabs" in result.stdout
    assert "user" in result.stdout


def test_list_empty_human_hint(runner: CliRunner) -> None:
    result = runner.invoke(app, ["context", "list"])
    assert result.exit_code == 0, result.output
    assert "No context entries yet" in result.stdout


def test_ctx_alias_adds(runner: CliRunner) -> None:
    result = runner.invoke(app, ["ctx", "add", "via alias", "--user"])
    assert result.exit_code == 0, result.output
    assert "✓ added (user): via alias" in result.stdout


def test_mutually_exclusive_pools_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(app, ["context", "add", "x", "--user", "--project"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_project_pool_is_scoped_to_its_directory(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["context", "add", "only-here", "--project"])
    runner.invoke(app, ["context", "add", "everywhere", "--user"])

    elsewhere = tmp_path / "another"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = runner.invoke(app, ["--json", "context", "list"])
    assert result.exit_code == 0, result.output
    texts = {e["text"] for e in _json(result.stdout)}
    assert texts == {"everywhere"}  # the other project's entry is out of scope


def test_show_displays_the_entry(runner: CliRunner) -> None:
    entry_id = _add(runner, "showme", "--user", "--tag", "k")
    result = runner.invoke(app, ["context", "show", entry_id])
    assert result.exit_code == 0, result.output
    assert "showme" in result.stdout
    assert entry_id in result.stdout
    assert "user" in result.stdout


def test_show_resolves_a_prefix(runner: CliRunner) -> None:
    entry_id = _add(runner, "by prefix", "--user")
    result = runner.invoke(app, ["--json", "context", "show", entry_id[:14]])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["id"] == entry_id


def test_show_unknown_id_fails(runner: CliRunner) -> None:
    result = runner.invoke(app, ["context", "show", "ctx_nope"])
    assert result.exit_code == 1
    assert "no context entry matches" in result.output


def test_show_ambiguous_id_fails(runner: CliRunner) -> None:
    _add(runner, "one", "--user")
    _add(runner, "two", "--user")
    result = runner.invoke(app, ["--json", "context", "show", "ctx"])
    assert result.exit_code == 1
    assert _json(result.stdout) == {"error": "ambiguous_id", "ref": "ctx"}


def test_edit_saves_changes(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    entry_id = _add(runner, "before", "--user")
    monkeypatch.setattr("aisquare.services.context.edit_text", lambda _text: "after")
    result = runner.invoke(app, ["context", "edit", entry_id])
    assert result.exit_code == 0, result.output
    assert "✓ updated (user): after" in result.stdout
    shown = runner.invoke(app, ["--json", "context", "show", entry_id])
    assert _json(shown.stdout)["text"] == "after"


def test_edit_aborted_leaves_entry_unchanged(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = _add(runner, "keep me", "--user")
    monkeypatch.setattr("aisquare.services.context.edit_text", lambda _text: None)
    result = runner.invoke(app, ["--json", "context", "edit", entry_id])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout)["text"] == "keep me"


def test_remove_deletes_the_entry(runner: CliRunner) -> None:
    entry_id = _add(runner, "delete me", "--user")
    result = runner.invoke(app, ["context", "remove", entry_id])
    assert result.exit_code == 0, result.output
    assert "✓ removed" in result.stdout
    listing = runner.invoke(app, ["--json", "context", "list"])
    assert _json(listing.stdout) == []


def test_remove_unknown_id_fails(runner: CliRunner) -> None:
    result = runner.invoke(app, ["context", "remove", "ctx_nope"])
    assert result.exit_code == 1
    assert "no context entry matches" in result.output


def test_search_finds_matches(runner: CliRunner) -> None:
    _add(runner, "prefer pytest", "--user")
    _add(runner, "use ruff", "--user")
    result = runner.invoke(app, ["--json", "context", "search", "pytest"])
    assert result.exit_code == 0, result.output
    assert [e["text"] for e in _json(result.stdout)] == ["prefer pytest"]


def test_search_no_matches_message(runner: CliRunner) -> None:
    _add(runner, "something", "--user")
    result = runner.invoke(app, ["context", "search", "zzznomatch"])
    assert result.exit_code == 0, result.output
    assert "No entries match" in result.stdout


def test_promote_moves_entry_to_user_pool(runner: CliRunner) -> None:
    entry_id = _add(runner, "promote me", "--project")
    result = runner.invoke(app, ["context", "promote", entry_id])
    assert result.exit_code == 0, result.output
    assert "✓ promoted (user): promote me" in result.stdout
    shown = runner.invoke(app, ["--json", "context", "show", entry_id])
    payload = _json(shown.stdout)
    assert payload["pool"] == "user"
    assert payload["project_id"] is None


def test_promote_user_entry_fails(runner: CliRunner) -> None:
    entry_id = _add(runner, "already global", "--user")
    result = runner.invoke(app, ["context", "promote", entry_id])
    assert result.exit_code == 1
    assert "already in the user pool" in result.output


def test_export_markdown_to_file(runner: CliRunner, tmp_path: Path) -> None:
    _add(runner, "prefer tabs", "--user", "--tag", "style")
    _add(runner, "run make check", "--project")
    out = tmp_path / "context.md"
    result = runner.invoke(app, ["context", "export", str(out), "--format", "md"])
    assert result.exit_code == 0, result.output
    assert f"✓ exported to {out}" in result.stdout
    text = out.read_text(encoding="utf-8")
    assert "## User" in text
    assert "- prefer tabs #style" in text
    assert "## Project" in text
    assert "- run make check" in text


def test_export_json_to_stdout(runner: CliRunner) -> None:
    _add(runner, "only entry", "--user", "--tag", "a")
    result = runner.invoke(app, ["context", "export", "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = _json(result.stdout)
    assert [(e["text"], e["tags"]) for e in payload] == [("only entry", ["a"])]


def test_import_json_adds_entries(runner: CliRunner, tmp_path: Path) -> None:
    src = tmp_path / "in.json"
    src.write_text(
        json.dumps(
            [
                {"text": "imported one", "tags": ["x"], "pool": "user"},
                {"text": "imported two"},
            ]
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["context", "import", str(src)])
    assert result.exit_code == 0, result.output
    assert "✓ imported 2 entries" in result.stdout
    listing = runner.invoke(app, ["--json", "context", "list"])
    assert {e["text"] for e in _json(listing.stdout)} == {"imported one", "imported two"}


def test_import_markdown_adds_entries(runner: CliRunner, tmp_path: Path) -> None:
    src = tmp_path / "notes.md"
    src.write_text(
        "# Notes\n\n- first fact #ci\n- second fact\n\nnot a bullet\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--json", "context", "import", str(src)])
    assert result.exit_code == 0, result.output
    assert _json(result.stdout) == {"imported": 2}
    listing = runner.invoke(app, ["--json", "context", "list"])
    entries = {e["text"]: e["tags"] for e in _json(listing.stdout)}
    assert entries == {"first fact": ["ci"], "second fact": []}


def test_export_then_import_round_trips(runner: CliRunner, tmp_path: Path) -> None:
    _add(runner, "round trip", "--user", "--tag", "t")
    dump = tmp_path / "dump.json"
    runner.invoke(app, ["context", "export", str(dump), "--format", "json"])
    result = runner.invoke(app, ["context", "import", str(dump)])
    assert result.exit_code == 0, result.output
    listing = runner.invoke(app, ["--json", "context", "list"])
    texts = [e["text"] for e in _json(listing.stdout)]
    assert texts.count("round trip") == 2  # original + imported copy


def test_import_rescopes_project_entries(runner: CliRunner, tmp_path: Path) -> None:
    # A project entry carrying a foreign project_id must re-scope locally, not
    # fail the foreign key.
    src = tmp_path / "proj.json"
    src.write_text(
        json.dumps([{"text": "scoped here", "pool": "project", "project_id": "prj_bogus"}]),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["context", "import", str(src)])
    assert result.exit_code == 0, result.output
    listing = runner.invoke(app, ["--json", "context", "list"])
    entries = [e for e in _json(listing.stdout) if e["text"] == "scoped here"]
    assert len(entries) == 1
    assert entries[0]["pool"] == "project"
    assert entries[0]["project_id"] != "prj_bogus"


def test_import_missing_file_fails(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["context", "import", str(tmp_path / "nope.json")])
    assert result.exit_code == 1
    assert "no such file" in result.output


def test_import_invalid_json_fails(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    result = runner.invoke(app, ["context", "import", str(bad)])
    assert result.exit_code == 1
    assert "could not import" in result.output
