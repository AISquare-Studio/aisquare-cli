"""Hand specific Claude Code sessions to another agent, per task (Tier 2b).

A spawned teammate is briefed about the *project* — snapshot pointers, context
pools, the team board — never about the *conversations* that led to its task.
``claude --resume`` reopens exactly one session and cannot merge several, and
becoming a session is not receiving it. This module closes that gap: given
session ids, it finds each transcript, redacts it, distills it into a
**state-of-play brief** (goal, current state, decisions and why, open threads,
file paths — *especially* the task chatter that long-term memory deliberately
drops), writes the briefs under ``~/.aisquare/handoffs/``, and optionally puts
a note on the team pipe pointing the target role at them.

Route, don't dump: the pipe gets a pointer plus the summary line; the brief
itself lives on disk where the receiving agent reads it whole.

The distillation model call is a seam (``_distill_llm``): the default shells
out to the local ``claude`` binary in print mode on the caller's own account,
``--no-llm`` (or an unavailable binary) degrades to a structural brief that
still carries the digest, and tests replace the seam entirely.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from aisquare.core import paths
from aisquare.core.config import load_config
from aisquare.core.redaction import redact
from aisquare.core.spawn import untraced_env
from aisquare.services import team as team_service
from aisquare.services.claude_import import default_claude_projects

_DIGEST_CAP = 80_000
"""Character budget per session digest — head and tail survive, middle folds."""
_BLOCK_CAP = 2_000
"""Character budget per digest line (one message, tool call or result)."""
_LLM_TIMEOUT_SECONDS = 300

_BRIEF_PROMPT = (
    "You are preparing a handoff brief so another agent can pick up this work "
    "without having lived the conversation. From the transcript digest below, "
    "write a state-of-play brief in Markdown with exactly these sections: "
    "## Goal, ## Current state, ## Decisions and why, ## Open threads, "
    "## Key files and paths. Be specific; keep exact file paths, branch names, "
    "ids and commands verbatim; do not invent anything that is not in the "
    "digest.\n\nTranscript digest:\n"
)


class SessionNotFoundError(LookupError):
    """No transcript matches the given session id (or prefix)."""

    def __init__(self, ref: str, projects_dir: Path) -> None:
        super().__init__(f"no session transcript matches {ref!r} under {projects_dir}")
        self.ref = ref


class AmbiguousSessionError(LookupError):
    """A session id prefix matches more than one transcript."""

    def __init__(self, ref: str, matches: list[Path]) -> None:
        names = ", ".join(match.stem for match in matches[:5])
        super().__init__(f"session id {ref!r} is ambiguous: {names}")
        self.ref = ref


class SessionBrief(BaseModel):
    """One distilled session."""

    session_id: str
    transcript: Path
    source_dir: str
    """The Claude Code project slug the transcript lives under (its cwd)."""
    turns: int
    brief_path: Path
    raw_path: Path | None = None
    distilled: bool
    """True when a model produced the brief; False for the structural fallback."""


class HandoffReport(BaseModel):
    """What ``handoff`` produced."""

    briefs: list[SessionBrief] = Field(default_factory=list)
    bundle_path: Path
    note_posted: bool = False
    note_skipped_reason: str | None = None


def handoff(
    session_ids: list[str],
    *,
    claude_projects: Path | None = None,
    to_role: str | None = None,
    task_ref: str | None = None,
    raw: bool = False,
    use_llm: bool = True,
    out: Path | None = None,
) -> HandoffReport:
    """Distill each session into a brief and bundle them for the receiving agent."""
    projects_dir = claude_projects or default_claude_projects()
    level = load_config().redaction.level
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = paths.ensure_home() / "handoffs" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    briefs: list[SessionBrief] = []
    for ref in session_ids:
        transcript = _locate(projects_dir, ref)
        digest, turns = _digest(transcript)
        digest = redact(digest, level)
        brief_text, distilled = _brief(digest, use_llm=use_llm)
        brief_path = out_dir / f"brief-{transcript.stem}.md"
        header = (
            f"# Handoff brief — session {transcript.stem}\n\n"
            f"- source: `{transcript.parent.name}` ({turns} turns)\n"
            f"- distilled: {'model' if distilled else 'structural fallback (no model call)'}\n\n"
        )
        brief_path.write_text(header + brief_text.rstrip() + "\n", encoding="utf-8")
        raw_path: Path | None = None
        if raw:
            raw_path = out_dir / f"transcript-{transcript.stem}.md"
            raw_path.write_text(digest + "\n", encoding="utf-8")
        briefs.append(
            SessionBrief(
                session_id=transcript.stem,
                transcript=transcript,
                source_dir=transcript.parent.name,
                turns=turns,
                brief_path=brief_path,
                raw_path=raw_path,
                distilled=distilled,
            )
        )

    bundle_path = out if out is not None else out_dir / "handoff.md"
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(_bundle(briefs), encoding="utf-8")

    report = HandoffReport(briefs=briefs, bundle_path=bundle_path)
    if to_role is not None or task_ref is not None:
        report.note_posted, report.note_skipped_reason = _post_note(
            bundle_path, briefs, to_role=to_role, task_ref=task_ref
        )
    return report


def _locate(projects_dir: Path, ref: str) -> Path:
    """The transcript for a session id (prefixes fine, across all cwd slugs)."""
    if not projects_dir.is_dir():
        raise SessionNotFoundError(ref, projects_dir)
    matches = sorted(projects_dir.glob(f"*/{ref}*.jsonl"))
    if not matches:
        raise SessionNotFoundError(ref, projects_dir)
    exact = [match for match in matches if match.stem == ref]
    if exact:
        return exact[0]
    if len(matches) > 1:
        raise AmbiguousSessionError(ref, matches)
    return matches[0]


def _digest(transcript: Path) -> tuple[str, int]:
    """A bounded plain-text digest of one transcript, and its turn count.

    Each JSONL line is one event; unparseable lines are skipped rather than
    fatal — a live session may be appending mid-read, and one torn line must
    not cost the other thousand.
    """
    lines: list[str] = []
    turns = 0
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            kind = event.get("type")
            message = event.get("message") or {}
            content = message.get("content")
            if kind == "user":
                turns += _emit_user(lines, content)
            elif kind == "assistant":
                _emit_assistant(lines, content)
    return _fold("\n".join(lines), _DIGEST_CAP), turns


def _emit_user(lines: list[str], content: object) -> int:
    """Append user text and tool results; return 1 for a real user turn."""
    if isinstance(content, str):
        if content.strip():
            lines.append(f"USER: {_clip(content)}")
            return 1
        return 0
    turns = 0
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            lines.append(f"USER: {_clip(str(block['text']))}")
            turns = 1
        elif block.get("type") == "tool_result":
            lines.append(f"TOOL RESULT: {_clip(_flatten(block.get('content')), 300)}")
    return turns


def _emit_assistant(lines: list[str], content: object) -> None:
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            lines.append(f"ASSISTANT: {_clip(str(block['text']))}")
        elif block.get("type") == "tool_use":
            payload = json.dumps(block.get("input", {}), ensure_ascii=False)
            lines.append(f"TOOL {block.get('name', '?')}: {_clip(payload, 300)}")


def _flatten(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(part for part in parts if part)
    return ""


def _clip(text: str, cap: int = _BLOCK_CAP) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= cap else flat[: cap - 1] + "…"


def _fold(digest: str, cap: int) -> str:
    """Keep the head and tail of an oversized digest; say what was folded."""
    if len(digest) <= cap:
        return digest
    head = digest[: cap // 2]
    tail = digest[-(cap // 2) :]
    return f"{head}\n\n[… middle of the conversation folded for length …]\n\n{tail}"


def _brief(digest: str, *, use_llm: bool) -> tuple[str, bool]:
    if use_llm:
        distilled = _distill_llm(digest)
        if distilled is not None:
            return distilled, True
    return _structural_brief(digest), False


def _distill_llm(digest: str) -> str | None:
    """Distill via the local ``claude`` binary in print mode; ``None`` on any failure.

    Failure here is ordinary (no binary on PATH, rate-limited account, timeout)
    and the caller has a real fallback, so this returns ``None`` instead of
    raising — the brief still ships, just structural.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", _BRIEF_PROMPT + digest],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LLM_TIMEOUT_SECONDS,
            check=False,
            env=untraced_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _structural_brief(digest: str) -> str:
    """A no-model brief: first and last exchanges plus the (bounded) digest.

    Honest about what it is — the receiving agent gets the material to orient
    from rather than a summary pretending someone read it.
    """
    lines = digest.splitlines()
    user_lines = [line for line in lines if line.startswith("USER: ")]
    first = user_lines[0][len("USER: ") :] if user_lines else "(no user prompt captured)"
    last = user_lines[-1][len("USER: ") :] if user_lines else "(no user prompt captured)"
    return (
        "## Goal\n"
        f"First ask: {_clip(first, 500)}\n\n"
        "## Current state\n"
        f"Latest ask: {_clip(last, 500)}\n\n"
        "## Conversation digest\n"
        "No model was available to distill this session — read the digest "
        "below (oldest first) and orient from it directly.\n\n"
        "```\n" + _fold(digest, 12_000) + "\n```\n"
    )


