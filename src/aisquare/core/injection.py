"""Assemble in-scope context into a block for an agent, and record injections.

Today the "agent session" is just stdout — ``inject`` writes the block there so
it can be piped or, later, consumed by an installed agent hook (``agents
connect``). Selection is currently "everything in scope" (the user pool plus the
current project); relevance ranking comes later.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aisquare.core import paths
from aisquare.models import ContextEntry, InjectionRecord, ProjectInfo


def build_block(entries: list[ContextEntry], project: ProjectInfo) -> str:
    """Render in-scope ``entries`` into the Markdown context block for an agent."""
    user = [entry for entry in entries if entry.pool == "user"]
    project_entries = [entry for entry in entries if entry.pool == "project"]
    lines = ["# Context (via aisquare)", ""]
    if user:
        lines.append("## Your preferences")
        lines += [f"- {entry.text}" for entry in user]
        lines.append("")
    if project_entries:
        lines.append(f"## Project: {project.root.name or project.id}")
        lines += [f"- {entry.text}" for entry in project_entries]
        lines.append("")
    if not user and not project_entries:
        lines += ["_No saved context yet._", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def record_injection(entries: list[ContextEntry], project: ProjectInfo) -> InjectionRecord:
    """Persist a record of an injection and return it."""
    record = InjectionRecord(
        injected_at=datetime.now(tz=UTC),
        project_id=project.id,
        user_count=sum(1 for entry in entries if entry.pool == "user"),
        project_count=sum(1 for entry in entries if entry.pool == "project"),
        entry_ids=[entry.id for entry in entries],
    )
    paths.ensure_home()
    paths.last_injection_path().write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return record


def load_last() -> InjectionRecord | None:
    """Return the most recent injection record, or ``None`` if there is none."""
    path = paths.last_injection_path()
    if not path.exists():
        return None
    return InjectionRecord.model_validate_json(path.read_text(encoding="utf-8"))
