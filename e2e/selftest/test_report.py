"""Self-tests of the report and the publisher: build from a fake run, read what a person reads.

    .venv/bin/python -m pytest e2e/selftest/test_report.py -q -p no:cacheprovider \\
        --noconftest --rootdir e2e -c /dev/null

Standard library plus pytest, no browser, no network: the publisher's git half
runs against a bare repository in tmp_path, and its gh half against a stand-in
``gh`` on PATH that records its arguments.

Each claim is asserted on the artefact it is about: the bytes inlined in the
gallery against the PNG on disk, the summary's length as GitHub counts it, the
tree of the pushed branch as the remote holds it, and the run directory's files
before and after a dry run.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import struct
import subprocess
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from e2e.contracts import Check, load, save
from e2e.report import build, markdown, publish, secret_scan
from e2e.report.facts import Links, of
from e2e.selftest import fake_run

IMAGE_BASE = "https://raw.githubusercontent.com/me/e2e-sandbox/e2e-artifacts/runs/fake"
LINKS = Links(artifact_name="e2e-linux-main")
VERDICT_KEYS = {
    "verdict",
    "stage",
    "env",
    "ref",
    "counts",
    "failures",
    "preflight_failed",
    "notes",
}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "run"
    fake_run.write(path)
    return path


@pytest.fixture
def built(run_dir: Path) -> build.Built:
    return build.build(run_dir, image_base=IMAGE_BASE, links=LINKS)


def _steps_with_shots(run_dir: Path) -> list[tuple[str, str]]:
    run = load(run_dir / "run.json")
    return [(s.id, s.shot) for j in run.journeys for s in j.steps if s.shot]


def _read(built: build.Built, name: str) -> str:
    return built.path(name).read_text(encoding="utf-8")


# -- build: the four files -------------------------------------------------------------


def test_build_writes_four_files_and_exits_0_for_a_failed_run(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("E2E_ARTIFACT_NAME", "e2e-linux-main")
    assert load(run_dir / "run.json").verdict == "RC_FAILED"  # the premise: a red run
    assert build.main([str(run_dir)]) == 0  # the verdict is data, not the exit status
    for name in build.OUTPUTS:
        assert (run_dir / "report" / name).stat().st_size > 0, name
    assert "RC_FAILED" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("damage", "why"),
    [
        (lambda p: p.unlink(), "missing"),
        (lambda p: p.write_text("{not json", encoding="utf-8"), "JSONDecodeError"),
        (lambda p: p.write_text('{"schema": "other/v9"}', encoding="utf-8"), "not an"),
        (lambda p: p.write_text("[]", encoding="utf-8"), "AttributeError"),
        (
            lambda p: p.write_text(
                p.read_text(encoding="utf-8").replace('"status": "passed"', '"status": "flaky"', 1),
                encoding="utf-8",
            ),
            "'flaky' is unknown",
        ),
    ],
)
def test_build_exits_2_on_a_run_json_it_cannot_trust_and_leaves_no_stale_verdict(
    run_dir: Path,
    damage: Callable[[Path], object],
    why: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert build.main([str(run_dir)]) == 0  # an earlier, good build
    assert (run_dir / "report" / "verdict.json").is_file()
    damage(run_dir / "run.json")
    assert build.main([str(run_dir)]) == 2
    assert why in capsys.readouterr().err
    for name in build.OUTPUTS:
        assert not (run_dir / "report" / name).exists(), f"stale {name} survived"


# -- gallery.html ----------------------------------------------------------------------


def test_gallery_inlines_every_screenshot_byte_for_byte(run_dir: Path, built: build.Built) -> None:
    html = _read(built, "gallery.html")
    inlined = dict(
        re.findall(r'data-step="([^"]+)"[^>]*><img src="data:image/png;base64,([^"]+)"', html)
    )
    expected = _steps_with_shots(run_dir)
    assert len(expected) == 12  # 10 journey steps + 2 watch items: the premise of the count
    assert set(inlined) == {step_id for step_id, _ in expected}
    for step_id, shot in expected:
        assert base64.b64decode(inlined[step_id]) == (run_dir / shot).read_bytes(), step_id
    assert built.page.shots_inlined == len(expected)


def test_gallery_has_a_lightbox_every_step_and_no_network(
    run_dir: Path, built: build.Built
) -> None:
    html = _read(built, "gallery.html")
    assert '<dialog id="lightbox"' in html
    for needle in ("showModal", "ArrowRight", "ArrowLeft", "Escape"):
        assert needle in html, needle
    run = load(run_dir / "run.json")
    for journey in run.journeys:
        assert f'id="{journey.id}"' in html
        for step in journey.steps:
            assert f'id="{step.id}"' in html, step.id
    # Self-contained: nothing fetched from anywhere, scripts and styles inline.
    assert not re.search(r'(?:src|href)="(?:https?:)?//', html)
    assert "<script src" not in html
    assert '<link rel="stylesheet"' not in html
    # Links to the files beside report/ in the artifact, relative.
    assert 'href="../video/J02.webm"' in html
    assert 'href="../text/J04/J04.S03.f12-scrollback.txt"' in html
    assert 'class="banner"' in html
    assert "RC_FAILED" in html


def test_gallery_reports_watch_items_apart_and_in_their_own_colour(built: build.Built) -> None:
    html = _read(built, "gallery.html")
    journeys_part, _, watch_part = html.partition('id="watchlist"')
    assert watch_part, "no watchlist section"
    assert "QA-01" in watch_part and "still open" in watch_part
    assert "QA-13" in watch_part and "FIXED · promote me" in watch_part
    assert "QA-01" not in journeys_part.split('class="panel index"')[1].split("</table>")[0]
    assert 'class="step w-open"' in watch_part  # amber, not the red of a failure
    assert 'class="step s-failed"' not in watch_part


def test_gallery_escapes_what_the_product_printed(run_dir: Path) -> None:
    run = load(run_dir / "run.json")
    step = run.journeys[0].steps[0]
    step.title = "<script>alert(1)</script>"
    step.checks.append(Check('"><img src=x onerror=alert(2)>', False, "</pre><b>x</b>"))
    save(run, run_dir / "run.json")
    html = _read(build.build(run_dir, links=LINKS), "gallery.html")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html
    assert "</pre><b>x</b>" not in html


def test_gallery_reads_no_file_outside_the_run(run_dir: Path, tmp_path: Path) -> None:
    secret = tmp_path / "outside.png"
    shutil.copyfile(run_dir / _steps_with_shots(run_dir)[0][1], secret)
    run = load(run_dir / "run.json")
    run.journeys[0].steps[0].shot = "../outside.png"
    run.journeys[0].steps[1].shot = str(secret)  # absolute
    save(run, run_dir / "run.json")
    built = build.build(run_dir, links=LINKS)
    assert built.page.shots_inlined == 10
    assert {"../outside.png", str(secret)} <= set(built.page.missing)
    assert "screenshot missing" in _read(built, "gallery.html")


# -- summary.md and step-summary.md ----------------------------------------------------


def test_summary_has_the_tables_details_and_pictures_under_the_image_base(
    run_dir: Path, built: build.Built
) -> None:
    text = _read(built, "summary.md")
    assert markdown.size(text) < markdown.GITHUB_LIMIT
    assert text.startswith("<!-- aisquare-e2e run=")
    assert "\n## ❌ E2E RC_FAILED: 1 of 3 journeys failed\n" in text
    assert "| Journey | Result | Steps | Time |" in text
    assert "| Finding | Check | State |" in text
    assert "<details open>\n<summary><b>J04</b>" in text
    for journey in ("J01", "J02"):
        assert f"<details>\n<summary><b>{journey}</b>" in text
    for finding in ("QA-01", "QA-13"):  # evidence for known bugs: collapsed
        assert f"<details>\n<summary><code>{finding}</code>" in text
    assert text.count("<details") == text.count("</details>") == 5
    for step_id, shot in _steps_with_shots(run_dir):
        assert f'<img src="{IMAGE_BASE}/{shot}" width="720" alt="{step_id}">' in text
    # The failing check is there with its detail, as a reader needs it.
    assert "✗ <b>pane rows match tmux capture-pane (cropped)</b>: rows 3-5 differ" in text


def test_summary_without_an_image_base_points_at_the_files_from_report(run_dir: Path) -> None:
    built = build.build(run_dir, links=LINKS)
    text = _read(built, "summary.md")
    assert '<img src="../shots/J01/J01.S01.one-liner.png" width="720"' in text
    assert "https://" not in text.split("<b>Notes</b>")[0]


def test_step_summary_has_no_pictures_and_says_where_they_are(built: build.Built) -> None:
    text = _read(built, "step-summary.md")
    assert "<img" not in text
    assert "| Journey | Result | Steps | Time |" in text
    assert text.count("<details") == 5  # the same sections, pictures aside
    assert text.rstrip().splitlines()[-1] == (
        "Screenshots: download the `e2e-linux-main` artifact and open report/gallery.html"
    )


def test_markdown_escapes_cells_backticks_mentions_and_references(run_dir: Path) -> None:
    run = load(run_dir / "run.json")
    run.journeys[0].title = "`project forget` | @octocat fixed #248 *bold*"
    save(run, run_dir / "run.json")
    text = _read(build.build(run_dir, image_base=IMAGE_BASE, links=LINKS), "summary.md")
    row = next(line for line in text.splitlines() if line.startswith("| <b>J01</b>"))
    assert row.count(" | ") == 3, row  # the title's pipe did not split the row
    assert "<code>project forget</code>" in row and "`" not in row
    assert "@⁠octocat" in row and "#⁠248" in row  # no ping, no cross-reference
    assert "\\*bold\\*" in row
    summary_line = next(line for line in text.splitlines() if line.startswith("<summary><b>J01"))
    assert "<code>project forget</code>" in summary_line and "\\" not in summary_line


def test_summary_sheds_detail_to_fit_githubs_limit_failures_last(run_dir: Path) -> None:
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    template = next(j for j in data["journeys"] if j["id"] == "J02")  # passed, four pictures
    for number in range(60):
        clone = json.loads(json.dumps(template))
        clone["id"] = jid = f"J{number + 10}"
        for step in clone["steps"]:
            step["id"] = step["id"].replace("J02", jid)
            step["checks"] = [{"name": "a check " * 20, "ok": True, "detail": "x" * 300}]
        data["journeys"].append(clone)
    (run_dir / "run.json").write_text(json.dumps(data), encoding="utf-8")
    facts = of(load(run_dir / "run.json"))
    unbounded = markdown.summary(facts, image_base=IMAGE_BASE, links=LINKS, budget=10**9)
    assert markdown.size(unbounded) > markdown.GITHUB_LIMIT  # the premise: it must shed
    text = markdown.summary(facts, image_base=IMAGE_BASE, links=LINKS)
    assert markdown.size(text) < markdown.GITHUB_LIMIT
    assert "Shortened to fit GitHub's 65,536-character limit" in text
    failing = f'<img src="{IMAGE_BASE}/shots/J04/{fake_run.FAILING_STEP}.png"'
    assert failing in text  # the failed journey keeps its picture longest
    assert text.count("<img") < unbounded.count("<img")
    assert text.count("<details") == text.count("</details>")
    steps_only = markdown.step_summary(facts, links=LINKS)
    assert markdown.size(steps_only) < markdown.GITHUB_LIMIT


# -- verdict.json ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "verdict", "failures", "preflight"),
    [
        ("rc-failed", "RC_FAILED", [(fake_run.FAILING_STEP, "RC_FAILED", "call")], False),
        ("passed", "PASSED", [], False),
        ("gate-error", "GATE_ERROR", [(fake_run.FAILING_STEP, "GATE_ERROR", "call")], False),
        ("env-failed", "ENV_FAILED", [("preflight", "ENV_FAILED", "setup")], True),
    ],
)
def test_verdict_json_has_the_gates_shape_and_classes(
    tmp_path: Path,
    scenario: fake_run.Scenario,
    verdict: str,
    failures: list[tuple[str, str, str]],
    preflight: bool,
) -> None:
    run_dir = tmp_path / scenario
    fake_run.write(run_dir, scenario)
    payload = json.loads(_read(build.build(run_dir, links=LINKS), "verdict.json"))
    assert set(payload) == VERDICT_KEYS
    assert payload["verdict"] == verdict
    assert payload["stage"] == "e2e"
    assert payload["env"] == "Linux 6.8.0-1015-azure x86_64 (fake)/fake/main"
    assert payload["ref"] == "0123456789ab"
    assert payload["preflight_failed"] is preflight
    got = [(f["test"], f["class"], f["when"]) for f in payload["failures"]]
    assert got == failures
    assert all(
        set(f) == {"test", "class", "when", "message"} and f["message"] for f in payload["failures"]
    )
    counts = payload["counts"]
    assert set(counts) == {"passed", "failed", "error", "skipped"}
    if scenario == "rc-failed":
        assert counts == {"passed": 9, "failed": 1, "error": 0, "skipped": 0}
        assert "pane rows match tmux capture-pane" in payload["failures"][0]["message"]
    if scenario != "env-failed":
        # Watch items: never failures, never counted, always in the notes.
        assert any(n.startswith("QA-01 still open: ") for n in payload["notes"])
        assert any(n.startswith("QA-13 FIXED · promote me: ") for n in payload["notes"])
        assert counts["passed"] + counts["failed"] + counts["error"] == 10


def test_a_verdict_milder_than_the_journeys_is_raised_and_noted(run_dir: Path) -> None:
    run = load(run_dir / "run.json")
    run.verdict = "PASSED"  # a harness that under-reports
    save(run, run_dir / "run.json")
    payload = json.loads(_read(build.build(run_dir, links=LINKS), "verdict.json"))
    assert payload["verdict"] == "RC_FAILED"
    assert any("run.json says PASSED" in note for note in payload["notes"])


# -- the fake run itself ---------------------------------------------------------------


def _png_rows(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and inflated scanlines, checking every chunk's CRC on the way."""
    assert data[:8] == fake_run.PNG_SIGNATURE
    position = 8
    chunks: dict[bytes, bytes] = {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        tag = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length : position + 12 + length])
        assert zlib.crc32(tag + body) == crc, tag
        chunks[tag] = chunks.get(tag, b"") + body
        position += 12 + length
    width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, colour) == (8, 2)
    assert b"IEND" in chunks
    return width, height, zlib.decompress(chunks[b"IDAT"])


