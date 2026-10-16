"""Self-tests for e2e/report/ci_publish.py: no network — a local bare repo and a fake GitHub API."""

from __future__ import annotations

import io
import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from e2e.report import ci_publish
from e2e.report.ci_publish import GitHub, PublishError, find_sticky, ref_for, sticky_marker
from e2e.selftest import fake_run


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    target = tmp_path / "run"
    fake_run.main([str(target), "--scenario", "passed"])
    return target


# --- finding the comment to edit ------------------------------------------------------------


def _comment(cid: int, body: str, kind: str = "Bot") -> dict[str, Any]:
    return {"id": cid, "body": body, "user": {"login": "x", "type": kind}}


def test_the_newest_bot_comment_with_this_tracks_marker_is_the_one_edited() -> None:
    marker = sticky_marker("main")
    comments = [
        _comment(1, marker + "\nold run"),
        _comment(2, sticky_marker("release") + "\nother track"),
        _comment(3, marker + "\nnewer run"),
    ]
    assert find_sticky(comments, "main") == 3
    assert find_sticky(comments, "release") == 2


def test_a_humans_comment_quoting_the_marker_is_never_edited() -> None:
    comments = [_comment(7, sticky_marker("main") + "\nlook at this", kind="User")]
    assert find_sticky(comments, "main") is None


def test_no_marker_means_a_new_comment() -> None:
    assert find_sticky([_comment(1, "LGTM")], "main") is None


@pytest.mark.parametrize("bad", ["", "../x", "a b", "x;rm -rf", "-flag", "a" * 200])
def test_unsafe_run_ids_never_become_refs(bad: str) -> None:
    with pytest.raises(PublishError):
        ref_for(bad)


def test_a_safe_run_id_becomes_a_ref_outside_branches_and_tags() -> None:
    ref = ref_for("12345-1-main")
    assert ref == "refs/e2e/runs/12345-1-main"
    assert not ref.startswith(("refs/heads/", "refs/tags/"))


# --- the command ------------------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_touches_nothing(run_dir: Path) -> None:
    out = io.StringIO()
    before = sorted(p.name for p in run_dir.rglob("*"))
    code = ci_publish.run([str(run_dir), "--repo", "o/r", "--pr", "5", "--dry-run"], {}, out)
    assert code == 0
    assert "refs/e2e/runs/" in out.getvalue() and "pr #5" in out.getvalue()
    assert sorted(p.name for p in run_dir.rglob("*")) == before


def test_a_credential_in_the_run_refuses_to_publish(run_dir: Path) -> None:
    leak = "sk-ant-" + "api03-" + "x" * 30  # assembled, so the scan never trips on this file
    (run_dir / "text" / "leak.txt").parent.mkdir(parents=True, exist_ok=True)
    (run_dir / "text" / "leak.txt").write_text(f"token={leak}\n", encoding="utf-8")
    with pytest.raises(PublishError) as caught:
        ci_publish.run([str(run_dir), "--repo", "o/r", "--dry-run"], {}, io.StringIO())
    assert caught.value.code == 3


def test_evidence_is_pushed_to_its_own_ref_as_one_orphan_commit(
    run_dir: Path, tmp_path: Path
) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "E2E_PUBLISH_REMOTE": str(bare),
    }
    sha = ci_publish.push_evidence(run_dir, "o/r", "refs/e2e/runs/1-1-main", env)
    show = subprocess.run(
        ["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "refs/e2e/runs/1-1-main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "run.json" in show and any(name.startswith("shots/") for name in show)
    parents = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-list", "--parents", "-n", "1", sha],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert parents == [sha]  # an orphan: no history to drag along
    branches = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref", "refs/heads", "refs/tags"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert branches == ""  # nothing a default clone would fetch


# --- the comments API, against a fake GitHub ------------------------------------------------


class _FakeGitHub(BaseHTTPRequestHandler):
    comments: ClassVar[list[dict[str, Any]]] = []
    calls: ClassVar[list[tuple[str, str]]] = []

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self.calls.append(("GET", self.path))
        query = parse_qs(urlsplit(self.path).query)
        page = int(query.get("page", ["1"])[0])
        self._send(200, self.comments if page == 1 else [])

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(("POST", self.path))
        made = {
            "id": 99,
            "body": body["body"],
            "user": {"type": "Bot"},
            "html_url": "https://x/c/99",
        }
        self.comments.append(made)
        self._send(201, made)

    def do_PATCH(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append(("PATCH", self.path))
        cid = int(self.path.rsplit("/", 1)[-1])
        for comment in self.comments:
            if comment["id"] == cid:
                comment["body"] = body["body"]
        self._send(200, {"id": cid, "html_url": f"https://x/c/{cid}"})

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def fake_api() -> Iterator[str]:
    _FakeGitHub.comments = []
    _FakeGitHub.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitHub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_first_run_creates_the_comment_and_the_next_one_edits_it(fake_api: str) -> None:
    gh = GitHub("o/r", "t0ken", fake_api)
    first = gh.upsert(5, sticky_marker("main") + "\nrun 1", "main")
    second = gh.upsert(5, sticky_marker("main") + "\nrun 2", "main")
    assert first.endswith("/99") and second.endswith("/99")
    methods = [m for m, _ in _FakeGitHub.calls if m != "GET"]
    assert methods == ["POST", "PATCH"]
    assert len(_FakeGitHub.comments) == 1
    assert _FakeGitHub.comments[0]["body"].endswith("run 2")
