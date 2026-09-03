"""A dated staging exception: deliver as the descriptor would, once it says so.

The branch is descriptor-gated by design: the hooks call only the triggers
``delivery[].hook_push`` lists and the recall tool exists only when ``mcp_pull``
is listed. The staging server (``aisquare-ci`` ``main`` ``4cb104b``, live
2026-09-02) still publishes the constant pre-CLI list ``[{"kind": "direct_api"}]``
for every run, so against it every prompt records ``trigger_not_in_descriptor``
and no hook call is ever made — the correct reading of that descriptor, and
useless for testing the wire.

This module is the interim the live-wiring handoff (§2 B) allows, and it must
read as one. With

    AISQUARE_CI_DELIVERY_OVERRIDE=hook_push:session_start,prompt_submit;mcp_pull

exported, the fetched descriptor's delivery list is replaced by the members
named — **only when the fetched descriptor is** ``direct_api``**-only**. Four
things make it impossible to mistake for the descriptor's ruling:

- every metric row and every join record carries ``delivery_source``
  (``descriptor`` or ``override``), CHECK-constrained, so rows the two produce
  can never be summed by accident;
- ``doctor`` warns on its own line whenever the override is in effect, and
  says so when it is set but ignored;
- it never applies when the descriptor lists real modes, and a spec this
  module cannot parse is ignored (the descriptor rules) rather than guessed at;
- it is never written to the descriptor cache — the cache holds what the
  server said, nothing else.

It is removed, or demoted to a test-only seam, once the server publishes real
delivery modes (one constant: ``app/api/runs.py::DIRECT_API_DELIVERY``). The
design rejected client-side delivery flags because they are "a second place the
experiment's shape lives"; that argument stands. This is a connectivity
instrument for a host whose runs are ``comparison_eligible: false`` anyway, and
a row it produced must never be read as a measurement.

Environment only, like the token: there is deliberately no config field.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import ValidationError

from aisquare.models import DeliverySource
from aisquare.services.ci_contract import (
    RECALL_TOOL,
    DeliveryDescriptor,
    DeliveryMode,
    HookPushDelivery,
    McpPullDelivery,
    clip,
    first_error,
)

ENV_VAR = "AISQUARE_CI_DELIVERY_OVERRIDE"

HOOK_ENDPOINT = "/v1/hook"
"""Where a ``hook_push`` member points. The server's route (``app/api/delivery.py``);
the spec names triggers only, because there is exactly one endpoint to name."""

EXAMPLE = "hook_push:session_start,prompt_submit;mcp_pull"
"""The spec that stands in for the delivery list the server will publish (§2 A)."""


@dataclass(frozen=True)
class Ruling:
    """Which document decides delivery for this gate, and why."""

    descriptor: DeliveryDescriptor
    source: DeliverySource
    detail: str = ""
    """One sentence for ``doctor``: empty when the variable is unset."""

    @property
    def active(self) -> bool:
        return self.source == "override"


def requested() -> str:
    """The raw spec from the environment, or ``""``."""
    return os.environ.get(ENV_VAR, "").strip()


def direct_api_only(descriptor: DeliveryDescriptor) -> bool:
    """The precondition: the server said ``direct_api`` and nothing else."""
    return all(mode.kind == "direct_api" for mode in descriptor.delivery)


def apply(descriptor: DeliveryDescriptor) -> Ruling:
    """The descriptor as the gate should read it. Never raises.

    Unset → the descriptor rules. Set against a descriptor that lists real
    modes → the descriptor rules, and the ruling says the variable was ignored.
    Set against ``direct_api`` only → the override rules if it parses; if it
    does not, the descriptor rules and the ruling names the fault.
    """
    spec = requested()
    if not spec:
        return Ruling(descriptor, "descriptor")
    if not direct_api_only(descriptor):
        return Ruling(
            descriptor,
            "descriptor",
            f"{ENV_VAR} is set but ignored — the descriptor lists real delivery modes",
        )
    try:
        overridden = with_delivery(descriptor, parse(spec))
    except ValidationError as exc:
        return Ruling(descriptor, "descriptor", _ignored(first_error(exc)))
    except ValueError as exc:
        return Ruling(descriptor, "descriptor", _ignored(str(exc)))
    return Ruling(
        overridden,
        "override",
        f"{ENV_VAR} is active: {describe(overridden)} — the descriptor said direct_api only",
    )


def parse(spec: str) -> list[DeliveryMode]:
    """``hook_push:t1,t2;mcp_pull`` → delivery members. Raises ``ValueError`` naming the fault.

    Each member is built through the contract's own model, so a trigger the
    contract does not know is refused by the same validator that would refuse
    it in a descriptor.
    """
    members: list[DeliveryMode] = []
    for part in (piece.strip() for piece in spec.split(";")):
        if not part:
            continue
        kind, _, rest = part.partition(":")
        kind = kind.strip()
        if kind == "hook_push":
            triggers = [trigger.strip() for trigger in rest.split(",") if trigger.strip()]
            members.append(
                HookPushDelivery.model_validate(
                    {"kind": "hook_push", "triggers": triggers, "endpoint": HOOK_ENDPOINT}
                )
            )
        elif kind == "mcp_pull":
            if rest.strip():
                raise ValueError(f"mcp_pull takes no arguments, got {clip(repr(rest.strip()), 40)}")
            members.append(McpPullDelivery(kind="mcp_pull", tool=RECALL_TOOL))
        else:
            raise ValueError(
                f"unknown delivery kind {clip(repr(kind), 40)} (hook_push or mcp_pull)"
            )
    if not members:
        raise ValueError("no delivery members")
    return members


def with_delivery(
    descriptor: DeliveryDescriptor, members: list[DeliveryMode]
) -> DeliveryDescriptor:
    """``descriptor`` with ``members`` as its delivery list, re-validated whole —
    the contract's own rules (one member per kind, ``direct_api`` alone) apply."""
    raw = descriptor.model_dump(mode="json")
    raw["delivery"] = [member.model_dump(mode="json") for member in members]
    return DeliveryDescriptor.model_validate(raw)


DIRECT_API_NOTE = "direct_api only — the hooks will not call"
"""What ``describe`` says of a ``direct_api``-only list when nothing overrides it."""


def describe(descriptor: DeliveryDescriptor, *, direct_api_note: str = DIRECT_API_NOTE) -> str:
    """``hook_push on a, b; mcp_pull (tool)`` — the delivery list as ``doctor`` prints it.

    ``direct_api_note`` is the sentence for a list with neither; the caller
    that knows an override is in effect passes one that does not promise
    silence the hooks will not keep.
    """
    modes: list[str] = []
    push = descriptor.hook_push
    if push is not None:
        modes.append(f"hook_push on {', '.join(push.triggers)}")
    if descriptor.mcp_pull is not None:
        modes.append(f"mcp_pull ({descriptor.mcp_pull.tool})")
    if not modes:
        modes.append(direct_api_note)
    return "; ".join(modes)


def _ignored(fault: str) -> str:
    """Bounded like every other detail: the value came from the environment,
    and ``doctor`` output is the most pasteable artefact there is."""
    return f"{ENV_VAR} is set but ignored — {clip(fault)}; expected e.g. {EXAMPLE}"