def test_fake_run_pictures_are_valid_pngs_and_all_different(run_dir: Path) -> None:
    digests = set()
    for _, shot in _steps_with_shots(run_dir):
        data = (run_dir / shot).read_bytes()
        width, height, rows = _png_rows(data)
        assert (width, height) == (fake_run.WIDTH, fake_run.HEIGHT)
        assert len(rows) == height * (1 + 3 * width)
        digests.add(rows)
    assert len(digests) == 12


def test_fake_run_refuses_a_directory_that_is_not_a_fake(tmp_path: Path, run_dir: Path) -> None:
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(fake_run.FakeRunError, match=r"no run\.json"):
        fake_run.write(stranger)
    run = load(run_dir / "run.json")
    run.env.runner = "github-hosted"  # a real run's
    save(run, run_dir / "run.json")
    with pytest.raises(fake_run.FakeRunError, match="real run"):
        fake_run.write(run_dir)
    assert (stranger / "notes.txt").read_text(encoding="utf-8") == "mine"


# -- secret scan -----------------------------------------------------------------------

_SAMPLES = {
    "anthropic-key": "sk-ant-api03-" + "Ab1_" * 10,
    "github-fine-grained-token": "github_pat_" + "A1b2" * 8,
    "github-token": "ghp_" + "a1B2" * 9,
    "slack-webhook": "hooks.slack.com/services/T000/B000/XXXXXXXX",
    "slack-token": "xoxb-1234567890-abcdefghij",
    "aws-access-key": "AKIA" + "ABCDEFGHIJ234567",
    "private-key": "-----BEGIN OPENSSH PRIVATE KEY-----",
}


