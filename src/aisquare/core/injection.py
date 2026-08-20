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
        lines += [_bullet(entry.text) for entry in user]
        lines.append("")
    if project_entries:
        lines.append(f"## Project: {project.root.name or project.id}")
        lines += [_bullet(entry.text) for entry in project_entries]
        lines.append("")
    if not user and not project_entries:
        lines += ["_No saved context yet._", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _bullet(text: str) -> str:
    """Render entry text as one Markdown list item, indenting continuation lines."""
    first, *rest = text.splitlines() or [""]
    return "\n".join([f"- {first}", *(f"  {line}" for line in rest)])


def build_retrieved_block(context: str, sources: list[str]) -> str:
    """Frame CI-retrieved documents as candidates, not as fact or instruction.

    The wording here is part of the experiment, not decoration. This material
    was retrieved by a machine against a prompt and the agent did not fetch it,
    so it may be irrelevant or wrong. Rendered as plain context it reads as
    established fact and gets acted on unchecked; rendered as an instruction it
    steers the agent instead of informing it. Labelling it as candidate
    reference with its sources attached keeps a bad retrieval visible in the
    transcript — the agent can open the cited file and disagree — rather than
    silently absorbed into the answer.
    """
    lines = [
        "## Possibly relevant (retrieved by aisquare — you did not fetch this)",
        "",
        "Candidate reference material for this prompt. It may be incomplete or",
        "wrong. Prefer it over exploring blind, but open the cited source before",
        "relying on anything from it.",
        "",
        context.strip(),
    ]
    if sources:
        lines += ["", "Sources: " + ", ".join(dict.fromkeys(sources))]
    return "\n".join(lines).rstrip("\n") + "\n"


def record_retrieval(*, project_id: str, context: str, sources: list[str]) -> InjectionRecord:
    """Persist what CI contributed to a turn, so ``why`` can account for it.

    Written only when there is something to record. The hook path runs in front
    of a waiting developer, and a file write on every prompt to note that
    nothing happened is a cost with no reader.
    """
    record = InjectionRecord(
        injected_at=datetime.now(tz=UTC),
        project_id=project_id,
        retrieved_chars=len(context),
        retrieved_sources=list(dict.fromkeys(sources)),
    )
    paths.ensure_home()
    paths.last_injection_path().write_text(record.model_dump_json(indent=2), encoding="utf-8")
    return record


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
