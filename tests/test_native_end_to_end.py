"""Deterministic process E2E: installed CLI + SQLite + real failing/passing pytest.

No agent mocks, runtime-service imports, paid model requests, or external servers.
Session hooks receive synthetic identities; this proves CLI wiring, not AI quality.
Set AISQUARE_E2E_CLI to a clean-installed asq executable to repeat against a wheel.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


@dataclass
class BlackBoxCLI:
    executable: str
    project: Path
    env: dict[str, str]

    def run(
        self,
        *args: str,
        expected: int = 0,
        json_output: bool = True,
        input_text: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.executable, *(["--json"] if json_output else []), *args]
        result = subprocess.run(
            command,
            cwd=self.project,
            env={**self.env, **(extra_env or {})},
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == expected, (
            f"{args!r}: expected {expected}, got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        if json_output:
            json.loads(result.stdout)  # one complete JSON value, no interspersed human text
        return result

    def data(self, *args: str, expected: int = 0) -> Any:
        return json.loads(self.run(*args, expected=expected).stdout)


@pytest.fixture
def native_cli(tmp_path: Path) -> BlackBoxCLI:
    project = tmp_path / "login-example"
    project.mkdir()
    home = tmp_path / "isolated-asq"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AISQUARE_", "CLAUDE_", "ANTHROPIC_", "OPENAI_"))
    }
    env.update(
        AISQUARE_HOME=str(home),
        CLAUDE_CONFIG_DIR=str(tmp_path / "isolated-claude"),
        TERM="dumb",
        NO_COLOR="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    subprocess.run(["git", "init", "-q", str(project)], check=True, env=env)
    (project / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (project / "login.py").write_text("def sign_in(password):\n    return 'dashboard'\n")
    (project / "test_login.py").write_text(
        "from login import sign_in\n\n"
        "def test_success():\n    assert sign_in('correct') == 'dashboard'\n\n"
        "def test_wrong_password():\n    assert sign_in('wrong') == 'error'\n"
    )
    executable = os.environ.get("AISQUARE_E2E_CLI", str(Path(sys.executable).with_name("asq")))
    assert Path(executable).is_file(), f"Install CLI first: missing {executable}"
    return BlackBoxCLI(executable=executable, project=project, env=env)


def test_login_failure_fix_recheck_personas_and_canonical_records(
    native_cli: BlackBoxCLI, tmp_path: Path
) -> None:
    cli = native_cli
    setup = cli.data("init", "--local", "--no-onboard", "--no-explainability")
    project_id = setup["project"]["id"]
    cli.data("team", "on")
    cli.data("brief", "mode", "native")
    coder = "e2e-coder-session"
    tester = "e2e-tester-session"
    for session, role in ((coder, "coder"), (tester, "tester")):
        response = cli.run(
            "hook",
            "session-start",
            json_output=False,
            input_text=json.dumps(
                {"cwd": str(cli.project), "session_id": session, "source": "startup"}
            ),
            extra_env={"AISQUARE_ROLE": role},
        )
        assert "aisquare: session-start failed" not in response.stderr
        assert session[:8] in response.stdout
        assert f"role: {role}" in response.stdout
    brief = cli.data(
        "brief",
        "create",
        "Login uses the existing sign-in flow",
        "-r",
        "Correct password opens the dashboard",
        "-r",
        "Wrong password shows an error",
        "--boundary",
        "Do not deploy this example",
    )
    brief_id = brief["id"]
    task = cli.data("task", "add", "Implement login outcomes", "--role", "coder", "--as", coder)
    task_id = task["id"]
    cli.data("brief", "link", brief_id, task_id, "-r", "R1", "-r", "R2")
    claimed = cli.data("task", "claim", task_id, "--as", coder)
    assert claimed["claimed_by"] == coder and claimed["status"] == "doing"
    initial = cli.data("brief", "check", brief_id, expected=1)
    assert initial["complete"] is False
    assert {row["status"] for row in initial["requirements"]} == {"missing-evidence"}

    # Every persona action and read is a new process: persistence/restart is real.
    before_task = cli.run("task", "show", task_id).stdout
    before_brief = cli.run("brief", "show", brief_id).stdout
    before_log = cli.run("team", "log").stdout
    studio = cli.data("persona", "use", "studio")
    assert studio["data"]["enabled"] is True
    cli.data("persona", "use", "mission-control", "--role", "coder")
    selected = cli.data("persona", "status")["data"]
    assert selected["effective"]["coder"].startswith("mission-control@")
    assert selected["effective"]["tester"].startswith("studio@")
    studio_preview = cli.data("persona", "preview", "studio", "--role", "coder")
    mission_preview = cli.data("persona", "preview", "mission-control", "--role", "coder")
    assert studio_preview["data"]["samples"] != mission_preview["data"]["samples"]
    assert studio_preview["data"]["original_failure"] == mission_preview["data"]["original_failure"]
    pack_path = tmp_path / "local-voice.json"
    cli.data("persona", "export", "studio", "--output", str(pack_path))
    pack = json.loads(pack_path.read_text())
    pack.update(id="local-e2e", name="Local E2E")
    pack["roles"]["coder"]["task_claimed"] = ["Local display voice: this task was claimed."]
    pack_path.write_text(json.dumps(pack))
    cli.data("persona", "add", str(pack_path))
    cli.data("persona", "use", "local-e2e", "--role", "coder")
    cli.data("persona", "off")
    assert cli.data("persona", "status")["data"]["enabled"] is False
    inactive = cli.data("persona", "use", "mission-control", "--role", "coder")
    assert "inactive" in inactive["message"]
    assert cli.data("persona", "status")["data"]["enabled"] is False
    cli.data("persona", "use", "studio")
    cli.data("persona", "use", "local-e2e", "--role", "coder")
    assert cli.data("persona", "status")["data"]["effective"]["coder"] == "local-e2e@1.0.0"
    assert cli.run("task", "show", task_id).stdout == before_task
    assert cli.run("brief", "show", brief_id).stdout == before_brief
    assert cli.run("team", "log").stdout == before_log

    cli.data("task", "review", task_id, "--as", coder)
    failed = cli.data(
        "exec",
        "--session",
        tester,
        "--task",
        task_id,
        "--project",
        project_id,
        "--",
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--color=no",
        expected=1,
    )
    assert failed["report"]["returncode"] == 1
    assert "1 failed, 1 passed" in failed["stdout"]
    assert "test_wrong_password" in failed["stdout"]
    fail_id = failed["report"]["id"]
    fail_artifact = Path(cli.env["AISQUARE_HOME"]) / "reports" / fail_id / "report.json"
    rejected = cli.data(
        "brief",
        "evidence",
        brief_id,
        "R2",
        "--task",
        task_id,
        "--verdict",
        "pass",
        "--summary",
        "Must reject a failed command as pass",
        "--report",
        fail_id,
        "--as",
        tester,
        expected=1,
    )
    assert rejected["error"] == "brief_error"
    assert cli.data("brief", "show", brief_id)["evidence"] == []
    failed_brief = cli.data(
        "brief",
        "evidence",
        brief_id,
        "R2",
        "--task",
        task_id,
        "--verdict",
        "fail",
        "--summary",
        "Wrong password opens dashboard",
        "--report",
        fail_id,
        "--as",
        tester,
    )
    assert len(failed_brief["evidence"]) == 1
    assert cli.data("task", "show", task_id)["status"] == "todo"
    assert cli.data("brief", "check", brief_id, expected=1)["requirements"][1]["status"] == "fail"
    blocked_done = cli.data("task", "done", task_id, "--as", coder, expected=1)
    assert blocked_done["error"] == "unverified_requirements"

    cli.data("task", "claim", task_id, "--as", coder)
    (cli.project / "login.py").write_text(
        "def sign_in(password):\n    return 'dashboard' if password == 'correct' else 'error'\n"
    )
    stale = cli.data("brief", "check", brief_id, expected=1)
    assert stale["requirements"][1]["status"] == "stale"
    cli.data("brief", "update", brief_id, "--source-revision", "login-v2")
    cli.data("task", "review", task_id, "--as", coder)
    passed = cli.data(
        "exec",
        "--session",
        tester,
        "--task",
        task_id,
        "--project",
        project_id,
        "--",
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--color=no",
    )
    assert passed["report"]["returncode"] == 0 and "2 passed" in passed["stdout"]
    pass_id = passed["report"]["id"]
    pass_artifact = Path(cli.env["AISQUARE_HOME"]) / "reports" / pass_id / "report.json"
    # A valid passing report becomes stale if code changes before its submission.
    source_path = cli.project / "login.py"
    checked_source = source_path.read_text()
    source_path.write_text(checked_source + "# changed after tests\n")
    rejected = cli.data(
        "brief",
        "evidence",
        brief_id,
        "R1",
        "--task",
        task_id,
        "--verdict",
        "pass",
        "--summary",
        "Must reject old source evidence",
        "--report",
        pass_id,
        "--as",
        tester,
        expected=1,
    )
    assert rejected["error"] == "brief_error"
    source_path.write_text(checked_source)
    for req in ("R1", "R2"):
        cli.data(
            "brief",
            "evidence",
            brief_id,
            req,
            "--task",
            task_id,
            "--verdict",
            "pass",
            "--summary",
            "Two real pytest checks passed on login-v2",
            "--report",
            pass_id,
            "--as",
            tester,
        )
    complete = cli.data("brief", "check", brief_id)
    assert complete["complete"] is True
    assert {row["status"] for row in complete["requirements"]} == {"pass"}
    assert cli.data("task", "done", task_id, "--as", tester)["status"] == "done"
    final_brief = cli.data("brief", "show", brief_id)
    assert [proof["verdict"] for proof in final_brief["evidence"]] == ["fail", "pass", "pass"]
    assert {proof["provenance"] for proof in final_brief["evidence"]} == {"command"}
    assert {proof["report_id"] for proof in final_brief["evidence"]} == {fail_id, pass_id}
    assert {proof["artifact"] for proof in final_brief["evidence"]} == {
        str(fail_artifact),
        str(pass_artifact),
    }
    raw = cli.data("reports", "show", fail_id, "--raw")
    assert raw["report"]["returncode"] == 1
    assert cli.data("brief", "check", brief_id)["complete"] is True
    saved = cli.data("reports", "list")
    assert {report["id"] for report in saved} == {fail_id, pass_id}  # retrieval did not rerun
    metrics = cli.data("reports", "stats")
    assert metrics["retained_report_count"] == 2
    assert "not provider usage" in metrics["token_estimate_method"]

    # Requirements are not made true by old evidence after another code change.
    (cli.project / "login.py").write_text("def sign_in(password):\n    return 'regression'\n")
    stale = cli.data("brief", "check", brief_id, expected=1)
    assert stale["complete"] is False
    assert {row["status"] for row in stale["requirements"]} == {"stale"}
