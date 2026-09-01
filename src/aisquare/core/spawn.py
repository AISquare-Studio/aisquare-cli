"""Every place this CLI starts a process, and whether it carries an identity.

Tracing identity is PROCESS-level: it rides in ``ANTHROPIC_BASE_URL`` plus the
``X-Agent-Name``/``X-Pipeline-Id`` pair in ``ANTHROPIC_CUSTOM_HEADERS``, and a
child inherits the parent's environment unless told otherwise. That makes every
``subprocess``/``exec`` call in this package a decision — traced or excluded —
and an *undecided* one is not neutral: a probe subprocess that inherits a real
role's identity mints a junk Run under that role and corrupts the dataset the
morning experiments measure.

So the decisions are written down here rather than left implicit, and
``tests/test_spawn_seams.py`` walks the AST of this package on every run to
assert that ``SEAMS`` still names every call site that exists. A docstring
inventory drifts silently the first time someone adds a ``subprocess.run``;
this one fails the build instead. That is the whole reason the registry lives
in ``src`` and not in the test: it is the artefact, the test is only the latch.

``untraced_env`` is how a seam acts on an "excluded" ruling.

THE INVENTORY (see ``SEAMS`` for the machine-readable copy)

Traced — these ARE the agent, and are meant to carry an identity:
  * ``cli/launch.py::_exec`` — ``aisquare launch`` replaces itself with the
    agent. Wired by ``services.explainability.wire_session``.
  * ``cli/team.py::spawn`` — ``aisquare team spawn --exec``, same wiring.

Excluded, and actively stripped — these talk to a model or to us, and would
otherwise inherit a live identity:
  * ``core/harness.py::probe_model`` — runs ``claude -p`` once per alias to
    check entitlement. A real LLM process, but NOT a session: it is a
    yes/no question about the account, and a Run for it is pure noise
    attributed to whoever happened to be probing.
  * ``core/brain.py::_run`` — the gbrain worker. Not an agent session, and its
    own ``_env`` already contemplates an Anthropic key path
    (``ANTHROPIC_API_KEY`` is popped only when embeddings are off), so an
    inherited base URL could route it through our proxy under the parent's
    name.
  * ``services/distill.py::spawn_drain`` — a detached ``aisquare team distill``
    of our own. A background worker of ours is not an agent session and has no
    business wearing one's identity.

Excluded, nothing stripped — these are not model processes at all, and
narrowing their environment would be change without a reason:
  * ``core/brain.py::gbrain_version`` — ``gbrain --version``, a string.
  * ``core/snapshot.py::head_sha`` and ``core/workspace.py::git_common_root`` —
    ``git rev-parse``.
  * ``core/snapshot.py::_run_repomix`` — repomix packs files; no model.
  * ``core/editor.py::edit_text`` — the operator's ``$EDITOR``. It is theirs,
    and it should get their environment.
  * ``cli/watch.py::action_open_transcript`` — a pager/viewer on a file.
  * ``services/explainability_ops.py::install_sdk`` — ``pip install``.
  * ``services/explainability_ops.py::sdk_doctor`` — the SDK's own doctor
    script. Not stripped: it needs the ``EXPLAINABILITY_*`` environment to
    diagnose the machine it is running on.

Checked and NOT a seam, recorded so the next reader does not re-derive it:
  * ``services/project.py`` — catches ``subprocess.SubprocessError`` but starts
    nothing itself; it is a caller of ``core/snapshot.py``'s seams.

NOT a spawn seam, and not fixable here: Claude Code subagents (``Task``) and
Workflow agents run IN-PROCESS inside one agent and inherit its environment
verbatim, so they collapse into the parent's identity — one pipeline session,
one trace, one AGENT span (runner receipt, 2026-08-17). Process is the identity
boundary. Nothing in this module can change that; it is a property of where
those agents run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: Environment that carries a tracing identity. A child that is not itself the
#: traced agent must not inherit these. Kept beside the seam registry rather
#: than imported from ``services.explainability`` because ``core`` does not
#: depend on ``services`` — ``tests/test_spawn_seams.py`` pins the two against
#: each other so they cannot drift apart.
TRACING_ENV_VARS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")

TRACED = "traced"
EXCLUDED = "excluded"


@dataclass(frozen=True)
class Seam:
    """One process-spawn site and the ruling on it."""

    decision: str
    reason: str
    #: True when the seam actively removes the identity rather than merely not
    #: adding one. Only meaningful for ``EXCLUDED``.
    strips_identity: bool = False


#: Every ``subprocess``/``os.exec*`` call site in this package, keyed by
#: ``<path under src>::<enclosing function>``. Line numbers are deliberately
#: not part of the key: they move on every edit, and a guard that fails on
#: reformatting is a guard people learn to silence.
SEAMS: dict[str, Seam] = {
    "aisquare/cli/launch.py::_exec": Seam(
        TRACED, "the launch seam — this process BECOMES the agent"
    ),
    "aisquare/cli/team.py::spawn": Seam(
        TRACED, "the spawn seam (--exec) — this process BECOMES the agent"
    ),
    "aisquare/core/harness.py::probe_model": Seam(
        EXCLUDED,
        "runs `claude -p` to test an entitlement; a Run for it is junk data "
        "attributed to whoever was probing",
        strips_identity=True,
    ),
    "aisquare/core/brain.py::_run": Seam(
        EXCLUDED,
        "the gbrain worker — not an agent session, and it has an Anthropic key "
        "path that an inherited base URL would redirect through our proxy",
        strips_identity=True,
    ),
    "aisquare/services/distill.py::spawn_drain": Seam(
        EXCLUDED,
        "a detached `aisquare team distill` of ours — a background worker is not an agent session",
        strips_identity=True,
    ),
    "aisquare/core/brain.py::gbrain_version": Seam(EXCLUDED, "`gbrain --version`, a string"),
    "aisquare/core/snapshot.py::head_sha": Seam(EXCLUDED, "`git rev-parse HEAD`"),
    "aisquare/core/snapshot.py::_run_repomix": Seam(EXCLUDED, "repomix packs files; no model"),
    "aisquare/core/workspace.py::git_common_root": Seam(EXCLUDED, "`git rev-parse`"),
    "aisquare/core/editor.py::edit_text": Seam(
        EXCLUDED, "the operator's $EDITOR — it is theirs, it gets their environment"
    ),
    "aisquare/cli/watch.py::action_open_transcript": Seam(
        EXCLUDED, "a pager/viewer on a transcript file"
    ),
    "aisquare/services/handoff.py::_distill_llm": Seam(
        EXCLUDED,
        "runs `claude -p` once to distill a transcript into a handoff brief; "
        "a Run for the distillation would sit beside the Runs of the sessions "
        "being distilled and read as a phantom sixth session",
        strips_identity=True,
    ),
    "aisquare/services/explainability_ops.py::install_sdk": Seam(
        EXCLUDED, "`pip install` — reaches PyPI, never the model API"
    ),
    "aisquare/services/explainability_ops.py::sdk_doctor": Seam(
        EXCLUDED,
        "the SDK's own `explainability-doctor` console script. Deliberately NOT "
        "stripped: it talks to the GATEWAY, not the model API, and it needs the "
        "EXPLAINABILITY_* environment to answer at all — stripping would make "
        "the diagnostic lie about the machine it is diagnosing",
    ),
}


def untraced_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``base`` (default the current environment) without the tracing identity.

    A plain copy minus :data:`TRACING_ENV_VARS`. Never mutates ``base``, and
    never raises — this runs on paths whose whole contract is that they degrade
    quietly, and a child losing two variables it was not entitled to is not a
    failure worth reporting.
    """
    source = os.environ if base is None else base
    return {key: value for key, value in source.items() if key not in TRACING_ENV_VARS}