def _bundle(briefs: list[SessionBrief]) -> str:
    lines = [
        "# Handoff",
        "",
        f"{len(briefs)} session(s) distilled. Read every brief before starting.",
        "",
    ]
    for brief in briefs:
        lines.append(f"- `{brief.session_id}` ({brief.turns} turns): {brief.brief_path}")
        if brief.raw_path is not None:
            lines.append(f"  - raw transcript digest: {brief.raw_path}")
    lines.append("")
    for brief in briefs:
        lines.append(brief.brief_path.read_text(encoding="utf-8").rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _post_note(
    bundle_path: Path,
    briefs: list[SessionBrief],
    *,
    to_role: str | None,
    task_ref: str | None,
) -> tuple[bool, str | None]:
    """Point the team pipe at the bundle; never let the pipe cost the briefs."""
    ids = ", ".join(brief.session_id[:12] for brief in briefs)
    text = f"handoff brief ready ({ids}): read {bundle_path} before starting"
    try:
        team_service.add_note(text, task_ref=task_ref, to_role=to_role)
    except team_service.TeamDisabledError:
        return False, "team mode is off (aisquare team on) — briefs written, note not posted"
    except (KeyError, ValueError) as exc:
        return False, f"note not posted: {exc} — briefs written"
    return True, None
