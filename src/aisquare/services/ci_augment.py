"""Putting CI-retrieved documents in front of the agent, before it explores.

This is the experiment. When a developer submits a prompt, ``UserPromptSubmit``
fires *before* Claude processes it, and whatever that hook prints becomes
context the model can see — one of only three events Claude Code treats that
way. So the shape is: capture the prompt, ask the CI endpoint whether it knows
anything relevant, and if it does, hand those documents over as candidate
reference material. The hypothesis is that an agent which starts better
informed explores less.

Two properties this module exists to protect.

**The hook is synchronous.** The developer has hit enter and is watching a
cursor. Everything here runs inside that wait, which is why the master switch
short-circuits before any work at all and why nothing retries.

**The framing is load-bearing.** These documents were retrieved by a machine
against a prompt, not fetched by the agent, and they may be wrong. Presented as
plain context they read as established fact and the agent will act on them
without checking; presented as instructions they steer it. They are labelled as
candidates with their sources attached so the agent can verify before relying
on anything — which also makes a bad retrieval visible in the transcript rather
than silently absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass

from aisquare.core.config import load_config
from aisquare.core.ids import new_trace_id
from aisquare.core.injection import build_retrieved_block, record_retrieval
from aisquare.services import ci_client
from aisquare.services.ci_contract import ADVISORY_BUDGET_MS, Action, Trigger


@dataclass(frozen=True)
class Augmentation:
    """What one CI consultation produced, and what it cost."""

    block: str = ""
    """Context to put in front of the agent; empty when there is nothing."""

    call: ci_client.Call | None = None
    """The call, for the metrics row. ``None`` when CI was never asked, which
    is what every turn looks like until someone opts in."""

    trace_id: str | None = None


def push_enabled() -> bool:
    """Whether retrieved context may be injected at all.

    The master switch is checked first and separately: with the experiment off
    this must not read config, so that the default state costs nothing on a
    path a developer is waiting on.
    """
    if not ci_client.enabled():
        return False
    try:
        return load_config().experiment.push
    except Exception:  # a broken config must not enable anything
        return False


def for_prompt(prompt: str | None, *, project_id: str, session_id: str | None) -> Augmentation:
    """Ask CI what is relevant to ``prompt``; returns the block to inject.

    Nothing returned means the turn proceeds exactly as it does today — the
    agent explores as it always has, and the only trace is a metrics row.
    """
    if not push_enabled() or not prompt or not prompt.strip():
        return Augmentation()
    trace_id = new_trace_id()
    call = ci_client.call(
        Trigger.prompt_submit,
        session_id=session_id or "",
        trace_id=trace_id,
        project_id=project_id,
        prompt=prompt,
        budget_ms=ADVISORY_BUDGET_MS,
    )
    return Augmentation(block=_block_for(call, project_id), call=call, trace_id=trace_id)


def for_session_start(*, project_id: str, session_id: str | None) -> Augmentation:
    """Warm the cache and collect anything worth injecting at session start.

    The response is cached under whatever key the server chose. Whether a later
    ``prompt_submit`` can *hit* that entry is a server-side scoping decision
    and is still open — see the cache-key entry in ``docs/ci-contract.md``.
    Until it is settled, every prompt makes a live call; the mechanism is built
    and tested, the policy is not ours to pick.
    """
    if not push_enabled():
        return Augmentation()
    trace_id = new_trace_id()
    call = ci_client.call(
        Trigger.session_start,
        session_id=session_id or "",
        trace_id=trace_id,
        project_id=project_id,
        budget_ms=ADVISORY_BUDGET_MS,
    )
    return Augmentation(block=_block_for(call, project_id), call=call, trace_id=trace_id)


def _block_for(call: ci_client.Call, project_id: str) -> str:
    """Render an ``inject`` response, and record it so ``why`` can explain it.

    Anything else — a degraded call, a ``noop``, an ``inject`` with no body —
    produces nothing. A degraded call reaches here as ``allow`` by construction,
    so this needs no error handling of its own.
    """
    if call.action is not Action.inject:
        return ""
    context = call.outcome.response.context
    if not context or not context.strip():
        return ""
    sources = [item.source for item in call.outcome.response.provenance]
    record_retrieval(project_id=project_id, context=context, sources=sources)
    return build_retrieved_block(context, sources)