@pytest.mark.parametrize("rule", sorted(_SAMPLES))
def test_secret_scan_sees_each_shape_it_claims(run_dir: Path, rule: str) -> None:
    assert {name for name, _ in secret_scan.RULES} == set(_SAMPLES)  # a sample per rule
    screen = run_dir / "text" / "J01" / "J01.S01.one-liner.txt"
    screen.write_text(f"prompt> {_SAMPLES[rule]}\n", encoding="utf-8")
    hits = secret_scan.scan(run_dir)
    assert [(h.path, h.offset, h.rule) for h in hits] == [
        ("text/J01/J01.S01.one-liner.txt", 8, rule)
    ]


def test_secret_scan_passes_a_clean_run_that_it_did_read(run_dir: Path) -> None:
    files = secret_scan.scanned_files(run_dir)
    names = {p.relative_to(run_dir).as_posix() for p in files}
    assert "run.json" in names and "text/J04/J04.S03.f12-scrollback.txt" in names
    assert not any(n.endswith(".png") or n.startswith("report/") for n in names)
    assert secret_scan.scan(run_dir) == []
    assert secret_scan.main([str(run_dir)]) == 0


# -- publish ---------------------------------------------------------------------------


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in root.rglob("*")
        if p.is_file()
    }


def _publish(*args: str, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = publish.main(list(args), environ=environ or dict(os.environ), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_publish_dry_run_prints_the_plan_and_touches_nothing(
    run_dir: Path, built: build.Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"a dry run ran {args!r}")

    monkeypatch.setattr(subprocess, "run", no_subprocess)  # the module publish calls through
    before = _snapshot(run_dir)
    code, out, err = _publish(str(run_dir), "--repo", "me/e2e-sandbox")
    assert (code, err) == (0, "")
    assert _snapshot(run_dir) == before
    run_id = load(run_dir / "run.json").run_id
    base = f"https://raw.githubusercontent.com/me/e2e-sandbox/e2e-artifacts/runs/{run_id}"
    for needle in (
        "DRY RUN: nothing was cloned, committed, pushed, written or posted",
        f"image base   {base}",
        "git ls-remote --exit-code --heads https://github.com/me/e2e-sandbox.git",
        f"runs/{run_id}/shots/J01/J01.S01.one-liner.png",
        f"runs/{run_id}/report/gallery.html",
        f"runs/{run_id}/run.json",
        f"runs/{run_id}/video/J02.webm",
        "[skipped: no --push]",
        "gh issue create --repo me/e2e-sandbox --title",
        "as QA Bot <e2e@example.invalid>",
    ):
        assert needle in out, needle


@pytest.mark.parametrize(
    "where",
    [
        ["--repo", "AISquare-Studio/aisquare-cli"],
        ["--repo", "aisquare-studio/anything"],
        ["--repo", "me/x", "--remote-url", "git@github.com:AISquare-Studio/aisquare-cli.git"],
    ],
)
def test_publish_refuses_the_org_unless_allowed(
    run_dir: Path, built: build.Built, where: list[str]
) -> None:
    code, out, err = _publish(str(run_dir), *where, "--push", "--create-issue")
    assert code == 2
    assert "belongs to AISquare-Studio" in err and "--allow-org" in err
    assert out == ""  # refused before any plan, let alone any action
    code, out, _ = _publish(str(run_dir), *where, "--allow-org")  # a dry run, once allowed
    assert code == 0 and "DRY RUN" in out


def test_publish_refuses_a_run_holding_a_credential_without_printing_it(
    run_dir: Path, built: build.Built
) -> None:
    token = _SAMPLES["anthropic-key"]
    (run_dir / "text" / "J02" / "J02.S01.use-this-folder.txt").write_text(
        f"export KEY={token}\n", encoding="utf-8"
    )
    code, out, err = _publish(str(run_dir), "--repo", "me/e2e-sandbox")
    assert code == 3
    assert "text/J02/J02.S01.use-this-folder.txt:11: anthropic-key" in err
    assert token not in err and token not in out


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    command = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args]
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def hermetic_env() -> dict[str, str]:
    """The publisher's environment, without this machine's git configuration."""
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture
def user_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A checkout the publisher runs from, which must come out exactly as it went in."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git("init", "--quiet", "--initial-branch", "main", cwd=repo)
    (repo / "file.txt").write_text("work in progress\n", encoding="utf-8")
    _git("add", "file.txt", cwd=repo)
    _git("commit", "--quiet", "-m", "start", cwd=repo)
    (repo / "file.txt").write_text("uncommitted edit\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    state = (_git("rev-parse", "HEAD", cwd=repo), _git("branch", "--list", cwd=repo))
    status = _git("status", "--porcelain", cwd=repo)
    yield repo
    assert (_git("rev-parse", "HEAD", cwd=repo), _git("branch", "--list", cwd=repo)) == state
    assert _git("status", "--porcelain", cwd=repo) == status


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_publish_pushes_runs_to_an_orphan_branch_and_never_clobbers(
    tmp_path: Path, hermetic_env: dict[str, str], user_checkout: Path
) -> None:
    remote = tmp_path / "remote.git"
    _git("init", "--quiet", "--bare", str(remote))
    url = remote.as_uri()
    runs = []
    for scenario in ("rc-failed", "passed"):
        run_dir = tmp_path / scenario
        run = fake_run.write(run_dir, scenario)
        build.build(run_dir, links=LINKS)
        code, out, err = _publish(
            str(run_dir),
            "--repo",
            "me/e2e-sandbox",
            "--remote-url",
            url,
            "--push",
            environ=hermetic_env,
        )
        assert (code, err) == (0, ""), err
        assert "pushed " in out
        runs.append(run.run_id)
    tree = set(
        _git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "e2e-artifacts").split()
    )
    for run_id in runs:
        for name in (
            "run.json",
            "report/gallery.html",
            "shots/J01/J01.S01.one-liner.png",
            "shots/W01/W01.S01.forget-pin.png",
            "video/J02.webm",
        ):
            assert f"runs/{run_id}/{name}" in tree, name
    assert {"README.md", publish.MARKER} <= tree
    assert not any(name.startswith(("text/", "logs/")) or "/text/" in name for name in tree)
    history = _git("--git-dir", str(remote), "log", "--format=%an <%ae>|%P", "e2e-artifacts")
    lines = history.splitlines()
    assert len(lines) == 2  # one commit per run, the second on top of the first
    assert all(line.startswith("QA Bot <e2e@example.invalid>|") for line in lines)
    assert lines[-1].endswith("|")  # the first commit has no parent: an orphan branch

    # Publishing the same run again changes nothing; the same id with other pixels is refused.
    rc_failed = tmp_path / "rc-failed"
    code, out, _ = _publish(
        str(rc_failed),
        "--repo",
        "me/e2e-sandbox",
        "--remote-url",
        url,
        "--push",
        environ=hermetic_env,
    )
    assert code == 0 and "already on e2e-artifacts, unchanged" in out
    shot = rc_failed / "shots" / "J01" / "J01.S01.one-liner.png"
    shot.write_bytes(shot.read_bytes() + b"\0")
    code, _, err = _publish(
        str(rc_failed),
        "--repo",
        "me/e2e-sandbox",
        "--remote-url",
        url,
        "--push",
        environ=hermetic_env,
    )
    assert code == 2 and "different content" in err
    assert len(_git("--git-dir", str(remote), "log", "--format=%H", "e2e-artifacts").split()) == 2


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_publish_adds_nothing_to_a_branch_without_the_marker(
    tmp_path: Path, run_dir: Path, built: build.Built, hermetic_env: dict[str, str]
) -> None:
    remote = tmp_path / "remote.git"
    _git("init", "--quiet", "--bare", "--initial-branch", "main", str(remote))
    seed = tmp_path / "seed"
    _git("clone", "--quiet", remote.as_uri(), str(seed))
    (seed / "code.py").write_text("print('the product')\n", encoding="utf-8")
    _git("add", "code.py", cwd=seed)
    _git("commit", "--quiet", "-m", "code", cwd=seed)
    _git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=seed)
    head = _git("--git-dir", str(remote), "rev-parse", "main")
    code, _, err = _publish(
        str(run_dir),
        "--repo",
        "me/e2e-sandbox",
        "--remote-url",
        remote.as_uri(),
        "--branch",
        "main",
        "--push",
        environ=hermetic_env,
    )
    assert code == 2 and f"has no {publish.MARKER}" in err
    assert _git("--git-dir", str(remote), "rev-parse", "main") == head


def test_publish_posts_the_preamble_and_the_summary_through_gh(
    tmp_path: Path, run_dir: Path, built: build.Built
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    recorded = tmp_path / "gh-args.txt"
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_ARGS_FILE"\n'
        'echo "https://github.com/me/e2e-sandbox/issues/7"\n',
        encoding="utf-8",
    )
    gh.chmod(0o755)
    preamble = tmp_path / "preamble.md"
    preamble.write_text("### How this run was made\n\n`act -j e2e`\n", encoding="utf-8")
    environ = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_ARGS_FILE": str(recorded),
    }
    code, out, err = _publish(
        str(run_dir),
        "--repo",
        "me/e2e-sandbox",
        "--create-issue",
        "--preamble",
        str(preamble),
        environ=environ,
    )
    assert (code, err) == (0, "")
    assert out.rstrip().splitlines()[-1] == "issue: https://github.com/me/e2e-sandbox/issues/7"
    body_file = run_dir / "report" / "issue-body.md"
    argv = recorded.read_text(encoding="utf-8").splitlines()
    assert argv[:4] == ["issue", "create", "--repo", "me/e2e-sandbox"]
    assert argv[argv.index("--body-file") + 1] == str(body_file)
    assert argv[argv.index("--title") + 1].startswith("E2E RC_FAILED: 1 of 3 journeys failed")
    body = body_file.read_text(encoding="utf-8")
    run_id = load(run_dir / "run.json").run_id
    base = f"https://raw.githubusercontent.com/me/e2e-sandbox/e2e-artifacts/runs/{run_id}"
    assert body.startswith("### How this run was made")
    assert f'<img src="{base}/shots/J01/J01.S01.one-liner.png"' in body
    assert markdown.size(body) < markdown.GITHUB_LIMIT
    summary = (run_dir / "report" / "summary.md").read_text(encoding="utf-8")
    assert f'<img src="{base}/shots/' in summary  # rewritten with the image base
